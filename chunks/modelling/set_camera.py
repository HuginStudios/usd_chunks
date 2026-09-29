from __future__ import annotations
from pxr import Usd, UsdGeom
from usd_chunks import chunk

#: Set on the chunk once CHUNK_LOADED has fired -- its value is in, the menu may fill.
_IN_SCENE = "_set_camera_in_scene"


def _chunk_loaded(owner, notice, sender):
    setattr(owner.get_chunk(), _IN_SCENE, True)
    _refresh_menu(owner, notice, sender)


def _camera_paths(stage):
    """Every camera prim on the stage, as path strings in traversal order."""
    if not stage:
        return []
    return [str(prim.GetPath()) for prim in stage.Traverse() if prim.IsA(UsdGeom.Camera)]


def _refresh_menu(owner, notice=None, _sender=None):
    """Rebuild the Camera menu from the cameras on the stage.

    Registered globally on stage object changes -- cameras usually come from a Camera
    chunk upstream, so they appear (and move) as the graph cooks, not when this chunk's own
    params move. Not gated on is_blocked(), and held off only until CHUNK_LOADED, for the
    reasons set_variant._refresh_menus spells out: a blocked node still needs a filled
    dropdown, and a list built before the saved value is restored would coerce that value
    to the first item.

    Only a prim resync can add or remove a camera; the info-only changes that make up
    nearly every round of a cook (a value written to an attribute) cannot. Rebuilding on
    all of them meant a full stage traversal per attribute write -- 3,579 traversals of a
    car asset over one scene open, measured -- so those rounds return before touching the
    stage. ObjectsChanged rather than StageContentsChanged for exactly this reason: only
    the former says what changed. A CHUNK_LOADED call arrives with a chunk notice instead
    and always rebuilds.
    """
    _chunk = owner.get_chunk()
    if _chunk is None or not getattr(_chunk, _IN_SCENE, False):
        return
    if isinstance(notice, Usd.Notice.ObjectsChanged) and not any(
        path.IsPrimPath() for path in notice.GetResyncedPaths()
    ):
        return
    menu = _chunk.get_param("camera")
    if menu is None:
        # Deleting the chunk tears its params down while the stage edits that teardown
        # makes are still firing this global handler for it.
        return
    menu.set_items(_camera_paths(_chunk.get_stage()))


def execute(node):
    stage = node.get_stage()
    path = node.get_param("camera").value or ""
    if not path:
        return
    prim = stage.GetPrimAtPath(path)
    if not prim.IsValid() or not prim.IsA(UsdGeom.Camera):
        return
    scene = node.get_scene()
    if scene is None:
        return
    scene.set_viewport_camera(prim)


def main():
    chunk_api = chunk.Chunk("Set Camera")

    # Filled from the stage. An empty menu accepts any value (Menu.convert_attr only checks
    # a value against a non-empty list), which is what lets a saved pick load before the
    # menu exists and an @Node.param@ expression stand in for it.
    camera = chunk.Menu(
        "camera", "Camera",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="The camera prims on the stage. The pick becomes the viewport camera.",
    )
    chunk_api.add_param(camera)

    chunk_api.add_callback(_chunk_loaded, notice_alias=chunk.ChunkNoticeAlias.CHUNK_LOADED)
    chunk_api.add_callback(
        _refresh_menu,
        notice_alias=chunk.ChunkNoticeAlias.OBJECT_CHANGED,
        listen_globally=True,
    )

    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.MODELLING,
        description="""
        Set Camera chunk — picks one of the stage's cameras and looks through it.

        Camera is a dropdown of every UsdGeom.Camera prim on the stage, and follows
        the stage: a camera created by a Camera chunk upstream shows up as soon as
        it cooks. The pick is stored on the chunk and re-applied on every cook, so
        it survives a save and can be lofted onto a chunkgroup to vary the camera
        per variant or shot. A pick the stage does not currently offer stays listed
        rather than being dropped, so a saved pick survives loading ahead of the
        chunk that creates the camera.

        Authors nothing on the stage: the only effect is the host viewport
        (scene.set_viewport_camera), the same call the Camera chunk's
        "Set as Viewport Camera" button makes.
        """,
    )
