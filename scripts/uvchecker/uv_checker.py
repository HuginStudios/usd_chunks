import os
from usd_chunks import chunk, chunk_notice, chunk_registry
from pxr import Sdf, UsdShade
from PySide6 import QtWidgets


def apply(owner: chunk.Chunk, notice, sender):
    # if notice.data is not sender.get_param("apply"):
    #     return

    # Access the parameter inside the group
    x_scale_param = owner.get_param("uvscale", chunk.is_inst(chunk.Vector))[0]
    y_scale_param = owner.get_param("uvscale", chunk.is_inst(chunk.Vector))[1]

    x_scale = x_scale_param.value
    y_scale = y_scale_param.value

    filebrowser = owner.get_param("browse", chunk.is_param_t(str))
    checker_file = filebrowser.value

    # api = sender.get_usdview_api()
    scene = chunk_registry.ChunkRegistry().scene
    stage = scene.get_stage()
    selected_prims = scene.get_selected_prims()

    if not selected_prims or selected_prims == stage.GetPseudoRoot():
        msg = "Prim not selected."
        if selected_prims:
            msg = "Cannot apply material on the top PseudoRoot!"
        error_dlg = QtWidgets.QMessageBox()
        error_dlg.setWindowTitle("Error")
        error_dlg.setText(msg)
        error_dlg.exec_()
        return

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
    stage.GetPrimAtPath(surface_path).GetAttribute("info:id").Set("UsdPreviewSurface")

    texture = UsdShade.Shader.Define(stage, texture_path)
    stage.GetPrimAtPath(texture_path).GetAttribute("info:id").Set("UsdUVTexture")

    transform2d = UsdShade.Shader.Define(stage, transform2d_path)
    stage.GetPrimAtPath(transform2d_path).GetAttribute("info:id").Set("UsdTransform2d")

    reader = UsdShade.Shader.Define(stage, reader_path)
    stage.GetPrimAtPath(reader_path).GetAttribute("info:id").Set("UsdPrimvarReader_float2")

    # Create the inputs and outputs for the shaders
    preview_output_surface = preview.CreateOutput("surface", Sdf.ValueTypeNames.Token)
    material.GetOutput("surface").ConnectToSource(preview_output_surface)

    preview_in_diff = preview.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f)
    texture_out_rgb = texture.CreateOutput("rgb", Sdf.ValueTypeNames.Color3f)
    preview_in_diff.ConnectToSource(texture_out_rgb)
    path_to_uv = checker_file
    if not path_to_uv:
        path_to_uv = os.path.sep.join([os.path.dirname(__file__), "CustomUVChecker_byValle_2K.png"])

    texture.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(path_to_uv)
    transform2d_out_result = transform2d.CreateOutput("result", Sdf.ValueTypeNames.Float2)
    texture.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(transform2d_out_result)

    transform2d.CreateInput("scale", Sdf.ValueTypeNames.Float2).Set((x_scale, y_scale))
    transform2d_in_uv = transform2d.CreateInput("in", Sdf.ValueTypeNames.Float2)
    reader_out = reader.CreateOutput("result", Sdf.ValueTypeNames.Float2)
    transform2d_in_uv.ConnectToSource(reader_out)
    reader.CreateInput("varname", Sdf.ValueTypeNames.String).Set("st")

    for prim in selected_prims:
        api = UsdShade.MaterialBindingAPI.Apply(prim)
        api.Bind(material, UsdShade.Tokens.strongerThanDescendants)


def clear(owner: chunk.Chunk, notice, sender):
    """Remove the assign checker edits"""
    chunk_registry.ChunkLayerManager().clear_layer(owner)


def scale(owner: chunk.Vector, notice, sender):
    stage = owner.get_stage()
    if not stage:
        return None

    base_material_grp = "/materials"
    material_path = f"{base_material_grp}/UVCheckerMaterial"
    transform2d_path = f"{material_path}/transform2d"

    prim = stage.GetPrimAtPath(transform2d_path)
    if not prim:
        return

    scale_attr = prim.GetAttribute("inputs:scale")
    scale_val = scale_attr.Get()

    # idx = 1 if param.name == "scale1" else 0
    scale_val[0] = owner[0].value
    scale_val[1] = owner[1].value
    scale_attr.Set(scale_val)


def main():
    chunk_api = chunk.Chunk("UV Checker")

    spinbox = chunk.Spinbox(default_value=1)
    vec = chunk.Vector("uvscale", "UV Scale", param=spinbox, length=2, suffixes="UV")

    # Adds a callback that runs when any of the spinboxes changes
    vec.add_callback(scale)

    filebrowser = chunk.FileBrowser("browse")

    visUVsButton = chunk.Button("apply", "Visualize")
    clearUVsButton = chunk.Button("clear", "Clear Vis")

    chunk_api.add_params([vec, filebrowser, visUVsButton, clearUVsButton])

    # Setting the sender explicitly. The callback will then get params as such:
    # 1 - chunk_api (the chunk)
    # 2 - Tf.Notice (the notice)
    # 3 - ChunkParam (the button)

    chunk_api.add_callback(apply, sender=visUVsButton, notice_alias=chunk_notice.ChunkNoticeAlias.PARAM_CHANGED)
    chunk_api.add_callback(clear, sender=clearUVsButton)

    return chunk_api


def config():
    return chunk.DefaulConfig(category=chunk.StandardCategory.UTILS)
