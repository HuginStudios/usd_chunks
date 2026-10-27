from __future__ import annotations
from pxr import UsdGeom
from usd_chunks import chunk

#: The camera this chunk last put in the viewport. A cook only re-applies a different pick:
#: this chunk is downstream of the Camera chunks, so it cooks whenever they do -- including
#: when their "Set as Viewport Camera" button is clicked -- and re-applying the same pick on
#: each of those took the viewport straight back from the camera just chosen.
_APPLIED = "_set_camera_applied"


def _camera_paths(stage):
    """Every camera prim on the stage, as path strings in traversal order."""
    if not stage:
        return []
    return [str(prim.GetPath()) for prim in stage.Traverse() if prim.IsA(UsdGeom.Camera)]


def menu_items(node, param_name):
    """The Camera menu's items, asked for as it opens (chunk.Menu.refresh_items).

    Not kept current by listening to the stage. Cameras usually come from a Camera chunk
    upstream, and the list is wanted only when someone opens the menu -- a global listener
    walked the stage for it on every round of prim changes, whether anyone looked or not.
    Read on opening, a saved pick is long in before anyone looks (a list built before the
    restore coerced it to the first item), a node blocked until it is wired fills like any
    other, and a pick the stage does not offer right now stays listed (Menu.set_items).
    """
    if param_name != "camera":
        return None
    return _camera_paths(node.get_stage())


def execute(node):
    stage = node.get_stage()
    path = node.get_param("camera").value or ""
    if not path:
        return
    prim = stage.GetPrimAtPath(path)
    if not prim.IsValid() or not prim.IsA(UsdGeom.Camera):
        return
    scene = node.get_scene()
    if scene is None or getattr(node, _APPLIED, None) == path:
        return
    setattr(node, _APPLIED, path)
    scene.set_viewport_camera(prim)


def main():
    chunk_api = chunk.Chunk("Set Camera")

    # Filled from the stage as it opens (menu_items). An empty menu accepts any value (Menu.convert_attr only checks
    # a value against a non-empty list), which is what lets a saved pick load before the
    # menu exists and an @Node.param@ expression stand in for it.
    camera = chunk.Menu(
        "camera", "Camera",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="The camera prims on the stage. The pick becomes the viewport camera.",
    )
    chunk_api.add_param(camera)

    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.MODELLING,
        icon="camera",
        description="""
        Set Camera chunk — picks one of the stage's cameras and looks through it.

        Camera is a dropdown of every UsdGeom.Camera prim on the stage, read as it
        opens: a camera created by a Camera chunk upstream is there the next time
        you open it. The pick is stored on the chunk and applied when it cooks, so
        it survives a save and can be lofted onto a chunkgroup to vary the camera
        per variant or shot. A pick the stage does not currently offer stays listed
        rather than being dropped, so a saved pick survives loading ahead of the
        chunk that creates the camera. A cook re-applies the pick only when it
        has changed, so a camera chosen elsewhere (a Camera chunk's "Set as
        Viewport Camera") is not taken back by the next upstream cook.

        Authors nothing on the stage: the only effect is the host viewport
        (scene.set_viewport_camera), the same call the Camera chunk's
        "Set as Viewport Camera" button makes.
        """,
    )
