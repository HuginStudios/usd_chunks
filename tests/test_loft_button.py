"""Loft a button: the group gets a button that clicks the inner one."""
from __future__ import annotations

from tests.test_unloft import _lofted, _loft, env  # noqa: F401
from PySide6 import QtGui, QtWidgets
from pxr import Usd

from usd_chunks import chunk, chunk_stage
from usd_chunks.chunk_stage import export_chunks
from usd_chunks.frontends.qt.widgets import ChunkPushButtonWidget, LabeledWidget
from tests.test_lofted_menu_follows_inner import _init, _mk, _settle


def _button_on(inner, clicks):
    btn = chunk.Button("go", "Go")
    btn.add_callback(lambda *a: clicks.append(1))
    inner.add_param(btn)
    return btn


def test_a_lofted_button_clicks_the_inner_one(env):
    group = env["mk"]("chunkgroup", name="Group")
    inner = env["mk"]("transform", context=group)
    env["settle"]()
    clicks = []
    btn = _button_on(inner, clicks)
    env["settle"]()

    lofted = _loft(env, btn, group)

    assert isinstance(lofted, chunk.Button)
    lofted.click()
    env["settle"]()
    assert clicks == [1]


def test_a_reopened_lofted_button_still_clicks_the_inner_one(qapp, tmp_path):
    stage = Usd.Stage.CreateInMemory()
    scene = _init(stage)
    group = _mk(scene, "chunkgroup")
    inner = _mk(scene, "export_chunkgroup", context=group)
    _settle(qapp)
    chunk_stage.loft_param_to_context(inner.get_param("export"), group)
    _settle(qapp)
    out = tmp_path / "lofted_button.usda"
    export_chunks(scene, out)

    scene2 = _init(Usd.Stage.Open(str(out)))
    _settle(qapp)
    group2 = next(c for c in scene2.get_chunks() if c.name == group.name)
    inner2 = next(c for c in group2.get_my_params() if c.name == inner.name)
    lofted2 = next(p for p in group2.get_my_params() if getattr(p, "_lofted", False))
    assert isinstance(lofted2, chunk.Button)

    clicks = []
    inner2.get_param("export").add_callback(lambda *a: clicks.append(1))
    lofted2.click()
    _settle(qapp)
    assert clicks == [1]


def test_right_clicking_a_button_offers_loft_up(env, qapp, monkeypatch):
    group = env["mk"]("chunkgroup", name="Group")
    inner = env["mk"]("transform", context=group)
    env["settle"]()
    btn = _button_on(inner, [])
    offered = []

    def _capture(menu, *args):
        offered.extend(a.text() for a in menu.actions())

    monkeypatch.setattr(QtWidgets.QMenu, "exec_", _capture)
    row = LabeledWidget(ChunkPushButtonWidget(btn))
    row.resize(200, 40)
    row.show()
    qapp.processEvents()
    click_at = row.widget.geometry().center()
    row.contextMenuEvent(
        QtGui.QContextMenuEvent(QtGui.QContextMenuEvent.Reason.Mouse, click_at)
    )

    assert "Loft up" in offered
