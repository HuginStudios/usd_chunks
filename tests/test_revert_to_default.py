"""Right-click Revert to Default on a param row puts the param back to its default value."""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from tests.test_unloft import env  # noqa: F401
from usd_chunks import chunk_core
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


def _uv_transform(env):  # noqa: F811 -- the imported fixture
    node = env["mk"]("uv_transform")
    widget = ChunkWidget(node)
    widget.resize(400, 600)
    widget.show()
    env["settle"]()
    return node, widget


def test_a_changed_slider_reverts_to_its_default(env, monkeypatch):  # noqa: F811
    node, widget = _uv_transform(env)
    rotation = node.get_param("rotation")
    rotation.value = 45.0
    env["settle"]()

    offered = _right_click(_row(widget, rotation), monkeypatch, pick=REVERT)
    env["settle"]()

    assert offered[REVERT] is True
    assert rotation.value == 0.0
    assert rotation.get_usd_prop().Get() == 0.0


def test_the_item_is_greyed_out_at_the_default(env, monkeypatch):  # noqa: F811
    node, widget = _uv_transform(env)

    offered = _right_click(_row(widget, node.get_param("rotation")), monkeypatch)

    assert offered[REVERT] is False


def test_an_expression_reverts_to_the_default(env, monkeypatch):  # noqa: F811
    source = env["mk"]("uv_transform", name="Source")
    source.get_param("rotation").value = 30.0
    node, widget = _uv_transform(env)
    rotation = node.get_param("rotation")
    rotation.value = f"@{source.name}.rotation@"
    env["settle"]()
    assert rotation.value == 30.0

    offered = _right_click(_row(widget, rotation), monkeypatch, pick=REVERT)
    env["settle"]()

    assert offered[REVERT] is True
    assert rotation.raw_value == 0.0


def test_a_changed_vector_reverts_from_its_header(env, monkeypatch):  # noqa: F811
    node, widget = _uv_transform(env)
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
    assert tuple(scale.get_usd_prop().Get()) == (1.0, 1.0)


def test_one_field_of_a_vector_reverts_alone(env, monkeypatch):  # noqa: F811
    node, widget = _uv_transform(env)
    scale = node.get_param("scale")
    scale.value = (2.0, 3.0)
    env["settle"]()

    _right_click(_row(widget, scale[0]), monkeypatch, pick=REVERT)
    env["settle"]()

    assert scale.value == (1.0, 3.0)


def test_a_button_has_no_item(env, monkeypatch):  # noqa: F811
    node = env["mk"]("transform")
    env["settle"]()
    # Built on its own: the button sits in Pivot, which starts collapsed.
    row = LabeledWidget(ChunkPushButtonWidget(node.get_param("center_pivot")))

    offered = _right_click(row, monkeypatch)

    assert REVERT not in offered
