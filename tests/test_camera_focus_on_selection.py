"""Focus on Selection: the camera's focus distance from what is selected in the viewport.

One prim focuses on it; several focus on their average. Depth along the view axis rather
than straight-line distance, because focus is a plane parallel to the sensor -- a prim at
the edge of frame is as far away as the plane through it.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest
from pxr import Gf, Usd, UsdGeom

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins.chunk_usd import initialize as _chunk_usd_init

CAMERA = "/cameras/camera1"


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage
        self.selected = []

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return list(self.selected)

    def get_current_frame(self):
        return 0.0

    def should_install_package(self, package):
        return False


def _cube(stage, path, center, size=1.0):
    cube = UsdGeom.Cube.Define(stage, path)
    cube.GetSizeAttr().Set(size)
    UsdGeom.XformCommonAPI(cube).SetTranslate(Gf.Vec3d(*center))
    return cube.GetPrim()


def _module():
    """scripts/ is not importable as a package; the chunk is loaded from its file."""
    path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "camera.py"
    spec = importlib.util.spec_from_file_location("camera_chunk_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_single_prim_sets_the_depth_to_its_centre() -> None:
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Camera.Define(stage, CAMERA)
    # The camera looks down -Z from the origin; a cube 40 units down that axis is 40 away.
    target = _cube(stage, "/world/near", (0, 0, -40))

    assert _module().selection_focus_distance(stage, CAMERA, [target]) == pytest.approx(40.0)


def test_several_prims_average_their_depths() -> None:
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Camera.Define(stage, CAMERA)
    a = _cube(stage, "/world/a", (0, 0, -10))
    b = _cube(stage, "/world/b", (0, 0, -30))

    assert _module().selection_focus_distance(stage, CAMERA, [a, b]) == pytest.approx(20.0)


def test_depth_is_along_the_view_axis_not_the_ray() -> None:
    """A prim 30 down the axis and 40 off to the side is 50 away as the ray flies, but the
    focal plane through it is 30 away, and that is what focuses it."""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Camera.Define(stage, CAMERA)
    side = _cube(stage, "/world/side", (40, 0, -30))

    assert _module().selection_focus_distance(stage, CAMERA, [side]) == pytest.approx(30.0)


def test_the_camera_placement_is_honoured() -> None:
    """A camera moved and turned measures from where it is, along where it looks."""
    stage = Usd.Stage.CreateInMemory()
    cam = UsdGeom.Camera.Define(stage, CAMERA)
    # At x=100, turned 90 degrees about Y so -Z becomes -X: it looks toward the origin.
    UsdGeom.XformCommonAPI(cam).SetTranslate(Gf.Vec3d(100, 0, 0))
    UsdGeom.XformCommonAPI(cam).SetRotate(Gf.Vec3f(0, 90, 0))
    target = _cube(stage, "/world/origin", (25, 0, 0))

    assert _module().selection_focus_distance(stage, CAMERA, [target]) == pytest.approx(75.0)


def test_nothing_measurable_leaves_the_value_alone() -> None:
    stage = Usd.Stage.CreateInMemory()
    cam = UsdGeom.Camera.Define(stage, CAMERA).GetPrim()

    assert _module().selection_focus_distance(stage, CAMERA, []) is None
    assert _module().selection_focus_distance(stage, CAMERA, [cam]) is None, "the camera itself is not a subject"
    assert _module().selection_focus_distance(stage, "/nowhere", [cam]) is None


def _camera_scene(qapp):
    stage = Usd.Stage.CreateInMemory()
    scene = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)

    camera_module = chunk_registry.ChunkRegistry().get_chunk_module("camera")
    assert camera_module is not None
    camera = (camera_module.create() if camera_module.loaded else camera_module.load())[0]
    scene.add_chunk(camera)
    qapp.processEvents()
    assert stage.GetPrimAtPath(camera.get_param("primpath_location").value).IsValid()
    return stage, scene, camera


def test_the_button_writes_the_param(qapp) -> None:
    stage, scene, camera = _camera_scene(qapp)

    scene.selected = [_cube(stage, "/world/a", (0, 0, -12)), _cube(stage, "/world/b", (0, 0, -18))]
    camera.get_param("focus_on_selection").click()

    assert camera.get_param("focusDistance").value == pytest.approx(15.0)


# ---------------------------------------------------------------------------
# autofocus: the point is remembered, and the distance follows the camera
# ---------------------------------------------------------------------------

def test_the_button_remembers_the_point_and_turns_autofocus_on(qapp) -> None:
    stage, scene, camera = _camera_scene(qapp)
    scene.selected = [_cube(stage, "/world/a", (0, 0, -12)), _cube(stage, "/world/b", (0, 0, -18))]

    camera.get_param("focus_on_selection").click()

    assert tuple(camera.get_param("focus_point").value) == pytest.approx((0.0, 0.0, -15.0))
    assert camera.get_param("autofocus").value is True


def test_moving_the_camera_refocuses_on_the_point(qapp) -> None:
    """The reported ask: pull focus to a prim once, then fly -- the distance follows."""
    stage, scene, camera = _camera_scene(qapp)
    scene.selected = [_cube(stage, "/world/a", (0, 0, -15))]
    camera.get_param("focus_on_selection").click()
    assert camera.get_param("focusDistance").value == pytest.approx(15.0)

    camera.get_param("tz").value = 5.0          # back away along the view axis
    qapp.processEvents()
    assert camera.get_param("focusDistance").value == pytest.approx(20.0)

    camera.get_param("tx").value = 100.0        # sideways: the focal plane is unchanged
    qapp.processEvents()
    assert camera.get_param("focusDistance").value == pytest.approx(20.0)


def test_the_point_stays_when_the_prim_moves(qapp) -> None:
    """A place in the world was remembered, not the prim."""
    stage, scene, camera = _camera_scene(qapp)
    cube = _cube(stage, "/world/a", (0, 0, -15))
    scene.selected = [cube]
    camera.get_param("focus_on_selection").click()

    UsdGeom.XformCommonAPI(cube).SetTranslate(Gf.Vec3d(0, 0, -40))
    camera.get_param("tz").value = 1.0
    qapp.processEvents()

    assert camera.get_param("focusDistance").value == pytest.approx(16.0)


def test_autofocus_off_leaves_the_distance_alone(qapp) -> None:
    stage, scene, camera = _camera_scene(qapp)
    scene.selected = [_cube(stage, "/world/a", (0, 0, -15))]
    camera.get_param("focus_on_selection").click()
    camera.get_param("autofocus").value = False
    qapp.processEvents()

    camera.get_param("tz").value = 5.0
    qapp.processEvents()

    assert camera.get_param("focusDistance").value == pytest.approx(15.0)


def test_a_typed_focus_point_works_without_a_selection(qapp) -> None:
    stage, scene, camera = _camera_scene(qapp)
    camera.get_param("focus_point").value = (0.0, 0.0, -30.0)
    camera.get_param("autofocus").value = True
    qapp.processEvents()

    assert camera.get_param("focusDistance").value == pytest.approx(30.0)
