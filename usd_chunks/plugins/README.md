# Plugins

The files here should provide implementations for external frameworks. 

**chunk_qt.py** implements the qt widgets for the chunks.

**chunk_usdview** is the plugin loaded by usdview. We prefix with "chunk_" because it decides the name of the plugin when loaded in usd. Hence, naming it simply "usdview" would be confusing to debug. When using this plugin for usdview, make sure to set environment variable

**PXR_PLUGINPATH_NAME**="path/to/usd_chunks/plugins/chunk_usdview"
