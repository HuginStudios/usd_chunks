"""Duplicating a chunk gives the copy its own prim path.

A copy carries the original's param values verbatim, so without this both chunks author at
the same path and the copy silently overrides the original instead of producing a second
prim. Duplicating a ChunkGroup holding a Duplicate Prim gave you a second graph and one
flower pot.

Paste is the one moment this is knowable. A cook cannot tell its own past output from a
stranger's prim -- saving bakes the result into the file, so on reopen they look identical
-- which is why the cook no longer tries and why this lives here instead.

Only PRIM_TARGET params move. A path a chunk READS has to keep pointing where it pointed:
bumping one aims the copy at a prim that does not exist. The flag exists because the name
cannot be trusted -- prim_path is a read target in ~79 places and a write target in
mtlx_shader, and preview_surface uses primpath_location for the opposite of what the other
twelve chunks use it for.
"""
from __future__ import annotations

import pytest
from pxr import Usd, UsdGeom

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.plugins import chunk_usd
from usd_chunks.plugins.chunk_qt_nodegraph import _unique_prim_targets


class _Scene(SceneSender):
    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture
def scene(qapp):
    stage = Usd.Stage.CreateInMemory()
    s = _Scene()
    s._stage = stage
    chunk_registry.ChunkRegistry().initialize(s, "pytest")
    chunk_registry.ChunkLayerManager().reset()
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_usd.initialize(stage, s)
    return s, stage


def _mk(scene_obj, key):
    mod = chunk_registry.ChunkRegistry().get_chunk_module(key)
    cs = mod.create() if mod.loaded else mod.load()
    for c in cs:
        scene_obj.add_chunk(c)
    return cs[0]


def test_a_duplicated_prim_chunk_gets_its_own_path(scene, qapp):
    s, stage = scene
    original = _mk(s, "prim")
    qapp.processEvents()
    taken = original.get_param("primpath_location").value

    # What paste does: a fresh chunk carrying the original's values.
    copy = _mk(s, "prim")
    copy.get_param("primpath_location").value = taken
    qapp.processEvents()

    _unique_prim_targets(copy, stage)

    assert copy.get_param("primpath_location").value != taken, (
        f"the copy still points at {taken!r}; it would override the original rather than "
        "make a prim of its own"
    )


def test_the_duplicate_prim_chunks_destination_moves_too(scene, qapp):
    """dest_path, not primpath_location -- the reported case, and a different param name."""
    s, stage = scene
    source = "/Kitchen_set/Props_grp/FlowerPotA_5"
    UsdGeom.Xform.Define(stage, source)
    taken = source + "_copy1"
    UsdGeom.Xform.Define(stage, taken)

    dup = _mk(s, "core_usd.duplicate_prim")
    dup.get_param("dest_path").value = taken
    qapp.processEvents()

    _unique_prim_targets(dup, stage)

    assert dup.get_param("dest_path").value != taken, (
        "the duplicated group would write over the first group's flower pot"
    )


def test_a_path_a_chunk_reads_is_left_alone(scene, qapp):
    """The whole reason for the flag: a read target must keep pointing where it pointed.

    Transform names this primpath_location -- the same name twelve other chunks use for
    where they CREATE a prim. Here it is the prim being transformed.
    """
    s, stage = scene
    target = "/Kitchen_set/Props_grp/FlowerPotA_5"
    UsdGeom.Xform.Define(stage, target)

    tr = _mk(s, "transform")
    tr.get_param("primpath_location").value = target
    qapp.processEvents()

    _unique_prim_targets(tr, stage)

    assert tr.get_param("primpath_location").value == target, (
        "a Transform's target was moved to a prim that does not exist -- the copy would "
        "silently do nothing, which is the bug this flag exists to avoid"
    )


def test_preview_surfaces_bind_target_is_left_alone(scene, qapp):
    """Same param NAME as twelve write targets, opposite meaning: it binds to what is there."""
    s, stage = scene
    target = "/Kitchen_set/Props_grp/FlowerPotA_5"
    UsdGeom.Xform.Define(stage, target)

    ps = _mk(s, "preview_surface")
    ps.get_param("primpath_location").value = target
    qapp.processEvents()

    _unique_prim_targets(ps, stage)

    assert ps.get_param("primpath_location").value == target, (
        "Preview Surface's bind target moved; the copy would bind to nothing"
    )


def test_the_flag_marks_writes_not_reads():
    """Pinned: the two params that share a name but not a meaning."""
    reg = chunk_registry.ChunkRegistry()

    ps = reg.get_chunk_module("preview_surface")
    cs = ps.create() if ps.loaded else ps.load()
    surface = cs[0]
    assert surface.get_param("mat_path").has_flag(ChunkFlags.ParamFlags.PRIM_TARGET)
    assert not surface.get_param("primpath_location").has_flag(
        ChunkFlags.ParamFlags.PRIM_TARGET
    ), "preview_surface's primpath_location is a bind target, not a write target"
