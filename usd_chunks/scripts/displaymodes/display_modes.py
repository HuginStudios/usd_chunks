import os
from usd_chunks.chunk import Chunk


def set_shademode(api, appController, shade_action):
    shade_action.setChecked(True)
    appController._changeRenderMode(shade_action)
    appController._stageView.updateGL()


def main(api, parent=None):
    chunk_api = Chunk(api, parent=parent)
    chunk_api.name = "Display Modes"
    chunk_api.category = "Utility"
    layout = chunk_api.create_layout()
    appController = api._UsdviewApi__appController
 
    icons_dir =  os.path.join(os.path.dirname(__file__),"icons")
    shade_modes = [
        (appController._ui.actionSmooth_Shaded, os.path.join(icons_dir, "Smoothshaded.png")),
        (appController._ui.actionWireframeOnSurface, os.path.join(icons_dir, "WireframeOnSurface.png")),
        (appController._ui.actionWireframe, os.path.join(icons_dir, "Wireframe.png")),
        (appController._ui.actionFlat_Shaded, os.path.join(icons_dir, "Flatshaded.png")),
        (appController._ui.actionPoints, os.path.join(icons_dir, "Points.png")),
    ]
    for shade_info in shade_modes:
        shade_action = shade_info[0]
        shade_icon = shade_info[1]
        chunk_api.create_button("",
            set_shademode,
            appController,
            shade_action,
            layout=layout,
            icon=shade_icon,
            width=40,
            height=40,
            tooltip=shade_action.text())
    
    return chunk_api