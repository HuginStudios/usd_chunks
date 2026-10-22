"""Clear Param clears a param in the layer its edits go to, and names the layers that still set it."""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from PySide6 import QtCore, QtGui, QtWidgets
from pxr import Sdf, Usd

from tests.test_override_through_composition import (  # noqa: F401 -- composed_shot is a fixture
    _SEQ_VALUE, _SHOT_VALUE, _TX, _tx_param, composed_shot,
)
from tests.test_unloft import env  # noqa: F401
from usd_chunks import chunk_core, chunk_undo
from usd_chunks.chunk_stage import can_clear_param, clear_param, set_param_default_in
from usd_chunks.frontends.qt import widgets
from usd_chunks.frontends.qt.widgets import ChunkPushButtonWidget, ChunkWidget, LabeledWidget

CLEAR = "Clear Param"


@dataclass
class _Prompts:
    """The questions Clear Param asked, as (text, detail), and the answer it gets."""

    asked: list = field(default_factory=list)
    answer: bool = False


@pytest.fixture
def prompts(monkeypatch) -> _Prompts:
    """Stands in for the Yes/No dialog, so a test can see and answer what it asks."""
    record = _Prompts()

    def _confirm(msg, title=None, detail=None):
        record.asked.append((msg, detail))
        return record.answer

    monkeypatch.setattr(widgets, "confirm_dial", _confirm)
    return record


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


def _dept_layer(env) -> Sdf.Layer:  # noqa: F811
    """A department layer, sublayered under the stage's root: weaker, and the stage's own."""
    dept = Sdf.Layer.CreateAnonymous("dept")
    env["stage"].GetRootLayer().subLayerPaths.append(dept.identifier)
    return dept


def _edited_with_dept_value(env, value):  # noqa: F811
    """A UV Transform whose rotation is 45 in the root, and value in a department layer.

    Returns:
        The rotation param, its row's widget and the department layer.
    """
    node, widget = _uv_transform(env)
    rotation = node.get_param("rotation")
    rotation.value = 45.0
    env["settle"]()
    dept = _dept_layer(env)
    with Usd.EditContext(env["stage"], dept):
        env["stage"].GetAttributeAtPath(rotation._param_attr_path).Set(value)
    env["settle"]()
    return rotation, widget, dept


def test_a_cleared_slider_leaves_nothing_in_usd(env, monkeypatch, prompts):  # noqa: F811
    node, widget = _uv_transform(env)
    before = _layers(env["stage"])
    rotation = node.get_param("rotation")
    rotation.value = 45.0
    env["settle"]()
    assert _layers(env["stage"]) != before

    offered = _right_click(_row(widget, rotation), monkeypatch, pick=CLEAR)
    env["settle"]()

    assert offered[CLEAR] is True
    assert rotation.value == 0.0
    assert rotation.get_usd_prop() is None
    assert _layers(env["stage"]) == before
    assert prompts.asked == []


def test_an_edit_after_a_clear_is_stored(env, monkeypatch):  # noqa: F811
    node, widget = _uv_transform(env)
    rotation = node.get_param("rotation")
    rotation.value = 45.0
    env["settle"]()
    _right_click(_row(widget, rotation), monkeypatch, pick=CLEAR)
    env["settle"]()

    rotation.value = 30.0
    env["settle"]()

    assert rotation.get_usd_prop().Get() == 30.0


def test_the_item_is_greyed_out_on_an_untouched_param(env, monkeypatch):  # noqa: F811
    node, widget = _uv_transform(env)

    offered = _right_click(_row(widget, node.get_param("rotation")), monkeypatch)

    assert offered[CLEAR] is False


def test_a_cleared_wire_leaves_no_connection(env, monkeypatch):  # noqa: F811
    source = env["mk"]("uv_transform", name="Source")
    source.get_param("rotation").value = 30.0
    node, widget = _uv_transform(env)
    before = _layers(env["stage"])
    rotation = node.get_param("rotation")
    rotation.value = f"@{source.name}.rotation@"
    env["settle"]()
    assert rotation.value == 30.0
    assert rotation.get_usd_prop().GetConnections()

    offered = _right_click(_row(widget, rotation), monkeypatch, pick=CLEAR)
    env["settle"]()

    assert offered[CLEAR] is True
    assert rotation.raw_value == 0.0
    assert _layers(env["stage"]) == before


def test_only_the_edit_layer_is_cleared_and_the_others_are_named(env, monkeypatch, prompts):  # noqa: F811
    rotation, widget, dept = _edited_with_dept_value(env, 10.0)
    dept_before = dept.ExportToString()
    path = Sdf.Path(rotation._param_attr_path)

    _right_click(_row(widget, rotation), monkeypatch, pick=CLEAR)
    env["settle"]()

    assert env["stage"].GetRootLayer().GetPropertyAtPath(path) is None
    assert dept.ExportToString() == dept_before
    assert rotation.value == 10.0
    [(msg, detail)] = prompts.asked
    assert "dept" in msg and "shows 10" in msg
    assert "default, 0," in detail


def test_the_default_can_be_set_over_the_others(env, monkeypatch, prompts):  # noqa: F811
    rotation, widget, dept = _edited_with_dept_value(env, 10.0)
    dept_before = dept.ExportToString()
    prompts.answer = True

    _right_click(_row(widget, rotation), monkeypatch, pick=CLEAR)
    env["settle"]()

    assert rotation.value == 0.0
    root = env["stage"].GetRootLayer()
    assert root.GetAttributeAtPath(rotation._param_attr_path).default == 0.0
    assert dept.ExportToString() == dept_before
    # The next edit goes where the default went.
    rotation.value = 20.0
    env["settle"]()
    assert root.GetAttributeAtPath(rotation._param_attr_path).default == 20.0
    assert dept.ExportToString() == dept_before


def test_the_default_hides_a_wire_in_another_layer(env):  # noqa: F811
    source = env["mk"]("uv_transform", name="Source")
    source.get_param("rotation").value = 30.0
    node, _ = _uv_transform(env)
    rotation = node.get_param("rotation")
    rotation.value = f"@{source.name}.rotation@"
    env["settle"]()
    # Move the wire into a department layer, and put a plain value over it in the root.
    stage = env["stage"]
    root = stage.GetRootLayer()
    path = Sdf.Path(rotation._param_attr_path)
    dept = _dept_layer(env)
    Sdf.CreatePrimInLayer(dept, path.GetPrimPath())
    assert Sdf.CopySpec(root, path, dept, path)
    del root.GetPrimAtPath(path.GetPrimPath()).properties[path.name]
    with Usd.EditContext(stage, root):
        stage.GetAttributeAtPath(path).Set(45.0)
        stage.GetAttributeAtPath(path).SetConnections([])
    env["settle"]()

    cleared = clear_param(rotation)
    env["settle"]()
    assert cleared.others == (dept,)
    assert stage.GetAttributeAtPath(path).GetConnections()  # the department's wire shows again

    set_param_default_in(rotation, cleared.layer)
    env["settle"]()

    attr = stage.GetAttributeAtPath(path)
    assert attr.GetConnections() == []
    assert attr.Get() == 0.0
    assert rotation.raw_value == 0.0


def test_a_cleared_vector_leaves_nothing_in_usd(env, monkeypatch):  # noqa: F811
    node, widget = _uv_transform(env)
    before = _layers(env["stage"])
    scale = node.get_param("scale")
    assert isinstance(scale, chunk_core.ChunkVectorBase)
    scale.value = (2.0, 3.0)
    env["settle"]()

    # On the header, the vector's name: the click goes up from there to the vector's row.
    header = _row(widget, scale).widget.minimize_button
    offered = _right_click(header, monkeypatch, pick=CLEAR)
    env["settle"]()

    assert offered == {CLEAR: True}
    assert scale.value == (1.0, 1.0)
    assert _layers(env["stage"]) == before


def test_vector_fields_clear_one_at_a_time(env, monkeypatch):  # noqa: F811
    node, widget = _uv_transform(env)
    before = _layers(env["stage"])
    scale = node.get_param("scale")
    scale.value = (2.0, 3.0)
    env["settle"]()

    _right_click(_row(widget, scale[0]), monkeypatch, pick=CLEAR)
    env["settle"]()
    # The other field is still edited, so the vector keeps its attr.
    assert scale.value == (1.0, 3.0)
    assert tuple(scale.get_usd_prop().Get()) == (1.0, 3.0)

    _right_click(_row(widget, scale[1]), monkeypatch, pick=CLEAR)
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

    assert CLEAR not in offered


def test_a_file_value_shows_through_and_the_file_is_untouched(composed_shot):  # noqa: F811
    _, stage, scene, seq_layer, _, transform_path = composed_shot
    tx = _tx_param(scene)
    tx.value = _SHOT_VALUE

    cleared = clear_param(tx)

    assert tx.value == _SEQ_VALUE
    assert cleared.others == (seq_layer,)
    assert cleared.default_can_win
    attr = stage.GetAttributeAtPath(f"{transform_path}.{_TX}")
    assert [s.layer for s in attr.GetPropertyStack(Usd.TimeCode.Default())] == [seq_layer]
    assert not seq_layer.dirty


def test_the_default_can_be_set_over_a_file_value(composed_shot):  # noqa: F811
    _, stage, scene, seq_layer, _, transform_path = composed_shot
    tx = _tx_param(scene)
    # Nothing in the shot yet: only the sequence sets it.
    assert can_clear_param(tx)

    cleared = clear_param(tx)
    assert cleared.others == (seq_layer,)
    set_param_default_in(tx, cleared.layer)

    assert tx.value == tx.default_value
    assert stage.GetAttributeAtPath(f"{transform_path}.{_TX}").Get() == tx.default_value
    assert not seq_layer.dirty


def test_undo_brings_the_edit_back_and_redo_clears_again(env):  # noqa: F811
    node, _ = _uv_transform(env)
    history = chunk_undo.History(env["stage"])
    history.listen()
    try:
        rotation = node.get_param("rotation")
        rotation.value = 45.0
        env["settle"]()
        edited = history.key()
        clear_param(rotation)
        env["settle"]()
        cleared = history.key()

        history.move_to(edited)
        env["settle"]()
        assert rotation.value == 45.0
        assert rotation.get_usd_prop().Get() == 45.0

        history.move_to(cleared)
        env["settle"]()
        assert rotation.value == 0.0
        assert not env["stage"].GetAttributeAtPath(rotation._param_attr_path or "/x.y")
    finally:
        history.close()
