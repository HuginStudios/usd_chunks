"""The Houdini SceneSender, and bringing the registry up inside Houdini.

A Solaris network has no single stage: every LOP cooks its own from its inputs. So
HoudiniScene has a stage only while a chunk node is cooking (lop.cook sets it), and None
the rest of the time. That None is load-bearing: chunk_cook.cook refuses to run a module
with no stage, so pushing parm values into a chunk between cooks only marks it stale
rather than cooking it on the spot -- Houdini decides when a node cooks, not the chunk.
"""
from __future__ import annotations

import logging
import threading

import hou

try:
    import hdefereval  # UI-only: absent in hython, where call_later queues instead
except ImportError:
    hdefereval = None

from usd_chunks import chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.hosts.houdini import layers

logger = logging.getLogger(__name__)

#: The registry's host identifier, what a chunk's load_for_plugin names.
PLUGIN = "houdini"


class HoudiniScene(SceneSender):
    """The scene as a chunk sees it from inside a Houdini session.

    Most of a SceneSender is about a viewer -- camera, renderer, timeline. The timeline maps
    onto Houdini's; the viewport calls are left to the base class (no-ops), since a LOP
    must not drive the Scene Viewer from its cook.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._stage = None
        #: call_later callbacks waiting for an event loop, when there is none (hython).
        self._pending: list = []
        #: True while the user is putting a chunk down (lop.create_from_tool): the one time
        #: it is right to ask them something. Never during a cook -- a .hip loading would
        #: stop on a dialog per node.
        self.asking = False

    def get_stage(self):
        return self._stage

    def set_stage(self, stage) -> None:
        """Set the stage a cook runs on; None between cooks. See the module docstring."""
        self._stage = stage

    def get_host(self) -> tuple[str, str]:
        return ("houdini", hou.applicationVersionString())

    def user_layers(self, stage):
        """The layers a Layer chunk named, and files: the rest of the stack is Houdini's."""
        return layers.user_layers(stage)

    def export_layer(self, layer):
        """layer without Houdini's own bookkeeping prim."""
        return layers.without_houdini_info(layer)

    def call_later(self, fn, delay: float = 0.0):
        """Run fn on Houdini's main thread once it is idle; returns a cancel function.

        hdefereval needs the UI's event loop. Without one (hython) the call waits in a
        queue that flush_pending empties -- lop.cook does after every cook, once the node
        is no longer cooking and may be edited.
        """
        cancelled = threading.Event()

        def run():
            if not cancelled.is_set():
                fn()

        if hdefereval is None or not hou.isUIAvailable():
            self._pending.append(run)
            return cancelled.set

        if delay > 0:
            timer = threading.Timer(delay, lambda: hdefereval.executeDeferred(run))
            timer.daemon = True
            timer.start()
        else:
            hdefereval.executeDeferred(run)
        return cancelled.set

    def flush_pending(self) -> None:
        """Run the call_later callbacks queued with no event loop to run them."""
        while self._pending:
            self._pending.pop(0)()

    def should_install_packages(self, packages, chunk_name: str) -> bool:
        """Ask before installing a chunk's missing pip packages -- only while putting one down.

        Otherwise no: the chunk does not load, and the warning names what is missing.
        """
        if not (self.asking and hou.isUIAvailable()):
            return False
        target = chunk_registry.ChunkDependencyManager().target_dir()
        answer = hou.ui.displayMessage(
            f"OK to install {', '.join(packages)} for chunk {chunk_name}?",
            buttons=("Install", "Cancel"), default_choice=0, close_choice=1,
            title="Missing chunk dependencies",
            details=f"pip will install into {target}. Without it the chunk cannot load.")
        return answer == 0

    def run_install(self, packages, chunk_name: str, run):
        with hou.InterruptableOperation(
                f"Installing {', '.join(packages)} for chunk {chunk_name}", open_interrupt_dialog=True):
            return run(None)

    def get_selected_prims(self):
        """The prims selected in the Scene Viewer, on the stage of the cook in progress."""
        stage = self.get_stage()
        if stage is None or not hou.isUIAvailable():
            return []
        viewer = hou.ui.paneTabOfType(hou.paneTabType.SceneViewer)
        if viewer is None:
            return []
        prims = (stage.GetPrimAtPath(p) for p in viewer.currentSceneGraphSelection())
        return [p for p in prims if p]

    def get_current_frame(self) -> float:
        return hou.frame()

    def get_start_frame(self) -> float:
        return hou.playbar.frameRange()[0] if hou.isUIAvailable() else hou.hscriptExpression("$FSTART")

    def get_end_frame(self) -> float:
        return hou.playbar.frameRange()[1] if hou.isUIAvailable() else hou.hscriptExpression("$FEND")

    def set_current_frame(self, frame: float):
        hou.setFrame(frame)


def ensure_scene() -> HoudiniScene:
    """The session's HoudiniScene, bringing the chunk registry up on first use.

    Discovery is the slow part (every module under USD_CHUNK_DIRS is imported), so it
    happens once, on the first TAB menu build or chunk cook -- not at Houdini startup.
    """
    reg = chunk_registry.ChunkRegistry()
    if reg.initialized and isinstance(reg.scene, HoudiniScene):
        return reg.scene
    scene = HoudiniScene()
    reg.initialize(scene, PLUGIN)
    return scene
