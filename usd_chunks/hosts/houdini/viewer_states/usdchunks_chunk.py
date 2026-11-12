"""The chunk node's viewer state, which Houdini finds in viewer_states/ on HOUDINI_PATH.

Only the entry point Houdini calls at startup; the state is usd_chunks.hosts.houdini.viewer_state.
Guarded like pythonrc.py: a missing dependency leaves the node without handles, never
stops Houdini registering its other states.
"""
from __future__ import annotations

try:
    from usd_chunks.hosts.houdini import viewer_state
except ImportError:
    viewer_state = None


def createViewerStateTemplate():
    return viewer_state.create_template() if viewer_state is not None else None
