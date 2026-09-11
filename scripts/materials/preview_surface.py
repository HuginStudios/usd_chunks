from pxr import Sdf, Gf, UsdShade
from usd_chunks import chunk, chunk_utils


def execute(node):
    stage = node.get_stage()
    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    mat_path = node.get_param("mat_path").value
    if not mat_path:
        return

    diffuse   = node.get_param("diffuse_color").value
    roughness = float(node.get_param("roughness").value)
    metallic  = float(node.get_param("metallic").value)
    opacity   = float(node.get_param("opacity").value)
    ior       = float(node.get_param("ior").value)

    _prim_path = getattr(node, '_exec_prim_path', None) or getattr(node, 'prim_path', None)
    chunk_prim = stage.GetPrimAtPath(_prim_path) if _prim_path else None

    material = UsdShade.Material.Define(stage, mat_path)
    shader = UsdShade.Shader.Define(stage, f"{mat_path}/surface")
    shader.CreateIdAttr("UsdPreviewSurface")

    diffuse_input = shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f)
    diffuse_input.Set(Gf.Vec3f(*diffuse))
    node.get_param("diffuse_color").connect_usd_attr(diffuse_input.GetAttr())
    chunk_utils.wire_shader_input(stage, chunk_prim, "diffuse_color", diffuse_input.GetAttr())

    for param_name, shader_name_str, sdf_type, value in [
        ("roughness", "roughness", Sdf.ValueTypeNames.Float, roughness),
        ("metallic",  "metallic",  Sdf.ValueTypeNames.Float, metallic),
        ("opacity",   "opacity",   Sdf.ValueTypeNames.Float, opacity),
        ("ior",       "ior",       Sdf.ValueTypeNames.Float, ior),
    ]:
        inp = shader.CreateInput(shader_name_str, sdf_type)
        inp.Set(value)
        node.get_param(param_name).connect_usd_attr(inp.GetAttr())

    shader.CreateOutput("surface", Sdf.ValueTypeNames.Token)
    material.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")

    primpath_param = node.get_param("primpath_location")
    if primpath_param.value and primpath_param.value != "/":
        for target_path in chunk_utils.iter_prim_targets(stage, primpath_param):
            prim = stage.GetPrimAtPath(target_path)
            if prim and prim.IsValid():
                UsdShade.MaterialBindingAPI.Apply(prim).Bind(material)


def main():
    chunk_api = chunk.Chunk("Preview Surface")

    mat_path = chunk.LineEdit(
        "mat_path",
        "Material Path",
        default_value="/materials/surface",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
    )
    primpath_location = chunk.LineEdit(
        "primpath_location",
        "Bind To",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
    )
    diffuse_color = chunk.ColorPicker(
        "diffuse_color",
        "Diffuse Color",
        default_value=(0.18, 0.18, 0.18),
    )
    roughness = chunk.Slider(
        "roughness", "Roughness",
        min_value=0.0, max_value=1.0,
        default_value=0.5,
    )
    metallic = chunk.Slider(
        "metallic", "Metallic",
        min_value=0.0, max_value=1.0,
        default_value=0.0,
    )
    opacity = chunk.Slider(
        "opacity", "Opacity",
        min_value=0.0, max_value=1.0,
        default_value=1.0,
    )
    ior = chunk.Slider(
        "ior", "IOR",
        min_value=1.0, max_value=3.0,
        default_value=1.5,
    )

    chunk_api.add_params([mat_path, primpath_location, diffuse_color, roughness, metallic, opacity, ior])
    return chunk_api


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.MATERIALS,
        description="""
        Preview Surface chunk — defines a UsdPreviewSurface material and binds it
        to one or more prims on the active stage.

        The material is created at mat_path with a /surface child shader. Bind To
        accepts space- or comma-separated prim paths and glob patterns:
          /geo/mesh*   direct children of /geo matching "mesh*"
          /geo/**      every prim recursively under /geo
          /**          every prim on the stage
        Each resolved prim gets a material binding via UsdShade.MaterialBindingAPI.

        mat_path is intentionally exposed so downstream UV Texture and Primvar
        Reader chunks can reference it with the expression @PreviewSurface.mat_path@.
        """,
    )
