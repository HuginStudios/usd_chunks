"""Running a simulation bake without stopping the host, and keeping its result.

A bake reads the scene, solves, and writes the result. Reading and writing are USD and belong
on the main thread; the solve is the long part and touches no USD. A bake is a generator
function that says where each part may run:

``` py
def bake(ctx):
    job = gather(stage)                 # main thread: read the scene
    yield sim_bake.WORKER
    result = solve(job, progress=ctx.progress, cancelled=ctx.cancelled)
    yield sim_bake.MAIN
    write(stage, result)                # main thread: author the result
    return result

sim_bake.run(node, bake, report=report, finish=finish)
```

In a UI host with an event loop, run() returns at once: the WORKER part runs on a thread, the
rest on the main thread, and finish(result) is called on the main thread at the end.
Headless, under test, or without Qt there is no loop to come back to, so the bake runs straight
through and run() returns when it is done.

An engine that holds the GIL while it computes still stops the UI from a thread: Taichi held it
for over a second at a time. in_subprocess() runs one solve function in a separate Python
process instead, from inside the WORKER part.

The bake's layer: promote() puts it in front of the chunk layers that authored the same prims,
so the baked samples win, and keep() / restore() carry a bake across the framework's
clear-before-cook, since a bake is too expensive to redo on every cook.
"""

from __future__ import annotations

import ctypes
import hashlib
import importlib.util
import logging
import os
import pickle
import queue
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Generator
from pathlib import Path

from pxr import Sdf, Usd

from usd_chunks import chunk_registry, cook_cache

try:
    from PySide6 import QtCore
except ImportError:  # pragma: no cover - headless install without Qt
    QtCore = None

try:
    from ctypes import wintypes
except (ImportError, ValueError):  # pragma: no cover - not Windows
    wintypes = None

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy comes with every solver
    np = None

logger = logging.getLogger(__name__)


class _Marker:
    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:
        return f"sim_bake.{self.name}"


#: Yield from a bake: what follows touches no USD and may run on a worker thread.
WORKER = _Marker("WORKER")
#: Yield from a bake: what follows reads or writes USD and runs on the main thread.
MAIN = _Marker("MAIN")

_RUNNING = "_sim_bake_running"
_CANCEL = "_sim_bake_cancel"
_KEPT = "_sim_bake_kept"
_INPUT_KEY = "_sim_bake_input_key"

#: How often progress reaches the readout, at most.
_PROGRESS_EVERY_S = 0.25

#: Marks the lines in_subprocess's child prints for its parent; anything else (an engine's own
#: banner) is passed through to the log.
_LINE = "@@sim_bake "


class Cancelled(Exception):
    """The bake was cancelled (cancel(node))."""


def in_background() -> bool:
    """Whether bakes run on a worker: only in a host with a running Qt event loop.

    That loop is the only way back to the main thread. Headless (cli), under test and in a
    script (Scene(), host "script") there is none, so the bake runs inline and is finished when
    run() returns -- even if the script made a QApplication, which does not run its loop.
    """
    if QtCore is None or QtCore.QCoreApplication.instance() is None:
        return False
    return chunk_registry.ChunkRegistry().plugin not in _INLINE_HOSTS


#: Hosts with no running event loop: bakes run inline there.
_INLINE_HOSTS = (None, "", "cli", "pytest", "script")


def is_running(node) -> bool:
    """Whether node has a bake in flight."""
    return bool(getattr(node, _RUNNING, False))


def cancel(node) -> None:
    """Ask node's running bake to stop. It stops at its next step or progress report."""
    setattr(node, _CANCEL, True)


class Context:
    """What a bake gets: progress, cancellation and live results, safe from any thread."""

    def __init__(self, node, report: Callable[[str], None], live: Callable | None, relay=None):
        self.node = node
        self._report = report
        self._live = live
        self._relay = relay
        self._started = time.perf_counter()
        self._first_frame = None
        self._last_report = 0.0
        self._latest = None

    @property
    def inline(self) -> bool:
        """True when the bake runs straight through on the calling thread."""
        return self._relay is None

    def cancelled(self) -> bool:
        """Whether cancel(node) was called."""
        return bool(getattr(self.node, _CANCEL, False))

    def check(self) -> None:
        """Raise Cancelled if the bake was cancelled."""
        if self.cancelled():
            raise Cancelled

    def status(self, text: str) -> None:
        """Show text in the readout."""
        self._call(lambda: self._report(text))

    def progress(self, done: int, total: int) -> None:
        """Report frame done (0-based) of total, with the time left once it can be guessed."""
        now = time.perf_counter()
        if self._first_frame is None:
            self._first_frame = now
        last = done >= total - 1
        if not last and now - self._last_report < _PROGRESS_EVERY_S:
            return
        self._last_report = now
        text = f"simulating... frame {done + 1} of {total}"
        if done > 0:
            left = (now - self._first_frame) / done * (total - done - 1)
            text += f" · about {left:.0f}s left"
        self.status(text)

    def live(self, partial) -> None:
        """Hand the frames done so far to the live callback, on the main thread.

        Only the newest matters: one still queued when the next arrives is dropped.
        """
        if self._live is None or self._relay is None:
            return
        self._latest = partial

        def apply():
            latest, self._latest = self._latest, None
            if latest is not None and is_running(self.node):
                self._live(latest)

        self._relay.call.emit(apply)

    def _call(self, fn: Callable) -> None:
        if self._relay is None or threading.current_thread() is threading.main_thread():
            fn()
        else:
            self._relay.call.emit(fn)


if QtCore is not None:

    class _Relay(QtCore.QObject):
        """Runs callables on the thread it was made on -- the main one -- when a worker emits
        them. A queued signal, because QTimer.singleShot fired from a worker thread without a
        main-thread context never runs."""

        call = QtCore.Signal(object)

        def __init__(self):
            super().__init__()
            self.call.connect(self._invoke, QtCore.Qt.QueuedConnection)

        def _invoke(self, fn):
            fn()


def run(node, bake: Callable[[Context], Generator], *, report: Callable[[str], None],
        finish: Callable | None = None, live: Callable | None = None, name: str = "SimBake") -> None:
    """Run bake(ctx) for node: inline, or with its WORKER parts on a thread (see the module).

    Args:
        node: The chunk baking. One bake at a time per chunk.
        bake: A generator function taking a Context; its return value is the result.
        report: Shows a line of text, e.g. in the chunk's readout. Called on the main thread.
        finish: Called with the result on the main thread when the bake is done.
        live: Called with partial results (Context.live) on the main thread while it runs.
        name: The worker thread's name.
    """
    if is_running(node):
        report("already simulating -- wait, or press Cancel")
        return
    setattr(node, _CANCEL, False)

    if not in_background():
        ctx = Context(node, report, None)
        try:
            result = drive(bake(ctx))
        except Cancelled:
            report("cancelled")
            return
        except Exception as e:  # noqa: BLE001 - a failed bake reports, it does not take the host down
            logger.exception("Simulation failed")
            report(f"simulation failed: {e}")
            return
        if finish is not None:
            finish(result)
        return

    relay = _Relay()
    # Held on the chunk: the worker's emits need the relay alive until the last one lands.
    node._sim_bake_relay = relay
    ctx = Context(node, report, live, relay)
    gen = bake(ctx)
    setattr(node, _RUNNING, True)

    def end():
        setattr(node, _RUNNING, False)

    def failed(error: BaseException):
        end()
        if isinstance(error, Cancelled) or ctx.cancelled():
            report("cancelled")
            return
        logger.error("Simulation failed", exc_info=error)
        report(f"simulation failed: {error}")

    def done(result):
        end()
        if finish is None:
            return
        try:
            finish(result)
        except Exception as e:  # noqa: BLE001
            logger.exception("Writing the simulation failed")
            report(f"writing the result failed: {e}")

    def on_main():
        try:
            finished, value = _phase(gen, ctx, WORKER)
        except BaseException as e:  # noqa: BLE001 - reported, see failed()
            failed(e)
            return
        if finished:
            done(value)
            return
        threading.Thread(target=on_worker, name=name, daemon=True).start()

    def on_worker():
        try:
            finished, value = _phase(gen, ctx, MAIN)
        except BaseException as e:  # noqa: BLE001
            relay.call.emit(lambda e=e: failed(e))
            return
        relay.call.emit((lambda: done(value)) if finished else on_main)

    on_main()


def drive(gen: Generator):
    """Run a bake generator straight through, ignoring where it says it may run.

    Returns:
        The generator's return value.
    """
    while True:
        try:
            next(gen)
        except StopIteration as e:
            return e.value


def _phase(gen: Generator, ctx: Context, stop: _Marker) -> tuple[bool, object]:
    """Step gen until it yields stop or ends. Returns (ended, return value)."""
    while True:
        if ctx.cancelled():
            gen.close()
            raise Cancelled
        try:
            step = next(gen)
        except StopIteration as e:
            return True, e.value
        if step is stop:
            return False, None


# ------------------------------------------------------------------------------------------
# A solve in its own process
# ------------------------------------------------------------------------------------------


def in_subprocess(ctx: Context, function: Callable, **kwargs):
    """Call function(progress=..., **kwargs) in a separate Python process; return its result.

    For an engine that holds the GIL while it computes, which stops the UI even from a
    thread. function must be a module-level function of a module that can be loaded from its
    file in a fresh interpreter, and kwargs and the result must pickle. function is called
    with progress(done, total), which reaches ctx.progress. Cancelling kills the process.

    Inline (ctx.inline) it is a plain call in this process: no event loop is waiting.
    """
    if ctx.inline:
        return function(progress=ctx.progress, **kwargs)

    folder = Path(tempfile.mkdtemp(prefix="sim_bake_"))
    job, out = folder / "job.pickle", folder / "result.pickle"
    with open(job, "wb") as f:
        pickle.dump(kwargs, f, protocol=pickle.HIGHEST_PROTOCOL)
    source = str(Path(sys.modules[function.__module__].__file__).resolve())
    # The child must import this same usd_chunks. A host started in a folder holding another
    # copy (usdview launched from C:\usd_chunks while running a worktree) found that one first:
    # "No module named usd_chunks.sim_bake". So the folder holding this package goes first on
    # its path, and it starts in the temporary folder, where there is nothing to find.
    root = str(Path(__file__).resolve().parent.parent)
    paths = [root] + [p for p in sys.path if p and p != root]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(paths))
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        [sys.executable, "-m", "usd_chunks.sim_bake", source, function.__name__, str(job), str(out)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, creationflags=flags,
        cwd=str(folder),
    )
    lines: queue.Queue = queue.Queue()
    errors: list[str] = []

    def read(stream, sink):
        for line in stream:
            sink(line)

    threading.Thread(target=read, args=(proc.stdout, lines.put), daemon=True).start()
    stderr_reader = threading.Thread(target=read, args=(proc.stderr, errors.append), daemon=True)
    stderr_reader.start()
    try:
        while True:
            if ctx.cancelled():
                proc.kill()
                raise Cancelled
            try:
                line = lines.get(timeout=0.1)
            except queue.Empty:
                if proc.poll() is not None and lines.empty():
                    break
                continue
            if line.startswith(_LINE):
                kind, *rest = line[len(_LINE):].split()
                if kind == "progress":
                    ctx.progress(int(rest[0]), int(rest[1]))
            elif line.strip():
                logger.debug("[%s] %s", function.__name__, line.rstrip())
        proc.wait()
        stderr_reader.join(timeout=5)
        if proc.returncode != 0:
            tail = "".join(errors[-20:]).strip().splitlines()
            raise RuntimeError(tail[-1] if tail else f"the solve exited with code {proc.returncode}")
        with open(out, "rb") as f:
            return pickle.load(f)
    finally:
        if proc.poll() is None:
            proc.kill()
        for path in (job, out):
            try:
                path.unlink()
            except OSError:
                pass
        try:
            folder.rmdir()
        except OSError:
            pass


def _opt_out_of_power_throttling() -> None:
    """Keep Windows from slowing this process down.

    A solver process has no window, so Windows treats it as background work and throttles it
    (EcoQoS) a few seconds in -- the same thing tests/conftest.py opts the test runner out of.
    A no-op elsewhere, or if Windows refuses.
    """
    if sys.platform != "win32" or wintypes is None:
        return

    class _State(ctypes.Structure):
        _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]

    try:
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        # ProcessPowerThrottling (4): control EXECUTION_SPEED (1), state 0 = never throttle.
        state = _State(1, 1, 0)
        kernel32.SetProcessInformation(kernel32.GetCurrentProcess(), 4, ctypes.byref(state), ctypes.sizeof(state))
    except (OSError, AttributeError):
        pass


def _child(source: str, function: str, job: str, out: str) -> None:
    """in_subprocess's other end: load the module, call the function, write the result."""
    _opt_out_of_power_throttling()
    spec = importlib.util.spec_from_file_location(f"_sim_bake_{Path(source).stem}", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    with open(job, "rb") as f:
        kwargs = pickle.load(f)

    def progress(done, total):
        print(f"{_LINE}progress {int(done)} {int(total)}", flush=True)

    result = getattr(module, function)(progress=progress, **kwargs)
    with open(out, "wb") as f:
        pickle.dump(result, f, protocol=pickle.HIGHEST_PROTOCOL)


# ------------------------------------------------------------------------------------------
# The cook cache
# ------------------------------------------------------------------------------------------


def digest(*parts) -> str:
    """A digest of everything that shapes a bake: dicts, lists and numpy arrays, all through."""
    h = hashlib.blake2b(digest_size=16)

    def feed(v):
        if isinstance(v, dict):
            for k in sorted(v, key=str):
                h.update(repr(k).encode())
                feed(v[k])
        elif isinstance(v, (list, tuple)):
            h.update(b"[")
            for x in v:
                feed(x)
            h.update(b"]")
        elif np is not None and isinstance(v, np.ndarray):
            h.update(str(v.shape).encode())
            h.update(str(v.dtype).encode())
            h.update(np.ascontiguousarray(v).tobytes())
        else:
            h.update(repr(v).encode())

    for part in parts:
        feed(part)
    return h.hexdigest()


def cache_entry(node, key: str):
    """The cook cache's entry for a bake made from inputs key, or None when it is off.

    A bake is made by Run, not by a cook, so the cook cache cannot work out its key: the
    module's cache_key(node) returns input_key(node), which is key only while this asks.
    Take the entry when the bake starts and entry.store() it when it lands.
    """
    setattr(node, _INPUT_KEY, key)
    try:
        return cook_cache.lookup(node)
    finally:
        setattr(node, _INPUT_KEY, None)


def input_key(node) -> str | None:
    """For a module's cache_key(node): the key cache_entry is asking with, else None.

    None the rest of the time means "cannot say", so an ordinary cook neither reads nor
    stores: working out a bake's inputs can take seconds.
    """
    return getattr(node, _INPUT_KEY, None)


# ------------------------------------------------------------------------------------------
# The bake's layer
# ------------------------------------------------------------------------------------------

_NO_DEFAULT = object()


def write_samples(attr: Usd.Attribute, samples, default=_NO_DEFAULT) -> None:
    """Author many time samples on attr at once, in its stage's edit target layer.

    attr.Set(value, time) once per frame sends one change notice per call, and everything
    listening -- the framework's scan for new chunks, usdview's prim tree -- runs for each:
    a breakable bake's live preview spent 3 s a time on 7,000 notices, all on the main thread.
    Written straight into the layer inside one change block, it is one notice.

    Args:
        attr: The attribute. Its spec in the edit target layer is made if it has none there
            (an instancer's positions authored by another chunk's layer).
        samples: (time, value) pairs; time a float or Usd.TimeCode.
        default: Also set the default value, when given.
    """
    target = attr.GetStage().GetEditTarget()
    layer = target.GetLayer()
    path = target.MapToSpecPath(attr.GetPath())
    with Sdf.ChangeBlock():
        if not layer.GetAttributeAtPath(path):
            owner = Sdf.CreatePrimInLayer(layer, path.GetPrimPath())
            Sdf.AttributeSpec(owner, path.name, attr.GetTypeName(), attr.GetVariability(), attr.IsCustom())
        for time_code, value in samples:
            t = time_code.GetValue() if isinstance(time_code, Usd.TimeCode) else float(time_code)
            layer.SetTimeSample(path, t, value)
        if default is not _NO_DEFAULT:
            layer.GetAttributeAtPath(path).default = default



def promote(layer: Sdf.Layer | None) -> None:
    """Make layer the strongest chunk layer, and its graph layer the strongest under its parent.

    A bake usually animates a prim another chunk authored (a Scatter's instancer), and chunk
    layers are ordered so the earlier-added sibling wins: that chunk's static values would
    hide the baked samples. Built from iter_chunk_layers() rather than one fixed root, since a
    graph layer is sublayered wherever its Edit Target says.
    """
    lreg = chunk_registry.ChunkLayerManager()
    if not lreg.initialized or not layer:
        return
    try:
        hierarchy: dict = {}
        for parent, sub in lreg.iter_chunk_layers():
            hierarchy[sub.identifier] = parent
            if sub.identifier != layer.identifier:
                continue
            if sub.identifier in parent.subLayerPaths and parent.subLayerPaths[0] != sub.identifier:
                parent.subLayerPaths.remove(sub.identifier)
                parent.subLayerPaths.insert(0, sub.identifier)
            grandparent = hierarchy.get(parent.identifier)
            if (
                grandparent
                and parent.identifier in grandparent.subLayerPaths
                and grandparent.subLayerPaths[0] != parent.identifier
            ):
                grandparent.subLayerPaths.remove(parent.identifier)
                grandparent.subLayerPaths.insert(0, parent.identifier)
            break
    except Exception as e:  # noqa: BLE001 - a failed reorder must not lose the bake
        logger.warning("Could not promote the bake layer: %s", e)


def keep(node, layer: Sdf.Layer | None) -> None:
    """Keep a copy of the bake in layer on node, for restore() after the next cook clears it.

    Kept on the chunk, so it goes when the chunk does.
    """
    if node is not None and layer is not None:
        kept = Sdf.Layer.CreateAnonymous("sim_bake_kept")
        kept.TransferContent(layer)
        setattr(node, _KEPT, kept)


def restore(node, layer: Sdf.Layer | None) -> bool:
    """Put the kept bake back into layer and promote it. False when nothing is kept."""
    kept = getattr(node, _KEPT, None)
    if kept is None or layer is None:
        return False
    layer.TransferContent(kept)
    promote(layer)
    return True


def forget(node) -> None:
    """Drop the kept bake, so the next cook does not put it back (a Reset)."""
    setattr(node, _KEPT, None)


if __name__ == "__main__":
    _child(*sys.argv[1:5])
