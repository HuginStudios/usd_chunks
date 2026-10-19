"""Revert to Default puts a param back as if it had never been edited: nothing of it in USD."""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets
from pxr import Usd

from tests.test_override_through_composition import (  # noqa: F401 -- composed_shot is a fixture
    _SEQ_VALUE, _SHOT_VALUE, _TX, _tx_param, composed_shot,
)
from tests.test_unloft import env  # noqa: F401
from usd_chunks import chunk_core, chunk_undo
from usd_chunks.chunk_stage import can_revert_to_default, revert_param_to_default
from usd_chunks.frontends.qt.widgets import ChunkPushButtonWidget, ChunkWidget, LabeledWidget

REVERT = "Revert to Default"


def _row(widget: ChunkWidget, model) -> LabeledWidget:
    """The row in widget that shows model."""
    for row in widget.findChildren(LabeledWidget):
        if row._model is model:
            return row
    raise AssertionError(f"no row for {model.name}")


def _right_click(target, monkeypatch, pick=None) -> dict:
    """Right-click target through Qt's event system and pick the item named pick, if any.

    Args:
        target: The widget to right-click: a row, or a widget inside one.
        monkeypatch: pytest's monkeypatch, to stand in for the menu's exec_.
        pick: The text of the item to choose, or None to close the menu.

    Returns:
        The menu's items, text to whether they were enabled.
    """
    offered = {}

    def _exec(menu, *args):
        offered.update({a.text(): a.isEnabled() for a in menu.actions()})
        return next((a for a in menu.actions() if a.text() == pick), None)

    monkeypatch.setattr(QtWidgets.QMenu, "exec_", _exec)
    pos = QtCore.QPoint(2, 2)
    event = QtGui.QContextMenuEvent(QtGui.QContextMenuEvent.Reason.Mouse, pos, target.mapToGlobal(pos))
    QtWidgets.QApplication.sendEvent(target, event)
    return offered


def _layers(stage: Usd.Stage) -> dict[str, str]:
    """Every layer the stage uses, as text: what it would save."""
    return {layer.identifier: layer.ExportToString() for layer in stage.GetUsedLayers()}


def _uv_transform(env):  # noqa: F811 -- the imported fixture
    node = env["mk"]("uv_transform")
    widget = ChunkWidget(node)
    widget.resize(400, 600)
    widget.show()
    env["settle"]()
    return node, widget


def test_a_reverted_slider_leaves_nothing_in_usd(env, monkeypatch):  # noqa: F811
    node, widget = _uv_transform(env)
    before = _layers(env["stage"])
    rotation = node.get_param("rotation")
    rotation.value = 45.0
    env["settle"]()
    assert _layers(env["stage"]) != before

    offered = _right_click(_row(widget, rotation), monkeypatch, pick=REVERT)
    env["settle"]()

    assert offered[REVERT] is True
    assert rotation.value == 0.0
    assert rotation.get_usd_prop() is None
    assert _layers(env["stage"]) == before


def test_an_edit_after_a_revert_is_stored(env, monkeypatch):  # noqa: F811
    node, widget = _uv_transform(env)
    rotation = node.get_param("rotation")
    rotation.value = 45.0
    env["settle"]()
    _right_click(_row(widget, rotation), monkeypatch, pick=REVERT)
    env["settle"]()

    rotation.value = 30.0
    env["settle"]()

    assert rotation.get_usd_prop().Get() == 30.0


def test_the_item_is_greyed_out_on_an_untouched_param(env, monkeypatch):  # noqa: F811
    node, widget = _uv_transform(env)

    offered = _right_click(_row(widget, node.get_param("rotation")), monkeypatch)

    assert offered[REVERT] is False


def test_a_reverted_wire_leaves_no_connection(env, monkeypatch):  # noqa: F811
    source = env["mk"]("uv_transform", name="Source")
    source.get_param("rotation").value = 30.0
    node, widget = _uv_transform(env)
    before = _layers(env["stage"])
    rotation = node.get_param("rotation")
    rotation.value = f"@{source.name}.rotation@"
    env["settle"]()
    assert rotation.value == 30.0
    assert rotation.get_usd_prop().GetConnections()

    offered = _right_click(_row(widget, rotation), monkeypatch, pick=REVERT)
    env["settle"]()

    assert offered[REVERT] is True
    assert rotation.raw_value == 0.0
    assert _layers(env["stage"]) == before


def test_a_reverted_vector_leaves_nothing_in_usd(env, monkeypatch):  # noqa: F811
    node, widget = _uv_transform(env)
    before = _layers(env["stage"])
    scale = node.get_param("scale")
    assert isinstance(scale, chunk_core.ChunkVectorBase)
    scale.value = (2.0, 3.0)
    env["settle"]()

    # On the header, the vector's name: the click goes up from there to the vector's row.
    header = _row(widget, scale).widget.minimize_button
    offered = _right_click(header, monkeypatch, pick=REVERT)
    env["settle"]()

    assert offered == {REVERT: True}
    assert scale.value == (1.0, 1.0)
    assert _layers(env["stage"]) == before


def test_vector_fields_revert_one_at_a_time(env, monkeypatch):  # noqa: F811
    node, widget = _uv_transform(env)
    before = _layers(env["stage"])
    scale = node.get_param("scale")
    scale.value = (2.0, 3.0)
    env["settle"]()

    _right_click(_row(widget, scale[0]), monkeypatch, pick=REVERT)
    env["settle"]()
    # The other field is still edited, so the vector keeps its attr.
    assert scale.value == (1.0, 3.0)
    assert tuple(scale.get_usd_prop().Get()) == (1.0, 3.0)

    _right_click(_row(widget, scale[1]), monkeypatch, pick=REVERT)
    env["settle"]()
    # Both back: as if the vector was never edited.
    assert scale.value == (1.0, 1.0)
    assert _layers(env["stage"]) == before


def test_a_button_has_no_item(env, monkeypatch):  # noqa: F811
    node = env["mk"]("transform")
    env["settle"]()
    # Built on its own: the button sits in Pivot, which starts collapsed.
    row = LabeledWidget(ChunkPushButtonWidget(node.get_param("center_pivot")))

    offered = _right_click(row, monkeypatch)

    assert REVERT not in offered


def test_a_file_value_shows_through_and_the_file_is_untouched(composed_shot):  # noqa: F811
    _, stage, scene, seq_layer, _, transform_path = composed_shot
    tx = _tx_param(scene)
    assert not can_revert_to_default(tx)
    tx.value = _SHOT_VALUE
    assert can_revert_to_default(tx)

    revert_param_to_default(tx)

    assert tx.value == _SEQ_VALUE
    attr = stage.GetAttributeAtPath(f"{transform_path}.{_TX}")
    assert [s.layer for s in attr.GetPropertyStack(Usd.TimeCode.Default())] == [seq_layer]
    assert not seq_layer.dirty


def test_undo_brings_the_edit_back_and_redo_reverts_again(env):  # noqa: F811
    node, _ = _uv_transform(env)
    history = chunk_undo.History(env["stage"])
    history.listen()
    try:
        rotation = node.get_param("rotation")
        rotation.value = 45.0
        env["settle"]()
        edited = history.key()
        revert_param_to_default(rotation)
        env["settle"]()
        reverted = history.key()

        history.move_to(edited)
        env["settle"]()
        assert rotation.value == 45.0
        assert rotation.get_usd_prop().Get() == 45.0

        history.move_to(reverted)
        env["settle"]()
        assert rotation.value == 0.0
        assert not env["stage"].GetAttributeAtPath(rotation._param_attr_path or "/x.y")
    finally:
        history.close()
