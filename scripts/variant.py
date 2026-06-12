from usd_chunks import chunk, chunk_utils, chunk_notice


def execute(owner, notice, _sender):
    param = notice.data
    _chunk = param.get_chunk()
    if getattr(_chunk, '_executing', False):
        return
    _chunk._executing = True
    try:
        _execute(_chunk, owner)
    finally:
        _chunk._executing = False


def _execute(_chunk, owner):
    stage = owner.get_stage()

    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    raw_path = _chunk.get_param("primpath_location").value

    vs_list   = _chunk.get_param("variant_entries", chunk.is_inst(chunk.ListGroup))
    vs_name_t = vs_list.get_param("vs_name")
    v_names_t = vs_list.get_param("v_names")

    vsets: dict[str, list[str]] = {}
    for i in range(vs_list.size):
        vs_name = vs_list.get_indexed_param(vs_name_t, i).value or ""
        v_names = vs_list.get_indexed_param(v_names_t, i).value or ""
        if vs_name:
            variants = [v.strip() for v in v_names.split(",") if v.strip()]
            vsets[vs_name] = variants

    with chunk_utils.author_in_variant_context(stage, raw_path) as target_path:
        target_prim = stage.OverridePrim(target_path)
        for vs_name, variants in vsets.items():
            vset = target_prim.GetVariantSets().AddVariantSet(vs_name)
            for v in variants:
                vset.AddVariant(v)


def main():
    chunk_api = chunk.Chunk("Add Variants")

    primpath = chunk.LineEdit(
        "primpath_location",
        "Prim Path",
        default_value="/prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
    )
    chunk_api.add_param(primpath)
    primpath.add_callback(execute)

    vs_list = chunk.ListGroup("variant_entries")
    vs_name = chunk.LineEdit("vs_name", "Variant Set", flags=chunk.ChunkFlags.ParamFlags.LABELED)
    v_names = chunk.LineEdit("v_names", "Variants",    flags=chunk.ChunkFlags.ParamFlags.LABELED)
    vs_list.add_params([vs_name, v_names])
    vs_list.size = 1
    vs_list.add_callback(execute)
    chunk_api.add_param(vs_list)
    chunk_api.add_callback(execute, notice_alias=chunk_notice.ChunkNoticeAlias.CHUNK_LOADED)

    return chunk_api


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.EXAMPLES,
        description="""
        Add Variants chunk — authors one or more variant sets onto a prim at a
        given stage path, using OverridePrim so the prim doesn't need to exist yet.

        Parameters:
          - Prim Path: the stage path of the prim to receive the variant sets.
            Defaults to /prim. Supports embedded variant selections via
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
