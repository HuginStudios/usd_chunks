from usd_chunks import chunk, chunk_utils


def execute(node):
    stage = node.get_stage()
    if not stage:
        return

    primpath_param = node.get_param("primpath_location")
    raw_path = primpath_param.value
    prim_type = node.get_param("prim_type").value
    _, _, var_selections = chunk_utils.split_variant_path(raw_path)

    # Authored at the path the parameter holds, exactly as given. Uniquing belongs to
    # creation -- main() picks a free default so a new chunk does not land on an existing
    # prim -- and a path that has been set since is an instruction, not a request for a
    # free name. Re-uniquing it on every cook meant a path you typed could be silently
    # replaced by one you did not, and a chunk targeting the same prim as another (a
    # deliberate override, which is ordinary USD) was impossible to express.

    with chunk_utils.author_in_variant_context(stage, raw_path) as target_path:
        prim = stage.DefinePrim(target_path, prim_type)
        if not var_selections:
            actual_path = prim.GetPath().pathString
            raw_stored = primpath_param.raw_value
            if not chunk_utils.has_expression(raw_stored or "") and actual_path != (raw_stored or ""):
                primpath_param.value = actual_path


def main():
    chunk_api = chunk.Chunk("Prim")

    # prim location
    stage = chunk_api.get_stage()
    prim_path = chunk_utils.get_unique_path(stage, "/prim")
    primpath = chunk.LineEdit(
        "primpath_location",
        "Prim Path",
        default_value=prim_path,
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_TARGET,
    )
    chunk_api.add_param(primpath)
    prim_type = chunk.Menu(
        "prim_type",
        "Prim type",
        menu_items=["Xform", "Scope", "Class"],
    )
    chunk_api.add_param(prim_type)

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
