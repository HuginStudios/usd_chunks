from usd_chunks import chunk, chunk_utils, chunk_notice

def execute(owner, notice, sender):
    param = notice.data
    _chunk = param.get_chunk()
    stage = owner.get_stage()

    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    raw_path = _chunk.get_param("primpath_location").value
    prim_type = _chunk.get_param("prim_type").value
    _, _, var_selections = chunk_utils.split_variant_path(raw_path)

    if not var_selections:
        raw_path = chunk_utils.get_unique_path(stage, raw_path)

    with chunk_utils.author_in_variant_context(stage, raw_path) as target_path:
        prim = stage.DefinePrim(target_path, prim_type)
        if not var_selections and prim.GetPath().pathString != target_path:
            _chunk.get_param("primpath_location").value = prim.GetPath().pathString


def main():
    chunk_api = chunk.Chunk("Prim")

    # prim location
    stage = chunk_api.get_stage()
    prim_path = chunk_utils.get_unique_path(stage, "/prim")
    primpath = chunk.LineEdit(
        "primpath_location",
        "Prim Path",
        default_value=prim_path,
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
    )
    chunk_api.add_param(primpath)
    primpath.add_callback(execute)

    prim_type = chunk.Menu(
        "prim_type",
        "Prim type",
        menu_items=["Xform", "Scope", "Class"],
    )
    chunk_api.add_param(prim_type)
    prim_type.add_callback(execute)

    chunk_api.add_callback(execute, notice_alias=chunk_notice.ChunkNoticeAlias.CHUNK_LOADED)

    return chunk_api


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.EXAMPLES,
        description="""
        Prim chunk — defines a new USD prim at a given path on the active stage.

        Parameters:
          - Prim Path: the stage path where the prim will be created. Auto-initialised
            to a unique /prim path on load. If the path is already taken (and no
            variant selection is embedded), a unique suffix is chosen and the field
            is updated to reflect the actual path.
          - Prim Type: the USD prim type to define — either Xform, Scope or Class.

        When the path contains an embedded variant selection (e.g. /root{var=val}/child),
        uniqueness is not enforced and the prim is authored directly inside that
        variant via author_in_variant_context. Outside of variants the chunk clears
        its edit-target layer before each execution so re-runs are idempotent.
        """,
    )
