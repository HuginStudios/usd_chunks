from __future__ import annotations

import importlib

from pxr import Sdf, Usd, UsdGeom

from plugins.chunk_usd import (
    _EXEC_INPUT,
    _EXEC_OUTPUT,
    get_chunk_prim_path,
    initialize as _chunk_usd_init,
)
from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _mk(mod, scene):
    cs = mod.create() if mod.loaded else mod.load()
    for c in cs:
        scene.add_chunk(c)
    return cs[0]


def _wire(dst, src, stage):
    p = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(dst)))
    a = p.GetAttribute(_EXEC_INPUT) or p.CreateAttribute(_EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False)
    a.SetConnections([Sdf.Path(f"{get_chunk_prim_path(src)}.{_EXEC_OUTPUT}")])
    dst.add_dependency(src)


def test_rbd_run_does_not_reexecute_upstream_chunk() -> None:
    """Regression reported live in usdview: with Scatter -> RBD Solver wired, pressing "Run
    Simulation" crashed with USD's "Layer ... is not in the local LayerStack" -- the same
    nested-solve-cascade crash class Scatter's old exec_with_hidden_view-based fix could hit,
    because recook_for_view's own re-execution of RBD's upstream (Scatter) could reach Scatter
    reactively while already inside another chunk's dispatch. It also re-ran Scatter's
    placement (visible as a stray "[ScatterPoints] Scattered ..." print) as a side effect of
    establishing RBD's "hidden view" -- surprising, since the user only pressed RBD's button.

    RBD's _run now resolves its target prim through get_upstream_stage() instead (see
    scatter_points.py's execute() for the same fix) -- a pure read-only composition of
    already-existing layers, so pressing "Run Simulation" must not execute any other chunk at
    all (no crash risk, and no more surprise re-scatters).
    """
    import scripts.rbd_solver as rbd_mod_file

    if not rbd_mod_file._HAS_MUJOCO:
        import pytest

        pytest.skip("mujoco not installed in this environment")

    stage = Usd.Stage.CreateInMemory()

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    lm = chunk_registry.ChunkLayerManager()
    lm.initialize(stage)
    _chunk_usd_init(stage, scene)

    scatter_mod = reg.get_chunk_module("scatter_points")
    rbd_mod = reg.get_chunk_module("rbd_solver")

    mesh = UsdGeom.Mesh.Define(stage, "/apple")
    mesh.GetPointsAttr().Set([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
    mesh.GetFaceVertexCountsAttr().Set([3])
    mesh.GetFaceVertexIndicesAttr().Set([0, 1, 2])

    scatter = _mk(scatter_mod, scene)
    rbd = _mk(rbd_mod, scene)
    _wire(rbd, scatter, stage)

    scatter.get_param("surface_prim").value = "/apple"
    scatter.get_param("max_count").value = 2

    rbd.get_param("prim_path").value = "/ScatterInstancer"
    rbd.get_param("frame_count").value = 2  # keep the test fast

    real_scatter_module = reg.get_chunk_module("scatter_points").module
    real_rbd_module = reg.get_chunk_module("rbd_solver").module

    call_count = {"n": 0}
    orig_execute = real_scatter_module.execute

    def traced_execute(node):
        call_count["n"] += 1
        return orig_execute(node)

    real_scatter_module.execute = traced_execute
    call_count["n"] = 0  # reset after the reactive scatter runs triggered by the setup above

    # This must not raise (the reported crash) and must not touch scatter at all.
    run_btn = rbd.get_param("run_sim")
    real_rbd_module._run(run_btn, None, run_btn)

    assert call_count["n"] == 0, (
        "pressing Run Simulation must not re-execute an upstream chunk like Scatter as a "
        "side effect -- get_upstream_stage is a pure read, not a re-execution"
    )

    # Regression: the baked animation must land on the REAL stage/layer, not vanish onto a
    # throwaway scratch stage discarded when _run() returns.
    positions_attr = stage.GetPrimAtPath("/ScatterInstancer").GetAttribute("positions")
    assert positions_attr.GetNumTimeSamples() > 0, (
        "the simulation must bake real time samples onto the actual stage, not a discarded "
        "scratch stage"
    )
