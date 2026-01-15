from usd_chunks import chunk, chunk_notice, chunk_registry

from functools import wraps
from pxr import Tf, Usd

_type_map = {
    "float": chunk.Slider,
    "token": chunk.Menu,
    "bool": chunk.Checkbox,
    "string": chunk.LineEdit,
    "asset": chunk.LineEdit,
    "path": chunk.LineEdit,
    "float2": (chunk.Vector, {"param": chunk.Slider("0"), "length": 2}),
    "double2": (chunk.Vector, {"param": chunk.Slider("0"), "length": 2}),
    "float3": (chunk.Vector, {"param": chunk.Slider(), "length": 3, "suffixes": "XYZ"}),
    "double3": (chunk.Vector, {"param": chunk.Slider(), "length": 3, "suffixes": "XYZ"}),
}

def set_callback(param, notice, sender):
    attr = param.usd_attr
    if not attr:
        return
    stage = sender.get_stage()
    if not stage:
        return
    if attr.Get(0) != sender.value:
        if isinstance(sender.value, tuple):
            attr.Set(list(sender.value))
        else:
            attr.Set(sender.value)


def update_selection(mychunk, notice, sender):
    resyncVariantChanges = [
        str(p) for p in notice.GetResyncedPaths() if "variantSelection" in notice.GetChangedFields(p)
    ]
    if not resyncVariantChanges:
        return
    primpath = resyncVariantChanges[0]
    if not primpath:
        return
    stage = notice.GetStage()
    if not stage:
        return
    selected_prim = stage.GetPrimAtPath(primpath)
    if not selected_prim.IsValid():
        return
    update(mychunk, selected_prim)


def update(mychunk, selected_prim):
    # remove current params.
    for p in mychunk.get_my_params():
        p.request_delete()

    attributes = selected_prim.GetAttributes()
    for attr in attributes:
        attr_name = attr.GetName()
        type_name = attr.GetTypeName()

        print(attr_name, type_name)
        chunk_param_type = _type_map.get(str(type_name))
        print(chunk_param_type)
        if not chunk_param_type:
            continue
        if isinstance(chunk_param_type, tuple):
            attr_param = chunk_param_type[0](attr_name, **chunk_param_type[1])
        else:
            attr_param = chunk_param_type(attr_name)
        
        if attr_param.get_type() is chunk.ChunkFlags.ParamType.STRING_MENU:
            if allowed_tokens := attr.GetMetadata("allowedTokens"):
                attr_param.menu_items = [str(x) for x in allowed_tokens]

        if isinstance(chunk_param_type, tuple):
            for i, param in enumerate(attr_param.children.get_params()):
                print(attr.Get(0)[i])
                param.default_value = attr.Get(0)[i]
        else:
            attr_param.value = attr.Get(0)
            attr_param.default_value = attr.Get(Usd.TimeCode.Default())

        attr_param.set_flag(chunk.ChunkFlags.ParamFlags.LABELED)
       
        attr_param.usd_attr = attr

        attr_param.name = attr_name
        attr_param.add_callback(set_callback, send_self=True)
        mychunk.add_param(attr_param)

    #mychunk.add_params([vs_group])
    chunk_notice.send_notice(chunk_notice.ChunkNoticeAlias.CHUNK_CHANGED, mychunk.get_owner())


def selection_changed(mychunk, notice: chunk_notice.PrimSelectionChangedNotice, sender: chunk_notice.UsdviewSender):
    if not notice:
        return
    selected_prim = None
    for selected_prim in notice.selected_prims:
        print("Selected prim", selected_prim)
        break
    if not selected_prim:
        return
    update(mychunk, selected_prim)


def main(api, parent=None):
    attr_chunk = chunk.Chunk("Attribute Modifier", category="Utility")
    reg = chunk_registry.ChunkRegistry()

    attr_chunk.add_callback(
        selection_changed,
        sender=reg.usdview_sender,
        notice_alias=chunk_notice.ChunkNoticeAlias.PRIM_SELECTION_CHANGED,
        send_self=True,
    )

    conn = chunk_notice.NoticeConnection(
        update_selection,
        chunk_notice.ChunkNoticeAlias.OBJECT_CHANGED,
        reg.usdview_sender,
        True,
        True,
        attr_chunk,
    )
    attr_chunk.add_connection(conn)

    return attr_chunk
