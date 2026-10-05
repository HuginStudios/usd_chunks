"""Set Camera's dropdown lists the stage's cameras, follows the stage, and sets the viewport.

Same menu rules as Set Variant: read off the stage as it opens -- no stage listener --
and a value the stage does not offer right now stays listed rather than being coerced away.
"""
from __future__ import annotations

import pytest
from pxr import Usd, UsdGeom

from usd_chunks import chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import initialize as _chunk_usd_init


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage
        self.viewport_cameras = []

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []

    def should_install_package(self, package):
        return False

    def set_viewport_camera(self, prim):
        self.viewport_cameras.append(str(prim.GetPath()))


def _define_camera(stage, path):
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        UsdGeom.Camera.Define(stage, path)


@pytest.fixture
def scene(qapp):
    stage = Usd.Stage.CreateInMemory()
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        stage.DefinePrim("/geo", "Xform")
    _define_camera(stage, "/cameras/main")
    _define_camera(stage, "/cameras/closeup")
    sc = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(sc, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, sc)
    return sc


def _set_camera(qapp, scene):
    module = chunk_registry.ChunkRegistry().get_chunk_module("set_camera")
    assert module is not None
    node = (module.create() if module.loaded else module.load())[0]
    scene.add_chunk(node)
    qapp.processEvents()
    return node


def _opened(menu):
    """The menu's items as a frontend shows them: asked for as it opens (Menu.refresh_items)."""
    menu.refresh_items()
    return menu.menu_items


def _listens_globally(node):
    return [d["conn"].get_method() for d in node._callbacks.callbacks if d["conn"].globally]


def test_the_menu_lists_the_stages_cameras(qapp, scene) -> None:
    node = _set_camera(qapp, scene)

    assert _opened(node.get_param("camera")) == ["/cameras/main", "/cameras/closeup"]


def test_the_menu_follows_the_stage(qapp, scene) -> None:
    node = _set_camera(qapp, scene)

    _define_camera(scene.get_stage(), "/cameras/wide")
    qapp.processEvents()

    assert "/cameras/wide" in _opened(node.get_param("camera"))
    assert _listens_globally(node) == []


def test_picking_a_camera_sets_the_viewport(qapp, scene) -> None:
    node = _set_camera(qapp, scene)

    node.get_param("camera").value = "/cameras/closeup"
    qapp.processEvents()

    assert scene.viewport_cameras[-1] == "/cameras/closeup"


def test_a_pick_the_stage_lacks_stays_listed(qapp, scene) -> None:
    node = _set_camera(qapp, scene)
    menu = node.get_param("camera")
    menu.value = "/cameras/closeup"
    qapp.processEvents()

    with Usd.EditContext(scene.get_stage(), scene.get_stage().GetSessionLayer()):
        scene.get_stage().RemovePrim("/cameras/closeup")
    qapp.processEvents()

    assert menu.value == "/cameras/closeup"
    assert "/cameras/closeup" in _opened(menu)


def test_a_recook_with_the_same_pick_leaves_the_viewport_alone(qapp, scene) -> None:
    """Set Camera sits downstream of the Camera chunks and cooks whenever they do -- their
    "Set as Viewport Camera" button cooks them. Re-applying its own pick on each of those took
    the viewport straight back from the camera the button had just chosen."""
    node = _set_camera(qapp, scene)
    node.get_param("camera").value = "/cameras/closeup"
    qapp.processEvents()
    applied = len(scene.viewport_cameras)

    node.execute()
    qapp.processEvents()

    assert len(scene.viewport_cameras) == applied
