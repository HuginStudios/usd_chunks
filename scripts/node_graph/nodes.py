"""
Node definitions for the USD node graph.
Each node has typed ports (prim, string, variant_list, bool, etc.)
and an execute() method that runs when inputs change.
"""
from NodeGraphQt import BaseNode
from NodeGraphQt.widgets.node_widgets import NodeBaseWidget
from PySide6 import QtWidgets, QtCore, QtGui
from usd_chunks import chunk_core, chunk_notice, chunk_registry
from usd_chunks.chunk import Button
from usd_chunks import chunk
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


class PrimPathNode(UsdNodeBase):
    """Resolves a prim path to a Usd.Prim. Supports glob patterns (* ?) to match multiple prims."""
    NODE_NAME = "Prim Path"
    CATEGORY = "Input"

    def __init__(self):
        super().__init__()
        self.add_text_input("prim_path", "Prim Path", text="/")
        # Thumbnail (hidden until captured)
        self._prim_thumb = _PrimThumbWidget(self.view, name="prim_thumb")
        self._prim_thumb._widget.hide()
        self.add_custom_widget(self._prim_thumb)
        # Compact preview chip
        self._preview = _NodePreviewWidget(self.view, name="preview")
        self.add_custom_widget(self._preview)
        self.add_output("prim", color=PORT_COLORS["prim"])
        self.add_output("prims", color=PORT_COLORS["list"])
        self.add_output("count", color=PORT_COLORS["float"])
        QtCore.QTimer.singleShot(0, lambda: self._on_resize(260))

    def execute(self, stage):
        import fnmatch
        path = self.get_property("prim_path") or ""
        if not path:
            self._preview.set_value("")
            return {"prim": None, "prims": [], "count": 0}
        # Glob mode: */ChairB_* or /Kitchen_set/**/Mesh*
        if "*" in path or "?" in path:
            matched = [p for p in stage.Traverse()
                       if fnmatch.fnmatch(str(p.GetPath()), path)
                       or fnmatch.fnmatch(p.GetName(), path.split("/")[-1])]
            count = len(matched)
            name = path.split("/")[-1] or path
            self._preview.set_value(f"● {name}  ×{count}" if count else "✗  no match")
            return {
                "prim": matched[0] if matched else None,
                "prims": matched,
                "count": count,
            }
        prim = stage.GetPrimAtPath(path)
        valid = prim and prim.IsValid()
        if valid:
            name = path.rstrip("/").rsplit("/", 1)[-1] or "/"
            self._preview.set_value(f"● {name}")
        else:
            self._preview.set_value(f"✗  no prim")
        return {"prim": prim if valid else None, "prims": [prim] if valid else [], "count": 1 if valid else 0}

    def to_python(self, var_map):
        path = self.get_property("prim_path") or "/"
        v = var_map.get("prim", "prim")
        return f'{v} = stage.GetPrimAtPath("{path}")'
class SelectedPrimNode(UsdNodeBase):
    """Outputs the currently selected prim(s) from usdview."""
    NODE_NAME = "Selected Prim"
    CATEGORY = "Input"

    def __init__(self):
        super().__init__()
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage):
        try:
            from .graph_host import GraphHost
            prims = GraphHost.current().get_selection()
            return {"prim": prims[0] if prims else None}
        except Exception:
            return {"prim": None}


class StringNode(UsdNodeBase):
    """A simple string constant."""
    NODE_NAME = "String"
    CATEGORY = "Input"

    def __init__(self):
        super().__init__()
        self.add_text_input("value", "Value", text="")
        self._preview = _NodePreviewWidget(self.view, name="preview")
        self.add_custom_widget(self._preview)
        self.add_output("string", color=PORT_COLORS["string"])

    def execute(self, stage):
        val = self.get_property("value") or ""
        self._preview.set_value(f'"{val[:36]}"' if val else '""')
        return {"string": val}


# ────────────────────────────────────────────
#  USD Query nodes
# ────────────────────────────────────────────
class GetVariantsNode(UsdNodeBase):
    """Lists variant sets and their options for a prim."""
    NODE_NAME = "Get Variants"
    CATEGORY = "USD"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_output("variant_sets", color=PORT_COLORS["list"])
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage):
        prim = self._get_input_value("prim")
        if not prim:
            return {"variant_sets": {}, "prim": None}
        vsets = prim.GetVariantSets()
        result = {}
        for name in vsets.GetNames():
            vs = vsets.GetVariantSet(name)
            result[name] = {
                "options": vs.GetVariantNames(),
                "selected": vs.GetVariantSelection(),
            }
        return {"variant_sets": result, "prim": prim}


class SetVariantNode(UsdNodeBase):
    """Sets a variant selection on a prim."""
    NODE_NAME = "Set Variant"
    CATEGORY = "USD"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("set_name", "Variant Set", text="")
        self.add_text_input("selection", "Selection", text="")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage):
        prim = self._get_input_value("prim")
        set_name = self.get_property("set_name")
        selection = self.get_property("selection")
        if prim and set_name and selection:
            vsets = prim.GetVariantSets()
            if vsets.HasVariantSet(set_name):
                vsets.GetVariantSet(set_name).SetVariantSelection(selection)
        return {"prim": prim}


class GetPrimInfoNode(UsdNodeBase):
    """Outputs prim type, name, path, and children count."""
    NODE_NAME = "Get Prim Info"
    CATEGORY = "USD"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_output("type", color=PORT_COLORS["string"])
        self.add_output("name", color=PORT_COLORS["string"])
        self.add_output("path", color=PORT_COLORS["string"])
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage):
        prim = self._get_input_value("prim")
        if not prim:
            return {"type": "", "name": "", "path": "", "prim": None}
        return {
            "type": prim.GetTypeName(),
            "name": prim.GetName(),
            "path": str(prim.GetPath()),
            "prim": prim,
        }



class GetChildrenNode(UsdNodeBase):
    """Outputs children of a prim."""
    NODE_NAME = "Get Children"
    CATEGORY = "USD"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_output("children", color=PORT_COLORS["list"])

    def execute(self, stage):
        prim = self._get_input_value("prim")
        if not prim:
            return {"children": []}
        return {"children": list(prim.GetChildren())}



# ────────────────────────────────────────────
#  Action nodes
# ────────────────────────────────────────────

class PrintNode(UsdNodeBase):
    """Debug node — prints whatever comes in."""
    NODE_NAME = "Print"
    CATEGORY = "Debug"

    def __init__(self):
        super().__init__()
        self.add_input("value", color=PORT_COLORS["any"])

    def execute(self, stage):
        val = self._get_input_value("value")
        print(f"[PrintNode] {val}")
        return {}



class PythonNode(UsdNodeBase):
    """Execute custom Python code. Available variables: stage, inputs (dict of all input values).
    Set 'output' variable to pass a result downstream.
    
    REMEMBER: THE "inputs" IS A DICT OF ALL INPUT PORT VALUES, {"in_1": value1, "in_2": value2, ...}
    """
    NODE_NAME = "Python"
    CATEGORY = "Code"

    def __init__(self):
        super().__init__()
        self.add_input("in_1", color=PORT_COLORS["any"])
        self.add_input("in_2", color=PORT_COLORS["any"])
        self.add_input("in_3", color=PORT_COLORS["any"])
        self.add_text_input("code", "Code", text="output = inputs")
        self.add_output("result", color=PORT_COLORS["any"])

    def execute(self, stage):
        inputs = {}
        for port in self.input_ports():
            inputs[port.name()] = self._get_input_value(port.name())
        code = self.get_property("code") or ""
        local_vars = {"stage": stage, "inputs": inputs, "output": None}
        try:
            exec(code, {"__builtins__": __builtins__}, local_vars)
        except Exception as e:
            print(f"[PythonNode] Error: {e}")
            raise  # Let execution engine catch it for red indicator
        return {"result": local_vars.get("output")}

    def to_python(self, var_map):
        code = self.get_property("code") or ""
        return f"# Python node\n{code}"


# ────────────────────────────────────────────
#  Material / Shading nodes
# ────────────────────────────────────────────
class SetDisplayColorNode(UsdNodeBase):
    """Set displayColor on a mesh prim."""
    NODE_NAME = "Set Display Color"
    CATEGORY = "Material"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("color_r", "R", text="0.5")
        self.add_text_input("color_g", "G", text="0.5")
        self.add_text_input("color_b", "B", text="0.5")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage):
        from pxr import UsdGeom, Vt, Gf
        prims = self._get_prims("prim")
        if not prims:
            return {"prim": None}
        r = float(self.get_property("color_r") or 0.5)
        g = float(self.get_property("color_g") or 0.5)
        b = float(self.get_property("color_b") or 0.5)
        color = Vt.Vec3fArray([Gf.Vec3f(r, g, b)])
        for prim in prims:
            gprim = UsdGeom.Gprim(prim)
            if gprim:
                gprim.GetDisplayColorAttr().Set(color)
        return {"prim": prims if len(prims) > 1 else prims[0]}

    def to_python(self, var_map):
        pv = var_map.get("prim", "prim")
        r = self.get_property("color_r") or "0.5"
        g = self.get_property("color_g") or "0.5"
        b = self.get_property("color_b") or "0.5"
        return f'UsdGeom.Gprim({pv}).GetDisplayColorAttr().Set(Vt.Vec3fArray([Gf.Vec3f({r}, {g}, {b})]))'


class AssignMaterialNode(UsdNodeBase):
    """Bind an existing material to a prim."""
    NODE_NAME = "Assign Material"
    CATEGORY = "Material"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_input("material", color=PORT_COLORS["material"])
        self.add_text_input("material_path", "Material Path", text="")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage):
        from pxr import UsdShade
        prims = self._get_prims("prim")
        mat_prim = self._get_input_value("material")
        mat_path = self.get_property("material_path")
        if not prims:
            return {"prim": None}
        if not mat_prim and mat_path:
            mat_prim = stage.GetPrimAtPath(mat_path)
        if mat_prim and mat_prim.IsValid():
            mat = UsdShade.Material(mat_prim)
            for prim in prims:
                UsdShade.MaterialBindingAPI.Apply(prim).Bind(mat)
        return {"prim": prims if len(prims) > 1 else prims[0]}


class CreatePreviewSurfaceNode(UsdNodeBase):
    """Create a UsdPreviewSurface material."""
    NODE_NAME = "Create PreviewSurface"
    CATEGORY = "Material"

    def __init__(self):
        super().__init__()
        self.add_text_input("mat_path", "Material Path", text="/Materials/MyMaterial")
        self.add_text_input("diffuse_r", "Diffuse R", text="0.8")
        self.add_text_input("diffuse_g", "Diffuse G", text="0.8")
        self.add_text_input("diffuse_b", "Diffuse B", text="0.8")
        self.add_text_input("roughness", "Roughness", text="0.5")
        self.add_text_input("metallic", "Metallic", text="0.0")
        self.add_input("diffuse_texture", color=PORT_COLORS["texture"])
        self.add_output("material", color=PORT_COLORS["material"])
        self.add_output("shader", color=PORT_COLORS["prim"])

    def execute(self, stage):
        from pxr import UsdShade, Sdf, Gf
        mat_path = self.get_property("mat_path") or "/Materials/MyMaterial"
        r = float(self.get_property("diffuse_r") or 0.8)
        g = float(self.get_property("diffuse_g") or 0.8)
        b = float(self.get_property("diffuse_b") or 0.8)
        rough = float(self.get_property("roughness") or 0.5)
        metal = float(self.get_property("metallic") or 0.0)
        tex_path = self._get_input_value("diffuse_texture")

        mat_prim = stage.DefinePrim(mat_path, "Material")
        mat = UsdShade.Material(mat_prim)
        shader = UsdShade.Shader.Define(stage, f"{mat_path}/PBRShader")
        shader.CreateIdAttr("UsdPreviewSurface")
        shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(rough)
        shader.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(metal)

        if tex_path:
            tex_reader = UsdShade.Shader.Define(stage, f"{mat_path}/DiffuseTexture")
            tex_reader.CreateIdAttr("UsdUVTexture")
            tex_reader.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath(tex_path))
            tex_reader.CreateInput("wrapS", Sdf.ValueTypeNames.Token).Set("repeat")
            tex_reader.CreateInput("wrapT", Sdf.ValueTypeNames.Token).Set("repeat")
            tex_reader.CreateOutput("rgb", Sdf.ValueTypeNames.Float3)
            # UV reader
            uv_reader = UsdShade.Shader.Define(stage, f"{mat_path}/UVReader")
            uv_reader.CreateIdAttr("UsdPrimvarReader_float2")
            uv_reader.CreateInput("varname", Sdf.ValueTypeNames.Token).Set("st")
            uv_reader.CreateOutput("result", Sdf.ValueTypeNames.Float2)
            tex_reader.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(
                uv_reader.ConnectableAPI(), "result")
            shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(
                tex_reader.ConnectableAPI(), "rgb")
        else:
            shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(r, g, b))

        mat.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
        return {"material": mat_prim, "shader": shader.GetPrim()}


class _ThumbnailWidget(NodeBaseWidget):
    """Embeds a resizable thumbnail image inside a node."""
    def __init__(self, parent=None, name="thumbnail", label=""):
        super().__init__(parent, name, label)
        self._widget = _ThumbnailLabel()
        self._widget.setMinimumSize(180, 180)
        self._widget.setStyleSheet("background: #111; border: 1px solid #333;")
        self._widget.setAlignment(QtCore.Qt.AlignCenter)
        self._widget.setScaledContents(True)
        self.setWidget(self._widget)
        self._path = ""

    def get_value(self):
        return self._path

    def set_value(self, path=""):
        self._path = path
        if path:
            from PySide6 import QtGui
            pixmap = QtGui.QPixmap(path)
            if not pixmap.isNull():
                # Keep original quality, let the widget handle display
                self._widget.setPixmap(pixmap)
                return
        self._widget.setText("No image")


class _ThumbnailLabel(QtWidgets.QLabel):
    """QLabel with setTitleAlign for NodeGraphQt compat."""
    def setTitleAlign(self, *a): pass




class TextureFileNode(UsdNodeBase):
    """Load a texture file path. Shows preview on the node. Drag corner to resize."""
    NODE_NAME = "Texture File"
    CATEGORY = "Material"
    _thumb_size = 180

    def __init__(self):
        super().__init__()
        self.add_text_input("file_path", "File Path", text="")
        self._thumb = _ThumbnailWidget(self.view, name="thumb")
        self.add_custom_widget(self._thumb)
        self.add_output("texture", color=PORT_COLORS["texture"])
        self.set_property("width", 220)

    def _on_resize(self, width, height=None):
        self._thumb_size = max(80, width - 40)
        self._thumb._widget.setMinimumSize(self._thumb_size, self._thumb_size)
        self._thumb._widget.setMaximumSize(self._thumb_size, self._thumb_size)
        fp = self.get_property("file_path") or ""
        if fp:
            self._thumb.set_value(fp)
        super()._on_resize(width, height)

    def execute(self, stage):
        fp = self.get_property("file_path") or ""
        if fp:
            import os
            if os.path.isfile(fp):
                self._thumb.set_value(fp)
                self.set_icon(fp)
            self.set_name(f"Texture: {os.path.basename(fp)}")
        return {"texture": fp}


# ────────────────────────────────────────────
#  UV / Texture / Shader nodes
# ────────────────────────────────────────────
class CreateUVTextureNode(UsdNodeBase):
    """Create a UsdUVTexture shader with tiling controls."""
    NODE_NAME = "Create UV Texture"
    CATEGORY = "Material"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Shader Path", text="/Materials/MyMaterial/Texture")
        self.add_input("file", color=PORT_COLORS["texture"])
        self.add_text_input("file_path", "File (if no input)", text="")
        self.add_text_input("scale_s", "Scale S (tile U)", text="1.0")
        self.add_text_input("scale_t", "Scale T (tile V)", text="1.0")
        self.add_text_input("offset_s", "Offset S", text="0.0")
        self.add_text_input("offset_t", "Offset T", text="0.0")
        self.add_text_input("wrap_s", "Wrap S (repeat/clamp/mirror)", text="repeat")
        self.add_text_input("wrap_t", "Wrap T (repeat/clamp/mirror)", text="repeat")
        self.add_output("rgb", color=PORT_COLORS["any"])
        self.add_output("r", color=PORT_COLORS["float"])
        self.add_output("a", color=PORT_COLORS["float"])
        self.add_output("shader_prim", color=PORT_COLORS["prim"])
    def execute(self, stage):
        from pxr import UsdShade, Sdf, Gf
        p = self.get_property("path") or "/Materials/MyMaterial/Texture"
        fp = self._get_input_value("file") or (self.get_property("file_path") or "").strip()
        shader = UsdShade.Shader.Define(stage, p)
        shader.CreateIdAttr("UsdUVTexture")
        if fp:
            shader.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath(fp))
        shader.CreateInput("wrapS", Sdf.ValueTypeNames.Token).Set(
            self.get_property("wrap_s") or "repeat")
        shader.CreateInput("wrapT", Sdf.ValueTypeNames.Token).Set(
            self.get_property("wrap_t") or "repeat")
        # ST transform via UsdTransform2d
        ss = float(self.get_property("scale_s") or 1)
        st = float(self.get_property("scale_t") or 1)
        os_ = float(self.get_property("offset_s") or 0)
        ot = float(self.get_property("offset_t") or 0)
        if ss != 1 or st != 1 or os_ != 0 or ot != 0:
            parent = str(Sdf.Path(p).GetParentPath())
            # Create primvar reader
            uv_reader = UsdShade.Shader.Define(stage, f"{parent}/UVReader")
            uv_reader.CreateIdAttr("UsdPrimvarReader_float2")
            uv_reader.CreateInput("varname", Sdf.ValueTypeNames.Token).Set("st")
            uv_reader.CreateOutput("result", Sdf.ValueTypeNames.Float2)
            # Create transform2d
            xform = UsdShade.Shader.Define(stage, f"{parent}/UVTransform")
            xform.CreateIdAttr("UsdTransform2d")
            xform.CreateInput("in", Sdf.ValueTypeNames.Float2).ConnectToSource(
                uv_reader.ConnectableAPI(), "result")
            xform.CreateInput("scale", Sdf.ValueTypeNames.Float2).Set(Gf.Vec2f(ss, st))
            xform.CreateInput("translation", Sdf.ValueTypeNames.Float2).Set(Gf.Vec2f(os_, ot))
            xform.CreateOutput("result", Sdf.ValueTypeNames.Float2)
            shader.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(
                xform.ConnectableAPI(), "result")
        shader.CreateOutput("rgb", Sdf.ValueTypeNames.Float3)
        shader.CreateOutput("r", Sdf.ValueTypeNames.Float)
        shader.CreateOutput("a", Sdf.ValueTypeNames.Float)
        return {"rgb": f"{p}.rgb", "r": f"{p}.r", "a": f"{p}.a",
                "shader_prim": shader.GetPrim()}


class CreatePrimvarReaderNode(UsdNodeBase):
    """Create a UsdPrimvarReader_float2 for UV coordinates."""
    NODE_NAME = "Create Primvar Reader"
    CATEGORY = "Material"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Shader Path", text="/Materials/MyMaterial/UVReader")
        self.add_text_input("varname", "Primvar Name", text="st")
        self.add_output("result", color=PORT_COLORS["any"])
        self.add_output("shader_prim", color=PORT_COLORS["prim"])
    def execute(self, stage):
        from pxr import UsdShade, Sdf
        p = self.get_property("path") or "/Materials/MyMaterial/UVReader"
        shader = UsdShade.Shader.Define(stage, p)
        shader.CreateIdAttr("UsdPrimvarReader_float2")
        shader.CreateInput("varname", Sdf.ValueTypeNames.Token).Set(
            self.get_property("varname") or "st")
        shader.CreateOutput("result", Sdf.ValueTypeNames.Float2)
        return {"result": f"{p}.result", "shader_prim": shader.GetPrim()}


class GetMaterialShadersNode(UsdNodeBase):
    """Get the surface and displacement shaders from a material."""
    NODE_NAME = "Get Material Shaders"
    CATEGORY = "Material"
    def __init__(self):
        super().__init__()
        self.add_input("material", color=PORT_COLORS["material"])
        self.add_text_input("material_path", "Material Path (if no input)", text="")
        self.add_output("surface", color=PORT_COLORS["prim"])
        self.add_output("displacement", color=PORT_COLORS["prim"])
        self.add_output("shader_list", color=PORT_COLORS["list"])
    def execute(self, stage):
        from pxr import UsdShade
        mat_prim = self._get_input_value("material")
        if mat_prim is None:
            mp = (self.get_property("material_path") or "").strip()
            if mp:
                mat_prim = stage.GetPrimAtPath(mp)
        if mat_prim is None:
            return {"surface": None, "displacement": None, "shader_list": []}
        mat = UsdShade.Material(mat_prim)
        surf_out = mat.GetSurfaceOutput()
        disp_out = mat.GetDisplacementOutput()
        surf = disp = None
        if surf_out:
            src = surf_out.GetConnectedSources()
            if src and src[0]:
                surf = src[0][0].source.GetPrim()
        if disp_out:
            src = disp_out.GetConnectedSources()
            if src and src[0]:
                disp = src[0][0].source.GetPrim()
        # List all shader children
        shaders = [c for c in mat_prim.GetChildren()
                   if UsdShade.Shader(c).GetShaderId()]
        return {"surface": surf, "displacement": disp, "shader_list": shaders}


class GetShaderInputNode(UsdNodeBase):
    """Read an input value from a shader (roughness, diffuseColor, file, etc.)."""
    NODE_NAME = "Get Shader Input"
    CATEGORY = "Material"
    def __init__(self):
        super().__init__()
        self.add_input("shader", color=PORT_COLORS["prim"])
        self.add_text_input("input_name", "Input Name", text="diffuseColor")
        self.add_output("value", color=PORT_COLORS["any"])
        self.add_output("connected_source", color=PORT_COLORS["prim"])
        self.add_output("all_inputs", color=PORT_COLORS["list"])
    def execute(self, stage):
        from pxr import UsdShade
        prim = self._get_input_value("shader")
        if prim is None:
            return {"value": None, "connected_source": None, "all_inputs": []}
        shader = UsdShade.Shader(prim)
        name = (self.get_property("input_name") or "").strip()
        # List all input names
        all_inputs = [i.GetBaseName() for i in shader.GetInputs()]
        if not name:
            return {"value": None, "connected_source": None, "all_inputs": all_inputs}
        inp = shader.GetInput(name)
        if not inp:
            return {"value": None, "connected_source": None, "all_inputs": all_inputs}
        # Check if connected to another shader
        src = inp.GetConnectedSources()
        connected = None
        if src and src[0]:
            connected = src[0][0].source.GetPrim()
        val = inp.Get()
        return {"value": val, "connected_source": connected, "all_inputs": all_inputs}


class SetShaderInputNode(UsdNodeBase):
    """Set an input value on a shader."""
    NODE_NAME = "Set Shader Input"
    CATEGORY = "Material"
    def __init__(self):
        super().__init__()
        self.add_input("shader", color=PORT_COLORS["prim"])
        self.add_input("value", color=PORT_COLORS["any"])
        self.add_text_input("input_name", "Input Name", text="roughness")
        self.add_text_input("manual_value", "Value (if no input)", text="")
        self.add_output("shader", color=PORT_COLORS["prim"])
    def execute(self, stage):
        from pxr import UsdShade
        prim = self._get_input_value("shader")
        if prim is None:
            return {"shader": None}
        shader = UsdShade.Shader(prim)
        name = (self.get_property("input_name") or "").strip()
        val = self._get_input_value("value")
        if val is None:
            manual = (self.get_property("manual_value") or "").strip()
            if manual:
                try:
                    val = eval(manual)
                except Exception:
                    val = manual
        if name and val is not None:
            inp = shader.GetInput(name)
            if inp:
                inp.Set(val)
        return {"shader": prim}


class ConnectShaderNode(UsdNodeBase):
    """Connect a shader output to another shader's input."""
    NODE_NAME = "Connect Shaders"
    CATEGORY = "Material"
    def __init__(self):
        super().__init__()
        self.add_input("source_shader", color=PORT_COLORS["prim"])
        self.add_text_input("source_output", "Source Output", text="rgb")
        self.add_input("dest_shader", color=PORT_COLORS["prim"])
        self.add_text_input("dest_input", "Dest Input", text="diffuseColor")
        self.add_output("dest_shader", color=PORT_COLORS["prim"])
    def execute(self, stage):
        from pxr import UsdShade
        src_prim = self._get_input_value("source_shader")
        dst_prim = self._get_input_value("dest_shader")
        if src_prim is None or dst_prim is None:
            return {"dest_shader": dst_prim}
        src = UsdShade.Shader(src_prim)
        dst = UsdShade.Shader(dst_prim)
        src_out = self.get_property("source_output") or "rgb"
        dst_in = self.get_property("dest_input") or "diffuseColor"
        inp = dst.GetInput(dst_in)
        if inp:
            inp.ConnectToSource(src.ConnectableAPI(), src_out)
        return {"dest_shader": dst_prim}


class SetUVTransformNode(UsdNodeBase):
    """Set UV tiling by inserting/updating a UsdTransform2d between UV reader and texture.
    Connect the texture shader prim — it will find the st source and insert a transform."""
    NODE_NAME = "Set UV Transform"
    CATEGORY = "Material"
    def __init__(self):
        super().__init__()
        self.add_input("shader", color=PORT_COLORS["prim"])
        self.add_text_input("scale_s", "Scale S (tile U)", text="1.0")
        self.add_text_input("scale_t", "Scale T (tile V)", text="1.0")
        self.add_text_input("offset_s", "Offset S", text="0.0")
        self.add_text_input("offset_t", "Offset T", text="0.0")
        self.add_text_input("rotation", "Rotation (degrees)", text="0.0")
        self.add_output("shader", color=PORT_COLORS["prim"])
    def execute(self, stage):
        from pxr import UsdShade, Sdf, Gf
        prim = self._get_input_value("shader")
        if prim is None:
            return {"shader": None}
        shader = UsdShade.Shader(prim)
        ss = float(self.get_property("scale_s") or 1)
        st_val = float(self.get_property("scale_t") or 1)
        os_ = float(self.get_property("offset_s") or 0)
        ot = float(self.get_property("offset_t") or 0)
        rot = float(self.get_property("rotation") or 0)

        # Find the st input on this texture shader
        st_inp = shader.GetInput("st")
        if not st_inp:
            return {"shader": prim}

        # Get what's currently connected to st (should be a primvar reader)
        src = st_inp.GetConnectedSources()
        uv_source_shader = None
        uv_source_output = "result"
        if src and src[0]:
            uv_source_shader = src[0][0].source
            uv_source_output = src[0][0].sourceName

        # Create or find the transform2d shader next to this texture
        mat_path = prim.GetParent().GetPath()
        xform_path = f"{mat_path}/UVTransform"
        xform_shader = UsdShade.Shader.Define(stage, xform_path)
        xform_shader.CreateIdAttr("UsdTransform2d")
        xform_shader.CreateInput("scale", Sdf.ValueTypeNames.Float2).Set(
            Gf.Vec2f(ss, st_val))
        xform_shader.CreateInput("translation", Sdf.ValueTypeNames.Float2).Set(
            Gf.Vec2f(os_, ot))
        xform_shader.CreateInput("rotation", Sdf.ValueTypeNames.Float).Set(rot)
        xform_shader.CreateOutput("result", Sdf.ValueTypeNames.Float2)

        # Wire: UV reader → Transform2d → Texture st
        if uv_source_shader:
            xform_shader.CreateInput("in", Sdf.ValueTypeNames.Float2).ConnectToSource(
                uv_source_shader, uv_source_output)
        st_inp.ConnectToSource(xform_shader.ConnectableAPI(), "result")
        return {"shader": prim}


# ────────────────────────────────────────────
#  Query nodes
# ────────────────────────────────────────────
class GetAttributeNode(UsdNodeBase):
    """Get any attribute value from a prim."""
    NODE_NAME = "Get Attribute"
    CATEGORY = "USD"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("attr_name", "Attribute", text="")
        self.add_output("value", color=PORT_COLORS["any"])
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage):
        prims = self._get_prims("prim")
        attr_name = self.get_property("attr_name")
        if not prims or not attr_name:
            return {"value": None, "prim": None}

        def _get_val(p):
            # Handle pseudo-attributes
            if attr_name == "name":
                return p.GetName()
            if attr_name == "path":
                return str(p.GetPath())
            if attr_name == "type":
                return p.GetTypeName()
            attr = p.GetAttribute(attr_name)
            return attr.Get() if attr and attr.IsValid() else None

        if len(prims) == 1:
            return {"value": _get_val(prims[0]), "prim": prims[0]}
        vals = [_get_val(p) for p in prims]
        return {"value": vals, "prim": prims}

    def to_python(self, var_map):
        attr = self.get_property("attr_name") or "name"
        pv = var_map.get("prim", "prim")
        vv = var_map.get("value", "value")
        if attr == "name":
            return f'{vv} = {pv}.GetName()'
        if attr == "path":
            return f'{vv} = str({pv}.GetPath())'
        if attr == "type":
            return f'{vv} = {pv}.GetTypeName()'
        return f'{vv} = {pv}.GetAttribute("{attr}").Get()'


class SetAttributeNode(UsdNodeBase):
    """Set any attribute value on a prim."""
    NODE_NAME = "Set Attribute"
    CATEGORY = "USD"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_input("value", color=PORT_COLORS["any"])
        self.add_text_input("attr_name", "Attribute", text="")
        self.add_text_input("manual_value", "Value (if no input)", text="")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage):
        prims = self._get_prims("prim")
        val = self._get_input_value("value")
        attr_name = self.get_property("attr_name")
        if val is None:
            manual = self.get_property("manual_value")
            if manual:
                try:
                    val = eval(manual)
                except Exception:
                    val = manual
        if prims and attr_name and val is not None:
            for prim in prims:
                attr = prim.GetAttribute(attr_name)
                if attr and attr.IsValid():
                    # Convert value to match attribute's expected type
                    from pxr import Vt, Gf, Sdf
                    type_name = attr.GetTypeName()
                    try:
                        if type_name == Sdf.ValueTypeNames.Float3Array or type_name == Sdf.ValueTypeNames.Color3fArray:
                            if isinstance(val, (tuple, list)):
                                val = Vt.Vec3fArray([Gf.Vec3f(*[float(x) for x in val])])
                        elif type_name == Sdf.ValueTypeNames.Float3 or type_name == Sdf.ValueTypeNames.Color3f:
                            if isinstance(val, (tuple, list)):
                                val = Gf.Vec3f(*[float(x) for x in val])
                        elif type_name == Sdf.ValueTypeNames.Double3:
                            if isinstance(val, (tuple, list)):
                                val = Gf.Vec3d(*[float(x) for x in val])
                    except Exception:
                        pass
                    attr.Set(val)
        return {"prim": prims if len(prims) > 1 else (prims[0] if prims else None)}


class SelectPrimNode(UsdNodeBase):
    """Select the input prim in usdview viewport."""
    NODE_NAME = "Select in Viewport"
    CATEGORY = "USD"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])

    def execute(self, stage):
        prim = self._get_input_value("prim")
        if prim:
            try:
                from .graph_host import GraphHost
                GraphHost.current().set_selection(prim)
            except Exception:
                pass
        return {}


class FilterChildrenNode(UsdNodeBase):
    """Filter children of a prim by type name."""
    NODE_NAME = "Filter Children"
    CATEGORY = "USD"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("type_filter", "Type (e.g. Mesh)", text="Mesh")
        self.add_output("children", color=PORT_COLORS["list"])

    def execute(self, stage):
        prim = self._get_input_value("prim")
        type_filter = self.get_property("type_filter") or ""
        if not prim:
            return {"children": []}
        children = [c for c in prim.GetChildren()
                    if not type_filter or c.GetTypeName() == type_filter]
        return {"children": children}


# ────────────────────────────────────────────
#  Chunk wrapper nodes
# ────────────────────────────────────────────
class ChunkWrapperBase(UsdNodeBase):
    """Base for nodes that wrap existing chunk functionality.

    Supports the same "notice dependency" reactivity as the regular chunk panel:
    when the wrapped chunk has notice dependencies (`chunk.get_dependencies()`),
    a CHUNK_CHANGED listener is installed so that downstream nodes re-execute
    whenever the chunk reacts — effectively turning the chunk node into a live
    trigger, just like an Event node.
    """
    CATEGORY = "Chunks"

    def __init__(self):
        super().__init__()
        # Held reference to the NoticeConnection so the Tf.Listener stays alive
        self._chunk_notice_conn = None
        # Re-entry guard: while our own execute() is writing param values the
        # chunk will fire CHUNK_CHANGED synchronously — we must not let that
        # bounce back into another _trigger_downstream().
        self._executing = False

    def _subscribe_to_chunk_notices(self, chunk_obj):
        """Install a CHUNK_CHANGED listener on ``chunk_obj`` so downstream nodes
        re-execute whenever the chunk reacts — whether via an explicit notice
        dependency (``add_dependency``), a callback (``add_callback``), or a
        direct param edit in the side-panel ChunkWidget.

        All of those paths ultimately dirty the chunk's DagNode and terminate
        in ``chunk.send_self()``, which emits CHUNK_CHANGED with the chunk as
        sender. One listener covers all of them.

        Safe to call repeatedly; the subscription is created once per node.
        """
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
        """Tf.Notice callback. Defers downstream execution to the next event
        loop tick so the chunk has time to finish its own solve first."""
        if self._executing:
            return
        QtCore.QTimer.singleShot(0, self._trigger_downstream)

    def _get_graph_widget(self):
        import sys
        ref = sys.modules.get('_node_graph_ref')
        return ref.widget if ref and hasattr(ref, 'widget') else None

    def _trigger_downstream(self):
        """Re-execute this node then cascade to its downstream connected nodes.

        Mirrors _TriggerNodeBase._trigger_downstream, but also re-runs self so
        the output cache (``prim``, ``layer``) reflects the chunk's new state
        before downstream nodes pull from it.
        """
        widget = self._get_graph_widget()
        if not widget or not widget._stage:
            return
        try:
            widget._execute_node(self)
        except Exception:
            pass
        try:
            downstream = widget._get_downstream(self)
        except Exception:
            return
        # Also pull in @ref dependants, matching _TriggerNodeBase behavior.
        downstream_names = {n.name() for n in downstream}
        try:
            for n in widget._graph.all_nodes():
                if n in downstream or n is self:
                    continue
                if not hasattr(n, 'model'):
                    continue
                for val in n.model.custom_properties.values():
                    if isinstance(val, str) and val.startswith("@"):
                        ref_node = val[1:].split(".")[0]
                        if ref_node in downstream_names:
                            downstream.append(n)
                            break
        except Exception:
            pass
        for n in downstream:
            try:
                widget._execute_node(n)
            except Exception:
                pass
        # Re-assert focus on the node graph after usdview updates may have stolen it.
        # Use two back-to-back timers so we fire after usdview's notice callbacks settle.
        try:
            win = widget.window()
            def _refocus():
                try:
                    win.raise_()
                    win.activateWindow()
                    # Also focus the props panel so keyboard events stay in node graph
                    props = getattr(widget, '_props_widget', None)
                    if props:
                        props.setFocus(QtCore.Qt.OtherFocusReason)
                except Exception:
                    pass
            QtCore.QTimer.singleShot(150, _refocus)
        except Exception:
            pass


# class UVCheckerNode(ChunkWrapperBase):
#     """Apply UV checker material to a prim. Drives the live UV Checker chunk."""
#     NODE_NAME = "UV Checker"

#     def __init__(self):
#         super().__init__()
#         self.add_input("prim", color=PORT_COLORS["prim"])
#         # Read params from live chunk if already loaded
#         from .chunk_adapter import ChunkAdapter
#         adapter = ChunkAdapter("UV Checker")  # no auto_create
#         if adapter.chunk:
#             for p in adapter.get_params():
#                 if isinstance(p, chunk_core.ChunkVectorBase):
#                     for sub in p:
#                         self.add_input(f"in_{sub.name}", color=PORT_COLORS["float"])
#                         self.add_text_input(sub.name, sub.name,
#                                             text=str(sub.value if sub.value else 0))
#                 elif isinstance(p, Button):
#                     pass
#                 elif hasattr(p, "value"):
#                     self.add_input(f"in_{p.name}", color=PORT_COLORS["any"])
#                     self.add_text_input(p.name, p.name,
#                                         text=str(p.value) if p.value else "")
#         else:
#             self.add_input("in_u_scale", color=PORT_COLORS["float"])
#             self.add_text_input("u_scale", "U Scale", text="1.0")
#             self.add_input("in_v_scale", color=PORT_COLORS["float"])
#             self.add_text_input("v_scale", "V Scale", text="1.0")
#             self.add_input("in_browse", color=PORT_COLORS["string"])
#             self.add_text_input("browse", "Texture", text="")
#         self.add_output("prim", color=PORT_COLORS["prim"])

#     def execute(self, stage):
#         from .chunk_adapter import ChunkAdapter
#         prim = self._get_input_value("prim")
#         if not prim:
#             return {"prim": None}

#         # Reuse or create adapter (only auto_create once)
#         if not hasattr(self, '_cached_adapter') or not self._cached_adapter.chunk:
#             self._cached_adapter = ChunkAdapter("UV Checker", auto_create=True)
#         adapter = self._cached_adapter
#         if not adapter.chunk:
#             print(f"[UVCheckerNode] Not found. Available: {ChunkAdapter.list_available()}")
#             return {"prim": prim}

#         scene = chunk_registry.ChunkRegistry().scene
#         ctrl = scene.usdviewAPI._UsdviewApi__appController
#         old_sel = scene.get_selected_prims()
#         ctrl._dataModel.selection.setPrim(prim)

#         # Push values: try input port first, then text field
#         # Collect all our node properties
#         def _get_val(name):
#             """Try input port in_{name}, then property {name}."""
#             v = self._get_input_value(f"in_{name}")
#             if v is not None:
#                 return v
#             v = self.get_property(name)
#             return v

#         # Get all node property names (excluding standard ones)
#         prop_names = list(self.model.custom_properties.keys())

#         for p in adapter.get_params():
#             if isinstance(p, chunk_core.ChunkVectorBase):
#                 vals = []
#                 for i, sub in enumerate(p):
#                     # Try matching: in_{sub.name} port, then sub.name prop, then by index from our props
#                     v = _get_val(sub.name)
#                     if v is None and i < len(prop_names):
#                         v = _get_val(prop_names[i])
#                     if v is None:
#                         v = sub.value
#                     vals.append(float(v) if v else 0)
#                 adapter.set_param(p.name, vals)
#             elif isinstance(p, Button):
#                 pass
#             elif hasattr(p, "value"):
#                 v = _get_val(p.name)
#                 if v is not None and v != "":
#                     adapter.set_param(p.name, v)

#         print(f"[UVCheckerNode] params set: {[(p.name, p.value if hasattr(p,'value') else '?') for p in adapter.get_params() if not isinstance(p, Button)]}")

#         adapter.press_button("clear")
#         adapter.press_button("apply")

#         if old_sel:
#             ctrl._dataModel.selection.setPrim(old_sel[0])
#         return {"prim": prim}


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
        from .chunk_adapter import ChunkAdapter
        prim = self._get_input_value("prim")
        chunk_name = (self.get_property("chunk_name") or "").strip()
        if not chunk_name:
            print(f"[DynamicChunkNode] Available: {ChunkAdapter.list_available()}")
            return {"prim": prim}

        adapter = ChunkAdapter(chunk_name)
        if not adapter.chunk:
            print(f"[DynamicChunkNode] '{chunk_name}' not found. Available: {ChunkAdapter.list_available()}")
            return {"prim": prim}

        # Arm the notice-dependency listener so this node behaves like an
        # Event trigger when the wrapped chunk reacts — matches the chunk
        # panel's reactivity.
        self._subscribe_to_chunk_notices(adapter.chunk)

        self._executing = True
        try:
            old_sel = []
            if prim:
                from .graph_host import GraphHost
                host = GraphHost.current()
                old_sel = host.get_selection()
                host.set_selection(prim)

            btn = (self.get_property("button_name") or "").strip() or None
            adapter.press_button(btn)

            if prim and old_sel:
                from .graph_host import GraphHost
                GraphHost.current().set_selection(old_sel[0])
            return {"prim": prim}
        finally:
            self._executing = False


# ────────────────────────────────────────────
#  Transform nodes
# ────────────────────────────────────────────
class SetTransformNode(UsdNodeBase):
    """Set translate/rotate/scale on a prim."""
    NODE_NAME = "Set Transform"
    CATEGORY = "Transform"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("tx", "Translate X", text="0")
        self.add_text_input("ty", "Translate Y", text="0")
        self.add_text_input("tz", "Translate Z", text="0")
        self.add_text_input("rx", "Rotate X", text="0")
        self.add_text_input("ry", "Rotate Y", text="0")
        self.add_text_input("rz", "Rotate Z", text="0")
        self.add_text_input("sx", "Scale X", text="1")
        self.add_text_input("sy", "Scale Y", text="1")
        self.add_text_input("sz", "Scale Z", text="1")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage):
        from pxr import UsdGeom, Gf
        prim = self._get_input_value("prim")
        if not prim:
            return {"prim": None}
        xform = UsdGeom.Xformable(prim)
        xform.ClearXformOpOrder()
        xform.AddTranslateOp().Set(Gf.Vec3d(
            float(self.get_property("tx") or 0),
            float(self.get_property("ty") or 0),
            float(self.get_property("tz") or 0)))
        xform.AddRotateXYZOp().Set(Gf.Vec3f(
            float(self.get_property("rx") or 0),
            float(self.get_property("ry") or 0),
            float(self.get_property("rz") or 0)))
        s = Gf.Vec3f(
            float(self.get_property("sx") or 1),
            float(self.get_property("sy") or 1),
            float(self.get_property("sz") or 1))
        if s != Gf.Vec3f(1, 1, 1):
            xform.AddScaleOp().Set(Gf.Vec3f(s))
        return {"prim": prim}


class GetWorldTransformNode(UsdNodeBase):
    """Get world-space transform of a prim — individual components for easy referencing."""
    NODE_NAME = "Get World Transform"
    CATEGORY = "Transform"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_output("tx", color=PORT_COLORS["float"])
        self.add_output("ty", color=PORT_COLORS["float"])
        self.add_output("tz", color=PORT_COLORS["float"])
        self.add_output("rx", color=PORT_COLORS["float"])
        self.add_output("ry", color=PORT_COLORS["float"])
        self.add_output("rz", color=PORT_COLORS["float"])
        self.add_output("sx", color=PORT_COLORS["float"])
        self.add_output("sy", color=PORT_COLORS["float"])
        self.add_output("sz", color=PORT_COLORS["float"])
        self.add_output("matrix", color=PORT_COLORS["any"])
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage):
        from pxr import UsdGeom, Gf
        prim = self._get_input_value("prim")
        if prim is None:
            return {"tx": 0, "ty": 0, "tz": 0, "rx": 0, "ry": 0, "rz": 0,
                    "sx": 1, "sy": 1, "sz": 1, "matrix": None, "prim": None}
        xf = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(0)
        t = xf.ExtractTranslation()
        r = xf.ExtractRotation()
        rot = r.Decompose(Gf.Vec3d.XAxis(), Gf.Vec3d.YAxis(), Gf.Vec3d.ZAxis())
        # Extract scale from matrix
        m = xf
        sx = Gf.Vec3d(m[0][0], m[0][1], m[0][2]).GetLength()
        sy = Gf.Vec3d(m[1][0], m[1][1], m[1][2]).GetLength()
        sz = Gf.Vec3d(m[2][0], m[2][1], m[2][2]).GetLength()
        return {"tx": t[0], "ty": t[1], "tz": t[2],
                "rx": rot[0], "ry": rot[1], "rz": rot[2],
                "sx": sx, "sy": sy, "sz": sz,
                "matrix": xf, "prim": prim}


class ResetTransformNode(UsdNodeBase):
    """Clear all xform ops on a prim."""
    NODE_NAME = "Reset Transform"
    CATEGORY = "Transform"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage):
        from pxr import UsdGeom
        prim = self._get_input_value("prim")
        if prim:
            UsdGeom.Xformable(prim).ClearXformOpOrder()
        return {"prim": prim}


class GetBoundingBoxNode(UsdNodeBase):
    """Get bounding box of a prim."""
    NODE_NAME = "Get Bounding Box"
    CATEGORY = "Transform"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_output("min", color=PORT_COLORS["any"])
        self.add_output("max", color=PORT_COLORS["any"])
        self.add_output("size", color=PORT_COLORS["any"])
        self.add_output("center", color=PORT_COLORS["any"])

    def execute(self, stage):
        from pxr import UsdGeom, Gf
        prim = self._get_input_value("prim")
        if not prim:
            return {"min": None, "max": None, "size": None, "center": None}
        bbox = UsdGeom.BBoxCache(0, [UsdGeom.Tokens.default_]).ComputeWorldBound(prim)
        r = bbox.ComputeAlignedRange()
        mn, mx = r.GetMin(), r.GetMax()
        sz = mx - mn
        ct = (mn + mx) / 2
        return {"min": tuple(mn), "max": tuple(mx), "size": tuple(sz), "center": tuple(ct)}


class ConstrainTransformNode(UsdNodeBase):
    """Constrain target prim(s) to follow source prim's world transform.
    Unchecked: snap all targets to source. Checked: each target maintains
    its own relative offset, captured automatically on first execute.
    Clear the offset_data field to force a re-capture on all targets."""
    NODE_NAME = "Constrain Transform"
    CATEGORY = "Transform"

    def __init__(self):
        super().__init__()
        self.add_input("source", color=PORT_COLORS["prim"])
        self.add_input("target", color=PORT_COLORS["list"])
        self.add_checkbox("maintain_offset", label="Maintain Offset", text="Maintain Offset", state=False)
        # JSON dict: {"/prim/path": [tx, ty, tz, rx, ry, rz], ...} — one entry per target
        self.add_text_input("offset_data", "Offsets (auto)", text="")
        self.add_output("prim", color=PORT_COLORS["prim"])
        self.add_output("prims", color=PORT_COLORS["list"])

    def execute(self, stage):
        import json
        from pxr import UsdGeom, Gf
        source = self._get_input_value("source")
        targets = self._get_prims("target")
        if not source or not targets:
            return {"prim": None, "prims": []}

        maintain = bool(self.get_property("maintain_offset"))

        src_xf = UsdGeom.Xformable(source).ComputeLocalToWorldTransform(0)
        src_t = src_xf.ExtractTranslation()
        src_rot = src_xf.ExtractRotation().Decompose(
            Gf.Vec3d.XAxis(), Gf.Vec3d.YAxis(), Gf.Vec3d.ZAxis())

        if maintain:
            raw = self.get_property("offset_data") or ""
            try:
                offsets = json.loads(raw) if raw.strip() else {}
            except Exception:
                offsets = {}

            # Auto-capture any target not yet in the dict
            new_captures = False
            for target in targets:
                path = str(target.GetPath())
                if path not in offsets:
                    tgt_xf = UsdGeom.Xformable(target).ComputeLocalToWorldTransform(0)
                    tgt_t = tgt_xf.ExtractTranslation()
                    tgt_rot = tgt_xf.ExtractRotation().Decompose(
                        Gf.Vec3d.XAxis(), Gf.Vec3d.YAxis(), Gf.Vec3d.ZAxis())
                    offsets[path] = [
                        round(tgt_t[0] - src_t[0], 4),
                        round(tgt_t[1] - src_t[1], 4),
                        round(tgt_t[2] - src_t[2], 4),
                        round(tgt_rot[0] - src_rot[0], 4),
                        round(tgt_rot[1] - src_rot[1], 4),
                        round(tgt_rot[2] - src_rot[2], 4),
                    ]
                    new_captures = True
            if new_captures:
                self.set_property("offset_data", json.dumps(offsets))

            for target in targets:
                path = str(target.GetPath())
                o = offsets.get(path, [0, 0, 0, 0, 0, 0])
                xform = UsdGeom.Xformable(target)
                xform.ClearXformOpOrder()
                xform.AddTranslateOp().Set(Gf.Vec3d(
                    src_t[0] + o[0], src_t[1] + o[1], src_t[2] + o[2]))
                xform.AddRotateXYZOp().Set(Gf.Vec3f(
                    float(src_rot[0] + o[3]), float(src_rot[1] + o[4]), float(src_rot[2] + o[5])))
        else:
            for target in targets:
                xform = UsdGeom.Xformable(target)
                xform.ClearXformOpOrder()
                xform.AddTranslateOp().Set(Gf.Vec3d(src_t[0], src_t[1], src_t[2]))
                xform.AddRotateXYZOp().Set(Gf.Vec3f(
                    float(src_rot[0]), float(src_rot[1]), float(src_rot[2])))

        return {"prim": targets[0], "prims": targets}


# ────────────────────────────────────────────
#  Hierarchy nodes
# ────────────────────────────────────────────
class CreatePrimNode(UsdNodeBase):
    """Create/define a prim at a path with a type."""
    NODE_NAME = "Create Prim"
    CATEGORY = "Hierarchy"

    def __init__(self):
        super().__init__()
        self.add_text_input("prim_path", "Prim Path", text="/NewPrim")
        self.add_text_input("prim_type", "Type", text="Xform")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage):
        path = self.get_property("prim_path") or "/NewPrim"
        typ = self.get_property("prim_type") or "Xform"
        prim = stage.DefinePrim(path, typ)
        return {"prim": prim if prim.IsValid() else None}


class DeletePrimNode(UsdNodeBase):
    """Remove a prim from the stage."""
    NODE_NAME = "Delete Prim"
    CATEGORY = "Hierarchy"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])

    def execute(self, stage):
        prim = self._get_input_value("prim")
        if prim and prim.IsValid():
            stage.RemovePrim(prim.GetPath())
        return {}


class ReparentPrimNode(UsdNodeBase):
    """Move a prim under a new parent (via rename)."""
    NODE_NAME = "Reparent Prim"
    CATEGORY = "Hierarchy"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("new_parent", "New Parent Path", text="/")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage):
        from pxr import Sdf
        prim = self._get_input_value("prim")
        parent = self.get_property("new_parent") or "/"
        if not prim:
            return {"prim": None}
        old_path = prim.GetPath()
        new_path = Sdf.Path(parent).AppendChild(old_path.name)
        edit = Sdf.BatchNamespaceEdit()
        edit.Add(old_path, new_path)
        stage.GetEditTarget().GetLayer().Apply(edit)
        return {"prim": stage.GetPrimAtPath(new_path)}


class DuplicatePrimNode(UsdNodeBase):
    """Duplicate a prim to a new path."""
    NODE_NAME = "Duplicate Prim"
    CATEGORY = "Hierarchy"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("dest_path", "Destination Path", text="")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage):
        from pxr import Sdf
        prim = self._get_input_value("prim")
        dest = self.get_property("dest_path")
        if not prim or not dest:
            return {"prim": None}
        Sdf.CopySpec(stage.GetEditTarget().GetLayer(),
                      prim.GetPath(), stage.GetEditTarget().GetLayer(),
                      Sdf.Path(dest))
        return {"prim": stage.GetPrimAtPath(dest)}


# ────────────────────────────────────────────
#  Composition nodes
# ────────────────────────────────────────────
class AddReferenceNode(UsdNodeBase):
    """Add a reference to a prim from a USD file."""
    NODE_NAME = "Add Reference"
    CATEGORY = "Composition"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("file_path", "USD File", text="")
        self.add_text_input("prim_path", "Prim Path (optional)", text="")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage):
        from pxr import Sdf
        prim = self._get_input_value("prim")
        if not prim:
            return {"prim": None}
        fp = self.get_property("file_path") or ""
        pp = self.get_property("prim_path") or ""
        ref = Sdf.Reference(fp, Sdf.Path(pp)) if pp else Sdf.Reference(fp)
        prim.GetReferences().AddReference(ref)
        return {"prim": prim}


class AddPayloadNode(UsdNodeBase):
    """Add a payload to a prim."""
    NODE_NAME = "Add Payload"
    CATEGORY = "Composition"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("file_path", "USD File", text="")
        self.add_text_input("prim_path", "Prim Path (optional)", text="")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage):
        from pxr import Sdf
        prim = self._get_input_value("prim")
        if not prim:
            return {"prim": None}
        fp = self.get_property("file_path") or ""
        pp = self.get_property("prim_path") or ""
        payload = Sdf.Payload(fp, Sdf.Path(pp)) if pp else Sdf.Payload(fp)
        prim.GetPayloads().AddPayload(payload)
        return {"prim": prim}


class GetLayerStackNode(UsdNodeBase):
    """List all layers in the stage's layer stack."""
    NODE_NAME = "Get Layer Stack"
    CATEGORY = "Composition"

    def __init__(self):
        super().__init__()
        self.add_output("layers", color=PORT_COLORS["list"])

    def execute(self, stage):
        layers = [(l.identifier, l.realPath) for l in stage.GetLayerStack()]
        return {"layers": layers}


# ────────────────────────────────────────────
#  Visibility / Purpose nodes
# ────────────────────────────────────────────
class SetVisibilityNode(UsdNodeBase):
    """Set visibility on a prim (inherited/invisible)."""
    NODE_NAME = "Set Visibility"
    CATEGORY = "USD"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("visible", "Visible (true/false)", text="true")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage):
        from pxr import UsdGeom
        prim = self._get_input_value("prim")
        if not prim:
            return {"prim": None}
        vis = self.get_property("visible") or "true"
        img = UsdGeom.Imageable(prim)
        if vis.lower() in ("false", "0", "invisible"):
            img.MakeInvisible()
        else:
            img.MakeVisible()
        return {"prim": prim}


class SetPurposeNode(UsdNodeBase):
    """Set purpose on a prim (default/render/proxy/guide)."""
    NODE_NAME = "Set Purpose"
    CATEGORY = "USD"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("purpose", "Purpose", text="default")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage):
        from pxr import UsdGeom
        prim = self._get_input_value("prim")
        if not prim:
            return {"prim": None}
        UsdGeom.Imageable(prim).GetPurposeAttr().Set(self.get_property("purpose") or "default")
        return {"prim": prim}


# ────────────────────────────────────────────
#  Search / Query nodes
# ────────────────────────────────────────────
class FindByTypeNode(UsdNodeBase):
    """Find all prims of a given type."""
    NODE_NAME = "Find by Type"
    CATEGORY = "USD"

    def __init__(self):
        super().__init__()
        self.add_text_input("type_name", "Type (e.g. Mesh)", text="Mesh")
        self.add_output("prims", color=PORT_COLORS["list"])

    def execute(self, stage):
        t = self.get_property("type_name") or "Mesh"
        prims = [p for p in stage.Traverse() if p.GetTypeName() == t]
        return {"prims": prims}


class FindByPatternNode(UsdNodeBase):
    """Find prims whose path contains a substring."""
    NODE_NAME = "Find by Pattern"
    CATEGORY = "USD"

    def __init__(self):
        super().__init__()
        self.add_text_input("pattern", "Pattern", text="")
        self.add_output("prims", color=PORT_COLORS["list"])

    def execute(self, stage):
        pat = (self.get_property("pattern") or "").lower()
        prims = [p for p in stage.Traverse() if pat in str(p.GetPath()).lower()]
        return {"prims": prims}


class IsVisibleNode(UsdNodeBase):
    """Check if a prim is visible."""
    NODE_NAME = "Is Visible"
    CATEGORY = "USD"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_output("visible", color=PORT_COLORS["bool"])
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage):
        from pxr import UsdGeom
        prim = self._get_input_value("prim")
        if not prim:
            return {"visible": False, "prim": None}
        vis = UsdGeom.Imageable(prim).ComputeVisibility() != UsdGeom.Tokens.invisible
        return {"visible": vis, "prim": prim}


# ────────────────────────────────────────────
#  Animation nodes
# ────────────────────────────────────────────
class SetKeyNode(UsdNodeBase):
    """Set an attribute value at a specific timecode."""
    NODE_NAME = "Set Key"
    CATEGORY = "Animation"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("attr_name", "Attribute", text="xformOp:translate")
        self.add_text_input("time", "Time", text="0")
        self.add_text_input("value", "Value", text="(0,0,0)")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage):
        from pxr import Usd
        prim = self._get_input_value("prim")
        if not prim:
            return {"prim": None}
        attr = prim.GetAttribute(self.get_property("attr_name") or "")
        if attr and attr.IsValid():
            t = float(self.get_property("time") or 0)
            v = self.get_property("value") or ""
            try:
                val = eval(v)
            except Exception:
                val = v
            attr.Set(val, Usd.TimeCode(t))
        return {"prim": prim}


class GetTimeRangeNode(UsdNodeBase):
    """Get stage start/end frame."""
    NODE_NAME = "Get Time Range"
    CATEGORY = "Animation"

    def __init__(self):
        super().__init__()
        self.add_output("start", color=PORT_COLORS["float"])
        self.add_output("end", color=PORT_COLORS["float"])

    def execute(self, stage):
        return {"start": stage.GetStartTimeCode(), "end": stage.GetEndTimeCode()}


# ────────────────────────────────────────────
#  List utility nodes
# ────────────────────────────────────────────
class ListIndexNode(UsdNodeBase):
    """Get item at index from a list."""
    NODE_NAME = "List Index"
    CATEGORY = "Utility"

    def __init__(self):
        super().__init__()
        self.add_input("list", color=PORT_COLORS["list"])
        self.add_text_input("index", "Index", text="0")
        self.add_output("item", color=PORT_COLORS["any"])

    def execute(self, stage):
        lst = self._get_input_value("list") or []
        idx = int(self.get_property("index") or 0)
        return {"item": lst[idx] if 0 <= idx < len(lst) else None}


class ListLengthNode(UsdNodeBase):
    """Get length of a list."""
    NODE_NAME = "List Length"
    CATEGORY = "Utility"

    def __init__(self):
        super().__init__()
        self.add_input("list", color=PORT_COLORS["list"])
        self.add_output("length", color=PORT_COLORS["float"])

    def execute(self, stage):
        lst = self._get_input_value("list") or []
        return {"length": len(lst)}


class ForEachNode(UsdNodeBase):
    """Iterate a list — the execution engine re-runs downstream nodes per item."""
    NODE_NAME = "For Each"
    CATEGORY = "Utility"

    def __init__(self):
        super().__init__()
        self.add_input("list", color=PORT_COLORS["list"])
        self.add_output("item", color=PORT_COLORS["any"])
        self.add_output("index", color=PORT_COLORS["float"])
        self._iteration_list = []
        self._current_index = 0

    def execute(self, stage):
        lst = self._get_input_value("list") or []
        self._iteration_list = lst
        # Default: output first item. Engine calls set_iteration_index() for each.
        if lst:
            return {"item": lst[self._current_index], "index": self._current_index}
        return {"item": None, "index": -1}

    def set_iteration_index(self, idx):
        self._current_index = idx
        if 0 <= idx < len(self._iteration_list):
            self._output_cache = {"item": self._iteration_list[idx], "index": idx}
        return idx < len(self._iteration_list)


class MergeNode(UsdNodeBase):
    """Merge multiple prims into a list. Connect prim nodes to inputs."""
    NODE_NAME = "Merge"
    CATEGORY = "Utility"

    def __init__(self):
        super().__init__()
        for i in range(6):
            self.add_input(f"in_{i}", color=PORT_COLORS["prim"])
        self.add_output("prims", color=PORT_COLORS["list"])

    def execute(self, stage):
        prims = []
        for i in range(6):
            v = self._get_input_value(f"in_{i}")
            if v is not None:
                if isinstance(v, list):
                    prims.extend(v)
                else:
                    prims.append(v)
        return {"prims": prims}


# ────────────────────────────────────────────
#  Additional Input nodes
# ────────────────────────────────────────────
class FloatNode(UsdNodeBase):
    """A simple float constant."""
    NODE_NAME = "Float"
    CATEGORY = "Input"

    def __init__(self):
        super().__init__()
        self.add_text_input("value", "Value", text="0.0")
        self.add_output("float", color=PORT_COLORS["float"])

    def execute(self, stage):
        try:
            return {"float": float(self.get_property("value") or 0)}
        except ValueError:
            return {"float": 0.0}


class BoolNode(UsdNodeBase):
    """A boolean toggle constant."""
    NODE_NAME = "Bool"
    CATEGORY = "Input"

    def __init__(self):
        super().__init__()
        self.add_text_input("value", "Value (true/false)", text="true")
        self.add_output("bool", color=PORT_COLORS["bool"])

    def execute(self, stage):
        v = (self.get_property("value") or "").lower()
        return {"bool": v in ("true", "1", "yes")}


# ────────────────────────────────────────────
#  Additional Hierarchy nodes
# ────────────────────────────────────────────
class RenamePrimNode(UsdNodeBase):
    """Rename a prim (change its name, keep same parent)."""
    NODE_NAME = "Rename Prim"
    CATEGORY = "Hierarchy"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("new_name", "New Name", text="")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage):
        from pxr import Sdf
        prim = self._get_input_value("prim")
        new_name = (self.get_property("new_name") or "").strip()
        if not prim or not new_name:
            return {"prim": prim}
        old_path = prim.GetPath()
        new_path = old_path.GetParentPath().AppendChild(new_name)
        edit = Sdf.BatchNamespaceEdit()
        edit.Add(old_path, new_path)
        stage.GetEditTarget().GetLayer().Apply(edit)
        return {"prim": stage.GetPrimAtPath(new_path)}


# ────────────────────────────────────────────
#  Additional Utility nodes
# ────────────────────────────────────────────
class CompareNode(UsdNodeBase):
    """Compare two values. Outputs bool result."""
    NODE_NAME = "Compare"
    CATEGORY = "Utility"

    def __init__(self):
        super().__init__()
        self.add_input("a", color=PORT_COLORS["any"])
        self.add_input("b", color=PORT_COLORS["any"])
        self.add_text_input("op", "Operator (==, !=, <, >, <=, >=)", text="==")
        self.add_output("result", color=PORT_COLORS["bool"])

    def execute(self, stage):
        a = self._get_input_value("a")
        b = self._get_input_value("b")
        op = (self.get_property("op") or "==").strip()
        ops = {"==": lambda x, y: x == y, "!=": lambda x, y: x != y,
               "<": lambda x, y: x < y, ">": lambda x, y: x > y,
               "<=": lambda x, y: x <= y, ">=": lambda x, y: x >= y}
        try:
            return {"result": ops.get(op, ops["=="])(a, b)}
        except Exception:
            return {"result": False}


class SwitchNode(UsdNodeBase):
    """Route one of two inputs based on a bool condition."""
    NODE_NAME = "Switch"
    CATEGORY = "Utility"

    def __init__(self):
        super().__init__()
        self.add_input("condition", color=PORT_COLORS["bool"])
        self.add_input("if_true", color=PORT_COLORS["any"])
        self.add_input("if_false", color=PORT_COLORS["any"])
        self.add_output("output", color=PORT_COLORS["any"])

    def execute(self, stage):
        cond = self._get_input_value("condition")
        if cond:
            return {"output": self._get_input_value("if_true")}
        return {"output": self._get_input_value("if_false")}


# ────────────────────────────────────────────
#  Additional USD nodes
# ────────────────────────────────────────────
class StageInfoNode(UsdNodeBase):
    """Output stage metadata: up axis, meters per unit, default prim."""
    NODE_NAME = "Stage Info"
    CATEGORY = "USD"

    def __init__(self):
        super().__init__()
        self.add_output("up_axis", color=PORT_COLORS["string"])
        self.add_output("meters_per_unit", color=PORT_COLORS["float"])
        self.add_output("default_prim", color=PORT_COLORS["string"])

    def execute(self, stage):
        from pxr import UsdGeom
        return {
            "up_axis": UsdGeom.GetStageUpAxis(stage),
            "meters_per_unit": UsdGeom.GetStageMetersPerUnit(stage),
            "default_prim": stage.GetDefaultPrim().GetName() if stage.HasDefaultPrim() else "",
        }


class ExportUsdNode(UsdNodeBase):
    """Export the current stage (or edit target layer) to a USD file."""
    NODE_NAME = "Export USD"
    CATEGORY = "Utility"

    def __init__(self):
        super().__init__()
        self.add_text_input("file_path", "File Path", text="")
        self.add_text_input("mode", "Mode (stage/layer)", text="stage")
        self.add_output("path", color=PORT_COLORS["string"])

    def execute(self, stage):
        fp = (self.get_property("file_path") or "").strip()
        if not fp:
            return {"path": ""}
        mode = (self.get_property("mode") or "stage").lower()
        if mode == "layer":
            stage.GetEditTarget().GetLayer().Export(fp)
        else:
            stage.Export(fp)
        return {"path": fp}


# ────────────────────────────────────────────
#  Additional Animation nodes
# ────────────────────────────────────────────
class SetTimeCodeNode(UsdNodeBase):
    """Set stage start and end time codes."""
    NODE_NAME = "Set Time Codes"
    CATEGORY = "Animation"

    def __init__(self):
        super().__init__()
        self.add_text_input("start", "Start Frame", text="0")
        self.add_text_input("end", "End Frame", text="100")

    def execute(self, stage):
        start = float(self.get_property("start") or 0)
        end = float(self.get_property("end") or 100)
        stage.SetStartTimeCode(start)
        stage.SetEndTimeCode(end)
        return {}


# ────────────────────────────────────────────
#  Math nodes
# ────────────────────────────────────────────
class MathAddNode(UsdNodeBase):
    NODE_NAME = "Add"
    CATEGORY = "Math"
    def __init__(self):
        super().__init__()
        self.add_input("a", color=PORT_COLORS["float"])
        self.add_input("b", color=PORT_COLORS["float"])
        self.add_text_input("b_val", "B (if no input)", text="0")
        self.add_output("result", color=PORT_COLORS["float"])
    def execute(self, stage):
        a = self._get_input_value("a") or 0
        b = self._get_input_value("b")
        if b is None:
            b = float(self.get_property("b_val") or 0)
        return {"result": float(a) + float(b)}


class MathSubtractNode(UsdNodeBase):
    NODE_NAME = "Subtract"
    CATEGORY = "Math"
    def __init__(self):
        super().__init__()
        self.add_input("a", color=PORT_COLORS["float"])
        self.add_input("b", color=PORT_COLORS["float"])
        self.add_text_input("b_val", "B (if no input)", text="0")
        self.add_output("result", color=PORT_COLORS["float"])
    def execute(self, stage):
        a = self._get_input_value("a") or 0
        b = self._get_input_value("b")
        if b is None:
            b = float(self.get_property("b_val") or 0)
        return {"result": float(a) - float(b)}


class MathMultiplyNode(UsdNodeBase):
    NODE_NAME = "Multiply"
    CATEGORY = "Math"
    def __init__(self):
        super().__init__()
        self.add_input("a", color=PORT_COLORS["float"])
        self.add_input("b", color=PORT_COLORS["float"])
        self.add_text_input("b_val", "B (if no input)", text="1")
        self.add_output("result", color=PORT_COLORS["float"])
    def execute(self, stage):
        a = self._get_input_value("a") or 0
        b = self._get_input_value("b")
        if b is None:
            b = float(self.get_property("b_val") or 1)
        return {"result": float(a) * float(b)}


class MathDivideNode(UsdNodeBase):
    NODE_NAME = "Divide"
    CATEGORY = "Math"
    def __init__(self):
        super().__init__()
        self.add_input("a", color=PORT_COLORS["float"])
        self.add_input("b", color=PORT_COLORS["float"])
        self.add_text_input("b_val", "B (if no input)", text="1")
        self.add_output("result", color=PORT_COLORS["float"])
    def execute(self, stage):
        a = self._get_input_value("a") or 0
        b = self._get_input_value("b")
        if b is None:
            b = float(self.get_property("b_val") or 1)
        return {"result": float(a) / float(b) if float(b) != 0 else 0.0}


class MathClampNode(UsdNodeBase):
    NODE_NAME = "Clamp"
    CATEGORY = "Math"
    def __init__(self):
        super().__init__()
        self.add_input("value", color=PORT_COLORS["float"])
        self.add_text_input("min_val", "Min", text="0")
        self.add_text_input("max_val", "Max", text="1")
        self.add_output("result", color=PORT_COLORS["float"])
    def execute(self, stage):
        v = float(self._get_input_value("value") or 0)
        mn = float(self.get_property("min_val") or 0)
        mx = float(self.get_property("max_val") or 1)
        return {"result": max(mn, min(mx, v))}


class MathRemapNode(UsdNodeBase):
    NODE_NAME = "Remap"
    CATEGORY = "Math"
    def __init__(self):
        super().__init__()
        self.add_input("value", color=PORT_COLORS["float"])
        self.add_text_input("in_min", "In Min", text="0")
        self.add_text_input("in_max", "In Max", text="1")
        self.add_text_input("out_min", "Out Min", text="0")
        self.add_text_input("out_max", "Out Max", text="100")
        self.add_output("result", color=PORT_COLORS["float"])
    def execute(self, stage):
        v = float(self._get_input_value("value") or 0)
        i0 = float(self.get_property("in_min") or 0)
        i1 = float(self.get_property("in_max") or 1)
        o0 = float(self.get_property("out_min") or 0)
        o1 = float(self.get_property("out_max") or 100)
        if i1 == i0:
            return {"result": o0}
        t = (v - i0) / (i1 - i0)
        return {"result": o0 + t * (o1 - o0)}


# ────────────────────────────────────────────
#  String utility nodes
# ────────────────────────────────────────────
class StringFormatNode(UsdNodeBase):
    """Format string with {0}, {1}, {2} placeholders from inputs."""
    NODE_NAME = "String Format"
    CATEGORY = "Utility"
    def __init__(self):
        super().__init__()
        self.add_text_input("template", "Template", text="{0}")
        self.add_input("in_0", color=PORT_COLORS["any"])
        self.add_input("in_1", color=PORT_COLORS["any"])
        self.add_input("in_2", color=PORT_COLORS["any"])
        self.add_output("string", color=PORT_COLORS["string"])
    def execute(self, stage):
        t = self.get_property("template") or "{0}"
        args = [self._get_input_value(f"in_{i}") or "" for i in range(3)]
        try:
            return {"string": t.format(*args)}
        except Exception:
            return {"string": t}


class StringSplitNode(UsdNodeBase):
    NODE_NAME = "String Split"
    CATEGORY = "Utility"
    def __init__(self):
        super().__init__()
        self.add_input("string", color=PORT_COLORS["string"])
        self.add_text_input("delimiter", "Delimiter", text="/")
        self.add_output("parts", color=PORT_COLORS["list"])
    def execute(self, stage):
        s = str(self._get_input_value("string") or "")
        d = self.get_property("delimiter") or "/"
        return {"parts": s.split(d)}


class StringJoinNode(UsdNodeBase):
    NODE_NAME = "String Join"
    CATEGORY = "Utility"
    def __init__(self):
        super().__init__()
        self.add_input("list", color=PORT_COLORS["list"])
        self.add_text_input("delimiter", "Delimiter", text=", ")
        self.add_output("string", color=PORT_COLORS["string"])
    def execute(self, stage):
        lst = self._get_input_value("list") or []
        d = self.get_property("delimiter") or ", "
        return {"string": d.join(str(x) for x in lst)}


class StringReplaceNode(UsdNodeBase):
    NODE_NAME = "String Replace"
    CATEGORY = "Utility"
    def __init__(self):
        super().__init__()
        self.add_input("string", color=PORT_COLORS["string"])
        self.add_text_input("find", "Find", text="")
        self.add_text_input("replace", "Replace", text="")
        self.add_output("string", color=PORT_COLORS["string"])
    def execute(self, stage):
        s = str(self._get_input_value("string") or "")
        f = self.get_property("find") or ""
        r = self.get_property("replace") or ""
        return {"string": s.replace(f, r) if f else s}


# ────────────────────────────────────────────
#  Bool logic nodes
# ────────────────────────────────────────────
class BoolAndNode(UsdNodeBase):
    NODE_NAME = "And"
    CATEGORY = "Utility"
    def __init__(self):
        super().__init__()
        self.add_input("a", color=PORT_COLORS["bool"])
        self.add_input("b", color=PORT_COLORS["bool"])
        self.add_output("result", color=PORT_COLORS["bool"])
    def execute(self, stage):
        return {"result": bool(self._get_input_value("a")) and bool(self._get_input_value("b"))}


class BoolOrNode(UsdNodeBase):
    NODE_NAME = "Or"
    CATEGORY = "Utility"
    def __init__(self):
        super().__init__()
        self.add_input("a", color=PORT_COLORS["bool"])
        self.add_input("b", color=PORT_COLORS["bool"])
        self.add_output("result", color=PORT_COLORS["bool"])
    def execute(self, stage):
        return {"result": bool(self._get_input_value("a")) or bool(self._get_input_value("b"))}


class BoolNotNode(UsdNodeBase):
    NODE_NAME = "Not"
    CATEGORY = "Utility"
    def __init__(self):
        super().__init__()
        self.add_input("value", color=PORT_COLORS["bool"])
        self.add_output("result", color=PORT_COLORS["bool"])
    def execute(self, stage):
        return {"result": not bool(self._get_input_value("value"))}


class CoalesceNode(UsdNodeBase):
    """Returns the first non-null / valid input. For prims, checks IsValid()."""
    NODE_NAME = "Coalesce"
    CATEGORY = "Utility"
    def __init__(self):
        super().__init__()
        self.add_input("a", color=PORT_COLORS["prim"])
        self.add_input("b", color=PORT_COLORS["prim"])
        self.add_input("c", color=PORT_COLORS["prim"])
        self.add_output("prim", color=PORT_COLORS["prim"])
        self.add_output("index", color=PORT_COLORS["float"])
    def execute(self, stage):
        for i, name in enumerate(["a", "b", "c"]):
            v = self._get_input_value(name)
            if v is None:
                continue
            if hasattr(v, "IsValid") and not v.IsValid():
                continue
            return {"prim": v, "index": i}
        return {"prim": None, "index": -1}


class IsPrimValidNode(UsdNodeBase):
    """Check if a prim exists and is valid."""
    NODE_NAME = "Is Prim Valid"
    CATEGORY = "USD"
    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_output("valid", color=PORT_COLORS["bool"])
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage):
        prim = self._get_input_value("prim")
        valid = prim is not None and hasattr(prim, "IsValid") and prim.IsValid()
        return {"valid": valid, "prim": prim if valid else None}


# ────────────────────────────────────────────
#  Has nodes — branch true/false prim outputs
# ────────────────────────────────────────────
class HasVariantsNode(UsdNodeBase):
    """Check if prim has variant sets. Outputs prim on true or false branch."""
    NODE_NAME = "Has Variants"
    CATEGORY = "USD"
    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_output("true", color=PORT_COLORS["prim"])
        self.add_output("false", color=PORT_COLORS["prim"])
        self.add_output("names", color=PORT_COLORS["list"])
    def execute(self, stage):
        prim = self._get_input_value("prim")
        if prim is None or not hasattr(prim, "GetVariantSets"):
            return {"true": None, "false": None, "names": []}
        names = prim.GetVariantSets().GetNames()
        if names:
            return {"true": prim, "false": None, "names": names}
        return {"true": None, "false": prim, "names": []}


class HasChildrenNode(UsdNodeBase):
    """Check if prim has children."""
    NODE_NAME = "Has Children"
    CATEGORY = "USD"
    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_output("true", color=PORT_COLORS["prim"])
        self.add_output("false", color=PORT_COLORS["prim"])
        self.add_output("count", color=PORT_COLORS["float"])
    def execute(self, stage):
        prim = self._get_input_value("prim")
        if prim is None:
            return {"true": None, "false": None, "count": 0}
        children = list(prim.GetChildren())
        if children:
            return {"true": prim, "false": None, "count": len(children)}
        return {"true": None, "false": prim, "count": 0}


class HasMaterialNode(UsdNodeBase):
    """Check if prim has a material binding."""
    NODE_NAME = "Has Material"
    CATEGORY = "USD"
    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_output("true", color=PORT_COLORS["prim"])
        self.add_output("false", color=PORT_COLORS["prim"])
        self.add_output("material", color=PORT_COLORS["material"])
    def execute(self, stage):
        from pxr import UsdShade
        prim = self._get_input_value("prim")
        if prim is None:
            return {"true": None, "false": None, "material": None}
        binding = UsdShade.MaterialBindingAPI(prim).GetDirectBinding()
        mat_path = binding.GetMaterialPath() if binding else None
        if mat_path and str(mat_path) != "":
            mat_prim = stage.GetPrimAtPath(mat_path)
            return {"true": prim, "false": None, "material": mat_prim if mat_prim.IsValid() else None}
        return {"true": None, "false": prim, "material": None}


class HasAttributeNode(UsdNodeBase):
    """Check if prim has a specific attribute."""
    NODE_NAME = "Has Attribute"
    CATEGORY = "USD"
    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("attr_name", "Attribute Name", text="")
        self.add_output("true", color=PORT_COLORS["prim"])
        self.add_output("false", color=PORT_COLORS["prim"])
        self.add_output("value", color=PORT_COLORS["any"])
    def execute(self, stage):
        prim = self._get_input_value("prim")
        attr_name = (self.get_property("attr_name") or "").strip()
        if prim is None or not attr_name:
            return {"true": None, "false": prim, "value": None}
        attr = prim.GetAttribute(attr_name)
        if attr and attr.IsValid() and attr.HasValue():
            return {"true": prim, "false": None, "value": attr.Get()}
        return {"true": None, "false": prim, "value": None}


class HasReferenceNode(UsdNodeBase):
    """Check if prim has references or payloads."""
    NODE_NAME = "Has Reference"
    CATEGORY = "USD"
    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_output("true", color=PORT_COLORS["prim"])
        self.add_output("false", color=PORT_COLORS["prim"])
    def execute(self, stage):
        prim = self._get_input_value("prim")
        if prim is None:
            return {"true": None, "false": None}
        has = bool(prim.GetPrimStack()) and any(
            s.referenceList.prependedItems or s.referenceList.appendedItems
            or s.payloadList.prependedItems or s.payloadList.appendedItems
            for s in prim.GetPrimStack())
        return {"true": prim if has else None, "false": None if has else prim}


# ────────────────────────────────────────────
#  Additional USD query nodes
# ────────────────────────────────────────────
class GetRelationshipsNode(UsdNodeBase):
    """List relationships and their targets on a prim."""
    NODE_NAME = "Get Relationships"
    CATEGORY = "USD"
    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("rel_name", "Relationship (empty=all)", text="")
        self.add_output("targets", color=PORT_COLORS["list"])
        self.add_output("names", color=PORT_COLORS["list"])
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage):
        prim = self._get_input_value("prim")
        if not prim:
            return {"targets": [], "names": [], "prim": None}
        rel_name = (self.get_property("rel_name") or "").strip()
        if rel_name:
            rel = prim.GetRelationship(rel_name)
            if rel:
                # Resolve to actual prims where possible
                targets = []
                for t in rel.GetTargets():
                    p = stage.GetPrimAtPath(t)
                    targets.append(p if p.IsValid() else str(t))
                return {"targets": targets, "names": [rel_name], "prim": prim}
            return {"targets": [], "names": [], "prim": prim}
        # All relationships
        names = []
        targets = []
        for rel in prim.GetRelationships():
            names.append(rel.GetName())
            for t in rel.GetTargets():
                p = stage.GetPrimAtPath(t)
                targets.append(p if p.IsValid() else str(t))
        return {"targets": targets, "names": names, "prim": prim}


class GetMetadataNode(UsdNodeBase):
    """Get metadata from a prim. Leave Key empty to get ALL metadata as a dict."""
    NODE_NAME = "Get Metadata"
    CATEGORY = "USD"
    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("key", "Key (empty=all)", text="")
        self.add_output("value", color=PORT_COLORS["any"])
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage):
        prim = self._get_input_value("prim")
        if not prim:
            return {"value": None, "prim": None}
        key = (self.get_property("key") or "").strip()
        if not key:
            # Return all metadata as dict — same as usdview Metadata tab
            md = dict(prim.GetAllMetadata())
            md["customData"] = dict(prim.GetCustomData())
            md["path"] = str(prim.GetPath())
            md["typeName"] = prim.GetTypeName()
            md["specifier"] = str(prim.GetSpecifier())
            md["active"] = prim.IsActive()
            return {"value": md, "prim": prim}
        if key == "customData":
            return {"value": dict(prim.GetCustomData()), "prim": prim}
        return {"value": prim.GetMetadata(key), "prim": prim}


class SetMetadataNode(UsdNodeBase):
    """Set metadata on a prim."""
    NODE_NAME = "Set Metadata"
    CATEGORY = "USD"
    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_input("value", color=PORT_COLORS["any"])
        self.add_text_input("key", "Key", text="")
        self.add_text_input("manual_value", "Value (if no input)", text="")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage):
        prim = self._get_input_value("prim")
        key = (self.get_property("key") or "").strip()
        val = self._get_input_value("value")
        if val is None:
            manual = self.get_property("manual_value") or ""
            if manual:
                try:
                    val = eval(manual)
                except Exception:
                    val = manual
        if prim and key and val is not None:
            if key == "customData":
                prim.SetCustomData(val if isinstance(val, dict) else {"value": val})
            else:
                prim.SetMetadata(key, val)
        return {"prim": prim}


class GetPrimLayerStackNode(UsdNodeBase):
    """Get which layers have opinions about a specific prim."""
    NODE_NAME = "Get Prim Layers"
    CATEGORY = "USD"
    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_output("layers", color=PORT_COLORS["list"])
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage):
        from pxr import Usd
        prim = self._get_input_value("prim")
        if not prim:
            return {"layers": [], "prim": None}
        stack = prim.GetPrimStack()
        layers = [(s.layer.identifier, str(s.path)) for s in stack]
        return {"layers": layers, "prim": prim}


class ListPrimsNode(UsdNodeBase):
    """Traverse stage with combined filters: type, purpose, visibility, name pattern."""
    NODE_NAME = "List Prims"
    CATEGORY = "USD"
    def __init__(self):
        super().__init__()
        self.add_input("root", color=PORT_COLORS["prim"])
        self.add_text_input("type_filter", "Type (empty=any)", text="")
        self.add_text_input("name_pattern", "Name contains", text="")
        self.add_text_input("purpose_filter", "Purpose (empty=any)", text="")
        self.add_output("prims", color=PORT_COLORS["list"])
        self.add_output("count", color=PORT_COLORS["float"])
    def execute(self, stage):
        from pxr import UsdGeom
        root = self._get_input_value("root")
        type_f = (self.get_property("type_filter") or "").strip()
        name_f = (self.get_property("name_pattern") or "").strip().lower()
        purp_f = (self.get_property("purpose_filter") or "").strip()
        it = root.GetAllChildren() if root else stage.Traverse()
        if root:
            # Recursive traversal from root
            from pxr import Usd
            it = Usd.PrimRange(root)
        result = []
        for p in it:
            if type_f and p.GetTypeName() != type_f:
                continue
            if name_f and name_f not in p.GetName().lower():
                continue
            if purp_f:
                img = UsdGeom.Imageable(p)
                if img and img.GetPurposeAttr().Get() != purp_f:
                    continue
            result.append(p)
        return {"prims": result, "count": len(result)}


# ────────────────────────────────────────────
#  Counter node
# ────────────────────────────────────────────
class CounterNode(UsdNodeBase):
    """Increments each time the graph executes. Reset via the text field."""
    NODE_NAME = "Counter"
    CATEGORY = "Utility"
    _counters = {}  # node_id -> count

    def __init__(self):
        super().__init__()
        self.add_text_input("reset_to", "Reset To", text="0")
        self.add_output("count", color=PORT_COLORS["float"])

    def execute(self, stage):
        nid = self.id
        reset = self.get_property("reset_to")
        if nid not in CounterNode._counters:
            CounterNode._counters[nid] = int(reset or 0)
        else:
            CounterNode._counters[nid] += 1
        return {"count": CounterNode._counters[nid]}


# ────────────────────────────────────────────
#  Shape creation nodes
# ────────────────────────────────────────────
class CreateCubeNode(UsdNodeBase):
    NODE_NAME = "Create Cube"
    CATEGORY = "Create"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Path", text="/Cube")
        self.add_text_input("size", "Size", text="1.0")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage):
        from pxr import UsdGeom
        p = self.get_property("path") or "/Cube"
        prim = UsdGeom.Cube.Define(stage, p)
        prim.GetSizeAttr().Set(float(self.get_property("size") or 1))
        return {"prim": prim.GetPrim()}


class CreateSphereNode(UsdNodeBase):
    NODE_NAME = "Create Sphere"
    CATEGORY = "Create"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Path", text="/Sphere")
        self.add_text_input("radius", "Radius", text="1.0")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage):
        from pxr import UsdGeom
        p = self.get_property("path") or "/Sphere"
        prim = UsdGeom.Sphere.Define(stage, p)
        prim.GetRadiusAttr().Set(float(self.get_property("radius") or 1))
        return {"prim": prim.GetPrim()}


class CreateCylinderNode(UsdNodeBase):
    NODE_NAME = "Create Cylinder"
    CATEGORY = "Create"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Path", text="/Cylinder")
        self.add_text_input("radius", "Radius", text="1.0")
        self.add_text_input("h", "Height", text="2.0")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage):
        from pxr import UsdGeom
        p = self.get_property("path") or "/Cylinder"
        prim = UsdGeom.Cylinder.Define(stage, p)
        prim.GetRadiusAttr().Set(float(self.get_property("radius") or 1))
        prim.GetHeightAttr().Set(float(self.get_property("h") or 2))
        return {"prim": prim.GetPrim()}


class CreateConeNode(UsdNodeBase):
    NODE_NAME = "Create Cone"
    CATEGORY = "Create"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Path", text="/Cone")
        self.add_text_input("radius", "Radius", text="1.0")
        self.add_text_input("h", "Height", text="2.0")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage):
        from pxr import UsdGeom
        p = self.get_property("path") or "/Cone"
        prim = UsdGeom.Cone.Define(stage, p)
        prim.GetRadiusAttr().Set(float(self.get_property("radius") or 1))
        prim.GetHeightAttr().Set(float(self.get_property("h") or 2))
        return {"prim": prim.GetPrim()}


class CreateCapsuleNode(UsdNodeBase):
    NODE_NAME = "Create Capsule"
    CATEGORY = "Create"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Path", text="/Capsule")
        self.add_text_input("radius", "Radius", text="0.5")
        self.add_text_input("h", "Height", text="2.0")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage):
        from pxr import UsdGeom
        p = self.get_property("path") or "/Capsule"
        prim = UsdGeom.Capsule.Define(stage, p)
        prim.GetRadiusAttr().Set(float(self.get_property("radius") or 0.5))
        prim.GetHeightAttr().Set(float(self.get_property("h") or 2))
        return {"prim": prim.GetPrim()}


class CreatePlaneNode(UsdNodeBase):
    NODE_NAME = "Create Plane"
    CATEGORY = "Create"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Path", text="/Plane")
        self.add_text_input("w", "Width", text="10")
        self.add_text_input("length", "Length", text="10")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage):
        from pxr import UsdGeom, Vt, Gf
        p = self.get_property("path") or "/Plane"
        w = float(self.get_property("w") or 10) / 2
        l = float(self.get_property("length") or 10) / 2
        mesh = UsdGeom.Mesh.Define(stage, p)
        mesh.GetPointsAttr().Set(Vt.Vec3fArray([
            Gf.Vec3f(-w, -l, 0), Gf.Vec3f(w, -l, 0),
            Gf.Vec3f(w, l, 0), Gf.Vec3f(-w, l, 0)]))
        mesh.GetFaceVertexCountsAttr().Set([4])
        mesh.GetFaceVertexIndicesAttr().Set([0, 1, 2, 3])
        mesh.GetNormalsAttr().Set(Vt.Vec3fArray([Gf.Vec3f(0, 0, 1)] * 4))
        return {"prim": mesh.GetPrim()}


class CreateXformNode(UsdNodeBase):
    NODE_NAME = "Create Xform"
    CATEGORY = "Create"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Path", text="/Group")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage):
        from pxr import UsdGeom
        p = self.get_property("path") or "/Group"
        prim = UsdGeom.Xform.Define(stage, p)
        return {"prim": prim.GetPrim()}


# ────────────────────────────────────────────
#  Instancing nodes
# ────────────────────────────────────────────
class PointInstancerNode(UsdNodeBase):
    """Create a PointInstancer that scatters prototypes at given positions."""
    NODE_NAME = "Point Instancer"
    CATEGORY = "Create"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Instancer Path", text="/Instancer")
        self.add_input("prototype", color=PORT_COLORS["prim"])
        self.add_input("positions", color=PORT_COLORS["list"])
        self.add_text_input("grid", "Grid NxN (if no positions)", text="")
        self.add_text_input("spacing", "Grid Spacing", text="2.0")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage):
        from pxr import UsdGeom, Vt, Gf, Sdf
        inst_path = self.get_property("path") or "/Instancer"
        proto_prim = self._get_input_value("prototype")
        positions = self._get_input_value("positions")

        instancer = UsdGeom.PointInstancer.Define(stage, inst_path)

        # Setup prototype
        proto_container = stage.DefinePrim(f"{inst_path}/Prototypes", "Scope")
        if proto_prim and proto_prim.IsValid():
            proto_path = f"{inst_path}/Prototypes/proto_0"
            ref_prim = stage.DefinePrim(proto_path)
            ref_prim.GetReferences().AddInternalReference(proto_prim.GetPath())
        else:
            # Default: create a small cube as prototype
            proto_path = f"{inst_path}/Prototypes/proto_0"
            cube = UsdGeom.Cube.Define(stage, proto_path)
            cube.GetSizeAttr().Set(1.0)

        rel = instancer.GetPrototypesRel()
        rel.SetTargets([Sdf.Path(proto_path)])

        # Build positions
        if not positions:
            grid = (self.get_property("grid") or "").strip()
            if grid:
                n = int(grid)
                sp = float(self.get_property("spacing") or 2)
                positions = []
                off = (n - 1) * sp / 2
                for x in range(n):
                    for y in range(n):
                        positions.append((x * sp - off, y * sp - off, 0))

        if positions:
            pts = Vt.Vec3fArray([Gf.Vec3f(*p) if isinstance(p, (list, tuple))
                                 else Gf.Vec3f(p, 0, 0) for p in positions])
            instancer.GetPositionsAttr().Set(pts)
            instancer.GetProtoIndicesAttr().Set(Vt.IntArray([0] * len(pts)))
        return {"prim": instancer.GetPrim()}


class MakeInstanceableNode(UsdNodeBase):
    """Mark a prim as instanceable (native instancing)."""
    NODE_NAME = "Make Instanceable"
    CATEGORY = "Create"
    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("instanceable", "Instanceable (true/false)", text="true")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage):
        prim = self._get_input_value("prim")
        if prim:
            v = (self.get_property("instanceable") or "true").lower() in ("true", "1")
            prim.SetInstanceable(v)
        return {"prim": prim}


# ────────────────────────────────────────────
#  Lighting nodes
# ────────────────────────────────────────────
class CreateRectLightNode(UsdNodeBase):
    NODE_NAME = "Create Rect Light"
    CATEGORY = "Lighting"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Path", text="/RectLight")
        self.add_text_input("w", "Width", text="2.0")
        self.add_text_input("h", "Height", text="2.0")
        self.add_text_input("intensity", "Intensity", text="500")
        self.add_text_input("color_r", "Color R", text="1.0")
        self.add_text_input("color_g", "Color G", text="1.0")
        self.add_text_input("color_b", "Color B", text="1.0")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage):
        from pxr import UsdLux, Gf
        p = self.get_property("path") or "/RectLight"
        light = UsdLux.RectLight.Define(stage, p)
        light.GetWidthAttr().Set(float(self.get_property("w") or 2))
        light.GetHeightAttr().Set(float(self.get_property("h") or 2))
        light.GetIntensityAttr().Set(float(self.get_property("intensity") or 500))
        r = float(self.get_property("color_r") or 1)
        g = float(self.get_property("color_g") or 1)
        b = float(self.get_property("color_b") or 1)
        light.GetColorAttr().Set(Gf.Vec3f(r, g, b))
        return {"prim": light.GetPrim()}


class CreateSphereLightNode(UsdNodeBase):
    NODE_NAME = "Create Sphere Light"
    CATEGORY = "Lighting"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Path", text="/SphereLight")
        self.add_text_input("radius", "Radius", text="0.5")
        self.add_text_input("intensity", "Intensity", text="500")
        self.add_text_input("color_r", "Color R", text="1.0")
        self.add_text_input("color_g", "Color G", text="1.0")
        self.add_text_input("color_b", "Color B", text="1.0")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage):
        from pxr import UsdLux, Gf
        p = self.get_property("path") or "/SphereLight"
        light = UsdLux.SphereLight.Define(stage, p)
        light.GetRadiusAttr().Set(float(self.get_property("radius") or 0.5))
        light.GetIntensityAttr().Set(float(self.get_property("intensity") or 500))
        r = float(self.get_property("color_r") or 1)
        g = float(self.get_property("color_g") or 1)
        b = float(self.get_property("color_b") or 1)
        light.GetColorAttr().Set(Gf.Vec3f(r, g, b))
        return {"prim": light.GetPrim()}


class CreateDistantLightNode(UsdNodeBase):
    NODE_NAME = "Create Distant Light"
    CATEGORY = "Lighting"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Path", text="/DistantLight")
        self.add_text_input("intensity", "Intensity", text="500")
        self.add_text_input("angle", "Angle", text="0.53")
        self.add_text_input("color_r", "Color R", text="1.0")
        self.add_text_input("color_g", "Color G", text="1.0")
        self.add_text_input("color_b", "Color B", text="1.0")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage):
        from pxr import UsdLux, Gf
        p = self.get_property("path") or "/DistantLight"
        light = UsdLux.DistantLight.Define(stage, p)
        light.GetIntensityAttr().Set(float(self.get_property("intensity") or 500))
        light.GetAngleAttr().Set(float(self.get_property("angle") or 0.53))
        r = float(self.get_property("color_r") or 1)
        g = float(self.get_property("color_g") or 1)
        b = float(self.get_property("color_b") or 1)
        light.GetColorAttr().Set(Gf.Vec3f(r, g, b))
        return {"prim": light.GetPrim()}


class CreateDomeLightNode(UsdNodeBase):
    NODE_NAME = "Create Dome Light"
    CATEGORY = "Lighting"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Path", text="/DomeLight")
        self.add_text_input("intensity", "Intensity", text="1.0")
        self.add_text_input("texture", "Texture File (HDRI)", text="")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage):
        from pxr import UsdLux, Sdf
        p = self.get_property("path") or "/DomeLight"
        light = UsdLux.DomeLight.Define(stage, p)
        light.GetIntensityAttr().Set(float(self.get_property("intensity") or 1))
        tex = (self.get_property("texture") or "").strip()
        if tex:
            light.GetTextureFileAttr().Set(Sdf.AssetPath(tex))
        return {"prim": light.GetPrim()}


class CreateDiskLightNode(UsdNodeBase):
    NODE_NAME = "Create Disk Light"
    CATEGORY = "Lighting"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Path", text="/DiskLight")
        self.add_text_input("radius", "Radius", text="1.0")
        self.add_text_input("intensity", "Intensity", text="500")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage):
        from pxr import UsdLux
        p = self.get_property("path") or "/DiskLight"
        light = UsdLux.DiskLight.Define(stage, p)
        light.GetRadiusAttr().Set(float(self.get_property("radius") or 1))
        light.GetIntensityAttr().Set(float(self.get_property("intensity") or 500))
        return {"prim": light.GetPrim()}


# ────────────────────────────────────────────
#  Camera node
# ────────────────────────────────────────────
class CreateCameraNode(UsdNodeBase):
    NODE_NAME = "Create Camera"
    CATEGORY = "Camera"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Path", text="/Camera")
        self.add_text_input("focal_length", "Focal Length", text="50")
        self.add_text_input("h_aperture", "Horizontal Aperture", text="36")
        self.add_text_input("near_clip", "Near Clip", text="0.1")
        self.add_text_input("far_clip", "Far Clip", text="10000")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage):
        from pxr import UsdGeom
        p = self.get_property("path") or "/Camera"
        cam = UsdGeom.Camera.Define(stage, p)
        cam.GetFocalLengthAttr().Set(float(self.get_property("focal_length") or 50))
        cam.GetHorizontalApertureAttr().Set(float(self.get_property("h_aperture") or 36))
        cam.GetClippingRangeAttr().Set((
            float(self.get_property("near_clip") or 0.1),
            float(self.get_property("far_clip") or 10000)))
        return {"prim": cam.GetPrim()}


class SetActiveCameraNode(UsdNodeBase):
    """Set the active camera in usdview viewport."""
    NODE_NAME = "Set Active Camera"
    CATEGORY = "Camera"
    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("camera_path", "Camera Path (if no input)", text="")
    def execute(self, stage):
        prim = self._get_input_value("prim")
        if not prim:
            cp = (self.get_property("camera_path") or "").strip()
            if cp:
                prim = stage.GetPrimAtPath(cp)
        if prim and prim.IsValid():
            try:
                from usd_chunks import chunk_registry
                scene = chunk_registry.ChunkRegistry().scene
                ctrl = scene.usdviewAPI._UsdviewApi__appController
                ctrl._dataModel.viewSettings.cameraPrim = prim
            except Exception:
                pass
        return {}


# ────────────────────────────────────────────
#  Material query node
# ────────────────────────────────────────────
class GetPrimMaterialNode(UsdNodeBase):
    """Get the material bound to a prim, its surface shader, and all texture file paths."""
    NODE_NAME = "Get Prim Material"
    CATEGORY = "Material"
    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_output("material", color=PORT_COLORS["material"])
        self.add_output("shader", color=PORT_COLORS["prim"])
        self.add_output("textures", color=PORT_COLORS["list"])
        self.add_output("mat_path", color=PORT_COLORS["string"])
    def execute(self, stage):
        from pxr import UsdShade, Sdf
        prim = self._get_input_value("prim")
        if prim is None:
            return {"material": None, "shader": None, "textures": [], "mat_path": ""}
        # Walk up to find material binding
        binding = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
        mat = binding[0] if binding else None
        if not mat or not mat.GetPrim().IsValid():
            return {"material": None, "shader": None, "textures": [], "mat_path": ""}
        mat_prim = mat.GetPrim()
        # Find surface shader
        surf = None
        surf_out = mat.GetSurfaceOutput()
        if surf_out:
            src = surf_out.GetConnectedSources()
            if src and src[0]:
                surf = src[0][0].source.GetPrim()
        # Collect all texture file paths from child shaders
        textures = []
        for child in mat_prim.GetAllChildren():
            shader = UsdShade.Shader(child)
            if not shader:
                continue
            file_inp = shader.GetInput("file")
            if file_inp:
                val = file_inp.Get()
                if val:
                    path = val.path if hasattr(val, 'path') else str(val)
                    if path:
                        textures.append({"shader": str(child.GetPath()), "file": path})
        return {
            "material": mat_prim,
            "shader": surf,
            "textures": textures,
            "mat_path": str(mat_prim.GetPath()),
        }


# ────────────────────────────────────────────
#  Event trigger nodes
# ────────────────────────────────────────────
class _TriggerNodeBase(UsdNodeBase):
    """Base for event-driven trigger nodes. Subclasses set the event type."""
    CATEGORY = "Event"
    _active_triggers = {}  # node_id -> (cleanup_fn, refs_to_keep_alive)
    _min_interval_ms = 0  # 0 = no rate limit; subclasses override for high-frequency events

    def __init__(self):
        super().__init__()
        self.add_text_input("enabled", "Enabled (true/false)", text="true")
        self.add_output("trigger", color=PORT_COLORS["trigger"])
        self.add_output("prim", color=PORT_COLORS["prim"])
        self.add_output("event", color=PORT_COLORS["any"])

    def execute(self, stage):
        enabled = (self.get_property("enabled") or "true").lower() in ("true", "1")
        # Clean up previous listener
        nid = self.id
        if nid in _TriggerNodeBase._active_triggers:
            entry = _TriggerNodeBase._active_triggers[nid]
            if callable(entry):
                entry()
            elif isinstance(entry, tuple):
                entry[0]()
            del _TriggerNodeBase._active_triggers[nid]
        if enabled:
            result = self._subscribe(stage)
            if result:
                # Store as (cleanup_fn, [refs_to_keep_alive])
                _TriggerNodeBase._active_triggers[nid] = result
        return {"trigger": TRIGGER, "prim": None, "event": "armed" if enabled else "disabled"}

    def _subscribe(self, stage):
        """Override: subscribe to event, return cleanup function."""
        return None

    def _trigger_downstream(self):
        """Called when event fires — executes downstream connected nodes + @ref dependents."""
        if self._min_interval_ms > 0:
            from PySide6 import QtCore
            now = QtCore.QDateTime.currentMSecsSinceEpoch()
            last = getattr(self, '_last_trigger_ms', 0)
            if now - last < self._min_interval_ms:
                return
            self._last_trigger_ms = now
        graph_widget = self._get_graph_widget()
        if not graph_widget or not graph_widget._stage:
            return
        # Get wired downstream nodes
        downstream = graph_widget._get_downstream(self)
        if not downstream:
            return
        # Also find nodes that @reference any downstream node by name
        downstream_names = {n.name() for n in downstream}
        all_nodes = graph_widget._graph.all_nodes()
        for n in all_nodes:
            if n in downstream or isinstance(n, _TriggerNodeBase):
                continue
            if not hasattr(n, 'model'):
                continue
            for val in n.model.custom_properties.values():
                if isinstance(val, str) and val.startswith("@"):
                    ref_node = val[1:].split(".")[0]
                    if ref_node in downstream_names:
                        downstream.append(n)
                        break
        for node in downstream:
            graph_widget._execute_node(node)

    def _get_graph_widget(self):
        import sys
        ref = sys.modules.get('_node_graph_ref')
        return ref.widget if ref and hasattr(ref, 'widget') else None


class OnSelectionChangedNode(_TriggerNodeBase):
    """Triggers when prim selection changes in usdview. Outputs the newly selected prim."""
    NODE_NAME = "On Selection Changed"

    def __init__(self):
        super().__init__()

    def _subscribe(self, stage):
        from usd_chunks import chunk_registry
        try:
            scene = chunk_registry.ChunkRegistry().scene
            ctrl = scene.usdviewAPI._UsdviewApi__appController
            model = ctrl._dataModel

            def _on_sel_changed(*args):
                try:
                    prims = scene.get_selected_prims()
                    self._output_cache = {
                        "trigger": TRIGGER,
                        "prim": prims[0] if prims else None,
                        "event": "selection_changed",
                    }
                    QtCore.QTimer.singleShot(0, self._trigger_downstream)
                except Exception:
                    pass

            model.selection.signalPrimSelectionChanged.connect(_on_sel_changed)
            return lambda: model.selection.signalPrimSelectionChanged.disconnect(_on_sel_changed)
        except Exception:
            return None

    def execute(self, stage):
        result = super().execute(stage)
        # On first execute, output current selection
        try:
            from usd_chunks import chunk_registry
            scene = chunk_registry.ChunkRegistry().scene
            prims = scene.get_selected_prims() if scene else []
            result["prim"] = prims[0] if prims else None
        except Exception:
            pass
        return result


class OnFrameChangedNode(_TriggerNodeBase):
    """Triggers when the timeline frame changes."""
    NODE_NAME = "On Frame Changed"
    _min_interval_ms = 33  # ~30 fps cap — frame events fire on every scrub tick

    def __init__(self):
        super().__init__()
        self.add_output("frame", color=PORT_COLORS["float"])

    def _subscribe(self, stage):
        from usd_chunks import chunk_registry
        try:
            scene = chunk_registry.ChunkRegistry().scene
            ctrl = scene.usdviewAPI._UsdviewApi__appController
            model = ctrl._dataModel

            def _on_frame(*args):
                try:
                    frame = model.currentFrame.GetValue()
                    self._output_cache = {
                        "trigger": TRIGGER, "prim": None, "event": "frame_changed", "frame": frame,
                    }
                    QtCore.QTimer.singleShot(0, self._trigger_downstream)
                except Exception:
                    pass

            model.signalCurrentFrameChanged.connect(_on_frame)
            return lambda: model.signalCurrentFrameChanged.disconnect(_on_frame)
        except Exception:
            return None

    def execute(self, stage):
        result = super().execute(stage)
        result["frame"] = 0
        try:
            from usd_chunks import chunk_registry
            scene = chunk_registry.ChunkRegistry().scene
            ctrl = scene.usdviewAPI._UsdviewApi__appController
            result["frame"] = ctrl._dataModel.currentFrame.GetValue()
        except Exception:
            pass
        return result


class OnTimerNode(_TriggerNodeBase):
    """Triggers on a repeating timer interval."""
    NODE_NAME = "On Timer"

    def __init__(self):
        super().__init__()
        self.add_text_input("interval", "Interval (seconds)", text="1.0")
        self.add_output("tick", color=PORT_COLORS["float"])
        self._tick_count = 0

    def _subscribe(self, stage):
        interval = int(float(self.get_property("interval") or 1) * 1000)
        timer = QtCore.QTimer()
        timer.setInterval(max(100, interval))

        def _on_tick():
            self._tick_count += 1
            self._output_cache = {
                "trigger": TRIGGER, "prim": None, "event": "timer", "tick": self._tick_count,
            }
            self._trigger_downstream()

        timer.timeout.connect(_on_tick)
        timer.start()
        return lambda: timer.stop()

    def execute(self, stage):
        result = super().execute(stage)
        result["tick"] = self._tick_count
        return result


class OnStageChangedNode(_TriggerNodeBase):
    """Triggers when the USD stage is edited (any prim/attribute change)."""
    NODE_NAME = "On Stage Changed"

    def __init__(self):
        super().__init__()

    def _subscribe(self, stage):
        from pxr import Tf, Usd

        def _on_notice(notice, sender):
            self._output_cache = {"trigger": TRIGGER, "prim": None, "event": "stage_changed"}
            QtCore.QTimer.singleShot(50, self._trigger_downstream)

        self._notice_cb = _on_notice  # prevent GC
        self._notice_key = Tf.Notice.Register(Usd.Notice.ObjectsChanged, _on_notice, stage)
        return lambda: self._notice_key.Revoke()

    def execute(self, stage):
        return super().execute(stage)


class OnPrimChangedNode(_TriggerNodeBase):
    """Triggers when a specific prim is modified (any property/child change)."""
    NODE_NAME = "On Prim Changed"

    def __init__(self):
        super().__init__()
        self.add_text_input("prim_path", "Prim Path", text="/")

    def _subscribe(self, stage):
        from pxr import Tf, Usd
        watch_path = self.get_property("prim_path") or "/"

        def _on_notice(notice, sender):
            for p in notice.GetChangedInfoOnlyPaths():
                if str(p).startswith(watch_path):
                    prim = stage.GetPrimAtPath(watch_path)
                    self._output_cache = {
                        "trigger": TRIGGER,
                        "prim": prim if prim.IsValid() else None,
                        "event": str(p),
                    }
                    QtCore.QTimer.singleShot(50, self._trigger_downstream)
                    return
            for p in notice.GetResyncedPaths():
                if str(p).startswith(watch_path):
                    prim = stage.GetPrimAtPath(watch_path)
                    self._output_cache = {
                        "trigger": TRIGGER,
                        "prim": prim if prim.IsValid() else None,
                        "event": f"resynced:{p}",
                    }
                    QtCore.QTimer.singleShot(50, self._trigger_downstream)
                    return

        self._notice_cb = _on_notice
        self._notice_key = Tf.Notice.Register(Usd.Notice.ObjectsChanged, _on_notice, stage)
        return lambda: self._notice_key.Revoke()

    def execute(self, stage):
        result = super().execute(stage)
        path = self.get_property("prim_path") or "/"
        prim = stage.GetPrimAtPath(path)
        result["prim"] = prim if prim and prim.IsValid() else None
        return result


class OnAttributeChangedNode(_TriggerNodeBase):
    """Triggers when a specific attribute value changes."""
    NODE_NAME = "On Attribute Changed"

    def __init__(self):
        super().__init__()
        self.add_text_input("prim_path", "Prim Path", text="/")
        self.add_text_input("attr_name", "Attribute Name", text="")
        self.add_output("value", color=PORT_COLORS["any"])

    def _subscribe(self, stage):
        from pxr import Tf, Usd
        watch_path = self.get_property("prim_path") or "/"
        attr_name = (self.get_property("attr_name") or "").strip()
        if not attr_name:
            return None
        full_path = f"{watch_path}.{attr_name}"

        self._last_value = None
        # Read initial value
        prim = stage.GetPrimAtPath(watch_path)
        if prim and prim.IsValid():
            attr = prim.GetAttribute(attr_name)
            if attr and attr.IsValid():
                self._last_value = attr.Get()

        def _on_notice(notice, sender):
            for p in notice.GetChangedInfoOnlyPaths():
                if str(p) == full_path or str(p).startswith(watch_path):
                    prim = stage.GetPrimAtPath(watch_path)
                    if not prim or not prim.IsValid():
                        return
                    attr = prim.GetAttribute(attr_name)
                    if not attr or not attr.IsValid():
                        return
                    new_val = attr.Get()
                    if new_val != self._last_value:
                        self._last_value = new_val
                        self._output_cache = {
                            "trigger": TRIGGER,
                            "prim": prim, "event": f"{attr_name}_changed",
                            "value": new_val,
                        }
                        QtCore.QTimer.singleShot(50, self._trigger_downstream)
                    return

        self._notice_cb = _on_notice
        self._notice_key = Tf.Notice.Register(Usd.Notice.ObjectsChanged, _on_notice, stage)
        return lambda: self._notice_key.Revoke()

    def execute(self, stage):
        result = super().execute(stage)
        result["value"] = None
        path = self.get_property("prim_path") or "/"
        attr_name = (self.get_property("attr_name") or "").strip()
        prim = stage.GetPrimAtPath(path)
        if prim and prim.IsValid():
            result["prim"] = prim
            if attr_name:
                attr = prim.GetAttribute(attr_name)
                if attr and attr.IsValid():
                    result["value"] = attr.Get()
        return result


# ────────────────────────────────────────────
#  Registry — all nodes in one place
# ────────────────────────────────────────────
ALL_NODES = [
    # Input
    PrimPathNode,
    SelectedPrimNode,
    StringNode,
    FloatNode,
    BoolNode,
    # USD Query
    GetVariantsNode,
    SetVariantNode,
    GetPrimInfoNode,
    GetChildrenNode,
    GetAttributeNode,
    SetAttributeNode,
    FilterChildrenNode,
    SelectPrimNode,
    SetVisibilityNode,
    SetPurposeNode,
    IsVisibleNode,
    IsPrimValidNode,
    HasVariantsNode,
    HasChildrenNode,
    HasMaterialNode,
    HasAttributeNode,
    HasReferenceNode,
    FindByTypeNode,
    FindByPatternNode,
    StageInfoNode,
    GetRelationshipsNode,
    GetMetadataNode,
    SetMetadataNode,
    GetPrimLayerStackNode,
    ListPrimsNode,
    # Transform
    SetTransformNode,
    GetWorldTransformNode,
    ResetTransformNode,
    GetBoundingBoxNode,
    ConstrainTransformNode,
    # Hierarchy
    CreatePrimNode,
    DeletePrimNode,
    ReparentPrimNode,
    DuplicatePrimNode,
    RenamePrimNode,
    # Composition
    AddReferenceNode,
    AddPayloadNode,
    GetLayerStackNode,
    # Create
    CreateCubeNode,
    CreateSphereNode,
    CreateCylinderNode,
    CreateConeNode,
    CreateCapsuleNode,
    CreatePlaneNode,
    CreateXformNode,
    PointInstancerNode,
    MakeInstanceableNode,
    # Lighting
    CreateRectLightNode,
    CreateSphereLightNode,
    CreateDistantLightNode,
    CreateDomeLightNode,
    CreateDiskLightNode,
    # Camera
    CreateCameraNode,
    SetActiveCameraNode,
    # Animation
    SetKeyNode,
    GetTimeRangeNode,
    SetTimeCodeNode,
    # Material
    SetDisplayColorNode,
    AssignMaterialNode,
    CreatePreviewSurfaceNode,
    TextureFileNode,
    CreateUVTextureNode,
    CreatePrimvarReaderNode,
    GetMaterialShadersNode,
    GetShaderInputNode,
    SetShaderInputNode,
    ConnectShaderNode,
    SetUVTransformNode,
    GetPrimMaterialNode,
    # Utility
    ListIndexNode,
    ListLengthNode,
    ForEachNode,
    MergeNode,
    CompareNode,
    SwitchNode,
    ExportUsdNode,
    CounterNode,
    StringFormatNode,
    StringSplitNode,
    StringJoinNode,
    StringReplaceNode,
    BoolAndNode,
    BoolOrNode,
    BoolNotNode,
    CoalesceNode,
    # Math
    MathAddNode,
    MathSubtractNode,
    MathMultiplyNode,
    MathDivideNode,
    MathClampNode,
    MathRemapNode,
    # Code
    PythonNode,
    # Chunks
    DynamicChunkNode,
    # Debug
    PrintNode,
    # Event
    OnSelectionChangedNode,
    OnFrameChangedNode,
    OnTimerNode,
    OnStageChangedNode,
    OnPrimChangedNode,
    OnAttributeChangedNode,
]

# Category → [node_class] mapping for the tab menu
NODE_CATEGORIES = {}
for _n in ALL_NODES:
    cat = getattr(_n, "CATEGORY", "Other")
    NODE_CATEGORIES.setdefault(cat, []).append(_n)


# ────────────────────────────────────────────
#  Auto-generate chunk nodes from loaded modules
# ────────────────────────────────────────────
_SKIP_MODULES = {"node_graph_chunk", "usd_mcp_server", "kiro_chunk_web", "kiro_chunk",
                 "render_preview", "ai_chat.chunk"}

def _make_chunk_node(module_name, chunk_name):
    """Dynamically create a node class that wraps a chunk module."""

    class _AutoChunkNode(ChunkWrapperBase):
        __identifier__ = "usd_chunks"
        NODE_NAME = chunk_name
        CATEGORY = "Chunks"
        _chunk_module = module_name
        # NOTE: ``_chunk_display_name`` starts as a class-level default matching the
        # module's chunk name, but ``_ensure_chunk()`` rebinds it as an *instance*
        # attribute to the scene-assigned unique name ("Add Mesh1", "Add Mesh2", …).
        # That's what keeps multiple nodes of the same type pointing at distinct chunks.
        _chunk_display_name = chunk_name

        def _ensure_chunk(self):
            """Create (or return) this node's own private chunk instance.

            The first call creates a fresh chunk via ``chunk.create_chunks()`` which
            registers it in the scene and auto-renames to a unique name. The node's
            display name is synced to match so port/adapter lookups stay stable across
            NodeGraphQt's own rename dance (NodeGraphQt uses "Add Mesh 1" with a space
            while the scene assigns "Add Mesh1" with no space — we adopt the scene's
            name as the authoritative one).
            """
            my_chunk = getattr(self, "_my_chunk", None)
            if my_chunk is not None:
                return my_chunk
            try:
                created = chunk.create_chunks(self._chunk_module)
            except Exception as e:
                print(f"[NodeGraph] ensure_chunk err: {e}")
                created = None
            if not created:
                return None
            my_chunk = created[0]
            self._my_chunk = my_chunk
            # Rebind as instance attribute — shadows the class-level default.
            self._chunk_display_name = my_chunk.name
            try:
                # Sync NodeGraphQt node name to the chunk's scene-assigned name. This
                # keeps @refs legible ("@Add Mesh1.depth") and avoids the space/no-space
                # mismatch between NodeGraphQt ("Add Mesh 1") and the scene ("Add Mesh1").
                self.set_name(my_chunk.name)
            except Exception:
                pass
            return my_chunk

        def __init__(self):
            super().__init__()
            self.add_output("prim", color=PORT_COLORS["prim"])
            self.add_output("layer", color=PORT_COLORS["layer"])
            _needs_prim = False
            try:
                reg = chunk_registry.ChunkRegistry()
                mod = None
                for cm in reg.get_chunk_modules():
                    if cm.module_name == self._chunk_module:
                        mod = cm.module
                        break
                if mod and hasattr(mod, "main"):
                    tmp = mod.main()
                    if tmp:
                        _F = chunk_core.ChunkFlags.ParamType
                        _TYPE_COLORS = {
                            _F.FLOAT: PORT_COLORS["float"],
                            _F.INT: PORT_COLORS["float"],
                            _F.STRING: PORT_COLORS["string"],
                            _F.BOOL: PORT_COLORS["bool"],
                        }
                        def _walk_params(params):
                            for p in params:
                                if isinstance(p, chunk_core.ChunkGroupBase):
                                    _walk_params(p.get_params())
                                elif isinstance(p, chunk_core.ChunkVectorBase):
                                    for sub in p:
                                        self.add_text_input(sub.name, sub.name,
                                                            text=str(sub.value if sub.value else 0))
                                        ap = self.get_input(f"_{sub.name}")
                                        if ap:
                                            ap.color = PORT_COLORS["float"]
                                        out_name = f"_{sub.name}"
                                        existing_out = {op.name() for op in self.output_ports()}
                                        if out_name not in existing_out:
                                            self.add_output(out_name, color=PORT_COLORS["float"])
                                elif isinstance(p, Button):
                                    pass
                                elif isinstance(p, chunk_core.ChunkParamBase):
                                    self._add_chunk_port(p, _TYPE_COLORS)
                                    if hasattr(p, 'has_flag') and p.has_flag(chunk_core.ChunkFlags.ParamFlags.PRIM_SELECTION):
                                        nonlocal _needs_prim
                                        _needs_prim = True
                        _walk_params(tmp.get_params())
            except Exception:
                pass
            if _needs_prim:
                self.add_input("prim", color=PORT_COLORS["prim"])
            # Hide text widgets on node — the ChunkWidget in the side panel handles editing
            for w in self.view._widgets.values():
                w.setVisible(False)
            # Start folded — defer hiding ports until node is in the graph
            self._folded = True
            self.view._folded_state = True
            def _fold_ports():
                try:
                    for port in self.input_ports():
                        if port.name().startswith("_"):
                            port.set_visible(False)
                    for port in self.output_ports():
                        if port.name().startswith("_"):
                            port.set_visible(False)
                    self.view.draw_node()
                except Exception:
                    pass
            QtCore.QTimer.singleShot(0, _fold_ports)

        def _add_chunk_port(self, p, type_colors):
            """Add a text input + colored auto-port (in+out) for a chunk param."""
            if isinstance(p, (Button, chunk_core.ChunkGroupBase, chunk_core.ChunkVectorBase)):
                return
            # Skip names that clash with NodeGraphQt reserved properties
            _RESERVED = {"width", "height", "name", "color", "icon", "id",
                         "selected", "disabled", "visible", "pos"}
            if p.name in _RESERVED:
                return
            try:
                self.add_text_input(p.name, p.name,
                                    text=str(p.value) if p.value else "")
                port_color = PORT_COLORS.get("any", (140, 150, 170))
                pt = p.evaluate_type()
                for base_type, color in type_colors.items():
                    if pt & base_type:
                        port_color = color
                        break
                ap = self.get_input(f"_{p.name}")
                if ap:
                    ap.color = port_color
                # Add matching output port
                out_name = f"_{p.name}"
                existing_out = {op.name() for op in self.output_ports()}
                if out_name not in existing_out:
                    self.add_output(out_name, color=port_color)
            except Exception:
                pass

        def execute(self, stage):
            from .chunk_adapter import ChunkAdapter
            prim = self._get_input_value("prim")

            # Force-create a private chunk per node on first execute — otherwise every
            # node of this type would bind to the same single chunk (the first hit by
            # display name) and cross-node references would self-reference.
            my_chunk = self._ensure_chunk()
            print(f"[DBG] {self.name()} execute: _my_chunk={getattr(self,'_my_chunk',None)}, name={getattr(getattr(self,'_my_chunk',None),'name',None)}")
            if not hasattr(self, '_cached_adapter') or not self._cached_adapter.chunk:
                self._cached_adapter = ChunkAdapter(
                    self._chunk_display_name,
                    module_name=self._chunk_module,
                    chunk=my_chunk,
                )
            adapter = self._cached_adapter
            print(f"[DBG] {self.name()} adapter.chunk={getattr(adapter,'chunk',None)}, name={getattr(getattr(adapter,'chunk',None),'name',None)}")
            if not adapter.chunk:
                return {"prim": prim}

            # Now that the underlying chunk exists, arm the notice listener so
            # the node behaves like an Event trigger whenever the chunk reacts
            # via its notice-dependency graph (mirrors the chunk panel).
            self._subscribe_to_chunk_notices(adapter.chunk)

            # Guard against re-entry: adapter.set_param() below will fire
            # CHUNK_CHANGED synchronously and we must not bounce back into
            # another _trigger_downstream() from our own writes.
            self._executing = True
            try:
                # Set selection if prim provided
                old_sel = None
                if prim:
                    scene = chunk_registry.ChunkRegistry().scene
                    ctrl = scene.usdviewAPI._UsdviewApi__appController
                    old_sel = scene.get_selected_prims()
                    ctrl._dataModel.selection.setPrim(prim)

                # Only push values from connected ports — the ChunkWidget handles direct edits
                # But always push @references so chunk-core wiring activates.
                # depth and other params may live inside ChunkGroupBase containers, so we
                # must recurse rather than only iterating top-level chunk params.
                def _push_params(params):
                    for p in params:
                        if isinstance(p, chunk_core.ChunkGroupBase):
                            # Recurse — groups hold the actual leaf params (e.g. depth)
                            try:
                                _push_params(p.get_params())
                            except Exception:
                                pass
                        elif isinstance(p, chunk_core.ChunkVectorBase):
                            for sub in p:
                                v = self._get_input_value(f"_{sub.name}")
                                if v is not None:
                                    adapter.set_param(sub.name, float(v))
                        elif isinstance(p, Button):
                            pass
                        elif hasattr(p, "value"):
                            v = self._get_input_value(f"_{p.name}")
                            if v is not None:
                                adapter.set_param(p.name, v)
                            else:
                                # Push @references from node properties to the chunk param.
                                # Also sync the other direction: if chunk param already has a
                                # value_reference (set via port paste) but node property doesn't,
                                # mirror it into the node property so the side panel shows "@...".
                                existing_ref = getattr(p, "value_reference", None)
                                prop_val = self.get_property(p.name)
                                if isinstance(prop_val, str) and prop_val.startswith("@"):
                                    print(f"[DBG] {self.name()} set_param ref: {p.name} = {prop_val}")
                                    adapter.set_param(p.name, prop_val)
                                    print(f"[DBG] {self.name()} after set_param, value_reference={getattr(p,'value_reference',None)}, _ref_target={getattr(p,'_ref_target',None)}")
                                elif existing_ref and not (isinstance(prop_val, str) and prop_val.startswith("@")):
                                    try:
                                        self.set_property(p.name, existing_ref)
                                    except Exception:
                                        pass

                # Start from top-level chunk params; recurse handles nested groups
                try:
                    _push_params(adapter.chunk.get_params())
                except Exception:
                    _push_params(adapter.get_params())

                # References are pull-only: source changes don't fire notices on the
                # referencing param, so the chunk won't rebuild on its own. On Execute, walk
                # every ref'd param and fire data_changed(INPUT_CHANGED) on it. This triggers
                # _attrib_functions, which calls the lazy getter (lambda: target.value), writes
                # the pulled value to the param, and fires the normal DagNode propagation chain —
                # which causes the containing group to call send_self() (GROUP_CHANGED) and
                # trigger rebuild callbacks like cube_changed with the correctly pulled value.
                try:
                    from usd_chunks import chunk_notice as _cn
                    _ic_notice = _cn.get_notice(_cn.ChunkNoticeAlias.INPUT_CHANGED)()

                    def _fire_ref_params(container):
                        """Recursively walk params; fire data_changed(INPUT_CHANGED) on each
                        leaf with value_reference so _attrib_functions pull the current source
                        value.  After processing a group's children, call send_self() so the
                        GROUP_CHANGED notice fires and callbacks like cube_changed() run."""
                        try:
                            my_params = container.get_my_params()
                        except Exception as _e:
                            print(f"[DBG] _fire_ref_params get_my_params err: {_e}")
                            return
                        group_had_ref = False
                        for p in my_params:
                            if isinstance(p, chunk_core.ChunkGroupBase):
                                _fire_ref_params(p)
                            elif isinstance(p, chunk_core.ChunkVectorBase):
                                for sub in p:
                                    if getattr(sub, "value_reference", None):
                                        print(f"[DBG] firing INPUT_CHANGED on vector sub {sub.name}, ref={sub.value_reference}")
                                        try:
                                            sub.data_changed(_ic_notice)
                                            group_had_ref = True
                                        except Exception as _e:
                                            print(f"[DBG] data_changed err: {_e}")
                            else:
                                if getattr(p, "value_reference", None):
                                    print(f"[DBG] firing INPUT_CHANGED on {p.name}, ref={p.value_reference}, val_before={getattr(p,'value',None)}")
                                    try:
                                        p.data_changed(_ic_notice)
                                        group_had_ref = True
                                    except Exception:
                                        pass
                        # If any child in this container had a ref that was refreshed, fire
                        # GROUP_CHANGED on the container so its callbacks (e.g. cube_changed)
                        # rebuild with the newly pulled value.
                        if group_had_ref and hasattr(container, "send_self"):
                            try:
                                container.send_self()
                            except Exception:
                                pass

                    _fire_ref_params(adapter.chunk)
                except Exception as e:
                    print(f"[NodeGraph] ref refresh err: {e}")

                # Read back output prim — check chunk's layer for created prims
                out_prim = prim
                try:
                    layer = adapter.chunk.get_edit_context()
                    if layer:
                        for prim_spec in layer.rootPrims:
                            resolved = stage.GetPrimAtPath(prim_spec.path)
                            if resolved and resolved.IsValid():
                                out_prim = resolved
                                break
                except Exception:
                    pass
                # Fallback: scan params for prim paths
                if out_prim is prim:
                    def _find_output_prim(params):
                        for p in params:
                            if isinstance(p, chunk_core.ChunkGroupBase):
                                r = _find_output_prim(p.get_params())
                                if r:
                                    return r
                                continue
                            if not hasattr(p, 'value') or not p.value:
                                continue
                            sval = str(p.value)
                            if sval.startswith("/") and not sval.startswith("//"):
                                resolved = stage.GetPrimAtPath(sval)
                                if resolved and resolved.IsValid():
                                    return resolved
                        return None
                    found = _find_output_prim(adapter.chunk.get_params())
                    if found:
                        out_prim = found

                if prim and old_sel:
                    ctrl._dataModel.selection.setPrim(old_sel[0])

                # Get the chunk's edit layer
                out_layer = None
                try:
                    out_layer = adapter.chunk.get_edit_context()
                except Exception:
                    pass

                result = {"prim": out_prim, "layer": out_layer}
                # Export all chunk param values so they can be wired to other nodes
                # via auto-ports (e.g. Mesh A._dimx → Mesh B._dimx)
                try:
                    def _export_params(params):
                        for p in params:
                            if isinstance(p, chunk_core.ChunkGroupBase):
                                _export_params(p.get_params())
                            elif isinstance(p, chunk_core.ChunkVectorBase):
                                for sub in p:
                                    if hasattr(sub, 'value'):
                                        result[f"_{sub.name}"] = sub.value
                            elif hasattr(p, 'name') and hasattr(p, 'value'):
                                result[f"_{p.name}"] = p.value
                    _export_params(adapter.chunk.get_params())
                except Exception:
                    pass
                print(f"[chunk-exec] {self.name()} output keys: {[k for k in result.keys() if k.startswith('_')]}")
                return result
            finally:
                self._executing = False

    # Give unique class name for NodeGraphQt registration
    _AutoChunkNode.__name__ = f"Auto_{module_name.replace('.', '_')}"
    _AutoChunkNode.__qualname__ = _AutoChunkNode.__name__
    return _AutoChunkNode


def discover_chunk_nodes():
    """Scan loaded chunk modules and generate node classes."""
    discovered = []
    try:
        reg = chunk_registry.ChunkRegistry()
        if not reg.initialized:
            return discovered
        for cm in reg.get_chunk_modules():
            if cm.module_name in _SKIP_MODULES:
                continue
            if not cm.module or not hasattr(cm.module, "main"):
                continue
            # Skip if we already have a manual node for it
            if any(cm.module_name in getattr(n, '_chunk_module', '') for n in ALL_NODES):
                continue
            # Get chunk name from module without calling main() (avoids side effects)
            try:
                # Try to get name from already-loaded scene chunks
                scene = reg.scene
                chunk_name = None
                if scene:
                    for ch in scene.get_chunks():
                        if cm.module_name in str(getattr(ch, '_module_name', '')):
                            chunk_name = ch.name
                            break
                if not chunk_name:
                    # Use module name as fallback display name
                    chunk_name = cm.module_name.split('.')[-1].replace('_', ' ').title()
                node_cls = _make_chunk_node(cm.module_name, chunk_name)
                discovered.append(node_cls)
            except Exception:
                pass
    except Exception:
        pass
    return discovered
