"""frontends/qt/undo.py: the application's input is what splits undo steps."""
from __future__ import annotations

import pytest
from pxr import Sdf, Usd

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

from usd_chunks.frontends.qt import undo as qt_undo  # noqa: E402


@pytest.fixture
def installed(qapp):
    stage = Usd.Stage.CreateInMemory()
    spec = Sdf.CreatePrimInLayer(stage.GetRootLayer(), "/chunks/G")
    spec.specifier, spec.typeName = Sdf.SpecifierDef, "Chunk"
    Sdf.AttributeSpec(spec, "inputs:size", Sdf.ValueTypeNames.Double).default = 1.0
    stack, watcher = qt_undo.install(stage)
    yield stage, stack
    qapp.removeEventFilter(watcher)
    stack.close()


def _set(stage, value):
    stage.GetRootLayer().GetAttributeAtPath("/chunks/G.inputs:size").default = value


def _key(key, modifiers=QtCore.Qt.KeyboardModifier.NoModifier):
    return QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, key, modifiers)


def _send(event):
    QtWidgets.QApplication.sendEvent(QtWidgets.QApplication.instance(), event)


def test_a_key_press_starts_a_step(installed):
    stage, stack = installed
    _send(_key(QtCore.Qt.Key.Key_Return))
    _set(stage, 5.0)
    assert stack.can_undo()


def test_ctrl_z_and_a_held_modifier_are_not_input(installed):
    """Otherwise the recook after an undo would start a step and throw redo away."""
    stage, stack = installed
    _send(_key(QtCore.Qt.Key.Key_Return))
    _set(stage, 5.0)
    stack.undo()
    _send(_key(QtCore.Qt.Key.Key_Control, QtCore.Qt.KeyboardModifier.ControlModifier))
    _send(_key(QtCore.Qt.Key.Key_Z, QtCore.Qt.KeyboardModifier.ControlModifier))
    _set(stage, 1.5)  # the recook
    assert stack.can_redo()
