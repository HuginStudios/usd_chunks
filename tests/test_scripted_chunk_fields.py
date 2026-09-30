"""A chunk a script makes next to a View shows the values the script gave it.

Scripted chunks used to start blocked when their graph had a View. A blocked chunk's
params send no value notices, so its fields kept what they had when they were built: a
Transform wired to a Mesh's prim path showed "/" rather than @Mesh.prim_path@.
"""

from __future__ import annotations

import pytest
from PySide6 import QtCore, QtWidgets

from usd_chunks import Scene, chunk_scene
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.frontends.qt.factory import ChunkWidgetDocker

from qt_wait import pump_until


class _App(Scene):
    """Stands in for usdview: a live scene that belongs to an app."""

    is_app = True
    # An app's scene is borrowed: leaving a `with` block keeps it, as for usdview's.
    __enter__ = SceneSender.__enter__
    __exit__ = SceneSender.__exit__


@pytest.fixture
def app(qapp, monkeypatch):
    monkeypatch.setattr(chunk_scene, "graph_center_hook", None)
    host = _App(host_name="app")
    docker = ChunkWidgetDocker()
    docker.resize(900, 700)
    docker.show()
    qapp.processEvents()
    try:
        yield qapp, host
    finally:
        docker.close()
        docker.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        qapp.processEvents()
        host.is_app = False
        host.close()


def _field_texts(qapp):
    return [w.text() for w in qapp.allWidgets() if isinstance(w, QtWidgets.QLineEdit) and w.isVisible()]


def test_a_wired_prim_path_shows_its_expression(app) -> None:
    qapp, scene = app
    box = scene.create.cube(prim_path="/hej")
    scene.view(box)   # the View the graph adds after the first chunk
    lift = scene.create.transform(box, primpath_location=box.get_param("prim_path"), ty=3.0)

    pump_until(qapp, lambda: "@Mesh.prim_path@" in _field_texts(qapp))
    assert "@Mesh.prim_path@" in _field_texts(qapp)
    assert lift.is_blocked(), "still switched off: it is not in the View's chain"
