"""Property Editor chunk -- select a prim and edit all its attributes interactively.

Dynamically creates typed UI widgets (sliders, menus, text fields, etc.) for
every attribute and relationship on the selected prim. Changes write directly
to the USD attribute via the chunk's param-to-attribute connection system.

This is the "inspector/editor" tool -- you point it at a prim and get full
interactive control over all its properties without knowing attribute names.
"""
from __future__ import annotations

from usd_chunks import chunk, chunk_utils


def build(myChunk: chunk.Chunk, notice, sender):
    """Rebuild the attribute widgets when the prim selection changes."""
    dynGrp = myChunk.get_param("dyn_grp", chunk.is_group)
    ppParm = myChunk.get_param("prim", chunk.is_inst(chunk.LineEdit))
    stage = myChunk.get_stage()

    # Clear and rebuild regardless of what's selected now.
    for grp in dynGrp.get_my_params():
        grp.request_delete()

    if not stage:
        return

    # Not get_prims(): a selection inside a variant comes back as /asset{color=red}/geo,
    # which Usd.Stage.GetPrimAtPath doesn't understand (it's a chunk_utils-only DSL), so
    # get_prims() would resolve it to nothing and the panel would silently show no
    # attributes at all. The loop yields /asset/geo with "red" selected, so the attribute
    # defaults read below resolve against the intended variant.
    for target_path in chunk_utils.author_in_variant_contexts(stage, ppParm):
        prim = stage.GetPrimAtPath(target_path)
        if not prim.IsValid():
            continue
        prim_grp = chunk.Group(prim.GetName())
        dynGrp.add_param(prim_grp)
        # Called inside the loop, so show_attribute records the variant it is in on every
        # generated param, and a later edit from its slider/menu re-enters it instead of
        # authoring outside it.
        chunk.show_attribute(prim, prim_grp, collapse_non_inputs=True)


def main():
    """Build a dynamic chunk that updates based on the currently selected prims.
    All supported prim attributes will be created as chunk parameters, and
    connected to their UsdAttributes respectively.
    """
    myChunk = chunk.Chunk("Property Editor")

    primSel = chunk.LineEdit(
        "prim",
        "Prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="Select prim(s) to inspect and edit their attributes.",
    )
    myChunk.add_param(primSel)

    dynGroup = chunk.Group("dyn_grp", flags=chunk.ChunkFlags.GroupFlags.FRAMED)
    myChunk.add_param(dynGroup)

    myChunk.add_callback(build, sender=primSel, send_on_create=True)

    return myChunk


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.PROPERTIES,
        description="""
        Property Editor — select a prim and interactively edit all its
        attributes and relationships.

        Parameters:
          - Prim: select one or more prims (supports viewport picking, globs, and a
            variant selection embedded in the path, e.g. /asset{color=red}/geo). The
            attribute panel rebuilds automatically on selection change.

        For each selected prim, a collapsible group appears containing typed
        widgets for every attribute (Slider for float, Menu for token, LineEdit
        for string, Vector for float3, etc.) and relationships. Changes write
        directly to the USD attribute via the chunk's notice system -- inside the
        selected variant's own edit context when the Prim path named one, so an edit
        never leaks out to override every variant at once.

        Namespace groups (inputs:, xformOp:, etc.) are organized into
        collapsible sub-groups. The 'inputs' namespace is expanded by default.

        In the node graph: accepts a prim input port. Outputs the prim for
        downstream processing.
        """,
    )
