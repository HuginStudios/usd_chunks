"""Which chunks made or changed the prims selected in the viewport, shown in the node graph.

The nodes involved get a ring and a tag: "made" for the chunk that made a prim, a number for
each chunk that changed it (1 first, in the order the changes apply), "brought in" for the
chunk whose asset a prim sits inside. A line at the foot of the graph names them, with links
to jump to each. The answer comes from chunk_trace, read only when the selection changes.
"""

from __future__ import annotations

import html

from PySide6 import QtCore, QtGui, QtWidgets

from usd_chunks import chunk_trace

#: Ring and tag colour per role.
ROLE_COLOURS = {"made": "#f2b134", "changed": "#5fc4d9", "via": "#b39ddb"}
#: How far the ring sits outside the node.
_RING_GAP = 5.0
#: The most chunk names the line spells out per role before "+N more".
_NAMES_SHOWN = 6
#: A selected prim's path longer than this is shortened in the line (the tooltip has it whole).
_LONGEST_PATH = 60


class TraceMark(QtWidgets.QGraphicsItem):
    """A ring round a node, with a tag saying what it did to the selected prims.

    A child item, made only for the few nodes involved: the rest of the graph draws exactly
    as before.
    """

    def __init__(self, node: QtWidgets.QGraphicsItem) -> None:
        super().__init__(node)
        self.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
        self.setAcceptHoverEvents(False)
        self._rect = QtCore.QRectF()
        self._role = "made"
        self._label = ""
        self._font = QtGui.QFont("Arial", 8, QtGui.QFont.Weight.Bold)

    def set(self, role: str, label: str, rect: QtCore.QRectF) -> None:
        """Show role ("made", "changed" or "via") with label, round a node of rect."""
        self.prepareGeometryChange()
        self._role, self._label, self._rect = role, label, QtCore.QRectF(rect)
        self.update()

    def _tag_rect(self) -> QtCore.QRectF:
        metrics = QtGui.QFontMetricsF(self._font)
        width = metrics.horizontalAdvance(self._label) + 10
        height = metrics.height() + 4
        ring = self._rect.adjusted(-_RING_GAP, -_RING_GAP, _RING_GAP, _RING_GAP)
        return QtCore.QRectF(ring.left() + 6, ring.top() - height + 1, width, height)

    def boundingRect(self) -> QtCore.QRectF:  # noqa: N802 - Qt's name
        ring = self._rect.adjusted(-_RING_GAP - 2, -_RING_GAP - 2, _RING_GAP + 2, _RING_GAP + 2)
        return ring.united(self._tag_rect().adjusted(-1, -1, 1, 1))

    def paint(self, painter: QtGui.QPainter, option, widget=None) -> None:
        colour = QtGui.QColor(ROLE_COLOURS.get(self._role, ROLE_COLOURS["made"]))
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        # As wide on screen at any zoom: zoomed out over a big graph is when it's needed most.
        ring = QtGui.QPen(colour, 3.0)
        ring.setCosmetic(True)
        painter.setPen(ring)
        painter.drawRoundedRect(self._rect.adjusted(-_RING_GAP, -_RING_GAP, _RING_GAP, _RING_GAP), 8, 8)
        if self._label:
            tag = self._tag_rect()
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(colour)
            painter.drawRoundedRect(tag, 4, 4)
            painter.setPen(QtGui.QColor("#1e1e1e"))
            painter.setFont(self._font)
            painter.drawText(tag, QtCore.Qt.AlignmentFlag.AlignCenter, self._label)


def roles(traces: list[chunk_trace.PrimTrace]) -> list[tuple[object, str, str]]:
    """(chunk, role, tag) for every chunk the traces name, each once, maker before changer.

    One prim selected: changers are numbered in the order their changes apply. Several: a
    number would mean different things per prim, so they are tagged "changed".
    """
    found: dict[int, tuple[int, object, str, str]] = {}

    def add(chunk, rank: int, role: str, tag: str) -> None:
        if id(chunk) not in found or rank < found[id(chunk)][0]:
            found[id(chunk)] = (rank, chunk, role, tag)

    for trace in traces:
        if trace.made_by is not None:
            add(trace.made_by, 0, "made", "made")
        for number, chunk in enumerate(trace.changed_by, 1):
            add(chunk, 1, "changed", str(number) if len(traces) == 1 else "changed")
        if trace.via is not None:
            for chunk in trace.via[1]:
                add(chunk, 2, "via", "brought in")
    return [(chunk, role, tag) for _rank, chunk, role, tag in found.values()]


def _short(path) -> str:
    """A prim path short enough for one line: deep paths keep their last two names."""
    text = str(path)
    if len(text) <= _LONGEST_PATH:
        return text
    return "…/" + "/".join(text.rsplit("/", 2)[-2:])


def _links(chunks, role: str, path_of) -> str:
    colour = ROLE_COLOURS[role]
    shown = [f'<a href="chunk:{html.escape(path_of(c))}" style="color:{colour}; text-decoration:none">'
             f"{html.escape(c.name)}</a>" for c in chunks[:_NAMES_SHOWN]]
    if len(chunks) > _NAMES_SHOWN:
        shown.append(f"+{len(chunks) - _NAMES_SHOWN} more")
    return ", ".join(shown)


def trace_text(traces: list[chunk_trace.PrimTrace], path_of) -> str:
    """The line naming who made and changed the selected prims, as rich text with links.

    Args:
        traces: One per selected prim.
        path_of: chunk -> its prim path, for the links.

    Returns:
        The text; "" when nothing is selected.
    """
    if not traces:
        return ""
    if len(traces) == 1:
        subject = f"<b>{html.escape(_short(traces[0].path))}</b>"
    else:
        subject = f"<b>{len(traces)} prims</b>"
    parts = []
    for role, word in (("made", "made by"), ("changed", "changed by"), ("via", "inside what")):
        chunks = [c for c, r, _tag in roles(traces) if r == role]
        if not chunks:
            continue
        if role == "via" and len(traces) == 1:
            where = html.escape(_short(traces[0].via[0]))
            parts.append(f"inside {where}, brought in by {_links(chunks, role, path_of)}")
        elif role == "via":
            parts.append(f"inside what {_links(chunks, role, path_of)} brought in")
        else:
            parts.append(f"{word} {_links(chunks, role, path_of)}")
    if not parts:
        return f"{subject} &nbsp;&larr;&nbsp; no chunk made or changed this"
    frame = ' &nbsp; <a href="frame" style="color:#dddddd">frame them</a>'
    return f"{subject} &nbsp;&larr;&nbsp; " + " &nbsp;&middot;&nbsp; ".join(parts) + frame


class TraceBar(QtWidgets.QLabel):
    """The line at the foot of the graph: who made and changed the selected prims."""

    #: A link was clicked: "chunk:<prim path>" or "frame".
    picked = QtCore.Signal(str)

    def __init__(self, parent: QtWidgets.QWidget) -> None:
        super().__init__(parent)
        self.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.setOpenExternalLinks(False)
        self.setStyleSheet("QLabel { background: rgba(30, 30, 30, 215); color: #d8d8d8; border-radius: 4px;"
                           " padding: 4px 8px; font-size: 11px; }")
        self.linkActivated.connect(self.picked.emit)
        self.hide()

    def show_text(self, text: str, width: int) -> None:
        """Show text, at most width wide; hide when there is none."""
        if not text:
            self.hide()
            return
        self.setText(text)
        width = max(200, width)
        # One line when it fits; wrapped below the width when it doesn't, never cut off.
        self.setWordWrap(False)
        self.setMaximumWidth(16777215)
        if self.sizeHint().width() > width:
            self.setWordWrap(True)
            self.setFixedWidth(width)
        else:
            self.setMinimumWidth(0)
            self.setMaximumWidth(width)
        self.adjustSize()
        self.show()
        self.raise_()
