"""Custom modules for the Usdview UI Framework.

This module provides a framework for dynamically loading and managing custom UI modules
"""

from pxr import Tf
from pxr.Usdviewq.plugin import PluginContainer
from pxr.Usdviewq import usdviewApi as UsdviewModule

from usd_chunks import chunk_registry, chunk_core, chunk_serial
from plugins import chunk_qt


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

    def should_install_package(self, package_name: str) -> bool:
        return chunk_qt.confirm_dial(f"Install package dependency {package_name}?")

    def primSelectionChangedCallback(self, added, removed):
        if not added and not removed:
            return
        return super().primSelectionChangedCallback(added, removed)

    def get_stage(self):
        return self.usdviewAPI.stage

    def get_selected_prims(self):
        return self.usdviewAPI.selectedPrims  # type: ignore

    def get_selected_property(self):
        return self.usdviewAPI.property  # type: ignore


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

    def saveLayers(self, usdviewApi):
        reg = chunk_registry.ChunkLayerManager()
        for i in reg.chunk_layers.keys():
            layer = reg.get_layer(i)
            if not layer:
                continue

            name = layer.GetDisplayName().split("_")[0]
            path = f"C:/Users/Viktor/chunk_layers/{name}.usda"
            layer.Export(path)

    def saveScene(self, usdviewApi):
        reg = chunk_registry.ChunkRegistry()

        if reg.get_scene_file():
            return reg.save_scene()

        flt = " ".join([f"*{x}" for x in chunk_serial.scene_file_extensions()])
        if file := chunk_qt.file_dial(filter=flt, create_new=True):
            reg.export_scene(file)
            reg.set_scene_file(file)

    def openScene(self, usdviewApi):
        reg = chunk_registry.ChunkRegistry()

        flt = " ".join([f"*{x}" for x in chunk_serial.scene_file_extensions()])
        if file := chunk_qt.file_dial(filter=flt, create_new=False):
            reg.open_scene(file)

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
        self._openSceneCommand = plugRegistry.registerCommandPlugin(
            "ChunksPlugin.openScene", "Open Scene", self.openScene
        )

        launch_chunks(plugCtx)

    def configureView(self, plugRegistry, plugUIBuilder) -> None:
        menu = plugUIBuilder.findOrCreateMenu("Chunks")
        menu.addItem(self._openModuleSettingsCommand)
        menu.addItem(self._saveLayersCommand)
        menu.addItem(self._saveSceneCommand)
        menu.addItem(self._openSceneCommand)


Tf.Type.Define(ChunksPluginContainer)
