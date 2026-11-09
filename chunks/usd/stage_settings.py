"""Stage Settings chunk -- units, up axis, frame range and default prim on the layer being edited.

Requires: pxr (OpenUSD), usd_chunks.
"""

from __future__ import annotations

from pxr import Sdf, UsdGeom

from usd_chunks import chunk, chunk_utils

_UP_AXES = ["Y", "Z"]

# The layer metadata this chunk writes.
_FIELDS = (
    UsdGeom.Tokens.metersPerUnit, UsdGeom.Tokens.upAxis,
    "startTimeCode", "endTimeCode", "timeCodesPerSecond", "framesPerSecond", "defaultPrim",
)
#: On the chunk: (layer, what _FIELDS held there before this chunk wrote them), None for a
#: field that was not set. removed() puts it back.
_BEFORE = "_stage_settings_before"


def execute(node) -> None:
    """Write the settings onto the edit layer: the Edit Target's layer, or the session layer.

    Not the chunk's own layer. USD reads these only from a stage's root and session layers,
    and a baked export copies a chunk layer's prims, not its layer metadata -- so they go
    on the real layer, where an export carries them and an opened file applies them.
    """
    layer: Sdf.Layer = chunk_utils.get_edit_layer(node.get_scene_stage(), node)
    if layer is None:
        return
    _remember(node, layer)
    is_f, is_i, is_s = chunk.is_param_t(float), chunk.is_param_t(int), chunk.is_param_t(str)
    root = layer.pseudoRoot
    root.SetInfo(UsdGeom.Tokens.metersPerUnit, node.get_param("meters_per_unit", is_f).value)
    root.SetInfo(UsdGeom.Tokens.upAxis, node.get_param("up_axis", is_s).value)
    layer.startTimeCode = node.get_param("start_frame", is_i).value
    layer.endTimeCode = node.get_param("end_frame", is_i).value
    fps = node.get_param("fps", is_f).value
    layer.timeCodesPerSecond = fps
    layer.framesPerSecond = fps
    # Empty leaves the layer's own default prim (or none) as it was before this chunk.
    name = node.get_param("default_prim", is_s).value.strip().lstrip("/")
    before = getattr(node, _BEFORE)[1].get("defaultPrim")
    if name:
        root.SetInfo("defaultPrim", name)
    elif before is None:
        root.ClearInfo("defaultPrim")
    else:
        root.SetInfo("defaultPrim", before)


def removed(node) -> None:
    """Put back what the layer held before this chunk wrote to it (chunk_stage.REMOVED_HOOK).

    It writes outside its own layer, so deleting the chunk would leave its settings behind.
    """
    held = getattr(node, _BEFORE, None)
    setattr(node, _BEFORE, None)
    if held:
        _restore(*held)


def _remember(node, layer: Sdf.Layer) -> None:
    """Note what layer holds before the first write to it; give back a layer left behind."""
    held = getattr(node, _BEFORE, None)
    if held and held[0] == layer:
        return
    if held:
        _restore(*held)  # the Edit Target moved: the old layer gets its own values back
    root = layer.pseudoRoot
    setattr(node, _BEFORE, (layer, {k: root.GetInfo(k) if root.HasInfo(k) else None for k in _FIELDS}))


def _restore(layer: Sdf.Layer, before: dict) -> None:
    """Set each field in before back on layer, clearing the ones that were not set."""
    if layer.expired:
        return
    root = layer.pseudoRoot
    for key, value in before.items():
        if value is None:
            root.ClearInfo(key)
        else:
            root.SetInfo(key, value)


def main():
    chunk_api = chunk.Chunk("Stage Settings")
    chunk_api.add_params([
        chunk.Spinbox(
            "meters_per_unit",
            "Meters Per Unit",
            default_value=1.0,
            tooltip="How long one scene unit is, in metres: 1 for metres, 0.01 for "
            "centimetres. Solvers read it to scale gravity.",
        ),
        chunk.Menu("up_axis", "Up Axis", menu_items=_UP_AXES, default_value="Y"),
        chunk.Spinbox("start_frame", "Start Frame", default_value=1, is_int=True),
        chunk.Spinbox("end_frame", "End Frame", default_value=120, is_int=True),
        chunk.Spinbox("fps", "FPS", default_value=24.0, tooltip="Frames, and time codes, per second."),
        chunk.LineEdit(
            "default_prim",
            "Default Prim",
            default_value="",
            tooltip="The root prim a Reference or Payload to this file brings in, e.g. World. "
            "Empty leaves the layer's own as it is.",
        ),
    ])
    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD_LAYER,
        icon="sliders",
        description="""
        Sets the stage's units, up axis, frame range, frame rate and default prim.

        Stage Settings chunk -- writes a stage's units, up axis, frame range, frame rate
        and default prim onto the layer being edited.

        Default Prim [default_prim] names the root prim ("World") that a Reference or
        Payload to the file brings in; empty leaves the layer's own as it is.

        With an Edit Target upstream, they go on its layer, so an Export USD of that layer
        carries them: a shot file opened later starts in the right units and frame range.
        Without one, they go on the session layer and apply to the open stage right away.

        USD reads these from a stage's root and session layers only. On a layer that is
        sublayered into another, they do nothing until that layer is opened on its own.
        """,
    )
