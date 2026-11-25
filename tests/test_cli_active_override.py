"""`usdchunks run net.usda Chunk.active=false` leaves the chunk out, as D does in the graph.

A disabled chunk is saved as active=false on its prim, so that is the override people try
for a one-off "without this step" cook. It printed "Param not found" and cooked the chunk.
"""
from __future__ import annotations

import sys

import pytest
from pxr import Usd, UsdGeom

from usd_chunks import cli
from usd_chunks.chunk_script import Scene


@pytest.fixture
def network(tmp_path):
    path = tmp_path / "net.usda"
    scene = Scene.new(path=str(path))
    cube = scene.add("mesh", name="Box", mesh_type="cube", prim_path="/cube")
    scene.add("transform", cube, name="Lift", primpath_location="/cube", ty=5.0)
    scene.save()
    scene.close()
    return path


def _run(monkeypatch, network, *overrides):
    out = network.with_name(f"out{len(list(network.parent.glob('out*')))}.usda")
    monkeypatch.setattr(sys, "argv", ["usdchunks", "run", str(network), *overrides, "-o", str(out)])
    cli.main()
    stage = Usd.Stage.Open(str(out))  # held: a prim from a stage nobody holds is invalid
    return UsdGeom.Xformable(stage.GetPrimAtPath("/cube")).GetLocalTransformation().ExtractTranslation()[1]


def test_active_false_leaves_the_chunk_out(monkeypatch, network, capsys):
    assert _run(monkeypatch, network) == pytest.approx(5.0)
    assert _run(monkeypatch, network, "Lift.active=false") == pytest.approx(0.0)
    assert "not found" not in capsys.readouterr().out


def test_active_true_brings_back_a_chunk_saved_disabled(monkeypatch, network):
    stage = Usd.Stage.Open(str(network))
    lift = next(p for p in stage.Traverse(Usd.PrimAllPrimsPredicate) if p.GetName() == "Lift")
    lift.SetActive(False)
    stage.GetRootLayer().Save()
    assert _run(monkeypatch, network) == pytest.approx(0.0)
    assert _run(monkeypatch, network, "Lift.active=true") == pytest.approx(5.0)


def test_a_word_that_isnt_true_or_false_says_so(monkeypatch, network, capsys):
    assert _run(monkeypatch, network, "Lift.active=maybe") == pytest.approx(5.0)
    assert "expected true or false" in capsys.readouterr().out
