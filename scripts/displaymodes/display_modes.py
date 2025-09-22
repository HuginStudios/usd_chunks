import os
from usd_chunks import chunk


def set_shademode(chunkItem : chunk.ChunkParam, notice, sender):
    shade_action = chunkItem.user_data["action"]
    shade_action.setChecked(True)
    appController = chunkItem.get_usdview_api()._UsdviewApi__appController
    appController._changeRenderMode(shade_action)
    appController._stageView.updateGL()


def main(api):
    chunk_api = chunk.Chunk()
    chunk_api.name = "Display Modes"
    chunk_api.category = "Utility"
    # layout = chunk_api.new_layout()
    appController = api._UsdviewApi__appController
 
    icons_dir =  os.path.join(os.path.dirname(__file__),"icons")
    shade_modes = [
        (appController._ui.actionSmooth_Shaded, "Smoothshaded"),
        (appController._ui.actionWireframeOnSurface, "WireframeOnSurface"),
        (appController._ui.actionWireframe, "Wireframe"),
        (appController._ui.actionFlat_Shaded, "Flatshaded"),
        (appController._ui.actionPoints, "Points"),
    ]

    for shade_info in shade_modes:
        shade_action = shade_info[0]
        icon_name = shade_info[1]
        shade_icon = os.path.join(icons_dir, icon_name) + ".png"

        button = chunk.ChunkParam()
        button.param_type = chunk.ChunkParamType.BUTTON
        button.name = icon_name
        button.tooltip = shade_action.text()
        button.user_data["action"] = shade_action
        button.icon = shade_icon
        button.add_callback(set_shademode)

        chunk_api.add_param(button)

    return chunk_api