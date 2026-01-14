"""Custom modules for the Usdview UI Framework.

This module provides a framework for dynamically loading and managing custom UI modules
"""

from pxr import Tf
from pxr.Usdviewq.plugin import PluginContainer
from pxr.Usdviewq import usdviewApi as UsdviewModule

from usd_chunks import chunk_registry, chunk_notice
from plugins import chunk_qt


class UsdviewScene(chunk_notice.SceneSender):
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
    chunk_qt.build_chunk_docker(usdviewApi.qMainWindow)
    chunk_registry.ChunkRegistry().initialize(UsdviewScene(usdviewApi))

class ChunksPluginContainer(PluginContainer):
    def reloadChunks(self, usdviewApi):
        reg = chunk_registry.ChunkRegistry()

        for name, mod in reg.registered_modules.items():
            reg.unload_chunk_module(mod)
            if not mod.loaded:
                reg.load_module(mod)

    def openModuleSettings(self, usdviewApi):
        dial = chunk_qt.ChunkModuleLoader()
        dial.exec()

    def registerPlugins(self, plugRegistry, usdviewApi) -> None:
        """Register the UI Creator plugin with the plugin registry.

        Args:
            plugRegistry: The plugin registry to register the UI Creator plugin with.
            usdviewApi: The Usdview API instance to use for module creation.
        """

        self._reloadChunksCommand = plugRegistry.registerCommandPlugin(
            "ChunksPlugin.reloadChunks", "Reload Chunks", self.reloadChunks
        )
        self._openModuleSettingsCommand = plugRegistry.registerCommandPlugin(
            "ChunksPlugin.openModuleSettings", "Modules", self.openModuleSettings
        )

        launch_chunks(usdviewApi)

    def configureView(self, _, plugUIBuilder) -> None:
        menu = plugUIBuilder.findOrCreateMenu("Chunks")
        menu.addItem(self._reloadChunksCommand, "SHIFT+R")
        menu.addItem(self._openModuleSettingsCommand)


Tf.Type.Define(ChunksPluginContainer)
