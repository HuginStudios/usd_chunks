"""Custom modules for the Usdview UI Framework.

This module provides a framework for dynamically loading and managing custom UI modules
"""

from pxr import Tf
from kraken.plugin import KrakenPluginContainer
from kraken.scene import SceneManager
# import kraken

from usd_chunks import chunk_registry, chunk_notice
from plugins import chunk_qt


class KrakenScene(chunk_notice.SceneSender):
    """
    This class is responsible for sending some UsdView related notices. In other DCC's, this class should not be used
    TODO: Make this abstract and move implementation to a UsdView related location
    """

    def __init__(self, scene: SceneManager):
        # self.usdviewAPI: UsdviewModule.UsdviewApi = usdviewAPI

        # Usdview stubs are not up to date, so need to ignore typing
        # data_model = self.usdviewAPI.dataModel

        # selection_model = data_model.selection  # type: ignore

        # selection_model.signalPrimSelectionChanged.connect(self.primSelectionChangedCallback)
        # selection_model.signalPropSelectionChanged.connect(self.propSelectionChangedCallback)
        self.scene = scene
        pass

    def get_stage(self):
        return self.scene.stage

    def get_selected_prims(self):
        return []

    def get_selected_property(self):
        return []


def launch_chunks(scene: SceneManager) -> None:
    """Launch the UI Creator plugin.

    Args:
        usdviewApi: The Usdview API instance to use for module creation.
    """
    # The order of these does not matter. We can initialize the QT widgets before the ChunkRegistry since
    # the QT widget subscribes to the CHUNK_LOADED notice, and will add chunks when that notice is recieved.

    sceneSender = KrakenScene(scene)
    if not scene.stage:
        raise AttributeError("Stage not initialized")

    chunk_registry.ChunkLayerManager().initialize(scene.stage)
    chunk_registry.ChunkRegistry().initialize(sceneSender, "chunk_kraken")


class ChunksPluginContainer(KrakenPluginContainer):
    # def reloadChunks(self, usdviewApi):
    #     reg = chunk_registry.ChunkRegistry()

    #     for name, mod in reg.registered_modules.items():
    #         reg.unload_chunk_module(mod)
    #         if not mod.loaded:
    #             reg.load_module(mod)

    # def openModuleSettings(self, usdviewApi):
    #     dial = chunk_qt.ChunkModuleLoader(usdviewApi.qMainWindow)
    #     dial.show()

    # def saveLayers(self, usdviewApi):
    #     reg = chunk_registry.ChunkLayerManager()
    #     for i in reg.chunk_layers.keys():
    #         layer = reg.get_layer(i)
    #         if not layer:
    #             continue

    #         name = layer.GetDisplayName().split("_")[0]
    #         path = f"C:/Users/Viktor/chunk_layers/{name}.usda"

    #         print("save layer: ", path)
    #         layer.Export(path)

    def registerPlugins(self, scene) -> None:
        # """Register the UI Creator plugin with the plugin registry.

        # Args:
        #     plugRegistry: The plugin registry to register the UI Creator plugin with.
        #     usdviewApi: The Usdview API instance to use for module creation.
        # """
        # self._reloadChunksCommand = plugRegistry.registerCommandPlugin(
        #     "ChunksPlugin.reloadChunks", "Reload Chunks", self.reloadChunks
        # )
        # self._openModuleSettingsCommand = plugRegistry.registerCommandPlugin(
        #     "ChunksPlugin.openModuleSettings", "Modules", self.openModuleSettings
        # )
        # self._saveLayersCommand = plugRegistry.registerCommandPlugin(
        #     "ChunksPlugin.saveLayers", "Save Layers", self.saveLayers
        # )

        launch_chunks(scene)
        pass

    def configureView(self, mainWindow) -> None:
        chunk_qt.build_chunk_docker(mainWindow)


Tf.Type.Define(ChunksPluginContainer)
