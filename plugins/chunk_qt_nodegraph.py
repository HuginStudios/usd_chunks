"""
Node graph view for Chunk prims.

Reads layout from UsdUINodeGraphNodeAPI; writes position back on drag.
Selection syncs with ChunkWidget via STATE_CHANGED notice.
Edges follow inputs:chunk connections between prims.
"""

from __future__ import annotations

import gc
import re
from typing import Optional

from pxr import Gf, Sdf, Usd, UsdUI
from PySide6 import QtCore, QtGui, QtWidgets

from usd_chunks import chunk as _chunk
from usd_chunks import chunk_core as core
from usd_chunks import chunk_registry
from plugins import chunk_usd
from usd_chunks.chunk import Checkbox, FileBrowser, LineEdit, Slider, Spinbox, TextEdit
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_notice import ChunkNoticeAlias, NoticeConnection
from plugins.chunk_qt_widgets import ContainerWidget, ChunkWidget, FilterableChunkMenu, CHUNK_STYLESHEET, ViewSlotState

_PORT_RADIUS = 5
_NODE_WIDTH = 220
_NODE_HEADER_H = 40


_COL_NODE_BG = QtGui.QColor("#3a3a3a")
_COL_NODE_HEADER = QtGui.QColor("#2a2a2a")
_COL_NODE_BORDER = QtGui.QColor("#555555")
_COL_NODE_SELECTED = QtGui.QColor("#cab46a")  # matches ChunkWidget.SELECTED_COLOR
_COL_GROUP_HEADER = QtGui.QColor("#1a1a1a")   # near-black — group/context nodes
_COL_LOCKED_HEADER = QtGui.QColor("#3a3020")  # dark amber — locked (external-layer) nodes
_COL_LOCKED_BG = QtGui.QColor("#2e2e28")
_COL_VIEW_HEADER = QtGui.QColor("#c87830")   # pale orange — view/terminal nodes
_COL_PORT_EXEC = QtGui.QColor("#999999")
_COL_PORT_FLOAT = QtGui.QColor("#a06845")  # muted orange
_COL_PORT_INT = QtGui.QColor("#5d7ea8")    # muted blue
_COL_PORT_BOOL = QtGui.QColor("#9e8e4a")   # muted yellow
_COL_PORT_STRING = QtGui.QColor("#6a9070")  # muted green
_COL_TEXT_PRIMARY = QtGui.QColor("#ffffff")
_COL_TEXT_SECONDARY = QtGui.QColor("#aaaaaa")
_COL_TEXT_LOCKED = QtGui.QColor("#888877")

_CHUNKS_ROOT = "/chunks"
_CHUNK_PRIM_TYPENAME = "Chunk"
_EXEC_INPUT = "inputs:chunk"
_EXEC_OUTPUT = "outputs:chunk"
_UNBUILT = object()  # sentinel for ChunkGraphScene._built_context

# Hidden top-level sink widget: child QWidgets parented here avoid a native OS
# window (and the Windows flash that comes with it) while staying off the viewport.
_proxy_sink: Optional[QtWidgets.QWidget] = None


def _get_proxy_sink() -> QtWidgets.QWidget:
    global _proxy_sink
    if _proxy_sink is None:
        _proxy_sink = QtWidgets.QWidget()
        # Logically visible (so children inherit WA_WState_Visible and isVisible()
        # returns True for port placement) but physically suppressed so nothing renders.
        _proxy_sink.setAttribute(QtCore.Qt.WA_DontShowOnScreen)
        _proxy_sink.show()
    return _proxy_sink

_PORT_EXEC = "exec"
_PORT_FLOAT = "float"
_PORT_INT = "int"
_PORT_BOOL = "bool"
_PORT_STRING = "string"

_PORT_TYPE_COLOR = {
    _PORT_EXEC:   _COL_PORT_EXEC,
    _PORT_FLOAT:  _COL_PORT_FLOAT,
    _PORT_INT:    _COL_PORT_INT,
    _PORT_BOOL:   _COL_PORT_BOOL,
    _PORT_STRING: _COL_PORT_STRING,
}


class ChunkPortItem(QtWidgets.QGraphicsEllipseItem):
    """A connection dot on a node."""

    def __init__(self, attr_path: str, port_type: str, parent: "ChunkNodeItem"):
        r = _PORT_RADIUS
        super().__init__(-r, -r, r * 2, r * 2, parent)
        self.attr_path = attr_path
        self.port_type = port_type
        self.is_input = ".inputs:" in attr_path
        self.setBrush(_PORT_TYPE_COLOR[port_type])
        self.setPen(QtGui.QPen(QtCore.Qt.NoPen))
        self.setZValue(1)

    def set_highlighted(self, on: bool) -> None:
        self.setPen(QtGui.QPen(QtGui.QColor("white"), 1.5) if on else QtGui.QPen(QtCore.Qt.NoPen))



def _iter_connectable_params(obj: core.ChunkCommon):
    """Recursively yield float/int/bool/string params from a chunk or group."""
    for param in obj.get_my_params():
        if isinstance(param, (Slider, Spinbox, Checkbox, LineEdit, TextEdit, FileBrowser)):
            yield param
        elif not isinstance(param, core.ChunkParamBase):
            yield from _iter_connectable_params(param)


def _param_port_type(param: core.ChunkParamBase) -> str:
    if isinstance(param, Checkbox):
        return _PORT_BOOL
    if isinstance(param, (LineEdit, TextEdit, FileBrowser)):
        return _PORT_STRING
    if param.value is not None and isinstance(param.value, int) and not isinstance(param.value, bool):
        return _PORT_INT
    return _PORT_FLOAT



class ChunkNodeItem(QtWidgets.QGraphicsItem):
    """Represents a single Chunk prim as a compact draggable node.

    Shows float/int/bool param dots for wiring; all other editing happens
    in the Params panel via selection sync.
    """

    def __init__(self, chunk: core.ChunkBase, prim: Usd.Prim, locked: bool = False):
        super().__init__()
        self.chunk = chunk
        self.prim = prim
        self._locked = locked
        self._is_group = chunk.has_flag(ChunkFlags.GroupFlags.CONTEXT)
        self._ports: list[ChunkPortItem] = []
        self._typed_ports: list[ChunkPortItem] = []
        self._selected = chunk.has_state(ChunkFlags.State.SELECTED)
        self._is_view = prim.GetAttribute("info:chunkType").Get() == "view"
        self._drag_active = False
        self._drag_start = QtCore.QPointF()
        self._proxy: Optional[QtWidgets.QGraphicsProxyWidget] = None
        self._port_rebuild_timer: Optional[QtCore.QTimer] = None
        self._height = _NODE_HEADER_H
        self._full_height = _NODE_HEADER_H

        self.setFlag(QtWidgets.QGraphicsItem.ItemIsMovable, False)  # handled manually — header only
        self.setFlag(QtWidgets.QGraphicsItem.ItemSendsGeometryChanges, True)
        self.setFlag(QtWidgets.QGraphicsItem.ItemIsFocusable, True)

        self._state_conn = NoticeConnection(
            self._on_state_changed,
            ChunkNoticeAlias.STATE_CHANGED,
            chunk.get_attribute_sender("state"),
        )

        self._build_ports(prim)
        self._apply_usd_pos(prim)

    # ------------------------------------------------------------------
    # Build

    def _build_ports(self, prim: Usd.Prim):
        # Exec ports only — lightweight, no widget creation.
        # Proxy (ChunkWidget) is created later via create_proxy() to allow batched deferred loading.
        self._exec_in_port = ChunkPortItem(
            f"{prim.GetPath()}.inputs:chunk", _PORT_EXEC, parent=self
        )
        self._exec_in_port.setPos(0, _NODE_HEADER_H // 2)
        self._ports.append(self._exec_in_port)

        if prim.HasAttribute("outputs:chunk"):
            self._exec_out_port = ChunkPortItem(
                f"{prim.GetPath()}.outputs:chunk", _PORT_EXEC, parent=self
            )
            self._exec_out_port.setPos(_NODE_WIDTH, _NODE_HEADER_H // 2)
            self._ports.append(self._exec_out_port)
        else:
            self._exec_out_port = None

    def create_proxy(self):
        """Create the embedded ChunkWidget proxy. Called in deferred batches after scene build."""
        # Create as a child of the sink so the show() called during init (via
        # update_from_state → set_hidden(False)) doesn't flash a native OS window.
        chunk_widget = ChunkWidget(self.chunk, parent=_get_proxy_sink())
        # setWidget() requires a top-level widget.  Reparent to None first, but set
        # WA_DontShowOnScreen beforehand so the OS never calls ShowWindow during the
        # transition from child → top-level.
        chunk_widget.setAttribute(QtCore.Qt.WA_DontShowOnScreen)
        chunk_widget.setParent(None)
        chunk_widget.setStyleSheet(CHUNK_STYLESHEET)
        chunk_widget.setFixedWidth(_NODE_WIDTH)
        self._chunk_widget = chunk_widget  # pin Python ref; PySide6 may GC despite Qt C++ ownership
        self._proxy = QtWidgets.QGraphicsProxyWidget(self)
        self._proxy.setWidget(chunk_widget)
        self._proxy.setPos(0, _NODE_HEADER_H)

        self._full_height = _NODE_HEADER_H + chunk_widget.sizeHint().height()
        self._height = self._full_height
        self.prepareGeometryChange()

        self._port_rebuild_timer = QtCore.QTimer(chunk_widget)
        self._port_rebuild_timer.setSingleShot(True)
        self._port_rebuild_timer.setInterval(0)
        self._port_rebuild_timer.timeout.connect(self._rebuild_typed_ports)
        # geometryChanged fires for collapse/expand after initial setup.
        # Initial port placement is triggered explicitly below so it doesn't
        # depend on geometryChanged firing at the right time relative to the connection.
        self._proxy.geometryChanged.connect(self._port_rebuild_timer.start)
        QtCore.QTimer.singleShot(0, self._rebuild_typed_ports)

    def _rebuild_typed_ports(self):
        if self._proxy is None or self._proxy.widget() is None:
            return
        scene = self.scene()
        for port in self._typed_ports:
            port.setParentItem(None)
            if scene:
                scene.removeItem(port)
        self._typed_ports.clear()
        self._ports = [p for p in [self._exec_in_port, self._exec_out_port] if p is not None]
        if self.chunk.has_state(ChunkFlags.State.COLLAPSED):
            # Proxy is hidden — place ports at header centre using param list alone.
            self._place_param_ports_collapsed()
        else:
            self._place_param_ports(self._proxy.widget())
        if scene:
            scene._build_edges()

    def _place_param_ports_collapsed(self):
        for param in _iter_connectable_params(self.chunk):
            ptype = _param_port_type(param)
            in_port = ChunkPortItem(f"{self.prim.GetPath()}.inputs:{param.name}", ptype, parent=self)
            in_port.setPos(0, _NODE_HEADER_H // 2)
            self._ports.append(in_port)
            self._typed_ports.append(in_port)
            out_port = ChunkPortItem(f"{self.prim.GetPath()}.outputs:{param.name}", ptype, parent=self)
            out_port.setPos(_NODE_WIDTH, _NODE_HEADER_H // 2)
            self._ports.append(out_port)
            self._typed_ports.append(out_port)

    def _place_param_ports(self, chunk_widget: QtWidgets.QWidget):

        chunk_widget.adjustSize()
        self._full_height = _NODE_HEADER_H + chunk_widget.height()
        self._height = self._full_height
        self.prepareGeometryChange()

        param_widgets = {
            w.param: w
            for w in chunk_widget.findChildren(QtWidgets.QWidget)
            if isinstance(w, core.ChunkParamWidgetBase)
        }

        for param in _iter_connectable_params(self.chunk):
            widget = param_widgets.get(param)
            if widget is None:
                continue

            ptype = _param_port_type(param)

            if widget.isVisible():
                local = widget.mapTo(chunk_widget, QtCore.QPoint(0, widget.height() // 2))
            else:
                # Param lives inside a collapsed group — walk up to find the nearest
                # visible ContainerWidget and place the port there instead.
                effective: QtWidgets.QWidget | None = None
                parent = widget.parent()
                while parent is not None and parent is not chunk_widget:
                    if isinstance(parent, ContainerWidget) and parent.isVisible():
                        effective = parent
                        break
                    parent = parent.parent()
                if effective is None:
                    continue
                local = effective.mapTo(chunk_widget, QtCore.QPoint(0, effective.height() // 2))

            y = _NODE_HEADER_H + local.y()

            in_port = ChunkPortItem(f"{self.prim.GetPath()}.inputs:{param.name}", ptype, parent=self)
            in_port.setPos(0, y)
            self._ports.append(in_port)
            self._typed_ports.append(in_port)

            out_port = ChunkPortItem(f"{self.prim.GetPath()}.outputs:{param.name}", ptype, parent=self)
            out_port.setPos(_NODE_WIDTH, y)
            self._ports.append(out_port)
            self._typed_ports.append(out_port)


    def _apply_usd_pos(self, prim: Usd.Prim):
        ui_api = UsdUI.NodeGraphNodeAPI(prim)
        pos_attr = ui_api.GetPosAttr()
        if pos_attr and pos_attr.HasAuthoredValue():
            p = pos_attr.Get()
            # USD Y-up → Qt Y-down
            self.setPos(p[0], -p[1])

    # ------------------------------------------------------------------
    # Port lookup

    def get_port(self, attr_path: str) -> Optional["ChunkPortItem"]:
        for port in self._ports:
            if port.attr_path == attr_path:
                return port
        return None

    # ------------------------------------------------------------------
    # QGraphicsItem interface

    def boundingRect(self) -> QtCore.QRectF:
        return QtCore.QRectF(
            -_PORT_RADIUS, 0,
            _NODE_WIDTH + _PORT_RADIUS * 2,
            self._height,
        )

    def paint(self, painter: QtGui.QPainter, option, widget=None):
        bg_color = _COL_LOCKED_BG if self._locked else _COL_NODE_BG
        body = QtCore.QRectF(0, 0, _NODE_WIDTH, self._height)
        border_color = _COL_NODE_SELECTED if self._selected else _COL_NODE_BORDER

        painter.setBrush(bg_color)
        painter.setPen(QtGui.QPen(border_color, 1.5))
        painter.drawRoundedRect(body, 4, 4)

        # Header background — square bottom corners so it merges with body
        if self._is_view:
            header_color = _COL_VIEW_HEADER
        elif self._is_group:
            header_color = _COL_GROUP_HEADER
        elif self._locked:
            header_color = _COL_LOCKED_HEADER
        else:
            header_color = _COL_NODE_HEADER
        painter.setBrush(header_color)
        painter.setPen(QtCore.Qt.NoPen)
        header = QtCore.QRectF(0, 0, _NODE_WIDTH, _NODE_HEADER_H)
        painter.drawRoundedRect(header, 4, 4)
        # Cover the rounded bottom of the header rect
        painter.drawRect(QtCore.QRectF(0, 10, _NODE_WIDTH, _NODE_HEADER_H - 10))

        name_color = _COL_TEXT_LOCKED if self._locked else _COL_TEXT_PRIMARY
        painter.setPen(QtGui.QPen(name_color))
        painter.setFont(QtGui.QFont("Arial", 9, QtGui.QFont.Bold))
        label = self.chunk.name
        if self._is_view:
            slot = self.prim.GetAttribute("view:slot").Get()
            label = f"View [{slot}]" if slot is not None else "View"
        elif self._is_group:
            label = f"{label}  ▶"
        elif self._locked:
            label = f"{label}  \U0001f512"
        painter.drawText(
            QtCore.QRectF(8, 4, _NODE_WIDTH - 16, 18),
            QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
            label,
        )

        chunk_type = self.prim.GetAttribute("info:chunkType").Get() or ""
        sub_color = _COL_TEXT_LOCKED if self._locked else _COL_TEXT_SECONDARY
        painter.setPen(QtGui.QPen(sub_color))
        painter.setFont(QtGui.QFont("Arial", 8))
        painter.drawText(
            QtCore.QRectF(8, 22, _NODE_WIDTH - 16, 14),
            QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
            str(chunk_type),
        )

    def mouseDoubleClickEvent(self, event):
        if event.pos().y() <= _NODE_HEADER_H:
            if self._is_view:
                slot = self.prim.GetAttribute("view:slot").Get()
                if slot is not None:
                    for view in (self.scene().views() if self.scene() else []):
                        if isinstance(view, ChunkGraphView):
                            view._view_state.set_slot(slot)
                            break
                event.accept()
                return
            if self._is_group:
                scene = self.scene()
                if scene and hasattr(scene, "dive_into"):
                    scene.dive_into.emit(self.chunk)
                event.accept()
                return
            if not self._locked:
                if self.chunk.has_state(ChunkFlags.State.COLLAPSED):
                    self.chunk.unset_state(ChunkFlags.State.COLLAPSED)
                else:
                    self.chunk.set_state(ChunkFlags.State.COLLAPSED)
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            scene = self.scene()
            if scene and hasattr(scene, "_scene_sender") and scene._scene_sender:
                scene._scene_sender.set_selected_chunks([self.chunk])
            if event.pos().y() <= _NODE_HEADER_H and not self._locked:
                self.setFocus()
                self._drag_active = True
                self._drag_start = event.pos()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_active:
            delta = event.pos() - self._drag_start
            self.setPos(self.pos() + delta)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_active:
            self._drag_active = False
            self._write_pos_to_usd()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def itemChange(self, change, value):
        if change == QtWidgets.QGraphicsItem.ItemPositionHasChanged:
            scene = self.scene()
            if scene:
                scene.update_edges_for(self)
        return super().itemChange(change, value)

    # ------------------------------------------------------------------
    # Internal

    def _write_pos_to_usd(self):
        p = self.pos()
        ui_api = UsdUI.NodeGraphNodeAPI(self.prim)
        ui_api.GetPosAttr().Set(Gf.Vec2f(p.x(), -p.y()))

    def _set_collapsed(self, collapsed: bool):
        self.prepareGeometryChange()
        self._height = _NODE_HEADER_H if collapsed else self._full_height
        if self._proxy is not None:
            self._proxy.setVisible(not collapsed)
        if collapsed:
            # Move typed ports to header centre so edges still connect to the node.
            for port in self._typed_ports:
                x = 0 if ".inputs:" in port.attr_path else _NODE_WIDTH
                port.setPos(x, _NODE_HEADER_H // 2)
        elif self._port_rebuild_timer is not None:
            # Recompute port positions from widget geometry now that proxy is visible.
            self._port_rebuild_timer.start()
        self.update()
        scene = self.scene()
        if scene:
            scene.update_edges_for(self)

    def _on_state_changed(self, notice, sender):
        selected = self.chunk.has_state(ChunkFlags.State.SELECTED)
        if selected != self._selected:
            self._selected = selected
            self.update()
        self._set_collapsed(self.chunk.has_state(ChunkFlags.State.COLLAPSED))
        disabled = self.chunk.has_state(ChunkFlags.State.DISABLED)
        self.setOpacity(0.35 if disabled else 1.0)


# ---------------------------------------------------------------------------
# Edge

class ChunkEdgeItem(QtWidgets.QGraphicsPathItem):
    """Cubic bezier connecting two ChunkPortItems."""

    _EXP_RADIUS = 12

    def __init__(
        self,
        src_port: "ChunkPortItem",
        dst_port: "ChunkPortItem",
        expression: str | None = None,
    ):
        super().__init__()
        self.src_port = src_port
        self.dst_port = dst_port
        self._expression = expression
        self._p1 = QtCore.QPointF()
        self._p2 = QtCore.QPointF()
        self.setPen(QtGui.QPen(src_port.brush().color(), 1.5))
        self.setZValue(-1)
        if expression:
            self.setAcceptHoverEvents(True)
            self.setToolTip(expression)
        self.update_path()

    def update_path(self):
        self.prepareGeometryChange()
        self._p1 = self.src_port.scenePos()
        self._p2 = self.dst_port.scenePos()
        p1, p2 = self._p1, self._p2
        dx = abs(p2.x() - p1.x()) * 0.5
        path = QtGui.QPainterPath(p1)
        path.cubicTo(p1.x() + dx, p1.y(), p2.x() - dx, p2.y(), p2.x(), p2.y())
        self.setPath(path)

    def boundingRect(self) -> QtCore.QRectF:
        rect = super().boundingRect()
        if self._expression:
            r = self._EXP_RADIUS
            mid = QtCore.QPointF(
                (self._p1.x() + self._p2.x()) / 2,
                (self._p1.y() + self._p2.y()) / 2,
            )
            rect = rect.united(QtCore.QRectF(mid.x() - r, mid.y() - r, r * 2, r * 2))
        return rect

    def paint(self, painter: QtGui.QPainter, option, widget=None):
        super().paint(painter, option, widget)
        if not self._expression:
            return
        r = self._EXP_RADIUS
        mid = QtCore.QPointF(
            (self._p1.x() + self._p2.x()) / 2,
            (self._p1.y() + self._p2.y()) / 2,
        )
        painter.setBrush(self.pen().color())
        painter.setPen(QtCore.Qt.NoPen)
        painter.drawEllipse(mid, r, r)
        painter.setPen(QtGui.QPen(QtGui.QColor("white")))
        painter.setFont(QtGui.QFont("Arial", 7, QtGui.QFont.Bold))
        painter.drawText(
            QtCore.QRectF(mid.x() - r, mid.y() - r, r * 2, r * 2),
            QtCore.Qt.AlignCenter,
            "Exp",
        )


# ---------------------------------------------------------------------------
# Scene

class ChunkGraphScene(QtWidgets.QGraphicsScene):
    """QGraphicsScene backed by a USD stage of Chunk prims."""

    _PROXY_BATCH_SIZE = 5  # ChunkWidgets created per event-loop tick

    dive_into = QtCore.Signal(object)  # emitted with the CONTEXT ChunkBase when a group node is double-clicked

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stage: Optional[Usd.Stage] = None
        self._scene_sender: Optional[core.SceneSender] = None
        self._nodes: dict[str, ChunkNodeItem] = {}  # prim path → node
        self._edges: list[ChunkEdgeItem] = []
        self._objects_changed_conn: Optional[NoticeConnection] = None
        self._show_param_edges: bool = True
        self._built_context: object = _UNBUILT  # sentinel — tracks what context is currently rendered

    def set_show_param_edges(self, show: bool):
        self._show_param_edges = show
        self._build_edges()

    def invalidate(self):
        self._built_context = _UNBUILT

    def build(self, stage: Usd.Stage, scene_sender: core.SceneSender, context=None):
        self._built_context = context
        self.clear()
        self._nodes.clear()
        self._edges.clear()
        self._stage = stage
        self._scene_sender = scene_sender

        self._objects_changed_conn = NoticeConnection(
            self._on_objects_changed, ChunkNoticeAlias.OBJECT_CHANGED, globally=True
        )
        self._chunk_deleted_conn = NoticeConnection(
            self._on_chunk_deleted, ChunkNoticeAlias.CHUNK_DELETED, globally=True
        )

        chunks_by_path = {
            f"{_resolve_chunk_root(c)}/{c.name}": c
            for c in scene_sender.get_chunks()
            if c.get_context() == context
        }

        # Derive the prim root for this context
        if context is not None and getattr(context, "prim_path", None):
            chunks_root_path = Sdf.Path(f"{context.prim_path}/chunks")
        else:
            chunks_root_path = Sdf.Path(_CHUNKS_ROOT)

        chunks_root = stage.GetPrimAtPath(chunks_root_path)
        if not chunks_root.IsValid():
            return

        # Also include CONTEXT group chunks whose prims live directly under this root
        for prim in chunks_root.GetChildren():
            path = str(prim.GetPath())
            if path not in chunks_by_path:
                ctx_chunk = scene_sender.get_context_for_prim_path(path)
                if ctx_chunk is not None:
                    chunks_by_path[path] = ctx_chunk

        # Pre-compute lock state: a prim is locked when authored in a layer that isn't
        # the session layer, root layer, or current edit target.
        root_id = stage.GetRootLayer().identifier
        edit_id = stage.GetEditTarget().GetLayer().identifier

        for prim in chunks_root.GetChildren():
            if prim.GetTypeName() != _CHUNK_PRIM_TYPENAME:
                continue
            path = str(prim.GetPath())
            chunk = chunks_by_path.get(path)
            if chunk is None:
                continue
            src_layer = next(
                (spec.layer for spec in prim.GetPrimStack()
                 if not spec.layer.anonymous and spec.layer.identifier != root_id),
                None,
            )
            locked = src_layer is not None and src_layer.identifier != edit_id
            node = ChunkNodeItem(chunk, prim, locked=locked)
            self.addItem(node)
            self._nodes[path] = node

        # Exec edges are built immediately (exec ports always exist).
        self._build_edges()

        # Proxy widgets (ChunkWidget) are created in small batches so the graph
        # renders immediately with node headers and fills in progressively.
        self._create_proxies_batch(list(self._nodes.values()), on_complete=self._on_proxies_ready)

    def _on_proxies_ready(self):
        for view in self.views():
            if view.isVisible():
                view._frame_nodes()
            else:
                view._pending_frame = True

    def _create_proxies_batch(self, remaining: list, on_complete=None):
        batch, rest = remaining[:self._PROXY_BATCH_SIZE], remaining[self._PROXY_BATCH_SIZE:]
        for node in batch:
            node.create_proxy()
        if rest:
            QtCore.QTimer.singleShot(0, lambda: self._create_proxies_batch(rest, on_complete))
        elif on_complete:
            QtCore.QTimer.singleShot(0, on_complete)

    def _build_edges(self):
        for edge in self._edges:
            self.removeItem(edge)
        self._edges.clear()

        for node in self._nodes.values():
            for attr in node.prim.GetAttributes():
                attr_name = attr.GetName()
                if not attr_name.startswith("inputs:"):
                    continue
                is_exec = attr_name == "inputs:chunk"
                if not is_exec and not self._show_param_edges:
                    continue
                dst_attr_path = f"{node.prim.GetPath()}.{attr_name}"
                dst_port = node.get_port(dst_attr_path)
                if dst_port is None:
                    continue
                expression: str | None = None
                try:
                    param = node.chunk.get_param(attr_name[len("inputs:"):])
                    raw = getattr(param, 'raw_value', None) if param else None
                    if isinstance(raw, str) and re.search(r'@\w+\.\w+@', raw):
                        expression = raw
                except Exception:
                    pass

                for conn_path in attr.GetConnections():
                    src_node = self._nodes.get(str(conn_path.GetPrimPath()))
                    if src_node is None:
                        continue
                    # Connection is always owned by the consumer, so the source
                    # always visualizes from its output dot regardless of attr prefix.
                    src_path = str(conn_path).replace(".inputs:", ".outputs:", 1)
                    src_port = src_node.get_port(src_path)
                    if src_port is None:
                        continue
                    edge = ChunkEdgeItem(src_port, dst_port, expression)
                    self.addItem(edge)
                    self._edges.append(edge)

    def update_edges_for(self, node: ChunkNodeItem):
        node_ports = set(node._ports)
        for edge in self._edges:
            if edge.src_port in node_ports or edge.dst_port in node_ports:
                edge.update_path()

    def _on_chunk_deleted(self, notice, sender):
        prim_path = next(
            (p for p, n in self._nodes.items() if n.chunk is sender), None
        )
        if prim_path is None:
            return
        node = self._nodes.pop(prim_path)
        # Defer so all CHUNK_DELETED callbacks (including ChunkWidget.delete_self)
        # complete before Qt destroys the embedded widget.
        QtCore.QTimer.singleShot(0, lambda: (self.removeItem(node), self._build_edges()))

    def _on_objects_changed(self, notice, sender):
        if not self._stage:
            return
        if sender.GetRootLayer().identifier != self._stage.GetRootLayer().identifier:
            return
        for path in notice.GetChangedInfoOnlyPaths():
            if path.IsPropertyPath() and path.name == "ui:nodegraph:node:pos":
                prim_path = str(path.GetPrimPath())
                node = self._nodes.get(prim_path)
                if node:
                    node._apply_usd_pos(node.prim)
        self._build_edges()


# ---------------------------------------------------------------------------
# Proxy key router

class _ProxyKeyRouter(QtCore.QObject):
    """Application-level event filter that routes key events to the proxy widget
    that was most recently clicked.

    usdview's MainWindow immediately reclaims native keyboard focus whenever
    any child widget (including our QGraphicsView) gets it, so the normal
    Qt proxy-focus mechanism never works.  Instead we track which proxy widget
    was last clicked (_active) and manually forward key events to the logically-
    focused child within that widget, bypassing native focus entirely.

    ShortcutOverride events are accepted while a proxy is active so usdview's
    camera-key shortcuts (F/W/A/S/D…) don't fire while the user is typing.
    """

    def __init__(self, view: "ChunkGraphView"):
        super().__init__(view)
        self._view = view
        self._busy = False
        self._active: Optional[QtWidgets.QWidget] = None  # proxy widget last clicked
        self._view_active: bool = False  # True while graph view was most recently clicked

    def set_active(self, widget: Optional[QtWidgets.QWidget]) -> None:
        self._active = widget

    def set_view_active(self, active: bool) -> None:
        self._view_active = active

    # Widget types that acquire native focus themselves and handle their own keys.
    _INPUT_TYPES = (
        QtWidgets.QLineEdit,
        QtWidgets.QTextEdit,
        QtWidgets.QPlainTextEdit,
        QtWidgets.QAbstractSpinBox,
        QtWidgets.QComboBox,
    )

    def eventFilter(self, _obj, event) -> bool:
        if self._busy:
            return False
        etype = event.type()
        if etype not in (QtCore.QEvent.Type.KeyPress, QtCore.QEvent.Type.ShortcutOverride):
            return False

        fw = QtWidgets.QApplication.focusWidget()
        if isinstance(fw, self._INPUT_TYPES):
            inside_proxy = self._active is not None and (
                fw is self._active or self._active.isAncestorOf(fw)
            )
            if inside_proxy:
                # QLineEdit inside our proxy has Qt-internal focus — prevent shortcuts
                # but let KeyPress flow natively so the widget handles it directly.
                if etype == QtCore.QEvent.Type.ShortcutOverride:
                    event.accept()
                return False
            # Input widget outside our proxy has focus — deactivate and defer.
            self._active = None
            self._view_active = False
            return False

        # F-to-frame: intercept only when cursor is over the graph view and no proxy active.
        if self._active is None and event.key() == QtCore.Qt.Key.Key_F:
            view = self._view
            view_global = QtCore.QRect(view.mapToGlobal(QtCore.QPoint(0, 0)), view.size())
            if view_global.contains(QtGui.QCursor.pos()):
                if etype == QtCore.QEvent.Type.ShortcutOverride:
                    event.accept()
                    return True
                if etype == QtCore.QEvent.Type.KeyPress:
                    view._frame_nodes()
                    event.accept()
                    return True

        if self._active is None:
            return False
        if etype == QtCore.QEvent.Type.ShortcutOverride:
            event.accept()
            return True
        # Route to whichever child last had logical (not native) focus.
        target = self._active.focusWidget() or self._active
        self._busy = True
        try:
            QtWidgets.QApplication.sendEvent(target, event)
        finally:
            self._busy = False
        return True


# ---------------------------------------------------------------------------
# View

class ChunkGraphView(QtWidgets.QGraphicsView):
    """QGraphicsView with zoom, pan (middle mouse or Space+drag), and F to frame."""

    _SCENE_EXTENT = 50000

    dive_into = QtCore.Signal(object)  # re-emitted from scene when a group node is double-clicked

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(ChunkGraphScene())
        self.setRenderHint(QtGui.QPainter.Antialiasing)
        self.setDragMode(QtWidgets.QGraphicsView.NoDrag)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.AnchorUnderMouse)
        self.setBackgroundBrush(QtGui.QColor("#252525"))
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        # NoFocus prevents Qt's automatic click-to-focus on the view, which would
        # steal native focus away from QLineEdits embedded in proxy widgets.
        # We grant focus to the view explicitly in mousePressEvent when needed.
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        e = self._SCENE_EXTENT
        self.setSceneRect(-e, -e, e * 2, e * 2)
        self._pan_active = False
        self._pan_start = QtCore.QPoint()
        self._pending_frame = False

        self._menubar = QtWidgets.QMenuBar(self)
        view_menu = self._menubar.addMenu("View")
        self._param_edge_action = view_menu.addAction("Show param connections")
        self._param_edge_action.setCheckable(True)
        self._param_edge_action.setChecked(True)
        self._param_edge_action.triggered.connect(self._on_toggle_param_edges)
        self._menubar.setFixedWidth(self.width())
        mh = self._menubar.sizeHint().height()
        self.setViewportMargins(0, mh, 0, 0)

        self._proxy_key_router = _ProxyKeyRouter(self)
        QtWidgets.QApplication.instance().installEventFilter(self._proxy_key_router)

        self._drag_src_port: Optional[ChunkPortItem] = None
        self._drag_edge: Optional[QtWidgets.QGraphicsPathItem] = None
        self._view_state = ViewSlotState(self)
        self._view_state.slot_changed.connect(self._on_view_slot_changed)
        self._view_state.create_view_for.connect(self._on_create_view_for_path)

    def _on_toggle_param_edges(self, checked: bool):
        graph_scene = self.scene()
        if isinstance(graph_scene, ChunkGraphScene):
            graph_scene.set_show_param_edges(checked)

    def resizeEvent(self, event: QtGui.QResizeEvent):
        super().resizeEvent(event)
        self._menubar.setFixedWidth(self.width())

    def drawForeground(self, painter: QtGui.QPainter, rect) -> None:
        super().drawForeground(painter, rect)
        painter.save()
        painter.resetTransform()
        vp = self.viewport()
        if self._view_state.active_slot == 0:
            text = "View: –"
            bg = QtGui.QColor(40, 40, 40, 180)
            fg = QtGui.QColor(120, 120, 120)
        else:
            text = f"View: {self._view_state.active_slot}"
            bg = QtGui.QColor(_COL_VIEW_HEADER.red(), _COL_VIEW_HEADER.green(), _COL_VIEW_HEADER.blue(), 210)
            fg = QtGui.QColor("#ffffff")
        font = QtGui.QFont("monospace", 9, QtGui.QFont.Bold)
        painter.setFont(font)
        fm = QtGui.QFontMetrics(font)
        pad_x, pad_y = 8, 5
        w = fm.horizontalAdvance(text) + pad_x * 2
        h = fm.height() + pad_y * 2
        x = vp.width() - w - 8
        y = vp.height() - h - 8
        painter.setBrush(bg)
        painter.setPen(QtCore.Qt.NoPen)
        painter.drawRoundedRect(QtCore.QRect(x, y, w, h), 4, 4)
        painter.setPen(fg)
        painter.drawText(QtCore.QRect(x, y, w, h), QtCore.Qt.AlignCenter, text)
        painter.restore()

    def sizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(200, 200)

    def minimumSizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(0, 0)

    def build(self, stage: Usd.Stage, scene_sender: core.SceneSender, context=None):
        scene = self.scene()
        scene.build(stage, scene_sender, context)
        scene.dive_into.connect(self.dive_into)

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        if self._pending_frame:
            self._pending_frame = False
            self._frame_nodes()

    # ------------------------------------------------------------------
    # Zoom

    def wheelEvent(self, event: QtGui.QWheelEvent):
        factor = 1.03 if event.angleDelta().y() > 0 else 1 / 1.03
        self.scale(factor, factor)

    # ------------------------------------------------------------------
    # Pan — middle mouse or Space + left drag

    def _start_pan(self, pos: QtCore.QPoint):
        self._pan_active = True
        self._pan_start = pos
        self.setCursor(QtCore.Qt.ClosedHandCursor)

    def _stop_pan(self):
        self._pan_active = False
        self.setCursor(QtCore.Qt.ArrowCursor)

    def mousePressEvent(self, event: QtGui.QMouseEvent):
        scene_pos = self.mapToScene(event.pos())
        hit = self.scene().itemAt(scene_pos, QtGui.QTransform())

        # Walk up item parent chain to find a proxy widget, if any.
        proxy = None
        item = hit
        while item is not None:
            if isinstance(item, QtWidgets.QGraphicsProxyWidget):
                proxy = item
                break
            item = item.parentItem()

        # Port left-click → start wire drag; right-click → disconnect menu.
        if isinstance(hit, ChunkPortItem):
            if event.button() == QtCore.Qt.LeftButton:
                self._start_wire_drag(hit, scene_pos)
                return
            if event.button() == QtCore.Qt.RightButton:
                self._show_port_menu(hit, event.globalPos())
                return

        # Track which proxy widget was clicked so the key router can forward
        # key events to it without relying on native focus (which usdview steals).
        # Find the specific child widget at the click position rather than the
        # top-level proxy widget, so key events reach e.g. QLineEdit directly.
        if proxy:
            proxy_local = proxy.mapFromScene(scene_pos).toPoint()
            clicked_child = proxy.widget().childAt(proxy_local)
            self._proxy_key_router.set_active(clicked_child or proxy.widget())
        else:
            self._proxy_key_router.set_active(None)
        self._proxy_key_router.set_view_active(True)
        if proxy is None:
            self.setFocus()

        if event.button() == QtCore.Qt.MiddleButton or (
            event.button() == QtCore.Qt.LeftButton
            and QtWidgets.QApplication.keyboardModifiers() == QtCore.Qt.AltModifier
        ):
            self._start_pan(event.pos())
        else:
            if event.button() == QtCore.Qt.LeftButton:
                if hit is None:
                    graph_scene = self.scene()
                    if isinstance(graph_scene, ChunkGraphScene) and graph_scene._scene_sender:
                        graph_scene._scene_sender.set_selected_chunks([])
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent):
        if self._drag_src_port is not None:
            self._update_wire_drag(self.mapToScene(event.pos()))
            return
        if self._pan_active:
            delta = event.pos() - self._pan_start
            self._pan_start = event.pos()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent):
        if self._drag_src_port is not None and event.button() == QtCore.Qt.LeftButton:
            self._finish_wire_drag(self.mapToScene(event.pos()))
            return
        if self._pan_active and event.button() in (QtCore.Qt.MiddleButton, QtCore.Qt.LeftButton):
            self._stop_pan()
        else:
            super().mouseReleaseEvent(event)

    # ------------------------------------------------------------------
    # Keyboard

    def keyPressEvent(self, event: QtGui.QKeyEvent):
        if event.key() == QtCore.Qt.Key.Key_F:
            self._frame_nodes()
            event.accept()
            return
        if event.key() == QtCore.Qt.Key.Key_Escape:
            if self._drag_src_port is not None:
                self._cancel_wire_drag()
            else:
                self._proxy_key_router.set_active(None)
                self.setFocus()
            event.accept()
            return
        if event.key() == QtCore.Qt.Key.Key_Delete:
            self._delete_selected()
            event.accept()
            return
        if event.key() == QtCore.Qt.Key.Key_D:
            self._toggle_disable_selected()
            event.accept()
            return
        if event.key() == QtCore.Qt.Key.Key_V:
            self._create_view_node()
            event.accept()
            return
        if event.key() in (QtCore.Qt.Key.Key_1, QtCore.Qt.Key.Key_2, QtCore.Qt.Key.Key_3):
            slot = event.key() - QtCore.Qt.Key.Key_0
            cur = self._view_state.active_slot
            self._view_state.set_slot(0 if cur == slot else slot)
            event.accept()
            return
        super().keyPressEvent(event)

    def _delete_selected(self) -> None:
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene):
            return
        for node in list(graph_scene._nodes.values()):
            if node.chunk.has_state(ChunkFlags.State.SELECTED):
                node.chunk.request_delete()

    def _toggle_disable_selected(self) -> None:
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene):
            return
        for node in list(graph_scene._nodes.values()):
            if node.chunk.has_state(ChunkFlags.State.SELECTED):
                if node.chunk.has_state(ChunkFlags.State.DISABLED):
                    node.chunk.unset_state(ChunkFlags.State.DISABLED)
                else:
                    node.chunk.set_state(ChunkFlags.State.DISABLED)

    # ------------------------------------------------------------------
    # Wire drag

    def _start_wire_drag(self, port: ChunkPortItem, scene_pos: QtCore.QPointF) -> None:
        self._drag_src_port = port
        self._drag_edge = QtWidgets.QGraphicsPathItem()
        pen = QtGui.QPen(port.brush().color(), 1.5, QtCore.Qt.DashLine)
        self._drag_edge.setPen(pen)
        self._drag_edge.setZValue(10)
        self.scene().addItem(self._drag_edge)
        self._update_wire_drag(scene_pos)

    def _update_wire_drag(self, scene_pos: QtCore.QPointF) -> None:
        if self._drag_edge is None:
            return
        p1 = self._drag_src_port.scenePos()
        p2 = scene_pos
        dx = abs(p2.x() - p1.x()) * 0.5
        path = QtGui.QPainterPath(p1)
        path.cubicTo(p1.x() + dx, p1.y(), p2.x() - dx, p2.y(), p2.x(), p2.y())
        self._drag_edge.setPath(path)

        # Highlight compatible destination ports.
        graph_scene = self.scene()
        if isinstance(graph_scene, ChunkGraphScene):
            for node in graph_scene._nodes.values():
                for port in node._ports:
                    port.set_highlighted(
                        port is not self._drag_src_port
                        and self._can_connect(self._drag_src_port, port)
                    )

    def _finish_wire_drag(self, scene_pos: QtCore.QPointF) -> None:
        # Use items() instead of itemAt() so the rubber-band edge (Z=10) doesn't
        # occlude the port underneath it.
        hit = next(
            (item for item in self.scene().items(scene_pos) if isinstance(item, ChunkPortItem)),
            None,
        )
        if hit is not None and self._can_connect(self._drag_src_port, hit):
            self._author_connection(self._drag_src_port, hit)
        self._cancel_wire_drag()

    def _cancel_wire_drag(self) -> None:
        if self._drag_edge is not None:
            self.scene().removeItem(self._drag_edge)
            self._drag_edge = None
        graph_scene = self.scene()
        if isinstance(graph_scene, ChunkGraphScene):
            for node in graph_scene._nodes.values():
                for port in node._ports:
                    port.set_highlighted(False)
        self._drag_src_port = None

    def _can_connect(self, a: ChunkPortItem, b: ChunkPortItem) -> bool:
        if a.port_type != b.port_type:
            return False
        if a.is_input == b.is_input:
            return False
        if Sdf.Path(a.attr_path).GetPrimPath() == Sdf.Path(b.attr_path).GetPrimPath():
            return False
        return True

    def _author_connection(self, a: ChunkPortItem, b: ChunkPortItem) -> None:
        src, dst = (b, a) if a.is_input else (a, b)
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene) or not graph_scene._stage:
            return
        dst_attr = graph_scene._stage.GetAttributeAtPath(Sdf.Path(dst.attr_path))
        if not (dst_attr and dst_attr.IsValid()):
            # Attribute doesn't exist yet (e.g. inputs:chunk on a not-yet-connected node).
            prim = graph_scene._stage.GetPrimAtPath(Sdf.Path(dst.attr_path).GetPrimPath())
            if not prim.IsValid():
                return
            sdf_type = Sdf.ValueTypeNames.Token if dst.port_type == _PORT_EXEC else Sdf.ValueTypeNames.String
            dst_attr = prim.CreateAttribute(dst.attr_path.rsplit(".", 1)[-1], sdf_type, custom=False)
            if not dst_attr.IsValid():
                return
        # Exec connections reference outputs:chunk; typed param connections reference inputs:param.
        if src.port_type == _PORT_EXEC:
            src_conn_path = Sdf.Path(src.attr_path)
        else:
            src_conn_path = Sdf.Path(src.attr_path.replace(".outputs:", ".inputs:", 1))
        dst_attr.SetConnections([src_conn_path])

        # For typed params, also write an expression so the value resolves at runtime.
        # The user can clear it from the QLineEdit to keep the wiring without expression eval.
        if src.port_type != _PORT_EXEC:
            src_node_name = Sdf.Path(src.attr_path).GetPrimPath().name
            src_param_name = src.attr_path.rsplit(":", 1)[-1]
            dst_param_name = dst.attr_path.rsplit(":", 1)[-1]
            dst_node = graph_scene._nodes.get(str(Sdf.Path(dst.attr_path).GetPrimPath()))
            if dst_node:
                param = dst_node.chunk.get_param(dst_param_name)
                if param is not None:
                    param.value = f"@{src_node_name}.{src_param_name}@"

        graph_scene._build_edges()

    def _show_port_menu(self, port: ChunkPortItem, global_pos: QtCore.QPoint) -> None:
        if not port.is_input:
            return
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene) or not graph_scene._stage:
            return
        attr = graph_scene._stage.GetAttributeAtPath(Sdf.Path(port.attr_path))
        if not (attr and attr.IsValid() and attr.GetConnections()):
            return
        menu = QtWidgets.QMenu()
        menu.addAction("Disconnect").triggered.connect(
            lambda: self._disconnect_port(port, attr, graph_scene)
        )
        menu.exec(global_pos)

    def _disconnect_port(self, port: ChunkPortItem, attr: Usd.Attribute, graph_scene: "ChunkGraphScene") -> None:
        # For exec ports: rewire predecessor → successor before clearing.
        if port.port_type == _PORT_EXEC:
            prim_path = Sdf.Path(port.attr_path).GetPrimPath()
            out_attr_path = str(prim_path) + ".outputs:chunk"
            # Find any successor whose inputs:chunk points to this node's outputs:chunk.
            for node in graph_scene._nodes.values():
                succ_attr = graph_scene._stage.GetAttributeAtPath(
                    Sdf.Path(str(node.prim.GetPath()) + ".inputs:chunk")
                )
                if succ_attr and succ_attr.IsValid():
                    if any(str(c) == out_attr_path for c in succ_attr.GetConnections()):
                        # Rewire successor to this node's predecessor.
                        pred_conns = attr.GetConnections()
                        if pred_conns:
                            succ_attr.SetConnections(list(pred_conns))
                        else:
                            succ_attr.ClearConnections()
                        break
        attr.ClearConnections()
        graph_scene._build_edges()

    def _show_chunk_menu(self, node: "ChunkNodeItem", global_pos: QtCore.QPoint) -> None:
        self._view_state.show_chunk_menu(node.chunk, str(node.prim.GetPath()), global_pos)

    # ------------------------------------------------------------------
    # View nodes

    def _create_view_node(self) -> None:
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene) or not graph_scene._scene_sender or not graph_scene._stage:
            return
        selected = [n for n in graph_scene._nodes.values() if n.chunk.has_state(ChunkFlags.State.SELECTED)]
        if not selected:
            return
        self._create_view_node_for(selected[0])

    def _create_view_node_for(self, target_node: "ChunkNodeItem") -> None:
        self._create_view_node_for_prim(str(target_node.prim.GetPath()))

    def _create_view_node_for_prim(self, target_prim_path: str) -> None:
        """Create a view node wired to target_prim_path using only USD stage data."""
        registry = chunk_registry.ChunkRegistry()
        if not registry.initialized:
            return
        stage = registry.scene.get_stage()
        if not stage:
            return

        target_prim = stage.GetPrimAtPath(Sdf.Path(target_prim_path))
        if not target_prim.IsValid():
            return
        if target_prim.GetAttribute("info:chunkType").Get() == "view":
            return

        # If a view is already wired to this prim, activate it instead of creating a duplicate.
        for prim in stage.Traverse():
            if prim.GetAttribute("info:chunkType").Get() != "view":
                continue
            exec_in = prim.GetAttribute(_EXEC_INPUT)
            if exec_in.IsValid():
                for conn_path in exec_in.GetConnections():
                    if str(conn_path.GetPrimPath()) == target_prim_path:
                        existing_slot = prim.GetAttribute("view:slot").Get()
                        if existing_slot is not None:
                            self._view_state.set_slot(existing_slot)
                        return

        # Find an unused slot.
        used_slots: set = set()
        for prim in stage.Traverse():
            s = prim.GetAttribute("view:slot").Get()
            if s is not None:
                used_slots.add(s)
        slot = next((s for s in [1, 2, 3] if s not in used_slots), None)
        if slot is None:
            return

        mod = registry.get_chunk_module("view")
        if not mod:
            return
        chunks = mod.create() if mod.loaded else mod.load()
        if not chunks:
            return
        view_chunk = chunks[0]
        view_chunk.name = f"View{slot}"
        view_chunk.set_state(ChunkFlags.State.COLLAPSED)

        # Read target pos from USD before add_chunk fires CHUNK_LOADED and rebuilds the graph.
        node_api = UsdUI.NodeGraphNodeAPI(target_prim)
        pos_attr = node_api.GetPosAttr()
        raw_pos = pos_attr.Get() if (pos_attr and pos_attr.IsValid()) else None
        tx, ty = (float(raw_pos[0]), float(raw_pos[1])) if raw_pos is not None else (0.0, 0.0)

        scene = _chunk.get_scene()
        if not scene:
            return
        scene.add_chunk(view_chunk)

        view_prim_path = f"{_resolve_chunk_root(view_chunk)}/{view_chunk.name}"
        stage = registry.scene.get_stage()
        if not stage:
            return
        view_prim = stage.GetPrimAtPath(Sdf.Path(view_prim_path))
        if not view_prim.IsValid():
            return

        exec_in = view_prim.GetAttribute(_EXEC_INPUT)
        if not exec_in.IsValid():
            exec_in = view_prim.CreateAttribute(_EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False)
        exec_in.SetConnections([Sdf.Path(f"{target_prim_path}.{_EXEC_OUTPUT}")])

        slot_attr = view_prim.CreateAttribute("view:slot", Sdf.ValueTypeNames.Int, custom=True)
        slot_attr.SetVariability(Sdf.VariabilityUniform)
        slot_attr.Set(slot)

        UsdUI.NodeGraphNodeAPI.Apply(view_prim).CreatePosAttr().Set(
            Gf.Vec2f(tx + _NODE_WIDTH + 40, ty - 80)
        )

        QtCore.QTimer.singleShot(0, lambda: self._view_state.set_slot(slot))

    def set_view_state(self, vs: ViewSlotState) -> None:
        self._view_state.slot_changed.disconnect(self._on_view_slot_changed)
        self._view_state.create_view_for.disconnect(self._on_create_view_for_path)
        self._view_state = vs
        vs.slot_changed.connect(self._on_view_slot_changed)
        vs.create_view_for.connect(self._on_create_view_for_path)

    def _on_create_view_for_path(self, prim_path: str) -> None:
        self._create_view_node_for_prim(prim_path)

    def _on_view_slot_changed(self, slot: int) -> None:
        if slot == 0:
            self._do_deactivate_view()
        else:
            self._do_activate_view_slot(slot)
        self.viewport().update()

    def _do_deactivate_view(self) -> None:
        registry = chunk_registry.ChunkRegistry()
        if not registry.initialized:
            return
        stage = registry.scene.get_stage()
        if not stage:
            return
        context = self._graph_context()
        lm = chunk_registry.ChunkLayerManager()
        for c in registry.scene.get_chunks():
            if chunk_usd.is_view_chunk(c):
                continue
            if context and c.get_context() != context:
                continue
            c.set_blocked(False)
            layer = lm.get_layer(c)
            if layer and not c.has_state(ChunkFlags.State.DISABLED):
                layer.SetMuted(False)
        for head in chunk_usd.get_exec_head_chunks(stage, registry.scene, context):
            if not head.has_state(ChunkFlags.State.DISABLED):
                head.execute()

    def _do_activate_view_slot(self, slot: int) -> None:
        registry = chunk_registry.ChunkRegistry()
        if not registry.initialized:
            return
        stage = registry.scene.get_stage()
        if not stage:
            return
        context = self._graph_context()

        view_chunk = chunk_usd.find_view_chunk_for_slot(slot, stage, registry.scene)
        if view_chunk is None:
            return

        gc.collect()  # break zombie NoticeConnection cycles before STATE_CHANGED fires
        for c in registry.scene.get_chunks():
            if c.has_state(ChunkFlags.State.SELECTED):
                c.set_selected(False)
        view_chunk.set_selected(True)

        lm = chunk_registry.ChunkLayerManager()
        src_chunks = chunk_usd.get_view_exec_sources(view_chunk, stage, registry.scene)
        if not src_chunks:
            return

        downstream: set = set()
        for src in src_chunks:
            downstream |= chunk_usd.get_exec_downstream_chunks(src, stage, registry.scene)

        # Block before mute: ObjectsChanged from SetMuted must not re-execute blocked nodes.
        for c in registry.scene.get_chunks():
            if chunk_usd.is_view_chunk(c):
                continue
            if context and c.get_context() != context:
                continue
            if c in downstream:
                c.set_blocked(True)
            else:
                c.set_blocked(False)
                layer = lm.get_layer(c)
                if layer and not c.has_state(ChunkFlags.State.DISABLED):
                    layer.SetMuted(False)

        for c in downstream:
            if chunk_usd.is_view_chunk(c):
                continue
            layer = lm.get_layer(c)
            if layer:
                layer.SetMuted(True)

        for src in src_chunks:
            if not src.has_state(ChunkFlags.State.DISABLED):
                src.execute()

    def _graph_context(self) -> "Optional[core.ChunkBase]":
        graph_scene = self.scene()
        return graph_scene._built_context if isinstance(graph_scene, ChunkGraphScene) else None

    # ------------------------------------------------------------------
    # Frame

    def _frame_nodes(self):
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene) or not graph_scene._nodes:
            return
        nodes = [
            n for n in graph_scene._nodes.values()
            if n.chunk.has_state(ChunkFlags.State.SELECTED)
        ] or list(graph_scene._nodes.values())

        rect = QtCore.QRectF()
        for node in nodes:
            rect = rect.united(node.sceneBoundingRect())
        rect.adjust(-60, -60, 60, 60)
        self.fitInView(rect, QtCore.Qt.KeepAspectRatio)

    def contextMenuEvent(self, event: QtGui.QContextMenuEvent):
        scene_pos = self.mapToScene(event.pos())
        # Node right-click: show the chunk's own context menu.
        for item in self.scene().items(scene_pos):
            if isinstance(item, ChunkNodeItem):
                self._show_chunk_menu(item, event.globalPos())
                event.accept()
                return

        mod = FilterableChunkMenu().select_module(event.globalPos())
        if not mod:
            return

        chunks = mod.create() if mod.loaded else mod.load()

        scene = _chunk.get_scene()
        if not scene:
            return
        for c in chunks:
            scene.add_chunk(c)



# ---------------------------------------------------------------------------
# Helpers

def _resolve_chunk_root(chunk: core.ChunkBase) -> str:
    context = chunk.get_context()
    if context and getattr(context, "prim_path", None):
        return f"{context.prim_path}/chunks"
    return _CHUNKS_ROOT
