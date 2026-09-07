"""A chunkgroup must see the stage of the chunkgroup wired into it.

Reported: wire chunkgroup A -> chunkgroup B, dive into B, and everything A put on the
stage is gone -- so B's inner chunks cook against a stage their own input isn't on. It
looked like a group could only reach another group's stage through a *data* wire, because
connecting A's Description output happens to author a connection from the group prim down
to an inner chunk, which is what dragged those inner chunks into recook_focus's
node-level "keep the focused group's upstream" set at all.

The exec wire alone gave that set only the group NODE, whose own layer is empty: a group
authors nothing, all of its content lives in its children's layers. Muting them is what
emptied the stage.
"""
from __future__ import annotations

import weakref

import pytest
from PySide6 import QtCore
from pxr import Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins import chunk_usd
from usd_chunks.plugins.chunk_qt import ChunkWidgetDocker
from usd_chunks.plugins.chunk_usd import (
    connect_exec, get_chunk_prim_path, initialize as _chunk_usd_init,
)


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


@pytest.fixture
def groups(qapp):
    """groupA[ prim -> groupA2[ prim ] ] -> groupB[ prim -> groupB2[ prim ] ] -> view,
    plus an unwired groupC.

    Every prim chunk is named after the group holding it, so what is composed on the stage
    says directly which group's content survived.
    """
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
        """Let the debounced recooks actually run (see test_nested_group_focus_keeps_upstream)."""
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

    def prim_in(group, path):
        c = mk("prim", context=group)
        c.get_param("primpath_location").value = path
        return c

    ga = mk("chunkgroup")
    pa = prim_in(ga, "/from_a")
    ga2 = mk("chunkgroup", context=ga)
    pa2 = prim_in(ga2, "/from_a2")
    gb = mk("chunkgroup")
    pb = prim_in(gb, "/from_b")
    gb2 = mk("chunkgroup", context=gb)
    pb2 = prim_in(gb2, "/from_b2")
    gc = mk("chunkgroup")
    pc = prim_in(gc, "/from_c")
    settle()

    # One tail per group, so each group's own cook reaches all of its content.
    connect_exec(stage, scene, get_chunk_prim_path(pa), get_chunk_prim_path(ga2))
    connect_exec(stage, scene, get_chunk_prim_path(pb), get_chunk_prim_path(gb2))
    view = next(c for c in scene.get_chunks() if chunk_usd.is_view_chunk(c))
    connect_exec(stage, scene, get_chunk_prim_path(ga), get_chunk_prim_path(gb))
    connect_exec(stage, scene, get_chunk_prim_path(gb), get_chunk_prim_path(view))
    settle()
    # The tab's settled cook -- connect_exec authors the wire, the graph widget is what
    # normally follows it with a recook.
    chunk_usd.recook_focus(stage, scene, scene.get_root_context(), view)
    settle()

    return dict(
        qapp=qapp, scene=scene, stage=stage, lm=lm, docker=docker, settle=settle,
        ga=ga, ga2=ga2, gb=gb, gb2=gb2, gc=gc, pa=pa, pa2=pa2, pb=pb, pb2=pb2, pc=pc,
    )


def _content(stage):
    return {
        str(p.GetPath()) for p in stage.Traverse()
        if not str(p.GetPath()).startswith("/chunks")
    }


def test_tab_cook_composes_every_wired_group(groups):
    """Baseline: with nothing focused, the wired groups' content is on the stage."""
    stage = groups["stage"]
    assert {"/from_a", "/from_a2", "/from_b", "/from_b2"} <= _content(stage)


def test_diving_into_a_group_keeps_its_upstream_groups_content(groups):
    """The reported case: A's content must survive focusing B."""
    groups["docker"]._on_dive_into(groups["gb"])
    groups["settle"]()

    content = _content(groups["stage"])
    assert "/from_a" in content, "the group wired into this one must stay composed"
    assert "/from_a2" in content, "including a group nested inside that upstream group"
    assert "/from_b" in content, "and the focused group's own content, of course"


def test_diving_still_mutes_an_unrelated_group(groups):
    """The muting pass must still do its job -- an unwired sibling group is not upstream."""
    groups["docker"]._on_dive_into(groups["gb"])
    groups["settle"]()

    assert "/from_c" not in _content(groups["stage"])


def test_diving_two_levels_deep_still_reaches_the_upstream_group(groups):
    """The focused group's own upstream is its PARENT's -- one level further out."""
    groups["docker"]._on_dive_into(groups["gb2"])
    groups["settle"]()

    content = _content(groups["stage"])
    assert "/from_a" in content
    assert "/from_a2" in content


def test_a_chunk_inside_the_group_reads_the_upstream_group_when_naming(qapp):
    """The reported symptom, through a real chunk rather than the mute state.

    Duplicate Prim suggests a destination by walking the '_copy', '_copy1', ... suffix
    until it finds a path nothing occupies -- read off the live stage. With group A's copy
    muted away, the chunk inside group B saw a free '_copy' and suggested the path group A
    had already taken, which is what made the bug visible in the first place: two chunks
    duplicating the same source both claiming one destination.
    """
    src = "/Kitchen/Pot"
    stage = Usd.Stage.CreateInMemory()
    stage.DefinePrim(src, "Xform")

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
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
    dupe_a = mk("core_usd.duplicate_prim", context=ga)
    gb = mk("chunkgroup")
    dupe_b = mk("core_usd.duplicate_prim", context=gb)
    settle()
    view = next(c for c in scene.get_chunks() if chunk_usd.is_view_chunk(c))
    connect_exec(stage, scene, get_chunk_prim_path(ga), get_chunk_prim_path(gb))
    connect_exec(stage, scene, get_chunk_prim_path(gb), get_chunk_prim_path(view))
    settle()
    chunk_usd.recook_focus(stage, scene, scene.get_root_context(), view)
    settle()

    dupe_a.get_param("source_prim").value = src
    settle()
    assert dupe_a.get_param("dest_path").value == f"{src}_copy"

    # Diving in is how you get a chunk into group B at all, so it is where the user hits this.
    docker._on_dive_into(gb)
    settle()

    dupe_b.get_param("source_prim").value = src
    settle()
    assert dupe_b.get_param("dest_path").value == f"{src}_copy1", (
        "the copy group A already made is on the stage; the next free name is _copy1"
    )
