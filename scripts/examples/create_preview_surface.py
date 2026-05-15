"""Create PreviewSurface chunk — creates a UsdPreviewSurface PBR material."""

from __future__ import annotations

from pxr import Gf, Sdf, Usd, UsdShade

from usd_chunks import chunk


def execute(owner, notice, sender):
    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage:
        return

    mat_path = (_chunk.get_param("mat_path").value or "").strip() or "/Materials/MyMaterial"
    r = float(_chunk.get_param("diffuse_r").value or 0.8)
    g = float(_chunk.get_param("diffuse_g").value or 0.8)
    b = float(_chunk.get_param("diffuse_b").value or 0.8)
    rough = float(_chunk.get_param("roughness").value or 0.5)
    metal = float(_chunk.get_param("metallic").value or 0.0)
    tex_file = (_chunk.get_param("diffuse_texture").value or "").strip()

    layer = owner.get_edit_context()
    if not layer:
        return
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)

    with Usd.EditContext(stage, layer):
        mat_prim = stage.DefinePrim(mat_path, "Material")
        mat = UsdShade.Material(mat_prim)
        shader = UsdShade.Shader.Define(stage, f"{mat_path}/PBRShader")
        shader.CreateIdAttr("UsdPreviewSurface")
        shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(rough)
        shader.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(metal)

        if tex_file:
            tex_reader = UsdShade.Shader.Define(stage, f"{mat_path}/DiffuseTexture")
            tex_reader.CreateIdAttr("UsdUVTexture")
            tex_reader.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath(tex_file))
            tex_reader.CreateInput("wrapS", Sdf.ValueTypeNames.Token).Set("repeat")
            tex_reader.CreateInput("wrapT", Sdf.ValueTypeNames.Token).Set("repeat")
            tex_reader.CreateOutput("rgb", Sdf.ValueTypeNames.Float3)
            uv_reader = UsdShade.Shader.Define(stage, f"{mat_path}/UVReader")
            uv_reader.CreateIdAttr("UsdPrimvarReader_float2")
            uv_reader.CreateInput("varname", Sdf.ValueTypeNames.Token).Set("st")
            uv_reader.CreateOutput("result", Sdf.ValueTypeNames.Float2)
            tex_reader.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(
                uv_reader.ConnectableAPI(), "result"
            )
            shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(
                tex_reader.ConnectableAPI(), "rgb"
            )
        else:
            shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(
                Gf.Vec3f(r, g, b)
            )

        mat.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")


def main():
    chunk_api = chunk.Chunk("Create PreviewSurface")

    chunk_api.add_param(
        chunk.LineEdit("mat_path", "Material Path", default_value="/Materials/MyMaterial")
    )

    color_grp = chunk.Group("Diffuse Color")
    color_grp.add_param(chunk.Spinbox("diffuse_r", "R", default_value=0.8))
    color_grp.add_param(chunk.Spinbox("diffuse_g", "G", default_value=0.8))
    color_grp.add_param(chunk.Spinbox("diffuse_b", "B", default_value=0.8))
    chunk_api.add_param(color_grp)

    mat_grp = chunk.Group("Material")
    mat_grp.add_param(chunk.Spinbox("roughness", "Roughness", default_value=0.5))
    mat_grp.add_param(chunk.Spinbox("metallic", "Metallic", default_value=0.0))
    chunk_api.add_param(mat_grp)

    chunk_api.add_param(
        chunk.FileBrowser(
            "diffuse_texture",
            "Diffuse Texture",
            extensions=["png", "jpg", "jpeg", "exr", "tif", "tiff"],
        )
    )

    for name in ("mat_path", "diffuse_r", "diffuse_g", "diffuse_b", "roughness", "metallic", "diffuse_texture"):
        chunk_api.get_param(name).add_callback(execute)

    return chunk_api


def config():
    return chunk.DefaulConfig(category="Material")
