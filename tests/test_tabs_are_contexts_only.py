"""Tabs are navigation, not something you create.

A tab used to be creatable by hand: index 0 was a dedicated slot holding a "+" button
whose only job was to spin up a new CONTEXT chunk. That made a tab a thing the user
authors, competing with the chunk hierarchy as a way to organise a scene.

Tabs now only ever mirror the hierarchy -- the root context, plus a chunkgroup you have
dived into -- so every tab corresponds to a real chunk context and index 0 is a real tab
rather than a reserved button slot. Creating a context is still possible internally (the
root has to come from somewhere, and a chunk added with no tab to receive it needs one),
it is just no longer offered as a UI affordance.
"""
from __future__ import annotations

import pytest
from pxr import Usd
from PySide6 import QtWidgets

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins.chunk_qt import ChunkWidgetDocker
from usd_chunks.chunk_stage import initialize as _chunk_usd_init


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


@pytest.fixture
def docker(qapp):
    stage = Usd.Stage.CreateInMemory()

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)

    d = ChunkWidgetDocker()
    d.resize(500, 600)
    d.show()
    qapp.processEvents()
    return qapp, reg, scene, d


def _add(reg, scene, key="transform"):
    mod = reg.get_chunk_module(key)
    cs = mod.create() if mod.loaded else mod.load()
    for c in cs:
        scene.add_chunk(c)
    return cs[0]


def test_there_is_no_new_tab_button(docker):
    _qapp, _reg, _scene, d = docker

    plus = [
        b for b in d.tab_widget.findChildren(QtWidgets.QAbstractButton)
        if b.text().strip() == "+"
    ]
    assert not plus, "the '+' button created tabs by hand; tabs are navigation only now"


def test_every_tab_is_a_real_context(docker):
    qapp, reg, scene, d = docker
    _add(reg, scene)
    qapp.processEvents()

    tw = d.tab_widget
    assert tw.count() >= 1
    for i in range(tw.count()):
        assert tw.get_chunk_context(i) is not None, (
            f"tab {i} has no chunk context -- a reserved button slot, not a real tab"
        )


def test_the_root_context_is_the_first_tab(docker):
    qapp, reg, scene, d = docker
    _add(reg, scene)
    qapp.processEvents()

    tw = d.tab_widget
    first = tw.get_chunk_context(0)
    assert first is not None
    assert d._current_context() is first.group, (
        "index 0 is a real context now, so it must report as the current one rather than "
        "being treated as the '+' slot"
    )


def test_a_chunk_still_lands_in_a_tab(docker):
    """The internal 'no tab to receive this' fallback must survive losing the button."""
    qapp, reg, scene, d = docker
    c = _add(reg, scene)
    qapp.processEvents()

    tw = d.tab_widget
    holders = [
        i for i in range(tw.count())
        if (cw := tw.get_chunk_context(i)) is not None and c in cw.group.get_my_params()
    ]
    assert holders, "the chunk was created with no tab open; one should have been made for it"
