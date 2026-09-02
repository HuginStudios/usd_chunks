"""Deleting inside a chunkgroup must not delete the chunkgroup.

_on_chunk_deleted tidies up the chunk root once its last chunk goes -- an empty
/chunks prim is bookkeeping nobody wants left behind. But a chunk inside a group has
the GROUP as its root, so emptying a group deleted the group's own prim with it.

That surfaced as a wiring bug. With prim -> group -> view, deleting the group deletes
its contents first; the last of those took the group's prim down early, so by the time
the group's own delete ran heal_exec_chain_on_delete found no prim, bailed at its
IsValid() guard, and never repointed the view. The view kept a connection to a prim
that no longer existed instead of falling back to prim -> view.
"""
from __future__ import annotations

import weakref

import pytest
from pxr import Sdf, Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins.chunk_usd import (
    _EXEC_INPUT,
    _EXEC_OUTPUT,
    connect_exec,
    get_chunk_prim_path,
    initialize as _chunk_usd_init,
)


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


def _exec_inputs(stage, chunk):
    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(chunk)))
    if not (prim and prim.IsValid()):
        return None
    attr = prim.GetAttribute(_EXEC_INPUT)
    return [str(c) for c in attr.GetConnections()] if (attr and attr.IsValid()) else []


def test_emptying_a_chunkgroup_leaves_the_group_standing(scene):
    qapp, reg, s, stage = scene
    group = _mk(reg, s, "chunkgroup")
    s._current_context = weakref.ref(group)
    inner = _mk(reg, s, "transform")
    s._current_context = None
    qapp.processEvents()

    group_path = Sdf.Path(get_chunk_prim_path(group))
    assert stage.GetPrimAtPath(group_path).IsValid()

    inner.request_delete()
    qapp.processEvents()

    assert stage.GetPrimAtPath(group_path).IsValid(), (
        "deleting the last chunk inside a group must not delete the group itself -- "
        "an empty group is still a group"
    )


def test_deleting_a_wired_chunkgroup_heals_the_chain(scene):
    """prim -> group -> view, minus the group, is prim -> view."""
    qapp, reg, s, stage = scene
    upstream = _mk(reg, s, "prim")
    group = _mk(reg, s, "chunkgroup")
    s._current_context = weakref.ref(group)
    _mk(reg, s, "transform")          # contents: what used to break the heal
    s._current_context = None
    view = _mk(reg, s, "view")
    qapp.processEvents()

    connect_exec(stage, reg.scene, get_chunk_prim_path(upstream), get_chunk_prim_path(group))
    connect_exec(stage, reg.scene, get_chunk_prim_path(group), get_chunk_prim_path(view))
    qapp.processEvents()

    upstream_out = f"{get_chunk_prim_path(upstream)}.{_EXEC_OUTPUT}"
    assert _exec_inputs(stage, view) == [f"{get_chunk_prim_path(group)}.{_EXEC_OUTPUT}"]

    group.request_delete()
    qapp.processEvents()

    assert _exec_inputs(stage, view) == [upstream_out], (
        "the view must fall back to what fed the group, not keep a connection to a prim "
        "that no longer exists"
    )


def test_an_empty_chunkgroup_can_still_be_deleted(scene):
    """The tidy-up this changes must still happen for the group itself."""
    qapp, reg, s, stage = scene
    group = _mk(reg, s, "chunkgroup")
    qapp.processEvents()
    group_path = Sdf.Path(get_chunk_prim_path(group))

    group.request_delete()
    qapp.processEvents()

    assert not stage.GetPrimAtPath(group_path).IsValid()


# Disable / enable


def test_re_enabling_a_chunkgroup_brings_its_contents_back(scene):
    """Reported live: disable a chunkgroup, enable it again, and the chunks inside stay
    dead until you dive in and select them.

    Deactivating the group's prim deactivates its descendants too -- that is inherited in
    USD, nothing is authored on the children. But the stage->chunk mirror read the COMPOSED
    prim.IsActive(), so every inner chunk latched a DISABLED state of its own. Re-enabling
    the group reactivated the group's prim, and each inner chunk promptly wrote its own
    active=false straight back from that latched state.
    """
    qapp, reg, s, stage = scene
    group = _mk(reg, s, "chunkgroup")
    s._current_context = weakref.ref(group)
    inner = _mk(reg, s, "transform")
    s._current_context = None
    qapp.processEvents()

    from usd_chunks.chunk_flags import ChunkFlags

    group.disable()
    qapp.processEvents()
    group.enable()
    qapp.processEvents()

    assert not inner.has_state(ChunkFlags.State.DISABLED), (
        "an inner chunk must not latch a disabled state it never had -- it was only ever "
        "inactive because its ancestor was"
    )
    inner_prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(inner)))
    assert inner_prim.IsActive(), "and it must be live again once the group is"


def test_a_chunk_disabled_on_its_own_stays_disabled(scene):
    """The mirror still has to work for a prim genuinely deactivated on the stage."""
    qapp, reg, s, stage = scene
    chunk = _mk(reg, s, "transform")
    qapp.processEvents()

    from usd_chunks.chunk_flags import ChunkFlags

    stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(chunk))).SetActive(False)
    qapp.processEvents()

    assert chunk.has_state(ChunkFlags.State.DISABLED)
