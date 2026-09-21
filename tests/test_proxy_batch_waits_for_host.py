"""ChunkGraphScene._create_proxies_batch holds off while the host shows a busy cursor.

usdview keeps a busy override cursor up from early in its start-up until its first image
is drawn, and drains every pending zero-timer -- the whole proxy batch chain -- inside a
processEvents() it makes on the way. A ChunkWidget built in that pass cost 10-15x what the
same widget costs right after (measured: 50s of start-up against 15s on a 21-node scene).
The batch therefore waits for the cursor to be restored, for at most
_PROXY_BUSY_WAIT_MAX_S so a host that stays busy still gets its graph.
"""

import pytest
from PySide6 import QtCore, QtWidgets

from usd_chunks.plugins import chunk_qt_nodegraph as ng


class _Node:
    """Stands in for a ChunkNodeItem: all the batch touches is create_proxy()."""

    def __init__(self, log, name):
        self._log = log
        self.chunk = type("C", (), {"name": name})()

    def create_proxy(self):
        self._log.append(self.chunk.name)


def _pump(qapp, ms):
    deadline = QtCore.QDeadlineTimer(ms)
    while not deadline.hasExpired():
        qapp.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 10)
        QtCore.QThread.msleep(5)


@pytest.fixture
def scene(qapp):
    sc = ng.ChunkGraphScene()
    sc._build_gen = 1
    yield sc
    # Never leave a cursor override behind for the next test.
    while QtWidgets.QApplication.overrideCursor() is not None:
        QtWidgets.QApplication.restoreOverrideCursor()


def test_batch_waits_while_the_host_is_busy_then_builds(qapp, scene):
    built = []
    nodes = [_Node(built, f"n{i}") for i in range(7)]

    QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.BusyCursor)
    scene._create_proxies_batch(nodes, scene._build_gen)
    assert built == [], "nothing may be built while the host is busy"
    _pump(qapp, 200)
    assert built == [], "still nothing: the retry keeps deferring while the cursor is up"

    QtWidgets.QApplication.restoreOverrideCursor()
    _pump(qapp, 300)
    assert built == [f"n{i}" for i in range(7)], (
        "once the host is idle every node gets its proxy, in order, across the batches"
    )


def test_batch_builds_anyway_after_the_wait_limit(qapp, scene, monkeypatch):
    monkeypatch.setattr(ng, "_PROXY_BUSY_WAIT_MAX_S", 0.15)
    built = []
    nodes = [_Node(built, f"n{i}") for i in range(3)]

    QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.BusyCursor)
    scene._create_proxies_batch(nodes, scene._build_gen)
    assert built == []
    _pump(qapp, 400)
    assert built == ["n0", "n1", "n2"], (
        "a host that stays busy past the limit still gets its graph, at the slow price"
    )


def test_batch_runs_at_once_when_the_host_is_idle(qapp, scene):
    built = []
    nodes = [_Node(built, f"n{i}") for i in range(2)]
    assert QtWidgets.QApplication.overrideCursor() is None
    scene._create_proxies_batch(nodes, scene._build_gen)
    assert built == ["n0", "n1"], "no cursor, no wait: the first batch builds synchronously"
