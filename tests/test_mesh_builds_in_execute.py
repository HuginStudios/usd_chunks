"""The Mesh chunk builds its shape in execute(), and only there.

Each shape's group had a callback that built it, and execute() replayed one, finding the
group by name. A Mesh chunk named "cube" has its cube group renamed "cube1", so execute()
found nothing, and every recook -- which starts from a cleared layer -- left it empty:
behind an Edit Target, where a chunk is cooked again once it is homed, the cube was built
by the callback and gone. The rest pin the build now living in execute() alone.
"""

from __future__ import annotations

import pytest
from pxr import UsdGeom

from usd_chunks import Scene, chunk


def _points(stage, path):
    prim = stage.GetPrimAtPath(path)
    assert prim and prim.GetTypeName() == "Mesh", f"no mesh at {path}"
    return len(UsdGeom.Mesh(prim).GetPointsAttr().Get())


@pytest.mark.parametrize("shape", ["cube", "sphere", "torus", "cone", "tube", "grid"])
def test_every_shape_builds(shape) -> None:
    with Scene() as scene:
        scene.create.mesh(mesh_type=shape, prim_path=f"/geo/{shape}")
        assert _points(scene.get_stage(), f"/geo/{shape}") > 0


def test_a_mesh_named_after_its_shape_builds() -> None:
    with Scene() as scene:
        mesh = scene.create.cube(prim_path="/geo/box", name="cube")
        assert _points(scene.get_stage(), "/geo/box") == 8
        cube_params = next(g for g in mesh.get_my_params(chunk.is_group) if g.get_label() == "Cube")
        assert not cube_params.has_state(chunk.ChunkFlags.State.HIDDEN), "its params are shown"


def test_a_cube_behind_an_edit_target_is_built() -> None:
    with Scene() as scene:
        layer = scene.create.layer(layer_name="geo")
        with scene.create.edit_target(layer, edit_target="geo"):
            scene.create.cube(prim_path="/asset/geo", name="box")
        assert _points(scene.get_stage(), "/asset/geo") == 8


def test_a_recook_with_nothing_changed_keeps_the_shape() -> None:
    with Scene() as scene:
        mesh = scene.create.cube(prim_path="/geo/box", name="box")
        mesh.execute()
        assert _points(scene.get_stage(), "/geo/box") == 8


def test_a_param_change_rebuilds_and_a_move_leaves_nothing_behind() -> None:
    with Scene() as scene:
        mesh = scene.create.cube(prim_path="/geo/box", name="box")
        mesh.get_param("dimx", chunk.is_param_t(int)).value = 3
        stage = scene.get_stage()
        assert _points(stage, "/geo/box") > 8
        mesh.get_param("prim_path", chunk.is_param_t(str)).value = "/geo/moved"
        assert _points(stage, "/geo/moved") > 8
        assert not stage.GetPrimAtPath("/geo/box"), "the old path is gone"


def test_none_builds_nothing() -> None:
    with Scene() as scene:
        mesh = scene.create.cube(prim_path="/geo/box", name="box")
        mesh.get_param("mesh_type", chunk.is_param_t(str)).value = "none"
        assert not scene.get_stage().GetPrimAtPath("/geo/box")
