"""Set UV Transform chunk — inserts or updates a UsdTransform2d between UV reader and texture.

Requires: pxr (OpenUSD), usd_chunks.
"""

from __future__ import annotations

from pxr import Gf, Sdf, UsdShade

from usd_chunks import chunk


def _run(owner, notice, sender):
    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage:
        return

    shader_path = _chunk.get_param("shader_path", chunk.is_param_t(str)).value.strip()
    if not shader_path:
        return
    prim = stage.GetPrimAtPath(shader_path)
    if not prim or not prim.IsValid():
        return

    is_f = chunk.is_param_t(float)
    ss = _chunk.get_param("scale_s", is_f).value
    st = _chunk.get_param("scale_t", is_f).value
    os_ = _chunk.get_param("offset_s", is_f).value
    ot = _chunk.get_param("offset_t", is_f).value
    rot = _chunk.get_param("rotation", is_f).value

    layer = owner.get_edit_context()
    if not layer:
        return
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)

    # Callbacks already run inside the chunk's edit context (NoticeConnection).
    shader = UsdShade.Shader(prim)
    st_inp = shader.GetInput("st")
    if not st_inp:
        return

    src = st_inp.GetConnectedSources()
    uv_source_shader = None
    uv_source_output = "result"
    if src and src[0]:
        uv_source_shader = src[0][0].source
        uv_source_output = src[0][0].sourceName

    mat_path = prim.GetParent().GetPath()
    xform_path = f"{mat_path}/UVTransform"
    xform_shader = UsdShade.Shader.Define(stage, xform_path)
    xform_shader.CreateIdAttr("UsdTransform2d")
    xform_shader.CreateInput("scale", Sdf.ValueTypeNames.Float2).Set(Gf.Vec2f(ss, st))
    xform_shader.CreateInput("translation", Sdf.ValueTypeNames.Float2).Set(Gf.Vec2f(os_, ot))
    xform_shader.CreateInput("rotation", Sdf.ValueTypeNames.Float).Set(rot)
    xform_shader.CreateOutput("result", Sdf.ValueTypeNames.Float2)

    if uv_source_shader:
        xform_shader.CreateInput("in", Sdf.ValueTypeNames.Float2).ConnectToSource(uv_source_shader, uv_source_output)
    st_inp.ConnectToSource(xform_shader.ConnectableAPI(), "result")


def main():
    chunk_api = chunk.Chunk("Set UV Transform")

    chunk_api.add_param(
        chunk.LineEdit(
            "shader_path",
            "Shader Prim Path",
            tooltip="Path to the texture shader whose 'st' (UV) input should be transformed.",
        )
    )

    scale_grp = chunk.Group("Scale")
    scale_grp.add_param(
        chunk.Spinbox("scale_s", "Scale S", default_value=1.0, tooltip="UV tiling along S: >1 repeats, <1 zooms in.")
    )
    scale_grp.add_param(
        chunk.Spinbox("scale_t", "Scale T", default_value=1.0, tooltip="UV tiling along T: >1 repeats, <1 zooms in.")
    )
    chunk_api.add_param(scale_grp)

    offset_grp = chunk.Group("Offset")
    offset_grp.add_param(chunk.Spinbox("offset_s", "Offset S", default_value=0.0, tooltip="Pan the UVs along S."))
    offset_grp.add_param(chunk.Spinbox("offset_t", "Offset T", default_value=0.0, tooltip="Pan the UVs along T."))
    chunk_api.add_param(offset_grp)

    chunk_api.add_param(
        chunk.Spinbox("rotation", "Rotation (degrees)", default_value=0.0, tooltip="Rotate the UVs, in degrees.")
    )

    for name in ("shader_path", "scale_s", "scale_t", "offset_s", "offset_t", "rotation"):
        chunk_api.get_param(name).add_callback(_run)

    return chunk_api


def config():
    return chunk.DefaulConfig(
        category="Material",
        description=(
            "Inserts (or updates) a UsdTransform2d node between a texture's UV reader and "
            "the texture, letting you scale/offset/rotate UVs without touching the texture "
            "itself. Point Shader Prim Path at the texture shader whose 'st' input you "
            "want to transform."
        ),
    )
