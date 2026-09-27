"""The Kraken host plugin: chunks inside Hugin's in-house Kraken toolset."""
from __future__ import annotations

from typing_extensions import override

from pxr import Tf
from kraken.plugin import KrakenPluginContainer
from kraken.scene import SceneManager, PrimSelectionNotice, StageLoadedNotice

from usd_chunks import chunk_registry, chunk_core
from usd_chunks.plugins import chunk_qt
from usd_chunks import chunk_stage


class KrakenScene(chunk_core.SceneSender):
    """The SceneSender for Kraken: the stage and selection come from Kraken's scene manager."""

    def __init__(self, kraken_scene: SceneManager):
        self.selection_listener = Tf.Notice.RegisterGlobally(PrimSelectionNotice, self.primSelectionChangedCallback)
        self.loaded_listener = Tf.Notice.RegisterGlobally(StageLoadedNotice, self.stage_loaded)
        self.kraken_scene = kraken_scene

        super().__init__()

    def stage_loaded(self, notice, sender=None):
        """Reload every chunk module against a stage Kraken just loaded.

        Args:
            notice: The stage-loaded notice.
            sender: The sender.
        """
        chunk_stage.ChunkLayerManager().clear_all_layers()
        chunk_stage.ChunkLayerManager().stage = notice.stage
        reg = chunk_registry.ChunkRegistry()
        reg.reload_modules()

    @override
    def get_stage(self):
        return self.kraken_scene.stage

    @override
    def get_selected_prims(self):
        if not self.kraken_scene or not self.kraken_scene.stage:
            return []

        return [self.kraken_scene.stage.GetPrimAtPath(x) for x in self.kraken_scene.selected_prims]

    @override
    def get_selected_property(self):
        return None


def launch_chunks(scene: SceneManager) -> None:
    """Start the chunk system on Kraken's stage.

    Args:
        scene: Kraken's scene manager.

    Raises:
        AttributeError: The scene has no stage yet.
    """
    # The order of these does not matter. We can initialize the QT widgets before the ChunkRegistry since
    # the QT widget subscribes to the CHUNK_LOADED notice, and will add chunks when that notice is received.

    sceneSender = KrakenScene(scene)
    if not scene.stage:
        raise AttributeError("Stage not initialized")

    chunk_stage.initialize(scene.stage, sceneSender)
    chunk_registry.ChunkRegistry().initialize(sceneSender, "chunk_kraken")


class ChunksPluginContainer(KrakenPluginContainer):
    """Registers the chunk panel and menu commands with Kraken."""
    def reloadChunks(self):
        """Reload every chunk module."""
        reg = chunk_registry.ChunkRegistry()
        reg.reload_modules()

    def openModuleSettings(self):
        """Open the chunk module loader dialog."""
        dial = chunk_qt.ChunkModuleLoader(SceneManager().main_window)
        dial.show()

    def saveLayers(self):
        """Export each chunk's layer to a file."""
        reg = chunk_stage.ChunkLayerManager()
        for i in reg.chunk_layers.keys():
            layer = reg.get_layer(i)
            if not layer:
                continue

            name = layer.GetDisplayName().split("_")[0]
            path = f"C:/Users/Viktor/chunk_layers/{name}.usda"

            print("save layer: ", path)
            layer.Export(path)

    def saveChunkGroup(self):
        """Export the current chunk graph as one clean, portable USD layer.

        See chunk_qt.save_chunkgroup_dialog.
        """
        chunk_qt.save_chunkgroup_dialog()


    def load(self, scene) -> None:
        """Add the Chunks menu commands to Kraken.

        Args:
            scene: Kraken's scene manager.
        """
        self.add_command_plugin("Chunks", "Reload Chunks", self.reloadChunks)
        self.add_command_plugin("Chunks", "Modules", self.openModuleSettings)
        self.add_command_plugin("Chunks", "Save Layers", self.saveLayers)

        self.add_command_plugin("Chunks", "Save ChunkGroup", self.saveChunkGroup)

    def configure(self, scene, window) -> None:
        """Build the chunk dock in Kraken's window and start the chunk system for the scene.

        Args:
            scene: Kraken's scene.
            window: Kraken's main window.
        """
        chunk_qt.build_chunk_docker(window)

        launch_chunks(scene)


Tf.Type.Define(ChunksPluginContainer)
