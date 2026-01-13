import os
from usd_chunks import chunk, chunk_notice
from pxr import Sdf, Usd, UsdGeom, UsdShade
from PySide6 import QtWidgets


def apply(notice, sender: chunk.Chunk):
    if notice.data is not sender.get_param("apply"):
        return

    # Access the parameter inside the group
    x_scale_param = sender.get_param("uvscale")[0]
    y_scale_param = sender.get_param("uvscale")[1]

    x_scale = x_scale_param.value
    y_scale = y_scale_param.value

    filebrowser = sender.get_param("browse")

    api = sender.get_usdview_api()
    checker_file = filebrowser.value
    selected_prim = api.prim

    if not selected_prim or selected_prim == api.stage.GetPseudoRoot():
        msg = "Prim not selected."
        if selected_prim:
            msg = "Cannot apply material on the top PseudoRoot!"
        error_dlg = QtWidgets.QMessageBox()
        error_dlg.setWindowTitle("Error")
        error_dlg.setText(msg)
        error_dlg.exec_()
        return

    # Create an group to hold the UV checker matirial
    stage = api.stage

    uv_checker_layer = Sdf.Layer.CreateAnonymous("uv_checker")
    stage.GetSessionLayer().subLayerPaths.append(uv_checker_layer.identifier)
    with Usd.EditContext(stage, uv_checker_layer):
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

        selected_prim.ApplyAPI(UsdShade.MaterialBindingAPI)
        UsdShade.MaterialBindingAPI(selected_prim).Bind(material, UsdShade.Tokens.strongerThanDescendants)


def clear(notice, sender: chunk.Chunk):
    if notice.data is not sender.get_param("clear"):
        return

    """Remove the assign checker edits"""
    stage = sender.get_stage()
    sublayer_paths = list(stage.GetSessionLayer().subLayerPaths).copy()
    for i, sublayer in enumerate(sublayer_paths):
        if "uv_checker" in sublayer:
            sublayer_paths.pop(i)
    stage.GetSessionLayer().subLayerPaths = sublayer_paths


def scale(notice, sender):
    # _, param = chunkItem.get_last_param_change()
    # print(param.get_last_change())
    
    stage = sender.get_stage()
    layer_id = None
    sublayer_paths = list(stage.GetSessionLayer().subLayerPaths)
    for sublayer in sublayer_paths:
        if "uv_checker" in sublayer:
            layer_id = sublayer
            break
    if layer_id is None:
        return

    layer = Sdf.Layer.FindOrOpen(layer_id)

    with Usd.EditContext(stage, layer):
        base_material_grp = "/materials"
        material_path = f"{base_material_grp}/UVCheckerMaterial"
        transform2d_path = f"{material_path}/transform2d"

        prim = stage.GetPrimAtPath(transform2d_path)
        if not prim:
            return

        scale_attr = prim.GetAttribute("inputs:scale")
        scale_val = scale_attr.Get()

        # idx = 1 if param.name == "scale1" else 0
        scale_val[0] = sender[0].value
        scale_val[1] = sender[1].value
        scale_attr.Set(scale_val)


def main():
    chunk_api = chunk.Chunk("UV Checker", category="Utility")

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

    chunk_api.add_callback(apply, notice_alias=chunk_notice.ChunkNoticeAlias.PARAM_CHANGED)
    chunk_api.add_callback(clear, notice_alias=chunk_notice.ChunkNoticeAlias.PARAM_CHANGED)

    return chunk_api
