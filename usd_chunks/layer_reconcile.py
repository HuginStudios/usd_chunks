"""Make one layer hold what another does, changing only the specs that differ.

A chunk's cook used to clear its layer and write it again (#277). To USD, a prim spec
removed and added back is a resync of that prim even when it comes back the same, and
Hydra answers a resync by building the prim again -- an instance's prototype too. A cook
that reconciles its result into the layer instead (diff cook, chunk_cook) leaves an
unchanged spec untouched, sets a changed value in place (an info-only change Hydra applies
without a rebuild), and resyncs only a prim that appears, disappears or moves.

reconcile(src, dst) is the whole of it: after it, dst exports as src does, but for the
order of properties, which composition does not use. It says which prims it changed, as
skip_cook.changed_paths would have found by comparing the two layers again.
"""

from __future__ import annotations

from pxr import Sdf

#: Info fields the bindings refuse to set directly; each has its own way in.
_LIST_EDITED = {
    "targetPaths": "targetPathList",
    "connectionPaths": "connectionPathList",
}
#: Sublayer offsets go with the sublayer paths, set together; the bindings cannot read them
#: as info.
_LAYER_SKIP = frozenset({"subLayers", "subLayerOffsets"})

_ROOT = Sdf.Path.absoluteRootPath


def reconcile(src: Sdf.Layer, dst: Sdf.Layer,
              sublayers: list[tuple[str, Sdf.LayerOffset]] | None = None) -> set[Sdf.Path]:
    """Change dst, in one Sdf.ChangeBlock, until it holds what src does.

    Args:
        src: The layer as it should be (a cook's scratch layer).
        dst: The layer to change (the chunk's layer on the stage).
        sublayers: (identifier, offset) pairs dst is to have instead of src's sublayers: a
            copy of the session layer with a layer of its own on top (skip_cook).

    Returns:
        The prims it changed, variant selections stripped, as skip_cook.changed_paths says
        it: a prim added, removed or moved is itself (what is under it goes with it), one
        whose own fields or properties changed is itself, and a change to the layer's own
        metadata or sublayers is the root -- everything.
    """
    changed: set[Sdf.Path] = set()
    with Sdf.ChangeBlock():
        if _layer_info(src, dst, sublayers):
            changed.add(_ROOT)
        _children(src, dst, src.pseudoRoot, dst.pseudoRoot, changed)
    return {path if path == _ROOT else path.StripAllVariantSelections() for path in changed}


def _layer_info(src: Sdf.Layer, dst: Sdf.Layer, sublayers=None) -> bool:
    if sublayers is None:
        paths, offsets = list(src.subLayerPaths), list(src.subLayerOffsets)
    else:
        paths, offsets = [path for path, _ in sublayers], [offset for _, offset in sublayers]
    moved = paths != list(dst.subLayerPaths) or offsets != list(dst.subLayerOffsets)
    if moved:
        dst.subLayerPaths = paths
        for i, offset in enumerate(offsets):
            dst.subLayerOffsets[i] = offset
    return _info(src.pseudoRoot, dst.pseudoRoot, skip=_LAYER_SKIP) or moved


def _info(s, d, skip: frozenset = frozenset()) -> bool:
    """Set on d each info field of s it lacks or holds differently; clear the ones s lacks.
    Whether it changed any."""
    changed = False
    s_keys = s.ListInfoKeys()
    d_keys = set(d.ListInfoKeys())
    for key in d_keys.difference(s_keys):
        if key in skip:
            continue
        changed = True
        if key == "timeSamples":
            _time_samples(s, d, {})
        elif key in _LIST_EDITED:
            getattr(d, _LIST_EDITED[key]).ClearEdits()
        else:
            d.ClearInfo(key)
    for key in s_keys:
        if key in skip:
            continue
        value = s.GetInfo(key)
        if key in d_keys and d.GetInfo(key) == value:
            continue
        changed = True
        if key == "timeSamples":
            _time_samples(s, d, value)
        elif key in _LIST_EDITED:
            _list_op(getattr(d, _LIST_EDITED[key]), value)
        else:
            d.SetInfo(key, value)
    return changed


def _time_samples(s, d, samples: dict) -> None:
    layer, path = d.layer, d.path
    old = d.GetInfo("timeSamples") if d.HasInfo("timeSamples") else {}
    for time in old.keys() - samples.keys():
        layer.EraseTimeSample(path, time)
    for time, value in samples.items():
        if time not in old or old[time] != value:
            layer.SetTimeSample(path, time, value)


def _list_op(editor, op) -> None:
    """Make a list editor hold op's edits."""
    if op.isExplicit:
        editor.ClearEditsAndMakeExplicit()
        editor.explicitItems = list(op.explicitItems)
        return
    editor.ClearEdits()
    for name in ("prependedItems", "appendedItems", "deletedItems", "addedItems", "orderedItems"):
        items = list(getattr(op, name))
        if items:
            setattr(editor, name, items)


def _copy(src: Sdf.Layer, dst: Sdf.Layer, path: Sdf.Path) -> None:
    if not Sdf.CopySpec(src, path, dst, path):
        raise RuntimeError(f"could not copy {path} into {dst.identifier}")


def _remove_child(parent, name: str, changed: set) -> None:
    changed.add(parent.path.AppendChild(name))
    del parent.nameChildren[name]


def _children(src: Sdf.Layer, dst: Sdf.Layer, s, d, changed: set) -> None:
    """Reconcile the prim children of s into d's, keeping s's order."""
    want = list(s.nameChildren.keys())
    have = list(d.nameChildren.keys())
    wanted = set(want)
    for name in have:
        if name not in wanted:
            _remove_child(d, name, changed)
    have = [name for name in have if name in wanted]
    # Sdf cannot move a child in place: from the first one out of order on, they are
    # removed and copied again, in order. A cook writes in the same order each time, so
    # this is a prim added in the middle, rarely more.
    kept = 0
    for name in have:
        if kept < len(want) and want[kept] == name:
            kept += 1
        else:
            break
    for name in have[kept:]:
        _remove_child(d, name, changed)
    for i, name in enumerate(want):
        child = s.nameChildren[name]
        if i >= kept:
            _copy(src, dst, child.path)
            changed.add(child.path)
        else:
            _prim(src, dst, child, d.nameChildren[name], changed)


def _prim(src: Sdf.Layer, dst: Sdf.Layer, s, d, changed: set) -> None:
    if not _same_variants(s, d):
        parent = d.nameParent if d.nameParent else dst.pseudoRoot
        # Replaced where it stands would put it last among its siblings; its parent's
        # _children compares order, so only a prim whose variants differ in shape is
        # copied whole -- and copied back into place by removing what follows it.
        names = list(parent.nameChildren.keys())
        after = names[names.index(d.name):]
        for name in after:
            _remove_child(parent, name, changed)
        s_parent = s.nameParent if s.nameParent else src.pseudoRoot
        for name in after:
            if name in s_parent.nameChildren:
                _copy(src, dst, s_parent.nameChildren[name].path)
        return
    if _info(s, d) | _properties(src, dst, s, d):
        changed.add(s.path)
    for set_name, vset in s.variantSets.items():
        d_set = d.variantSets[set_name]
        for v_name, variant in vset.variants.items():
            s_prim, d_prim = variant.primSpec, d_set.variants[v_name].primSpec
            if _info(s_prim, d_prim) | _properties(src, dst, s_prim, d_prim):
                changed.add(s_prim.path)
            _children(src, dst, s_prim, d_prim, changed)
    _children(src, dst, s, d, changed)


def _same_variants(s, d) -> bool:
    """Whether s and d have the same variant sets holding the same variants."""
    if not s.variantSets and not d.variantSets:
        return True
    if set(s.variantSets.keys()) != set(d.variantSets.keys()):
        return False
    return all(set(vset.variants.keys()) == set(d.variantSets[name].variants.keys())
               for name, vset in s.variantSets.items())


def _properties(src: Sdf.Layer, dst: Sdf.Layer, s, d) -> bool:
    """Reconcile the properties of s into d's. Whether it changed any."""
    changed = False
    # Each read once into a dict: every lookup through a children proxy builds a spec handle.
    s_props = {prop.name: prop for prop in s.properties}
    d_props = {prop.name: prop for prop in d.properties}
    for name, dp in d_props.items():
        if name not in s_props:
            d.RemoveProperty(dp)
            changed = True
    for name, sp in s_props.items():
        dp = d_props.get(name)
        if dp is None:
            _copy(src, dst, sp.path)
            changed = True
            continue
        if type(sp) is not type(dp):
            d.RemoveProperty(dp)
            _copy(src, dst, sp.path)
            changed = True
            continue
        changed |= _info(sp, dp)
    return changed
