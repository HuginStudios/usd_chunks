from usd_chunks import chunk, chunk_notice, chunk_registry



def variant_selection_callback(notice, sender):
    stage = sender.get_stage()
    if not stage:
        return
    primpath = sender.user_data.get("prim_path")
    if not primpath:
        return
    selected_prim = stage.GetPrimAtPath(primpath)
    if not selected_prim.IsValid():
        return
    vset = selected_prim.GetVariantSet(sender.label)
    vset.SetVariantSelection(sender.value)


def update_variant_selection(mychunk, notice, sender):
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

    if not selected_prim.HasVariantSets():
        label = chunk.Label("No variant exists.")
        mychunk.add_params([label])

    vsets = selected_prim.GetVariantSets()
    for vset_name in vsets.GetNames():
        vset = vsets.GetVariantSet(vset_name)
        variants = vset.GetVariantNames()
        menu = chunk.Menu(
            "menu",
            vset_name,
            menu_items=variants,
        )
        menu.default_value = vset.GetVariantSelection()
        menu.set_flag(chunk.ChunkFlags.ParamFlags.LABELED)
        menu.user_data = {"prim_path": str(selected_prim.GetPath())}
        mychunk.add_params([menu])
        menu.add_callback(variant_selection_callback)

    mychunk.print_hierarchy()
    chunk_notice.send_notice(chunk_notice.ChunkNoticeAlias.CHUNK_CHANGED, mychunk.get_owner())


def selection_changed(mychunk, notice: chunk_notice.PrimSelectionChangedNotice, sender: chunk_notice.UsdviewSender):
    if not notice:
        return
    selected_prim = None
    for selected_prim in notice.selected_prims:
        break
    if not selected_prim:
        return
    update(mychunk, selected_prim)


def main(api, parent=None):
    vSwitcherChunk = chunk.Chunk("Variant Switcher", category="Utility")
    reg = chunk_registry.ChunkRegistry()

    vSwitcherChunk.add_callback(
        selection_changed,
        sender=reg.usdview_sender,
        notice_alias=chunk_notice.ChunkNoticeAlias.PRIM_SELECTION_CHANGED,
        send_self=True,
    )

    conn = chunk_notice.NoticeConnection(
        update_variant_selection,
        chunk_notice.ChunkNoticeAlias.OBJECT_CHANGED,
        reg.usdview_sender,
        True,
        True,
        vSwitcherChunk,
    )
    vSwitcherChunk.add_connection(conn)

    return vSwitcherChunk
