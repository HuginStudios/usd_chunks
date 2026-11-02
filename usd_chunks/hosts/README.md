# Hosts

Each directory binds usd_chunks to one application. The UI itself lives in
`usd_chunks/frontends/`; usdview uses the Qt one.

**usdview** is the plugin loaded by usdview. Its USD plugin name is the full module path,
`usd_chunks.hosts.usdview`. Chunks that only work there still say
`load_for_plugin="chunk_usdview"` -- that string is the registry's host identifier, not a path.
When using this plugin for usdview, set

**PXR_PLUGINPATH_NAME**="path/to/usd_chunks/hosts/usdview"

or print the right path for an installed copy with `usdchunks plugin-path --host usdview`.

**houdini** makes every chunk a Solaris LOP, under TAB > usdchunks. Houdini wires the
chunks and decides when they cook; their params are native Houdini parms. Set it up by
pointing `HOUDINI_PACKAGE_DIR` at `houdini/packages` -- see
[docs/houdini.md](../../docs/houdini.md).

**Blender** has no folder here. Its add-on is in `usd_chunks/frontends/blender/addon`. See
[docs/blender.md](../../docs/blender.md) to install it.
