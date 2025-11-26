from usd_chunks import chunk, chunk_interface, chunk_registry, chunk_notice

from pxr import Sdf, Usd, Tf

from typing import Dict

_type_map = {
    "float": chunk.Slider,
    "int": (chunk.Spinbox, {"is_int": True}),
    "token": chunk.Menu,
    "bool": chunk.Checkbox,
    "string": chunk.LineEdit,
    "asset": chunk.LineEdit,
    "path": chunk.LineEdit,
    "float2": (chunk.Vector, {"param": chunk.Slider("0"), "length": 2}),
    "float3": (chunk.Vector, {"param": chunk.Slider("0"), "length": 3}),
}


def create_attr_param(attr_def, prop_name, prim):
    type_token = attr_def.GetTypeNameToken()

    t = _type_map.get(type_token)
    if not t:
        return None

    param_name = prop_name.split(":")[-1]

    if isinstance(t, tuple):
        param = t[0](param_name, **t[1])
    else:
        param = t(param_name)

    attr = prim.GetAttribute(prop_name)

    param.set_flag(chunk.ChunkFlags.ParamFlags.LABELED)
    param.user_data = {"primpath": str(prim.GetPath()), "attrname": prop_name}

    param_type = param.get_type()

    if param_type is chunk.ChunkFlags.ParamType.STRING_MENU:
        if allowed_tokens := attr_def.GetMetadata("allowedTokens"):
            param.menu_items = [str(x) for x in allowed_tokens]

    if attr.HasFallbackValue():
        param.default_value = attr_def.GetFallbackValue()
    else:
        param.default_value = attr.Get(0)

    param.value = attr.Get(0)

    if (chunk.ChunkFlags.ParamType.FLOAT | chunk.ChunkFlags.ParamType.INT) not in param_type:
        return param

    if limits := attr.GetLimits():
        if hard := limits.get("hard"):
            limit_min = hard.get("minimum", None)
            limit_max = hard.get("maximum", None)

            if hasattr(param, "min_value"):
                param.min_value = limit_min
            if hasattr(param, "max_value"):
                param.max_value = limit_max
    return param


def create_rel_param(attr_def, prop_name, prim):
    param_name = prop_name.split(":")[-1]

    rel = prim.GetRelationship(prop_name)
    if not rel:
        return

    param = chunk.LineEdit(param_name)
    param.set_flag(chunk.ChunkFlags.ParamFlags.LABELED)
    param.user_data = {"primpath": str(prim.GetPath()), "relname": prop_name}


    param.value = ",".join([str(x) for x in rel.GetForwardedTargets()])
    return param


def param_changed(notice, sender: chunk.ChunkParamBase):
    stage = sender.get_stage()
    if not stage:
        return

    primpath = sender.user_data.get("primpath")
    if not primpath:
        return

    attrname = sender.user_data.get("attrname")

    if not attrname:
        relname = sender.user_data.get("relname")
        if not relname:
            return
        
        prim = stage.GetPrimAtPath(primpath)
        attr = prim.GetRelationship(relname)

        old_val = attr.GetForwardedTargets()
        new_val = sender.value

        if new_val is None or new_val == old_val:
            return

        attr.SetTargets(new_val.split(","))
        return

    prim = stage.GetPrimAtPath(primpath)
    attr = prim.GetAttribute(attrname)

    old_val = attr.Get(0)
    new_val = sender.value

    if new_val == old_val:
        return

    attr.Set(new_val)


def update_chunk(myChunk: chunk.Chunk, notice, sender: chunk_notice.UsdviewSender):
    stage = myChunk.get_stage()

    registry = Usd.SchemaRegistry()

    for p in myChunk.get_my_params():
        p.request_delete()

    if not stage:
        return

    for prim in sender.selectedPrims:
        schemas = prim.GetAppliedSchemas()
        for api in schemas:
            api_def = registry.FindAppliedAPIPrimDefinition(api)

            group = chunk.Group(api)
            group.set_flag(chunk.ChunkFlags.GroupFlags.COLLAPSABLE)
            group.set_state(chunk.ChunkFlags.State.COLLAPSED)

            for prop_name in api_def.GetPropertyNames():
                if attr_def := api_def.GetAttributeDefinition(prop_name):
                    param = create_attr_param(attr_def, prop_name, prim)
                elif rel_def := api_def.GetRelationshipDefinition(prop_name):
                    param = create_rel_param(rel_def, prop_name, prim)
                else:
                    continue

                if not param:
                    continue

                param.add_callback(param_changed)
                group.add_param(param)

            myChunk.add_param(group)


def main(api, parent=None):
    myChunk = chunk.Chunk("APISchema", category="Attributes")

    reg = chunk_registry.ChunkRegistry()

    conn = chunk_notice.NoticeConnection(
        update_chunk, chunk_notice.ChunkNoticeAlias.PRIM_SELECTION_CHANGED, reg.usdview_sender, True, True, myChunk
    )
    myChunk.add_connection(conn)

    return myChunk
