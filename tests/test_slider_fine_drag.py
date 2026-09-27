from __future__ import annotations

from PySide6 import QtCore, QtGui

from usd_chunks.frontends.qt.widgets import _FineDragSlider


def _mouse_event(etype, pos, modifiers=QtCore.Qt.KeyboardModifier.NoModifier):
    p = QtCore.QPointF(*pos)
    return QtGui.QMouseEvent(
        etype, p, p,
        QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.MouseButton.LeftButton, modifiers,
    )


def _make_slider(qapp, width=200):
    slider = _FineDragSlider(QtCore.Qt.Orientation.Horizontal)
    slider.setRange(-1000, 1000)
    slider.resize(width, 24)
    slider.setValue(0)
    return slider


def test_ctrl_drag_moves_value_at_reduced_sensitivity(qapp) -> None:
    slider = _make_slider(qapp)
    ctrl = QtCore.Qt.KeyboardModifier.ControlModifier

    slider.mousePressEvent(_mouse_event(QtCore.QEvent.Type.MouseButtonPress, (50, 10), ctrl))
    assert slider._fine_dragging is True

    slider.mouseMoveEvent(_mouse_event(QtCore.QEvent.Type.MouseMove, (100, 10), ctrl))
    slider.mouseReleaseEvent(_mouse_event(QtCore.QEvent.Type.MouseButtonRelease, (100, 10), ctrl))

    # 50px moved, range 2000 over a 200px-wide groove -> 10 units/px at full speed,
    # * 0.1 sensitivity = 1 unit/px = 50 units total.
    assert slider.value() == 50
    assert slider._fine_dragging is False


def test_plain_drag_does_not_enter_fine_mode(qapp) -> None:
    """A plain (no-Ctrl) press must fall through to QSlider's own default handling
    (click-to-jump + normal-speed drag), completely untouched by the fine-drag logic."""
    slider = _make_slider(qapp)

    slider.mousePressEvent(_mouse_event(QtCore.QEvent.Type.MouseButtonPress, (50, 10)))
    assert slider._fine_dragging is False


def test_ctrl_drag_clamps_to_slider_range(qapp) -> None:
    slider = _make_slider(qapp)
    ctrl = QtCore.Qt.KeyboardModifier.ControlModifier

    slider.mousePressEvent(_mouse_event(QtCore.QEvent.Type.MouseButtonPress, (0, 10), ctrl))
    # A huge rightward drag, at reduced sensitivity, would still overshoot the max --
    # must clamp rather than wrap or exceed it.
    slider.mouseMoveEvent(_mouse_event(QtCore.QEvent.Type.MouseMove, (100000, 10), ctrl))

    assert slider.value() == slider.maximum() == 1000


def test_ctrl_drag_is_slower_than_a_full_speed_equivalent(qapp) -> None:
    """Sanity-check the whole point of this feature: the same pixel distance moves the
    value by less under Ctrl than it would at the slider's normal (Qt-default) speed."""
    slider = _make_slider(qapp)
    ctrl = QtCore.Qt.KeyboardModifier.ControlModifier

    slider.mousePressEvent(_mouse_event(QtCore.QEvent.Type.MouseButtonPress, (50, 10), ctrl))
    slider.mouseMoveEvent(_mouse_event(QtCore.QEvent.Type.MouseMove, (100, 10), ctrl))
    fine_delta = slider.value()

    full_speed_delta = 50 * ((slider.maximum() - slider.minimum()) / slider.width())
    assert abs(fine_delta) < abs(full_speed_delta)
