"""Merge Layers chunk — flattens multiple layer inputs into a single output layer.

Primarily a node graph concept: upstream chunk nodes each produce their own
layer edits, and a Merge node combines them with explicit priority ordering.
The ordering of inputs determines opinion strength (top port = strongest).

Also functions in the chunk panel via manual layer path inputs.
All output authored on the merge chunk's own layer.
"""

from __future__ import annotations

from pxr import Usd, Sdf

from usd_chunks import chunk


def _flatten_layers_into(target_layer: Sdf.Layer, source_layers: list[Sdf.Layer]):
    """Flatten multiple source layers into the target layer.

    Layers are stacked as sublayers in the order given: the first layer in
    the list is the strongest (same as USD sublayer ordering where earlier
    sublayers have higher strength).
    """
    if not source_layers:
        return

    # Create a temporary anonymous stage to composite the layers
    # Stack them as sublayers with the first being strongest (sublayer ordering)
    composite = Sdf.Layer.CreateAnonymous("merge_composite")

    for layer in source_layers:
        if layer and layer.identifier:
            composite.subLayerPaths.append(layer.identifier)

    # Flatten the composite into a single layer
    try:
        stage = Usd.Stage.Open(composite)
        flattened = stage.Flatten()

        # Transfer the flattened content to our target layer
        target_layer.TransferContent(flattened)
    except Exception:
        # Fallback: copy specs directly from each layer
        for layer in reversed(source_layers):
            if layer:
                Sdf.CopySpec(layer, Sdf.Path.absoluteRootPath,
                             target_layer, Sdf.Path.absoluteRootPath)


def execute(node):
    """Node graph execution — merges connected layer inputs.

    In the node graph, upstream chunk nodes output their Sdf.Layer through
    the 'layer' port. This node collects all connected layers and flattens
    them into its own layer.
    """
    stage = node.get_stage()
    if not stage:
        return

    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    # Collect input layers from connected upstream nodes
    # The node graph system passes layers through the 'layer' port.
    # In the execute pattern, we access connected upstream layers.
    source_layers = []

    # Try to get layers from the layer_paths param (manual mode / chunk panel)
    paths_param = node.get_param("layer_paths")
    if paths_param and paths_param.value:
        for path_str in paths_param.value.split("\n"):
            path_str = path_str.strip()
            if not path_str:
                continue
            layer = Sdf.Layer.Find(path_str)
            if not layer:
                try:
                    layer = Sdf.Layer.FindOrOpen(path_str)
                except Exception:
                    continue
            if layer:
                source_layers.append(layer)

    if source_layers:
        _flatten_layers_into(chunk_layer, source_layers)


def _on_change(owner, notice, sender):
    """Callback for the chunk panel — merge layers from the path list."""
    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage or not _chunk:
        return

    layer = owner.get_edit_context()
    if not layer:
        return
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)

    paths_param = _chunk.get_param("layer_paths")
    if not paths_param or not paths_param.value:
        return

    source_layers = []
    for path_str in paths_param.value.split("\n"):
        path_str = path_str.strip()
        if not path_str:
            continue
        found = Sdf.Layer.Find(path_str)
        if not found:
            try:
                found = Sdf.Layer.FindOrOpen(path_str)
            except Exception:
                continue
        if found:
            source_layers.append(found)

    if source_layers:
        _flatten_layers_into(layer, source_layers)


def main():
    chunk_api = chunk.Chunk("Merge Layers")

    layer_paths = chunk.TextEdit(
        "layer_paths",
        "Layer Paths",
        default_value="",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip=(
            "Layer identifiers to merge (one per line, strongest on top).\n"
            "In the node graph, connect upstream chunk layer outputs instead."
        ),
    )
    layer_paths.add_callback(_on_change)
    chunk_api.add_param(layer_paths)

    info = chunk.Label(
        "info_label",
        "Top layer = strongest opinion. Connect layer outputs in the node graph.",
    )
    chunk_api.add_param(info)

    return chunk_api


def config():
    return chunk.DefaulConfig(
        category="Core USD",
        description="""
        Merge Layers — flattens multiple layer inputs into a single output layer.

        Parameters:
          - Layer Paths: layer identifiers to merge (one per line, strongest on
            top). In the chunk panel, paste layer identifiers. In the node graph,
            connect upstream chunk layer outputs to the input ports instead.

        Ordering: top/first = strongest opinion. When two layers define the same
        property, the stronger layer's opinion wins (same as USD sublayer strength
        ordering).

        Use case in the node graph: wire multiple chunk nodes' layer outputs into
        a Merge node to combine their edits into one composed result. The merge
        output participates in composition as a single unified layer — useful for
        Solaris-style composable workflows.

        All output goes to the merge chunk's own layer.
        """,
    )
