"""Custom modules for the Usdview UI Framework.

This module provides a framework for dynamically loading and managing custom UI modules
"""

from pxr import Tf
from pxr.Usdviewq.plugin import PluginContainer
from pxr.Usdviewq import usdviewApi as UsdviewModule

from . import chunk_registry, chunk_qt


class ChunksPlugin(PluginContainer):
    def launch(self, usdviewApi: UsdviewModule) -> None:
        """Launch the UI Creator plugin.

        Args:
            usdviewApi: The Usdview API instance to use for module creation.
        """

        # The order of these does not matter. We can initialize the QT widgets before the ChunkRegistry since
        # the QT widget subscribes to the CHUNK_LOADED notice, and will add chunks when that notice is recieved.  
        chunk_qt.build_chunk_docker(usdviewApi.qMainWindow)
        chunk_registry.ChunkRegistry().initialize(usdviewApi)

        print(f"Number of Registered Chunks {len(chunk_registry.ChunkRegistry().registered_modules)}")

    def registerPlugins(self, plugRegistry, usdviewApi: UsdviewModule) -> None:
        """Register the UI Creator plugin with the plugin registry.

        Args:
            plugRegistry: The plugin registry to register the UI Creator plugin with.
            usdviewApi: The Usdview API instance to use for module creation.
        """
        self.launch(usdviewApi)

    def configureView(self, _, plugUIBuilder) -> None:
        pass


Tf.Type.Define(ChunksPlugin)
