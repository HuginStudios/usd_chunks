import re
from usd_chunks import chunk, chunk_notice, chunk_core

def show_attribute(prim, _chunk):
    attr_grp = _chunk.get_param("attr_grp", chunk.is_group)

    # clean params.
    for grp in attr_grp.get_my_params():
        grp.request_delete()

    prim_grp = chunk.Group("Attributes")
    attr_grp.add_param(prim_grp)

    # generate params
    attributes = prim.GetAttributes()
    groups = {}
    for attr in attributes:
        if not (param := chunk.param_from_attr(attr)):
            continue

        if not isinstance(param, chunk.ChunkGroupBase):
            param.set_flag(chunk_core.ChunkFlags.ParamFlags.LABELED)

        ns = attr.GetNamespace()
        tgt_grp = prim_grp if not ns else groups.get(ns)

        if not tgt_grp:
            tgt_grp = chunk.Group(
                ns,
                flags=chunk_core.ChunkFlags.GroupFlags.COLLAPSABLE | chunk_core.ChunkFlags.GroupFlags.LABELED,
            )
            if ns and ns != "inputs":
                tgt_grp.set_state(chunk_core.ChunkFlags.State.COLLAPSED)

            prim_grp.add_param(tgt_grp)
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
        tgt_grp = prim_grp if not ns else groups.get(ns)

        if not tgt_grp:
            tgt_grp = chunk.Group(
                ns,
                flags=chunk_core.ChunkFlags.GroupFlags.COLLAPSABLE | chunk_core.ChunkFlags.GroupFlags.LABELED,
            )
            prim_grp.add_param(tgt_grp)
            groups[ns] = tgt_grp

        if param and tgt_grp:
            tgt_grp.add_param(param)


def main():
    chunk_api = chunk.Chunk("Light")

    # prim location
    stage = chunk_api.get_stage()
    prim_path = get_unique_path(stage, "/lights/light1")
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

    chunk_api.add_callback(execute, notice_alias=chunk_notice.ChunkNoticeAlias.CHUNK_LOADED)

    attribute_group = chunk.Group("attr_grp", flags=chunk.ChunkFlags.GroupFlags.FRAMED)
    chunk_api.add_param(attribute_group)

    return chunk_api


def get_unique_path(stage, prim_path):
    if not stage.GetPrimAtPath(prim_path).IsValid():
        return prim_path

    # Strip any trailing digits to get the base
    base = re.sub(r'\d+$', '', prim_path)

    count = 1
    while stage.GetPrimAtPath(f"{base}{count}").IsValid():
        count += 1

    return f"{base}{count}"

def execute(owner, notice, sender):
    param = notice.data
    _chunk = param.get_chunk()
    stage = owner.get_stage()
    # Clear any edits in this layer before importing a reference again.
    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()
    prim_path = _chunk.get_param("primpath_location").value
    light_type = _chunk.get_param("light_type").value

    prim_path = get_unique_path(stage, prim_path)
    prim = stage.DefinePrim(prim_path, light_type)
    
    if prim.GetPath().pathString != prim_path:
        _chunk.get_param("primpath_location").value = prim.GetPath().pathString
    show_attribute(prim, _chunk)


def config():
    return chunk.DefaulConfig(category=chunk.StandardCategory.EXAMPLES)

