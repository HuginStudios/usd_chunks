"""Sdf layer surgery, free of any usd_chunks import.

A leaf module on purpose: chunk_notice runs this after every cook, and chunk_utils reaches
usd_chunks.chunk_registry, so keeping these functions there forced an import inside a
function to dodge the cycle.
"""

from __future__ import annotations

from pxr import Sdf


def define_ancestors(layer: Sdf.Layer) -> int:
    """Make every ancestor of a prim this layer defines a `def` in this layer too.

    UsdStage.DefinePrim writes `def` only for ancestors not yet defined anywhere, so the
    first chunk to cook owns `def "World"` and every later one holds a mere `over`. Mute the
    first -- a Stage Expression Filter or Switch gating it off -- and /World is defined by
    nobody: the camera and every light under it silently leave the scene. A chunk's layer
    has to stand on its own. Only ancestors of a `def` are touched, so an `over` that just
    overrides someone else's prim stays one.

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
    todo = {s.path: s for s in changed if s.specifier == Sdf.SpecifierOver}
    if todo:
        with Sdf.ChangeBlock():
            for spec in todo.values():
                spec.specifier = Sdf.SpecifierDef
    return len(todo)
