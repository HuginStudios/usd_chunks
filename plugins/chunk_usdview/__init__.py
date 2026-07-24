"""Custom modules for the Usdview UI Framework.

This module provides a framework for dynamically loading and managing custom UI modules
"""

import logging

from pxr import Tf, Usd, Gf
from pxr.Usdviewq.plugin import PluginContainer
from pxr.Usdviewq import usdviewApi as UsdviewModule
from pxr.Usdviewq.qt import QtCore
from pxr.Usdviewq.rootDataModel import RootDataModel

from usd_chunks import chunk_animation, chunk_registry, chunk_core, chunk_serial
from plugins import chunk_qt, chunk_usd

from typing import List

logger = logging.getLogger(__name__)

_frame_hook_installed = False


def _install_frame_change_hook() -> None:
    """Patch RootDataModel.currentFrame so every frame-change path notifies
    ChunkAnimationManager -- interactive timeline scrubbing, playback, our own
    UsdviewScene.set_current_frame()/appController.setFrame(), and usdview's own startup
    frame-range init all assign to this one property.

    appController exposes no signal for frame changes. Wrapping appController.setFrame()
    alone was tried and rejected during design: scrubbing and playback both go through
    appController.frameSlider.valueChanged -> _setFrameIndex() -> dataModel.currentFrame
    directly, bypassing setFrame() entirely. RootDataModel.currentFrame's setter
    (rootDataModel.py) is the one point every path actually shares. See CONTEXT.md's
    "Attribute Animation (keyframes)" section for the full trace.
    """
    global _frame_hook_installed
    if _frame_hook_installed:
        return
    _frame_hook_installed = True

    original_property = RootDataModel.currentFrame
    original_setter = original_property.fset

    def _patched_setter(self, value):
        original_setter(self, value)
        try:
            chunk_animation.ChunkAnimationManager().on_frame_changed(value.GetValue())
        except Exception:
            logger.exception("chunk_animation frame-change hook failed")

    RootDataModel.currentFrame = property(original_property.fget, _patched_setter)


class UsdviewScene(chunk_core.SceneSender):
    """
    This class is responsible for sending some UsdView related notices. In other DCC's, this class should not be used
    TODO: Make this abstract and move implementation to a UsdView related location
    """

    def __init__(self, usdviewAPI: UsdviewModule.UsdviewApi):
        self.usdviewAPI: UsdviewModule.UsdviewApi = usdviewAPI

        _install_frame_change_hook()

        # Usdview stubs are not up to date, so need to ignore typing
        data_model = self.usdviewAPI.dataModel

        selection_model = data_model.selection  # type: ignore
        selection_model.signalPrimSelectionChanged.connect(self.primSelectionChangedCallback)
        selection_model.signalPropSelectionChanged.connect(self.propSelectionChangedCallback)

        view_settings = data_model.viewSettings  # type: ignore
        view_settings.signalSettingChanged.connect(self._on_view_settings_changed)

        data_model.signalStageReplaced.connect(self._on_stage_replaced)  # type: ignore

        self._current_cam = None

        # Adds a delay to the set_viewport_camera as a workaround to remove thread racing
        self._camera_prim = None
        self._camera_snap_timer = QtCore.QTimer()
        self._camera_snap_timer.setSingleShot(True)
        self._camera_snap_timer.timeout.connect(self._on_snap_timer)

        super().__init__()

    def get_current_frame(self) -> float:
        # dataModel.currentFrame is actually a Usd.TimeCode (RootDataModel's setter enforces
        # this), not a plain float, despite this method's own -> float annotation. Usd.TimeCode(x)
        # tolerates being handed an already-TimeCode value almost everywhere, which is why this
        # went unnoticed -- but Stage.SetStartTimeCode/SetEndTimeCode strictly require a plain
        # double and reject a TimeCode outright (Boost.Python ArgumentError). GetValue() is the
        # actual, always-correct plain-float extraction.
        return self.usdviewAPI.dataModel.currentFrame.GetValue()

    def get_end_frame(self) -> float:
        # dataModel has no frameRangeEnd of its own -- the real, always-initialized state
        # usdview itself uses for playback range is appController.realEndTimeCode (see
        # _UpdateTimeSamples). set_end_frame below writes both; only realEndTimeCode is
        # guaranteed to exist before the first set_end_frame call.
        appCtrl = getattr(self.usdviewAPI, "_UsdviewApi__appController")
        return appCtrl.realEndTimeCode

    def get_start_frame(self) -> float:
        appCtrl = getattr(self.usdviewAPI, "_UsdviewApi__appController")
        return appCtrl.realStartTimeCode

    def set_current_frame(self, frame: float):
        appCtrl = self.usdviewAPI._UsdviewApi__appController
        appCtrl.setFrame(frame)
        QtCore.QCoreApplication.processEvents()

    def set_end_frame(self, frame: float):
        stage = self.get_stage()
        if not stage:
            return

        appCtrl = getattr(self.usdviewAPI, "_UsdviewApi__appController")
        with Usd.EditContext(self.get_stage(), stage.GetSessionLayer()):
            # SetEndTimeCode (not SetMetadata("endFrame", ...), a nonstandard key USD's own
            # HasAuthoredTimeCodeRange()/GetEndTimeCode() don't recognize) persists the real
            # stage metadata. But _UpdateTimeSamples() (below) does NOT re-derive
            # realStartTimeCode/realEndTimeCode from that metadata -- only _reloadFixedUI()
            # does, and it's heavier than needed here (recomputes FPS, step, etc). So
            # realEndTimeCode must also be assigned directly, or _UpdateTimeSamples() just
            # recomputes _timeSamples from whatever (possibly still None) value it already had.
            stage.SetEndTimeCode(frame)
        appCtrl.realEndTimeCode = frame
        appCtrl._UpdateTimeSamples()

    def set_start_frame(self, frame: float):
        stage = self.get_stage()
        if not stage:
            return

        appCtrl = getattr(self.usdviewAPI, "_UsdviewApi__appController")
        with Usd.EditContext(self.get_stage(), stage.GetSessionLayer()):
            stage.SetStartTimeCode(frame)
        appCtrl.realStartTimeCode = frame
        appCtrl._UpdateTimeSamples()

    def request_save(self) -> bool:
        reg = chunk_registry.ChunkRegistry().scene

        if not chunk_qt.confirm_dial("Save chunk scene?"):
            return True

        flt = "*" + chunk_serial.FileExtensions.SCENE
        if file := chunk_qt.file_dial(flt, create_new=True):
            reg.export_scene(file)
            return True

        return False

    def should_install_package(self, package_name: str) -> bool:
        return chunk_qt.confirm_dial(f"Install package dependency {package_name}?")

    def primSelectionChangedCallback(self, added, removed):
        if not added and not removed:
            return
        return super().primSelectionChangedCallback()

    def get_stage(self) -> Usd.Stage:
        return self.usdviewAPI.stage  # type: ignore

    def get_selected_prims(self) -> List[Usd.Prim]:
        return self.usdviewAPI.selectedPrims  # type: ignore

    def get_selected_property(self) -> Usd.Property:
        return self.usdviewAPI.property  # type: ignore

    def _get_ctrl(self):
        try:
            return self.usdviewAPI._UsdviewApi__appController
        except Exception:
            return None

    def refresh_timeline(self, start: float, end: float) -> None:
        ctrl = self._get_ctrl()
        if ctrl:
            try:
                ctrl.realStartTimeCode = start
                ctrl.realEndTimeCode = end
                ctrl._UpdateTimeSamples()
            except Exception:
                pass

    def _get_free_camera(self):
        app = self.usdviewAPI._UsdviewApi__appController
        return app._dataModel.viewSettings.freeCamera

    def get_active_camera(self):
        free_cam = self._get_free_camera()
        if free_cam is None:
            return None
        return free_cam.computeGfCamera(Gf.BBox3d())

    def _set_viewport_camera(self, prim):
        app = self.usdviewAPI._UsdviewApi__appController
        app._dataModel.viewSettings.cameraPrim = prim

    def set_viewport_camera(self, prim) -> None:
        self._snap_prim = prim
        self._camera_snap_timer.start(150)

    def _on_view_settings_changed(self):
        current_cam = self._get_free_camera()

        if current_cam == self._current_cam:
            return

        if self._current_cam is not None:
            self._current_cam.signalFrustumChanged.disconnect(self.activeCameraChangedCallback)

        if current_cam is not None:
            self._current_cam = current_cam
            self._current_cam.signalFrustumChanged.connect(self.activeCameraChangedCallback)

    def _on_snap_timer(self):
        if self._snap_prim is not None:
            self._set_viewport_camera(self._snap_prim)

    def _on_stage_replaced(self):
        self.notify_stage_replaced(self.get_stage())


def launch_chunks(usdviewApi) -> None:
    """Launch the UI Creator plugin.

    Args:
        usdviewApi: The Usdview API instance to use for module creation.
    """
    # The order of these does not matter. We can initialize the QT widgets before the ChunkRegistry since
    # the QT widget subscribes to the CHUNK_LOADED notice, and will add chunks when that notice is recieved.

    scene = UsdviewScene(usdviewApi)

    chunk_qt.build_chunk_docker(usdviewApi.qMainWindow)
    chunk_registry.ChunkRegistry().initialize(scene, "chunk_usdview")
    chunk_usd.initialize(usdviewApi.stage, scene)


class ChunksPluginContainer(PluginContainer):
    def openModuleSettings(self, usdviewApi):
        dial = chunk_qt.ChunkModuleLoader(usdviewApi.qMainWindow)
        dial.show()

    def reloadChunks(self, usdviewApi):
        reg = chunk_registry.ChunkRegistry()
        reg.reload_modules()

    def saveLayers(self, usdviewApi):
        reg = chunk_usd.ChunkLayerManager()
        # flt = "*.usda *.usd *.usdc"
        if not (path := chunk_qt.file_dial(directory=True)):
            return

        reg.save_layers(path)

    def saveScene(self, usdviewApi):
        reg = chunk_registry.ChunkRegistry().scene

        if reg.get_scene_file():
            return reg.save_scene()

        flt = "*" + chunk_serial.FileExtensions.SCENE
        if file := chunk_qt.file_dial(flt, create_new=True):
            reg.export_scene(file)

    def saveSceneAs(self, usdviewApi):
        reg = chunk_registry.ChunkRegistry().scene

        flt = "*" + chunk_serial.FileExtensions.SCENE
        if file := chunk_qt.file_dial(flt, create_new=True):
            reg.export_scene(file)

    def openScene(self, usdviewApi):
        reg = chunk_registry.ChunkRegistry().scene

        flt = "*" + chunk_serial.FileExtensions.SCENE
        if file := chunk_qt.file_dial(flt, create_new=False):
            reg.open_scene(file)

    def importChunk(self, usdviewApi):
        reg = chunk_registry.ChunkRegistry()

        exts = (
            chunk_serial.FileExtensions.CHUNK_MODULE,
            chunk_serial.FileExtensions.PY_MODULE,
        )

        flt = " ".join([f"*{x}" for x in exts])
        if file := chunk_qt.file_dial(flt, create_new=False):
            reg.import_chunk(file)

    def openLayerStack(self, usdviewApi):
        """Open the global Layer Stack inspector window."""
        import importlib
        try:
            layer_stack = importlib.import_module("scripts.layer_stack")
        except Exception:
            import os
            import importlib.util
            path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                "scripts", "layer_stack.py",
            )
            spec = importlib.util.spec_from_file_location("layer_stack", path)
            layer_stack = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(layer_stack)
        # Keep a reference so the window isn't garbage-collected while open.
        self._layerStackWin = layer_stack.open_window(usdviewApi)

    def registerPlugins(self, plugRegistry, plugCtx) -> None:
        """Register the UI Creator plugin with the plugin registry.

        Args:
            plugRegistry: The plugin registry to register the UI Creator plugin with.
            plugCtx: The Usdview API instance to use for module creation.
        """
        self._openModuleSettingsCommand = plugRegistry.registerCommandPlugin(
            "ChunksPlugin.openModuleSettings", "Modules", self.openModuleSettings
        )
        self._saveLayersCommand = plugRegistry.registerCommandPlugin(
            "ChunksPlugin.saveLayers", "Save Layers", self.saveLayers
        )

        self._saveSceneCommand = plugRegistry.registerCommandPlugin(
            "ChunksPlugin.saveScene", "Save Scene", self.saveScene
        )
        self._saveSceneAsCommand = plugRegistry.registerCommandPlugin(
            "ChunksPlugin.saveSceneAs", "Save Scene As", self.saveSceneAs
        )
        self._openSceneCommand = plugRegistry.registerCommandPlugin(
            "ChunksPlugin.openScene", "Open Scene", self.openScene
        )

        self._importChunkCommand = plugRegistry.registerCommandPlugin(
            "ChunksPlugin.importChunk", "Import Module", self.importChunk
        )

        self._reloadChunkCommand = plugRegistry.registerCommandPlugin(
            "ChunksPlugin.reloadChunk", "Reload Chunks", self.reloadChunks
        )

        self._layerStackCommand = plugRegistry.registerCommandPlugin(
            "ChunksPlugin.openLayerStack", "Layer Stack", self.openLayerStack
        )

        launch_chunks(plugCtx)

    def configureView(self, plugRegistry, plugUIBuilder) -> None:
        menu = plugUIBuilder.findOrCreateMenu("Chunks")
        menu.addItem(self._openModuleSettingsCommand)
        menu.addItem(self._importChunkCommand)
        menu.addSeparator()
        menu.addItem(self._saveLayersCommand)
        menu.addSeparator()
        menu.addItem(self._saveSceneCommand)
        menu.addItem(self._saveSceneAsCommand)
        menu.addItem(self._openSceneCommand)
        menu.addSeparator()
        menu.addItem(self._layerStackCommand)
        menu.addItem(self._reloadChunkCommand, "SHIFT+R")


Tf.Type.Define(ChunksPluginContainer)
