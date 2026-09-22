"""Diving into a group must not mute the chain that group feeds.

Reported against a hosted layout: reference a saved network at /world/house/bedroom, wire
a Transform after it to place the room, and it sits where it should -- until you dive into
the node to edit it, at which point every asset in the room jumps back to the origin.
Coming out puts them back.

recook_focus mutes everything outside the focused group so the parent tab's active view
and unrelated sibling branches cannot bleed through, and it already keeps the group's
UPSTREAM composed (test_nested_group_focus_keeps_upstream). Its downstream was treated as
one of those unrelated branches -- but a chunk wired after the group is the same branch,
and it is what puts the group's result where it belongs.

Kept only as far as the outer view actually cooks it, so a second branch off the same
output does not start bleeding through on dive.
"""
from __future__ import annotations

import weakref

import pytest
from PySide6 import QtCore
from pxr import Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks import chunk_stage
from usd_chunks.plugins.chunk_qt import ChunkWidgetDocker
from usd_chunks.chunk_stage import (
    connect_exec, get_chunk_prim_path, initialize as _chunk_usd_init,
)


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


@pytest.fixture
def placed_group(qapp):
    """group -> after -> view, with a transform inside the group, and `aside` hanging off
    the group's output on a branch the view does not cook."""
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

    docker = ChunkWidgetDocker()
    docker.resize(600, 600)
    docker.show()
    qapp.processEvents()

    def settle():
        for _ in range(30):
            qapp.processEvents()
            QtCore.QThread.msleep(10)
        qapp.processEvents()

    def mk(key, context=None):
        mod = reg.get_chunk_module(key)
        cs = mod.create() if mod.loaded else mod.load()
        prev = scene._current_context
        scene._current_context = weakref.ref(context) if context is not None else None
        try:
            for c in cs:
                scene.add_chunk(c)
        finally:
            scene._current_context = prev
        return cs[0]

    group = mk("chunkgroup")
    mk("transform", context=group)
    after = mk("transform")
    aside = mk("transform")
    settle()
    view = next(c for c in reg.scene.get_chunks() if chunk_stage.is_view_chunk(c))
    connect_exec(stage, reg.scene, get_chunk_prim_path(group), get_chunk_prim_path(after))
    connect_exec(stage, reg.scene, get_chunk_prim_path(after), get_chunk_prim_path(view))
    connect_exec(stage, reg.scene, get_chunk_prim_path(group), get_chunk_prim_path(aside))
    settle()
    # Establish the outer view over the finished wiring. connect_exec is the engine call,
    # not the Qt edit path, so nothing recooks behind it -- without this the outer chain
    # has never cooked and "still cooking after the dive" would pass on a graph that was
    # not cooking before it either.
    root = reg.scene.get_root_context()
    chunk_stage.recook_focus(stage, reg.scene, root, view)
    assert not after.is_blocked(), "fixture: the outer view must be driving before the dive"

    return qapp, scene, stage, lm, docker, group, after, aside, settle


def _muted(stage, lm, chunk):
    layer = lm.get_layer(chunk)
    return layer is not None and layer.identifier in set(stage.GetMutedLayers())


def test_diving_in_keeps_what_the_group_feeds(placed_group):
    """The reported case: the node that places the group's result must keep cooking."""
    qapp, scene, stage, lm, docker, group, after, aside, settle = placed_group

    docker._on_dive_into(group)
    settle()

    assert not after.is_blocked(), (
        "the chunk wired after the group is what places its result -- blocking it on dive "
        "sends everything the group made back to the origin"
    )
    assert not _muted(stage, lm, after)


def test_a_branch_the_outer_view_does_not_cook_stays_muted(placed_group):
    """Respecting the outer view means respecting what it leaves out, too."""
    qapp, scene, stage, lm, docker, group, after, aside, settle = placed_group

    docker._on_dive_into(group)
    settle()

    assert aside.is_blocked(), (
        "a second branch off the group's output is not on screen outside; diving in must "
        "not start cooking it"
    )


def test_coming_back_out_still_works(placed_group):
    """Navigating in and out must leave the outer chain cooking, as it was."""
    qapp, scene, stage, lm, docker, group, after, aside, settle = placed_group

    docker._on_dive_into(group)
    settle()
    docker.tab_widget.setCurrentIndex(0)
    settle()

    assert not after.is_blocked()
    assert not _muted(stage, lm, after)
