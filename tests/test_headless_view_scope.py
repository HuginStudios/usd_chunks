"""Headless cooks what the active view reaches, like the UI does.

`load_chunks` executes every dependency-free chunk. In the UI the view recook then mutes
whatever the active view cannot reach; headless simply never ran that step, so `run`
composed every network in the file and produced a superset of what the graph showed -- a
file with two experiments in it exported both.

One execution model, not two. A file with no view falls back to cooking everything, because
silently producing nothing for every existing recipe would be worse than the inconsistency
this fixes, and `--all` forces that fallback.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pxr import Sdf, Usd, UsdGeom

from usd_chunks import chunk_registry
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
def two_branches(tmp_path):
    """A viewed branch that moves /a, and a dangling one that moves /b with no view."""
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

    def wire(dst, src):
        prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(dst)))
        attr = prim.GetAttribute(_EXEC_INPUT) or prim.CreateAttribute(
            _EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False
        )
        attr.SetConnections([Sdf.Path(f"{get_chunk_prim_path(src)}.{_EXEC_OUTPUT}")])

    viewed = make("transform", "Viewed")
    viewed.get_param("primpath_location").value = "/a"
    viewed.get_param("tz").value = 5.0
    viewed.execute()

    dangling = make("transform", "Dangling")
    dangling.get_param("primpath_location").value = "/b"
    dangling.get_param("tz").value = 7.0
    dangling.execute()

    view = make("view", "View")
    wire(view, viewed)
    view_prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(view)))
    view_prim.CreateAttribute("view:slot", Sdf.ValueTypeNames.Int).Set(1)

    export_chunks(scene, str(out))
    exported = Usd.Stage.Open(str(out))
    UsdGeom.Cube.Define(exported, "/a")
    UsdGeom.Cube.Define(exported, "/b")
    exported.GetRootLayer().Save()
    return out


def _run(network, out, *extra):
    cmd = [sys.executable, "-m", "usd_chunks.cli", "run",
           str(network), "--export", str(out), *extra]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO))


def _height(exported, prim_path):
    stage = Usd.Stage.Open(str(exported))
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        return None
    matrix = UsdGeom.Xformable(prim).GetLocalTransformation(Usd.TimeCode.Default())
    matrix = matrix[0] if isinstance(matrix, tuple) else matrix
    return matrix.ExtractTranslation()[2]


def test_a_dangling_network_does_not_reach_the_export(two_branches, tmp_path):
    """The behaviour change: headless used to compose both branches."""
    out = tmp_path / "result.usda"
    result = _run(two_branches, out)
    assert result.returncode == 0, result.stdout + result.stderr

    assert _height(out, "/a") == pytest.approx(5.0), "the viewed branch should have cooked"
    assert _height(out, "/b") in (None, pytest.approx(0.0)), (
        "the dangling branch reached the export -- headless is still cooking everything"
    )


def test_all_restores_the_old_behaviour(two_branches, tmp_path):
    """For a file whose side branches are meant to run."""
    out = tmp_path / "result.usda"
    assert _run(two_branches, out, "--all").returncode == 0

    assert _height(out, "/a") == pytest.approx(5.0)
    assert _height(out, "/b") == pytest.approx(7.0)


def test_a_file_with_no_view_still_cooks_everything(tmp_path):
    """The fallback. Silently producing nothing for every existing recipe would be worse
    than the inconsistency this fixes."""
    out_net = tmp_path / "noview.usda"
    stage = Usd.Stage.CreateInMemory()
    scene = _Scene(stage)
    registry = chunk_registry.ChunkRegistry()
    registry.initialize(scene, "pytest")
    init_chunk_usd(stage, scene)

    mod = registry.get_chunk_module("transform")
    chunks = mod.create() if mod.loaded else mod.load()
    node = chunks[0]
    node.name = "Lonely"
    for c in chunks:
        scene.add_chunk(c)
    node.get_param("primpath_location").value = "/a"
    node.get_param("tz").value = 3.0
    node.execute()

    export_chunks(scene, str(out_net))
    exported = Usd.Stage.Open(str(out_net))
    UsdGeom.Cube.Define(exported, "/a")
    exported.GetRootLayer().Save()

    out = tmp_path / "result.usda"
    assert _run(out_net, out).returncode == 0
    assert _height(out, "/a") == pytest.approx(3.0)
