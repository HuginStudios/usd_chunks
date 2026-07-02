"""Set UV Transform chunk — inserts or updates a UsdTransform2d between UV reader and texture."""

from __future__ import annotations

from pxr import Gf, Sdf, UsdShade

from usd_chunks import chunk


def _run(owner, notice, sender):
    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage:
        return

    shader_path = (_chunk.get_param("shader_path").value or "").strip()
    if not shader_path:
        return
    prim = stage.GetPrimAtPath(shader_path)
    if not prim or not prim.IsValid():
        return

    ss = float(_chunk.get_param("scale_s").value or 1.0)
    st = float(_chunk.get_param("scale_t").value or 1.0)
    os_ = float(_chunk.get_param("offset_s").value or 0.0)
    ot = float(_chunk.get_param("offset_t").value or 0.0)
    rot = float(_chunk.get_param("rotation").value or 0.0)

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

    chunk_api.add_param(chunk.LineEdit("shader_path", "Shader Prim Path"))

    scale_grp = chunk.Group("Scale")
    scale_grp.add_param(chunk.Spinbox("scale_s", "Scale S", default_value=1.0))
    scale_grp.add_param(chunk.Spinbox("scale_t", "Scale T", default_value=1.0))
    chunk_api.add_param(scale_grp)

    offset_grp = chunk.Group("Offset")
    offset_grp.add_param(chunk.Spinbox("offset_s", "Offset S", default_value=0.0))
    offset_grp.add_param(chunk.Spinbox("offset_t", "Offset T", default_value=0.0))
    chunk_api.add_param(offset_grp)

    chunk_api.add_param(chunk.Spinbox("rotation", "Rotation (degrees)", default_value=0.0))

    for name in ("shader_path", "scale_s", "scale_t", "offset_s", "offset_t", "rotation"):
        chunk_api.get_param(name).add_callback(_run)

    return chunk_api


def config():
    return chunk.DefaulConfig(category="Material")
