"""Dropping a USD file on an empty panel creates the context it needs.

Drop handling lives on ChunkGraphView, and a graph view only exists inside a context
tab -- so before any context existed there was nothing to drop onto and the drag was
simply refused. You had to know to create a context first, which is not discoverable.

The panel now accepts the drop itself while no graph view exists, creates the context,
and hands the files to the graph view that results. This is the same gap
_EmptyPanelTabRouter already covers for the Tab key.
"""
from __future__ import annotations

import pytest
from pxr import Usd
from PySide6 import QtCore, QtGui

from usd_chunks import chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.frontends.qt.factory import ChunkWidgetDocker
from usd_chunks.frontends.qt.nodegraph import ChunkGraphView
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


@pytest.fixture
def usd_file(tmp_path):
    """A plain USD scene -- offers Sublayer/Reference, not "Import as Chunks"."""
    path = tmp_path / "dropped.usda"
    s = Usd.Stage.CreateNew(str(path))
    s.DefinePrim("/World", "Xform")
    s.GetRootLayer().Save()
    return path


def _drop_event(path, pos=QtCore.QPointF(10, 10)):
    """A synthetic file drop.

    QDropEvent does not take ownership of its QMimeData, so a local one is collected
    before the handler reads it and event.mimeData() then dereferences freed memory --
    an access violation that kills the interpreter rather than failing the test. Keep a
    reference alive on the event itself.
    """
    mime = QtCore.QMimeData()
    mime.setUrls([QtCore.QUrl.fromLocalFile(str(path))])
    event = QtGui.QDropEvent(
        pos,
        QtCore.Qt.DropAction.CopyAction,
        mime,
        QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.KeyboardModifier.NoModifier,
    )
    event._mime_keepalive = mime
    return event


def _pick(text):
    def _exec(self, menu, _global_pos):
        for act in menu.actions():
            if act.text() == text:
                return act
        return None
    return _exec


def test_the_panel_starts_with_no_graph_view(docker):
    """The precondition: this is why the drop had nowhere to go."""
    _qapp, _reg, _scene, d = docker
    assert d.tab_widget.get_current_graph_view() is None


def test_dropping_a_file_on_an_empty_panel_creates_a_context(docker, usd_file, monkeypatch):
    qapp, _reg, scene, d = docker
    monkeypatch.setattr(ChunkGraphView, "_exec_menu", _pick("Import as Sublayer"))

    d.tab_widget.dropEvent(_drop_event(usd_file))
    qapp.processEvents()

    assert d.tab_widget.get_current_graph_view() is not None, (
        "the drop must create the context it needs rather than being refused"
    )
    assert scene.get_chunks(), "the dropped file should have produced a chunk"


def test_the_dropped_file_is_actually_imported(docker, usd_file, monkeypatch):
    qapp, _reg, scene, d = docker
    monkeypatch.setattr(ChunkGraphView, "_exec_menu", _pick("Import as Sublayer"))

    d.tab_widget.dropEvent(_drop_event(usd_file))
    qapp.processEvents()

    names = [c.name for c in scene.get_chunks()]
    assert any("ublayer" in n for n in names), f"expected a Sublayer chunk, got {names}"


def test_a_non_usd_drag_is_left_alone(docker, tmp_path):
    """The panel must not swallow drops it has no business handling."""
    _qapp, _reg, _scene, d = docker
    other = tmp_path / "notes.txt"
    other.write_text("hello", encoding="utf-8")

    assert d.tab_widget._empty_panel_drop_paths(_drop_event(other)) == []


def test_once_a_graph_view_exists_the_panel_stops_intercepting(docker, usd_file, monkeypatch):
    """The graph view knows the drop position; the panel must hand back over to it."""
    qapp, _reg, _scene, d = docker
    monkeypatch.setattr(ChunkGraphView, "_exec_menu", _pick("Import as Sublayer"))

    d.tab_widget.dropEvent(_drop_event(usd_file))
    qapp.processEvents()

    assert d.tab_widget.get_current_graph_view() is not None
    assert d.tab_widget._empty_panel_drop_paths(_drop_event(usd_file)) == []
