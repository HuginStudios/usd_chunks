"""Offering to install a chunk's missing pip packages, from inside Blender.

usdview asks with a blocking dialog at the moment a chunk loads. Blender cannot: its dialogs
are operators that return straight away and answer later, and the add-on loads every chunk
module at once while generating node classes. So BlenderScene.should_install_packages only
queues what is missing here and answers no for now, and a timer then offers the whole queue
in one dialog -- "OK to install requests for chunk AMDMaterialXLibrary?" for one chunk, a
checkbox per chunk for several.

The install itself runs on a worker thread under a modal operator, which shows pip's
progress in the status bar and the USDChunks sidebar, then registers the node types of the
chunks that can load now and recooks the ones that were waiting. Nothing is installed without
the user's yes, nor while Blender's "Allow Online Access" is off -- the dialog says so and
carries the toggle.

A dialog is easy to lose (moving the mouse off one closes it), so what is still missing also
stays in the USDChunks sidebar with its own Install button, and every step is logged to the
console.

Packages go to the dependency dir (USD_CHUNK_DEPENDENCY_DIR, one folder per Python
version), not into Blender's own Python -- which is shared by every add-on and replaced on
upgrade.
"""
from __future__ import annotations

import logging
import threading
import time

import bpy

from usd_chunks import chunk_cook, chunk_registry
from usd_chunks.frontends.blender import nodegraph as bg

logger = logging.getLogger(__name__)

#: chunk name -> requirement strings waiting to be offered, in the order chunks asked.
_pending: dict[str, list[str]] = {}

#: chunk name -> requirement strings not installed yet: _pending outlives its dialog here,
#: so the sidebar can offer them again after the dialog is dismissed.
_missing: dict[str, list[str]] = {}

#: The install running now; the modal operator writes it, the panel reads it.
_status: dict = {"running": False, "line": "", "label": "", "started": 0.0}


def queue(packages, chunk_name: str) -> None:
    """Remember that `chunk_name` needs `packages`, and offer them once Blender is idle.

    Offered with Allow Online Access off too: the dialog says so and carries the toggle.
    Only a console warning, as this was, left a network that silently placed nothing.
    """
    logger.warning("usd_chunks: chunk %s needs %s, which is not installed -- asking to "
                   "install it", chunk_name, ", ".join(packages))
    for pending in (_pending, _missing):
        wanted = pending.setdefault(chunk_name, [])
        wanted.extend(p for p in packages if p not in wanted)
    if not bpy.app.timers.is_registered(_offer_pending):
        bpy.app.timers.register(_offer_pending, first_interval=0.2)


def report_line(line: str) -> None:
    """on_line for pip: called from the worker thread, so it only stores the text."""
    _status["line"] = line
    if line.strip():
        logger.info("usd_chunks: pip: %s", line.rstrip())


def _still_missing(wanted=None) -> dict[str, list[str]]:
    """`wanted` (the queue) minus whatever an install since has already satisfied."""
    mgr = chunk_registry.ChunkDependencyManager()
    out = {}
    for chunk_name, packages in (_pending if wanted is None else wanted).items():
        if left := [p for p in packages if not mgr.package_in_cache(p)]:
            out[chunk_name] = left
    return out


def _offer_pending():
    """Timer: open the install dialog for the queue. Returns seconds to retry, or None."""
    if _status["running"]:
        return 0.5
    missing = _still_missing()
    _pending.clear()
    if not missing:
        return None
    windows = bpy.context.window_manager.windows
    if not windows:
        _pending.update(missing)
        return 1.0
    _pending.update(missing)
    with bpy.context.temp_override(window=windows[0]):
        bpy.ops.usdchunks.install_dependencies("INVOKE_DEFAULT")
    return None


def _redraw() -> None:
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "NODE_EDITOR":
                area.tag_redraw()


def draw_progress(layout) -> None:
    """The sidebar's progress row while an install runs; nothing otherwise."""
    if not _status["running"]:
        return
    box = layout.box()
    box.label(text=_status["label"], icon="IMPORT")
    # pip does not say how much is left, so the ring turns rather than fills.
    spin = (time.monotonic() - _status["started"]) % 1.0
    box.progress(factor=spin, type="RING", text=_status["line"][:60] or "Starting pip...")


class USDCHUNKS_PG_missing_dependency(bpy.types.PropertyGroup):
    chunk: bpy.props.StringProperty()
    packages: bpy.props.StringProperty()
    install: bpy.props.BoolProperty(name="Install", default=True)


class USDCHUNKS_OT_install_dependencies(bpy.types.Operator):
    """Install the Python packages chunks need, after asking"""

    bl_idname = "usdchunks.install_dependencies"
    bl_label = "Install Chunk Dependencies"
    bl_options = {"INTERNAL"}

    items: bpy.props.CollectionProperty(type=USDCHUNKS_PG_missing_dependency)

    _thread: threading.Thread | None = None
    _timer = None
    _result: list

    def invoke(self, context, event):
        self.items.clear()
        if not _pending:  # the sidebar's Install button: offer everything still missing
            _pending.update(_still_missing(_missing))
        # Only what the open network uses starts ticked, and first: every module's
        # requirements land in the queue when node types are generated, and ticking them all
        # installed the whole library (taichi, mujoco and theirs) to get one scipy.
        used = _used_modules()
        for chunk_name in sorted(_pending, key=lambda name: name not in used):
            item = self.items.add()
            item.chunk = chunk_name
            item.packages = "\n".join(_pending[chunk_name])
            item.install = chunk_name in used
        _pending.clear()
        if not self.items:
            return {"CANCELLED"}
        return context.window_manager.invoke_props_dialog(
            self, width=460, title="Missing chunk dependencies", confirm_text="Install"
        )

    def cancel(self, context):
        logger.warning("usd_chunks: install declined -- the USDChunks sidebar's Missing "
                       "dependencies box offers it again")
        _redraw()

    def draw(self, context):
        col = self.layout.column()
        if not _online():
            box = col.box()
            box.label(text="Installing needs Blender's online access, which is off.", icon="ERROR")
            box.prop(context.preferences.system, "use_online_access")
            col.separator()
        if len(self.items) == 1:
            item = self.items[0]
            col.label(text=f"OK to install {_names(item)} for chunk {item.chunk}?")
        else:
            col.label(text="OK to install these for the chunks that need them?")
            for item in self.items:
                col.prop(item, "install", text=f"{_names(item)} for chunk {item.chunk}")
        col.separator()
        target = chunk_registry.ChunkDependencyManager().target_dir()
        col.label(text=f"pip installs into {target}", icon="INFO")

    def execute(self, context):
        chosen = [i for i in self.items if i.install or len(self.items) == 1]
        if not chosen:
            return {"CANCELLED"}
        if not _online():
            self.report({"ERROR"}, "Turn on Allow Online Access to install chunk dependencies")
            return {"CANCELLED"}
        logger.info("usd_chunks: installing %s", ", ".join(
            f"{_names(i)} for {i.chunk}" for i in chosen))
        packages: list[str] = []
        for item in chosen:
            packages.extend(p for p in item.packages.split("\n") if p and p not in packages)
        chunks = ", ".join(i.chunk for i in chosen)
        self._chunks = {i.chunk for i in chosen}

        _status.update(running=True, line="", started=time.monotonic(),
                       label=f"Installing {', '.join(packages)} for {chunks}")
        self._result = []
        # The manager's bookkeeping after pip touches no bpy, so the whole install can run
        # off the main thread; BlenderScene.run_install hands pip's lines to report_line.
        self._thread = threading.Thread(
            target=lambda: self._result.append(
                chunk_registry.ChunkDependencyManager().install(packages, chunks)
            ),
            name="usd_chunks-pip", daemon=True,
        )
        self._thread.start()
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.1, window=context.window)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        if self._thread is not None and self._thread.is_alive():
            context.workspace.status_text_set(f"{_status['label']}...  {_status['line'][:80]}")
            _redraw()
            return {"PASS_THROUGH"}

        context.window_manager.event_timer_remove(self._timer)
        context.workspace.status_text_set(None)
        _status["running"] = False
        _redraw()
        if self._result and self._result[0]:
            left = _still_missing(_missing)
            _missing.clear()
            _missing.update(left)
            made = bg.register_chunk_types()
            redone = _recook_waiting(self._chunks)
            message = (f"Installed; {len(made)} chunk node type(s) now available"
                       + (f", {redone} chunk(s) recooked" if redone else ""))
            logger.info("usd_chunks: %s", message)
            self.report({"INFO"}, message)
        else:
            self.report({"ERROR"}, "Installing chunk dependencies failed -- see the console")
        _redraw()
        return {"FINISHED"}


def _recook_waiting(modules: set) -> int:
    """Re-execute the open network's chunks of these modules; how many.

    They loaded and cooked while the install was still a question -- a chunk that needs
    the package places nothing without it (Scatter Points) -- so nothing else would cook
    them again once it is there.
    """
    scene = chunk_registry.ChunkRegistry().scene
    if scene is None:
        return 0
    waiting = [c for c in scene.get_chunks()
               if (module := c.get_chunk_module()) and module.module_name in modules]
    # Batched: they are often chained (Grow Near reads an earlier scatter), and one
    # execute() at a time recooked everything below each of them again.
    with chunk_cook.batched_solve():
        for c in waiting:
            c.execute()
    return len(waiting)


def draw_missing(layout, context) -> None:
    """The sidebar's Missing dependencies box: what the open network still waits for.

    Chunks used in the open network come first and by name; the rest of the library only
    as a count, so one missing package for one Scatter is not buried under a dozen chunks
    nobody placed.
    """
    if _status["running"] or not (missing := _still_missing(_missing)):
        return
    used = _used_modules()
    box = layout.box()
    box.label(text="Missing dependencies", icon="ERROR")
    for chunk_name in sorted(set(missing) & used):
        box.label(text=f"{chunk_name}: {', '.join(missing[chunk_name])}")
    if others := len(set(missing) - used):
        box.label(text=f"+ {others} chunk(s) not in this network")
    if not _online():
        box.prop(context.preferences.system, "use_online_access")
    box.operator(USDCHUNKS_OT_install_dependencies.bl_idname, text="Install...", icon="IMPORT")


def _used_modules() -> set:
    """The module names of the chunks in the open network."""
    scene = chunk_registry.ChunkRegistry().scene
    return {m.module_name for c in (scene.get_chunks() if scene else ())
            if (m := c.get_chunk_module())}


def _online() -> bool:
    return getattr(bpy.app, "online_access", True)


def _names(item) -> str:
    return ", ".join(p for p in item.packages.split("\n") if p)


CLASSES = (USDCHUNKS_PG_missing_dependency, USDCHUNKS_OT_install_dependencies)


def unregister() -> None:
    if bpy.app.timers.is_registered(_offer_pending):
        bpy.app.timers.unregister(_offer_pending)
    _pending.clear()
    _missing.clear()
