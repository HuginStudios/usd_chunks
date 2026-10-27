from __future__ import annotations
from usd_chunks import chunk, chunk_utils

#: The Variant Selection entry that clears the selection instead of setting one.
NONE = "(none)"

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
    for target in chunk_utils.target_paths(param):
        prim = stage.GetPrimAtPath(target)
        if prim and prim.IsValid():
            return prim
    return None


def menu_items(node, param_name):
    """Variant Set's items, the sets on the target prim; Variant Selection's, the chosen set's
    variants and (none). Asked for as each menu opens (chunk.Menu.refresh_items).

    Not kept current by listening to the stage. The sets a prim carries usually come from a
    chunk upstream, and appear as the graph cooks -- a global listener rebuilt both menus on
    every stage change, for every Set Variant, whether anyone looked or not. Read on opening
    instead, three things the listener had to work for come free: a saved value is in long
    before anyone opens a menu (a list built before the restore coerced it to the first
    item), a node blocked until it is wired fills like any other, and a value the prim does
    not offer yet stays listed (Menu.set_items) until the stage catches up with it.
    """
    if param_name not in ("variant_set", "variant_selection"):
        return None
    prim = _first_target_prim(node)
    vsets = prim.GetVariantSets() if prim is not None else None
    names = vsets.GetNames() if vsets else []
    if param_name == "variant_set":
        return names
    chosen = node.get_param("variant_set").value or ""
    variants = list(vsets.GetVariantSet(chosen).GetVariantNames()) if chosen in names else []
    return [NONE] + variants


def execute(node):
    stage = node.get_stage()

    primpath_param = node.get_param("primpath_location")
    vs_name   = node.get_param("variant_set").value or ""
    selection = node.get_param("variant_selection").value or ""
    if selection == NONE:
        selection = ""

    if not vs_name or not primpath_param.value:
        return

    for target_path in chunk_utils.author_in_variant_contexts(stage, primpath_param):
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
    # Menus, filled from the prim as they open (menu_items): the sets it carries, then the
    # variants of the chosen set. An empty menu accepts any value (Menu.convert_attr only checks a value against a
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

    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD,
        icon="merge",
        description="""
        Set Variant chunk — authors an active variant selection on one or more prims.

        Separate from Add Variants (which defines which variant sets and names
        exist): this chunk controls which variant is currently selected, authoring
        an OverridePrim opinion with the chosen selection. Picking (none) removes
        the selection opinion entirely.

        Variant Set and Variant Selection are dropdowns read off the first prim
        Prim Path resolves to, as they open: a set added by an upstream chunk is
        there the next time you open them. A value the prim does not currently
        offer stays listed rather than being dropped, so a saved selection survives
        loading ahead of the chunk that defines it.

        Prim Path accepts comma-separated paths, Houdini-style glob patterns
        (*, **, ?), or a %name collection reference — the selection is applied
        to every matched prim.

        Designed to be the final node in a variant network so the selection can
        be lofted or overridden independently of the variant set definitions.
        """,
    )
