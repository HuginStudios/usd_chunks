"""Focusing a group two levels deep must not mute what feeds its parent.

Reported: group some chunks, dive in, group again, dive into that -- and the sublayer's
scene disappears from the viewport. Coming back out restores it, so content appears to
come and go depending on which level you are looking at.

recook_focus mutes everything outside the focused group so the parent tab's active view
and unrelated sibling branches cannot bleed through. It already keeps the focused group's
own upstream composed, because the group's inner chunks author on top of that content --
but it only asks the FOCUSED group what feeds it. A group nested inside another is fed by
its parent's chain, and everything feeding the parent fell outside that set.

Not caused by Ctrl+G: the same nesting built by hand behaves identically.
"""
from __future__ import annotations

import weakref

import pytest
from PySide6 import QtCore
from pxr import Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks import chunk_stage
from usd_chunks.frontends.qt.factory import ChunkWidgetDocker
from usd_chunks.chunk_stage import (
    connect_exec, get_chunk_prim_path, initialize as _chunk_usd_init,
)


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


@pytest.fixture
def nested(qapp):
    """sublayer -> group1 -> view, with a transform and a group2 inside group1."""
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
        """Let the debounced recooks actually run.

        Draining without advancing the clock leaves timer-driven work pending, so the mute
        state read afterwards is a transient rather than where the cook settles.
        """
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

    sub = mk("sublayer")
    g1 = mk("chunkgroup")
    t1 = mk("transform", context=g1)
    settle()
    view = next(c for c in reg.scene.get_chunks() if chunk_stage.is_view_chunk(c))
    connect_exec(stage, reg.scene, get_chunk_prim_path(sub), get_chunk_prim_path(g1))
    connect_exec(stage, reg.scene, get_chunk_prim_path(g1), get_chunk_prim_path(view))
    settle()

    g2 = mk("chunkgroup", context=g1)
    mk("duplicate_prim", context=g2)
    connect_exec(stage, reg.scene, get_chunk_prim_path(t1), get_chunk_prim_path(g2))
    settle()

    return qapp, scene, stage, lm, docker, sub, g1, g2, settle


def _muted(stage, lm, chunk):
    layer = lm.get_layer(chunk)
    return layer is not None and layer.identifier in set(stage.GetMutedLayers())


def test_one_level_deep_keeps_its_upstream(nested):
    """The case that already worked -- pinned so a fix cannot trade it away."""
    qapp, scene, stage, lm, docker, sub, g1, g2, settle = nested

    docker._on_dive_into(g1)
    settle()

    assert not _muted(stage, lm, sub), "the sublayer feeds this group; it must stay composed"


def test_two_levels_deep_keeps_the_parents_upstream_too(nested):
    """The reported case: what feeds the PARENT group must survive focusing the child."""
    qapp, scene, stage, lm, docker, sub, g1, g2, settle = nested

    docker._on_dive_into(g2)
    settle()

    assert not _muted(stage, lm, sub), (
        "a group nested inside another is fed by its parent's chain -- muting what feeds "
        "the parent leaves the inner chunks authoring against a stage their targets are "
        "not on"
    )


def test_coming_back_out_still_works(nested):
    """Navigating in and out must not leave anything muted behind."""
    qapp, scene, stage, lm, docker, sub, g1, g2, settle = nested

    docker._on_dive_into(g2)
    settle()
    docker._on_dive_into(g1)
    settle()

    assert not _muted(stage, lm, sub)
