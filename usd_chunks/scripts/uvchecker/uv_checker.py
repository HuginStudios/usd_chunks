import os
from usd_chunks.chunk import Chunk
from pxr import Sdf, Usd, UsdGeom, UsdShade

applied_materials = {}

def apply(api, x_scale, y_scale):

    x_scale = x_scale.value()
    y_scale = y_scale.value()
    selected_prim = api.prim
    if not selected_prim:
        print("Prim not selected.")
        return
   
    # Create an group to hold the UV checker matirial
    stage = api.stage
    
    base_material_grp = "/materials"
    material_path = f"{base_material_grp}/UVCheckerMaterial"
    surface_path = f"{material_path}/previewSurface"
    texture_path = f"{material_path}/texture"
    transform2d_path = f"{material_path}/transform2d"
    reader_path = f"{material_path}/reader"

    # Create the material, shaders and its id
    stage.DefinePrim(base_material_grp, "Scope")
    material = UsdShade.Material.Define(stage, material_path)
    
    preview = UsdShade.Shader.Define(stage, surface_path)
    stage.GetPrimAtPath(surface_path).GetAttribute('info:id').Set("UsdPreviewSurface")

    texture = UsdShade.Shader.Define(stage, texture_path)
    stage.GetPrimAtPath(texture_path).GetAttribute('info:id').Set("UsdUVTexture")

    transform2d = UsdShade.Shader.Define(stage, transform2d_path)
    stage.GetPrimAtPath(transform2d_path).GetAttribute('info:id').Set("UsdTransform2d")

    reader = UsdShade.Shader.Define(stage, reader_path)
    stage.GetPrimAtPath(reader_path).GetAttribute('info:id').Set("UsdPrimvarReader_float2")

    # Create the inputs and outputs for the shaders
    preview_output_surface = preview.CreateOutput("surface", Sdf.ValueTypeNames.Token)
    material.GetOutput("surface").ConnectToSource(preview_output_surface)

    preview_in_diff = preview.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f)
    texture_out_rgb = texture.CreateOutput("rgb", Sdf.ValueTypeNames.Color3f)
    preview_in_diff.ConnectToSource(texture_out_rgb)

    path_to_uv = os.path.sep.join([os.path.dirname(__file__),"CustomUVChecker_byValle_2K.png"])
    texture.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(path_to_uv)
    transform2d_out_result = transform2d.CreateOutput("result", Sdf.ValueTypeNames.Float2)
    texture.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(transform2d_out_result)

    transform2d.CreateInput("scale", Sdf.ValueTypeNames.Float2).Set((x_scale, y_scale))
    transform2d_in_uv = transform2d.CreateInput("in", Sdf.ValueTypeNames.Float2)
    reader_out = reader.CreateOutput("result", Sdf.ValueTypeNames.Float2)
    transform2d_in_uv.ConnectToSource(reader_out)
    reader.CreateInput("varname", Sdf.ValueTypeNames.String).Set("st")

    selected_prim.ApplyAPI(UsdShade.MaterialBindingAPI)
    UsdShade.MaterialBindingAPI(selected_prim).Bind(material, UsdShade.Tokens.strongerThanDescendants)

def main(api, parent=None):
    chunk_api = Chunk(api, parent=parent)
    chunk_api.name = "UV Checker"
    chunk_api.category = "Utility"
    layout = chunk_api.create_layout()
    x_scale = chunk_api.create_float_spinbox(value=1.0, layout=layout)
    y_scale = chunk_api.create_float_spinbox(value=1.0, layout=layout)
    chunk_api.create_button("Assign Checker!", apply, x_scale, y_scale)

    return chunk_api





