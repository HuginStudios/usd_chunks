"""Backdrops and sticky notes on the Qt node graph canvas.

What they are and how they are stored: usd_chunks/graph/backdrops.py. This module draws
them and lets you move, resize and edit them. It does not import nodegraph.py; it calls
back into the scene through BackdropScene.
"""

from __future__ import annotations

from typing import Protocol

from pxr import Usd
from PySide6 import QtCore, QtGui, QtWidgets

from usd_chunks.frontends.qt import node_theme
from usd_chunks.graph import backdrops

_RADIUS = 8.0
_GRIP = 14.0  # size of the resize corner, bottom right
_BORDER = 6.0  # how far in from the edge a backdrop can still be grabbed
_TEXT_PAD = 10.0
_FILL_ALPHA = 60  # a backdrop's body is see-through, so the grid and wires still show
_HEADER_ALPHA = 200
_Z_BACKDROP = -3.0  # under the wires (-1) and the nodes (0 and up)
_Z_NOTE = -2.0


class BackdropScene(Protocol):
    """What a backdrop needs from the scene it sits in."""

    def drag_members(self, item: BackdropItem) -> list[QtWidgets.QGraphicsItem]:
        """What moves when this backdrop is dragged, itself left out."""

    def select_backdrop(self, item: BackdropItem, toggle: bool) -> None:
        """Select this backdrop: alone, or toggled in or out of the selection."""

    def backdrop_moved(self, item: BackdropItem, members: list[QtWidgets.QGraphicsItem]) -> None:
        """Save a finished drag: the backdrop's own rect and its members' positions."""

    def edit_backdrop_text(self, item: BackdropItem) -> QtWidgets.QWidget | None:
        """Open the text editor for this backdrop or note."""


def text_color_on(fill: QtGui.QColor) -> QtGui.QColor:
    """Dark text on a light colour, light text on a dark one.

    Args:
        fill: The colour behind the text.

    Returns:
        The colour to draw the text in.
    """
    return QtGui.QColor("#202020") if fill.lightnessF() > 0.55 else node_theme.TEXT_PRIMARY


class BackdropItem(QtWidgets.QGraphicsItem):
    """One backdrop or sticky note on the canvas.

    A backdrop is grabbed by its title bar or its edge; its middle is left to the canvas,
    so a click there still starts a box-select and a right-click still makes a chunk. A
    note is small, so all of it can be grabbed. Both resize from the bottom-right corner.

    Args:
        prim: Its Backdrop prim.
        locked: True when it comes from a layer this scene must not edit.
    """

    def __init__(self, prim: Usd.Prim, locked: bool = False):
        super().__init__()
        self.prim = prim
        self.locked = locked
        self.selected = False
        self.info = backdrops.read(prim)
        self._mode: str | None = None  # "move" or "resize" while the mouse is down
        self._press_scene = QtCore.QPointF()
        self._start_rect: backdrops.Rect = self.info.rect
        self._members: list[tuple[QtWidgets.QGraphicsItem, QtCore.QPointF]] = []
        self._moved = False
        self.setAcceptHoverEvents(True)
        self._apply_info()

    # -- state ----------------------------------------------------------------------

    @property
    def is_note(self) -> bool:
        return self.info.note

    def changed_in(self, notice: Usd.Notice.ObjectsChanged) -> bool:
        """Whether a stage change touched anything this is drawn from."""
        if not self.prim or not self.prim.IsValid():
            return False
        return any(notice.AffectedObject(self.prim.GetAttribute(name)) for name in backdrops.WATCHED_ATTRS)

    def rect(self) -> backdrops.Rect:
        """Where it is now, in scene coordinates, mid-drag included."""
        _, _, w, h = self.info.rect
        return self.pos().x(), self.pos().y(), w, h

    def refresh(self) -> None:
        """Read the prim again and redraw."""
        if not self.prim or not self.prim.IsValid():
            return
        self.info = backdrops.read(self.prim)
        self._apply_info()

    def set_locked(self, locked: bool) -> None:
        if locked != self.locked:
            self.locked = locked
            self.update()

    def set_selected(self, selected: bool) -> None:
        if selected != self.selected:
            self.selected = selected
            self.update()

    def _apply_info(self) -> None:
        self.prepareGeometryChange()
        x, y, w, h = self.info.rect
        self.setPos(x, y)
        if self.is_note:
            self.setZValue(_Z_NOTE)
        else:
            # Smaller on top, so a backdrop inside a bigger one can still be grabbed.
            self.setZValue(_Z_BACKDROP - min(w * h, 1e8) / 1e9)
        self.update()

    def _size(self) -> tuple[float, float]:
        return self.info.rect[2], self.info.rect[3]

    def _color(self) -> QtGui.QColor:
        return QtGui.QColor.fromRgbF(*self.info.color)

    # -- geometry -------------------------------------------------------------------

    def boundingRect(self) -> QtCore.QRectF:  # noqa: N802
        w, h = self._size()
        return QtCore.QRectF(-2, -2, w + 4, h + 4)

    def _grip_rect(self) -> QtCore.QRectF:
        w, h = self._size()
        return QtCore.QRectF(w - _GRIP, h - _GRIP, _GRIP, _GRIP)

    def _header_rect(self) -> QtCore.QRectF:
        w, _ = self._size()
        return QtCore.QRectF(0, 0, w, backdrops.HEADER_HEIGHT)

    def shape(self) -> QtGui.QPainterPath:
        """The part that takes clicks: a note's whole body, a backdrop's frame only."""
        w, h = self._size()
        path = QtGui.QPainterPath()
        if self.is_note:
            path.addRoundedRect(QtCore.QRectF(0, 0, w, h), _RADIUS, _RADIUS)
            return path
        outer = QtGui.QPainterPath()
        outer.addRect(QtCore.QRectF(0, 0, w, h))
        inner = QtGui.QPainterPath()
        top = backdrops.HEADER_HEIGHT
        inner.addRect(QtCore.QRectF(_BORDER, top, max(w - 2 * _BORDER, 0), max(h - top - _BORDER, 0)))
        grip = QtGui.QPainterPath()
        grip.addRect(self._grip_rect())
        # united, not addRect: where the grip overlaps the frame, an added rect would make
        # a hole under the default odd-even fill, right where the grip is drawn.
        return outer.subtracted(inner).united(grip)

    # -- drawing --------------------------------------------------------------------

    def paint(self, painter: QtGui.QPainter, option, widget=None) -> None:
        w, h = self._size()
        color = self._color()
        rect = QtCore.QRectF(0, 0, w, h)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        if self.selected:
            border = QtGui.QPen(node_theme.NODE_SELECTED, 2)
        else:
            border = QtGui.QPen(color.lighter(130), 1)
        if self.locked:
            border.setStyle(QtCore.Qt.PenStyle.DashLine)

        if self.is_note:
            painter.setPen(border)
            painter.setBrush(color)
            painter.drawRoundedRect(rect, _RADIUS, _RADIUS)
            self._draw_text(painter, rect.adjusted(_TEXT_PAD, _TEXT_PAD, -_TEXT_PAD, -_TEXT_PAD),
                            self.info.text or "Double-click to write a note", color, wrap=True,
                            placeholder=not self.info.text)
        else:
            body = QtGui.QColor(color)
            body.setAlpha(_FILL_ALPHA)
            painter.setPen(border)
            painter.setBrush(body)
            painter.drawRoundedRect(rect, _RADIUS, _RADIUS)
            header = QtGui.QColor(color)
            header.setAlpha(_HEADER_ALPHA)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(header)
            path = QtGui.QPainterPath()
            path.addRoundedRect(self._header_rect(), _RADIUS, _RADIUS)
            # Square off the bottom corners of the title bar, where it meets the body.
            square = QtGui.QPainterPath()
            square.addRect(QtCore.QRectF(0, backdrops.HEADER_HEIGHT / 2, w, backdrops.HEADER_HEIGHT / 2))
            painter.drawPath(path.united(square))
            self._draw_text(painter, self._header_rect().adjusted(_TEXT_PAD, 0, -_TEXT_PAD, 0),
                            self.info.text or "Backdrop", header, wrap=False, bold=True,
                            placeholder=not self.info.text)

        if not self.locked:
            grip = QtGui.QColor(text_color_on(color) if self.is_note else color.lighter(150))
            grip.setAlpha(140)
            painter.setPen(QtGui.QPen(grip, 1.5))
            for step in (4, 8):
                painter.drawLine(QtCore.QPointF(w - step, h - 2), QtCore.QPointF(w - 2, h - step))

    def _draw_text(self, painter, rect, text, fill, wrap, bold=False, placeholder=False) -> None:
        font = QtGui.QFont(painter.font())
        font.setPointSizeF(13 if bold else 11)
        font.setBold(bold)
        font.setItalic(placeholder)
        painter.setFont(font)
        pen = text_color_on(fill)
        if placeholder:
            pen = QtGui.QColor(pen)
            pen.setAlpha(120)
        painter.setPen(pen)
        if wrap:
            flags = QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop | QtCore.Qt.TextFlag.TextWordWrap
            painter.drawText(rect, int(flags), text)
        else:
            elided = QtGui.QFontMetrics(font).elidedText(text, QtCore.Qt.TextElideMode.ElideRight, int(rect.width()))
            painter.drawText(rect, int(QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.AlignmentFlag.AlignLeft), elided)

    # -- mouse ----------------------------------------------------------------------

    def hoverMoveEvent(self, event) -> None:  # noqa: N802
        if not self.locked and self._grip_rect().contains(event.pos()):
            self.setCursor(QtCore.Qt.CursorShape.SizeFDiagCursor)
        else:
            self.unsetCursor()
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        scene = self.scene()
        toggle = bool(event.modifiers() & (QtCore.Qt.KeyboardModifier.ControlModifier
                                           | QtCore.Qt.KeyboardModifier.ShiftModifier))
        if scene is not None and hasattr(scene, "select_backdrop"):
            scene.select_backdrop(self, toggle)
        self._moved = False
        self._mode = None
        if not self.locked and not toggle:
            self._press_scene = event.scenePos()
            self._start_rect = self.rect()
            if self._grip_rect().contains(event.pos()):
                self._mode = "resize"
            else:
                self._mode = "move"
                members = scene.drag_members(self) if scene is not None else []
                self._members = [(item, item.pos()) for item in members]
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._mode is None:
            super().mouseMoveEvent(event)
            return
        delta = event.scenePos() - self._press_scene
        if not self._moved and abs(delta.x()) + abs(delta.y()) <= 2:
            return
        self._moved = True
        x, y, w, h = self._start_rect
        if self._mode == "resize":
            min_w, min_h = backdrops.MIN_SIZE
            self.prepareGeometryChange()
            self.info.rect = (x, y, max(w + delta.x(), min_w), max(h + delta.y(), min_h))
            self.update()
        else:
            self.setPos(x + delta.x(), y + delta.y())
            for item, start in self._members:
                try:
                    item.setPos(start + delta)
                except RuntimeError:
                    continue
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._mode is None:
            super().mouseReleaseEvent(event)
            return
        mode, self._mode = self._mode, None
        members = [item for item, _ in self._members]
        self._members = []
        if self._moved:
            if mode == "move":
                x, y = self.pos().x(), self.pos().y()
                self.info.rect = (x, y, *self._size())
            scene = self.scene()
            if scene is not None and hasattr(scene, "backdrop_moved"):
                scene.backdrop_moved(self, members if mode == "move" else [])
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        scene = self.scene()
        if not self.locked and scene is not None and hasattr(scene, "edit_backdrop_text"):
            # After this event has finished: a popup opened from inside a mouse handler has
            # crashed the graph before (#86).
            QtCore.QTimer.singleShot(0, lambda: self.prim.IsValid() and scene.edit_backdrop_text(self))
        event.accept()


class BackdropTextPopup(QtWidgets.QFrame):
    """Edits a backdrop's title or a note's text, laid over it on the canvas.

    A popup, like the Tab menu, so it gets the keyboard even while usdview holds focus.
    Clicking outside or pressing Enter keeps the text (Ctrl+Enter for a note, where Enter
    starts a new line). Esc throws the edit away.

    Args:
        view: The graph view it opens over.
        item: The backdrop or note to edit.
    """

    def __init__(self, view: QtWidgets.QGraphicsView, item: BackdropItem):
        super().__init__(view, QtCore.Qt.WindowType.Popup)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        self._item = item
        self._cancelled = False
        if item.is_note:
            self.field = QtWidgets.QPlainTextEdit(item.info.text)
            self.field.setPlaceholderText("What is this part of the network for?")
        else:
            self.field = QtWidgets.QLineEdit(item.info.text, placeholderText="Backdrop title")
            self.field.selectAll()
        self.field.installEventFilter(self)
        hint = QtWidgets.QLabel("Ctrl+Enter to keep, Esc to cancel" if item.is_note
                                else "Enter to keep, Esc to cancel")
        hint.setStyleSheet("color: #8b8f98;")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.field)
        layout.addWidget(hint)
        # Over the item, the size it is on screen (within reason), and kept inside the view:
        # a wide backdrop scrolled half out of sight has its corner off every screen.
        x, y, w, h = item.rect()
        viewport = view.viewport()
        top_left = viewport.mapToGlobal(view.mapFromScene(QtCore.QPointF(x, y)))
        scale = view.transform().m11() or 1.0
        width = int(min(max(w * scale, 240), 600))
        self.setFixedWidth(width)
        if item.is_note:
            self.field.setFixedHeight(int(min(max(h * scale, 120), 400)))
        self.adjustSize()
        area = QtCore.QRect(viewport.mapToGlobal(QtCore.QPoint(0, 0)), viewport.size())
        if screen := (QtGui.QGuiApplication.screenAt(area.center()) or view.screen()):
            area = area.intersected(screen.availableGeometry())
        top_left.setX(max(area.left(), min(top_left.x(), area.right() - self.width())))
        top_left.setY(max(area.top(), min(top_left.y(), area.bottom() - self.height())))
        self.move(top_left)

    def popup(self) -> None:
        self.show()
        self.field.setFocus()

    def text(self) -> str:
        if isinstance(self.field, QtWidgets.QPlainTextEdit):
            return self.field.toPlainText()
        return self.field.text()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.field and event.type() == QtCore.QEvent.Type.KeyPress:
            key = event.key()
            if key == QtCore.Qt.Key.Key_Escape:
                self._cancelled = True
                self.close()
                return True
            if key in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
                ctrl = bool(event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier)
                if ctrl or not self._item.is_note:
                    self.close()
                    return True
        return super().eventFilter(obj, event)

    def hideEvent(self, event) -> None:  # noqa: N802
        # Every way out but Esc keeps the text, a click outside included.
        if not self._cancelled:
            self._cancelled = True  # once: close() after a hide must not write twice
            item = self._item
            text = self.text()
            if item.prim.IsValid() and text != item.info.text:
                backdrops.set_text(item.prim, text)
        super().hideEvent(event)
