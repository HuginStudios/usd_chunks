"""Deleting a chunk, and undoing that, keeps the chain above and below it working.

Reported in usdview: delete a Transform wired under a Reference, undo, and the Transform is
blocked ("isn't wired above") though the wire is drawn. The wire was back in USD but not in
the DAG. Deleting a middle chunk had the same gap for the chunk below it.
"""
from __future__ import annotations

import pytest
from pxr import Usd, UsdGeom

from usd_chunks import Scene, chunk_undo


@pytest.fixture
def chain(tmp_path):
    """Reference -> Transform1 -> Transform2, all on the referenced prim."""
    asset = tmp_path / "asset.usda"
    stage = Usd.Stage.CreateNew(str(asset))
    stage.SetDefaultPrim(UsdGeom.Cube.Define(stage, "/Cube").GetPrim())
    stage.Save()
    scene = Scene.new()
    ref = scene.create.reference(name="Reference", file_path=str(asset), primpath_location="/World/asset")
    t1 = scene.create.transform(ref, name="Transform1", primpath_location="@Reference.primpath_location@", ty=5.0)
    t2 = scene.create.transform(t1, name="Transform2", primpath_location="/World/asset", tx=3.0)
    scene.cook(t2)
    yield scene
    scene.close()


def _upstream(scene, name):
    return [dep.name for _, dep in scene.get_chunk(name).get_dependencies()]


def _ops(scene):
    prim = scene.get_stage().GetPrimAtPath("/World/asset")
    return [str(op) for op in UsdGeom.Xformable(prim).GetXformOpOrderAttr().Get() or []]


def test_deleting_a_middle_chunk_wires_the_one_below_to_the_one_above(chain):
    chain.remove_chunk("Transform1")
    assert _upstream(chain, "Transform2") == ["Reference"]
    chain.cook(chain.get_chunk("Transform2"))
    assert any("Transform2" in op for op in _ops(chain))


def test_undoing_a_delete_brings_back_the_wires_above_and_below(chain):
    undo = chunk_undo.UndoStack(chain.get_stage())
    undo.note_input()
    chain.remove_chunk("Transform1")
    assert undo.undo()
    assert _upstream(chain, "Transform1") == ["Reference"]
    assert _upstream(chain, "Transform2") == ["Transform1"]
    chain.cook(chain.get_chunk("Transform2"))
    ops = _ops(chain)
    assert any("Transform1" in op for op in ops) and any("Transform2" in op for op in ops)
