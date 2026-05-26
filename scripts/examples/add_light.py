from usd_chunks import chunk, chunk_notice, chunk_core, chunk_utils
from pxr import UsdGeom

def show_attribute(prim, _chunk):
    attr_grp = _chunk.get_param("attr_grp", chunk.is_group)

    # clean params.
    for grp in attr_grp.get_my_params():
        grp.request_delete()

    # generate params
    attributes = prim.GetAttributes()
    groups = {}
    for attr in attributes:
        if not (param := chunk.param_from_attr(attr)):
            continue

        if not isinstance(param, chunk.ChunkGroupBase):
            param.set_flag(chunk_core.ChunkFlags.ParamFlags.LABELED)

        ns = attr.GetNamespace()
        tgt_grp = attr_grp if not ns else groups.get(ns)

        if not tgt_grp:
            tgt_grp = chunk.Group(
                ns,
                flags=chunk_core.ChunkFlags.GroupFlags.COLLAPSABLE | chunk_core.ChunkFlags.GroupFlags.LABELED,
            )
            if ns and ns != "inputs":
                tgt_grp.set_state(chunk_core.ChunkFlags.State.COLLAPSED)

            attr_grp.add_param(tgt_grp)
            groups[ns] = tgt_grp

        if param:
            tgt_grp.add_param(param)

    relationships = prim.GetRelationships()
    # Do the same for relationships
    for rel in relationships:
        if not (param := chunk.param_from_rel(rel)):
            continue

        if not isinstance(param, chunk.ChunkGroupBase):
            param.set_flag(chunk_core.ChunkFlags.ParamFlags.LABELED)

        ns = rel.GetNamespace()
        tgt_grp = attr_grp if not ns else groups.get(ns)

        if not tgt_grp:
            tgt_grp = chunk.Group(
                ns,
                flags=chunk_core.ChunkFlags.GroupFlags.COLLAPSABLE | chunk_core.ChunkFlags.GroupFlags.LABELED,
            )
            attr_grp.add_param(tgt_grp)
            groups[ns] = tgt_grp

        if param and tgt_grp:
            tgt_grp.add_param(param)


def main():
    chunk_api = chunk.Chunk("Light")

    # prim location
    stage = chunk_api.get_stage()
    prim_path = chunk_utils.get_unique_path(stage, "/lights/light1")
    primpath = chunk.LineEdit(
        "primpath_location",
        "Prim Path",
        default_value=prim_path,
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
    )
    chunk_api.add_param(primpath)
    primpath.add_callback(execute)

    light_type = chunk.Menu(
        "light_type",
        "Light type",
        menu_items=["DiskLight", "RectLight", "DomeLight", "DistantLight", "SphereLight", "CylinderLight"],
    )
    chunk_api.add_param(light_type)
    light_type.add_callback(execute)

    attribute_group = chunk.Group("attr_grp", flags=chunk.ChunkFlags.GroupFlags.FRAMED)
    chunk_api.add_param(attribute_group)

    chunk_api.add_callback(execute, notice_alias=chunk_notice.ChunkNoticeAlias.CHUNK_LOADED)

    return chunk_api


def execute(owner, notice, sender):
    param = notice.data
    _chunk = param.get_chunk()
    stage = owner.get_stage()
    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    raw_path = _chunk.get_param("primpath_location").value
    light_type = _chunk.get_param("light_type").value
    _, _, var_selections = chunk_utils.split_variant_path(raw_path)
    if not var_selections:
        raw_path = chunk_utils.get_unique_path(stage, raw_path)

    with chunk_utils.author_in_variant_context(stage, raw_path) as target_path:
        prim = stage.DefinePrim(target_path, light_type)
        attr = UsdGeom.VisibilityAPI.Apply(prim).CreateGuideVisibilityAttr()
        attr.Set(UsdGeom.Tokens.inherited)
        if not var_selections and prim.GetPath().pathString != target_path:
            _chunk.get_param("primpath_location").value = prim.GetPath().pathString
        #show_attribute(prim, _chunk)


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.EXAMPLES,
        description="""
        Light chunk — creates a USD light prim of a chosen type at a given path
        on the active stage.

        Parameters:
          - Prim Path: stage path for the light prim. Auto-initialised to a unique
            /lights/light1 path on load. Updated in-place if the path was already
            taken (outside of variant contexts).
          - Light Type: the UsdLux light schema to define — DiskLight, RectLight,
            DomeLight, DistantLight, SphereLight, or CylinderLight.

        On any change the edit-target layer is cleared and the prim re-defined,
        keeping authoring idempotent. GuideVisibility is explicitly set to
        inherited so the light is visible in DCC guide modes. Variant-path
        authoring is supported via author_in_variant_context — uniqueness checks
        are skipped when a variant selection is embedded in the path.
        """,
    )

