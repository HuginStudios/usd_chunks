"""Authoring into a layer the stage reaches only through a reference or payload.

Imports nothing of usd_chunks, so the cook (chunk_cook, which imports nothing above
chunk_notice) can use it as well as everything above.
"""

from __future__ import annotations

from pxr import Sdf, Usd


#: (stage root identifier, layer identifier) -> path of the prim whose arc brings the layer in.
#: Checked against the prim's index before use, so a stale entry is only a slower lookup.
_arc_prims: dict[tuple[str, str], Sdf.Path] = {}


def _arc_node(prim: Usd.Prim, layer: Sdf.Layer):
    """The node of prim's index whose layer stack holds layer, or None."""
    index = prim.GetPrimIndex()
    if not index.IsValid():
        return None
    stack = list(index.rootNode.children)
    while stack:
        node = stack.pop(0)
        if layer in node.layerStack.layers:
            return node
        stack.extend(node.children)
    return None


def arc_edit_target(stage: Usd.Stage, layer: Sdf.Layer) -> Usd.EditTarget | None:
    """An edit target for a layer the stage reaches only through a reference or payload.

    A layer an asset brings in by arc -- Bottle.usd payloads Bottle_payload, which
    references Bottle.geom -- is not in the stage's local layer stack, so it cannot be
    edited directly. It can be edited through the arc: authoring at /Bottle/geo lands at
    whatever path the arc maps that to in the layer (and in any layer it sublayers, a
    chunk's cook layer among them). The first prim in traversal order whose arc holds the
    layer decides the mapping; a path outside that arc's namespace cannot be mapped.

    Args:
        stage: The stage.
        layer: The layer, reached through an arc.

    Returns:
        The mapped edit target, or None when no arc on the stage brings the layer in.
    """
    key = (stage.GetRootLayer().identifier, layer.identifier)
    if (path := _arc_prims.get(key)) is not None:
        prim = stage.GetPrimAtPath(path)
        if prim and (node := _arc_node(prim, layer)) is not None:
            return Usd.EditTarget(layer, node)
        del _arc_prims[key]
    for prim in stage.Traverse():
        if not (prim.HasAuthoredPayloads() or prim.HasAuthoredReferences()):
            continue
        if (node := _arc_node(prim, layer)) is not None:
            _arc_prims[key] = prim.GetPath()
            return Usd.EditTarget(layer, node)
    return None


def edit_target(stage: Usd.Stage, layer: Sdf.Layer | None):
    """What to hand Usd.EditContext to author into layer on stage.

    The layer itself when it is in the stage's local layer stack -- every layer, until a
    network makes one that an asset brings in by payload or reference. Such a layer, and a
    chunk's cook layer sublayered under it, is edited through the arc (arc_edit_target).
    Use this wherever a chunk's layer is authored into, rather than the layer as it is.

    Args:
        stage: The stage.
        layer: The layer to author into, or None.

    Returns:
        layer, or a mapped Usd.EditTarget for it; layer unchanged when no arc reaches it
        either, so authoring fails the way it always did.
    """
    if layer is None or stage is None or stage.HasLocalLayer(layer):
        return layer
    return arc_edit_target(stage, layer) or layer


def is_editable(stage: Usd.Stage, layer: Sdf.Layer) -> bool:
    """Whether layer can be authored into on stage: local, or reached through an arc."""
    return stage.HasLocalLayer(layer) or arc_edit_target(stage, layer) is not None

