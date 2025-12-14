import os
from usd_chunks import chunk


def set_shademode(notice, sender: chunk.Button):
    shade_action = sender.user_data["action"]
    shade_action.setChecked(True)
    appController = sender.get_usdview_api()._UsdviewApi__appController
    appController._changeRenderMode(shade_action)
    appController._stageView.updateGL()


def main(api):
    chunk_api = chunk.Chunk("Display Modes", category="Utility")
    appController = api._UsdviewApi__appController

    icons_dir = os.path.join(os.path.dirname(__file__), "icons")
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

        button = chunk.Button(
            icon_name,
            tooltip=shade_action.text(),
            user_data={"action": shade_action},
            icon=shade_icon
        )
        button.add_callback(set_shademode)

        chunk_api.add_param(button)

    return chunk_api
