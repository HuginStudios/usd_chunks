from __future__ import annotations
from pxr import Sdf, Gf, Usd, UsdShade
from usd_chunks import chunk, chunk_utils


def execute(node):
    stage = node.get_stage()
    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    primpath_location = node.get_param("primpath_location").value
    rotation    = float(node.get_param("rotation").value)
    scale       = node.get_param("scale").value
    translation = node.get_param("translation").value

    if not primpath_location:
        return

    _prim_path = getattr(node, '_exec_prim_path', None) or getattr(node, 'prim_path', None)
    chunk_prim = stage.GetPrimAtPath(_prim_path) if _prim_path else None

    xform = UsdShade.Shader.Define(stage, primpath_location)
    xform.CreateIdAttr("UsdTransform2d")

    rotation_in = xform.CreateInput("rotation", Sdf.ValueTypeNames.Float)
    rotation_in.Set(rotation)
    node.get_param("rotation").connect_usd_attr(rotation_in.GetAttr())

    scale_in = xform.CreateInput("scale", Sdf.ValueTypeNames.Float2)
    scale_in.Set(Gf.Vec2f(*scale))
    node.get_param("scale").connect_usd_attr(scale_in.GetAttr())

    translation_in = xform.CreateInput("translation", Sdf.ValueTypeNames.Float2)
    translation_in.Set(Gf.Vec2f(*translation))
    node.get_param("translation").connect_usd_attr(translation_in.GetAttr())

    uv_in = xform.CreateInput("in", Sdf.ValueTypeNames.Float2)
    chunk_utils.wire_shader_input(stage, chunk_prim, "in", uv_in.GetAttr(), clear_on_miss=True)

    xform.CreateOutput("result", Sdf.ValueTypeNames.Float2)

    if chunk_prim and chunk_prim.IsValid():
        with Usd.EditContext(stage, stage.GetSessionLayer()):
            attr = chunk_prim.GetAttribute("outputs:result")
            if attr and attr.IsValid():
                attr.SetConnections([Sdf.Path(f"{primpath_location}.outputs:result")])


def main():
    chunk_api = chunk.Chunk("UV Transform")

    primpath_location = chunk.LineEdit(
        "primpath_location",
        "Prim Path",
        default_value="/materials/transform2d",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
    )
    uv_in = chunk.Vector(
        "in", "In",
        param=chunk.Slider(min_value=0.0, max_value=1.0),
        length=2,
        suffixes="st",
        default_value=(0.0, 0.0),
    )
    rotation = chunk.Slider(
        "rotation", "Rotation",
        min_value=0.0, max_value=360.0,
        default_value=0.0,
    )
    scale = chunk.Vector(
        "scale", "Scale",
        param=chunk.Slider(min_value=0.0, max_value=4.0, default_value=1.0),
        length=2,
        suffixes="st",
        default_value=(1.0, 1.0),
    )
    translation = chunk.Vector(
        "translation", "Translation",
        param=chunk.Slider(min_value=-1.0, max_value=1.0, default_value=0.0),
        length=2,
        suffixes="st",
        default_value=(0.0, 0.0),
    )

    chunk_api.add_params([primpath_location, uv_in, rotation, scale, translation])
    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.MATERIALS,
        outputs=[("result", "Float2")],
        description="""
        UV Transform chunk — inserts a UsdTransform2d shader to offset, scale and rotate
        UV coordinates before they reach a texture.

        Connect an upstream primvar reader or another transform to the 'in' port.
        Connect the 'result' output to a UV Texture chunk's 'st' input.
        """,
    )
