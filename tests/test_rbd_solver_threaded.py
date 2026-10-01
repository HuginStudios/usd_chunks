"""In a UI, Run solves on a worker thread and writes the bake back on the main thread.

The single-prim, instancer and articulated paths used to step MuJoCo on the main thread, and
usdview stopped responding for the whole bake. They now read the scene, hand the stepping to
sim_bake's worker, and write on the main thread.
"""

from __future__ import annotations

import threading
import time

import pytest
from pxr import Usd, UsdGeom

import chunks.dynamics.rbd_solver as rbd_mod_file
from usd_chunks import chunk_registry, sim_bake
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import initialize as _chunk_usd_init

pytestmark = pytest.mark.skipif(not rbd_mod_file._HAS_MUJOCO, reason="needs mujoco")


class _StageScene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture
def solver(qapp, monkeypatch):
    monkeypatch.setattr(sim_bake, "in_background", lambda: True)
    stage = Usd.Stage.CreateInMemory()
    scene = _StageScene(stage)
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)
    mod = reg.get_chunk_module("rbd_solver")
    node = (mod.create() if mod.loaded else mod.load())[0]
    scene.add_chunk(node)
    code = node.get_chunk_module().module  # the copy execute() and _run() live in
    stepped_on = []
    real_wind = code._apply_wind

    def wind(*args, **kwargs):
        stepped_on.append(threading.current_thread() is threading.main_thread())
        return real_wind(*args, **kwargs)

    monkeypatch.setattr(code, "_apply_wind", wind)
    return qapp, stage, node, code, stepped_on


def _run_and_wait(qapp, node, code):
    code._run(node, None, None)
    assert sim_bake.is_running(node), "Run returns while the solve goes on"
    started = time.perf_counter()
    while sim_bake.is_running(node) and time.perf_counter() - started < 60:
        qapp.processEvents()
        time.sleep(0.01)
    qapp.processEvents()


def test_a_single_prim_solves_on_a_worker_and_is_baked(solver):
    qapp, stage, node, code, stepped_on = solver
    cube = UsdGeom.Cube.Define(stage, "/Box")
    cube.AddTranslateOp().Set((0.0, 50.0, 0.0))
    node.get_param("prim_path").value = "/Box"
    node.get_param("frame_count").value = 6

    _run_and_wait(qapp, node, code)

    assert stepped_on and not any(stepped_on), "MuJoCo stepped on the worker only"
    op = [a for a in stage.GetPrimAtPath("/Box").GetAttributes() if a.GetName().startswith("xformOp:transform")]
    assert op and op[0].GetNumTimeSamples() == 6
    assert node.get_param("readout").value == "baked 6 frames on Box"


def test_an_instancer_solves_on_a_worker_and_is_baked(solver):
    qapp, stage, node, code, stepped_on = solver
    UsdGeom.Cube.Define(stage, "/Protos/Box")
    inst = UsdGeom.PointInstancer.Define(stage, "/Stack")
    inst.CreatePrototypesRel().SetTargets(["/Protos/Box"])
    inst.CreateProtoIndicesAttr([0, 0, 0])
    inst.CreatePositionsAttr([(0, 10, 0), (0, 13, 0), (0, 16, 0)])
    node.get_param("prim_path").value = "/Stack"
    node.get_param("frame_count").value = 5

    _run_and_wait(qapp, node, code)

    assert stepped_on and not any(stepped_on)
    assert stage.GetPrimAtPath("/Stack").GetAttribute("positions").GetNumTimeSamples() == 5


def test_cancel_leaves_no_half_bake(solver):
    qapp, stage, node, code, stepped_on = solver
    UsdGeom.Cube.Define(stage, "/Box").AddTranslateOp().Set((0.0, 50.0, 0.0))
    node.get_param("prim_path").value = "/Box"
    node.get_param("frame_count").value = 2000
    code._run(node, None, None)
    sim_bake.cancel(node)
    started = time.perf_counter()
    while sim_bake.is_running(node) and time.perf_counter() - started < 60:
        qapp.processEvents()
        time.sleep(0.01)
    assert node.get_param("readout").value == "cancelled"
    op = [a for a in stage.GetPrimAtPath("/Box").GetAttributes() if a.GetName().startswith("xformOp:transform")]
    assert not op or op[0].GetNumTimeSamples() == 0


def _grid(stage, path, n):
    """A flat n x n quad grid: 2 n^2 triangles."""
    mesh = UsdGeom.Mesh.Define(stage, path)
    pts = [(float(i), 0.0, float(j)) for j in range(n + 1) for i in range(n + 1)]
    faces = []
    for j in range(n):
        for i in range(n):
            a = j * (n + 1) + i
            faces += [a, a + 1, a + n + 2, a + n + 1]
    mesh.CreatePointsAttr(pts)
    mesh.CreateFaceVertexCountsAttr([4] * (n * n))
    mesh.CreateFaceVertexIndicesAttr(faces)
    return mesh.GetPrim()


def test_a_mesh_collider_over_the_limit_is_collided_as_a_heightfield(monkeypatch):
    """Falling_rocks_2: a 225k-triangle rock cluster set to Mesh hung usdview for hours."""
    code = rbd_mod_file
    monkeypatch.setattr(code, "_MESH_TRIANGLE_LIMIT", 100)
    stage = Usd.Stage.CreateInMemory()
    small, big = _grid(stage, "/Small", 5), _grid(stage, "/Big", 10)  # 50 and 200 triangles

    assert code._collider_settings([small], "Mesh", 500_000) == ("Mesh", 100, "")
    mode, budget, note = code._collider_settings([small, big], "Mesh", 500_000)
    assert (mode, budget) == ("Heightfield", 100)
    assert "200 triangles" in note and "Heightfield" in note
    assert code._collider_settings([big], "Auto", 500_000) == ("Auto", 100, ""), "Auto keeps its own fallback"
