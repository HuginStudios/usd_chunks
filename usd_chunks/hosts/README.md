# Hosts

Each directory binds usd_chunks to one application. The UI itself lives in
`usd_chunks/frontends/`; usdview uses the Qt one.

**usdview** is the plugin loaded by usdview. Its USD plugin name is the full module path,
`usd_chunks.hosts.usdview`. Chunks that only work there still say
`load_for_plugin="chunk_usdview"` -- that string is the registry's host identifier, not a path.
When using this plugin for usdview, set

**PXR_PLUGINPATH_NAME**="path/to/usd_chunks/hosts/usdview"

or print the right path for an installed copy with `usdchunks plugin-path --host usdview`.
