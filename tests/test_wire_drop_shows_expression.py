"""Wiring a param into a chunk created by the wire drop shows the expression on it.

Reported: drag a Prim chunk's "Prim Path" output into empty space, pick Transform from the
menu, and the wire is made and works -- but the Transform's Prim Path field shows nothing.

The chunk is blocked the moment it is created (view-driven execution: nothing is wired to
it yet), and a blocked chunk suppresses its own param notices. So the deferred connect
authored the expression, it resolved correctly, and the widget never heard a thing -- a
node whose field is empty while the graph behaves as though it is not.
"""
from __future__ import annotations

import time

import pytest
from PySide6 import QtCore, QtWidgets
from pxr import Usd

from usd_chunks import chunk_core, chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks import chunk_stage
from usd_chunks.plugins.chunk_qt import ChunkWidgetDocker
from usd_chunks.chunk_stage import get_chunk_prim_path, initialize as _chunk_usd_init

EXPRESSION = "@Prim.primpath_location@"


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


@pytest.fixture
def dropped(qapp):
    """A Prim chunk, then a Transform created and wired the way a wire drop does it."""
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
        for _ in range(25):
            qapp.processEvents()
            QtCore.QThread.msleep(10)
        qapp.processEvents()

    def mk(key):
        mod = reg.get_chunk_module(key)
        cs = mod.create() if mod.loaded else mod.load()
        for c in cs:
            scene.add_chunk(c)
        return cs[0]

    prim_chunk = mk("prim")
    settle()
    graph_view = docker.tab_widget.get_current_graph_view()
    src_attr = f"{get_chunk_prim_path(prim_chunk)}.outputs:primpath_location"

    # What _create_and_connect_from_menu does once the menu has picked a module: create it
    # blocked (a view exists, and nothing is wired to this node yet), add it, then connect
    # on a deferred pass because the new node's ports do not exist until the rebuild runs.
    view_exists = graph_view._context_has_view(scene, graph_view._graph_context())
    assert view_exists, "the fixture must reproduce the blocked-on-create path"
    mod = reg.get_chunk_module("transform")
    chunks = mod.create() if mod.loaded else mod.load()
    new_chunk = chunks[0]
    for c in chunks:
        if not chunk_stage.is_view_chunk(c) and not chunk_stage.is_group_chunk(c):
            c.set_blocked(True)
        scene.add_chunk(c)
    settle()
    graph_view._connect_ports_by_path(src_attr, False, get_chunk_prim_path(new_chunk))
    settle()

    # docker is returned, not just used: nothing else holds it, and a garbage-collected
    # docker takes the graph view's C++ object with it ("already deleted") mid-test.
    return dict(qapp=qapp, gv=graph_view, chunk=new_chunk, stage=stage, docker=docker)


def _field_text(qapp, gv, chunk, param_name):
    path = get_chunk_prim_path(chunk)
    node = gv.scene()._nodes.get(path)
    end = time.monotonic() + 2.0
    while time.monotonic() < end and (node is None or getattr(node, "_chunk_widget", None) is None):
        qapp.processEvents()
        node = gv.scene()._nodes.get(path)
    assert node is not None and node._chunk_widget is not None
    widget = next(
        w for w in node._chunk_widget.findChildren(QtWidgets.QWidget)
        if isinstance(w, chunk_core.ChunkParamWidgetBase)
        and getattr(w.param, "name", None) == param_name
    )
    line_edit = widget.findChild(QtWidgets.QLineEdit)
    assert line_edit is not None
    return line_edit.text()


def test_the_field_shows_the_expression(dropped):
    text = _field_text(dropped["qapp"], dropped["gv"], dropped["chunk"], "primpath_location")

    assert text == EXPRESSION, f"the field should show the wire, got {text!r}"


def test_the_expression_is_still_what_the_param_holds(dropped):
    """Pinned: the wiring worked before this fix, and must keep working."""
    param = dropped["chunk"].get_param("primpath_location")

    assert param.raw_value == EXPRESSION
    assert param.value == "/prim", "and still resolves through the wire"


def test_the_chunk_is_left_blocked(dropped):
    """Showing the value must not cost the block.

    Unblocking is how the widget hears the change, but the node has nothing wired into its
    exec chain yet -- leaving it unblocked would let it cook before the recook says it may.
    """
    chunk = dropped["chunk"]

    assert chunk.is_blocked(), "the chunk must go back to blocked once the value is set"
    assert not getattr(chunk, "_executing", False), "and the execute guard must be released"
