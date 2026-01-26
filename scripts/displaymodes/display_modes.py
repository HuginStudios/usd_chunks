import os
from usd_chunks import chunk, chunk_registry
from plugins import chunk_usdview


def set_shademode(owner: chunk.ChunkParamBase, notice, sender: chunk.Button):
    shade_action = owner.user_data["action"]
    shade_action.setChecked(True)

    scene = chunk_registry.ChunkRegistry().scene
    # For chunk_usdview plugin, the SceneSender is subclassed to to also provide
    # access to the usdviewAPI
    appController = scene.usdviewAPI._UsdviewApi__appController
    appController._changeRenderMode(shade_action)
    appController._stageView.updateGL()


def main():
    chunk_api = chunk.Chunk("Display Modes")
    scene = chunk_registry.ChunkRegistry().scene

    if not isinstance(scene, chunk_usdview.UsdviewScene):
        return

    appController = scene.usdviewAPI._UsdviewApi__appController

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
            icon_name, tooltip=shade_action.text(), user_data={"action": shade_action}, icon=shade_icon
        )
        button.add_callback(set_shademode)

        chunk_api.add_param(button)

    return chunk_api


def config():
    return chunk.DefaulConfig(load_for_plugin="chunk_usdview", category=chunk.StandardCategory.UTILS)
