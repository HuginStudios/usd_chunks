"""A solver in a script (Scene.new(), host "script") bakes on cook, like `usdchunks run`.

The RBD and Particle Solvers counted only `usdchunks run` as headless. In a script they
took the interactive path: Auto Simulate off, Run waiting for a press nobody makes, so the
cook baked nothing and the scene stayed still. The build scripts had to call the Run
button's callback by hand.
"""

from __future__ import annotations

import importlib.util

import pytest

from pxr import Sdf, Usd, UsdGeom

from usd_chunks import Scene, chunk, sim_bake


def test_a_script_counts_as_unattended() -> None:
    with Scene():
        assert sim_bake.is_unattended()


@pytest.mark.skipif(importlib.util.find_spec("mujoco") is None, reason="needs mujoco")
def test_rbd_bakes_on_cook_in_a_script() -> None:
    with Scene() as scene:
        ground = scene.create.cube(prim_path="/ground", width=10.0, height=0.2, depth=10.0, name="ground_mesh")
        box = scene.create.cube(ground, prim_path="/box", name="box_mesh")
        lifted = scene.create.transform(box, primpath_location="/box", ty=3.0, name="lift")
        rbd = scene.create.rbd_solver(lifted, prim_path="/box", collision_path="/ground", frame_count=12)

        assert rbd.get_param("auto_sim").value, "Auto Simulate starts on where nobody can press Run"
        box_prim = scene.get_stage().GetPrimAtPath("/box")
        assert any(a.GetNumTimeSamples() for a in box_prim.GetAttributes()), "baked without pressing Run"


@pytest.mark.skipif(importlib.util.find_spec("mujoco") is None, reason="needs mujoco")
def test_a_saved_rbd_scene_reopens_and_bakes(tmp_path) -> None:
    """Opening it bakes during the load. The report at the end asked an expired prim for its
    name -- the bake's own layer move had recomposed the stage -- and the open raised.

    Shaped like a department task: the shot comes in through a Sublayer, and the cfx layer
    sits above it behind an Edit Target.
    """
    shot = Sdf.Layer.CreateNew(str(tmp_path / "shot.usda"))
    stage = Usd.Stage.Open(shot)
    UsdGeom.Cube.Define(stage, "/World/ground").GetSizeAttr().Set(10.0)
    UsdGeom.Xformable(stage.GetPrimAtPath("/World/ground")).AddTranslateOp().Set((0.0, -5.0, 0.0))
    box = UsdGeom.Cube.Define(stage, "/World/box")
    UsdGeom.Xformable(box).AddTranslateOp().Set((0.0, 3.0, 0.0))
    shot.Save()

    network = str(tmp_path / "cfx_scene.usda")
    with Scene() as scene:
        subs = scene.create.sublayer(name="shot_in")
        rows = subs.get_param("imports", chunk.is_inst(chunk.ListGroup))
        rows.size = 1
        rows.get_indexed_param("sublayer_path", 0, chunk.is_param_t(str)).value = str(tmp_path / "shot.usda")
        layer = scene.create.layer(subs, layer_name="cfx", insert_mode="Prepend")
        with scene.create.edit_target(layer, edit_target="cfx"):
            scene.create.rbd_solver(prim_path="/World/box", collision_path="/World/ground", frame_count=12)
        scene.save(network)

    with Scene.open(network) as scene:
        box_prim = scene.get_stage().GetPrimAtPath("/World/box")
        assert any(a.GetNumTimeSamples() for a in box_prim.GetAttributes())
