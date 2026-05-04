"""
Shared primitives used by all nodes:
imports, port colors, base classes, UsdNodeBase, thumbnail helpers.
"""
import inspect as _inspect
import re as _re
from NodeGraphQt import BaseNode
from NodeGraphQt.widgets.node_widgets import NodeBaseWidget
from PySide6 import QtWidgets, QtCore
from usd_chunks import chunk_notice
# ─── Port color palette ───
PORT_COLORS = {
    "prim":     (230, 120, 40),   # warm orange
    "string":   (60, 210, 120),   # neon green
    "bool":     (230, 210, 50),   # neon yellow
    "list":     (70, 150, 230),   # bright blue
    "any":      (140, 150, 170),  # cool grey
    "color":    (230, 60, 80),    # neon red
    "float":    (50, 210, 220),   # cyan
    "material": (180, 100, 240),  # neon violet
    "texture":  (240, 190, 60),   # gold
    "trigger":  (200, 80, 220),   # purple — execution flow only
    "layer":    (100, 200, 255),  # light blue — USD layer
}
# Reverse map: color tuple → data type name (used for type enforcement)
_COLOR_TO_TYPE = {v: k for k, v in PORT_COLORS.items()}

_PORT_TYPE_CACHE = {}

def _get_node_port_types(node_cls):
    """Return {"inputs": set, "outputs": set} of port data-type names for a node class.

    Uses source introspection to find PORT_COLORS["x"] patterns in __init__.
    All UsdNodeBase nodes accept 'trigger' (added via deferred _init_trigger_input).
    Falls back to {"inputs": {"any","trigger"}, "outputs": {"any"}} on errors.
    """
    if node_cls in _PORT_TYPE_CACHE:
        return _PORT_TYPE_CACHE[node_cls]
    result = {"inputs": {"trigger"}, "outputs": set()}
    try:
        src = _inspect.getsource(node_cls.__init__)
        for m in _re.finditer(r'add_input\([^)]*PORT_COLORS\["(\w+)"\]', src):
            result["inputs"].add(m.group(1))
        for m in _re.finditer(r'add_output\([^)]*PORT_COLORS\["(\w+)"\]', src):
            result["outputs"].add(m.group(1))
    except Exception:
        result = {"inputs": {"any", "trigger"}, "outputs": {"any"}}
    _PORT_TYPE_CACHE[node_cls] = result
    return result

# Sentinel: when a port carries this value, downstream nodes ignore it as data
class _TriggerSignal:
    """Sentinel value for trigger ports — causes get_property to skip the connection."""
    def __repr__(self): return "<trigger>"
TRIGGER = _TriggerSignal()


def _safe_set_port_visible(port, visible):
    """Set port visibility, silently ignoring errors (e.g. node not yet in graph)."""
    try:
        port.set_visible(visible)
    except Exception:
        pass


# ────────────────────────────────────────────
#  Base
# ────────────────────────────────────────────
_EDGE_MARGIN = 8  # pixels from edge to trigger resize

CATEGORY_COLORS = {
    "Input":       (14, 50, 40),
    "USD":         (14, 35, 55),
    "Create":      (14, 45, 30),
    "Transform":   (14, 45, 35),
    "Hierarchy":   (30, 20, 50),
    "Composition": (40, 30, 16),
    "Material":    (45, 16, 35),
    "Lighting":    (50, 42, 12),
    "Camera":      (20, 35, 48),
    "Animation":   (48, 35, 12),
    "Utility":     (22, 24, 30),
    "Math":        (14, 40, 42),
    "Code":        (18, 18, 22),
    "Chunks":      (16, 32, 42),
    "Debug":       (42, 18, 18),
    "Event":       (50, 20, 50),
}

def _detect_edge(view, pos):
    """Detect if pos is near the right/bottom edge of the node."""
    rect = view.boundingRect()
    near_right = abs(pos.x() - rect.right()) < _EDGE_MARGIN
    near_bottom = abs(pos.y() - rect.bottom()) < _EDGE_MARGIN
    if near_right and near_bottom:
        return "corner"
    if near_right and pos.y() > rect.top() + 20:
        return "right"
    if near_bottom and pos.x() > rect.left() + 20:
        return "bottom"
    return None


class UsdNodeBase(BaseNode):
    """Common base for all USD graph nodes."""
    __identifier__ = "usd_chunks"
    NODE_NAME = "UsdNodeBase"
    CATEGORY = "Base"

    def __init__(self):
        super().__init__()
        self._handle = None
        self._min_w = None
        self._min_h = None
        self._auto_ports_done = False
        self._widgets_to_hide = set()
        self._hide_scheduled = False
        self._exec_error_msg = ""
        self.view._node_type_name = self.NODE_NAME
        cat = getattr(self, 'CATEGORY', 'Base')
        if cat in CATEGORY_COLORS:
            self.set_color(*CATEGORY_COLORS[cat])
        # Auto-add hidden trigger input — shown only when a trigger wire connects.
        # Deferred because set_visible() requires the node to be in the graph.
        QtCore.QTimer.singleShot(0, self._init_trigger_input)

    def _init_trigger_input(self):
        try:
            existing = {p.name() for p in self.input_ports()}
            if "trigger" not in existing:
                port = self.add_input("trigger", color=PORT_COLORS["trigger"])
                QtCore.QTimer.singleShot(0, lambda: _safe_set_port_visible(port, False))
        except Exception:
            pass
        QtCore.QTimer.singleShot(500, self._setup_resize)

    def add_input(self, name='input', multi_input=False, display_name=True,
                  color=None, locked=False, painter_func=None):
        port = super().add_input(name, multi_input=multi_input,
                                 display_name=display_name, color=color,
                                 locked=locked, painter_func=painter_func)
        dtype = _COLOR_TO_TYPE.get(color, 'any') if color is not None else 'any'
        try:
            port.view._data_type = dtype
        except Exception:
            pass
        return port

    def add_output(self, name='output', multi_output=True, display_name=True,
                   color=None, locked=False, painter_func=None):
        port = super().add_output(name, multi_output=multi_output,
                                  display_name=display_name, color=color,
                                  locked=locked, painter_func=painter_func)
        dtype = _COLOR_TO_TYPE.get(color, 'any') if color is not None else 'any'
        try:
            port.view._data_type = dtype
        except Exception:
            pass
        return port

    def add_text_input(self, name, *args, **kwargs):
        """Override: auto-create a connectable input port for each text input."""
        super().add_text_input(name, *args, **kwargs)
        if name not in ("code",):
            port_name = f"_{name}"
            existing = {p.name() for p in self.input_ports()}
            if port_name not in existing:
                self.add_input(port_name, color=PORT_COLORS.get("any", (140, 150, 170)))
        # Collect widget name; one batched hide fires after all add_text_input calls settle
        self._widgets_to_hide.add(name)
        if not self._hide_scheduled:
            self._hide_scheduled = True
            QtCore.QTimer.singleShot(200, self._batch_hide_widgets)

    def _batch_hide_widgets(self):
        """Hide all collected text-input widgets in one pass, then recompute layout."""
        self._hide_scheduled = False
        changed = False
        for name in self._widgets_to_hide:
            w = self.view._widgets.get(name)
            if w and w.isVisible():
                w.setVisible(False)
                changed = True
        self._widgets_to_hide.clear()
        if changed:
            try:
                self.view.draw_node()
            except Exception:
                pass

    def set_preview(self, text):
        """Update the node's preview chip (if it has one) with a compact info string."""
        w = self.view._widgets.get("preview")
        if w:
            lbl = w.widget().findChild(QtWidgets.QLabel)
            if lbl:
                lbl.setText(text)
            self.view.update()

    def _get_prop_value(self, name, cast=None):
        """Get value from connected port '_{name}', fallback to text property.
        Optionally cast to float/int/str."""
        val = self._get_input_value(f"_{name}")
        if val is None:
            val = self.get_property(name)
        if cast and val is not None:
            try:
                val = cast(val)
            except (ValueError, TypeError):
                pass
        return val

    _graph_ref = None  # set by NodeGraphWidget so nodes can find each other

    def get_property(self, name):
        """Override: check connected port, then @reference, then text value."""
        # Check for connected auto-port first
        port = self.get_input(f"_{name}")
        if port:
            connected = port.connected_ports()
            if connected:
                src_node = connected[0].node()
                val = src_node._output_cache.get(connected[0].name()) if hasattr(src_node, "_output_cache") else None
                if val is not None and not isinstance(val, _TriggerSignal):
                    return str(val) if isinstance(val, (int, float)) else val
        # Check for @reference expression
        raw = super().get_property(name)
        if isinstance(raw, str) and raw.startswith("@") and "." in raw:
            return self._resolve_ref(raw)
        return raw

    def _resolve_ref(self, ref_str):
        """Resolve @name.property to a concrete value.

        Preferred path: delegate to chunk_core.resolve_reference, which looks the reference up
        scene-wide (works for any chunk, even ones not displayed in the node graph) and also
        supports @/prim/path.attr USD attribute refs.

        Fallback: if chunk-core can't resolve it, walk the node graph for a matching node. This
        keeps references to pure node-graph nodes (Event, Trigger, etc.) working.
        """
        # 1. Chunk-core resolution — scene chunks + USD prim attrs.
        try:
            from usd_chunks import chunk_core
            # Any loaded chunk has a get_scene(). Use the first chunk in the scene as 'owner'.
            from usd_chunks import chunk_registry
            scene = chunk_registry.ChunkRegistry().scene
            chunks = scene.get_chunks() if scene else ()
            if chunks:
                target = chunk_core.resolve_reference(chunks[0], ref_str)
                if target is not None:
                    # Chunk param — return its current value so callers get a concrete value.
                    if isinstance(target, chunk_core.ChunkParamBase):
                        val = target.value
                        return str(val) if isinstance(val, (int, float)) else val
                    # USD attribute — pull its current time-sample.
                    get_fn = getattr(target, "Get", None)
                    if callable(get_fn):
                        val = get_fn()
                        return str(val) if isinstance(val, (int, float)) else val
        except Exception:
            pass

        # 2. Fallback: node-graph-local lookup for pure node-graph nodes.
        try:
            parts = ref_str[1:].split(".", 1)  # strip @, split node.prop
            node_name, prop_name = parts[0], parts[1]
            graph = UsdNodeBase._graph_ref
            if not graph:
                return ref_str
            for n in graph.all_nodes():
                if n.name() == node_name:
                    if hasattr(n, "_output_cache") and prop_name in n._output_cache:
                        val = n._output_cache[prop_name]
                        return str(val) if isinstance(val, (int, float)) else val
                    return BaseNode.get_property(n, prop_name)
        except Exception:
            pass
        return ref_str

    def _setup_resize(self):
        try:
            r = self.view.boundingRect()
            self._min_w = r.width()
            self._min_h = r.height()
            # Patch the node view for edge-resize
            view = self.view
            view._resize_node = self
            view._resize_dragging = False
            view._resize_edge = None
            view.setAcceptHoverEvents(True)

            orig_hover = view.hoverMoveEvent
            orig_press = view.mousePressEvent
            orig_move = view.mouseMoveEvent
            orig_release = view.mouseReleaseEvent

            def _hover(event, _orig=orig_hover):
                edge = _detect_edge(view, event.pos())
                if edge:
                    cursors = {
                        "right": QtCore.Qt.SizeHorCursor,
                        "bottom": QtCore.Qt.SizeVerCursor,
                        "corner": QtCore.Qt.SizeFDiagCursor,
                    }
                    view.setCursor(cursors.get(edge, QtCore.Qt.ArrowCursor))
                else:
                    view.unsetCursor()
                if _orig:
                    _orig(event)

            def _press(event, _orig=orig_press):
                edge = _detect_edge(view, event.pos())
                if edge and event.button() == QtCore.Qt.LeftButton:
                    view._resize_dragging = True
                    view._resize_edge = edge
                    view._resize_start = event.scenePos()
                    view._resize_start_w = view.boundingRect().width()
                    view._resize_start_h = view.boundingRect().height()
                    event.accept()
                    return
                _orig(event)

            def _move(event, _orig=orig_move):
                if view._resize_dragging:
                    dx = event.scenePos().x() - view._resize_start.x()
                    dy = event.scenePos().y() - view._resize_start.y()
                    edge = view._resize_edge
                    w = view._resize_start_w + (dx if edge in ("right", "corner") else 0)
                    h = view._resize_start_h + (dy if edge in ("bottom", "corner") else 0)
                    view._resize_node._on_resize(int(w), int(h))
                    event.accept()
                    return
                _orig(event)

            def _release(event, _orig=orig_release):
                if view._resize_dragging:
                    view._resize_dragging = False
                    view._resize_edge = None
                    event.accept()
                    return
                _orig(event)

            view.hoverMoveEvent = _hover
            view.mousePressEvent = _press
            view.mouseMoveEvent = _move
            view.mouseReleaseEvent = _release
        except Exception:
            pass

    def _on_resize(self, width, height=None):
        w = max(self._min_w or 120, width)
        h = max(self._min_h or 60, height) if height else None
        # Only resize widget max widths (not min) so they can shrink
        widget_w = int(w - 46)
        for wgt in self.view._widgets.values():
            wgt.widget().setMaximumWidth(widget_w)
            for child in wgt.widget().findChildren(QtWidgets.QLineEdit):
                child.setMaximumWidth(widget_w)
        orig_calc = self.view.calc_size
        def _forced_size(add_w=0, add_h=0):
            ow, oh = orig_calc(add_w, add_h)
            return (max(w, ow), max(h or oh, oh))
        self.view.calc_size = _forced_size
        self.view.draw_node()
        self.view.calc_size = orig_calc

    def execute(self, stage):
        return {}

    def to_python(self, var_map):
        """Generate Python code for this node. var_map: {port_name: variable_name}."""
        return f"# {self.NODE_NAME} (no code generation)"

    def _get_input_value(self, port_name):
        port = self.get_input(port_name)
        if not port:
            return None
        connected = port.connected_ports()
        if not connected:
            return None
        src_node = connected[0].node()
        val = src_node._output_cache.get(connected[0].name()) if hasattr(src_node, "_output_cache") else None
        if isinstance(val, _TriggerSignal):
            return None
        # Auto-unwrap single-item lists for prim ports
        if isinstance(val, list) and len(val) == 1:
            val = val[0]
        return val


    @property
    def color(self):
        c = super().color
        return c[:3] if len(c) > 3 else c

    def _get_prims(self, port_name="prim"):
        """Get prim(s) from input — returns list whether single or merged."""
        v = self._get_input_value(port_name)
        if v is None:
            return []
        if isinstance(v, list):
            return [p for p in v if p is not None]
        return [v]

# ────────────────────────────────────────────
#  Input / Utility nodes
# ────────────────────────────────────────────
class _PrimThumbLabel(QtWidgets.QLabel):
    """QLabel compat shim for NodeGraphQt."""
    def setTitleAlign(self, *a): pass


class _NodePreviewWidget(NodeBaseWidget):
    """One-line dim preview shown at the bottom of a node after execution."""
    def __init__(self, parent=None, name="preview", label=""):
        super().__init__(parent, name, label)
        self._container = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(self._container)
        lay.setContentsMargins(8, 3, 8, 4)
        self._lbl = QtWidgets.QLabel("")
        self._lbl.setStyleSheet(
            "color: rgba(140,160,200,0.55); font: 7pt 'Consolas'; background: transparent;")
        self._lbl.setTitleAlign = lambda *a: None
        lay.addWidget(self._lbl)
        self._container.setStyleSheet(
            "background: rgba(0,0,0,0.18);"
            " border-top: 1px solid rgba(30,36,56,0.6);")
        self._container.setTitleAlign = lambda *a: None
        self.setWidget(self._container)

    def get_value(self):
        return self._lbl.text()

    def set_value(self, val=""):
        self._lbl.setText(val or "")


class _PrimThumbWidget(NodeBaseWidget):
    """Thumbnail widget for PrimPathNode — scalable like TextureFileNode."""
    def __init__(self, parent=None, name="prim_thumb", label=""):
        super().__init__(parent, name, label)
        self._widget = _PrimThumbLabel()
        self._widget.setMinimumSize(180, 120)
        self._widget.setStyleSheet("background: #0a0c12; border: 1px solid #1e2438; border-radius: 4px;")
        self._widget.setAlignment(QtCore.Qt.AlignCenter)
        self._widget.setScaledContents(True)
        self.setWidget(self._widget)
        self._pixmap = None

    def get_value(self):
        return ""

    def set_value(self, val=""):
        pass

    def set_pixmap(self, pixmap):
        self._pixmap = pixmap
        self._widget.setPixmap(pixmap)


# ────────────────────────────────────────────
#  Chunk wrapper base
# ────────────────────────────────────────────
class ChunkWrapperBase(UsdNodeBase):
    """Base for nodes that wrap existing chunk functionality.

    Supports the same "notice dependency" reactivity as the regular chunk panel:
    when the wrapped chunk has notice dependencies, a CHUNK_CHANGED listener is
    installed so that downstream nodes re-execute whenever the chunk reacts.
    """
    CATEGORY = "Chunks"

    def __init__(self):
        super().__init__()
        self._chunk_notice_conn = None
        self._executing = False

    def _subscribe_to_chunk_notices(self, chunk_obj):
        """Install a CHUNK_CHANGED listener. Safe to call repeatedly; created once per node."""
        if self._chunk_notice_conn is not None or chunk_obj is None:
            return
        try:
            self._chunk_notice_conn = chunk_notice.NoticeConnection(
                self._on_chunk_notice_fired,
                alias=chunk_notice.ChunkNoticeAlias.CHUNK_CHANGED,
                sender=chunk_obj,
            )
        except Exception:
            self._chunk_notice_conn = None

    def _on_chunk_notice_fired(self, notice, sender):
        """Defers downstream execution so the chunk finishes its own solve first."""
        if self._executing:
            return
        QtCore.QTimer.singleShot(0, self._trigger_downstream)

    def _get_graph_widget(self):
        import sys
        ref = sys.modules.get('_node_graph_ref')
        return ref.widget if ref and hasattr(ref, 'widget') else None

    def _trigger_downstream(self):
        """Re-execute this node and cascade to downstream via executor."""
        widget = self._get_graph_widget()
        if not widget or not widget._stage:
            return
        widget._executor.mark_dirty(self)
        try:
            win = widget.window()

            def _refocus():
                try:
                    win.raise_()
                    win.activateWindow()
                    props = getattr(widget, '_props_widget', None)
                    if props:
                        props.setFocus(QtCore.Qt.OtherFocusReason)
                except Exception:
                    pass
            QtCore.QTimer.singleShot(150, _refocus)
        except Exception:
            pass


class DynamicChunkNode(ChunkWrapperBase):
    """Wraps any loaded chunk. Set chunk_name, then execute fires its first button."""
    NODE_NAME = "Chunk (Dynamic)"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("chunk_name", "Chunk Name", text="")
        self.add_text_input("button_name", "Button", text="")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage):
        from ..chunk_adapter import ChunkAdapter
        prim = self._get_input_value("prim")
        chunk_name = (self.get_property("chunk_name") or "").strip()
        if not chunk_name:
            print(f"[DynamicChunkNode] Available: {ChunkAdapter.list_available()}")
            return {"prim": prim}

        adapter = ChunkAdapter(chunk_name)
        if not adapter.chunk:
            print(f"[DynamicChunkNode] '{chunk_name}' not found. Available: {ChunkAdapter.list_available()}")
            return {"prim": prim}

        self._subscribe_to_chunk_notices(adapter.chunk)

        self._executing = True
        try:
            old_sel = []
            if prim:
                from ..graph_host import GraphHost
                host = GraphHost.current()
                old_sel = host.get_selection()
                host.set_selection(prim)

            btn = (self.get_property("button_name") or "").strip() or None
            adapter.press_button(btn)

            if prim and old_sel:
                from ..graph_host import GraphHost
                GraphHost.current().set_selection(old_sel[0])
            return {"prim": prim}
        finally:
            self._executing = False
