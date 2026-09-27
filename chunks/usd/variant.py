from __future__ import annotations
from usd_chunks import chunk, chunk_utils


def execute(node):
    stage = node.get_stage()
    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    primpath_param = node.get_param("primpath_location")
    if not primpath_param.value:
        return

    vs_list   = node.get_param("variant_entries", chunk.is_inst(chunk.ListGroup))
    vs_name_t  = vs_list.get_param("vs_name")
    v_names_t  = vs_list.get_param("v_names")
    v_default_t = vs_list.get_param("v_default")

    vsets: dict[str, tuple[list[str], str]] = {}
    for i in range(vs_list.size):
        vs_name   = vs_list.get_indexed_param(vs_name_t, i).value or ""
        v_names   = vs_list.get_indexed_param(v_names_t, i).value or ""
        v_default = vs_list.get_indexed_param(v_default_t, i).value or ""
        if vs_name:
            variants = [v.strip() for v in v_names.split(",") if v.strip()]
            vsets[vs_name] = (variants, v_default)

    for target_path in chunk_utils.author_in_variant_contexts(stage, primpath_param):
        target_prim = stage.OverridePrim(target_path)
        for vs_name, (variants, v_default) in vsets.items():
            vset = target_prim.GetVariantSets().AddVariantSet(vs_name)
            for v in variants:
                vset.AddVariant(v)
            if v_default and v_default in variants:
                vset.SetVariantSelection(v_default)


def main():
    chunk_api = chunk.Chunk("Add Variants")

    primpath = chunk.LineEdit(
        "primpath_location",
        "Prim Path",
        default_value="/prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
    )
    chunk_api.add_param(primpath)
    vs_list   = chunk.ListGroup("variant_entries")
    vs_name   = chunk.LineEdit("vs_name",   "Variant Set", flags=chunk.ChunkFlags.ParamFlags.LABELED)
    v_names   = chunk.LineEdit("v_names",   "Variants",    flags=chunk.ChunkFlags.ParamFlags.LABELED)
    v_default = chunk.LineEdit("v_default", "Default",     flags=chunk.ChunkFlags.ParamFlags.LABELED)
    vs_list.add_params([vs_name, v_names, v_default])
    vs_list.size = 1
    chunk_api.add_param(vs_list)

    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD,
        description="""
        Add Variants chunk — authors one or more variant sets onto one or more prims,
        using OverridePrim so the prims don't need to exist yet.

        Parameters:
          - Prim Path: comma-separated paths, Houdini-style glob patterns (*, **, ?),
            or a %name collection reference — the variant sets are added to every
            matched prim. Defaults to /prim. Supports embedded variant selections via
            author_in_variant_context.
          - Variant Entries (list): a repeatable row of two fields:
              - Variant Set: the name of the variant set to create (e.g. "shading").
              - Variants: comma-separated list of variant names to add to that set
                (e.g. "red, green, blue"). Whitespace around commas is trimmed.
            Rows with an empty Variant Set name are silently skipped.

        Each execution clears the edit-target layer first, then re-authors all
        variant sets from scratch, making the operation idempotent. Multiple
        variant sets can be created in a single chunk by adding more rows to
        the list.
        """,
    )
