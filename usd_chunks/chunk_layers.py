"""Sdf layer surgery, free of any usd_chunks import.

A leaf module on purpose: chunk_notice runs this after every cook, and chunk_utils reaches
usd_chunks.chunk_registry, so keeping these functions there forced an import inside a
function to dodge the cycle.
"""

from __future__ import annotations

import posixpath

from pxr import Sdf

#: What a cook sublayer's file name adds to the stem of the layer it belongs to:
#: layout.usda -> layout.cook.usda.
COOK_SUFFIX = ".cook"


def define_ancestors(layer: Sdf.Layer, defined_elsewhere=None) -> int:
    """Make every ancestor of a prim this layer defines a `def` in this layer too.

    UsdStage.DefinePrim writes `def` only for ancestors not yet defined anywhere, so the
    first chunk to cook owns `def "World"` and every later one holds a mere `over`. Mute the
    first -- a Switch or a Stage Expression Switch gating it off -- and /World is defined by
    nobody: the camera and every light under it silently leave the scene. A chunk's layer
    has to stand on its own. Only ancestors of a `def` are touched, so an `over` that just
    overrides someone else's prim stays one.

    `defined_elsewhere(path)` is the caller's veto: return True for an ancestor that is
    fine as it is and must keep the specifier USD gave it. The gate that needs this asks
    the composed stage whether the ancestor is still defined at all, so a scope another
    live chunk or a referenced asset defines is never claimed -- only the ones muting has
    just orphaned. Without the callback everything is promoted.

    Args:
        layer (Sdf.Layer): The layer to fix up -- the chunk's own
        defined_elsewhere (callable | None): path -> bool, True to leave it alone

    Returns:
        int: how many specs were changed
    """
    changed = []

    def walk(spec, overs):
        if spec.specifier == Sdf.SpecifierDef:
            changed.extend(overs)
            overs = []
        elif spec.specifier == Sdf.SpecifierOver:
            overs = overs + [spec]
        else:
            overs = []  # a class is its own root; nothing above it is being defined
        for child in spec.nameChildren:
            walk(child, overs)
        for vset in spec.variantSets:
            for variant in vset.variants:
                for child in variant.primSpec.nameChildren:
                    walk(child, overs)

    for root in layer.rootPrims:
        walk(root, [])
    todo = {
        s.path: s for s in changed
        if s.specifier == Sdf.SpecifierOver
        and not (defined_elsewhere is not None and defined_elsewhere(s.path))
    }
    if todo:
        with Sdf.ChangeBlock():
            for spec in todo.values():
                spec.specifier = Sdf.SpecifierDef
    return len(todo)


def broken_property_specs(layer: Sdf.Layer) -> list[Sdf.Path]:
    """Property paths this layer lists but holds no usable spec for.

    A spec whose type never got set is invisible to GetObjectAtPath while Traverse still
    walks it, and UsdUtils.FlattenLayerStack refuses the whole layer stack over one of them
    ("Unknown spec type SdfSpecTypeUnknown at ..."). Since flattening is how a scene is
    saved, one stub anywhere costs the user everything they have done since the last save.

    What creates one is not yet known -- see drop_broken_property_specs.

    Args:
        layer: The layer to scan.

    Returns:
        The stub property paths.
    """
    found: list[Sdf.Path] = []
    layer.Traverse(Sdf.Path("/"), lambda path: found.append(path))
    return [p for p in found if p.IsPropertyPath() and layer.GetObjectAtPath(p) is None]


def drop_broken_property_specs(layers, logger=None) -> list[str]:
    """Remove every unusable property stub from layers, and say what was removed.

    Deliberately loud rather than quiet: this is a rescue, not a repair. What creates these
    stubs is not yet known, so the logged layer and path are there to help find it.
    Dropping the stub is safe because it holds nothing: no value, no type, no connection.

    Args:
        layers: The layers to clean.
        logger: Where to report each removal; None reports nothing.

    Returns:
        "<layer>: <path>" for each stub removed.
    """
    removed = []
    for layer in layers:
        for path in broken_property_specs(layer):
            prim_spec = layer.GetPrimAtPath(path.GetPrimPath())
            if prim_spec is not None and path.name in prim_spec.properties:
                del prim_spec.properties[path.name]
            removed.append(f"{layer.GetDisplayName() or layer.identifier}: {path}")
    if removed and logger is not None:
        logger.warning(
            "Dropped %d unusable property spec(s) before flattening -- please report these, "
            "they are a bug we have not reproduced yet: %s",
            len(removed), "; ".join(removed),
        )
    return removed


def cook_sublayer_name(home: Sdf.Layer) -> str:
    """The file name of home's cook sublayer: layout.usda -> layout.cook.usda.

    Args:
        home: A file-backed layer that defines chunks.

    Returns:
        The bare file name, to be found beside home.
    """
    stem, ext = posixpath.splitext(posixpath.basename((home.realPath or home.identifier).replace("\\", "/")))
    return f"{stem}{COOK_SUFFIX}{ext}"


def find_cook_sublayer(home: Sdf.Layer) -> Sdf.Layer | None:
    """home's cook sublayer, when home lists one: the layer its chunks' cooks land in.

    The link is part of what a layer publishes -- written while its owner can still edit
    it -- so a locked file still has its procedurals cook into the right place: the cook
    fills the cook layer in memory and never needs to write either file. Listed but not on
    disk yet (a publish that never baked) opens nothing and is treated as not listed.

    Args:
        home: A file-backed layer that defines chunks.

    Returns:
        The cook layer, or None when home lists none or it does not open.
    """
    name = cook_sublayer_name(home)
    for path in home.subLayerPaths:
        if posixpath.basename(path.replace("\\", "/")) == name:
            return Sdf.Layer.FindOrOpenRelativeToLayer(home, path)
    return None


def add_cook_sublayer(home: Sdf.Layer) -> Sdf.Layer:
    """Give home a cook sublayer: an empty X.cook file beside it, listed as home's strongest
    sublayer -- so home's own opinions beat its cook, and its cook beats everything weaker.

    An edit to home, so for its owner to make (a publish, or authoring chunks into it) --
    never done on open. Saving home is left to the caller.

    Args:
        home: A file-backed layer that defines chunks.

    Returns:
        The cook layer.
    """
    if (cook := find_cook_sublayer(home)) is not None:
        return cook
    name = cook_sublayer_name(home)
    path = posixpath.join(posixpath.dirname(home.realPath.replace("\\", "/")), name)
    cook = Sdf.Layer.FindOrOpen(path) or Sdf.Layer.CreateNew(path)
    cook.customLayerData = {**cook.customLayerData, "usdchunks:derived": True}
    cook.Save()
    home.subLayerPaths.insert(0, f"./{name}")
    return cook
