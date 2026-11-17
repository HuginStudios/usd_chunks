"""The Houdini host's gizmo maths: a chunk's box and translate gizmos as Houdini handles.

hosts/houdini/gizmos.py imports no hou, so this runs anywhere. The viewer state that binds
the handles needs Houdini's UI; test_houdini_host checks the HDA opens it.
"""
from __future__ import annotations

import pytest
from pxr import Gf, Usd, UsdGeom

from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_core import ChunkConfig, ChunkFlags
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.hosts.houdini import gizmos

from chunk_stubs import attach_stub_module

FACES = ("px", "nx", "py", "ny", "pz", "nz")


class _Scene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


@pytest.fixture(autouse=True)
def _registry():
    chunk_registry.ChunkRegistry().initialize(_Scene(), "pytest")


def _box_chunk(hidden: bool = False):
    c = chunk.Chunk("Volume")
    c.add_param(chunk.LineEdit("primpath_location", "Location", default_value="/World/Box",
                               flags=ChunkFlags.ParamFlags.LABELED))
    centre = chunk.Group("centre_grp", "Centre", gizmo="translate")
    centre.add_params([chunk.Slider(n, n, default_value=0.0, min_value=-10.0, max_value=10.0)
                       for n in ("cx", "cy", "cz")])
    c.add_param(centre)
    for name, value in zip(FACES, (1.0, 2.0, 3.0, 0.0, 4.0, 5.0)):
        c.add_param(chunk.Slider(name, name, default_value=value, min_value=0.0, max_value=50.0))
    c._config = ChunkConfig(module="volume", gizmos=[
        chunk.Gizmo("box", type="box", driver=list(FACES)),
        chunk.Gizmo("zdist", type="cube", driver="px"),  # a type Houdini has no handle for yet
    ])
    attach_stub_module(c, **({"gizmo_hidden": lambda node, name: name == "box"} if hidden else {}))
    return c


def _turned(angle: float = 30.0, scale: float = 1.0, at=(1.0, 2.0, 3.0)) -> Gf.Matrix4d:
    return (Gf.Matrix4d(1.0).SetScale(scale) * Gf.Matrix4d(1.0).SetRotate(Gf.Rotation(Gf.Vec3d(0, 1, 0), angle))
            * Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(*at)))


def test_box_and_translate_gizmos_get_handles_and_others_do_not():
    targets = gizmos.targets(_box_chunk())
    assert [(t.name, t.type, t.anchor) for t in targets] == [
        ("box", "box", "/World/Box"), ("centre_grp", "translate", "/World/Box")]


def test_a_hidden_gizmo_gets_no_handle():
    assert [t.name for t in gizmos.targets(_box_chunk(hidden=True))] == ["centre_grp"]


def test_driver_values_are_read_off_the_params():
    c = _box_chunk()
    box = gizmos.targets(c)[0]
    assert gizmos.driver_values(c, box) == [1.0, 2.0, 3.0, 0.0, 4.0, 5.0]


def test_an_unturned_box_is_its_centre_and_size():
    parms = gizmos.box_to_handle([1, 2, 3, 0, 4, 5], Gf.Matrix4d(1.0))
    assert parms["centerx"] == pytest.approx(-0.5)
    assert parms["centery"] == pytest.approx(1.5)
    assert parms["centerz"] == pytest.approx(-0.5)
    assert (parms["sizex"], parms["sizey"], parms["sizez"]) == pytest.approx((3, 3, 9))


@pytest.mark.parametrize("world", [Gf.Matrix4d(1.0), _turned(), _turned(-75.0, 0.01)])
def test_a_box_comes_back_from_its_handle_unchanged(world):
    values = [1.0, 2.0, 3.0, 0.0, 4.0, 5.0]
    assert gizmos.handle_to_box(gizmos.box_to_handle(values, world), world) == pytest.approx(values, abs=1e-9)


def test_a_turned_box_handle_is_turned_with_its_anchor():
    parms = gizmos.box_to_handle([1, 1, 1, 1, 1, 1], _turned(30.0))
    assert (parms["rx"], parms["ry"], parms["rz"]) == pytest.approx((0.0, 30.0, 0.0), abs=1e-6)


def test_pulling_one_face_of_the_handle_moves_only_that_face():
    # Houdini's boundingbox handle moves one face by growing the size and shifting the centre
    # half as far, along the handle's (the anchor's) own axis.
    world = _turned(40.0, 2.0)
    parms = gizmos.box_to_handle([1.0, 2.0, 3.0, 0.0, 4.0, 5.0], world)
    axis = world.TransformDir(Gf.Vec3d(1, 0, 0)).GetNormalized()
    parms["sizex"] += 1.0
    for i, name in enumerate(("centerx", "centery", "centerz")):
        parms[name] += axis[i] * 0.5
    assert gizmos.handle_to_box(parms, world) == pytest.approx([1.5, 2.0, 3.0, 0.0, 4.0, 5.0], abs=1e-9)


def test_a_translate_handle_moves_in_its_parent_space():
    parent = _turned(90.0, 2.0)
    value = gizmos.moved((1.0, 0.0, 0.0), (5.0, 5.0, 5.0), (5.0, 5.0, 3.0), parent)
    # -2 along world Z is +1 along the parent's X: turned 90 about Y, twice the size.
    assert tuple(value) == pytest.approx((2.0, 0.0, 0.0), abs=1e-9)


def test_anchor_matrices_are_in_houdinis_world():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/World").AddTranslateOp().Set(Gf.Vec3d(0, 0, 10))
    UsdGeom.Xform.Define(stage, "/World/Box").AddTranslateOp().Set(Gf.Vec3d(1, 0, 0))
    to_host = Gf.Matrix4d(1.0).SetScale(0.01)  # a centimetre network in Houdini's metres
    world, parent = gizmos.anchor_matrices(stage, "/World/Box", to_host)
    assert tuple(world.ExtractTranslation()) == pytest.approx((0.01, 0.0, 0.1))
    assert tuple(parent.ExtractTranslation()) == pytest.approx((0.0, 0.0, 0.1))
    assert gizmos.anchor_matrices(stage, "/World/Missing", to_host) is None
