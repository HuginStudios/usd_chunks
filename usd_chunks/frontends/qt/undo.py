"""Ctrl+Z and Ctrl+Shift+Z for chunk networks in a Qt app with no undo of its own (usdview).

`chunk_undo.UndoStack` makes a step of everything from one user input to the next. This
watches the whole application for that input -- a mouse press, a key, the wheel -- so every
widget, the node graph and the viewport's gizmos are covered without a hook in each. The
host binds the keys (usdview: its Edit menu, hosts/usdview).

Only chunk prims are undone (docs/adr/0003); the C++ usd-undo plugin covers everything
else, and a host loads one or the other, never both.
"""
from __future__ import annotations

from PySide6 import QtCore, QtGui

from usd_chunks import chunk_undo

#: Keys that are not input on their own: a modifier held on the way to Ctrl+Z.
_MODIFIER_KEYS = frozenset({
    QtCore.Qt.Key.Key_Control, QtCore.Qt.Key.Key_Shift, QtCore.Qt.Key.Key_Alt,
    QtCore.Qt.Key.Key_Meta, QtCore.Qt.Key.Key_AltGr,
})


def _is_undo_key(event: QtGui.QKeyEvent) -> bool:
    return event.key() == QtCore.Qt.Key.Key_Z and \
        bool(event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier)


class InputWatcher(QtCore.QObject):
    """Tells an UndoStack about each user input, application-wide.

    Args:
        stack: The stack to tell.
    """

    _INPUT = frozenset({QtCore.QEvent.Type.MouseButtonPress,
                        QtCore.QEvent.Type.MouseButtonDblClick,
                        QtCore.QEvent.Type.Wheel})

    def __init__(self, stack: chunk_undo.UndoStack):
        super().__init__()
        self.stack = stack

    def eventFilter(self, _obj, event) -> bool:  # noqa: N802 - Qt's name
        kind = event.type()
        if kind in self._INPUT:
            self.stack.note_input()
        elif kind == QtCore.QEvent.Type.KeyPress and not event.isAutoRepeat() \
                and event.key() not in _MODIFIER_KEYS and not _is_undo_key(event):
            # Ctrl+Z itself is not an edit: a cook that lands after it belongs to the
            # step it went back to.
            self.stack.note_input()
        return False


def install(stage) -> tuple[chunk_undo.UndoStack, InputWatcher]:
    """An UndoStack for `stage`, fed by the application's input.

    Returns:
        (stack, watcher). Keep both: the watcher stops when it is collected.
    """
    stack = chunk_undo.UndoStack(stage)
    watcher = InputWatcher(stack)
    app = QtCore.QCoreApplication.instance()
    if app is not None:
        app.installEventFilter(watcher)
    return stack, watcher
