"""`usdchunks sweep` end to end: a network in, N different variants out, all reproducible.

The unit tests in test_wedge.py pin the sampling. This pins the part that only breaks once
a file has been through a save and a reload -- which is where the first working version of
this failed, silently: runtime-lofted params do not survive an export, so the reloaded wedge
held only its controls and every variant of a sweep came out identical to variant 0, from a
file that looked intact on disk.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pxr import Sdf, Usd, UsdGeom

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins.chunk_usd import (
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
    """A wedge driving a transform, exported to a .usda the CLI can sweep."""
    out = tmp_path / "net.usda"
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
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

    wedge = make("wedge", "Wedge")
    wedge.get_param("seed").value = 11
    wedge.get_param("count").value = 4
    wedge.get_param("ranges").value = "height 1 9"

    place = make("transform", "Place")
    place.get_param("primpath_location").value = "/World/box"
    place.get_param("tz").value = "@Wedge.height@"

    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(place)))
    attr = prim.GetAttribute(_EXEC_INPUT) or prim.CreateAttribute(
        _EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False
    )
    attr.SetConnections([Sdf.Path(f"{get_chunk_prim_path(wedge)}.{_EXEC_OUTPUT}")])
    place.execute()

    export_chunks(scene, str(out))
    # The cube has to be in the exported file, not only in the in-memory root layer:
    # export_chunks writes the network, and a reloaded network whose target prim is absent
    # cooks without complaint and exports an empty stage.
    exported = Usd.Stage.Open(str(out))
    UsdGeom.Cube.Define(exported, "/World/box")
    exported.GetRootLayer().Save()
    return out


def _sweep(network, out, *extra):
    cmd = [sys.executable, "-m", "usd_chunks.plugins.usdchunks_cli", "sweep",
           str(network), "--out", str(out), *extra]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO))


def _tz(variant_usda):
    """The z translation the transform baked, read off the exported stage."""
    stage = Usd.Stage.Open(str(variant_usda))
    matrix = UsdGeom.Xformable(stage.GetPrimAtPath("/World/box")).GetLocalTransformation(
        Usd.TimeCode.Default()
    )
    matrix = matrix[0] if isinstance(matrix, tuple) else matrix
    return matrix.ExtractTranslation()[2]


def test_the_spec_survives_the_file_and_the_variants_differ(network, tmp_path):
    """The failure this exists to catch: a wedge that reloads holding nothing drives
    nothing, and every variant is a copy of variant 0."""
    out = tmp_path / "data"
    result = _sweep(network, out)
    assert result.returncode == 0, result.stdout + result.stderr

    heights = [_tz(out / f"variant_{i:05d}" / "variant.usda") for i in range(4)]
    assert len(set(heights)) == 4, f"variants did not differ: {heights}"
    assert all(1.0 <= h <= 9.0 for h in heights), heights


def test_one_variant_regenerates_alone_and_matches(network, tmp_path):
    """--only is the whole recipe claim in miniature: variant 3 without producing 0-2."""
    out = tmp_path / "data"
    assert _sweep(network, out).returncode == 0
    expected = _tz(out / "variant_00003" / "variant.usda")

    alone = tmp_path / "alone"
    assert _sweep(network, alone, "--only", "3").returncode == 0

    assert _tz(alone / "variant_00003" / "variant.usda") == expected


def test_verify_reports_every_variant_identical(network, tmp_path):
    out = tmp_path / "data"
    assert _sweep(network, out).returncode == 0

    result = _sweep(network, out, "--verify")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "identical : 4/4" in result.stdout


def test_verify_notices_a_variant_that_changed(network, tmp_path):
    """A verifier that cannot fail proves nothing, so tamper with one and check it says so."""
    out = tmp_path / "data"
    assert _sweep(network, out).returncode == 0

    tampered = out / "variant_00002" / "variant.usda"
    tampered.write_text(tampered.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")

    result = _sweep(network, out, "--verify")
    assert result.returncode == 1
    assert "variant_00002" in result.stdout
    assert "identical : 3/4" in result.stdout


def test_a_network_without_a_wedge_is_refused(tmp_path):
    plain = tmp_path / "plain.usda"
    # Held in a local: dropping the stage collects it, and the layer it hands back is
    # already invalid by the time Save() reaches it.
    stage = Usd.Stage.CreateNew(str(plain))
    stage.GetRootLayer().Save()

    result = _sweep(plain, tmp_path / "data")
    assert result.returncode == 1
    assert "No wedge chunk" in result.stdout + result.stderr
