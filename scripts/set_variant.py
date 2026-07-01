from usd_chunks import chunk, chunk_utils


def execute(node):
    stage = node.get_stage()
    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    raw_path  = node.get_param("primpath_location").value
    vs_name   = node.get_param("variant_set").value or ""
    selection = node.get_param("variant_selection").value or ""

    if not vs_name:
        return

    with chunk_utils.author_in_variant_context(stage, raw_path) as target_path:
        prim = stage.OverridePrim(target_path)
        vset = prim.GetVariantSets().GetVariantSet(vs_name)
        if not vset:
            return
        if selection:
            vset.SetVariantSelection(selection)
        else:
            vset.ClearVariantSelection()


def main():
    chunk_api = chunk.Chunk("Set Variant")

    primpath = chunk.LineEdit(
        "primpath_location", "Prim Path",
        default_value="/prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
    )
    vs_name = chunk.LineEdit(
        "variant_set", "Variant Set",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
    )
    selection = chunk.LineEdit(
        "variant_selection", "Variant Selection",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
    )

    chunk_api.add_param(primpath)
    chunk_api.add_param(vs_name)
    chunk_api.add_param(selection)

    return chunk_api


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.EXAMPLES,
        description="""
        Set Variant chunk — authors an active variant selection on a prim.

        Separate from Add Variants (which defines which variant sets and names
        exist): this chunk controls which variant is currently selected, authoring
        an OverridePrim opinion with the chosen selection. Clearing the selection
        field removes the selection opinion entirely.

        Designed to be the final node in a variant network so the selection can
        be lofted or overridden independently of the variant set definitions.
        """,
    )
