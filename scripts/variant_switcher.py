from usd_chunks import chunk, chunk_notice, chunk_registry


def variant_selection_callback(owner: chunk.ChunkParamBase, notice, sender):
    stage = owner.get_stage()
    if not stage:
        return
    primpath = owner.user_data.get("prim_path")
    if not primpath:
        return
    selected_prim = stage.GetPrimAtPath(primpath)
    if not selected_prim.IsValid():
        return

    if owner.label:
        vset = selected_prim.GetVariantSet(owner.label)
    if owner.value is not None:
        vset.SetVariantSelection(owner.value)


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


def update(mychunk: chunk.Chunk, selected_prim):
    # remove current params.
    for p in mychunk.get_params():
        mychunk.remove_param_or_group(p)

    if not selected_prim.HasVariantSets():
        label = chunk.Label("No variant exists.")
        mychunk.add_params([label])
        return

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


def selection_changed(mychunk, notice: chunk_notice.PrimSelectionChangedNotice, sender: chunk_notice.SceneSender):
    if not notice:
        return
    selected_prim = None
    for selected_prim in notice.data:
        break
    if not selected_prim:
        return
    update(mychunk, selected_prim)


def main():
    vSwitcherChunk = chunk.Chunk("Variant Switcher")
    reg = chunk_registry.ChunkRegistry()

    vSwitcherChunk.add_callback(
        selection_changed,
        sender=reg.scene,
        notice_alias=chunk_notice.ChunkNoticeAlias.PRIM_SELECTION_CHANGED,
    )

    vSwitcherChunk.add_callback(
        update_variant_selection, sender=reg.scene, notice_alias=chunk_notice.ChunkNoticeAlias.OBJECT_CHANGED
    )

    return vSwitcherChunk

def config():
    return chunk.DefaulConfig(category="Utility")
