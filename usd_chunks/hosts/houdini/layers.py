"""Layers in Houdini: a chunk's edits go into the active layer, and Layer starts a new one.

usd_chunks elsewhere has one stage: a Layer chunk adds a layer to it, and an Edit Target
chunk points every chunk after it at that layer. A Solaris network has no one stage -- each
node gets the stage its input made and adds to it -- but it has the same idea built in: the
active layer. Every node authors into it until a node starts a new one (Configure Layer's
Start New Layer). So here:

- every chunk node merges its chunk's layer into the active layer (merge), its edits
  winning over what is already there, instead of adding a sublayer of its own;
- a Layer chunk node starts a new active layer -- the chunk node type routes it through a
  Configure Layer that does, with its save path -- and names it with its Layer Name (tag);
- an Edit Target chunk pointing at the newest named layer has nothing to do: the chunks
  after it already author there. Pointing at an older layer is not supported yet, and
  says so (check_edit_target).

A named layer is what Export USD and Edit Target list and export here (user_layers); the
rest of the stack is Houdini's own bookkeeping.
"""
from __future__ import annotations

from pxr import Sdf, UsdUtils

#: The customLayerData key holding a layer's name, as a Layer chunk gave it.
LAYER_NAME_KEY = "usdchunks:layerName"
#: The prim Houdini keeps each of its layers' bookkeeping in.
HOUDINI_INFO = Sdf.Path("/HoudiniLayerInfo")
#: The chunk module that starts a new layer.
LAYER_MODULE = "layer"
#: The chunk module that picks the layer the chunks after it author into.
EDIT_TARGET_MODULE = "edit_target"
#: Edit Target menu items that mean the newest layer, as far as Houdini is concerned.
NEWEST = ("(Follow Upstream Layer)", "Session Layer")
#: A Layer chunk node's save path, for a USD ROP: its Layer Name, or the node's name, under
#: $HIP/usd. Evaluated on the Configure Layer inside the chunk node.
SAVE_PATH = '$HIP/usd/`ifs(strcmp(chs("../layer_name"), ""), chs("../layer_name"), opname(".."))`.usda'
#: The chunk node type's switch index: 1 (through Configure Layer) for a Layer chunk.
NEW_LAYER_SWITCH = f'strcmp(chs("../usdchunks_type"), "{LAYER_MODULE}") == 0'


def layer_name(layer: Sdf.Layer) -> str | None:
    """The name a Layer chunk gave layer, or None."""
    return layer.customLayerData.get(LAYER_NAME_KEY)


def tag(layer: Sdf.Layer, name: str) -> None:
    """Name layer, so Export USD and Edit Target find it."""
    data = dict(layer.customLayerData)
    data[LAYER_NAME_KEY] = name
    layer.customLayerData = data


def user_layers(stage) -> list[tuple[str, Sdf.Layer]]:
    """The layers a user picks or exports, strongest first: named ones and files on disk.

    Houdini's own layers (one per run of nodes, the session layer's) are anonymous and
    unnamed, and are left out.
    """
    out = []
    for layer in stage.GetLayerStack(includeSessionLayers=True):
        if (name := layer_name(layer)) is not None:
            out.append((name, layer))
        elif not layer.anonymous:
            out.append((layer.GetDisplayName() or layer.identifier, layer))
    return out


def without_houdini_info(layer: Sdf.Layer) -> Sdf.Layer:
    """layer, or a copy without Houdini's HoudiniLayerInfo prim, as a USD ROP saves it."""
    if not layer.GetPrimAtPath(HOUDINI_INFO):
        return layer
    copy = Sdf.Layer.CreateAnonymous()
    copy.TransferContent(layer)
    del copy.rootPrims[HOUDINI_INFO.name]
    return copy


def newest_layer_name(stage) -> str | None:
    """The name of the strongest named layer on stage: the one chunks are authoring into."""
    return next((name for layer in stage.GetLayerStack(includeSessionLayers=False)
                 if (name := layer_name(layer)) is not None), None)


def check_edit_target(selection: str, stage) -> str | None:
    """Why an Edit Target chunk's selection cannot be honoured here, or None if it can."""
    if selection in NEWEST or not selection:
        return None
    newest = newest_layer_name(stage) if stage is not None else None
    if selection == newest:
        return None
    if newest is None:
        return (f"No layer named '{selection}' comes before this node. Put a Layer chunk "
                f"named '{selection}' upstream.")
    return (f"'{selection}' is not the newest layer ('{newest}' is). In Houdini the chunks "
            f"after an Edit Target can only author into the newest layer.")


def merge(active: Sdf.Layer, delta: Sdf.Layer) -> None:
    """Merge a chunk's layer into the active layer, the chunk's opinions winning.

    As if delta were a sublayer above active: where both have a value, delta's is kept,
    and an attribute delta animates keeps delta's samples only, not a mix of the two.
    """
    merged = Sdf.Layer.CreateAnonymous()
    merged.TransferContent(delta)
    UsdUtils.StitchLayers(merged, active)

    def keep_samples(path):
        if not delta.ListTimeSamplesForPath(path):
            return
        for t in merged.ListTimeSamplesForPath(path):
            merged.EraseTimeSample(path, t)
        for t in delta.ListTimeSamplesForPath(path):
            merged.SetTimeSample(path, t, delta.QueryTimeSample(path, t))

    delta.Traverse(Sdf.Path.absoluteRootPath, keep_samples)
    sublayers = list(delta.subLayerPaths)
    sublayers += [p for p in active.subLayerPaths if p not in sublayers]
    data = dict(active.customLayerData)
    data.update(delta.customLayerData)

    active.TransferContent(merged)
    active.subLayerPaths = sublayers
    active.customLayerData = data
