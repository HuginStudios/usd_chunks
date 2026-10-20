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

wait() is for a long part of a chunk's own cook. The chunks below it read its result, so
the cook waits for it, and returns it: nothing else cooks meanwhile (chunk_cook.hold). The
WORKER parts still run on a thread, so the host paints, and a progress window shows what
it is doing, with Cancel. The rest of the window takes no input until it ends. A bake made
of steps says which one it is on with Context.step; the window adds the time so far and
how long the chunk's last wait took, which is the estimate a bar of steps cannot give.

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

from pxr import Sdf, Usd, UsdGeom

from usd_chunks import chunk_cook, chunk_registry, cook_cache
from usd_chunks.chunk_process import python_executable
from usd_chunks.cook_errors import CookCancelled

try:
    from PySide6 import QtCore
except ImportError:  # pragma: no cover - headless install without Qt
    QtCore = None

try:
    from PySide6 import QtWidgets
except ImportError:  # pragma: no cover - Qt without widgets
    QtWidgets = None

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

#: How often a step's bar moves within the same step, at most.
_STEP_EVERY_S = 0.1

#: On a chunk: how long its last wait() took, shown as the next one's estimate.
_LAST_WAIT = "_sim_bake_last_wait"

#: Marks the lines in_subprocess's child prints for its parent; anything else (an engine's own
#: banner) is passed through to the log.
_LINE = "@@sim_bake "


class Cancelled(CookCancelled):
    """The bake was cancelled (cancel(node)). Out of a cook, it fails the cook quietly."""


def in_background() -> bool:
    """Whether bakes run on a worker: only in a host with a running Qt event loop.

    That loop is the only way back to the main thread. Headless (cli), under test and in a
    script (Scene(), host "script") there is none, so the bake runs inline and is finished when
    run() returns -- even if the script made a QApplication, which does not run its loop.
    """
    if QtCore is None or QtCore.QCoreApplication.instance() is None:
        return False
    return chunk_registry.ChunkRegistry().plugin not in _INLINE_HOSTS


#: Hosts with nobody at them: no button to press, no timer to wait for.
_UNATTENDED_HOSTS = (None, "", "cli", "script")

#: Hosts with no running event loop: bakes run inline there.
_INLINE_HOSTS = (*_UNATTENDED_HOSTS, "pytest")


def is_unattended() -> bool:
    """Whether nobody can press Run: `usdchunks run`, a sweep, or a script (Scene.new()).

    A solver bakes on cook there, since a cook that only waits for a button bakes nothing
    and leaves a motionless scene that looks entirely plausible. The test host is not in
    the list: the tests drive the interactive path.
    """
    return chunk_registry.ChunkRegistry().plugin in _UNATTENDED_HOSTS


def is_running(node) -> bool:
    """Whether node has a bake in flight."""
    return bool(getattr(node, _RUNNING, False))


def cancel(node) -> None:
    """Ask node's running bake to stop. It stops at its next step or progress report."""
    setattr(node, _CANCEL, True)


class Context:
    """What a bake gets: progress, cancellation and live results, safe from any thread."""

    def __init__(self, node, report: Callable[[str], None], live: Callable | None, relay=None,
                 bar: Callable[[int, int], None] | None = None):
        self.node = node
        self._report = report
        self._live = live
        self._relay = relay
        self._bar = bar
        self._started = time.perf_counter()
        self._first_frame = None
        self._last_report = 0.0
        self._latest = None
        self._step_text = None
        self._last_step = 0.0

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

    def step(self, text: str, done: int, total: int) -> None:
        """Show what a bake made of steps is doing: text in the readout, the bar at done of total.

        For a bake whose parts are not frames. Its steps take what they take, so the bar
        says how far through them it is, not how long is left.
        """
        # Only what changed, and the bar at most every _STEP_EVERY_S: sampling the Kitchen
        # set reports once per mesh, 1,788 times, each a hop to the main thread, and the
        # readout's text the same each time.
        now = time.perf_counter()
        if text != self._step_text:
            self._step_text = text
            self.status(text)
        elif now - self._last_step < _STEP_EVERY_S:
            return
        self._last_step = now
        if self._bar is not None:
            self._call(lambda: self._bar(int(done), int(total)))

    def progress(self, done: int, total: int) -> None:
        """Report frame done (0-based) of total, with the time left once it can be guessed."""
        now = time.perf_counter()
        if self._first_frame is None:
            self._first_frame = now
        last = done >= total - 1
        if not last and now - self._last_report < _PROGRESS_EVERY_S:
            return
        self._last_report = now
        if self._bar is not None:
            self._call(lambda: self._bar(done + 1, total))
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


def wait(node, bake: Callable[[Context], Generator], *, report: Callable[[str], None],
         name: str = "SimBake"):
    """Run bake(ctx) for node and return its result, cooking nothing else until it ends.

    For a long part of a chunk's own cook: the chunks below it read the result, so they
    wait for it rather than cook against the last one and again when it lands. In a UI
    host the WORKER parts run on a thread while a progress window shows the bake's status,
    with Cancel; the rest of the window takes no input, and every cook asked for meanwhile
    runs once it has ended (chunk_cook.hold). Headless it runs straight through.

    Args:
        node: The chunk cooking.
        bake: A generator function taking a Context; its return value is the result.
        report: Shows a line of text in the chunk's readout. Called on the main thread.
        name: The worker thread's name.

    Returns:
        What bake returned.

    Raises:
        Cancelled: Cancel was pressed, or cancel(node) called.
    """
    setattr(node, _CANCEL, False)
    if not in_background():
        return drive(bake(Context(node, report, None)))

    window = _ProgressWindow(node, lambda: cancel(node), getattr(node, _LAST_WAIT, None))

    def shown(text: str) -> None:
        report(text)
        window.show_text(text)

    relay = _Relay()
    ctx = Context(node, shown, None, relay, bar=window.show_progress)
    gen = bake(ctx)
    loop = QtCore.QEventLoop()
    outcome: dict = {}

    def end(**result) -> None:
        outcome.update(result)
        loop.quit()

    def on_main():
        try:
            finished, value = _phase(gen, ctx, WORKER)
        except BaseException as e:  # noqa: BLE001 - raised on to the cook, below
            end(error=e)
            return
        if finished:
            end(value=value)
            return
        threading.Thread(target=on_worker, name=name, daemon=True).start()

    def on_worker():
        try:
            finished, value = _phase(gen, ctx, MAIN)
        except BaseException as e:  # noqa: BLE001
            relay.call.emit(lambda e=e: end(error=e))
            return
        relay.call.emit((lambda: end(value=value)) if finished else on_main)

    setattr(node, _RUNNING, True)
    started = time.perf_counter()
    try:
        with chunk_cook.hold(_later), window:
            on_main()
            if not outcome:
                loop.exec()
    finally:
        setattr(node, _RUNNING, False)
    if "error" in outcome:
        raise outcome["error"]
    setattr(node, _LAST_WAIT, time.perf_counter() - started)
    return outcome["value"]


def _later(fn: Callable) -> None:
    """Run fn from the event loop, once the current call stack has returned."""
    QtCore.QTimer.singleShot(0, QtCore.QCoreApplication.instance(), fn)


def _duration(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds} s" if seconds < 60 else f"{seconds // 60} min {seconds % 60:02d} s"


class _ProgressWindow:
    """What wait() shows while it waits: the chunk, its status, a bar, the time so far and
    the last wait's, and Cancel.

    It appears only if the wait lasts: a quarter of a second, so a short one does not flash.
    From the start, though, the rest of the window takes no input -- clicks, keys, the
    wheel, closing it -- so nothing can change the graph while a cook waits.
    """

    _DELAY_MS = 250

    def __init__(self, node, on_cancel: Callable[[], None], last: float | None = None):
        self._node = node
        self._on_cancel = on_cancel
        self._last = last
        self._started = time.perf_counter()
        self._dialog = None
        self._text = ""
        self._progress: tuple[int, int] | None = None
        self._timer = None
        self._clock = None
        self._filter = None

    def __enter__(self):
        app = QtWidgets.QApplication.instance() if QtWidgets is not None else None
        if app is None:
            return self
        self._filter = _InputFilter(self)
        app.installEventFilter(self._filter)
        self._timer = QtCore.QTimer()
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._show)
        self._timer.start(self._DELAY_MS)
        return self

    def __exit__(self, *exc):
        for timer in (self._timer, self._clock):
            if timer is not None:
                timer.stop()
        if self._filter is not None:
            QtWidgets.QApplication.instance().removeEventFilter(self._filter)
        if self._dialog is not None:
            self._dialog.hide()
            self._dialog.deleteLater()
            self._dialog = None
        return False

    def owns(self, obj) -> bool:
        """Whether obj is the window or a widget in it: the only things that take input."""
        if self._dialog is None:
            return False
        if not isinstance(obj, QtWidgets.QWidget):
            # Input reaches a window's QWindow before its widgets.
            return obj is self._dialog.windowHandle()
        return obj is self._dialog or self._dialog.isAncestorOf(obj)

    def show_text(self, text: str) -> None:
        self._text = text
        if self._dialog is not None:
            self._dialog.setLabelText(self._label())

    def show_progress(self, done: int, total: int) -> None:
        self._progress = (done, total)
        if self._dialog is not None:
            self._dialog.setRange(0, total)
            self._dialog.setValue(min(done, total))

    def _label(self) -> str:
        clock = _duration(time.perf_counter() - self._started)
        if self._last is not None:
            clock += f" · last time {_duration(self._last)}"
        return "\n".join(line for line in (self._node.name, self._text, clock) if line)

    def _tick(self) -> None:
        if self._dialog is not None:
            self._dialog.setLabelText(self._label())

    def _show(self) -> None:
        dialog = QtWidgets.QProgressDialog(self._label(), "Cancel", 0, 0, QtWidgets.QApplication.activeWindow())
        dialog.setWindowTitle("Cooking")
        dialog.setWindowModality(QtCore.Qt.ApplicationModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setMinimumWidth(360)
        dialog.canceled.connect(self._cancel)
        self._dialog = dialog
        self._clock = QtCore.QTimer()
        self._clock.timeout.connect(self._tick)
        self._clock.start(1000)
        if self._progress is not None:
            self.show_progress(*self._progress)
        dialog.show()

    def _cancel(self) -> None:
        if self._clock is not None:
            self._clock.stop()
        if self._dialog is not None:
            self._dialog.setLabelText(f"{self._node.name}\ncancelling…")
            self._dialog.setCancelButton(None)
        self._on_cancel()


if QtCore is not None:

    class _InputFilter(QtCore.QObject):
        """Eats input meant for anything but the progress window (see _ProgressWindow)."""

        _EATEN = frozenset(
            getattr(QtCore.QEvent.Type, name)
            for name in (
                "MouseButtonPress", "MouseButtonRelease", "MouseButtonDblClick", "MouseMove",
                "KeyPress", "KeyRelease", "ShortcutOverride", "Shortcut", "Wheel",
                "ContextMenu", "DragEnter", "DragMove", "Drop", "Close", "TabletPress",
                "TouchBegin",
            )
        )

        def __init__(self, window: _ProgressWindow):
            super().__init__()
            self._window = window

        def eventFilter(self, obj, event) -> bool:
            if event.type() not in self._EATEN or self._window.owns(obj):
                return False
            if event.type() == QtCore.QEvent.Type.Close:
                event.ignore()
            return True


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
    with progress(done, total), which reaches ctx.progress, or progress(done, total, text)
    for a step, which reaches ctx.step. Cancelling kills the process.

    Inline (ctx.inline) it is a plain call in this process: no event loop is waiting.
    """
    if ctx.inline:
        return function(progress=lambda done, total, text="": (
            ctx.step(text, done, total) if text else ctx.progress(done, total)), **kwargs)

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
        [python_executable(), "-m", "usd_chunks.sim_bake", source, function.__name__, str(job), str(out)],
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
                kind, *rest = line[len(_LINE):].rstrip("\n").split(" ", 3)
                if kind == "progress" and len(rest) > 2 and rest[2]:
                    ctx.step(rest[2], int(rest[0]), int(rest[1]))
                elif kind == "progress":
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

    def progress(done, total, text=""):
        # One line each: the words of a step (Context.step) go last, newlines out.
        words = " ".join(str(text).split())
        print(f"{_LINE}progress {int(done)} {int(total)} {words}".rstrip(), flush=True)

    result = getattr(module, function)(progress=progress, **kwargs)
    with open(out, "wb") as f:
        pickle.dump(result, f, protocol=pickle.HIGHEST_PROTOCOL)


def gravity(value: float, stage: Usd.Stage) -> float:
    """A solver's Gravity param in scene units/s²; 0 means Earth gravity in stage's units.

    A fixed default is right in one unit only: -981 in a metre scene made bodies fall a
    hundred times too fast.
    """
    return value or -9.81 / UsdGeom.GetStageMetersPerUnit(stage)


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
