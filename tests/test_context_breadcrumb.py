"""The tab row says which groups are open; the breadcrumb says where you are.

Tabs are the groups you have opened, in the order you opened them -- browser tabs, not a
tree -- so dive straight into a nested group from the graph and its parent may not be open
at all. No arrangement of that row can show the nesting; a path can, because it has
direction and is as long as the nesting is deep rather than as long as the session.
"""
from __future__ import annotations

import weakref

import pytest
from PySide6 import QtCore, QtWidgets
from pxr import Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins.chunk_qt import (
    ChunkContextBreadcrumb, ChunkWidgetDocker, context_ancestry,
)
from usd_chunks.plugins.chunk_usd import initialize as _chunk_usd_init


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


@pytest.fixture
def nested(qapp):
    """Three groups nested inside one another, plus a sibling of the outermost."""
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
    docker.resize(900, 700)
    docker.show()
    qapp.processEvents()

    def settle():
        for _ in range(20):
            qapp.processEvents()
            QtCore.QThread.msleep(5)
        qapp.processEvents()

    def mk(context=None):
        mod = reg.get_chunk_module("chunkgroup")
        cs = mod.create() if mod.loaded else mod.load()
        prev = scene._current_context
        scene._current_context = weakref.ref(context) if context is not None else None
        try:
            for c in cs:
                scene.add_chunk(c)
        finally:
            scene._current_context = prev
        settle()
        return cs[0]

    outer = mk()
    middle = mk(outer)
    inner = mk(middle)
    sibling = mk()
    # The scene's own default group is the root of every path -- it is what the outermost
    # user-made group is nested in, so it belongs in the row.
    root = scene.get_root_context()
    return dict(docker=docker, settle=settle, outer=outer, middle=middle,
                inner=inner, sibling=sibling, scene=scene, root=root)


def _texts(breadcrumb):
    return [
        w.text() for w in breadcrumb.findChildren(QtWidgets.QToolButton)
    ]


def test_the_path_runs_root_first(nested):
    chain = context_ancestry(nested["inner"])

    assert [c.name for c in chain] == [
        nested["root"].name, nested["outer"].name, nested["middle"].name, nested["inner"].name
    ]


def test_the_row_shows_the_whole_path(nested):
    docker = nested["docker"]
    docker._on_dive_into(nested["inner"])
    nested["settle"]()

    assert _texts(docker._breadcrumb) == [
        nested["root"].name, nested["outer"].name, nested["middle"].name, nested["inner"].name
    ]


def test_the_group_you_are_in_is_not_a_button_to_press(nested):
    """It is a position, not an offer -- pressing it would do nothing anyway."""
    docker = nested["docker"]
    docker._on_dive_into(nested["inner"])
    nested["settle"]()

    entries = docker._breadcrumb.findChildren(QtWidgets.QToolButton)
    assert entries[-1].text() == nested["inner"].name
    assert not entries[-1].isEnabled()
    assert all(e.isEnabled() for e in entries[:-1]), "every ancestor stays clickable"


def test_clicking_an_ancestor_goes_there(nested):
    docker = nested["docker"]
    docker._on_dive_into(nested["outer"])
    docker._on_dive_into(nested["inner"])
    nested["settle"]()
    assert docker._current_context() is nested["inner"]

    # By position, not by text: a group nested inside another may legitimately carry the
    # same name (/chunks/ChunkGroup/ChunkGroup), so the label alone does not identify one.
    entry = docker._breadcrumb.findChildren(QtWidgets.QToolButton)[1]
    entry.click()
    nested["settle"]()
    assert docker._current_context() is nested["outer"]


def test_it_reaches_a_group_that_has_no_tab_open(nested):
    """The point of making every entry clickable.

    Diving straight into a nested group leaves its parent with no tab, and a path you can
    only partly click is the context-navigation irritation this is meant to avoid.
    """
    docker = nested["docker"]
    # Close every tab but the one we are in, so the ancestors have none.
    docker._on_dive_into(nested["inner"])
    nested["settle"]()
    keep = docker.tab_widget.currentIndex()
    for i in reversed(range(docker.tab_widget.count())):
        if i != keep:
            docker.tab_widget.removeTab(i)
    nested["settle"]()
    assert docker.tab_widget.get_chunk_context_index(nested["middle"]) is None

    # Position again -- index 2 is `middle` in root > outer > middle > inner.
    entry = docker._breadcrumb.findChildren(QtWidgets.QToolButton)[2]
    entry.click()
    nested["settle"]()

    assert docker._current_context() is nested["middle"]


def test_only_the_ancestors_are_marked_on_the_tabs(nested):
    """What arranging the tab row was meant to convey, without moving anything.

    Opens a tab for each group first: marking is about the tabs already in front of you,
    and a group you have never dived into does not have one.
    """
    docker = nested["docker"]
    for group in ("sibling", "outer", "middle", "inner"):
        docker._on_dive_into(nested[group])
    nested["settle"]()

    bar = docker.tab_widget.tabBar()
    colours = {}
    for i in range(docker.tab_widget.count()):
        ctx = docker.tab_widget.widget(i).ctx()
        if ctx is not None:
            colours[ctx.name] = bar.tabTextColor(i)

    marked = colours.get(nested["outer"].name)
    assert marked == colours.get(nested["middle"].name), "both ancestors marked alike"
    assert colours.get(nested["sibling"].name) != marked, "an unrelated group is not"
    assert colours.get(nested["inner"].name) != marked, "nor the tab you are on"


def test_a_context_with_no_parent_is_a_path_of_one(qapp):
    """The row is never empty, so nothing below it moves as you navigate."""
    breadcrumb = ChunkContextBreadcrumb()
    breadcrumb.set_context(None)

    assert _texts(breadcrumb) == []
