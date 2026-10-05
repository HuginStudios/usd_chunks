"""A solver in a script (Scene.new(), host "script") bakes on cook, like `usdchunks run`.

The RBD and Particle Solvers counted only `usdchunks run` as headless. In a script they
took the interactive path: Auto Simulate off, Run waiting for a press nobody makes, so the
cook baked nothing and the scene stayed still. The build scripts had to call the Run
button's callback by hand.
"""

from __future__ import annotations

from usd_chunks import Scene, sim_bake


def test_a_script_counts_as_unattended() -> None:
    with Scene():
        assert sim_bake.is_unattended()


def test_rbd_bakes_on_cook_in_a_script() -> None:
    with Scene() as scene:
        ground = scene.create.cube(prim_path="/ground", width=10.0, height=0.2, depth=10.0, name="ground_mesh")
        box = scene.create.cube(ground, prim_path="/box", name="box_mesh")
        lifted = scene.create.transform(box, primpath_location="/box", ty=3.0, name="lift")
        rbd = scene.create.rbd_solver(lifted, prim_path="/box", collision_path="/ground", frame_count=12)

        assert rbd.get_param("auto_sim").value, "Auto Simulate starts on where nobody can press Run"
        box_prim = scene.get_stage().GetPrimAtPath("/box")
        assert any(a.GetNumTimeSamples() for a in box_prim.GetAttributes()), "baked without pressing Run"
