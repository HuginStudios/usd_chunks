"""Prune Prim chunk — marks prims for removal from the composed result.

Since we can't remove specs from other layers non-destructively, this chunk
deactivates the prim AND authors a custom metadata flag that signals downstream
tools (flatten, export, render) to strip the prim entirely.

This is stronger than Mute: Prune communicates intent to permanently remove,
while Mute is a temporary bypass that can be toggled.

All edits authored on the chunk's own layer.
"""

from __future__ import annotations

from pxr import Usd

from usd_chunks import chunk, chunk_utils


# The metadata convention for marking pruned prims
_PRUNE_KEY = "usd_chunks:pruned"


def execute(node):
    stage = node.get_stage()
    if not stage:
        return

    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    primpath_param = node.get_param("prim_path")

    if not primpath_param.value:
        return

    for target_path in chunk_utils.iter_prim_targets(stage, primpath_param):
        prim = stage.GetPrimAtPath(target_path)
        if not prim.IsValid():
            prim = stage.OverridePrim(target_path)

        # Deactivate the prim
        prim.SetActive(False)

        # Author the pruned metadata flag
        custom_data = prim.GetMetadata("customData") or {}
        custom_data = dict(custom_data)
        custom_data[_PRUNE_KEY] = True
        prim.SetMetadata("customData", custom_data)


def _on_change(owner, notice, sender):
    """Callback for the chunk panel — prune selected prims."""
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

        # Deactivate
        over.SetActive(False)

        # Author pruned flag
        custom_data = over.GetMetadata("customData") or {}
        custom_data = dict(custom_data)
        custom_data[_PRUNE_KEY] = True
        over.SetMetadata("customData", custom_data)


def main():
    chunk_api = chunk.Chunk("Prune Prim")

    prim_path = chunk.LineEdit(
        "prim_path",
        "Prims",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="Select prim(s) to prune. Supports glob patterns and multi-selection.",
    )
    prim_path.add_callback(_on_change)
    chunk_api.add_param(prim_path)

    # Info label explaining the difference from Mute
    info = chunk.Label(
        "info_label",
        "Pruned prims are deactivated and flagged for removal on export/flatten.",
    )
    chunk_api.add_param(info)

    return chunk_api


def config():
    return chunk.DefaulConfig(
        category="Core USD",
        description="""
        Prune Prim — marks prims for permanent removal from the composed result.

        Parameters:
          - Prims: select prim(s) to prune. Supports glob patterns (*, **, ?),
            comma-separated paths, and %collection references.

        Unlike Mute (which is a reversible bypass), Prune communicates the
        intent to permanently remove the prim. It authors:
          1. SetActive(false) — immediately excludes from traversal/render
          2. customData["usd_chunks:pruned"] = true — metadata flag for
             downstream tools to strip the prim on flatten/export

        The metadata convention allows export and flatten utilities to
        distinguish "temporarily disabled" prims from "meant to be deleted"
        prims, enabling clean final outputs.

        All edits go to the chunk's own layer (non-destructive, reversible
        by removing the chunk).
        """,
    )
