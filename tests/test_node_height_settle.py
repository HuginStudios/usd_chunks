"""Heights must be settled before anything is allowed to hide a node's body.

A node's height comes from measuring its body widget, and that measurement only becomes
meaningful once the body has been laid out -- which takes several event loop turns and
produces several intermediate measurements along the way, each a perfectly plausible number.
Hiding a body mid-layout strands the node at whatever partial height it had reached, and its
ports and edges move with it.

Settling is answered by the scene, not the node: measurements are event-driven, so a node
that has simply not been re-measured yet is indistinguishable from one that has finished.
"""
from __future__ import annotations
import pytest
from PySide6 import QtCore, QtWidgets

from usd_chunks.plugins.chunk_qt_nodegraph import (
    ChunkGraphScene, ChunkNodeItem, _HEIGHT_SETTLE_MS)


@pytest.fixture
def scene(qapp):
    s = ChunkGraphScene()
    yield s
    s.deleteLater()


def _spin(qapp, ms):
    loop = QtCore.QEventLoop()
    QtCore.QTimer.singleShot(ms, loop.quit)
    loop.exec()
    qapp.processEvents()


def test_not_settled_before_the_graph_has_finished_building(qapp, scene):
    """Proxies are built in deferred batches, and building one measures nothing -- so
    mid-build there are quiet gaps longer than the settle window. A quiet period alone
    reported "settled" while a third of the graph had not been laid out."""
    scene.note_height_change()
    _spin(qapp, _HEIGHT_SETTLE_MS * 2)
    assert scene.heights_settled() is False


def test_settles_once_built_and_quiet(qapp, scene):
    scene._build_complete = True
    scene.note_height_change()
    assert scene.heights_settled() is False

    _spin(qapp, _HEIGHT_SETTLE_MS * 2)
    assert scene.heights_settled() is True


def test_a_partial_measurement_does_not_count_as_settled(qapp, scene):
    """The case every threshold test got wrong: 64px of an eventual 885px is non-zero and
    taller than the header, so only "it stopped changing" can reject it."""
    scene._build_complete = True
    for _ in range(4):  # heights still arriving, inside the window each time
        scene.note_height_change()
        _spin(qapp, _HEIGHT_SETTLE_MS // 3)
        assert scene.heights_settled() is False

    _spin(qapp, _HEIGHT_SETTLE_MS * 2)
    assert scene.heights_settled() is True


def test_a_later_change_unsettles_it_again(qapp, scene):
    scene._build_complete = True
    scene.note_height_change()
    _spin(qapp, _HEIGHT_SETTLE_MS * 2)
    assert scene.heights_settled() is True

    scene.note_height_change()  # e.g. a group expanded
    assert scene.heights_settled() is False
    _spin(qapp, _HEIGHT_SETTLE_MS * 2)
    assert scene.heights_settled() is True


class _FakeNode:
    """set_body_visible's guards alone, without a USD stage."""

    set_body_visible = ChunkNodeItem.set_body_visible

    def __init__(self, settled):
        self._settled = settled
        self._lod_simple = False
        self._ports = []
        self._disabled = False
        self._proxy = QtWidgets.QGraphicsProxyWidget()
        self._proxy.setWidget(QtWidgets.QWidget())

    def is_height_settled(self):
        return self._settled

    def _apply_dim(self):
        pass

    def update(self):
        pass


def test_body_cannot_be_hidden_while_heights_are_unsettled(qapp):
    node = _FakeNode(settled=False)
    node.set_body_visible(False)
    assert node._lod_simple is False, "hid a body before its height was known"


def test_body_hides_once_heights_have_settled(qapp):
    node = _FakeNode(settled=True)
    node.set_body_visible(False)
    assert node._lod_simple is True


def test_a_visible_proxy_flagged_flat_is_corrected(qapp):
    """The flag and the proxy can diverge -- a body that declined an earlier hide leaves the
    flag saying flat while the widget is still up. Trusting the flag alone stranded 41 of 56
    buried nodes visible."""
    node = _FakeNode(settled=True)
    node._lod_simple = True           # says flat...
    node._proxy.setVisible(True)      # ...but the body is up

    node.set_body_visible(False)
    assert node._proxy.isVisible() is False
