"""
Plugin implementation for Hugins' inhouse Kraken toolset
"""

from pxr import Tf
from kraken.plugin import KrakenPluginContainer
from kraken.scene import SceneManager, PrimSelectionNotice, StageLoadedNotice

from usd_chunks import chunk_registry, chunk_core, chunk_serial
from plugins import chunk_qt, chunk_usd


class KrakenScene(chunk_core.SceneSender):
    """
    This class is responsible for sending some UsdView related notices. In other DCC's, this class should not be used
    TODO: Make this abstract and move implementation to a UsdView related location
    """

    def __init__(self, kraken_scene: SceneManager):
        self.selection_listener = Tf.Notice.RegisterGlobally(PrimSelectionNotice, self.primSelectionChangedCallback)
        self.loaded_listener = Tf.Notice.RegisterGlobally(StageLoadedNotice, self.stage_loaded)
        self.kraken_scene = kraken_scene

        super().__init__()

    def stage_loaded(self, notice, sender=None):
        chunk_usd.ChunkLayerManager().clear_all_layers()
        chunk_usd.ChunkLayerManager().stage = notice.stage
        reg = chunk_registry.ChunkRegistry()
        reg.reload_modules()

    def get_stage(self):
        return self.kraken_scene.stage

    def get_selected_prims(self):
        if not self.kraken_scene or not self.kraken_scene.stage:
            return []

        return [self.kraken_scene.stage.GetPrimAtPath(x) for x in self.kraken_scene.selected_prims]

    def get_selected_property(self):
        return None


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

    chunk_usd.initialize(scene.stage, sceneSender)
    chunk_registry.ChunkRegistry().initialize(sceneSender, "chunk_kraken")


class ChunksPluginContainer(KrakenPluginContainer):
    def reloadChunks(self):
        reg = chunk_registry.ChunkRegistry()
        reg.reload_modules()

    def openModuleSettings(self):
        dial = chunk_qt.ChunkModuleLoader(SceneManager().main_window)
        dial.show()

    def saveLayers(self):
        reg = chunk_usd.ChunkLayerManager()
        for i in reg.chunk_layers.keys():
            layer = reg.get_layer(i)
            if not layer:
                continue

            name = layer.GetDisplayName().split("_")[0]
            path = f"C:/Users/Viktor/chunk_layers/{name}.usda"

            print("save layer: ", path)
            layer.Export(path)

    def saveScene(self):
        reg = chunk_registry.ChunkRegistry()

        if reg.get_scene_file():
            return reg.save_scene()

        flt = "*" + chunk_serial.FileExtensions.SCENE
        if file := chunk_qt.file_dial(filter=flt, create_new=True):
            reg.export_scene(file)
            reg.set_scene_file(file)

    def saveSceneAs(self):
        reg = chunk_registry.ChunkRegistry()

        flt = "*" + chunk_serial.FileExtensions.SCENE
        if file := chunk_qt.file_dial(filter=flt, create_new=True):
            reg.export_scene(file)
            reg.set_scene_file(file)

    def openScene(self):
        reg = chunk_registry.ChunkRegistry()

        flt = "*" + chunk_serial.FileExtensions.SCENE
        if file := chunk_qt.file_dial(filter=flt, create_new=False):
            reg.open_scene(file)


    def load(self, scene) -> None:
        """Register the UI Creator plugin with the plugin registry.

        Args:
            plugRegistry: The plugin registry to register the UI Creator plugin with.
            usdviewApi: The Usdview API instance to use for module creation.
        """

        self.add_command_plugin("Chunks", "Reload Chunks", self.reloadChunks)
        self.add_command_plugin("Chunks", "Modules", self.openModuleSettings)
        self.add_command_plugin("Chunks", "Save Layers", self.saveLayers)

        self.add_command_plugin("Chunks", "Save Scene", self.saveScene)
        self.add_command_plugin("Chunks", "Save Scene As", self.saveSceneAs)
        self.add_command_plugin("Chunks", "Open Scene", self.openScene)

    def configure(self, scene, window) -> None:
        chunk_qt.build_chunk_docker(window)

        launch_chunks(scene)


Tf.Type.Define(ChunksPluginContainer)
