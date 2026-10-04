"""Offering to install a chunk's missing pip packages, from inside Blender.

usdview asks with a blocking dialog at the moment a chunk loads. Blender cannot: its dialogs
are operators that return straight away and answer later, and the add-on loads every chunk
module at once while generating node classes. So BlenderScene.should_install_packages only
queues what is missing here and answers no for now, and a timer then offers the whole queue
in one dialog -- "OK to install requests for chunk AMDMaterialXLibrary?" for one chunk, a
checkbox per chunk for several.

The install itself runs on a worker thread under a modal operator, which shows pip's
progress in the status bar and the USDChunks sidebar, then registers the node types of the
chunks that can load now. Nothing is installed without the user's yes, and nothing is asked
while Blender's "Allow Online Access" is off.

Packages go to the dependency dir (USD_CHUNK_DEPENDENCY_DIR, one folder per Python
version), not into Blender's own Python -- which is shared by every add-on and replaced on
upgrade.
"""
from __future__ import annotations

import logging
import threading
import time

import bpy

from usd_chunks import chunk_registry
from usd_chunks.frontends.blender import nodegraph as bg

logger = logging.getLogger(__name__)

#: chunk name -> requirement strings waiting to be offered, in the order chunks asked.
_pending: dict[str, list[str]] = {}

#: The install running now; the modal operator writes it, the panel reads it.
_status: dict = {"running": False, "line": "", "label": "", "started": 0.0}


def queue(packages, chunk_name: str) -> None:
    """Remember that `chunk_name` needs `packages`, and offer them once Blender is idle."""
    if not getattr(bpy.app, "online_access", True):
        logger.warning(
            "usd_chunks: chunk %s needs %s. Turn on Preferences > System > Allow Online "
            "Access to be offered an install.", chunk_name, ", ".join(packages),
        )
        return
    wanted = _pending.setdefault(chunk_name, [])
    wanted.extend(p for p in packages if p not in wanted)
    if not bpy.app.timers.is_registered(_offer_pending):
        bpy.app.timers.register(_offer_pending, first_interval=0.2)


def report_line(line: str) -> None:
    """on_line for pip: called from the worker thread, so it only stores the text."""
    _status["line"] = line


def _still_missing() -> dict[str, list[str]]:
    """The queue minus whatever an install since has already satisfied."""
    mgr = chunk_registry.ChunkDependencyManager()
    out = {}
    for chunk_name, packages in _pending.items():
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
        for chunk_name, packages in _pending.items():
            item = self.items.add()
            item.chunk = chunk_name
            item.packages = "\n".join(packages)
        _pending.clear()
        if not self.items:
            return {"CANCELLED"}
        return context.window_manager.invoke_props_dialog(
            self, width=460, title="Missing chunk dependencies", confirm_text="Install"
        )

    def draw(self, context):
        col = self.layout.column()
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
        packages: list[str] = []
        for item in chosen:
            packages.extend(p for p in item.packages.split("\n") if p and p not in packages)
        chunks = ", ".join(i.chunk for i in chosen)

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
            made = bg.register_chunk_types()
            self.report({"INFO"}, f"Installed; {len(made)} chunk node type(s) now available")
        else:
            self.report({"ERROR"}, "Installing chunk dependencies failed -- see the console")
        return {"FINISHED"}


def _names(item) -> str:
    return ", ".join(p for p in item.packages.split("\n") if p)


CLASSES = (USDCHUNKS_PG_missing_dependency, USDCHUNKS_OT_install_dependencies)


def unregister() -> None:
    if bpy.app.timers.is_registered(_offer_pending):
        bpy.app.timers.unregister(_offer_pending)
    _pending.clear()
