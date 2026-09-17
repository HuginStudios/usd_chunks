"""Host integrations for usd_chunks.

Everything under here binds the host-agnostic core in ``usd_chunks`` to a concrete
application: ``chunk_qt*`` (the widget and node-graph UI), ``chunk_usdview``/``chunk_kraken``/``chunk_houdini`` (per-DCC entry
points), and ``usdchunks_cli`` (the headless ``usdchunks`` command).

This is a subpackage rather than a top-level ``plugins`` package so that installing
usd_chunks cannot collide with any other distribution shipping that very common name.
"""
