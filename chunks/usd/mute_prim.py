"""Mute Prim chunk -- deactivates (bypasses) prims on the stage.

Sets SetActive(false) on selected prims, effectively removing them from the
composed stage without deleting the prim spec. This is the non-destructive
"mute" or "bypass" -- the prim data remains in its layers but is excluded
from traversal and rendering.

To unmute, simply disable this chunk/node (D key) or disconnect the prims.

Supports multiple prims at once via glob patterns and multi-selection.
All edits authored on the chunk's own layer.
"""

from __future__ import annotations


from usd_chunks import chunk, chunk_utils


def execute(node):
    stage = node.get_stage()
    if not stage:
        return

    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    primpath_param = node.get_param("prim_path")

    if not primpath_param.value:
        return

    for target_path in chunk_utils.author_in_variant_contexts(stage, primpath_param):
        prim = stage.GetPrimAtPath(target_path)
        if not prim.IsValid():
            prim = stage.OverridePrim(target_path)
        prim.SetActive(False)


def _on_change(owner, notice, sender):
    """Callback for the chunk panel -- deactivate selected prims."""
    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage or not _chunk:
        return

    prim_param = _chunk.get_param("prim_path")
    prims = prim_param.get_prims()
    if not prims:
        return

    layer = owner.get_edit_context()
    if not layer:
        return
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)

    for prim in prims:
        over = stage.OverridePrim(prim.GetPath())
        over.SetActive(False)


def main():
    chunk_api = chunk.Chunk("Mute Prim")

    prim_path = chunk.LineEdit(
        "prim_path",
        "Prims",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip=(
            "Select prim(s) to mute (deactivate). Supports glob patterns and multi-selection.\n"
            "Disable this node (D) to unmute."
        ),
    )
    prim_path.add_callback(_on_change)
    chunk_api.add_param(prim_path)

    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD,
        description="""
        Mute Prim — deactivates (bypasses) prims on the stage via SetActive(false).

        Parameters:
          - Prims: select prim(s) to mute. Supports glob patterns (*, **, ?),
            comma-separated paths, and %collection references.

        Deactivated prims remain in the layer stack but are excluded from
        stage traversal and rendering — a non-destructive bypass. The original
        prim data is untouched; only an active=false opinion is authored on
        the chunk's own layer.

        To unmute: disable this node (D key) or disconnect the prims.
        The chunk layer is cleared when disabled, restoring the prims.

        Difference from Prune: Mute is temporary (session bypass). Prune
        marks prims for permanent removal on export/flatten.
        """,
    )
