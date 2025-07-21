"""Custom modules for the Usdview UI Framework.

This module provides a framework for dynamically loading and managing custom UI modules
"""
import sys
from typing import Optional
from pxr import Tf
from pxr.Usdviewq.plugin import PluginContainer 
from pxr.Usdviewq import usdviewApi as UsdviewModule

from . import custom_modules


class ChunksPlugin(PluginContainer):
    usdview_plugin: Optional[custom_modules.FileReader] = None


    def launch(self, usdviewApi: UsdviewModule) -> None:
        """Launch the UI Creator plugin.
        
        Args:
            usdviewApi: The Usdview API instance to use for module creation.
        """
        self.usdview_plugin = custom_modules.ChunksReader(
            usdviewApi, custom_modules.FileReader()
        )
        print(f"Number of Registered Chunks {len(self.usdview_plugin.registered_modules)}")

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
