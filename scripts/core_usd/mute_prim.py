"""Mute Prim chunk — deactivates (bypasses) prims on the stage.

Sets SetActive(false) on selected prims, effectively removing them from the
composed stage without deleting the prim spec. This is the non-destructive
"mute" or "bypass" — the prim data remains in its layers but is excluded
from traversal and rendering.

Supports multiple prims at once via glob patterns and multi-selection.
All edits authored on the chunk's own layer.
"""

from __future__ import annotations

from pxr import Usd

from usd_chunks import chunk, chunk_utils


def execute(node):
    stage = node.get_stage()
    if not stage:
        return

    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    primpath_param = node.get_param("prim_path")
    mute_enabled = node.get_param("mute_enabled").value

    if not primpath_param.value:
        return

    for target_path in chunk_utils.iter_prim_targets(stage, primpath_param):
        prim = stage.GetPrimAtPath(target_path)
        if not prim.IsValid():
            prim = stage.OverridePrim(target_path)
        prim.SetActive(not mute_enabled)


def _on_change(owner, notice, sender):
    """Callback for the chunk panel — toggle active state on selected prims."""
    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage or not _chunk:
        return

    prim_param = _chunk.get_param("prim_path")
    mute_enabled = _chunk.get_param("mute_enabled").value

    prims = prim_param.get_prims()
    if not prims:
        return

    layer = owner.get_edit_context()
    if not layer:
        return
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)

    for prim in prims:
        # Author an override with the active state
        over = stage.OverridePrim(prim.GetPath())
        over.SetActive(not mute_enabled)


def main():
    chunk_api = chunk.Chunk("Mute Prim")

    prim_path = chunk.LineEdit(
        "prim_path",
        "Prims",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="Select prim(s) to mute/unmute. Supports glob patterns and multi-selection.",
    )
    prim_path.add_callback(_on_change)
    chunk_api.add_param(prim_path)

    mute_enabled = chunk.Checkbox(
        "mute_enabled",
        "Mute (Deactivate)",
        default_value=True,
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="When checked, selected prims are deactivated (SetActive=false). Uncheck to reactivate.",
    )
    mute_enabled.add_callback(_on_change)
    chunk_api.add_param(mute_enabled)

    return chunk_api


def config():
    return chunk.DefaulConfig(
        category="Core USD",
        description="""
        Mute Prim — deactivates (bypasses) prims on the stage via SetActive(false).

        Parameters:
          - Prims: select prim(s) to mute. Supports glob patterns (*, **, ?),
            comma-separated paths, and %collection references.
          - Mute (Deactivate): checkbox to toggle the mute state. When checked,
            prims are deactivated. Uncheck to reactivate.

        Deactivated prims remain in the layer stack but are excluded from
        stage traversal and rendering — a non-destructive bypass. The original
        prim data is untouched; only an active=false opinion is authored on
        the chunk's own layer.

        In the node graph, accepts prim input and outputs the muted prim(s).
        """,
    )
