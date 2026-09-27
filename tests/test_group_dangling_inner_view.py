"""A group whose inner view is wired to nothing must still cook its contents.

Live-reported: sublayer -> chunkgroup A (Duplicate Prim) -> chunkgroup B, and from inside
B the duplicated prim is nowhere on the stage. The diagnosis was not muting: A's Duplicate
Prim was BLOCKED, i.e. it had never run. A's inner view existed but pulled nothing, so A's
own cook set was empty and A cooked nothing at all -- its contents went blocked and muted,
and it stopped feeding everything downstream, with no sign of why.

A view wired to nothing is not an answer to "what does this group output". It is a default
view that never got pointed at anything (ChunkGraphView._ensure_default_view creates one
whenever you dive in, including into an empty group), or one left dangling when the chunk
it watched was deleted -- heal_exec_chain_on_delete clears the connection when the deleted
node had no predecessor to inherit. Both are ordinary things to do to a graph.
"""
from __future__ import annotations

import weakref

import pytest
from PySide6 import QtCore
from pxr import Sdf, Usd

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
def dangling(qapp):
    """groupA[ prim, an inner view pointing at nothing ] -> groupB."""
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

    ga = mk("chunkgroup")
    settle()
    # Diving in is what creates the group's inner view -- the same UI action a user does.
    docker._on_dive_into(ga)
    settle()
    pa = mk("prim", context=ga)
    settle()

    inner_view = next(
        c for c in scene.get_chunks()
        if chunk_stage.is_view_chunk(c)
        and get_chunk_prim_path(c).startswith(get_chunk_prim_path(ga) + "/")
    )
    vp = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(inner_view)))
    chunk_stage.ChunkStageSync().clear_attr_connections(vp.GetAttribute("inputs:chunk"))
    settle()

    docker.tab_widget.setCurrentIndex(0)
    settle()
    gb = mk("chunkgroup")
    settle()
    connect_exec(stage, scene, get_chunk_prim_path(ga), get_chunk_prim_path(gb))
    settle()

    return dict(scene=scene, stage=stage, lm=lm, docker=docker, settle=settle,
                ga=ga, gb=gb, pa=pa, inner_view=inner_view)


def _content(stage):
    return {
        str(p.GetPath()) for p in stage.Traverse()
        if not str(p.GetPath()).startswith("/chunks")
    }


def test_the_group_still_cooks_its_contents(dangling):
    """Wiring the group into the graph must not silently switch its contents off."""
    assert "/prim" in _content(dangling["stage"])
    assert not dangling["pa"].is_blocked(), "the chunk never ran, rather than being hidden"


def test_the_group_downstream_of_it_sees_that_content(dangling):
    """The reported case: from inside the group wired after it."""
    dangling["docker"]._on_dive_into(dangling["gb"])
    dangling["settle"]()

    assert "/prim" in _content(dangling["stage"])


def test_an_empty_group_with_a_dangling_view_is_still_fine(qapp):
    """There is nothing to fall back to, and that must be a quiet no-op, not a crash."""
    stage = Usd.Stage.CreateInMemory()

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)

    docker = ChunkWidgetDocker()
    docker.show()
    qapp.processEvents()

    mod = reg.get_chunk_module("chunkgroup")
    group = (mod.create() if mod.loaded else mod.load())[0]
    scene.add_chunk(group)
    qapp.processEvents()
    docker._on_dive_into(group)
    for _ in range(30):
        qapp.processEvents()
        QtCore.QThread.msleep(10)

    view = next(c for c in scene.get_chunks() if chunk_stage.is_view_chunk(c))
    chunk_stage.recook_focus(stage, scene, scene.get_root_context(), view)
    qapp.processEvents()
