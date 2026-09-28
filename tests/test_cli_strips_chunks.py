"""A delivered file carries the result, and nothing that says how it was made.

`run --export` and `--printout` already dropped every Chunk-typed prim, but the scope
those prims hung under is authored separately and survived the sweep -- so a "plain USD"
export still opened with `def Scope "chunks" {}` at the top. Empty, inert, and still the
one line telling anyone reading the file that it wants a plugin they do not have.

`--procedural` is the opposite promise (the network reloads and re-cooks), so there the
scope has to stay.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pxr import Sdf, Usd, UsdGeom

from usd_chunks import chunk_registry
from usd_chunks.chunk_naming import CHUNKS_ROOT, CHUNK_PRIM_TYPENAME
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import (
    _EXEC_INPUT,
    _EXEC_OUTPUT,
    export_chunks,
    get_chunk_prim_path,
    initialize as init_chunk_usd,
)

REPO = Path(__file__).resolve().parents[1]


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture
def network(tmp_path):
    """One viewed transform moving /a, saved as a network file the CLI can run."""
    out = tmp_path / "net.usda"
    stage = Usd.Stage.CreateInMemory()
    scene = _Scene(stage)
    registry = chunk_registry.ChunkRegistry()
    registry.initialize(scene, "pytest")
    init_chunk_usd(stage, scene)

    def make(module_name, node_name):
        mod = registry.get_chunk_module(module_name)
        chunks = mod.create() if mod.loaded else mod.load()
        node = chunks[0]
        node.name = node_name
        for c in chunks:
            scene.add_chunk(c)
        return node

    move = make("transform", "Move")
    move.get_param("primpath_location").value = "/a"
    move.get_param("tz").value = 5.0
    move.execute()

    view = make("view", "View")
    view_prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(view)))
    attr = view_prim.GetAttribute(_EXEC_INPUT) or view_prim.CreateAttribute(
        _EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False
    )
    attr.SetConnections([Sdf.Path(f"{get_chunk_prim_path(move)}.{_EXEC_OUTPUT}")])
    view_prim.CreateAttribute("view:slot", Sdf.ValueTypeNames.Int).Set(1)

    export_chunks(scene, str(out))
    exported = Usd.Stage.Open(str(out))
    UsdGeom.Cube.Define(exported, "/a")
    exported.GetRootLayer().Save()
    return out


def _run(*args):
    cmd = [sys.executable, "-m", "usd_chunks.cli", "run", *args]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO))
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def test_export_leaves_no_chunks_scope(network, tmp_path):
    out = tmp_path / "result.usda"
    _run(str(network), "--export", str(out))

    stage = Usd.Stage.Open(str(out))
    assert stage.GetPrimAtPath("/a"), "the cooked result should be in the export"
    assert not [p for p in stage.TraverseAll() if p.GetTypeName() == CHUNK_PRIM_TYPENAME]
    assert not stage.GetPrimAtPath(Sdf.Path(CHUNKS_ROOT)), "the /chunks scope survived"


def test_printout_leaves_no_chunks_scope(network):
    result = _run(str(network), "--printout")
    assert "def Cube" in result.stdout
    assert "chunks" not in result.stdout


def test_procedural_export_keeps_the_scope(network, tmp_path):
    """The recipe is the point of --procedural, and the scope is part of it."""
    out = tmp_path / "recipe.usda"
    _run(str(network), "--export", str(out), "--procedural")

    stage = Usd.Stage.Open(str(out))
    root = stage.GetPrimAtPath(Sdf.Path(CHUNKS_ROOT))
    assert root and root.IsValid(), "--procedural dropped the scope its chunks need"
    assert [p for p in stage.TraverseAll() if p.GetTypeName() == CHUNK_PRIM_TYPENAME]
