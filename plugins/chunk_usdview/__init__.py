"""Custom modules for the Usdview UI Framework.

This module provides a framework for dynamically loading and managing custom UI modules
"""

from pxr import Tf, Usd
from pxr.Usdviewq.plugin import PluginContainer
from pxr.Usdviewq import usdviewApi as UsdviewModule

from usd_chunks import chunk_registry, chunk_core, chunk_serial
from plugins import chunk_qt

from typing import Any, Callable, List, Optional
from collections import defaultdict

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
        super().__init__()

    def request_save(self) -> bool:
        reg = chunk_registry.ChunkRegistry()

        if not chunk_qt.confirm_dial("Save scene?"):
            return True

        flt = "*" + chunk_serial.FileExtensions.SCENE
        if file := chunk_qt.file_dial(filter=flt, create_new=True):
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

    def set_selection(self, prim: Any) -> None:
        ctrl = self._get_ctrl()
        if ctrl and prim:
            try:
                ctrl._dataModel.selection.setPrim(prim)
            except Exception:
                pass

    def get_viewport_screenshot(self) -> Any:
        ctrl = self._get_ctrl()
        if not ctrl:
            return None
        try:
            sv = ctrl._stageView
            try:
                return sv.grabFramebuffer()
            except Exception:
                return sv.grab()
        except Exception:
            return None

    def frame_prim(self, prim: Any, on_ready: Optional[Callable] = None) -> None:
        from PySide6.QtCore import QTimer
        ctrl = self._get_ctrl()
        if not ctrl or not prim:
            if on_ready:
                on_ready(None)
            return
        try:
            sv = ctrl._stageView
            old_state = sv.copyViewState()
            ctrl._dataModel.selection.setPrim(prim)
            ctrl._frameSelection()

            def _do_grab() -> None:
                px = None
                try:
                    px = sv.grab()
                except Exception:
                    pass
                try:
                    sv.restoreViewState(old_state)
                    sv.updateView()
                except Exception:
                    pass
                if on_ready:
                    on_ready(px)

            QTimer.singleShot(250, _do_grab)
        except Exception:
            if on_ready:
                on_ready(None)

    def update_viewport(self) -> None:
        ctrl = self._get_ctrl()
        if ctrl:
            try:
                ctrl._stageView.updateView(resetCam=False, forceComputeBBox=True)
            except Exception:
                pass

    def set_camera_prim(self, prim: Any) -> None:
        ctrl = self._get_ctrl()
        if ctrl and prim:
            try:
                ctrl._dataModel.viewSettings.cameraPrim = prim
            except Exception:
                pass

    def get_current_frame(self) -> float:
        ctrl = self._get_ctrl()
        if ctrl:
            try:
                return ctrl._dataModel.currentFrame.GetValue()
            except Exception:
                pass
        return 0.0

    def refresh_timeline(self, start: float, end: float) -> None:
        ctrl = self._get_ctrl()
        if ctrl:
            try:
                ctrl.realStartTimeCode = start
                ctrl.realEndTimeCode = end
                ctrl._UpdateTimeSamples()
            except Exception:
                pass

    def subscribe_selection_changed(self, callback: Callable) -> Optional[Callable]:
        try:
            model = self.usdviewAPI.dataModel

            def _on_sel_changed(*args):
                callback()

            model.selection.signalPrimSelectionChanged.connect(_on_sel_changed)
            return lambda: model.selection.signalPrimSelectionChanged.disconnect(
                _on_sel_changed
            )
        except Exception:
            return None

    def subscribe_frame_changed(self, callback: Callable) -> Optional[Callable]:
        try:
            model = self.usdviewAPI.dataModel

            def _on_frame(*args):
                callback()

            model.signalCurrentFrameChanged.connect(_on_frame)
            return lambda: model.signalCurrentFrameChanged.disconnect(_on_frame)
        except Exception:
            return None


def launch_chunks(usdviewApi) -> None:
    """Launch the UI Creator plugin.

    Args:
        usdviewApi: The Usdview API instance to use for module creation.
    """
    # The order of these does not matter. We can initialize the QT widgets before the ChunkRegistry since
    # the QT widget subscribes to the CHUNK_LOADED notice, and will add chunks when that notice is recieved.

    scene = UsdviewScene(usdviewApi)

    chunk_qt.build_chunk_docker(usdviewApi.qMainWindow)
    chunk_registry.ChunkLayerManager().initialize(usdviewApi.stage)
    chunk_registry.ChunkRegistry().initialize(scene, "chunk_usdview")


class ChunksPluginContainer(PluginContainer):
    def openModuleSettings(self, usdviewApi):
        dial = chunk_qt.ChunkModuleLoader(usdviewApi.qMainWindow)
        dial.show()

    def reloadChunks(self, usdviewApi):
        reg = chunk_registry.ChunkRegistry()

        loaded_config_names = defaultdict(int)
        for c in reg.scene.get_chunks():
            if conf := c.get_config():
                loaded_config_names[conf.module] += 1
        
        reg.scene.clear()

        for mod in reg.registered_modules.values():
            # mod.unload()
            if mod.loaded:
                mod.unload()
            
            for i in range(loaded_config_names.get(mod.module_name, 0)):
                if i == 0:
                    chunks = mod.load()
                else:
                    chunks = mod.create()
                reg.scene.add_chunks(chunks)

    def saveLayers(self, usdviewApi):
        reg = chunk_registry.ChunkLayerManager()
        # flt = "*.usda *.usd *.usdc"
        if not (path := chunk_qt.file_dial(directory=True)):
            return

        reg.save_layers(path)

    def saveScene(self, usdviewApi):
        reg = chunk_registry.ChunkRegistry()

        if reg.get_scene_file():
            return reg.save_scene()

        flt = "*" + chunk_serial.FileExtensions.SCENE
        if file := chunk_qt.file_dial(filter=flt, create_new=True):
            reg.export_scene(file)
            reg.set_scene_file(file)

    def saveSceneAs(self, usdviewApi):
        reg = chunk_registry.ChunkRegistry()

        flt = "*" + chunk_serial.FileExtensions.SCENE
        if file := chunk_qt.file_dial(filter=flt, create_new=True):
            reg.export_scene(file)
            reg.set_scene_file(file)

    def openScene(self, usdviewApi):
        reg = chunk_registry.ChunkRegistry()

        flt = "*" + chunk_serial.FileExtensions.SCENE
        if file := chunk_qt.file_dial(filter=flt, create_new=False):
            reg.open_scene(file)

    def importChunk(self, usdviewApi):
        reg = chunk_registry.ChunkRegistry()

        exts = (
            chunk_serial.FileExtensions.CHUNK_MODULE,
            chunk_serial.FileExtensions.PY_MODULE,
        )

        flt = " ".join([f"*{x}" for x in exts])
        if file := chunk_qt.file_dial(filter=flt, create_new=False):
            reg.import_chunk(file)

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
        menu.addItem(self._reloadChunkCommand, "SHIFT+R")


Tf.Type.Define(ChunksPluginContainer)
