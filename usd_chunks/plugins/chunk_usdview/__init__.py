"""Custom modules for the Usdview UI Framework.

This module provides a framework for dynamically loading and managing custom UI modules
"""

from pxr import Tf, Usd, Gf
from pxr.Usdviewq.plugin import PluginContainer
from pxr.Usdviewq import usdviewApi as UsdviewModule
from pxr.Usdviewq.qt import QtCore

from usd_chunks import chunk_registry, chunk_core, chunk_serial
from usd_chunks.plugins import chunk_qt, chunk_usd

from typing import List


class UsdviewScene(chunk_core.SceneSender):
    """
    This class is responsible for sending some UsdView related notices. In other DCC's, this class should not be used
    TODO: Make this abstract and move implementation to a UsdView related location
    """

    def __init__(self, usdviewAPI: UsdviewModule.UsdviewApi):
        self.usdviewAPI: UsdviewModule.UsdviewApi = usdviewAPI

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
        # Initialised here as well as on first use: _on_view_settings_changed reads it to tell
        # a free camera we caused to be rebuilt from one the user owns, and that runs on any
        # view setting change, long before anything asks for a viewport camera.
        self._snap_prim = None
        #: The user's own free-camera field of view, held across usdview rebuilding it.
        self._free_camera_fov = None
        self._camera_snap_timer = QtCore.QTimer()
        self._camera_snap_timer.setSingleShot(True)
        self._camera_snap_timer.timeout.connect(self._on_snap_timer)

        super().__init__()

    def get_current_frame(self) -> float:
        return self.usdviewAPI.dataModel.currentFrame

    def get_end_frame(self) -> float:
        return self.usdviewAPI.dataModel.frameRangeEnd

    def get_start_frame(self) -> float:
        return self.usdviewAPI.dataModel.frameRangeBegin

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
            stage.SetMetadata("endFrame", frame)
            self.usdviewAPI.dataModel.frameRangeEnd = frame
            appCtrl.realEndTimeCode = frame
        appCtrl._UpdateTimeSamples()

    def set_start_frame(self, frame: float):
        stage = self.get_stage()
        if not stage:
            return

        appCtrl = getattr(self.usdviewAPI, "_UsdviewApi__appController")
        with Usd.EditContext(self.get_stage(), stage.GetSessionLayer()):
            stage.SetMetadata("startFrame", frame)
            self.usdviewAPI.dataModel.frameRangeBegin = frame
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
        # Before pointing the viewport at a prim, while the free camera is still the user's
        # own. From here on usdview rebuilds it from that prim on every navigation, so this is
        # the last moment its lens means anything.
        self._remember_free_camera_lens()
        self._snap_prim = prim
        self._camera_snap_timer.start(150)

    def _remember_free_camera_lens(self) -> None:
        """Snapshot the free camera's lens, but only while it is genuinely the user's.

        Skipped whenever a camera prim is already in view, because the free camera is then a
        copy of *that* camera and remembering it would bake the leak in permanently instead of
        undoing it -- the lock re-asserts the prim after every move, so this is called
        repeatedly while a chunk camera is up.
        """
        try:
            if self.usdviewAPI.dataModel.viewSettings.cameraPrim is not None:
                return
            fov = getattr(self._get_free_camera(), "fov", None)
        except Exception:  # noqa: BLE001 - a lens is never worth raising into the host over
            return
        if fov is not None:
            self._free_camera_fov = fov

    def _on_view_settings_changed(self):
        current_cam = self._get_free_camera()

        if current_cam == self._current_cam:
            return

        if self._current_cam is not None:
            self._current_cam.signalFrustumChanged.disconnect(self.activeCameraChangedCallback)

        if current_cam is not None:
            self._current_cam = current_cam
            # Before connecting, deliberately: the fov setter emits signalFrustumChanged, and
            # hearing our own correction come back as a camera move would write the prim and
            # re-arm the snap, i.e. the very loop this is here to break.
            self._reconcile_free_camera_lens(current_cam)
            self._current_cam.signalFrustumChanged.connect(self.activeCameraChangedCallback)

    def _reconcile_free_camera_lens(self, free_cam) -> None:
        """Keep the user's own free-camera lens across usdview rebuilding it.

        usdview cannot tumble a camera prim, so any navigation while one is the viewport
        camera calls `switchToFreeCamera()`, which builds a **new** FreeCamera with
        `FreeCamera.FromGfCamera()` -- copying that prim's focal length and aperture. With
        `lock_to_freecam` re-asserting the prim after every move, that happens on every
        navigation, so the user's navigation lens is repainted with the chunk camera's each
        time. Reported as the camera's settings "transferring to the free camera": a 50mm on a
        13.3mm aperture turned the free camera into a 15-degree telephoto to fly around in.

        Which way to go is decided by whether a prim of ours is being viewed. If none is, this
        free camera is the user's and its lens is the thing worth remembering. If one is, the
        rebuild is a consequence of our own snapping and is ours to undo.
        """
        if self._snap_prim is None:
            self._free_camera_fov = getattr(free_cam, "fov", None)
            return

        if self._free_camera_fov is None:
            return
        # Compared rather than set unconditionally: the setter emits regardless, and an
        # unnecessary emit here is an unnecessary cook downstream.
        if abs(getattr(free_cam, "fov", self._free_camera_fov) - self._free_camera_fov) > 1e-6:
            free_cam.fov = self._free_camera_fov

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
        """The "nice save flow" -- the chunk graph plus correct up-axis/units.
        See chunk_qt.save_scene_dialog."""
        chunk_qt.save_scene_dialog()

    def saveChunkGroup(self, usdviewApi):
        """Export the current chunk graph as one clean, portable USD layer.
        See chunk_qt.save_chunkgroup_dialog."""
        chunk_qt.save_chunkgroup_dialog()

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

        self._saveChunkGroupCommand = plugRegistry.registerCommandPlugin(
            "ChunksPlugin.saveChunkGroup", "Save ChunkGroup", self.saveChunkGroup
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
        menu.addItem(self._saveChunkGroupCommand)
        menu.addSeparator()
        menu.addItem(self._layerStackCommand)
        menu.addItem(self._reloadChunkCommand, "SHIFT+R")


Tf.Type.Define(ChunksPluginContainer)
