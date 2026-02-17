"""Custom modules for the Usdview UI Framework.

This module provides a framework for dynamically loading and managing custom UI modules
"""

from pxr import Tf
from kraken.plugin import KrakenPluginContainer
from kraken.scene import SceneManager, PrimSelectionNotice, StageLoadedNotice
# import kraken

from usd_chunks import chunk_registry, chunk_notice
from plugins import chunk_qt


class KrakenScene(chunk_notice.SceneSender):
    """
    This class is responsible for sending some UsdView related notices. In other DCC's, this class should not be used
    TODO: Make this abstract and move implementation to a UsdView related location
    """

    def __init__(self, scene: SceneManager):
        self.selection_listener = Tf.Notice.RegisterGlobally(PrimSelectionNotice, self.primSelectionChangedCallback)
        self.loaded_listener = Tf.Notice.RegisterGlobally(StageLoadedNotice, self.stage_loaded)
        self.scene = scene
        pass

    def stage_loaded(self, notice, sender=None):
        chunk_registry.ChunkLayerManager().clear_all_layers()
        chunk_registry.ChunkLayerManager().stage = notice.stage
        reg = chunk_registry.ChunkRegistry()
        reg.reload_all()

    def get_stage(self):
        return self.scene.stage

    def get_selected_prims(self):
        if not self.scene or not self.scene.stage:
            return

        return [self.scene.stage.GetPrimAtPath(x) for x in self.scene.selected_prims]

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
    def reloadChunks(self):
        reg = chunk_registry.ChunkRegistry()
        reg.reload_all()

    def openModuleSettings(self):
        dial = chunk_qt.ChunkModuleLoader(SceneManager().main_window)
        dial.show()

    def saveLayers(self):
        reg = chunk_registry.ChunkLayerManager()
        for i in reg.chunk_layers.keys():
            layer = reg.get_layer(i)
            if not layer:
                continue

            name = layer.GetDisplayName().split("_")[0]
            path = f"C:/Users/Viktor/chunk_layers/{name}.usda"

            print("save layer: ", path)
            layer.Export(path)

    def load(self, scene) -> None:
        """Register the UI Creator plugin with the plugin registry.

        Args:
            plugRegistry: The plugin registry to register the UI Creator plugin with.
            usdviewApi: The Usdview API instance to use for module creation.
        """

        self.add_command_plugin("Chunks", "Reload Chunks", self.reloadChunks)
        self.add_command_plugin("Chunks", "Modules", self.openModuleSettings)
        self.add_command_plugin("Chunks", "Save Layers", self.saveLayers)

    def configure(self, scene, mainWindow) -> None:
        chunk_qt.build_chunk_docker(mainWindow)

        launch_chunks(scene)


Tf.Type.Define(ChunksPluginContainer)
