"""
NodeGraph widget wrapper — embeds NodeGraphQt into a QWidget,
adds a Tab menu for spawning nodes, and an execution engine.
"""

from __future__ import annotations

import os
import sys
import warnings

from pxr import Sdf
from PySide6 import QtWidgets, QtCore, QtGui
from NodeGraphQt import NodeGraph, GroupNode
from NodeGraphQt.base.graph import SubGraph
from ._property_panel import build_node_panel
from NodeGraphQt.constants import PortTypeEnum
from NodeGraphQt.qgraphics.node_base import NodeItem
from NodeGraphQt.qgraphics.pipe import PipeItem
from NodeGraphQt.qgraphics.port import PortItem
from . import nodes
from .chunk_adapter import ChunkAdapter
from .graph_executor import GraphExecutor
from .graph_host import GraphHost
from .nodes._base import (
    _get_promotions,
    _get_promoted_outputs,
    _save_promotions,
    PORT_COLORS,
)
from ._graph_ui import _ViewportLabel, _TabSearchDialog
from ._neon_theme import install_neon_theme
from ._graph_layout import layout_nodes
from ._graph_export import export_python_script, export_group_as_chunk
from usd_chunks import chunk_core, chunk_registry as _cr
from usd_chunks.chunk import Slider, Spinbox, Menu, Checkbox

# NodeGraphQt's deserializer calls model.set_property() which only succeeds for
# keys already present in custom_properties.  Pre-register the two promo keys on
# every GroupNode instance so save/load round-trips don't raise NodePropertyError.
_orig_group_node_init = GroupNode.__init__


def _patched_group_node_init(self, *args, **kwargs):
    _orig_group_node_init(self, *args, **kwargs)
    self.model.custom_properties.setdefault("promoted_params", "{}")
    self.model.custom_properties.setdefault("promoted_outputs", "{}")


GroupNode.__init__ = _patched_group_node_init

MENU_STYLE = (
    "QMenu { background: #0e1016; color: #c8d0e8; border: 1px solid #1e2438; padding: 2px; }"
    "QMenu::item { padding: 4px 20px 4px 8px; }"
    "QMenu::item:selected { background: #1e2a40; }"
    "QMenu::separator { height: 1px; background: #1e2438; margin: 2px 4px; }"
)

PANEL_STYLE = (
    "QWidget { background: #1e1e1e; color: #ddd; font: 11px 'Consolas'; }"
    "QLabel { color: #aaa; font-size: 11px; }"
    "QLineEdit { background: #2a2a2a; color: #ddd; border: 1px solid #444;"
    "  border-radius: 4px; padding: 3px 6px; font: 10px 'Consolas'; }"
    "QLineEdit:focus { border-color: #32d2dc; }"
    "QComboBox { background: #2a2a2a; color: #ddd; border: 1px solid #444;"
    "  border-radius: 4px; padding: 3px 6px; font: 10px 'Consolas'; }"
    "QAbstractItemView { background: #1e1e1e; color: #ddd; border: 1px solid #444;"
    "  selection-background-color: #2a3a4a; selection-color: #ddd; outline: none; }"
    "QPushButton { background: #2a2a2a; color: #ddd; border: 1px solid #444;"
    "  border-radius: 4px; padding: 3px 8px; font: 10px 'Consolas'; }"
    "QPushButton:hover { background: #333; border-color: #666; }"
)

_BTN_STYLE = (
    "QPushButton { background:#0e1016; color:#c8d0e8; border:1px solid #1e2438;"
    "border-radius:6px; padding:5px 12px; font:11px 'Consolas'; }"
    "QPushButton:hover { background:#161b26; border-color:#2a3550; }"
    "QPushButton:pressed { background:#0a0d12; }"
)
_RUN_STYLE = (
    "QPushButton { background:#e8533a; color:#fff; border:none;"
    "border-radius:6px; padding:5px 14px; font:bold 11px 'Consolas'; }"
    "QPushButton:hover { background:#f06848; }"
)


def _safe_port_visible(port, visible):
    """Set port visibility, silently ignoring errors (node not yet in graph)."""
    try:
        port.set_visible(visible)
    except Exception:
        pass


def _remove_layer_from_hierarchy(parent_layer, target_layer):
    """Recursively remove target_layer from parent_layer's sublayer tree.

    Prunes empty intermediate layers after removal so the stage's layer
    stack stays tidy (avoids dangling empty graph-group layers).
    """
    paths = list(parent_layer.subLayerPaths)
    if target_layer.identifier in paths:
        paths.remove(target_layer.identifier)
        parent_layer.subLayerPaths = paths
        return True
    for path in paths:
        sub = Sdf.Layer.Find(path)
        if sub and _remove_layer_from_hierarchy(sub, target_layer):
            if not sub.subLayerPaths:
                current = list(parent_layer.subLayerPaths)
                if sub.identifier in current:
                    current.remove(sub.identifier)
                    parent_layer.subLayerPaths = current
            return True
    return False


class NodeGraphWidget(QtWidgets.QWidget):
    """Main widget: graph canvas + toolbar."""

    _suggestions_ready = QtCore.Signal(list)
    _active_instance = None  # class-level ref to the one open graph

    def __init__(self, parent=None, stage=None) -> None:
        if NodeGraphWidget._active_instance is not None:
            raise RuntimeError(
                "A node graph is already open. Close it before opening another."
            )
        super().__init__(parent)
        NodeGraphWidget._active_instance = self
        self._stage = stage
        # Patch NodeGraphQt bug: set_context_menu_from_file crashes with None path
        _orig = NodeGraph.set_context_menu_from_file
        NodeGraph.set_context_menu_from_file = lambda self, f=None: (
            None if f is None else _orig(self, f)
        )
        self._graph = NodeGraph()
        NodeGraph.set_context_menu_from_file = _orig
        nodes.UsdNodeBase._graph_ref = self._graph

        install_neon_theme(self)

        # Register all node types
        for node_cls in nodes.ALL_NODES:
            self._graph.register_node(node_cls)
        self._graph.register_node(GroupNode)

        # Auto-discover chunk nodes
        self._discovered_chunks = nodes.discover_chunk_nodes()
        for node_cls in self._discovered_chunks:
            self._graph.register_node(node_cls)

        # Layout
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._executor = GraphExecutor(self)

        # Toolbar
        toolbar = QtWidgets.QHBoxLayout()
        toolbar.setContentsMargins(6, 6, 6, 2)
        self._add_node_btn = QtWidgets.QPushButton("+ Add Node")
        self._add_node_btn.setStyleSheet(_BTN_STYLE)
        self._add_node_btn.clicked.connect(self._show_add_menu)
        self._exec_btn = QtWidgets.QPushButton("▶ Execute")
        self._exec_btn.setStyleSheet(_RUN_STYLE)
        self._exec_btn.clicked.connect(self._executor.run_full)
        self._save_btn = QtWidgets.QPushButton("💾 Save")
        self._save_btn.setStyleSheet(_BTN_STYLE)
        self._save_btn.clicked.connect(self._save_graph)
        self._load_btn = QtWidgets.QPushButton("📂 Load")
        self._load_btn.setStyleSheet(_BTN_STYLE)
        self._load_btn.clicked.connect(self._load_graph)
        toolbar.addWidget(self._add_node_btn)
        toolbar.addWidget(self._exec_btn)
        toolbar.addWidget(self._save_btn)
        toolbar.addWidget(self._load_btn)
        self._export_btn = QtWidgets.QPushButton("🐍 Export")
        self._export_btn.setStyleSheet(_BTN_STYLE)
        self._export_btn.clicked.connect(self._export_python)
        toolbar.addWidget(self._export_btn)
        self._viewport_btn = QtWidgets.QPushButton("👁 Viewport")
        self._viewport_btn.setStyleSheet(_BTN_STYLE)
        self._viewport_btn.setCheckable(True)
        self._viewport_btn.clicked.connect(self._toggle_viewport_preview)
        toolbar.addWidget(self._viewport_btn)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        # Splitter: graph canvas (left) + properties panel (right)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.setStyleSheet(
            "QSplitter::handle { background: #1e2438; width: 2px; }QSplitter { background: #08090c; }"
        )
        splitter.addWidget(self._graph.widget)

        self._props_widget = QtWidgets.QWidget()
        self._props_widget.setStyleSheet(PANEL_STYLE)
        self._props_layout = QtWidgets.QVBoxLayout(self._props_widget)
        self._props_layout.setContentsMargins(8, 8, 8, 0)
        self._props_layout.setSpacing(0)

        # ── Pinned header (title + type, never scrolls) ──
        self._props_label = QtWidgets.QLineEdit("Select a node")
        self._props_label.setReadOnly(True)
        self._props_label.setStyleSheet(
            "QLineEdit { font: bold 12px 'Segoe UI'; color: #c8d0e8;"
            "  background: transparent; border: none;"
            "  border-bottom: 1px solid #1e2438; padding: 2px 2px 4px 2px; }"
            "QLineEdit:focus { background: rgba(0,0,0,0.25); border: 1px solid #2a3550;"
            "  border-radius: 4px; padding: 2px 6px; }"
            "QLineEdit:read-only { color: #7a86a8; }"
        )
        self._props_layout.addWidget(self._props_label)
        self._props_type_label = QtWidgets.QLabel("")
        self._props_type_label.setStyleSheet(
            "color: #888; font: italic 10px 'Consolas'; padding: 0 2px 6px 2px; border-bottom: 1px solid #333;"
        )
        self._props_type_label.hide()
        self._props_layout.addWidget(self._props_type_label)
        self._props_error_label = QtWidgets.QLabel("")
        self._props_error_label.setStyleSheet(
            "color: #ff6b6b; background: rgba(220,60,60,0.12);"
            " border: 1px solid rgba(220,60,60,0.3); border-radius: 4px;"
            " padding: 4px 6px; font: 9px 'Consolas'; margin: 2px 0 4px 0;"
        )
        self._props_error_label.setWordWrap(True)
        self._props_error_label.hide()
        self._props_layout.addWidget(self._props_error_label)

        # ── Scrollable content area ──
        self._scroll_area = QtWidgets.QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._scroll_area.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
            "QScrollBar:vertical { background: #0c0e14; width: 6px; border-radius: 3px; }"
            "QScrollBar::handle:vertical { background: #1e2438; border-radius: 3px; min-height: 20px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        )
        _scroll_content = QtWidgets.QWidget()
        _scroll_content.setStyleSheet("background: transparent;")
        _scroll_layout = QtWidgets.QVBoxLayout(_scroll_content)
        _scroll_layout.setContentsMargins(0, 6, 0, 8)
        _scroll_layout.setSpacing(0)

        self._viewport_label = _ViewportLabel()
        self._viewport_label.setStyleSheet(
            "background: #08090c; border: 1px solid #1e2438; border-radius: 6px;"
        )
        self._viewport_label.setMinimumHeight(150)
        self._viewport_label.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred
        )
        self._viewport_label.hide()
        _scroll_layout.addWidget(self._viewport_label)
        self._viewport_timer = QtCore.QTimer(self)
        self._viewport_timer.timeout.connect(self._grab_viewport)

        self._props_form = QtWidgets.QFormLayout()
        self._props_form.setContentsMargins(0, 4, 0, 0)
        _scroll_layout.addLayout(self._props_form)
        _scroll_layout.addStretch()

        self._scroll_area.setWidget(_scroll_content)
        self._props_layout.addWidget(self._scroll_area)
        self._props_widget.setMinimumWidth(200)
        splitter.addWidget(self._props_widget)
        splitter.setSizes([600, 250])

        layout.addWidget(splitter)

        # Minimap overlay
        self._minimap = QtWidgets.QGraphicsView(self._graph.viewer().scene(), self)
        self._minimap.setFixedSize(200, 150)
        self._minimap.setStyleSheet(
            "border: 1px solid #1e2438; background: #08090c; border-radius: 6px;"
        )
        self._minimap.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._minimap.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._minimap.setInteractive(False)
        self._minimap.setRenderHint(QtGui.QPainter.Antialiasing)
        self._minimap.raise_()
        self._minimap_orig_paint = self._minimap.viewport().paintEvent
        self._minimap.viewport().paintEvent = self._paint_minimap_health
        self._minimap_timer = QtCore.QTimer(self)
        self._minimap_timer.timeout.connect(self._update_minimap)
        self._minimap_timer.start(500)

        # Wire node selection
        self._graph.node_selected.connect(self._on_node_selected)
        self._graph.node_double_clicked.connect(self._on_node_double_clicked)

        try:
            self._graph.port_connected.connect(self._on_ports_connected)
            self._graph.port_disconnected.connect(self._on_ports_disconnected)
        except Exception as e:
            print(f"[NodeGraph] could not attach port signals: {e}")

        try:
            self._graph.nodes_deleted.connect(self._on_nodes_deleted)
        except Exception as e:
            print(f"[NodeGraph] could not attach nodes_deleted signal: {e}")

        try:
            self._graph.undo_stack().indexChanged.connect(self._on_undo_index_changed)
        except Exception as e:
            print(f"[NodeGraph] could not attach undo stack signal: {e}")

        # Right-click context menu on viewer
        viewer = self._graph.viewer()
        if viewer:
            viewer.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
            viewer.customContextMenuRequested.connect(self._show_node_context_menu)
        self._prop_editors = {}
        self._suggestion_id = 0
        self._suggestions_ready.connect(self._show_ghost_buttons)
        self._init_timers = []  # repeating per-node timers collected at creation

        # Build the add-node menu
        self._node_menu = QtWidgets.QMenu(self)
        all_nodes = list(nodes.ALL_NODES) + self._discovered_chunks
        categories = {}
        for n in all_nodes:
            cat = getattr(n, "CATEGORY", "Other")
            categories.setdefault(cat, []).append(n)
        for category, node_list in categories.items():
            sub = self._node_menu.addMenu(category)
            for node_cls in node_list:
                action = sub.addAction(node_cls.NODE_NAME)
                action.setData(node_cls)
                action.triggered.connect(self._on_add_node)

        # Tab key interceptor on the graph viewer
        self._graph.widget.installEventFilter(self)
        viewer = self._graph.viewer()
        if viewer:
            viewer.installEventFilter(self)
            viewer.scene().installEventFilter(self)
            viewer.setAcceptDrops(True)
            self._orig_apply_live = viewer.apply_live_connection
            viewer.apply_live_connection = self._patched_apply_live
            viewer._orig_dragEnter = viewer.dragEnterEvent
            viewer._orig_drop = viewer.dropEvent
            viewer.dragEnterEvent = self._viewer_drag_enter
            viewer.dropEvent = self._viewer_drop
            scene = viewer.scene()
            if scene:
                scene.dragEnterEvent = self._viewer_drag_enter
                scene.dragMoveEvent = self._viewer_drag_enter
                scene.dropEvent = self._scene_drop
            viewer.dragMoveEvent = self._viewer_drag_enter

    def _patched_apply_live(self, event) -> None:
        viewer = self._graph.viewer()
        if viewer._LIVE_PIPE.isVisible():
            end_port = None
            for item in viewer.scene().items(event.scenePos()):
                if isinstance(item, PortItem):
                    end_port = item
                    break
            if end_port is None and viewer._start_port:
                self._pending_port = viewer._start_port
                self._pending_scene_pos = event.scenePos()
                self._orig_apply_live(event)
                QtCore.QTimer.singleShot(0, self._connect_from_drop)
                return
        self._orig_apply_live(event)

    def _connect_from_drop(self) -> None:
        port_item = self._pending_port
        scene_pos = self._pending_scene_pos
        self._pending_port = None

        src_is_output = port_item.port_type == PortTypeEnum.OUT.value
        dlg = _TabSearchDialog(
            self,
            port_type=port_item.port_type,
            dragged_dtype=getattr(port_item, "_data_type", "any"),
            dragged_is_output=src_is_output,
        )
        dlg.move(QtGui.QCursor.pos())
        if dlg.exec_() != QtWidgets.QDialog.Accepted or not dlg.selected_node:
            return

        node = self._create_node(dlg.selected_node, pos=(scene_pos.x(), scene_pos.y()))
        if not node:
            return

        g = self._active_graph()
        src_node = g.get_node_by_id(port_item.node.id)

        if src_is_output:
            src_port = next(
                (p for p in src_node.output_ports() if p.name() == port_item.name), None
            )
            if src_port:
                for p in node.input_ports():
                    try:
                        src_port.connect_to(p)
                        break
                    except Exception:
                        continue
        else:
            src_port = next(
                (p for p in src_node.input_ports() if p.name() == port_item.name), None
            )
            if src_port:
                for p in node.output_ports():
                    try:
                        p.connect_to(src_port)
                        break
                    except Exception:
                        continue

    def _active_graph(self) -> object:
        """Return the graph (main or sub-graph) for the currently active tab."""
        widget = self._graph.widget
        idx = widget.currentIndex()
        if idx == 0:
            return self._graph
        for sub in self._graph.sub_graphs.values():
            if widget.indexOf(sub.widget) == idx:
                return sub
        return self._graph

    # ── Tab menu ──────────────────────────────
    def eventFilter(self, obj, event) -> bool:
        try:
            if event.type() == QtCore.QEvent.GraphicsSceneMouseMove:
                try:
                    items = self._graph.viewer().scene().items(event.scenePos())
                    for item in items:
                        if isinstance(item, PipeItem):
                            port = getattr(item, "_output_port", None)
                            if port:
                                nid = port.node.id
                                src_node = self._graph.get_node_by_id(nid)
                                if src_node and hasattr(src_node, "_output_cache"):
                                    val = src_node._output_cache.get(port.name, "?")
                                    QtWidgets.QToolTip.showText(
                                        QtGui.QCursor.pos(), str(val)[:200]
                                    )
                                    break
                except Exception:
                    pass
            if event.type() == QtCore.QEvent.KeyPress:
                g = self._active_graph()
                # Ctrl+Alt activates pipe slicer by remapping to viewer's Alt+Shift state
                _ctrl_alt = QtCore.Qt.ControlModifier | QtCore.Qt.AltModifier
                if event.modifiers() == _ctrl_alt:
                    v = g.viewer()
                    v.ALT_state = True
                    v.SHIFT_state = True
                    v.setCursor(QtCore.Qt.CrossCursor)
                if event.key() == QtCore.Qt.Key_Tab:
                    self._show_search_menu()
                    return True
                if event.key() == QtCore.Qt.Key_Delete:
                    for pipe in g.selected_pipes():
                        try:
                            pipe.delete()
                        except Exception:
                            pass
                    g.delete_nodes(g.selected_nodes())
                    return True
                if (
                    event.key() == QtCore.Qt.Key_E
                    and event.modifiers() == QtCore.Qt.ShiftModifier
                ):
                    sel = g.selected_nodes()
                    if sel:
                        self._executor.run_from(sel[0])
                    else:
                        self._executor.run_full()
                    return True
                if event.key() == QtCore.Qt.Key_B:
                    self._add_backdrop()
                    return True
                if event.key() == QtCore.Qt.Key_F:
                    sel = g.selected_nodes()
                    if sel:
                        g.center_on(sel)
                    else:
                        g.fit_to_selection()
                    return True
                if event.key() == QtCore.Qt.Key_L:
                    sel = g.selected_nodes()
                    self._layout_nodes(sel if sel else None)
                    return True
                if event.key() == QtCore.Qt.Key_F2:
                    sel = g.selected_nodes()
                    if sel:
                        self._rename_selected_node(sel[0])
                    return True
                if (
                    event.key() == QtCore.Qt.Key_D
                    and event.modifiers() == QtCore.Qt.ControlModifier
                ):
                    for n in g.selected_nodes():
                        n.set_disabled(not n.disabled())
                    return True
                if (
                    event.key() == QtCore.Qt.Key_G
                    and event.modifiers() == QtCore.Qt.ControlModifier
                ):
                    self._group_selected()
                    return True
                if (
                    event.key() == QtCore.Qt.Key_C
                    and event.modifiers() == QtCore.Qt.ControlModifier
                ):
                    g.copy_nodes()
                    return True
                if (
                    event.key() == QtCore.Qt.Key_V
                    and event.modifiers() == QtCore.Qt.ControlModifier
                ):
                    g.paste_nodes()
                    return True
                if (
                    event.key() == QtCore.Qt.Key_X
                    and event.modifiers() == QtCore.Qt.ControlModifier
                ):
                    g.cut_nodes()
                    return True
                if (
                    event.key() == QtCore.Qt.Key_Z
                    and event.modifiers() == QtCore.Qt.ControlModifier
                ):
                    g.undo()
                    return True
                if (
                    event.key() == QtCore.Qt.Key_A
                    and event.modifiers() == QtCore.Qt.ControlModifier
                ):
                    g.select_all()
                    return True
            if event.type() == QtCore.QEvent.KeyRelease:
                g = self._active_graph()
                _ctrl_alt = QtCore.Qt.ControlModifier | QtCore.Qt.AltModifier
                if event.modifiers() != _ctrl_alt:
                    v = g.viewer()
                    if getattr(v, "SHIFT_state", False) and getattr(
                        v, "ALT_state", False
                    ):
                        v.ALT_state = False
                        v.SHIFT_state = False
                        v.unsetCursor()
            # Alt+Tab (or any focus loss) while the viewer is panning leaves
            # ALT_state=True forever because the KeyRelease never arrives.
            # Reset modifier state whenever the viewer loses keyboard focus.
            if event.type() == QtCore.QEvent.FocusOut:
                try:
                    v = self._active_graph().viewer()
                    v.ALT_state = False
                    if hasattr(v, "SHIFT_state"):
                        v.SHIFT_state = False
                    v.unsetCursor()
                except Exception:
                    pass
            return super().eventFilter(obj, event)
        except RuntimeError:
            return False

    def _update_minimap(self) -> None:
        try:
            sr = self._graph.viewer().scene().itemsBoundingRect()
            if not sr.isNull():
                self._minimap.fitInView(sr, QtCore.Qt.KeepAspectRatio)
            self._minimap.viewport().update()
        except Exception:
            pass

    def _paint_minimap_health(self, event) -> None:
        """Paint health-colored dots on the minimap for each node."""
        self._minimap_orig_paint(event)
        try:
            _STATE_COLORS = {
                "valid": QtGui.QColor(50, 200, 100, 200),
                "none": QtGui.QColor(100, 120, 150, 160),
                "error": QtGui.QColor(220, 60, 60, 220),
                "running": QtGui.QColor(220, 170, 40, 200),
                "skipped": QtGui.QColor(70, 80, 110, 160),
            }
            painter = QtGui.QPainter(self._minimap.viewport())
            if not painter.isActive():
                return
            painter.setRenderHint(QtGui.QPainter.Antialiasing)
            for n in self._graph.all_nodes():
                state = getattr(n.view, "_exec_state", "")
                if state not in _STATE_COLORS:
                    continue
                center = n.view.sceneBoundingRect().center()
                vp = self._minimap.mapFromScene(center)
                painter.setPen(QtCore.Qt.NoPen)
                painter.setBrush(_STATE_COLORS[state])
                painter.drawEllipse(vp, 4, 4)
            painter.end()
        except Exception:
            pass

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_minimap"):
            self._minimap.move(5, self.height() - 160)

    def _show_layer_edits(self, node) -> None:
        """Show the chunk's edit layer as USD text in a popup."""
        try:
            adapter = getattr(node, "_cached_adapter", None)
            if not adapter or not adapter.chunk:
                return
            layer = adapter.chunk.get_edit_context()
            if not layer:
                QtWidgets.QMessageBox.information(
                    self, "Layer Edits", "No layer edits found."
                )
                return
            text = layer.ExportToString()
            dlg = QtWidgets.QDialog(self)
            dlg.setWindowTitle(f"Layer Edits — {node.name()}")
            dlg.resize(600, 500)
            dlg.setStyleSheet("QDialog { background: #0c0e14; }")
            layout = QtWidgets.QVBoxLayout(dlg)
            layout.setContentsMargins(8, 8, 8, 8)
            edit = QtWidgets.QPlainTextEdit()
            edit.setReadOnly(True)
            edit.setPlainText(text or "(empty layer)")
            edit.setStyleSheet(
                "QPlainTextEdit { background: #14161e; color: #c8d0e8;"
                " font: 10px 'Consolas'; border: 1px solid #1e2438; border-radius: 6px; }"
            )
            layout.addWidget(edit)
            dlg.exec_()
        except Exception as e:
            print(f"[NodeGraph] show layer err: {e}")

    def _toggle_chunk_fold(self, node) -> None:
        """Toggle folding on a chunk node — hide unconnected _ ports when folded."""
        folded = not getattr(node, "_folded", True)
        node._folded = folded
        node.view._folded_state = folded
        for port in node.input_ports() + node.output_ports():
            if port.name() in ("prim", "layer"):
                continue
            if port.name().startswith("_"):
                if folded:
                    port.set_visible(bool(port.connected_ports()))
                else:
                    port.set_visible(True)
        node.view.draw_node()

    def _toggle_viewport_preview(self, checked) -> None:
        if checked:
            self._viewport_label.show()
            self._grab_viewport()
            self._viewport_timer.start(100)
        else:
            self._viewport_timer.stop()
            self._viewport_label.hide()

    def _grab_viewport(self) -> None:
        pixmap = GraphHost.current().get_viewport_screenshot()
        if pixmap and not pixmap.isNull():
            self._viewport_label.set_raw_pixmap(pixmap)
        else:
            self._viewport_label.setText("No viewport")

    def _show_pipe_context_menu(self, graph, pipe_item, pos) -> None:
        """Right-click on a connection wire — offers Delete Connection."""
        menu = QtWidgets.QMenu(self)
        menu.setStyleSheet(MENU_STYLE)
        in_name = pipe_item.input_port.name
        out_name = pipe_item.output_port.name
        header = menu.addAction(f"⟶  {out_name} → {in_name}")
        header.setEnabled(False)
        menu.addSeparator()
        del_act = menu.addAction("✂  Delete Connection")
        action = menu.exec_(QtGui.QCursor.pos())
        if action == del_act:
            try:
                in_node = graph.get_node_by_id(pipe_item.input_port.node.id)
                out_node = graph.get_node_by_id(pipe_item.output_port.node.id)
                in_port = in_node.get_input(in_name)
                out_port = out_node.get_output(out_name)
                if in_port and out_port:
                    in_port.disconnect_from(out_port)
            except Exception as e:
                print(f"[NodeGraph] disconnect err: {e}")

    def _show_port_context_menu(self, graph, viewer, port_item, pos) -> None:
        """Port-right-click menu: copy reference from output, paste/clear on input."""
        node = graph.get_node_by_id(port_item.node.id)
        port_name = port_item.name
        param_name = port_name[1:] if port_name.startswith("_") else port_name
        is_output = port_item.port_type == PortTypeEnum.OUT.value

        menu = QtWidgets.QMenu(self)
        menu.setStyleSheet(MENU_STYLE)
        menu.setTitle(f"{node.name()}.{param_name}")
        header = menu.addAction(f"🔌 {node.name()}.{param_name}")
        header.setEnabled(False)
        menu.addSeparator()

        is_sub = isinstance(graph, SubGraph)

        copy_act = paste_act = clear_act = promote_out_act = None
        if is_output:
            copy_act = menu.addAction("📋 Copy as Reference")
            if is_sub:
                dtype = getattr(port_item, "_data_type", "any")
                if dtype in ("prim", "layer"):
                    promote_out_act = menu.addAction("⬆ Promote Output to Group")
        else:
            paste_act = menu.addAction("📌 Paste Reference")
            paste_act.setEnabled(NodeGraphWidget._ref_clipboard is not None)
            adapter, chunk_param = self._resolve_chunk_param_for_port(
                node, node.get_input(port_name)
            )
            existing_ref = None
            if chunk_param is not None:
                existing_ref = getattr(chunk_param, "value_reference", None)
            if not existing_ref:
                prop_val = node.get_property(param_name) if param_name else None
                if isinstance(prop_val, str) and prop_val.startswith("@"):
                    existing_ref = prop_val
            if existing_ref:
                ref_info = menu.addAction(f"🔗 Ref: {existing_ref}")
                ref_info.setEnabled(False)
                clear_act = menu.addAction("✖ Clear Reference")

        action = menu.exec_(viewer.mapToGlobal(pos))
        if action is None:
            return

        if action == copy_act:
            _, chunk_param = self._resolve_chunk_param_for_port(
                node, node.get_output(port_name)
            )
            if chunk_param is not None:
                NodeGraphWidget._ref_clipboard = self._build_chunk_param_ref(
                    node, chunk_param
                )
            else:
                NodeGraphWidget._ref_clipboard = f"@{node.name()}.{param_name}"
            print(f"[NodeGraph] copied ref: {NodeGraphWidget._ref_clipboard}")
            return

        if action == promote_out_act and promote_out_act is not None:
            dtype = getattr(port_item, "_data_type", "any")
            color = PORT_COLORS.get(dtype, (140, 150, 170))
            self._promote_output_to_group(node, port_name, color)
            return

        if action == paste_act and NodeGraphWidget._ref_clipboard:
            ref = NodeGraphWidget._ref_clipboard
            adapter, chunk_param = self._resolve_chunk_param_for_port(
                node, node.get_input(port_name)
            )
            try:
                if chunk_param is not None and hasattr(chunk_param, "value_reference"):
                    chunk_param.value_reference = ref
                else:
                    node.set_property(param_name, ref)
                try:
                    node.set_property(param_name, ref)
                except Exception:
                    pass
                print(f"[NodeGraph] pasted ref {ref} -> {node.name()}.{param_name}")
            except Exception as e:
                print(f"[NodeGraph] paste ref err: {e}")
            try:
                if self._selected_node is node:
                    self._on_node_selected(node)
            except Exception:
                pass
            return

        if action == clear_act:
            _, chunk_param = self._resolve_chunk_param_for_port(
                node, node.get_input(port_name)
            )
            if chunk_param is not None:
                try:
                    chunk_param.value_reference = None
                except Exception as e:
                    print(f"[NodeGraph] clear ref err: {e}")
            try:
                node.set_property(param_name, "")
            except Exception:
                pass
            try:
                if self._selected_node is node:
                    self._on_node_selected(node)
            except Exception:
                pass

    def _show_node_context_menu(self, pos) -> None:
        g = self._active_graph()
        viewer = g.viewer()
        scene_pos = viewer.mapToScene(pos)
        items = viewer.scene().items(scene_pos)
        # Check for wire/pipe under cursor first
        for item in items:
            if isinstance(item, PipeItem) and item.input_port and item.output_port:
                self._show_pipe_context_menu(g, item, pos)
                return
        for item in items:
            if isinstance(item, PortItem):
                self._show_port_context_menu(g, viewer, item, pos)
                return
        clicked_node = None
        for item in items:
            if isinstance(item, NodeItem):
                clicked_node = g.get_node_by_id(item.id)
                break
        if clicked_node:
            g.clear_selection()
            clicked_node.set_selected(True)

        selected = g.selected_nodes()

        is_sub = isinstance(g, SubGraph)

        menu = QtWidgets.QMenu(self)
        menu.setStyleSheet(MENU_STYLE)

        act_add_in = act_add_out = None
        if is_sub:
            act_add_in = menu.addAction("📥 Add Input Port")
            act_add_out = menu.addAction("📤 Add Output Port")
            if selected:
                menu.addSeparator()

        act_exec = act_disable = act_dup = act_del = act_color = act_frame = (
            act_fold
        ) = act_layer = act_export_chunk = None
        if selected:
            node = selected[0]
            act_exec = menu.addAction("▶ Execute from here")
            act_disable = menu.addAction(
                "🚫 Disable" if not node.disabled() else "✅ Enable"
            )
            if hasattr(node, "_chunk_module"):
                is_folded = getattr(node, "_folded", True)
                act_fold = menu.addAction(
                    "📂 Expand Ports" if is_folded else "📁 Collapse Ports"
                )
                act_layer = menu.addAction("📄 Show Layer Edits")
            if isinstance(node, GroupNode):
                act_export_chunk = menu.addAction("📦 Export as Chunk")
            act_dup = menu.addAction("📋 Duplicate")
            act_del = menu.addAction("🗑 Delete")
            act_color = menu.addAction("🎨 Change Color")
            if isinstance(node, nodes.PrimPathNode):
                act_frame = menu.addAction("🎯 Frame in Viewport")
            menu.addSeparator()
        if not menu.actions():
            return

        action = menu.exec_(QtGui.QCursor.pos())
        if not action:
            return

        if action == act_add_in and is_sub:
            group = g.node
            idx = len(group.input_ports())
            group.add_input(f"in_{idx}")
        elif action == act_add_out and is_sub:
            group = g.node
            idx = len(group.output_ports())
            group.add_output(f"out_{idx}")
        elif selected:
            node = selected[0]
            if action == act_exec:
                self._executor.run_from(node)
            elif action == act_disable:
                for n in selected:
                    n.set_disabled(not n.disabled())
            elif action == act_dup:
                g.copy_nodes()
                g.paste_nodes()
            elif action == act_del:
                g.delete_nodes(selected)
            elif action == act_color:
                color = QtWidgets.QColorDialog.getColor()
                if color.isValid():
                    for n in selected:
                        n.set_color(color.red(), color.green(), color.blue())
            elif action == act_frame and isinstance(node, nodes.PrimPathNode):
                self._on_node_double_clicked(node)
            elif action == act_fold and hasattr(node, "_chunk_module"):
                self._toggle_chunk_fold(node)
            elif action == act_layer and hasattr(node, "_chunk_module"):
                self._show_layer_edits(node)
            elif action == act_export_chunk and isinstance(node, GroupNode):
                self._export_group_as_chunk(node)

    def _export_group_as_chunk(self, group_node) -> None:
        """Export GroupNode as a chunk module and hot-register it."""
        g = self._active_graph()
        already_expanded = group_node.get_sub_graph() is not None
        sub = (
            group_node.get_sub_graph()
            if already_expanded
            else g.expand_group_node(group_node)
        )
        try:
            out_path = export_group_as_chunk(group_node, sub=sub)
            QtWidgets.QMessageBox.information(
                self,
                "Chunk Exported",
                f"Exported to:\n{out_path}\n\nChunk '{group_node.name()}' is now available in the panel.",
            )
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Export Failed", str(e))
        finally:
            if not already_expanded and sub:
                g.collapse_group_node(group_node)

    def _set_exec_state(self, node, state: str, error_msg: str = "") -> None:
        """Set persistent execution state on a node view item."""
        try:
            view = node.view
            view._exec_state = state
            view._exec_error = error_msg
            node._exec_error_msg = error_msg if state == "error" else ""
            if state == "error" and error_msg:
                view.setToolTip(error_msg)
            elif state == "none":
                view.setToolTip("No output")
            elif state == "skipped":
                view.setToolTip("Skipped — upstream error")
            else:
                view.setToolTip("")
            view.update()
            if state in ("valid", "none"):
                QtCore.QTimer.singleShot(
                    1500,
                    lambda v=view: (
                        (setattr(v, "_exec_state", ""), v.update())
                        if getattr(v, "_exec_state", "") == state
                        else None
                    ),
                )
            # Refresh side panel error label if this node is currently selected
            if node is getattr(self, "_selected_node", None):
                if state == "error" and error_msg:
                    self._props_error_label.setText(error_msg)
                    self._props_error_label.show()
                else:
                    self._props_error_label.setText("")
                    self._props_error_label.hide()
        except Exception:
            pass

    def _setup_sub_graph(self, sub) -> None:
        """Install eventFilter and context menu on a sub-graph viewer."""
        viewer = sub.viewer()
        if not viewer:
            return
        sub.set_background_color(8, 9, 12)
        sub.set_grid_color(30, 36, 56)
        sub.set_grid_mode(1)
        viewer.installEventFilter(self)
        viewer.scene().installEventFilter(self)
        viewer.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        viewer.customContextMenuRequested.connect(self._show_node_context_menu)
        sub.node_selected.connect(self._on_node_selected)
        sub.node_double_clicked.connect(self._on_node_double_clicked)
        orig = viewer.apply_live_connection

        def _patched(event, _orig=orig, _viewer=viewer) -> None:
            if _viewer._LIVE_PIPE.isVisible():
                end_port = None
                for item in _viewer.scene().items(event.scenePos()):
                    if isinstance(item, PortItem):
                        end_port = item
                        break
                if end_port is None and _viewer._start_port:
                    self._pending_port = _viewer._start_port
                    self._pending_scene_pos = event.scenePos()
                    _orig(event)
                    QtCore.QTimer.singleShot(0, self._connect_from_drop)
                    return
            _orig(event)

        viewer.apply_live_connection = _patched

        for n in sub.all_nodes():
            if getattr(n, "_folded", False):
                for w in n.view._widgets.values():
                    w.setVisible(False)

    # ------------------------------------------------------------------
    # Wire handlers — bridge node-graph pipes to the chunk_core notice DAG.
    # ------------------------------------------------------------------
    def _resolve_chunk_param_for_port(self, node, port) -> object | None:
        """Return (adapter, chunk_param) for a port on a chunk-backed node, or (None, None)."""
        display = getattr(node, "_chunk_display_name", None)
        if not display:
            return None, None
        port_name = port.name() if port else None
        if not port_name:
            return None, None
        if not port_name.startswith("_"):
            return None, None
        leaf_name = port_name[1:]
        try:
            adapter = getattr(node, "_cached_adapter", None)
            if adapter is None or not getattr(adapter, "chunk", None):
                my_chunk = None
                if hasattr(node, "_ensure_chunk"):
                    try:
                        my_chunk = node._ensure_chunk()
                    except Exception:
                        my_chunk = None
                if my_chunk is not None:
                    adapter = ChunkAdapter(display, chunk=my_chunk)
                else:
                    adapter = ChunkAdapter(display, auto_create=False)
                if adapter.chunk:
                    node._cached_adapter = adapter
            if not adapter or not adapter.chunk:
                return None, None

            p = adapter.chunk.get_param(leaf_name)
            if isinstance(p, chunk_core.ChunkParamBase):
                return adapter, p

            def _walk(container) -> None:
                try:
                    params = container.get_params()
                except Exception:
                    return None
                for p in params:
                    if isinstance(p, chunk_core.ChunkGroupBase):
                        hit = _walk(p)
                        if hit is not None:
                            return hit
                    elif isinstance(p, chunk_core.ChunkVectorBase):
                        for sub in p:
                            if getattr(sub, "name", None) == leaf_name:
                                return sub
                    else:
                        if getattr(p, "name", None) == leaf_name:
                            return p
                return None

            found = _walk(adapter.chunk)
            return adapter, found
        except Exception:
            return None, None

    def _on_ports_connected(self, in_port, out_port) -> None:
        if in_port.name() == "trigger":
            QtCore.QTimer.singleShot(0, lambda: _safe_port_visible(in_port, True))
        try:
            in_node = in_port.node()
            out_node = out_port.node()
            _, dst_param = self._resolve_chunk_param_for_port(in_node, in_port)
            _, src_param = self._resolve_chunk_param_for_port(out_node, out_port)
            if dst_param is not None and src_param is not None:
                dst_param.wire_from(src_param)
        except Exception as e:
            print(f"[NodeGraph] wire connect err: {e}")
        try:
            self._executor.run_from(out_port.node())
        except Exception:
            pass
        QtCore.QTimer.singleShot(
            0, lambda: self._refresh_panel_if_selected(in_port.node())
        )

    def _on_ports_disconnected(self, in_port, out_port) -> None:
        if in_port.name() == "trigger" and not in_port.connected_ports():
            QtCore.QTimer.singleShot(0, lambda: _safe_port_visible(in_port, False))
        try:
            in_node = in_port.node()
            out_node = out_port.node()
            _, dst_param = self._resolve_chunk_param_for_port(in_node, in_port)
            _, src_param = self._resolve_chunk_param_for_port(out_node, out_port)
            if dst_param is not None:
                dst_param.unwire(src_param)
        except Exception as e:
            print(f"[NodeGraph] wire disconnect err: {e}")
        try:
            self._executor.run_from(in_port.node())
        except Exception:
            pass
        QtCore.QTimer.singleShot(
            0, lambda: self._refresh_panel_if_selected(in_port.node())
        )

    def _refresh_panel_if_selected(self, node) -> None:
        if node is getattr(self, "_selected_node", None):
            self._on_node_selected(node)

    def _on_nodes_deleted(self, node_list) -> None:
        """Remove each deleted chunk node's Sdf.Layer from the stage layer stack."""
        for node_id in node_list:  # signal sends string IDs, not node objects
            layer_id = self._executor._node_layer_registry.pop(node_id, None)
            if not layer_id or not self._stage:
                continue
            layer = Sdf.Layer.Find(layer_id)
            if not layer:
                continue
            try:
                _remove_layer_from_hierarchy(self._stage.GetRootLayer(), layer)
            except Exception as e:
                print(f"[NodeGraph] layer cleanup err for {node_id}: {e}")
        # Force full scene repaint to clear rendering artifacts
        try:
            viewer = self._graph.viewer()
            if viewer:
                QtCore.QTimer.singleShot(
                    50,
                    lambda: (viewer.scene().invalidate(), viewer.viewport().update()),
                )
        except Exception:
            pass

    def _on_undo_index_changed(self, index) -> None:
        QtCore.QTimer.singleShot(0, self._executor.run_full)

    def _on_node_double_clicked(self, node) -> None:
        if isinstance(node, GroupNode):
            sub = self._graph.expand_group_node(node)
            if sub:
                self._setup_sub_graph(sub)
        elif isinstance(node, nodes.PrimPathNode):
            pp = node.get_property("prim_path")
            if pp:
                try:
                    host = GraphHost.current()
                    stage = host.get_stage()
                    if stage:
                        prim = stage.GetPrimAtPath(pp)
                        if prim.IsValid():
                            host.set_selection(prim)
                            host.update_viewport()
                except Exception:
                    pass

    def _rename_selected_node(self, node=None) -> None:
        if node is None:
            node = getattr(self, "_selected_node", None)
        if not node:
            return
        new_name, ok = QtWidgets.QInputDialog.getText(
            self, "Rename Node", "Name:", text=node.name()
        )
        if ok and new_name.strip():
            new_name = new_name.strip()
            node.set_name(new_name)
            self._props_label.setText(new_name)
            type_name = getattr(node, "NODE_NAME", "")
            if type_name and type_name != new_name:
                self._props_type_label.setText(f"Type: {type_name}")
                self._props_type_label.show()
            else:
                self._props_type_label.hide()

    def _on_node_selected(self, node) -> None:
        while self._props_form.count():
            item = self._props_form.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._prop_editors.clear()

        if not node:
            self._props_label.setReadOnly(True)
            self._props_label.setText("Select a node")
            try:
                self._props_label.editingFinished.disconnect()
            except RuntimeError:
                pass
            self._props_type_label.hide()
            self._props_error_label.hide()
            return

        type_name = getattr(node, "NODE_NAME", "")
        self._props_label.setReadOnly(False)
        self._props_label.setText(node.name())
        if type_name and type_name != node.name():
            self._props_type_label.setText(f"Type: {type_name}")
            self._props_type_label.show()
        else:
            self._props_type_label.hide()
        self._selected_node = node
        self._node_name_edit = self._props_label
        err = getattr(node, "_exec_error_msg", "")
        exec_state = getattr(getattr(node, "view", None), "_exec_state", "")
        if err and exec_state == "error":
            self._props_error_label.setText(err)
            self._props_error_label.show()
        else:
            self._props_error_label.setText("")
            self._props_error_label.hide()

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                self._props_label.editingFinished.disconnect()
            except Exception:
                pass

        def _do_rename(_node=node) -> None:
            new_name = self._props_label.text().strip()
            if not new_name:
                self._props_label.setText(_node.name())
                return
            if new_name != _node.name():
                _node.set_name(new_name)

        self._props_label.editingFinished.connect(_do_rename)

        if hasattr(node, "output_ports") and node.output_ports():
            self._suggestion_id += 1
            sid = self._suggestion_id
            try:
                from .ai_suggest import suggest_next_nodes, get_graph_context

                ctx = get_graph_context(self._graph)
                suggest_next_nodes(
                    ctx, lambda names, _sid=sid: self._on_ghost_suggestions(names, _sid)
                )
            except Exception:
                pass

        self._prop_editors.update(
            build_node_panel(
                node,
                self._props_form,
                on_changed=self._executor.mark_dirty,
                active_graph_fn=self._active_graph,
                ref_menu_fn=self._setup_ref_menu,
                get_prim_attrs_fn=self._get_prim_attrs,
            )
        )

    def _on_ghost_suggestions(self, suggested_names, sid) -> None:
        if not getattr(self, "_executor", None):
            return
        if sid == self._suggestion_id and suggested_names:
            self._suggestions_ready.emit(suggested_names)

    def _show_ghost_buttons(self, names) -> None:
        print(f"[ghost] showing buttons: {names}", file=sys.stderr)
        if not getattr(self, "_executor", None):
            return
        if not names or not hasattr(self, "_selected_node"):
            return
        try:
            out_types = {
                getattr(p.view, "_data_type", "any")
                for p in self._selected_node.output_ports()
            }
            if out_types and "any" not in out_types:
                all_node_list = list(nodes.ALL_NODES) + getattr(
                    self, "_discovered_chunks", []
                )
                node_map = {n.NODE_NAME: n for n in all_node_list}

                def _compat(name) -> bool:
                    cls = node_map.get(name)
                    if not cls:
                        return True
                    pt = nodes._get_node_port_types(cls)
                    return bool(out_types & pt["inputs"]) or "any" in pt["inputs"]

                names = [n for n in names if _compat(n)]
        except Exception:
            pass
        label = QtWidgets.QLabel("⭐ Suggested next:")
        label.setStyleSheet("color: #88aaff; font-size: 11px; margin-top: 8px;")
        self._props_form.addRow(label)
        for name in names:
            btn = QtWidgets.QPushButton(f"+ {name}")
            btn.setStyleSheet("text-align: left; padding: 3px 8px;")
            btn.setProperty("node_name", name)
            btn.clicked.connect(self._on_ghost_clicked)
            self._props_form.addRow(btn)
        self._props_widget.update()

    def _on_ghost_clicked(self) -> None:
        name = self.sender().property("node_name")
        all_nodes = list(nodes.ALL_NODES) + getattr(self, "_discovered_chunks", [])
        cls = next((n for n in all_nodes if n.NODE_NAME == name), None)
        if cls and hasattr(self, "_selected_node"):
            pos = self._selected_node.pos()
            node = self._create_node(cls, pos=(pos[0] + 250, pos[1]))
            if node:
                for out_port in self._selected_node.output_ports():
                    for in_port in node.input_ports():
                        try:
                            out_port.connect_to(in_port)
                            return
                        except Exception:
                            continue

    def _save_graph(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save Graph", "", "Node Graph (*.json)"
        )
        if path:
            self._graph.save_session(path)

    def _load_graph(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Load Graph", "", "Node Graph (*.json)"
        )
        if path:
            self._graph.load_session(path)
            QtCore.QTimer.singleShot(0, self._executor.run_full)

    def _export_python(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export Python", "", "Python (*.py)"
        )
        if not path:
            return
        export_python_script(self._graph, path)

    def _get_prim_attrs(self, node) -> list[str]:
        """Get attribute names from the prim connected to this node."""
        try:
            stage = _cr.ChunkRegistry().scene.get_stage()
            if hasattr(node, "_output_cache") and node._output_cache.get("prim"):
                prim = node._output_cache["prim"]
                if hasattr(prim, "GetAttributes"):
                    return sorted([a.GetName() for a in prim.GetAttributes()])
            port = node.get_input("prim")
            if port and port.connected_ports():
                src = port.connected_ports()[0].node()
                if hasattr(src, "_output_cache") and src._output_cache.get("prim"):
                    prim = src._output_cache["prim"]
                    if hasattr(prim, "GetAttributes"):
                        return sorted([a.GetName() for a in prim.GetAttributes()])
                if hasattr(src, "get_property"):
                    pp = src.get_property("prim_path")
                    if pp:
                        prim = stage.GetPrimAtPath(pp)
                        if prim and prim.IsValid():
                            return sorted([a.GetName() for a in prim.GetAttributes()])
        except Exception:
            pass
        return []

    def _promote_param_to_group(self, inner_node, param) -> None:
        """Promote a chunk param to the enclosing GroupNode as an input port."""
        g = self._active_graph()
        if not isinstance(g, SubGraph):
            return
        group = g.node

        param_name = getattr(param, "name", None)
        if not param_name:
            return

        port_color = PORT_COLORS.get("any", (140, 150, 170))
        chunk_param_class = "LineEdit"
        chunk_param_kwargs: dict = {}

        if isinstance(param, (Slider, Spinbox)):
            port_color = PORT_COLORS.get("float", (50, 210, 220))
            chunk_param_class = type(param).__name__
            chunk_param_kwargs = {
                "min_value": getattr(param, "min_value", None),
                "max_value": getattr(param, "max_value", None),
                "is_int": getattr(param, "is_int", False),
            }
        elif isinstance(param, Menu):
            chunk_param_class = "Menu"
            chunk_param_kwargs = {"menu_items": list(getattr(param, "menu_items", []))}
        elif isinstance(param, Checkbox):
            port_color = PORT_COLORS.get("bool", (230, 210, 50))
            chunk_param_class = "Checkbox"

        current_val = getattr(param, "value", None)
        promotions = _get_promotions(group)

        existing_ports = {p.name() for p in group.input_ports()}
        if param_name not in existing_ports:
            group.add_input(param_name, color=port_color)

        promotions[param_name] = {
            "inner_node_id": inner_node.id,
            "inner_node_name": inner_node.name(),
            "inner_param": param_name,
            "port_color": list(port_color),
            "chunk_param_class": chunk_param_class,
            "chunk_param_kwargs": chunk_param_kwargs,
            "default_value": current_val,
        }
        _save_promotions(group)
        print(
            f"[NodeGraph] promoted '{param_name}' from {inner_node.name()} → {group.name()}"
        )

    def _promote_prop_to_group(self, inner_node, prop_name, value) -> None:
        """Promote a regular node property to the enclosing GroupNode as an input port."""
        g = self._active_graph()
        if not isinstance(g, SubGraph):
            return
        group = g.node

        port_color = PORT_COLORS.get("any", (140, 150, 170))
        promotions = _get_promotions(group)

        existing_ports = {p.name() for p in group.input_ports()}
        if prop_name not in existing_ports:
            group.add_input(prop_name, color=port_color)

        promotions[prop_name] = {
            "inner_node_id": inner_node.id,
            "inner_node_name": inner_node.name(),
            "inner_param": prop_name,
            "port_color": list(port_color),
            "chunk_param_class": "LineEdit",
            "chunk_param_kwargs": {},
            "default_value": value,
        }
        _save_promotions(group)
        print(
            f"[NodeGraph] promoted prop '{prop_name}' from {inner_node.name()} → {group.name()}"
        )

    def _promote_output_to_group(self, inner_node, port_name, port_color) -> None:
        """Promote an inner output port to the enclosing GroupNode as an output port."""
        g = self._active_graph()
        if not isinstance(g, SubGraph):
            return
        group = g.node

        promo_outs = _get_promoted_outputs(group)
        existing_ports = {p.name() for p in group.output_ports()}
        if port_name not in existing_ports:
            group.add_output(
                port_name,
                color=tuple(port_color)
                if isinstance(port_color, (list, tuple))
                else port_color,
            )

        promo_outs[port_name] = {
            "inner_node_id": inner_node.id,
            "inner_port": port_name,
            "port_color": list(port_color)
            if isinstance(port_color, (list, tuple))
            else list(port_color),
        }
        _save_promotions(group)
        print(
            f"[NodeGraph] promoted output '{port_name}' from {inner_node.name()} → {group.name()}"
        )

    def _add_chunk_widget(self, factory, param, node) -> None:
        """Create a chunk widget for a param and add it to the side panel."""
        if isinstance(param, chunk_core.ChunkGroupBase):
            for sub in param.get_params():
                self._add_chunk_widget(factory, sub, node)
            return
        if isinstance(param, chunk_core.ChunkVectorBase):
            for sub in param:
                self._add_chunk_widget(factory, sub, node)
            return
        try:
            widget = factory.create_widget(param)
        except Exception:
            return
        if widget is None or not isinstance(widget, QtWidgets.QWidget):
            return
        label = getattr(param, "label", None) or getattr(param, "name", "")
        prop_name = param.name
        if prop_name in node.model.custom_properties:

            def _make_sync(pn, w) -> None:
                def _sync(*args) -> None:
                    try:
                        node.set_property(pn, str(w.get_value() or ""))
                    except Exception:
                        pass

                return _sync

            if hasattr(widget, "value_changed") and hasattr(
                widget.value_changed, "connect"
            ):
                widget.value_changed.connect(_make_sync(prop_name, widget))
        try:
            adapter = getattr(node, "_cached_adapter", None)
            if adapter is None or not getattr(adapter, "chunk", None):
                display = getattr(node, "_chunk_display_name", None)
                if display:
                    my_chunk = None
                    if hasattr(node, "_ensure_chunk"):
                        try:
                            my_chunk = node._ensure_chunk()
                        except Exception:
                            my_chunk = None
                    if my_chunk is not None:
                        adapter = ChunkAdapter(display, chunk=my_chunk)
                    else:
                        adapter = ChunkAdapter(display, auto_create=False)
                    if adapter.chunk:
                        node._cached_adapter = adapter
            if adapter and getattr(adapter, "chunk", None):
                self._setup_chunk_ref_menu(widget, node, param)
        except Exception as e:
            print(f"[NodeGraph] chunk ref menu err: {e}")
        self._props_form.addRow(label, widget)

    _ref_clipboard = None

    def _setup_ref_menu(self, edit, node, prop_name) -> None:
        """Add right-click Copy/Paste Reference to a QLineEdit."""
        edit.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)

        def _show_menu(pos, e=edit, n=node, p=prop_name) -> None:
            menu = e.createStandardContextMenu()
            menu.addSeparator()
            copy_act = menu.addAction("📋 Copy Reference")
            paste_act = menu.addAction("📌 Paste Reference")
            paste_act.setEnabled(NodeGraphWidget._ref_clipboard is not None)
            promote_act = None
            g = self._active_graph()
            if isinstance(g, SubGraph):
                menu.addSeparator()
                promote_act = menu.addAction("⬆ Promote to Group")
            action = menu.exec_(e.mapToGlobal(pos))
            if action == copy_act:
                NodeGraphWidget._ref_clipboard = f"@{n.name()}.{p}"
            elif action == paste_act and NodeGraphWidget._ref_clipboard:
                e.setText(NodeGraphWidget._ref_clipboard)
                e.editingFinished.emit()
            elif action == promote_act and promote_act is not None:
                self._promote_prop_to_group(n, p, e.text())
                g2 = self._active_graph()
                if isinstance(g2, SubGraph):
                    QtCore.QTimer.singleShot(0, lambda _n=n: self._on_node_selected(_n))

        edit.customContextMenuRequested.connect(_show_menu)

    @staticmethod
    def _build_chunk_param_ref(node, param) -> str:
        """Build '@NodeName.dotted.path.to.param' by walking the param up to its chunk root."""
        try:
            chunk_obj = param.get_chunk() if hasattr(param, "get_chunk") else None
        except Exception:
            chunk_obj = None
        parts = []
        cur = param
        while cur is not None and cur is not chunk_obj:
            nm = getattr(cur, "name", None)
            if not nm:
                break
            parts.append(nm)
            cur = cur.get_owner() if hasattr(cur, "get_owner") else None
        parts.reverse()
        if not parts:
            parts = [getattr(param, "name", "")]
        return f"@{node.name()}.{'.'.join(parts)}"

    def _setup_chunk_ref_menu(self, widget, node, param) -> None:
        """Attach Copy/Paste Reference menu to QLineEdit children of a chunk widget."""
        edits = widget.findChildren(QtWidgets.QLineEdit)
        if not edits:
            edits = [widget]

        for ed in edits:
            try:
                ed.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
            except Exception:
                continue

            def _show_menu(pos, e=ed, n=node, p=param) -> None:
                menu = (
                    e.createStandardContextMenu()
                    if isinstance(e, QtWidgets.QLineEdit)
                    else QtWidgets.QMenu(e)
                )
                menu.setAttribute(QtCore.Qt.WA_TranslucentBackground, False)
                menu.setStyleSheet(
                    "QMenu { background: #0e1016; color: #c8d0e8;"
                    " border: 1px solid #1e2438; padding: 2px; }"
                    "QMenu::item { padding: 4px 20px 4px 8px; }"
                    "QMenu::item:selected { background: #1e2a40; }"
                    "QMenu::separator { height: 1px; background: #1e2438; margin: 2px 0; }"
                )
                menu.addSeparator()
                copy_act = menu.addAction("📋 Copy Reference")
                paste_act = menu.addAction("📌 Paste Reference")
                clear_act = menu.addAction("✖ Clear Reference")
                paste_act.setEnabled(NodeGraphWidget._ref_clipboard is not None)
                clear_act.setEnabled(bool(getattr(p, "value_reference", None)))
                promote_act = None
                g = self._active_graph()
                if isinstance(g, SubGraph):
                    menu.addSeparator()
                    promote_act = menu.addAction("⬆ Promote to Group")
                action = menu.exec_(e.mapToGlobal(pos))
                if action == copy_act:
                    NodeGraphWidget._ref_clipboard = self._build_chunk_param_ref(n, p)
                elif action == paste_act and NodeGraphWidget._ref_clipboard:
                    try:
                        p.value_reference = NodeGraphWidget._ref_clipboard
                        if isinstance(e, QtWidgets.QLineEdit):
                            e.setText(NodeGraphWidget._ref_clipboard)
                        QtCore.QTimer.singleShot(
                            0,
                            lambda _n=n: (
                                self._executor.run_from(_n) if self._stage else None
                            ),
                        )
                    except Exception as err:
                        print(f"[NodeGraph] paste ref err: {err}")
                elif action == clear_act:
                    try:
                        p.value_reference = None
                        if isinstance(e, QtWidgets.QLineEdit):
                            e.setText(str(getattr(p, "value", "") or ""))
                        QtCore.QTimer.singleShot(
                            0,
                            lambda _n=n: (
                                self._executor.run_from(_n) if self._stage else None
                            ),
                        )
                    except Exception as err:
                        print(f"[NodeGraph] clear ref err: {err}")
                elif action == promote_act and promote_act is not None:
                    self._promote_param_to_group(n, p)
                    g2 = self._active_graph()
                    if isinstance(g2, SubGraph):
                        QtCore.QTimer.singleShot(
                            0, lambda _n=n: self._on_node_selected(_n)
                        )

            ed.customContextMenuRequested.connect(_show_menu)

    def _viewer_drag_enter(self, event) -> None:
        event.acceptProposedAction()

    def _viewer_drop(self, event) -> None:
        print("[drop] viewer drop fired", file=sys.stderr)
        self._handle_drop(event, use_scene_pos=False)

    def _scene_drop(self, event) -> None:
        print("[drop] scene drop fired", file=sys.stderr)
        self._handle_drop(event, use_scene_pos=True)

    def _extract_drop_text(self, mime) -> str | None:
        """Extract text from various mime formats."""
        if mime.hasUrls():
            return mime.urls()[0].toLocalFile()
        if mime.hasText():
            return mime.text().strip()
        if mime.hasFormat("application/x-qabstractitemmodeldatalist"):
            data = mime.data("application/x-qabstractitemmodeldatalist")
            stream = QtCore.QDataStream(data, QtCore.QIODevice.ReadOnly)
            while not stream.atEnd():
                stream.readInt32()
                stream.readInt32()
                count = stream.readInt32()
                for _ in range(count):
                    role = stream.readInt32()
                    value = stream.readQVariant()
                    if role == 0:
                        return str(value).strip()
        return ""

    def _handle_drop(self, event, use_scene_pos=False) -> None:
        text = self._extract_drop_text(event.mimeData())

        if not text:
            event.ignore()
            return

        _IMG_EXTS = {
            ".png",
            ".jpg",
            ".jpeg",
            ".exr",
            ".tga",
            ".bmp",
            ".tif",
            ".tiff",
            ".hdr",
            ".tx",
        }
        if os.path.isfile(text) and os.path.splitext(text)[1].lower() in _IMG_EXTS:
            if use_scene_pos:
                pos = (event.scenePos().x(), event.scenePos().y())
            else:
                viewer = self._graph.viewer()
                sp = viewer.mapToScene(event.pos())
                pos = (sp.x(), sp.y())
            node = self._create_node(nodes.TextureFileNode, pos=pos)
            if node:
                node.set_property("file_path", text)
            event.acceptProposedAction()
            return

        if not text.startswith("/"):
            try:
                _host = GraphHost.current()
                stage = _host.get_stage()
                sel = _host.get_selection()
                if sel and sel[0].GetAttribute(text).IsValid():
                    if use_scene_pos:
                        pos = (event.scenePos().x(), event.scenePos().y())
                    else:
                        viewer = self._graph.viewer()
                        sp = viewer.mapToScene(event.pos())
                        pos = (sp.x(), sp.y())
                    prim_node = self._create_node(
                        nodes.PrimPathNode, pos=(pos[0] - 200, pos[1])
                    )
                    attr_node = self._create_node(nodes.GetAttributeNode, pos=pos)
                    if prim_node and attr_node:
                        prim_node.set_property("prim_path", str(sel[0].GetPath()))
                        attr_node.set_property("attr_name", text)
                        out = next(
                            (p for p in prim_node.output_ports() if p.name() == "prim"),
                            None,
                        )
                        inp = next(
                            (p for p in attr_node.input_ports() if p.name() == "prim"),
                            None,
                        )
                        if out and inp:
                            out.connect_to(inp)
                        if self._stage:
                            self._executor.run_from(prim_node)
                    event.acceptProposedAction()
                    return

                if sel:
                    text = str(sel[0].GetPath())
                else:
                    for p in stage.Traverse():
                        if p.GetName() == text or text in p.GetName():
                            text = str(p.GetPath())
                            break
            except Exception as e:
                print(f"[drop] resolve failed: {e}", file=sys.stderr)

        if not text:
            event.ignore()
            return

        if use_scene_pos:
            pos = (event.scenePos().x(), event.scenePos().y())
        else:
            viewer = self._graph.viewer()
            sp = viewer.mapToScene(event.pos())
            pos = (sp.x(), sp.y())

        if text.startswith("/"):
            # If dropped onto an existing CollisionSetNode, append path to it
            drop_pt = QtCore.QPointF(pos[0], pos[1])
            for _n in self._graph.all_nodes():
                if isinstance(_n, nodes.CollisionSetNode):
                    if _n.view.sceneBoundingRect().contains(drop_pt):
                        _n.append_prim_path(text)
                        if self._stage:
                            self._executor.run_from(_n)
                        event.acceptProposedAction()
                        return
            node = self._create_node(nodes.PrimPathNode, pos=pos)
            if node:
                node.set_property("prim_path", text)
                if self._stage:
                    self._executor.run_from(node)
        else:
            node = self._create_node(nodes.StringNode, pos=pos)
            if node:
                node.set_property("value", text)
        event.acceptProposedAction()

    def _add_backdrop(self) -> None:
        """Add a backdrop around selected nodes."""
        g = self._active_graph()
        selected = g.selected_nodes()
        backdrop = g.create_node("nodeGraphQt.nodes.BackdropNode", name="Backdrop")
        if selected:
            min_x = min(n.pos()[0] for n in selected) - 40
            min_y = min(n.pos()[1] for n in selected) - 80
            max_x = max(n.pos()[0] for n in selected) + 200
            max_y = max(n.pos()[1] for n in selected) + 120
            backdrop.set_pos(min_x, min_y)
            backdrop.set_property("width", max_x - min_x)
            backdrop.set_property("height", max_y - min_y)

    def _group_selected(self) -> None:
        """Group selected nodes into a GroupNode sub-graph."""
        g = self._active_graph()
        selected = g.selected_nodes()
        if len(selected) < 1:
            return

        sel_ids = {n.id for n in selected}
        cx = sum(n.pos()[0] for n in selected) / len(selected)
        cy = sum(n.pos()[1] for n in selected) / len(selected)

        session = g._serialize(selected)

        ext_inputs = []
        ext_outputs = []
        for node in selected:
            for port in node.input_ports():
                for cp in port.connected_ports():
                    if cp.node().id not in sel_ids:
                        ext_inputs.append((cp, node.id, port.name()))
            for port in node.output_ports():
                for cp in port.connected_ports():
                    if cp.node().id not in sel_ids:
                        ext_outputs.append((node.id, port.name(), cp))

        g.delete_nodes(selected)

        group = g.create_node("nodeGraphQt.nodes.GroupNode", name="Group")
        group.set_pos(cx, cy)
        group.set_port_deletion_allowed(True)
        sub = self._graph.expand_group_node(group)

        for i, (_, nid, pname) in enumerate(ext_inputs):
            group.add_input(f"in_{i}")
        if not ext_outputs:
            group.add_output("out_0")
        for i, (nid, pname, _) in enumerate(ext_outputs):
            group.add_output(f"out_{i}")

        inner_nodes = sub._deserialize(session, adjust_graph_style=False)
        old_to_new = {}
        old_ids = list(session.get("nodes", {}).keys())
        for old_id, new_node in zip(old_ids, inner_nodes):
            old_to_new[old_id] = new_node

        port_in_nodes = sub.get_input_port_nodes()
        for i, (_, old_nid, pname) in enumerate(ext_inputs):
            inner = old_to_new.get(old_nid)
            pin = port_in_nodes[i] if i < len(port_in_nodes) else None
            if inner and pin:
                out_p = pin.output_ports()[0] if pin.output_ports() else None
                in_p = inner.inputs().get(pname)
                if out_p and in_p:
                    out_p.connect_to(in_p)

        port_out_nodes = sub.get_output_port_nodes()
        for i, (old_nid, pname, _) in enumerate(ext_outputs):
            inner = old_to_new.get(old_nid)
            pout = port_out_nodes[i] if i < len(port_out_nodes) else None
            if inner and pout:
                out_p = inner.outputs().get(pname)
                in_p = pout.input_ports()[0] if pout.input_ports() else None
                if out_p and in_p:
                    out_p.connect_to(in_p)

        self._graph.collapse_group_node(group)

        for i, (ext_port, _, _) in enumerate(ext_inputs):
            gp = group.input_ports()[i] if i < len(group.input_ports()) else None
            if gp:
                ext_port.connect_to(gp)
        for i, (_, _, ext_port) in enumerate(ext_outputs):
            gp = group.output_ports()[i] if i < len(group.output_ports()) else None
            if gp:
                gp.connect_to(ext_port)

    def _show_add_menu(self) -> None:
        self._node_menu.exec_(QtGui.QCursor.pos())

    def _show_search_menu(self) -> None:
        dragged_dtype = None
        dragged_is_output = None
        try:
            viewer = self._active_graph().viewer()
            start_port = getattr(viewer, "_start_port", None)
            if (
                start_port
                and getattr(viewer, "_LIVE_PIPE", None)
                and viewer._LIVE_PIPE.isVisible()
            ):
                dragged_dtype = getattr(start_port, "_data_type", "any")
                dragged_is_output = start_port.port_type == PortTypeEnum.OUT.value
        except Exception:
            pass
        dlg = _TabSearchDialog(
            self, dragged_dtype=dragged_dtype, dragged_is_output=dragged_is_output
        )
        if dlg.exec_() == QtWidgets.QDialog.Accepted and dlg.selected_node:
            g = self._active_graph()
            viewer = g.viewer()
            pos = viewer.mapFromGlobal(QtGui.QCursor.pos())
            scene_pos = viewer.mapToScene(pos)
            self._create_node(dlg.selected_node, pos=(scene_pos.x(), scene_pos.y()))

    def _on_add_node(self) -> None:
        action = self.sender()
        self._create_node(action.data())

    def _create_node(self, node_cls, pos=None, graph=None) -> None:
        g = graph or self._active_graph()
        identifier = f"{node_cls.__identifier__}.{node_cls.__name__}"
        kwargs = {"name": node_cls.NODE_NAME}
        if pos:
            kwargs["pos"] = pos
        return g.create_node(identifier, **kwargs)

    def set_stage(self, stage) -> None:
        self._stage = stage

    def execute_graph(self) -> None:
        """Delegate to executor.run_full() — kept for external callers."""
        self._executor.run_full()

    def _layout_nodes(self, node_list=None) -> None:
        layout_nodes(self._active_graph(), node_list)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        if hasattr(self, "_minimap_timer"):
            self._minimap_timer.stop()
        if hasattr(self, "_exec_pulse_timer"):
            self._exec_pulse_timer.stop()
        self._viewport_was_active = (
            hasattr(self, "_viewport_timer") and self._viewport_timer.isActive()
        )
        if self._viewport_was_active:
            self._viewport_timer.stop()
        for t in getattr(self, "_init_timers", []):
            t.stop()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if hasattr(self, "_minimap_timer"):
            self._minimap_timer.start(500)
        if hasattr(self, "_exec_pulse_timer"):
            self._exec_pulse_timer.start(50)
        if getattr(self, "_viewport_was_active", False):
            self._viewport_timer.start(100)
        for t in getattr(self, "_init_timers", []):
            t.start()

    def closeEvent(self, event) -> None:
        NodeGraphWidget._active_instance = None
        super().closeEvent(event)

    @property
    def graph(self) -> NodeGraph:
        return self._graph
