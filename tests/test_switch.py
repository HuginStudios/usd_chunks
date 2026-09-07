"""A Switch passes one upstream branch and mutes the rest.

Merge is fan-in -- every branch contributes and slot order decides who wins. Switch is the
choice instead of the sum. It authors nothing itself; it suppresses the branches it did not
pick by putting their chunks into DISABLED, which is already what mutes a chunk's layer, so
their opinions simply stop reaching the composed stage.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from pxr import Sdf, Usd, UsdGeom

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import ChunkConfig, SceneSender
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_registry import ChunkLayerManager
from usd_chunks.plugins import chunk_usd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
switch_mod = importlib.import_module("switch")
transform_mod = importlib.import_module("transform")


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture
def scene():
    stage = Usd.Stage.CreateInMemory()
    layer = Sdf.Layer.CreateAnonymous("scene")
    stage.GetRootLayer().subLayerPaths.append(layer.identifier)
    with Usd.EditContext(stage, layer):
        UsdGeom.Cube.Define(stage, "/a")
        UsdGeom.Cube.Define(stage, "/b")

    sender = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(sender, "pytest")
    ChunkLayerManager().initialize(stage)
    chunk_usd.ChunkStageSync().initialize(stage, sender)
    return stage, sender


def _mk(sender, module, name, config_module):
    node = module.main()
    node._config = ChunkConfig(module=config_module)
    node._module = module
    node.name = name
    sender.add_chunk(node)
    return node


def _place(sender, name, prim_path, height):
    node = _mk(sender, transform_mod, name, "transform")
    node.get_param("primpath_location").value = prim_path
    node.get_param("tz").value = height
    node.execute()
    return node


def _wire(stage, dst, *sources):
    prim = stage.GetPrimAtPath(Sdf.Path(chunk_usd.get_chunk_prim_path(dst)))
    attr = prim.GetAttribute(chunk_usd._EXEC_INPUT) or prim.CreateAttribute(
        chunk_usd._EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False
    )
    attr.SetConnections([
        Sdf.Path(f"{chunk_usd.get_chunk_prim_path(s)}.{chunk_usd._EXEC_OUTPUT}")
        for s in sources
    ])


def _height(stage, prim_path):
    """The composed z translation, or None when the branch that authored it is muted."""
    prim = stage.GetPrimAtPath(prim_path)
    matrix = UsdGeom.Xformable(prim).GetLocalTransformation(Usd.TimeCode.Default())
    matrix = matrix[0] if isinstance(matrix, tuple) else matrix
    return matrix.ExtractTranslation()[2]


def test_only_the_selected_branch_reaches_the_stage(scene):
    stage, sender = scene
    branch_a = _place(sender, "A", "/a", 5.0)
    branch_b = _place(sender, "B", "/b", 7.0)

    sw = _mk(sender, switch_mod, "Switch", "switch")
    _wire(stage, sw, branch_a, branch_b)
    sw.get_param("index").value = 0
    sw.execute()

    assert _height(stage, "/a") == pytest.approx(5.0)
    assert _height(stage, "/b") == pytest.approx(0.0), "branch B should be muted"
    assert branch_b.has_state(ChunkFlags.State.DISABLED)
    assert not branch_a.has_state(ChunkFlags.State.DISABLED)


def test_flipping_the_index_swaps_which_branch_is_live(scene):
    stage, sender = scene
    branch_a = _place(sender, "A", "/a", 5.0)
    branch_b = _place(sender, "B", "/b", 7.0)

    sw = _mk(sender, switch_mod, "Switch", "switch")
    _wire(stage, sw, branch_a, branch_b)
    sw.get_param("index").value = 0
    sw.execute()
    sw.get_param("index").value = 1
    sw.execute()

    assert _height(stage, "/b") == pytest.approx(7.0)
    assert _height(stage, "/a") == pytest.approx(0.0)
    assert branch_a.has_state(ChunkFlags.State.DISABLED)
    assert not branch_b.has_state(ChunkFlags.State.DISABLED)


def test_it_mutes_the_whole_branch_not_just_the_slot(scene):
    """Suppressing only the chunk plugged into the slot would leave the rest of that branch
    still authoring into the composed stage."""
    stage, sender = scene
    upstream = _place(sender, "Upstream", "/b", 7.0)
    tail = _mk(sender, transform_mod, "Tail", "transform")
    tail.get_param("primpath_location").value = "/b"
    tail.get_param("tx").value = 3.0
    _wire(stage, tail, upstream)
    tail.execute()

    live = _place(sender, "Live", "/a", 5.0)

    sw = _mk(sender, switch_mod, "Switch", "switch")
    _wire(stage, sw, live, tail)
    sw.get_param("index").value = 0
    sw.execute()

    assert upstream.has_state(ChunkFlags.State.DISABLED), "the branch's interior stayed live"
    assert tail.has_state(ChunkFlags.State.DISABLED)


def test_a_chunk_feeding_both_branches_stays_live(scene):
    """A diamond: the shared source is upstream of the muted branch *and* the chosen one,
    so muting it would break the branch that was selected."""
    stage, sender = scene
    shared = _place(sender, "Shared", "/a", 5.0)

    kept = _mk(sender, transform_mod, "Kept", "transform")
    kept.get_param("primpath_location").value = "/a"
    _wire(stage, kept, shared)
    kept.execute()

    dropped = _mk(sender, transform_mod, "Dropped", "transform")
    dropped.get_param("primpath_location").value = "/b"
    dropped.get_param("tz").value = 7.0
    _wire(stage, dropped, shared)
    dropped.execute()

    sw = _mk(sender, switch_mod, "Switch", "switch")
    _wire(stage, sw, kept, dropped)
    sw.get_param("index").value = 0
    sw.execute()

    assert not shared.has_state(ChunkFlags.State.DISABLED)
    assert dropped.has_state(ChunkFlags.State.DISABLED)
    assert _height(stage, "/a") == pytest.approx(5.0)


def test_a_hand_disabled_chunk_is_not_re_enabled(scene):
    """The switch only un-mutes what it muted. Flipping the index must not silently undo a
    DISABLED the user set themselves."""
    stage, sender = scene
    branch_a = _place(sender, "A", "/a", 5.0)
    branch_b = _place(sender, "B", "/b", 7.0)
    branch_a.set_state(ChunkFlags.State.DISABLED)

    sw = _mk(sender, switch_mod, "Switch", "switch")
    _wire(stage, sw, branch_a, branch_b)
    sw.get_param("index").value = 0
    sw.execute()

    assert branch_a.has_state(ChunkFlags.State.DISABLED), "the user's own disable was cleared"


def test_an_index_past_the_last_slot_clamps(scene):
    """Out of range picks the last connected branch rather than muting everything."""
    stage, sender = scene
    branch_a = _place(sender, "A", "/a", 5.0)
    branch_b = _place(sender, "B", "/b", 7.0)

    sw = _mk(sender, switch_mod, "Switch", "switch")
    _wire(stage, sw, branch_a, branch_b)
    sw.get_param("index").value = 99
    sw.execute()

    assert not branch_b.has_state(ChunkFlags.State.DISABLED)
    assert branch_a.has_state(ChunkFlags.State.DISABLED)


def test_a_switch_with_nothing_wired_does_nothing(scene):
    stage, sender = scene
    sw = _mk(sender, switch_mod, "Switch", "switch")
    sw.execute()  # must not raise
