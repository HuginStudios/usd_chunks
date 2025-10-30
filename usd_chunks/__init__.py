"""Custom modules for the Usdview UI Framework.

This module provides a framework for dynamically loading and managing custom UI modules
"""

from pxr import Tf
from pxr.Usdviewq.plugin import PluginContainer
from pxr.Usdviewq import usdviewApi as UsdviewModule

import sys
from pathlib import Path

try:
    import attrs  # noqa: F401
except ImportError:
    p = Path(__file__).parent.parent / ".venv/Lib/site-packages"
    if p.exists():
        sys.path.append(str(p))

from . import chunk_registry, chunk_qt


def launch_chunks(usdviewApi) -> None:
    """Launch the UI Creator plugin.

    Args:
        usdviewApi: The Usdview API instance to use for module creation.
    """

    # The order of these does not matter. We can initialize the QT widgets before the ChunkRegistry since
    # the QT widget subscribes to the CHUNK_LOADED notice, and will add chunks when that notice is recieved.
    chunk_qt.build_chunk_docker(usdviewApi.qMainWindow)

    chunk_registry.ChunkRegistry().initialize(usdviewApi)

    print(f"Number of Registered Chunks {len(chunk_registry.ChunkRegistry().registered_modules)}")


class ChunksPlugin(PluginContainer):
    def reloadChunks(self, usdviewApi):
        reg = chunk_registry.ChunkRegistry()

        for name, mod in reg.registered_modules.items():
            reg.unload_chunk_module(mod)
            if not mod.loaded:
                reg.load_module(mod)

    def registerPlugins(self, plugRegistry, usdviewApi) -> None:
        """Register the UI Creator plugin with the plugin registry.

        Args:
            plugRegistry: The plugin registry to register the UI Creator plugin with.
            usdviewApi: The Usdview API instance to use for module creation.
        """

        self._reloadChunksCommand = plugRegistry.registerCommandPlugin(
            "ChunksPlugin.reloadChunks", "Reload Chunks", self.reloadChunks
        )

        launch_chunks(usdviewApi)

    def configureView(self, _, plugUIBuilder) -> None:
        menu = plugUIBuilder.findOrCreateMenu("Chunks")
        menu.addItem(self._reloadChunksCommand, "R")
        pass


Tf.Type.Define(ChunksPlugin)
