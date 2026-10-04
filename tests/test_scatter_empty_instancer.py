"""An instancer with nothing placed must still be well-formed.

Reported as a hard usdview crash: drop a Scatter Brush and a Scatter Points, connect
the brush's painted points into the scatter's Points input, and usdview dies. It takes
a USD 26.05 build to crash; 25.08 tolerates it, which is why it only showed up on the
newer bundle.

Nothing is placed at that moment -- no surface mesh is set yet -- and scatter only
authored its arrays `if count`. So the PointInstancer was left declaring a prototype
while carrying no protoIndices and no positions at all. USD warns about it
("no prototype indices", pointInstancer.cpp) and Hydra then draws an instancer whose
prototypes it cannot index into.

Asserted here as the authoring invariant rather than by reproducing the crash: a test
that segfaults takes the runner with it, and the invariant is the thing that has to
hold anyway.
"""
from __future__ import annotations

import pytest
from pxr import Usd, UsdGeom

from usd_chunks import chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import get_chunk_prim_path, initialize as _chunk_usd_init


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


@pytest.fixture
def scene(qapp):
    stage = Usd.Stage.CreateInMemory()

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    s = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(s, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, s)
    return qapp, reg, s, stage


def _mk(reg, scene, key):
    mod = reg.get_chunk_module(key)
    cs = mod.create() if mod.loaded else mod.load()
    for c in cs:
        scene.add_chunk(c)
    return cs[0]


def _instancers(stage):
    return [
        UsdGeom.PointInstancer(p)
        for p in stage.TraverseAll()
        if p.GetTypeName() == "PointInstancer"
    ]


def _assert_well_formed(instancer):
    """Either it instances something, or it is explicitly empty. Never half-declared."""
    path = instancer.GetPrim().GetPath()
    targets = instancer.GetPrototypesRel().GetTargets()
    indices = instancer.GetProtoIndicesAttr()
    positions = instancer.GetPositionsAttr()

    if not targets:
        return  # nothing declared at all is coherent; Hydra has nothing to index

    assert indices.HasAuthoredValue(), (
        f"{path} declares {len(targets)} prototype(s) but authors no protoIndices -- "
        "Hydra cannot index into prototypes that nothing points at"
    )
    assert positions.HasAuthoredValue(), f"{path} authors protoIndices but no positions"
    assert len(indices.Get()) == len(positions.Get()), (
        f"{path} has {len(indices.Get())} indices for {len(positions.Get())} positions"
    )


def test_scatter_with_nothing_placed_leaves_a_well_formed_instancer(scene):
    """The reported case: painted points wired in, no surface mesh, nothing placed."""
    qapp, reg, s, stage = scene
    brush = _mk(reg, s, "scatter_brush")
    points = _mk(reg, s, "scatter_points")
    qapp.processEvents()

    published = brush.get_param("points_path").value
    points.get_param("points_prim").value = (
        published or f"{get_chunk_prim_path(brush)}/PaintedPoints"
    )
    points.recook()
    qapp.processEvents()

    found = _instancers(stage)
    assert found, "the scatter should still author its instancer"
    for instancer in found:
        _assert_well_formed(instancer)


def test_a_normal_scatter_is_still_well_formed(scene):
    """The invariant must hold when it does place something, too."""
    qapp, reg, s, stage = scene
    points = _mk(reg, s, "scatter_points")
    points.recook()
    qapp.processEvents()

    for instancer in _instancers(stage):
        _assert_well_formed(instancer)
