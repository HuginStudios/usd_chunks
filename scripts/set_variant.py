from usd_chunks import chunk, chunk_utils

#: The Variant Selection entry that clears the selection instead of setting one.
NONE = "(none)"

#: Set on the chunk once CHUNK_LOADED has fired -- its values are in, the menus may fill.
_IN_SCENE = "_set_variant_in_scene"


def _chunk_loaded(owner, notice, sender):
    setattr(owner.get_chunk(), _IN_SCENE, True)
    _refresh_menus(owner, notice, sender)


def _first_target_prim(node):
    """The first prim the Prim Path resolves to, or None.

    The menus are read off one prim: a path pattern may match many, but a set of variant
    sets that is the union across them would offer names most of the prims do not have.
    The variant-selection part of a path is stripped before the lookup, the way every
    authoring path does it (/asset{color=red}/geo is /asset/geo on the stage).
    """
    stage = node.get_stage()
    param = node.get_param("primpath_location")
    if not stage or param is None or not param.value:
        return None
    for path_str in param.resolve_paths():
        _anchor, target, _selections = chunk_utils.split_variant_path(path_str)
        prim = stage.GetPrimAtPath(target)
        if prim and prim.IsValid():
            return prim
    return None


def _set_items(menu, items) -> None:
    """Put `items` on `menu`, keeping whatever it currently says visible.

    The current value stays in the list even when the prim no longer offers it: a saved
    selection arrives before the upstream chunk that defines its variant set has cooked,
    and a view-driven recook mutes layers one at a time, so "not on the prim right now" is
    routinely transient. Dropping the value would lose it for good; listing it keeps the
    dropdown honest about what will be authored, and it disappears on its own once a
    real choice replaces it.

    Only assigned when something changed: menu_items is a plain field, and writing it
    re-triggers this chunk's execute() whether or not anything differs (see create_layer).
    """
    current = menu.value or ""
    items = list(items)
    if current and current not in items and not chunk_utils.has_expression(current):
        items.append(current)
    if list(menu.menu_items or []) != items:
        menu.menu_items = items


def _refresh_menus(owner, _notice=None, _sender=None):
    """Rebuild Variant Set from the target prim, and Variant Selection from the chosen set.

    Registered on the prim-path and variant-set params, and globally on stage content
    changes -- the sets a prim carries usually come from a chunk upstream, so they appear
    (and change) as the graph cooks, not when this chunk's own params move. Global means
    it fires for every Set Variant instance on every stage edit, hence the guard: a
    blocked chunk cannot usefully act on fresh menus.

    Held off only until this chunk is in the scene (CHUNK_LOADED), not for the whole of a
    load. Two things pull in opposite directions there. A saved value is restored before
    the chunk is added, and a Menu coerces a value to its first item when the list does
    not hold it -- so a list built before the restore turned a saved "red" into "(none)".
    But the upstream chunk that defines the set cooks later in the same load, and that
    cook is the last stage edit of it: refresh nothing during the load and the lists stay
    empty until something else happens to the stage. So: nothing until the values are in,
    everything after. Rebuilding then is safe because nothing here ever writes a value: a
    restored value no list offers yet is kept listed (_set_items) until the stage catches
    up with it.
    """
    _chunk = owner.get_chunk()
    if _chunk is None or _chunk.is_blocked() or not getattr(_chunk, _IN_SCENE, False):
        return
    vs_menu = _chunk.get_param("variant_set")
    sel_menu = _chunk.get_param("variant_selection")
    if vs_menu is None or sel_menu is None:
        # Deleting the chunk tears its params down while the stage edits that teardown
        # makes are still firing this global handler for it.
        return

    prim = _first_target_prim(_chunk)
    vsets = prim.GetVariantSets() if prim is not None else None
    _set_items(vs_menu, vsets.GetNames() if vsets else [])

    chosen = vs_menu.value or ""
    variants = []
    if vsets and chosen and chosen in vsets.GetNames():
        variants = list(vsets.GetVariantSet(chosen).GetVariantNames())
    _set_items(sel_menu, [NONE] + variants)


def execute(node):
    stage = node.get_stage()
    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    primpath_param = node.get_param("primpath_location")
    vs_name   = node.get_param("variant_set").value or ""
    selection = node.get_param("variant_selection").value or ""
    if selection == NONE:
        selection = ""

    if not vs_name or not primpath_param.value:
        return

    for target_path in chunk_utils.iter_prim_targets(stage, primpath_param):
        prim = stage.OverridePrim(target_path)
        vset = prim.GetVariantSets().GetVariantSet(vs_name)
        if not vset:
            continue
        if selection:
            vset.SetVariantSelection(selection)
        else:
            vset.ClearVariantSelection()


def main():
    chunk_api = chunk.Chunk("Set Variant")

    primpath = chunk.LineEdit(
        "primpath_location", "Prim Path",
        default_value="/prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
    )
    # Menus, filled from the prim: the sets it carries, then the variants of the chosen
    # set. An empty menu accepts any value (Menu.convert_attr only checks a value against a
    # non-empty list), which is what lets a saved selection load before the menus exist and
    # an @Node.param@ expression stand in for either.
    vs_name = chunk.Menu(
        "variant_set", "Variant Set",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="The variant sets on the first prim Prim Path resolves to.",
    )
    selection = chunk.Menu(
        "variant_selection", "Variant Selection",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="The variants of the chosen set. (none) clears the selection instead.",
    )

    chunk_api.add_param(primpath)
    chunk_api.add_param(vs_name)
    chunk_api.add_param(selection)

    primpath.add_callback(_refresh_menus, notice_alias=chunk.ChunkNoticeAlias.VALUE_CHANGED, send_on_create=False)
    vs_name.add_callback(_refresh_menus, notice_alias=chunk.ChunkNoticeAlias.VALUE_CHANGED, send_on_create=False)
    chunk_api.add_callback(_chunk_loaded, notice_alias=chunk.ChunkNoticeAlias.CHUNK_LOADED)
    chunk_api.add_callback(
        _refresh_menus,
        notice_alias=chunk.ChunkNoticeAlias.STAGE_CONTENTS_CHANGED,
        listen_globally=True,
    )

    return chunk_api


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.EXAMPLES,
        description="""
        Set Variant chunk — authors an active variant selection on one or more prims.

        Separate from Add Variants (which defines which variant sets and names
        exist): this chunk controls which variant is currently selected, authoring
        an OverridePrim opinion with the chosen selection. Picking (none) removes
        the selection opinion entirely.

        Variant Set and Variant Selection are dropdowns read off the first prim
        Prim Path resolves to, and follow the stage: a set added by an upstream
        chunk shows up as soon as it cooks. A value the prim does not currently
        offer stays listed rather than being dropped, so a saved selection survives
        loading ahead of the chunk that defines it.

        Prim Path accepts comma-separated paths, Houdini-style glob patterns
        (*, **, ?), or a %name collection reference — the selection is applied
        to every matched prim.

        Designed to be the final node in a variant network so the selection can
        be lofted or overridden independently of the variant set definitions.
        """,
    )
