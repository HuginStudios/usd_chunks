"""Nodes a script makes in an app start at the center of the graph view.

They used to go at (0, 0), (260, 0), ... wherever the view was looking, so a script run
in usdview could build nodes the user could not see.
"""

from __future__ import annotations

import pytest
from pxr import Gf, Sdf, Usd
from PySide6 import QtCore

from usd_chunks import Scene, chunk_scene, chunk_stage
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.frontends.qt.factory import ChunkWidgetDocker
from usd_chunks.graph import engine as chunk_graph

_STEP = chunk_scene._SCRIPT_NODE_STEP


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
    docker.resize(700, 700)
    docker.show()
    # The graph view comes with the first chunk, as in a usdview scene that has one.
    host.create.cube(prim_path="/geo/seed")
    qapp.processEvents()
    view = docker.tab_widget.get_current_graph_view()
    try:
        yield host, view
    finally:
        docker.close()
        docker.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        qapp.processEvents()
        host.is_app = False
        host.close()


def _pos(scene, chunk):
    prim = scene.get_stage().GetPrimAtPath(chunk_stage.get_chunk_prim_path(chunk))
    return chunk_graph.node_position(prim)


def _build(scene):
    box = scene.create.cube(prim_path="/geo/box")
    xform = scene.create.transform(box, primpath_location=box.get_param("prim_path"))
    return box, xform


def test_first_node_goes_to_the_center_of_the_view(app) -> None:
    host, view = app
    view.centerOn(3000, 1500)

    with Scene.current() as scene:
        box, xform = _build(scene)

    x, y = view.center_position()
    assert _pos(host, box) == pytest.approx((x, y))
    assert _pos(host, xform) == pytest.approx((x + _STEP, y)), "the next node goes in a row to its right"


def test_each_with_block_starts_at_the_view_again(app) -> None:
    host, view = app
    with Scene.current() as scene:
        _build(scene)

    view.centerOn(-2000, 800)
    with Scene.current() as scene:
        box, _ = _build(scene)

    assert _pos(host, box) == pytest.approx(view.center_position())


def test_a_script_s_own_scene_keeps_the_default_row(app) -> None:
    """No app, no view to aim at: until it saves, a script's network keeps the default row."""
    host, _view = app
    host.is_app = False
    host.close()

    with Scene.new() as scene:
        box, xform = _build(scene)
        assert _pos(scene, box) == (0.0, 0.0)
        assert _pos(scene, xform) == (_STEP, 0.0)


def _positions(path):
    stage = Usd.Stage.Open(str(path))
    root = chunk_graph.default_context(stage)
    return {p.GetName(): chunk_graph.node_position(p) for p in chunk_graph.resolve_graph_nodes(stage, root)}


def test_a_script_s_nodes_are_laid_out_when_it_saves(tmp_path) -> None:
    """Every scripted node went in one row (85 nodes reached x = 29,000). Saved, they are
    laid out as L does: a column per depth, branches one under the other."""
    path = tmp_path / "net.usda"
    with Scene.new() as scene:
        trunk = scene.create.transform(name="trunk")
        left = scene.create.transform(trunk, name="left")
        right = scene.create.transform(trunk, name="right")
        scene.create.merge(left, right, name="join")
        scene.save(str(path))
    pos = _positions(path)
    assert pos["left"][0] == pos["right"][0], "both branches in the column after the trunk"
    assert pos["left"][1] != pos["right"][1], "one under the other"
    assert pos["trunk"][0] < pos["left"][0] < pos["join"][0]


def test_nodes_already_in_a_file_stay_where_they_are(tmp_path) -> None:
    first, second = tmp_path / "first.usda", tmp_path / "second.usda"
    with Scene.new() as scene:
        scene.create.transform(name="old")
        scene.save(str(first))
    layer = Sdf.Layer.FindOrOpen(str(first))
    old = layer.GetAttributeAtPath(f"{chunk_graph.default_context(Usd.Stage.Open(layer))}/old.{chunk_graph.POS_ATTR}")
    old.default = Gf.Vec2f(-5000.0, 1234.0)
    layer.Save()

    with Scene.open(str(first)) as scene:
        old_chunk = next(c for c in scene.get_chunks() if c.name == "old")
        scene.create.transform(old_chunk, name="new")
        scene.save(str(second))
    pos = _positions(second)
    assert pos["old"] == (-5000.0, 1234.0)
    assert pos["new"][0] > pos["old"][0]
