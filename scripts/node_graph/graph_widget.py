"""
NodeGraph widget wrapper — embeds NodeGraphQt into a QWidget,
adds a Tab menu for spawning nodes, and an execution engine.
"""
from PySide6 import QtWidgets, QtCore, QtGui
from NodeGraphQt import NodeGraph, GroupNode
from . import nodes
from .nodes import CATEGORY_COLORS


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
    from pxr import Sdf
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


class _LineNumberArea(QtWidgets.QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self):
        return QtCore.QSize(self._editor._line_number_width(), 0)

    def paintEvent(self, event):
        self._editor._paint_line_numbers(event)


class _CodeEditor(QtWidgets.QPlainTextEdit):
    """PlainTextEdit with line numbers and autocomplete."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._line_area = _LineNumberArea(self)
        self.blockCountChanged.connect(lambda: self._update_margin())
        self.updateRequest.connect(self._update_line_area)
        self._update_margin()

        # Autocomplete
        self._completer = QtWidgets.QCompleter([
            # Variables
            "stage", "inputs", "output", "prim",
            # Common USD
            "stage.GetPrimAtPath", "stage.Traverse", "stage.DefinePrim",
            "stage.GetRootLayer", "stage.GetSessionLayer", "stage.GetLayerStack",
            "prim.GetPath", "prim.GetName", "prim.GetTypeName", "prim.GetChildren",
            "prim.GetAttribute", "prim.GetAttributes", "prim.GetReferences",
            "prim.GetVariantSets", "prim.IsValid", "prim.IsActive",
            "prim.GetParent", "prim.GetPrimAtPath",
            # UsdGeom
            "UsdGeom.Xformable", "UsdGeom.Mesh", "UsdGeom.BBoxCache",
            "UsdGeom.Imageable", "UsdGeom.Gprim",
            # UsdShade
            "UsdShade.Material", "UsdShade.Shader", "UsdShade.MaterialBindingAPI",
            # Gf/Vt
            "Gf.Vec3f", "Gf.Vec3d", "Gf.Matrix4d", "Vt.Vec3fArray",
            # Sdf
            "Sdf.Path", "Sdf.Reference", "Sdf.Payload", "Sdf.ValueTypeNames",
            # Common patterns
            "from pxr import", "from pxr import Usd, UsdGeom, Gf, Sdf",
            "for p in stage.Traverse():",
            "output = ", "inputs['in_1']", "inputs['in_2']", "inputs['in_3']",
        ])
        self._completer.setWidget(self)
        self._completer.setCompletionMode(QtWidgets.QCompleter.PopupCompletion)
        self._completer.setCaseSensitivity(QtCore.Qt.CaseInsensitive)
        self._completer.activated.connect(self._insert_completion)

    def _insert_completion(self, text):
        tc = self.textCursor()
        # Remove the partial word
        tc.movePosition(QtGui.QTextCursor.StartOfWord, QtGui.QTextCursor.KeepAnchor)
        tc.insertText(text)
        self.setTextCursor(tc)

    def keyPressEvent(self, event):
        # Let completer handle its keys
        if self._completer.popup().isVisible():
            if event.key() in (QtCore.Qt.Key_Enter, QtCore.Qt.Key_Return,
                               QtCore.Qt.Key_Escape, QtCore.Qt.Key_Tab):
                event.ignore()
                return

        # Ctrl+Space = AI suggestion
        if event.key() == QtCore.Qt.Key_Space and event.modifiers() == QtCore.Qt.ControlModifier:
            self._request_ai_suggestion()
            return

        super().keyPressEvent(event)

        # Get current word under cursor
        tc = self.textCursor()
        tc.movePosition(QtGui.QTextCursor.StartOfWord, QtGui.QTextCursor.KeepAnchor)
        prefix = tc.selectedText()

        if len(prefix) < 2:
            self._completer.popup().hide()
            return

        self._completer.setCompletionPrefix(prefix)
        cr = self.cursorRect()
        cr.setWidth(250)
        self._completer.complete(cr)

    def _request_ai_suggestion(self):
        """Ask ollama to complete code from cursor position."""
        tc = self.textCursor()
        # Get code before cursor
        pos = tc.position()
        full_text = self.toPlainText()
        code_before = full_text[:pos]
        code_after = full_text[pos:]

        inputs_desc = ""
        if hasattr(self, '_node_ref') and self._node_ref:
            node = self._node_ref
            parts = []
            for port in node.input_ports():
                connected = port.connected_ports()
                if connected:
                    src = connected[0].node()
                    parts.append(f"{port.name()} ← {src.name()} ({getattr(src, 'NODE_NAME', '?')})")
            inputs_desc = ", ".join(parts) if parts else "none"

        self._cursor_pos = pos
        self.setPlaceholderText("⏳ AI thinking...")

        from node_graph.ai_suggest import suggest_python_completion
        suggest_python_completion(code_before, code_after, inputs_desc, self._on_ai_code)

    def _on_ai_code(self, suggested_code):
        from PySide6 import QtCore
        self._pending_suggestion = suggested_code
        QtCore.QTimer.singleShot(0, self._apply_ai_code)

    def _apply_ai_code(self):
        code = getattr(self, '_pending_suggestion', '')
        self.setPlaceholderText("")
        if not code:
            return
        # Insert at the saved cursor position
        tc = self.textCursor()
        tc.setPosition(self._cursor_pos)
        tc.insertText(code)
        self.setTextCursor(tc)

    def _line_number_width(self):
        digits = max(1, len(str(self.blockCount())))
        return 8 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_margin(self):
        self.setViewportMargins(self._line_number_width(), 0, 0, 0)

    def _update_line_area(self, rect, dy):
        if dy:
            self._laddine_area.scroll(0, dy)
        else:
            self._line_area.update(0, rect.y(), self._line_area.width(), rect.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._line_area.setGeometry(cr.left(), cr.top(), self._line_number_width(), cr.height())

    def _paint_line_numbers(self, event):
        painter = QtGui.QPainter(self._line_area)
        painter.fillRect(event.rect(), QtGui.QColor("#21252b"))
        block = self.firstVisibleBlock()
        num = block.blockNumber() + 1
        top = int(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + int(self.blockBoundingRect(block).height())
        painter.setPen(QtGui.QColor("#4b5263"))
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.drawText(0, top, self._line_area.width() - 4,
                                 self.fontMetrics().height(), QtCore.Qt.AlignRight, str(num))
            block = block.next()
            top = bottom
            bottom = top + int(self.blockBoundingRect(block).height())
            num += 1
        painter.end()


class _PythonHighlighter(QtGui.QSyntaxHighlighter):
    """Minimal Python syntax highlighter."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._rules = []
        kw_fmt = QtGui.QTextCharFormat()
        kw_fmt.setForeground(QtGui.QColor("#c678dd"))
        kw_fmt.setFontWeight(QtGui.QFont.Bold)
        for w in ["def", "class", "import", "from", "return", "if", "elif",
                   "else", "for", "while", "try", "except", "with", "as",
                   "in", "not", "and", "or", "is", "None", "True", "False",
                   "lambda", "pass", "break", "continue", "raise", "yield"]:
            self._rules.append((QtCore.QRegularExpression(rf"\b{w}\b"), kw_fmt))
        str_fmt = QtGui.QTextCharFormat()
        str_fmt.setForeground(QtGui.QColor("#98c379"))
        self._rules.append((QtCore.QRegularExpression(r'"[^"]*"'), str_fmt))
        self._rules.append((QtCore.QRegularExpression(r"'[^']*'"), str_fmt))
        num_fmt = QtGui.QTextCharFormat()
        num_fmt.setForeground(QtGui.QColor("#d19a66"))
        self._rules.append((QtCore.QRegularExpression(r"\b\d+\.?\d*\b"), num_fmt))
        cmt_fmt = QtGui.QTextCharFormat()
        cmt_fmt.setForeground(QtGui.QColor("#5c6370"))
        cmt_fmt.setFontItalic(True)
        self._rules.append((QtCore.QRegularExpression(r"#.*$"), cmt_fmt))
        bi_fmt = QtGui.QTextCharFormat()
        bi_fmt.setForeground(QtGui.QColor("#61afef"))
        for b in ["print", "len", "range", "str", "int", "float", "list", "dict", "set", "type", "isinstance"]:
            self._rules.append((QtCore.QRegularExpression(rf"\b{b}\b"), bi_fmt))

    def highlightBlock(self, text):
        for pattern, fmt in self._rules:
            it = pattern.globalMatch(text)
            while it.hasNext():
                m = it.next()
                self.setFormat(m.capturedStart(), m.capturedLength(), fmt)


class _ViewportLabel(QtWidgets.QLabel):
    """QLabel that rescales its pixmap instantly on resize instead of waiting for the grab timer."""
    def __init__(self):
        super().__init__()
        self._raw_px = None
        self.setAlignment(QtCore.Qt.AlignCenter)

    def set_raw_pixmap(self, px):
        if isinstance(px, QtGui.QImage):
            px = QtGui.QPixmap.fromImage(px)
        self._raw_px = px
        self._rescale()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self):
        px = self._raw_px
        if px and not px.isNull() and self.width() > 0 and self.height() > 0:
            w = max(self.width() - 4, 100)
            h = max(self.height() - 4, 80)
            super().setPixmap(px.scaled(w, h, QtCore.Qt.KeepAspectRatio,
                                        QtCore.Qt.FastTransformation))


class NodeGraphWidget(QtWidgets.QWidget):
    """Main widget: graph canvas + toolbar."""
    _suggestions_ready = QtCore.Signal(list)

    def __init__(self, parent=None, stage=None):
        super().__init__(parent)
        self._stage = stage
        # Patch NodeGraphQt bug: set_context_menu_from_file crashes with None path
        _orig = NodeGraph.set_context_menu_from_file
        NodeGraph.set_context_menu_from_file = lambda self, f=None: None if f is None else _orig(self, f)
        self._graph = NodeGraph()
        NodeGraph.set_context_menu_from_file = _orig
        nodes.UsdNodeBase._graph_ref = self._graph

        # ── Dark neon theme ──
        self._graph.set_background_color(8, 9, 12)
        self._graph.set_grid_color(30, 36, 56)
        self._graph.set_grid_mode(1)  # dots
        self._graph.set_pipe_style(1)  # curved
        self._graph.set_pipe_slicing(True)  # Alt+click drag to cut wires
        # Override NodeGraphQt constants for neon selection colors
        from NodeGraphQt.constants import NodeEnum, PortEnum
        NodeEnum.SELECTED_COLOR._value_ = (255, 255, 255, 12)
        NodeEnum.SELECTED_BORDER_COLOR._value_ = (50, 210, 220, 255)  # cyan
        PortEnum.COLOR._value_ = (30, 80, 70, 255)
        PortEnum.BORDER_COLOR._value_ = (50, 210, 220, 255)
        PortEnum.ACTIVE_COLOR._value_ = (14, 35, 55, 255)
        PortEnum.ACTIVE_BORDER_COLOR._value_ = (100, 200, 230, 255)
        PortEnum.HOVER_COLOR._value_ = (20, 50, 80, 255)
        PortEnum.HOVER_BORDER_COLOR._value_ = (50, 230, 180, 255)

        # Monkey-patch node painting for hover glow + selection glow
        from NodeGraphQt.qgraphics.node_base import NodeItem
        from NodeGraphQt.constants import NodeEnum

        # Category accent colors (bright version for stripes/badges/borders)
        _CAT_ACCENTS = {
            "Input":       (60, 210, 120),
            "USD":         (50, 160, 230),
            "Create":      (60, 200, 100),
            "Transform":   (60, 200, 140),
            "Hierarchy":   (160, 100, 220),
            "Composition": (200, 160, 60),
            "Material":    (220, 80, 160),
            "Lighting":    (240, 200, 60),
            "Camera":      (100, 170, 220),
            "Animation":   (230, 170, 50),
            "Utility":     (140, 150, 170),
            "Math":        (50, 210, 220),
            "Code":        (140, 140, 160),
            "Chunks":      (80, 170, 210),
            "Debug":       (220, 80, 80),
            "Event":       (200, 80, 220),
        }
        # Map dark category color → (accent, category_name)
        _COLOR_TO_CAT = {}
        for cat_name, dark in CATEGORY_COLORS.items():
            _COLOR_TO_CAT[dark] = (cat_name, _CAT_ACCENTS.get(cat_name, (140, 150, 170)))

        # ── Exec-state pulse counter (shared across all nodes) ──
        import math as _math
        _pulse = [0.0]  # mutable float for animation phase

        # State color map: (R, G, B)
        _STATE_COLORS = {
            "running": (220, 170, 40),   # amber
            "valid":   (50, 200, 100),   # green
            "none":    (100, 120, 150),  # grey-blue
            "error":   (220, 60, 60),    # red
            "skipped": (70, 80, 110),    # muted grey-blue
        }

        def _neon_paint(self_node, painter, option, widget):
            painter.save()
            try:
                _neon_paint_inner(self_node, painter, option, widget)
            except Exception as _e:
                pass
            painter.restore()

        def _neon_paint_inner(self_node, painter, option, widget):
            painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(QtCore.Qt.NoBrush)

            margin = 1.0
            rect = self_node.boundingRect()
            rect = QtCore.QRectF(rect.left() + margin, rect.top() + margin,
                                 rect.width() - margin * 2, rect.height() - margin * 2)
            radius = 10.0
            is_hover = getattr(self_node, '_is_hovered', False)
            is_sel = self_node.selected
            r, g, b = self_node.color[:3]

            # Resolve category accent
            cat_name, accent = _COLOR_TO_CAT.get((r, g, b), ("", (140, 150, 170)))
            ar, ag, ab = accent

            # ── Exec state ──
            exec_state = getattr(self_node, '_exec_state', '')
            sr, sg, sb = _STATE_COLORS.get(exec_state, (0, 0, 0))
            phase = _pulse[0]

            # Dim node to 70% opacity for 'none' state
            if exec_state == 'none':
                painter.setOpacity(0.7)

            # ── Glow spill (painted, no QGraphicsEffect) ──
            if is_sel or is_hover:
                glow_alpha = 18 if is_sel else 10
                for i in range(4):
                    painter.setBrush(QtCore.Qt.NoBrush)
                    painter.setPen(QtGui.QPen(QtGui.QColor(ar, ag, ab, glow_alpha - i * 4),
                                              4 + i * 3))
                    painter.drawRoundedRect(rect.adjusted(-i*3, -i*3, i*3, i*3),
                                            radius + i*2, radius + i*2)
                painter.setPen(QtCore.Qt.NoPen)

            # ── Selection outer glow ──
            if is_sel:
                for i in range(3):
                    painter.setPen(QtGui.QPen(QtGui.QColor(ar, ag, ab, 22 - i * 6), 2.5 + i * 2))
                    painter.drawRoundedRect(rect.adjusted(-i*2, -i*2, i*2, i*2),
                                            radius + i, radius + i)
                painter.setPen(QtCore.Qt.NoPen)

            # ── State border glow (green ring for valid, red for error, amber pulse for running) ──
            if exec_state == 'valid':
                painter.setPen(QtGui.QPen(QtGui.QColor(sr, sg, sb, 50), 1.5))
                painter.setBrush(QtCore.Qt.NoBrush)
                painter.drawRoundedRect(rect.adjusted(-1, -1, 1, 1), radius, radius)
                painter.setPen(QtCore.Qt.NoPen)
            elif exec_state == 'error':
                painter.setPen(QtGui.QPen(QtGui.QColor(sr, sg, sb, 70), 1.5))
                painter.setBrush(QtCore.Qt.NoBrush)
                painter.drawRoundedRect(rect.adjusted(-1, -1, 1, 1), radius, radius)
                painter.setPen(QtCore.Qt.NoPen)
            elif exec_state == 'running':
                pulse_a = int(50 + 60 * (0.5 + 0.5 * _math.sin(phase * 5)))
                painter.setPen(QtGui.QPen(QtGui.QColor(sr, sg, sb, pulse_a), 2.5))
                painter.setBrush(QtCore.Qt.NoBrush)
                painter.drawRoundedRect(rect.adjusted(-1, -1, 1, 1), radius, radius)
                painter.setPen(QtCore.Qt.NoPen)

            # ── Background ──
            painter.setBrush(QtGui.QColor(14, 16, 22))
            painter.drawRoundedRect(rect, radius, radius)

            # ── Header gradient wash ──
            header_h = 28.0
            header_rect = QtCore.QRectF(rect.x(), rect.y(), rect.width(), header_h)
            grad = QtGui.QLinearGradient(header_rect.topLeft(), header_rect.topRight())
            grad.setColorAt(0.0, QtGui.QColor(ar, ag, ab, 25))
            grad.setColorAt(0.7, QtGui.QColor(ar, ag, ab, 5))
            grad.setColorAt(1.0, QtGui.QColor(0, 0, 0, 0))
            painter.save()
            painter.setBrush(grad)
            clip = QtGui.QPainterPath()
            clip.addRoundedRect(rect, radius, radius)
            painter.setClipPath(clip)
            painter.drawRect(header_rect)
            painter.restore()

            # ── Left edge color stripe (state-aware) ──
            stripe_margin = 10.0
            stripe_w = 4.0 if exec_state == 'running' else 2.5
            stripe_rect = QtCore.QRectF(rect.x() + 1, rect.y() + stripe_margin,
                                        stripe_w, rect.height() - stripe_margin * 2)
            if exec_state == 'running':
                pulse_a = int(140 + 115 * (0.5 + 0.5 * _math.sin(phase * 5)))
                painter.setBrush(QtGui.QColor(sr, sg, sb, pulse_a))
            elif exec_state in _STATE_COLORS:
                painter.setBrush(QtGui.QColor(sr, sg, sb, 180))
            else:
                painter.setBrush(QtGui.QColor(ar, ag, ab, 180))
            painter.drawRoundedRect(stripe_rect, 1.2, 1.2)

            # ── Badge (top-right): state pill overrides category badge ──
            badge_font = QtGui.QFont("Consolas", 7)
            badge_font.setBold(True)
            fm = QtGui.QFontMetrics(badge_font)
            if exec_state == 'valid':
                badge_text, br, bg, bb = "✓ OK", sr, sg, sb
            elif exec_state == 'none':
                badge_text, br, bg, bb = "∅ NONE", sr, sg, sb
            elif exec_state == 'error':
                badge_text, br, bg, bb = "! ERR", sr, sg, sb
            elif exec_state == 'running':
                badge_text, br, bg, bb = "⟳ RUN", sr, sg, sb
            elif exec_state == 'skipped':
                badge_text, br, bg, bb = "⊘ SKIP", sr, sg, sb
            elif cat_name:
                badge_text, br, bg, bb = cat_name.upper(), ar, ag, ab
            else:
                badge_text = None
            if badge_text:
                tw = fm.horizontalAdvance(badge_text) + 10
                badge_rect = QtCore.QRectF(rect.right() - tw - 6, rect.y() + 5, tw, 16)
                painter.setBrush(QtGui.QColor(br, bg, bb, 30))
                painter.setPen(QtGui.QPen(QtGui.QColor(br, bg, bb, 80), 0.8))
                painter.drawRoundedRect(badge_rect, 4, 4)
                painter.setFont(badge_font)
                painter.setPen(QtGui.QColor(br, bg, bb, 200))
                painter.drawText(badge_rect, QtCore.Qt.AlignCenter, badge_text)

            # ── Title text area (darken behind title) ──
            painter.setPen(QtCore.Qt.NoPen)
            padding = 3.0, 2.0
            text_rect = self_node._text_item.boundingRect()
            text_bg = QtCore.QRectF(text_rect.x() + padding[0], rect.y() + padding[1],
                                    rect.width() * 0.55,
                                    text_rect.height() - padding[1] * 2)
            painter.setBrush(QtGui.QColor(0, 0, 0, 40))
            painter.drawRoundedRect(text_bg, 3.0, 3.0)

            # ── NODE_NAME type label (bottom-left of header) ──
            type_name = getattr(self_node, '_node_type_name', None)
            if type_name:
                type_font = QtGui.QFont("Consolas", 6)
                type_font.setItalic(True)
                painter.setFont(type_font)
                painter.setPen(QtGui.QColor(ar, ag, ab, 70))
                tr = QtCore.QRectF(rect.x() + 12, rect.y() + header_h - 10,
                                   rect.width() * 0.55, 10)
                painter.drawText(tr, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, type_name)

            # ── Fold/unfold icon for chunk nodes (top-left) ──
            if hasattr(self_node, '_folded_state'):
                folded = self_node._folded_state
                ix = rect.x() + 8
                iy = rect.y() + 8
                tri = QtGui.QPainterPath()
                if folded:
                    # ▶ right-pointing triangle
                    tri.moveTo(ix, iy)
                    tri.lineTo(ix + 8, iy + 5)
                    tri.lineTo(ix, iy + 10)
                else:
                    # ▼ down-pointing triangle
                    tri.moveTo(ix, iy)
                    tri.lineTo(ix + 10, iy)
                    tri.lineTo(ix + 5, iy + 8)
                tri.closeSubpath()
                painter.setPen(QtCore.Qt.NoPen)
                painter.setBrush(QtGui.QColor(ar, ag, ab, 180 if is_hover else 120))
                painter.drawPath(tri)
                # ── Hidden-wires indicator dot (purple) ──
                try:
                    _node = self._graph.get_node_by_id(self_node.id)
                    if _node:
                        _has_hidden_wires = any(
                            not p.visible() and p.connected_ports()
                            for p in list(_node.input_ports()) + list(_node.output_ports())
                        )
                        if _has_hidden_wires:
                            painter.setBrush(QtGui.QColor(200, 80, 220, 220))
                            painter.setPen(QtCore.Qt.NoPen)
                            painter.drawEllipse(QtCore.QPointF(ix + 14, iy + 5), 3, 3)
                except Exception:
                    pass

            # ── Border ──
            if is_sel:
                border_color = QtGui.QColor(ar, ag, ab, 200)
                border_width = 1.5
            elif is_hover:
                border_color = QtGui.QColor(ar, ag, ab, 90)
                border_width = 1.0
            else:
                border_color = QtGui.QColor(ar, ag, ab, 22)
                border_width = 0.8
            pen = QtGui.QPen(border_color, border_width)
            pen.setCosmetic(self_node.viewer().get_zoom() < 0.0)
            path = QtGui.QPainterPath()
            path.addRoundedRect(rect, radius, radius)
            painter.setBrush(QtCore.Qt.NoBrush)
            painter.setPen(pen)
            painter.drawPath(path)

            # ── Running sweep bar (amber, animates across bottom) ──
            if exec_state == 'running':
                sweep = (phase * 0.8) % 1.0
                bar_w = rect.width() * 0.35
                bar_x = rect.x() + 2 + (rect.width() - 4 - bar_w) * sweep
                bar_rect = QtCore.QRectF(bar_x, rect.bottom() - 4, bar_w, 3)
                painter.setPen(QtCore.Qt.NoPen)
                bar_grad = QtGui.QLinearGradient(bar_rect.topLeft(), bar_rect.topRight())
                bar_grad.setColorAt(0.0, QtGui.QColor(sr, sg, sb, 0))
                bar_grad.setColorAt(0.3, QtGui.QColor(sr, sg, sb, 240))
                bar_grad.setColorAt(0.7, QtGui.QColor(sr, sg, sb, 240))
                bar_grad.setColorAt(1.0, QtGui.QColor(sr, sg, sb, 0))
                painter.setBrush(bar_grad)
                painter.drawRoundedRect(bar_rect, 1.5, 1.5)

            # ── Error inline message (bottom of card) ──
            if exec_state == 'error':
                err_msg = getattr(self_node, '_exec_error', '')
                if err_msg:
                    err_font = QtGui.QFont("Consolas", 6)
                    painter.setFont(err_font)
                    painter.setPen(QtGui.QColor(220, 60, 60, 180))
                    err_rect = QtCore.QRectF(rect.x() + 6, rect.bottom() - 14,
                                             rect.width() - 12, 12)
                    painter.drawText(err_rect, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                                     err_msg[:60])

        NodeItem._paint_horizontal = _neon_paint

        # ── Port data-type enforcement ──
        # Ports store _data_type on their view item. Connections between ports
        # with incompatible types are blocked here. 'any' bypasses the check.
        from NodeGraphQt.widgets.viewer import NodeViewer
        _orig_validate_accept = NodeViewer._validate_accept_connection

        def _typed_validate_accept(self_v, from_port, to_port):
            if not _orig_validate_accept(self_v, from_port, to_port):
                return False
            from_type = getattr(from_port, '_data_type', 'any')
            to_type = getattr(to_port, '_data_type', 'any')
            if from_type == 'any' or to_type == 'any':
                return True
            return from_type == to_type

        NodeViewer._validate_accept_connection = _typed_validate_accept

        # ── Pulse timer for running-state animation ──
        self._exec_pulse_timer = QtCore.QTimer(self)
        def _tick_pulse():
            _pulse[0] += 0.08
            # Only repaint nodes that are in 'running' state
            try:
                for n in self._graph.all_nodes():
                    if getattr(n.view, '_exec_state', '') == 'running':
                        n.view.update()
            except Exception:
                pass
        self._exec_pulse_timer.timeout.connect(_tick_pulse)
        self._exec_pulse_timer.start(50)

        # Enable hover events on all node items (no QGraphicsEffect — causes mouse issues)
        _orig_node_init = NodeItem.__init__
        def _patched_node_init(self_node, *args, **kwargs):
            _orig_node_init(self_node, *args, **kwargs)
            self_node.setAcceptHoverEvents(True)
            self_node._is_hovered = False
            self_node._exec_state = ''
            self_node._exec_error = ''

        def _hover_enter(self_node, event):
            if not self_node.scene():
                return
            self_node._is_hovered = True
            self_node.update()

        def _hover_leave(self_node, event):
            if not self_node.scene():
                return
            self_node._is_hovered = False
            self_node.update()

        NodeItem.__init__ = _patched_node_init
        NodeItem.hoverEnterEvent = _hover_enter
        NodeItem.hoverLeaveEvent = _hover_leave

        # ── Fold icon click handler ──
        _orig_mouse_press = NodeItem.mousePressEvent
        _graph_widget_ref = self
        def _patched_mouse_press(self_node, event):
            if hasattr(self_node, '_folded_state') and event.button() == QtCore.Qt.LeftButton:
                pos = event.pos()
                rect = self_node.boundingRect()
                # Check if click is in the fold icon area (top-left 20x20)
                if pos.x() < rect.x() + 22 and pos.y() < rect.y() + 22:
                    node = _graph_widget_ref._graph.get_node_by_id(self_node.id)
                    if node:
                        _graph_widget_ref._toggle_chunk_fold(node)
                        event.accept()
                        return
            _orig_mouse_press(self_node, event)
        NodeItem.mousePressEvent = _patched_mouse_press

        # ── Wire styling based on source node exec_state ──
        from NodeGraphQt.qgraphics.pipe import PipeItem
        _orig_pipe_paint = PipeItem.paint
        def _state_pipe_paint(self_pipe, painter, option, widget):
            # Determine source node exec_state
            src_state = ''
            try:
                src_port = getattr(self_pipe, '_output_port', None) or self_pipe.output_port
                if src_port:
                    src_view = src_port.node
                    src_state = getattr(src_view, '_exec_state', '')
            except Exception:
                pass
            if src_state in ('none', 'error'):
                painter.save()
                pen = painter.pen() if painter.pen().style() != QtCore.Qt.NoPen else QtGui.QPen()
                if src_state == 'error':
                    pen.setColor(QtGui.QColor(220, 60, 60, 160))
                else:
                    pen.setColor(QtGui.QColor(100, 120, 150, 100))
                pen.setStyle(QtCore.Qt.DashLine)
                pen.setWidthF(1.5)
                painter.setPen(pen)
                # Draw the pipe path manually
                if hasattr(self_pipe, 'path'):
                    painter.drawPath(self_pipe.path())
                painter.restore()
            else:
                _orig_pipe_paint(self_pipe, painter, option, widget)
        PipeItem.paint = _state_pipe_paint

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

        # Toolbar
        _BTN = ("QPushButton { background:#0e1016; color:#c8d0e8; border:1px solid #1e2438;"
                "border-radius:6px; padding:5px 12px; font:11px 'Consolas'; }"
                "QPushButton:hover { background:#161b26; border-color:#2a3550; }"
                "QPushButton:pressed { background:#0a0d12; }")
        _RUN = ("QPushButton { background:#e8533a; color:#fff; border:none;"
                "border-radius:6px; padding:5px 14px; font:bold 11px 'Consolas'; }"
                "QPushButton:hover { background:#f06848; }")
        toolbar = QtWidgets.QHBoxLayout()
        toolbar.setContentsMargins(6, 6, 6, 2)
        self._add_node_btn = QtWidgets.QPushButton("+ Add Node")
        self._add_node_btn.setStyleSheet(_BTN)
        self._add_node_btn.clicked.connect(self._show_add_menu)
        self._exec_btn = QtWidgets.QPushButton("▶ Execute")
        self._exec_btn.setStyleSheet(_RUN)
        self._exec_btn.clicked.connect(self.execute_graph)
        self._save_btn = QtWidgets.QPushButton("💾 Save")
        self._save_btn.setStyleSheet(_BTN)
        self._save_btn.clicked.connect(self._save_graph)
        self._load_btn = QtWidgets.QPushButton("📂 Load")
        self._load_btn.setStyleSheet(_BTN)
        self._load_btn.clicked.connect(self._load_graph)
        toolbar.addWidget(self._add_node_btn)
        toolbar.addWidget(self._exec_btn)
        toolbar.addWidget(self._save_btn)
        toolbar.addWidget(self._load_btn)
        self._export_btn = QtWidgets.QPushButton("🐍 Export")
        self._export_btn.setStyleSheet(_BTN)
        self._export_btn.clicked.connect(self._export_python)
        toolbar.addWidget(self._export_btn)
        self._viewport_btn = QtWidgets.QPushButton("👁 Viewport")
        self._viewport_btn.setStyleSheet(_BTN)
        self._viewport_btn.setCheckable(True)
        self._viewport_btn.clicked.connect(self._toggle_viewport_preview)
        toolbar.addWidget(self._viewport_btn)
        self._thumbs_btn = QtWidgets.QPushButton("📷 Thumbs")
        self._thumbs_btn.setStyleSheet(_BTN)
        self._thumbs_btn.setCheckable(True)
        self._thumbs_btn.setChecked(True)
        toolbar.addWidget(self._thumbs_btn)
        self._prim_thumbs_enabled = True
        self._thumbs_btn.clicked.connect(self._toggle_thumbs)
        self._thumb_cache = {}  # prim_path -> QPixmap
        toolbar.addStretch()
        layout.addLayout(toolbar)

        # Splitter: graph canvas (left) + properties panel (right)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.setStyleSheet(
            "QSplitter::handle { background: #1e2438; width: 2px; }"
            "QSplitter { background: #08090c; }")
        splitter.addWidget(self._graph.widget)

        # Simple properties panel
        self._props_widget = QtWidgets.QWidget()
        self._props_widget.setStyleSheet(
            "QWidget { background: #0c0e14; color: #c8d0e8; font: 10px 'Consolas'; }"
            "QLabel { color: #7a86a8; }"
            "QLineEdit { background: rgba(0,0,0,0.3); color: #c8d0e8; border: 1px solid #1e2438;"
            "  border-radius: 4px; padding: 3px 6px; font: 10px 'Consolas'; }"
            "QLineEdit:focus { border-color: #32d2dc; }"
            "QComboBox { background: rgba(0,0,0,0.3); color: #c8d0e8; border: 1px solid #1e2438;"
            "  border-radius: 4px; padding: 3px 6px; font: 10px 'Consolas'; }"
            "QAbstractItemView { background: #0e1016; color: #c8d0e8; border: 1px solid #1e2438;"
            "  selection-background-color: #1e2a40; selection-color: #c8d0e8; outline: none; }"
            "QPushButton { background: #0e1016; color: #c8d0e8; border: 1px solid #1e2438;"
            "  border-radius: 4px; padding: 3px 8px; font: 10px 'Consolas'; }"
            "QPushButton:hover { background: #161b26; border-color: #2a3550; }"
        )
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
            "QLineEdit:read-only { color: #7a86a8; }")
        self._props_layout.addWidget(self._props_label)
        self._props_type_label = QtWidgets.QLabel("")
        self._props_type_label.setStyleSheet(
            "color: #5a6688; font: italic 9px 'Consolas';"
            " padding: 0 2px 6px 2px; border-bottom: 1px solid #1e2438;")
        self._props_type_label.hide()
        self._props_layout.addWidget(self._props_type_label)

        # ── Scrollable content area ──
        self._scroll_area = QtWidgets.QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._scroll_area.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
            "QScrollBar:vertical { background: #0c0e14; width: 6px; border-radius: 3px; }"
            "QScrollBar::handle:vertical { background: #1e2438; border-radius: 3px; min-height: 20px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }")
        _scroll_content = QtWidgets.QWidget()
        _scroll_content.setStyleSheet("background: transparent;")
        _scroll_layout = QtWidgets.QVBoxLayout(_scroll_content)
        _scroll_layout.setContentsMargins(0, 6, 0, 8)
        _scroll_layout.setSpacing(0)

        # Viewport container (live screenshot mirror) — inside scroll
        self._viewport_label = _ViewportLabel()
        self._viewport_label.setStyleSheet(
            "background: #08090c; border: 1px solid #1e2438; border-radius: 6px;")
        self._viewport_label.setMinimumHeight(150)
        self._viewport_label.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
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
            "border: 1px solid #1e2438; background: #08090c; border-radius: 6px;")
        self._minimap.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._minimap.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._minimap.setInteractive(False)
        self._minimap.setRenderHint(QtGui.QPainter.Antialiasing)
        self._minimap.raise_()
        # Patch minimap viewport paintEvent for health dots
        self._minimap_orig_paint = self._minimap.viewport().paintEvent
        self._minimap.viewport().paintEvent = self._paint_minimap_health
        # Update minimap periodically
        self._minimap_timer = QtCore.QTimer(self)
        self._minimap_timer.timeout.connect(self._update_minimap)
        self._minimap_timer.start(500)

        # Wire node selection
        self._graph.node_selected.connect(self._on_node_selected)
        self._graph.node_double_clicked.connect(self._on_node_double_clicked)

        # Wire connect/disconnect -> chunk_core wire API (trigger-capable pipes)
        try:
            self._graph.port_connected.connect(self._on_ports_connected)
            self._graph.port_disconnected.connect(self._on_ports_disconnected)
        except Exception as e:
            print(f"[NodeGraph] could not attach port signals: {e}")

        # node.id → layer_identifier; populated on execution, consumed on deletion
        self._node_layer_registry: dict = {}
        try:
            self._graph.nodes_deleted.connect(self._on_nodes_deleted)
        except Exception as e:
            print(f"[NodeGraph] could not attach nodes_deleted signal: {e}")

        # Unified undo: re-execute graph after every undo/redo to restore USD scene
        try:
            self._graph.undo_stack().indexChanged.connect(self._on_undo_index_changed)
        except Exception as e:
            print(f"[NodeGraph] could not attach undo stack signal: {e}")

        # Debounce timer: property changes collapse into a single _execute_from_node
        # call 150 ms after the last change. Connection events bypass this and fire
        # immediately via _execute_from_node() directly.
        self._debounce_timer = QtCore.QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.setInterval(150)
        self._debounce_node = None
        self._debounce_timer.timeout.connect(self._flush_debounce)

        # Right-click context menu on viewer
        viewer = self._graph.viewer()
        if viewer:
            viewer.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
            viewer.customContextMenuRequested.connect(self._show_node_context_menu)
        self._prop_editors = {}  # name -> QLineEdit
        self._suggestion_id = 0  # increments on each selection to ignore stale results
        self._suggestions_ready.connect(self._show_ghost_buttons)

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
            # Enable drop on viewer
            viewer.setAcceptDrops(True)
            self._orig_apply_live = viewer.apply_live_connection
            viewer.apply_live_connection = self._patched_apply_live
            # Patch drag/drop on the viewer and its scene
            viewer._orig_dragEnter = viewer.dragEnterEvent
            viewer._orig_drop = viewer.dropEvent
            viewer.dragEnterEvent = self._viewer_drag_enter
            viewer.dropEvent = self._viewer_drop
            # Also patch the scene
            scene = viewer.scene()
            if scene:
                scene.dragEnterEvent = self._viewer_drag_enter
                scene.dragMoveEvent = self._viewer_drag_enter
                scene.dropEvent = self._scene_drop
            # Patch viewer too
            viewer.dragMoveEvent = self._viewer_drag_enter

    def _patched_apply_live(self, event):
        viewer = self._graph.viewer()
        if viewer._LIVE_PIPE.isVisible():
            # Check if dropping into empty space
            from NodeGraphQt.qgraphics.port import PortItem
            end_port = None
            for item in viewer.scene().items(event.scenePos()):
                if isinstance(item, PortItem):
                    end_port = item
                    break
            if end_port is None and viewer._start_port:
                # Save the source port info before it's cleared
                self._pending_port = viewer._start_port
                self._pending_scene_pos = event.scenePos()
                # Let original run (it will clean up the pipe)
                self._orig_apply_live(event)
                # Now show tab menu and auto-connect
                QtCore.QTimer.singleShot(0, self._connect_from_drop)
                return
        self._orig_apply_live(event)

    def _connect_from_drop(self):
        port_item = self._pending_port
        scene_pos = self._pending_scene_pos
        self._pending_port = None

        dlg = _TabSearchDialog(self, port_type=port_item.port_type)
        dlg.move(QtGui.QCursor.pos())
        if dlg.exec_() != QtWidgets.QDialog.Accepted or not dlg.selected_node:
            return

        node = self._create_node(dlg.selected_node, pos=(scene_pos.x(), scene_pos.y()))
        if not node:
            return

        # Find matching port on new node to connect
        from NodeGraphQt.constants import PortTypeEnum
        src_is_output = (port_item.port_type == PortTypeEnum.OUT.value)
        g = self._active_graph()
        src_node = g.get_node_by_id(port_item.node.id)

        if src_is_output:
            src_port = next((p for p in src_node.output_ports() if p.name() == port_item.name), None)
            if src_port:
                for p in node.input_ports():
                    try:
                        src_port.connect_to(p)
                        break
                    except Exception:
                        continue
        else:
            src_port = next((p for p in src_node.input_ports() if p.name() == port_item.name), None)
            if src_port:
                for p in node.output_ports():
                    try:
                        p.connect_to(src_port)
                        break
                    except Exception:
                        continue

    def _active_graph(self):
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
    def eventFilter(self, obj, event):
        try:
            # Wire value tooltip on hover
            if event.type() == QtCore.QEvent.GraphicsSceneMouseMove:
                try:
                    from NodeGraphQt.qgraphics.pipe import PipeItem
                    items = self._graph.viewer().scene().items(event.scenePos())
                    for item in items:
                        if isinstance(item, PipeItem):
                            port = getattr(item, '_output_port', None)
                            if port:
                                nid = port.node.id
                                src_node = self._graph.get_node_by_id(nid)
                                if src_node and hasattr(src_node, '_output_cache'):
                                    val = src_node._output_cache.get(port.name, '?')
                                    QtWidgets.QToolTip.showText(QtGui.QCursor.pos(), str(val)[:200])
                                    break
                except Exception:
                    pass
            if event.type() == QtCore.QEvent.KeyPress:
                g = self._active_graph()
                if event.key() == QtCore.Qt.Key_Tab:
                    self._show_search_menu()
                    return True
                if event.key() == QtCore.Qt.Key_Delete:
                    # Delete selected pipes first, then nodes
                    for pipe in g.selected_pipes():
                        try:
                            pipe.delete()
                        except Exception:
                            pass
                    g.delete_nodes(g.selected_nodes())
                    return True
                if event.key() == QtCore.Qt.Key_E and event.modifiers() == QtCore.Qt.ShiftModifier:
                    sel = g.selected_nodes()
                    if sel:
                        self._execute_from_node(sel[0])
                    else:
                        self.execute_graph()
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
                if event.key() == QtCore.Qt.Key_D and event.modifiers() == QtCore.Qt.ControlModifier:
                    for n in g.selected_nodes():
                        n.set_disabled(not n.disabled())
                    return True
                if event.key() == QtCore.Qt.Key_G and event.modifiers() == QtCore.Qt.ControlModifier:
                    self._group_selected()
                    return True
                if event.key() == QtCore.Qt.Key_C and event.modifiers() == QtCore.Qt.ControlModifier:
                    g.copy_nodes()
                    return True
                if event.key() == QtCore.Qt.Key_V and event.modifiers() == QtCore.Qt.ControlModifier:
                    g.paste_nodes()
                    return True
                if event.key() == QtCore.Qt.Key_X and event.modifiers() == QtCore.Qt.ControlModifier:
                    g.cut_nodes()
                    return True
                if event.key() == QtCore.Qt.Key_Z and event.modifiers() == QtCore.Qt.ControlModifier:
                    g.undo()
                    return True
                if event.key() == QtCore.Qt.Key_A and event.modifiers() == QtCore.Qt.ControlModifier:
                    g.select_all()
                    return True
            return super().eventFilter(obj, event)
        except RuntimeError:
            return False

    def _update_minimap(self):
        try:
            sr = self._graph.viewer().scene().itemsBoundingRect()
            if not sr.isNull():
                self._minimap.fitInView(sr, QtCore.Qt.KeepAspectRatio)
            # Draw health dots on minimap via overlay
            self._minimap.viewport().update()
        except Exception:
            pass

    def _paint_minimap_health(self, event):
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
                state = getattr(n.view, '_exec_state', '')
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

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Position minimap at bottom-left
        if hasattr(self, '_minimap'):
            self._minimap.move(5, self.height() - 160)

    def _show_layer_edits(self, node):
        """Show the chunk's edit layer as USD text in a popup."""
        try:
            adapter = getattr(node, '_cached_adapter', None)
            if not adapter or not adapter.chunk:
                return
            layer = adapter.chunk.get_edit_context()
            if not layer:
                QtWidgets.QMessageBox.information(self, "Layer Edits", "No layer edits found.")
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
                " font: 10px 'Consolas'; border: 1px solid #1e2438; border-radius: 6px; }")
            layout.addWidget(edit)
            dlg.exec_()
        except Exception as e:
            print(f"[NodeGraph] show layer err: {e}")

    def _toggle_chunk_fold(self, node):
        """Toggle folding on a chunk node — hide unconnected _ ports when folded."""
        folded = not getattr(node, '_folded', True)
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

    def _toggle_thumbs(self, checked):
        self._prim_thumbs_enabled = checked
        for n in self._graph.all_nodes():
            if isinstance(n, nodes.PrimPathNode) and hasattr(n, '_prim_thumb'):
                if checked:
                    if n._prim_thumb._pixmap:
                        n._prim_thumb._widget.show()
                else:
                    n._prim_thumb._widget.hide()
                n.view.draw_node()

    def _toggle_viewport_preview(self, checked):
        if checked:
            self._viewport_label.show()
            self._grab_viewport()
            self._viewport_timer.start(100)
        else:
            self._viewport_timer.stop()
            self._viewport_label.hide()

    def _grab_viewport(self):
        from .graph_host import GraphHost
        pixmap = GraphHost.current().get_viewport_screenshot()
        if pixmap and not pixmap.isNull():
            self._viewport_label.set_raw_pixmap(pixmap)
        else:
            self._viewport_label.setText("No viewport")

    def _show_port_context_menu(self, graph, viewer, port_item, pos):
        """Port-right-click menu. Copy a reference from an output port, paste / clear on an input.

        The port menu is the reliable entry point for references because it doesn't fight with
        the chunk-widget context menus in the side panel, and it works on every port (even those
        where the param widget has no right-click surface).
        """
        from NodeGraphQt.constants import PortTypeEnum
        node = graph.get_node_by_id(port_item.node.id)
        port_name = port_item.name
        # Chunk-auto ports are "_param"; strip underscore for the user-facing name and to look
        # up the chunk param. Non-underscore ports (prim, layer, custom) are left as-is.
        param_name = port_name[1:] if port_name.startswith("_") else port_name
        is_output = port_item.port_type == PortTypeEnum.OUT.value

        menu = QtWidgets.QMenu(self)
        menu.setTitle(f"{node.name()}.{param_name}")
        header = menu.addAction(f"🔌 {node.name()}.{param_name}")
        header.setEnabled(False)
        menu.addSeparator()

        copy_act = paste_act = clear_act = None
        if is_output:
            copy_act = menu.addAction("📋 Copy as Reference")
        else:
            paste_act = menu.addAction("📌 Paste Reference")
            paste_act.setEnabled(NodeGraphWidget._ref_clipboard is not None)
            # Show existing reference on the menu header so the user can see it's wired.
            adapter, chunk_param = self._resolve_chunk_param_for_port(node, node.get_input(port_name))
            existing_ref = None
            if chunk_param is not None:
                existing_ref = getattr(chunk_param, "value_reference", None)
            if not existing_ref:
                # Fall back to the node property (reference may have been typed rather than pasted)
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
            # Prefer chunk-aware dotted path when the port backs a chunk param.
            _, chunk_param = self._resolve_chunk_param_for_port(node, node.get_output(port_name))
            if chunk_param is not None:
                NodeGraphWidget._ref_clipboard = self._build_chunk_param_ref(node, chunk_param)
            else:
                # Pure node-graph node — use @NodeName.portName, node-graph fallback resolves it.
                NodeGraphWidget._ref_clipboard = f"@{node.name()}.{param_name}"
            print(f"[NodeGraph] copied ref: {NodeGraphWidget._ref_clipboard}")
            return

        if action == paste_act and NodeGraphWidget._ref_clipboard:
            ref = NodeGraphWidget._ref_clipboard
            adapter, chunk_param = self._resolve_chunk_param_for_port(node, node.get_input(port_name))
            try:
                if chunk_param is not None and hasattr(chunk_param, "value_reference"):
                    # Bypass adapter.set_param — its lookup is leaf-only and misses nested params.
                    # Setting value_reference directly fires ATTRIBUTE_CHANGED and _rewire runs.
                    chunk_param.value_reference = ref
                else:
                    node.set_property(param_name, ref)
                # Mirror into the node property so the side-panel text widget shows the ref.
                try:
                    node.set_property(param_name, ref)
                except Exception:
                    pass
                print(f"[NodeGraph] pasted ref {ref} -> {node.name()}.{param_name}")
            except Exception as e:
                print(f"[NodeGraph] paste ref err: {e}")
            # Refresh the side panel so the new ref is visible in any text widget.
            try:
                if self._selected_node is node:
                    self._on_node_selected(node)
            except Exception:
                pass
            return

        if action == clear_act:
            _, chunk_param = self._resolve_chunk_param_for_port(node, node.get_input(port_name))
            if chunk_param is not None:
                try:
                    chunk_param.value_reference = None
                except Exception as e:
                    print(f"[NodeGraph] clear ref err: {e}")
            # Also clear the node property so it no longer shows "@..." in the side panel.
            try:
                node.set_property(param_name, "")
            except Exception:
                pass
            try:
                if self._selected_node is node:
                    self._on_node_selected(node)
            except Exception:
                pass

    def _show_node_context_menu(self, pos):
        # Find node / port under cursor.
        g = self._active_graph()
        viewer = g.viewer()
        scene_pos = viewer.mapToScene(pos)
        items = viewer.scene().items(scene_pos)
        from NodeGraphQt.qgraphics.node_base import NodeItem
        from NodeGraphQt.qgraphics.port import PortItem
        # Port menu has priority — if the user right-clicked a port, show port-specific
        # Copy/Paste Reference options instead of the generic node menu. This works on any
        # node type, so it's the reliable way to set up references on chunk nodes.
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

        # Check if we're inside a sub-graph
        from NodeGraphQt.base.graph import SubGraph
        is_sub = isinstance(g, SubGraph)

        menu = QtWidgets.QMenu(self)

        # Sub-graph: add port creation options
        act_add_in = act_add_out = None
        if is_sub:
            act_add_in = menu.addAction("📥 Add Input Port")
            act_add_out = menu.addAction("📤 Add Output Port")
            if selected:
                menu.addSeparator()

        # Node-specific actions
        act_exec = act_disable = act_dup = act_del = act_color = act_frame = act_fold = act_layer = None
        if selected:
            node = selected[0]
            act_exec = menu.addAction("▶ Execute from here")
            act_disable = menu.addAction("🚫 Disable" if not node.disabled() else "✅ Enable")
            if hasattr(node, '_chunk_module'):
                is_folded = getattr(node, '_folded', True)
                act_fold = menu.addAction("📂 Expand Ports" if is_folded else "📁 Collapse Ports")
                act_layer = menu.addAction("📄 Show Layer Edits")
            act_dup = menu.addAction("📋 Duplicate")
            act_del = menu.addAction("🗑 Delete")
            act_color = menu.addAction("🎨 Change Color")
            if isinstance(node, nodes.PrimPathNode):
                act_frame = menu.addAction("🎯 Frame in Viewport")
            menu.addSeparator()
            # Add label for listing node references (if any)
            adapter = getattr(node, '_cached_adapter', None)
            if adapter and hasattr(adapter, 'chunk') and adapter.chunk:
                print(dir(adapter.chunk))
                print(adapter.chunk.get_my_params())
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
                self._execute_from_node(node)
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
            elif action == act_fold and hasattr(node, '_chunk_module'):
                self._toggle_chunk_fold(node)
            elif action == act_layer and hasattr(node, '_chunk_module'):
                self._show_layer_edits(node)

    def _set_exec_state(self, node, state, error_msg=""):
        """Set persistent execution state on a node view item.
        States: 'running', 'valid', 'none', 'error', '' (reset).
        Valid/none auto-clear after 1.5s so the category badge returns."""
        try:
            view = node.view
            view._exec_state = state
            view._exec_error = error_msg
            if state == 'error' and error_msg:
                view.setToolTip(error_msg)
            elif state == 'none':
                view.setToolTip("No output")
            elif state == 'skipped':
                view.setToolTip("Skipped — upstream error")
            else:
                view.setToolTip("")
            view.update()
            if state in ('valid', 'none'):
                QtCore.QTimer.singleShot(1500, lambda v=view: (
                    setattr(v, '_exec_state', ''),
                    v.update()
                ) if getattr(v, '_exec_state', '') == state else None)
        except Exception:
            pass

    def _execute_node(self, node):
        """Execute a single node — handles both UsdNodeBase and GroupNode."""
        if isinstance(node, GroupNode):
            self._execute_group(node)
            return
        if not isinstance(node, nodes.UsdNodeBase):
            return
        if node.disabled():
            node._output_cache = {}
            for out_port in node.output_ports():
                in_port = next((p for p in node.input_ports() if p.name() == out_port.name()), None)
                if in_port and in_port.connected_ports():
                    src_port = in_port.connected_ports()[0]
                    src_node = src_port.node()
                    if hasattr(src_node, "_output_cache"):
                        node._output_cache[out_port.name()] = src_node._output_cache.get(src_port.name())
                if out_port.name() not in node._output_cache and node.input_ports():
                    first_in = node.input_ports()[0]
                    if first_in.connected_ports():
                        src_port = first_in.connected_ports()[0]
                        src_node = src_port.node()
                        if hasattr(src_node, "_output_cache"):
                            node._output_cache[out_port.name()] = src_node._output_cache.get(src_port.name())
            return
        self._set_exec_state(node, "running")
        try:
            result = node.execute(self._stage)
            node._output_cache = result or {}
            vals = list((result or {}).values())
            all_null = vals and all(v is None for v in vals)
            self._set_exec_state(node, "none" if all_null else "valid")
            # Track chunk layer so we can remove it from the stage when this node is deleted.
            # Prefer _my_chunk (per-instance chunk) over the result dict "layer" output.
            try:
                chunk = getattr(node, '_my_chunk', None)
                if chunk is not None:
                    layer = chunk.get_edit_context()
                    if layer:
                        self._node_layer_registry[node.id] = layer.identifier
                elif result and result.get("layer") is not None:
                    self._node_layer_registry[node.id] = result["layer"].identifier
            except Exception:
                pass
            # Capture thumbnail for PrimPathNode (only on first execution)
            if (isinstance(node, nodes.PrimPathNode) and self._prim_thumbs_enabled
                    and result and result.get("prim")
                    and not node._prim_thumb._pixmap):
                self._capture_prim_thumb(node, result["prim"])
            return True
        except Exception as e:
            print(f"[NodeGraph] {node.name()} error: {e}")
            node._output_cache = {}
            self._set_exec_state(node, "error", str(e)[:120])
            return False

    def _capture_prim_thumb(self, node, prim):
        """Frame prim, grab viewport, show thumbnail on node widget."""
        prim_path = str(prim.GetPath())
        if prim_path in self._thumb_cache:
            node._prim_thumb.set_pixmap(self._thumb_cache[prim_path])
            node._prim_thumb._widget.show()
            node.view.draw_node()
            return
        try:
            from .graph_host import GraphHost

            def _on_ready(px):
                if px is not None and not px.isNull():
                    self._thumb_cache[prim_path] = px
                    node._prim_thumb.set_pixmap(px)
                    node._prim_thumb._widget.show()
                    node.view.draw_node()

            GraphHost.current().frame_prim(prim, on_ready=_on_ready)
        except Exception as e:
            print(f"[NodeGraph] thumb err: {e}")

    def _execute_group(self, group):
        """Execute a GroupNode by running its inner sub-graph."""
        from NodeGraphQt.nodes.port_node import PortInputNode, PortOutputNode
        # Temporarily expand to get the sub-graph
        sub = self._graph.expand_group_node(group)
        if not sub:
            return

        # Feed group input values into PortInputNodes
        for pin in sub.get_input_port_nodes():
            parent_port = pin.parent_port
            if parent_port:
                connected = parent_port.connected_ports()
                if connected:
                    src_port = connected[0]
                    src_node = src_port.node()
                    val = src_node._output_cache.get(src_port.name()) if hasattr(src_node, "_output_cache") else None
                    pin._output_cache = {pin.output_ports()[0].name(): val} if pin.output_ports() else {}

        # Topo-sort inner nodes
        visited = set()
        order = []
        def _topo(n):
            nid = n.id
            if nid in visited:
                return
            visited.add(nid)
            for p in n.input_ports():
                for cp in p.connected_ports():
                    _topo(cp.node())
            order.append(n)
        for n in sub.all_nodes():
            _topo(n)

        # Execute inner nodes (skip port nodes)
        for n in order:
            if isinstance(n, (PortInputNode, PortOutputNode)):
                continue
            self._execute_node(n)

        # Collect outputs from PortOutputNodes → group._output_cache
        group._output_cache = {}
        for pout in sub.get_output_port_nodes():
            parent_port = pout.parent_port
            if parent_port and pout.input_ports():
                in_p = pout.input_ports()[0]
                connected = in_p.connected_ports()
                if connected:
                    src_port = connected[0]
                    src_node = src_port.node()
                    val = src_node._output_cache.get(src_port.name()) if hasattr(src_node, "_output_cache") else None
                    group._output_cache[parent_port.name()] = val

        # Reset inner node colors before collapse so they don't get baked
        for n in sub.all_nodes():
            if isinstance(n, nodes.UsdNodeBase):
                n.set_color(*CATEGORY_COLORS.get(n.CATEGORY, (80, 80, 80)))

        # Collapse back
        self._graph.collapse_group_node(group)
        self._set_exec_state(group, "valid")

    def _schedule_execute(self, node=None):
        """Debounced re-execution: starts/resets the 150ms timer for a property change."""
        self._debounce_node = node
        self._debounce_timer.start()  # restart resets the interval

    def _flush_debounce(self):
        node = self._debounce_node
        self._debounce_node = None
        if node is not None:
            self._execute_from_node(node)
        else:
            self.execute_graph()

    def _execute_from_node(self, start_node):
        """Execute only the start_node and its downstream nodes."""
        if not self._stage:
            return
        # Collect downstream nodes
        visited = set()
        order = []
        def _downstream(node):
            if not hasattr(node, 'output_ports'):
                return
            nid = node.id
            if nid in visited:
                return
            visited.add(nid)
            order.append(node)
            for port in node.output_ports():
                for cp in port.connected_ports():
                    _downstream(cp.node())
        # First execute upstream (to populate caches)
        up_visited = set()
        up_order = []
        def _upstream(node):
            if not hasattr(node, 'input_ports'):
                return
            nid = node.id
            if nid in up_visited:
                return
            up_visited.add(nid)
            for port in node.input_ports():
                for cp in port.connected_ports():
                    _upstream(cp.node())
            up_order.append(node)
        _upstream(start_node)
        _downstream(start_node)
        full_order = up_order + [n for n in order if n.id not in up_visited]
        errored = set()
        for node in full_order:
            upstream_errored = any(
                cp.node().id in errored
                for port in (node.input_ports() if hasattr(node, 'input_ports') else [])
                for cp in port.connected_ports()
            )
            if upstream_errored:
                self._set_exec_state(node, "skipped")
                errored.add(node.id)
                node._output_cache = {}
                continue
            if self._execute_node(node) is False:
                errored.add(node.id)

    def _setup_sub_graph(self, sub):
        """Install eventFilter and context menu on a sub-graph viewer."""
        viewer = sub.viewer()
        if not viewer:
            return
        # Apply same dark neon theme
        sub.set_background_color(8, 9, 12)
        sub.set_grid_color(30, 36, 56)
        sub.set_grid_mode(1)  # dots
        viewer.installEventFilter(self)
        viewer.scene().installEventFilter(self)
        viewer.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        viewer.customContextMenuRequested.connect(self._show_node_context_menu)
        sub.node_selected.connect(self._on_node_selected)
        sub.node_double_clicked.connect(self._on_node_double_clicked)
        # Wire-drop-to-search: patch apply_live_connection on sub-graph viewer
        orig = viewer.apply_live_connection
        def _patched(event, _orig=orig, _viewer=viewer):
            if _viewer._LIVE_PIPE.isVisible():
                from NodeGraphQt.qgraphics.port import PortItem
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

    # ------------------------------------------------------------------
    # Wire handlers — bridge node-graph pipes to the chunk_core notice DAG.
    # A pipe between two chunk-backed ports becomes an add_dependency + pull
    # function on the downstream chunk param, so live updates propagate via
    # notices (the push-with-trigger semantic). Pipes between non-chunk
    # nodes (Event, Python, etc.) are left to the existing execute()-time
    # data shuttle — they don't have chunk params to bind to.
    # ------------------------------------------------------------------
    def _resolve_chunk_param_for_port(self, node, port):
        """Return (adapter, chunk_param) for a port on a chunk-backed node, or (None, None).

        Chunk-backed nodes name their ports ``_<leaf_param_name>`` even when the param lives
        inside a group (e.g. ``cube.depth`` is exposed as port ``_depth``). So we can't just
        call ``chunk.get_param("depth")`` — that misses nested params. Walk the chunk recursively
        and match by leaf name.
        """
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
            from .chunk_adapter import ChunkAdapter
            from usd_chunks import chunk_core
            adapter = getattr(node, "_cached_adapter", None)
            if adapter is None or not getattr(adapter, "chunk", None):
                # Prefer the node's own per-instance chunk (ensures unique binding
                # across multiple nodes of the same chunk type).
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

            # First try direct lookup (flat chunks / dotted names).
            p = adapter.chunk.get_param(leaf_name)
            if isinstance(p, chunk_core.ChunkParamBase):
                return adapter, p

            # Recursive search by leaf name through groups + vectors.
            def _walk(container):
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

    def _on_ports_connected(self, in_port, out_port):
        """Called by NodeGraphQt when a pipe is created. Signature order is (in, out)."""
        # Show trigger input port when a trigger wire lands on it
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
        # Immediate execution from the upstream node on new connection
        try:
            self._execute_from_node(out_port.node())
        except Exception:
            pass
        # Refresh side panel to grey out params now driven by this wire
        QtCore.QTimer.singleShot(0, lambda: self._refresh_panel_if_selected(in_port.node()))

    def _on_ports_disconnected(self, in_port, out_port):
        # Hide trigger input when its last wire is removed
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
        # Immediate re-execution from the now-disconnected downstream node
        try:
            self._execute_from_node(in_port.node())
        except Exception:
            pass
        # Refresh side panel to re-enable params no longer driven by a wire
        QtCore.QTimer.singleShot(0, lambda: self._refresh_panel_if_selected(in_port.node()))

    def _refresh_panel_if_selected(self, node):
        """Rebuild the side panel when *node* is currently displayed.

        Called after wire connect/disconnect so param lock state stays in sync.
        """
        if node is getattr(self, '_selected_node', None):
            self._on_node_selected(node)

    def _on_nodes_deleted(self, node_list):
        """Remove each deleted chunk node's Sdf.Layer from the stage layer stack."""
        from pxr import Sdf
        for node in node_list:
            layer_id = self._node_layer_registry.pop(node.id, None)
            if not layer_id:
                # Node was never executed — try reading the layer directly from chunk
                try:
                    chunk = getattr(node, '_my_chunk', None)
                    if chunk is not None:
                        lyr = chunk.get_edit_context()
                        if lyr:
                            layer_id = lyr.identifier
                except Exception:
                    pass
            if not layer_id or not self._stage:
                continue
            layer = Sdf.Layer.Find(layer_id)
            if not layer:
                continue
            try:
                _remove_layer_from_hierarchy(self._stage.GetRootLayer(), layer)
            except Exception as e:
                print(f"[NodeGraph] layer cleanup err for {node.name()}: {e}")

    def _on_undo_index_changed(self, index):
        """Re-execute the full graph after any undo or redo to restore USD scene state."""
        QtCore.QTimer.singleShot(0, self.execute_graph)

    def _on_node_double_clicked(self, node):
        if isinstance(node, GroupNode):
            sub = self._graph.expand_group_node(node)
            if sub:
                self._setup_sub_graph(sub)
        elif isinstance(node, nodes.PrimPathNode):
            pp = node.get_property("prim_path")
            if pp:
                try:
                    from .graph_host import GraphHost
                    host = GraphHost.current()
                    stage = host.get_stage()
                    if stage:
                        prim = stage.GetPrimAtPath(pp)
                        if prim.IsValid():
                            host.set_selection(prim)
                            host.update_viewport()
                except Exception:
                    pass

    def _rename_selected_node(self, node=None):
        if node is None:
            node = getattr(self, '_selected_node', None)
        if not node:
            return
        new_name, ok = QtWidgets.QInputDialog.getText(
            self, "Rename Node", "Name:", text=node.name())
        if ok and new_name.strip():
            new_name = new_name.strip()
            node.set_name(new_name)
            self._props_label.setText(new_name)
            type_name = getattr(node, 'NODE_NAME', '')
            if type_name and type_name != new_name:
                self._props_type_label.setText(type_name)
                self._props_type_label.show()
            else:
                self._props_type_label.hide()

    def _on_node_selected(self, node):
        # Clear old form
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
            return

        type_name = getattr(node, 'NODE_NAME', '')
        self._props_label.setReadOnly(False)
        self._props_label.setText(node.name())
        if type_name and type_name != node.name():
            self._props_type_label.setText(type_name)
            self._props_type_label.show()
        else:
            self._props_type_label.hide()
        self._selected_node = node
        self._node_name_edit = self._props_label

        try:
            self._props_label.editingFinished.disconnect()
        except RuntimeError:
            pass
        def _do_rename(_node=node):
            new_name = self._props_label.text().strip()
            if not new_name:
                self._props_label.setText(_node.name())
                return
            if new_name != _node.name():
                _node.set_name(new_name)
        self._props_label.editingFinished.connect(_do_rename)

        # Fire AI suggestion only for nodes with outputs
        if hasattr(node, 'output_ports') and node.output_ports():
            self._suggestion_id += 1
            sid = self._suggestion_id
            try:
                from node_graph.ai_suggest import suggest_next_nodes, get_graph_context
                ctx = get_graph_context(self._graph)
                suggest_next_nodes(ctx, lambda names, _sid=sid: self._on_ghost_suggestions(names, _sid))
            except Exception:
                pass

        # Build editable fields for custom properties
        # For chunk nodes: use the real chunk widget factory
        if hasattr(node, '_chunk_module'):
            self._build_chunk_panel(node)
            return
        skip = {"id", "name", "color", "disabled", "selected", "width", "height",
                "icon", "pos", "layout_direction", "type_", "thumb"}
        for name, val in node.model.custom_properties.items():
            if name in skip:
                continue
            if name == "code":
                code_widget = QtWidgets.QWidget()
                code_layout = QtWidgets.QVBoxLayout(code_widget)
                code_layout.setContentsMargins(0, 0, 0, 0)
                edit = _CodeEditor()
                edit._node_ref = node
                edit.setPlainText(str(val) if val else "")
                edit.setMinimumHeight(120)
                edit.setFont(QtGui.QFont("Consolas", 9))
                edit.setStyleSheet(
                    "QPlainTextEdit { background: #282c34; color: #abb2bf;"
                    " border: 1px solid #3e4451; border-radius: 4px; padding: 4px; }"
                )
                _PythonHighlighter(edit.document())
                edit.setProperty("prop_name", name)
                edit.textChanged.connect(lambda e=edit: self._on_code_changed(e))
                code_layout.addWidget(edit)
                btn_row = QtWidgets.QHBoxLayout()
                save_snip = QtWidgets.QPushButton("💾 Save Snippet")
                load_snip = QtWidgets.QPushButton("📂 Load Snippet")
                def _save_snippet(e=edit):
                    p, _ = QtWidgets.QFileDialog.getSaveFileName(
                        self, "Save Snippet", "", "Python (*.py)")
                    if p:
                        with open(p, "w", encoding="utf-8") as f:
                            f.write(e.toPlainText())
                def _load_snippet(e=edit):
                    p, _ = QtWidgets.QFileDialog.getOpenFileName(
                        self, "Load Snippet", "", "Python (*.py)")
                    if p:
                        with open(p, encoding="utf-8") as f:
                            e.setPlainText(f.read())
                save_snip.clicked.connect(_save_snippet)
                load_snip.clicked.connect(_load_snippet)
                btn_row.addWidget(save_snip)
                btn_row.addWidget(load_snip)
                code_layout.addLayout(btn_row)
                self._props_form.addRow(name, code_widget)
                self._prop_editors[name] = edit
                continue
            elif isinstance(val, bool):
                cb = QtWidgets.QCheckBox()
                cb.setChecked(val)
                cb.setProperty("prop_name", name)
                cb.toggled.connect(lambda state, n=name: self._on_bool_prop_changed(n, state))
                self._props_form.addRow(name, cb)
                self._prop_editors[name] = cb
                continue
            elif name == "file_path" and isinstance(node, nodes.TextureFileNode):
                # Texture file with browse button and thumbnail
                row = QtWidgets.QWidget()
                row_layout = QtWidgets.QVBoxLayout(row)
                row_layout.setContentsMargins(0, 0, 0, 0)
                file_row = QtWidgets.QHBoxLayout()
                edit = QtWidgets.QLineEdit(str(val) if val else "")
                edit.setProperty("prop_name", name)
                edit.editingFinished.connect(self._on_prop_changed)
                self._setup_ref_menu(edit, node, name)
                browse = QtWidgets.QPushButton("📁")
                browse.setFixedWidth(30)
                thumb = QtWidgets.QLabel()
                thumb.setFixedSize(200, 200)
                thumb.setStyleSheet("background: #1a1a1a; border: 1px solid #333;")
                thumb.setAlignment(QtCore.Qt.AlignCenter)

                def _make_browse(ed, th, nd):
                    def _fn():
                        path, _ = QtWidgets.QFileDialog.getOpenFileName(
                            self, "Select Texture", "", "Images (*.png *.jpg *.jpeg *.exr *.tga *.bmp)")
                        if path:
                            ed.setText(path)
                            ed.editingFinished.emit()
                            self._show_thumbnail(th, path)
                            if hasattr(nd, '_thumb'):
                                nd._thumb.set_value(path)
                    return _fn

                browse.clicked.connect(_make_browse(edit, thumb, node))
                file_row.addWidget(edit)
                file_row.addWidget(browse)
                row_layout.addLayout(file_row)
                row_layout.addWidget(thumb)
                if val:
                    self._show_thumbnail(thumb, str(val))
                self._props_form.addRow(name, row)
                self._prop_editors[name] = edit
                continue
            else:
                edit = QtWidgets.QLineEdit(str(val) if val else "")
                edit.setProperty("prop_name", name)
                edit.editingFinished.connect(self._on_prop_changed)
                self._setup_ref_menu(edit, node, name)
                # Show driven value if auto-port is connected (skip trigger signals)
                auto_port = node.get_input(f"_{name}")
                if auto_port and auto_port.connected_ports():
                    src_port = auto_port.connected_ports()[0]
                    src_node = src_port.node()
                    driven_val = src_node._output_cache.get(src_port.name()) if hasattr(src_node, "_output_cache") else None
                    if driven_val is not None and not isinstance(driven_val, nodes._TriggerSignal):
                        edit.setText(str(driven_val))
                        edit.setReadOnly(True)
                        edit.setStyleSheet(
                            "QLineEdit { background: rgba(50,210,220,0.08); color: #32d2dc;"
                            " border: 1px solid rgba(50,210,220,0.3); border-radius: 4px;"
                            " padding: 3px 6px; font: 10px 'Consolas'; }")
                        name = f"⚡ {name}"
                # Add autocomplete for attribute-related fields
                if name == "attr_name":
                    # Combo box listing connected prim's attributes
                    combo = QtWidgets.QComboBox()
                    combo.setEditable(True)
                    attrs = ["name", "path", "type"] + self._get_prim_attrs(node)
                    combo.addItems(attrs)
                    if val:
                        combo.setCurrentText(str(val))
                    combo.setProperty("prop_name", name)
                    combo.currentTextChanged.connect(
                        lambda text, n=name: self._on_combo_changed(n, text))
                    self._props_form.addRow(name, combo)
                    self._prop_editors[name] = combo
                    continue
                elif name in ("prim_path", "set_name", "type_filter",
                              "prim_type", "purpose", "material_path"):
                    self._add_completer(edit, name, node)
                edit.setPlaceholderText("Type to search...")
            self._props_form.addRow(name, edit)
            self._prop_editors[name] = edit

    def _on_ghost_suggestions(self, suggested_names, sid):
        """Called from AI thread — emit signal to main thread."""
        if sid == self._suggestion_id and suggested_names:
            self._suggestions_ready.emit(suggested_names)

    def _show_ghost_buttons(self, names):
        import sys
        print(f"[ghost] showing buttons: {names}", file=sys.stderr)
        if not names or not hasattr(self, '_selected_node'):
            return
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

    def _on_ghost_clicked(self):
        name = self.sender().property("node_name")
        # Find node class by name
        all_nodes = list(nodes.ALL_NODES) + getattr(self, '_discovered_chunks', [])
        cls = next((n for n in all_nodes if n.NODE_NAME == name), None)
        if cls and hasattr(self, '_selected_node'):
            pos = self._selected_node.pos()
            node = self._create_node(cls, pos=(pos[0] + 250, pos[1]))
            # Auto-connect first matching output→input
            if node:
                for out_port in self._selected_node.output_ports():
                    for in_port in node.input_ports():
                        try:
                            out_port.connect_to(in_port)
                            return
                        except Exception:
                            continue

    def _save_graph(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save Graph", "", "Node Graph (*.json)")
        if path:
            self._graph.save_session(path)

    def _load_graph(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Load Graph", "", "Node Graph (*.json)")
        if path:
            self._graph.load_session(path)
            # Restore USD scene — chunk layers are empty until executed
            QtCore.QTimer.singleShot(0, self.execute_graph)

    def _export_python(self):
        """Export the graph as a standalone Python script."""
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export Python", "", "Python (*.py)")
        if not path:
            return

        # Topological sort
        visited = set()
        order = []
        def _topo(node):
            if not hasattr(node, 'input_ports'):
                return
            nid = node.id
            if nid in visited:
                return
            visited.add(nid)
            for port in node.input_ports():
                for connected in port.connected_ports():
                    _topo(connected.node())
            order.append(node)
        for n in self._graph.all_nodes():
            _topo(n)

        lines = [
            '"""Auto-generated from Node Graph."""',
            'from pxr import Usd, UsdGeom, UsdShade, Sdf, Gf, Vt',
            '',
            '# Open your stage here:',
            '# stage = Usd.Stage.Open("path/to/file.usd")',
            '',
        ]

        var_counter = [0]
        node_vars = {}  # node.id -> {port_name: var_name}

        def _var(node, port_name):
            key = (node.id, port_name)
            if key not in node_vars:
                var_counter[0] += 1
                node_vars[key] = f"v{var_counter[0]}"
            return node_vars[key]

        for node in order:
            if not isinstance(node, nodes.UsdNodeBase) or node.disabled():
                continue
            # Build var_map from output ports
            var_map = {}
            for p in node.output_ports():
                var_map[p.name()] = _var(node, p.name())
            # Map input vars from connected nodes
            for p in node.input_ports():
                connected = p.connected_ports()
                if connected:
                    src_port = connected[0]
                    src_var = node_vars.get((src_port.node().id, src_port.name()))
                    if src_var:
                        var_map[p.name()] = src_var

            code = node.to_python(var_map)
            lines.append(f"# {node.name()}")
            lines.append(code)
            lines.append("")

        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"[NodeGraph] Exported to {path}")

    def _add_completer(self, edit, field_name, node):
        """Add context-aware autocomplete to a text field."""
        items = []
        try:
            from .graph_host import GraphHost
            stage = GraphHost.current().get_stage()

            if field_name == "attr_name":
                # Get attributes from connected prim
                prim = None
                if hasattr(node, '_output_cache'):
                    prim = node._output_cache.get("prim")
                if not prim:
                    port = node.get_input("prim")
                    if port and port.connected_ports():
                        src = port.connected_ports()[0].node()
                        if hasattr(src, '_output_cache'):
                            prim = src._output_cache.get("prim")
                        # Fallback: read prim_path property from source node
                        if not prim and hasattr(src, 'get_property'):
                            pp = src.get_property("prim_path")
                            if pp:
                                prim = stage.GetPrimAtPath(pp)
                if prim and hasattr(prim, 'IsValid') and prim.IsValid():
                    items = [a.GetName() for a in prim.GetAttributes()]

            elif field_name == "prim_path":
                items = [str(p.GetPath()) for p in stage.Traverse()][:200]

            elif field_name == "set_name":
                # Variant set names from connected prim
                prim = None
                port = node.get_input("prim")
                if port and port.connected_ports():
                    src = port.connected_ports()[0].node()
                    if hasattr(src, '_output_cache'):
                        prim = src._output_cache.get("prim")
                if prim and prim.IsValid():
                    items = prim.GetVariantSets().GetNames()

            elif field_name == "type_filter" or field_name == "prim_type":
                items = sorted(set(p.GetTypeName() for p in stage.Traverse() if p.GetTypeName()))

            elif field_name == "purpose":
                items = ["default", "render", "proxy", "guide"]

            elif field_name == "material_path":
                from pxr import UsdShade
                items = [str(p.GetPath()) for p in stage.Traverse()
                         if p.IsA(UsdShade.Material)]

        except Exception:
            pass

        if items:
            completer = QtWidgets.QCompleter(items)
            completer.setCaseSensitivity(QtCore.Qt.CaseInsensitive)
            completer.setFilterMode(QtCore.Qt.MatchContains)
            edit.setCompleter(completer)

    def _on_combo_changed(self, prop_name, text):
        if hasattr(self, "_selected_node") and self._selected_node:
            self._selected_node.set_property(prop_name, text)
            self._schedule_execute(self._selected_node)

    def _get_prim_attrs(self, node):
        """Get attribute names from the prim connected to this node."""
        try:
            from usd_chunks import chunk_registry as _cr
            stage = _cr.ChunkRegistry().scene.get_stage()
            # Try from output cache first
            if hasattr(node, '_output_cache') and node._output_cache.get("prim"):
                prim = node._output_cache["prim"]
                if hasattr(prim, 'GetAttributes'):
                    return sorted([a.GetName() for a in prim.GetAttributes()])
            # Try connected input prim
            port = node.get_input("prim")
            if port and port.connected_ports():
                src = port.connected_ports()[0].node()
                if hasattr(src, '_output_cache') and src._output_cache.get("prim"):
                    prim = src._output_cache["prim"]
                    if hasattr(prim, 'GetAttributes'):
                        return sorted([a.GetName() for a in prim.GetAttributes()])
                # Fallback: read prim_path property
                if hasattr(src, 'get_property'):
                    pp = src.get_property("prim_path")
                    if pp:
                        prim = stage.GetPrimAtPath(pp)
                        if prim and prim.IsValid():
                            return sorted([a.GetName() for a in prim.GetAttributes()])
        except Exception:
            pass
        return []

    def _show_thumbnail(self, label, path):
        pixmap = QtGui.QPixmap(path)
        if not pixmap.isNull():
            label.setPixmap(pixmap.scaled(200, 200, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation))
        else:
            label.setText("No preview")

    def _on_code_changed(self, edit):
        name = edit.property("prop_name")
        if hasattr(self, "_selected_node") and self._selected_node:
            self._selected_node.set_property(name, edit.toPlainText())
            self._schedule_execute(self._selected_node)

    def _build_chunk_panel(self, node):
        """Build the side panel using the real ChunkWidget from chunk_qt."""
        try:
            from plugins.chunk_qt import ChunkWidget
            from node_graph.chunk_adapter import ChunkAdapter
            adapter = getattr(node, '_cached_adapter', None)
            if not adapter or not adapter.chunk:
                # Use the node's per-instance chunk so the side panel edits the right one
                # (multiple nodes of the same chunk type would otherwise share).
                my_chunk = None
                if hasattr(node, "_ensure_chunk"):
                    try:
                        my_chunk = node._ensure_chunk()
                    except Exception:
                        my_chunk = None
                adapter = ChunkAdapter(
                    node._chunk_display_name,
                    module_name=node._chunk_module,
                    auto_create=(my_chunk is None),
                    chunk=my_chunk,
                )
                node._cached_adapter = adapter
            if not adapter.chunk:
                self._props_form.addRow(QtWidgets.QLabel("Chunk not loaded"))
                return
            # Arm the chunk's notice listener as soon as the chunk exists, so
            # edits in the side panel (or any notice-dependency reaction)
            # trigger downstream node execution even before the user has hit
            # "Execute Graph".
            if hasattr(node, "_subscribe_to_chunk_notices"):
                node._subscribe_to_chunk_notices(adapter.chunk)
            # Create the same widget used in the usdview chunk panel
            chunk_widget = ChunkWidget(adapter.chunk)
            # Style combo popup views — QAbstractItemView is a top-level OS window
            # and does not inherit parent stylesheets, so we must set it explicitly.
            _combo_popup_style = (
                "QAbstractItemView { background: #0e1016; color: #c8d0e8;"
                " border: 1px solid #1e2438;"
                " selection-background-color: #1e2a40; selection-color: #c8d0e8;"
                " outline: none; }"
            )
            for _cb in chunk_widget.findChildren(QtWidgets.QComboBox):
                _cb.view().setStyleSheet(_combo_popup_style)
                _cb.setStyleSheet(
                    "QComboBox { background: #111520; color: #c8d0e8;"
                    " border: 1px solid #1e2438; border-radius: 3px;"
                    " padding: 2px 6px; font: 10px 'Consolas'; }"
                    "QComboBox::drop-down { border: none; }"
                    "QComboBox::down-arrow { image: none; width: 10px; }"
                )
            # Wire right-click Copy/Paste Reference on every param widget inside the ChunkWidget.
            # ChunkParamWidgetBase subclasses all carry a `.param` attribute pointing at their
            # chunk param object — we use that to build the '@Node.param' reference string.
            for _pw in chunk_widget.findChildren(QtWidgets.QWidget):
                _p = getattr(_pw, 'param', None)
                if _p is not None and hasattr(_p, 'value_reference'):
                    self._setup_chunk_ref_menu(_pw, node, _p)
            self._props_form.addRow(chunk_widget)
            # Disable widgets whose params have connected input ports
            connected_params = set()
            for port in node.input_ports():
                if port.name().startswith("_") and port.connected_ports():
                    connected_params.add(port.name()[1:])  # strip leading _
            if connected_params:
                for child in chunk_widget.findChildren(QtWidgets.QWidget):
                    # Find widgets that are chunk param widgets with matching names
                    if hasattr(child, 'param') and hasattr(child.param, 'name'):
                        if child.param.name in connected_params:
                            child.setEnabled(False)
                            child.setToolTip(f"Driven by connection on _{child.param.name}")
        except Exception as e:
            print(f"[NodeGraph] chunk panel err: {e}")
            self._props_form.addRow(QtWidgets.QLabel(f"Error: {e}"))

    def _add_chunk_widget(self, factory, param, node):
        """Create a chunk widget for a param and add it to the side panel."""
        from usd_chunks import chunk_core
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
        label = getattr(param, 'label', None) or getattr(param, 'name', '')
        prop_name = param.name
        if prop_name in node.model.custom_properties:
            def _make_sync(pn, w):
                def _sync(*args):
                    try:
                        node.set_property(pn, str(w.get_value() or ""))
                    except Exception:
                        pass
                return _sync
            if hasattr(widget, 'value_changed') and hasattr(widget.value_changed, 'connect'):
                widget.value_changed.connect(_make_sync(prop_name, widget))
        # Wire copy/paste-as-reference on chunk widgets. Resolved adapter is needed so paste can
        # write to value_reference via set_param().
        try:
            adapter = getattr(node, "_cached_adapter", None)
            if adapter is None or not getattr(adapter, "chunk", None):
                from .chunk_adapter import ChunkAdapter
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

    def _on_bool_prop_changed(self, name, state):
        if hasattr(self, "_selected_node") and self._selected_node:
            self._selected_node.set_property(name, bool(state))
            self._schedule_execute(self._selected_node)

    def _on_prop_changed(self):
        edit = self.sender()
        name = edit.property("prop_name")
        if not (hasattr(self, "_selected_node") and self._selected_node):
            return
        text = edit.text()
        node = self._selected_node
        current = node.model.custom_properties.get(name)
        if isinstance(current, bool):
            node.set_property(name, text.strip().lower() in ("true", "1", "yes"))
        else:
            node.set_property(name, text)

        # If the edited property maps to a chunk param on a chunk-wrapped node, also push
        # through the chunk adapter. An '@...' string will land on value_reference, making
        # the binding live across the whole chunk system (not just the node graph).
        try:
            display = getattr(node, "_chunk_display_name", None)
            if display is None:
                return
            adapter = getattr(node, "_cached_adapter", None)
            if adapter is None or not getattr(adapter, "chunk", None):
                from .chunk_adapter import ChunkAdapter
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
            if adapter and adapter.chunk and adapter.chunk.get_param(name) is not None:
                adapter.set_param(name, text)
        except Exception:
            pass
        self._schedule_execute(node)

    _ref_clipboard = None  # class-level: stores "@node_name.prop_name"

    def _setup_ref_menu(self, edit, node, prop_name):
        """Add right-click Copy/Paste Reference to a QLineEdit."""
        edit.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        def _show_menu(pos, e=edit, n=node, p=prop_name):
            menu = e.createStandardContextMenu()
            menu.addSeparator()
            copy_act = menu.addAction("📋 Copy Reference")
            paste_act = menu.addAction("📌 Paste Reference")
            paste_act.setEnabled(NodeGraphWidget._ref_clipboard is not None)
            action = menu.exec_(e.mapToGlobal(pos))
            if action == copy_act:
                NodeGraphWidget._ref_clipboard = f"@{n.name()}.{p}"
            elif action == paste_act and NodeGraphWidget._ref_clipboard:
                e.setText(NodeGraphWidget._ref_clipboard)
                e.editingFinished.emit()
        edit.customContextMenuRequested.connect(_show_menu)

    @staticmethod
    def _build_chunk_param_ref(node, param):
        """Build '@NodeName.dotted.path.to.param' by walking the param up to its chunk root."""
        try:
            chunk_obj = param.get_chunk() if hasattr(param, "get_chunk") else None
        except Exception:
            chunk_obj = None
        parts = []
        cur = param
        # Walk up via get_owner() until we hit the chunk (or the top).
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

    def _setup_chunk_ref_menu(self, widget, node, param):
        """Attach Copy/Paste Reference menu to all QLineEdit children of a chunk widget.

        Copy stores the full dotted path from the chunk root. Paste sets value_reference
        directly on the param object and triggers execution.
        """
        edits = widget.findChildren(QtWidgets.QLineEdit)
        if not edits:
            # No text editor inside this widget — attach to the widget itself so right-click
            # still works (e.g. on a slider track).
            edits = [widget]

        for ed in edits:
            try:
                ed.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
            except Exception:
                continue

            def _show_menu(pos, e=ed, n=node, p=param):
                menu = e.createStandardContextMenu() if isinstance(e, QtWidgets.QLineEdit) else QtWidgets.QMenu(e)
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
                action = menu.exec_(e.mapToGlobal(pos))
                if action == copy_act:
                    NodeGraphWidget._ref_clipboard = self._build_chunk_param_ref(n, p)
                elif action == paste_act and NodeGraphWidget._ref_clipboard:
                    try:
                        # Set directly on the param object — no adapter lookup needed,
                        # this also fires _rewire_value_reference immediately.
                        p.value_reference = NodeGraphWidget._ref_clipboard
                        if isinstance(e, QtWidgets.QLineEdit):
                            e.setText(NodeGraphWidget._ref_clipboard)
                        # Execute the node so the reference resolves and the chunk rebuilds.
                        QtCore.QTimer.singleShot(0, lambda _n=n: self._execute_node(_n) if self._stage else None)
                    except Exception as err:
                        print(f"[NodeGraph] paste ref err: {err}")
                elif action == clear_act:
                    try:
                        p.value_reference = None
                        if isinstance(e, QtWidgets.QLineEdit):
                            e.setText(str(getattr(p, "value", "") or ""))
                        QtCore.QTimer.singleShot(0, lambda _n=n: self._execute_node(_n) if self._stage else None)
                    except Exception as err:
                        print(f"[NodeGraph] clear ref err: {err}")
            ed.customContextMenuRequested.connect(_show_menu)

    def _viewer_drag_enter(self, event):
        event.acceptProposedAction()

    def _viewer_drop(self, event):
        import sys
        print("[drop] viewer drop fired", file=sys.stderr)
        self._handle_drop(event, use_scene_pos=False)

    def _scene_drop(self, event):
        import sys
        print("[drop] scene drop fired", file=sys.stderr)
        self._handle_drop(event, use_scene_pos=True)

    def _extract_drop_text(self, mime):
        """Extract text from various mime formats."""
        if mime.hasUrls():
            return mime.urls()[0].toLocalFile()
        if mime.hasText():
            return mime.text().strip()
        # Qt tree item drag — decode the model data
        if mime.hasFormat("application/x-qabstractitemmodeldatalist"):
            from PySide6 import QtCore
            data = mime.data("application/x-qabstractitemmodeldatalist")
            stream = QtCore.QDataStream(data, QtCore.QIODevice.ReadOnly)
            while not stream.atEnd():
                stream.readInt32()  # row
                stream.readInt32()  # col
                count = stream.readInt32()  # map items
                for _ in range(count):
                    role = stream.readInt32()
                    value = stream.readQVariant()
                    if role == 0:  # Qt.DisplayRole
                        return str(value).strip()
        return ""

    def _handle_drop(self, event, use_scene_pos=False):
        text = self._extract_drop_text(event.mimeData())

        if not text:
            event.ignore()
            return

        # Image file → create Texture File node
        import os
        _IMG_EXTS = {".png", ".jpg", ".jpeg", ".exr", ".tga", ".bmp", ".tif", ".tiff", ".hdr", ".tx"}
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

        # If it's a tree item name (not a path), resolve to full prim path
        if not text.startswith("/"):
            try:
                from .graph_host import GraphHost
                _host = GraphHost.current()
                # Check if it's an attribute name (contains ":" or matches known attrs)
                stage = _host.get_stage()
                sel = _host.get_selection()
                if sel and sel[0].GetAttribute(text).IsValid():
                    # It's an attribute — create GetAttribute node
                    if use_scene_pos:
                        pos = (event.scenePos().x(), event.scenePos().y())
                    else:
                        viewer = self._graph.viewer()
                        sp = viewer.mapToScene(event.pos())
                        pos = (sp.x(), sp.y())
                    # Create PrimPath + GetAttribute connected
                    prim_node = self._create_node(nodes.PrimPathNode, pos=(pos[0] - 200, pos[1]))
                    attr_node = self._create_node(nodes.GetAttributeNode, pos=pos)
                    if prim_node and attr_node:
                        prim_node.set_property("prim_path", str(sel[0].GetPath()))
                        attr_node.set_property("attr_name", text)
                        out = next((p for p in prim_node.output_ports() if p.name() == "prim"), None)
                        inp = next((p for p in attr_node.input_ports() if p.name() == "prim"), None)
                        if out and inp:
                            out.connect_to(inp)
                        if self._stage:
                            self._execute_node(prim_node)
                    event.acceptProposedAction()
                    return

                # Otherwise resolve as prim name
                if sel:
                    text = str(sel[0].GetPath())
                else:
                    for p in stage.Traverse():
                        if p.GetName() == text or text in p.GetName():
                            text = str(p.GetPath())
                            break
            except Exception as e:
                import sys
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
            node = self._create_node(nodes.PrimPathNode, pos=pos)
            if node:
                node.set_property("prim_path", text)
                if self._stage:
                    self._execute_node(node)
        else:
            node = self._create_node(nodes.StringNode, pos=pos)
            if node:
                node.set_property("value", text)
        event.acceptProposedAction()

    def _add_backdrop(self):
        """Add a backdrop around selected nodes."""
        g = self._active_graph()
        selected = g.selected_nodes()
        backdrop = g.create_node(
            "nodeGraphQt.nodes.BackdropNode", name="Backdrop")
        if selected:
            min_x = min(n.pos()[0] for n in selected) - 40
            min_y = min(n.pos()[1] for n in selected) - 80
            max_x = max(n.pos()[0] for n in selected) + 200
            max_y = max(n.pos()[1] for n in selected) + 120
            backdrop.set_pos(min_x, min_y)
            backdrop.set_property("width", max_x - min_x)
            backdrop.set_property("height", max_y - min_y)

    def _group_selected(self):
        """Group selected nodes into a GroupNode sub-graph."""
        g = self._active_graph()
        selected = g.selected_nodes()
        if len(selected) < 1:
            return

        sel_ids = {n.id for n in selected}
        cx = sum(n.pos()[0] for n in selected) / len(selected)
        cy = sum(n.pos()[1] for n in selected) / len(selected)

        # Serialize selected nodes (captures internal connections)
        session = g._serialize(selected)

        # Find external connections crossing the group boundary
        ext_inputs = []   # (ext_out_port, inner_node_id, inner_port_name)
        ext_outputs = []  # (inner_node_id, inner_port_name, ext_in_port)
        for node in selected:
            for port in node.input_ports():
                for cp in port.connected_ports():
                    if cp.node().id not in sel_ids:
                        ext_inputs.append((cp, node.id, port.name()))
            for port in node.output_ports():
                for cp in port.connected_ports():
                    if cp.node().id not in sel_ids:
                        ext_outputs.append((node.id, port.name(), cp))

        # Delete originals first (before creating group to avoid ID conflicts)
        g.delete_nodes(selected)

        # Create group and expand it so add_input/add_output create port nodes
        group = g.create_node(
            "nodeGraphQt.nodes.GroupNode", name="Group")
        group.set_pos(cx, cy)
        group.set_port_deletion_allowed(True)
        sub = self._graph.expand_group_node(group)

        # Add ports while expanded — this auto-creates PortInputNode/PortOutputNode
        for i, (_, nid, pname) in enumerate(ext_inputs):
            group.add_input(f"in_{i}")
        if not ext_outputs:
            # Always provide at least one output so users can connect out
            group.add_output("out_0")
        for i, (nid, pname, _) in enumerate(ext_outputs):
            group.add_output(f"out_{i}")

        # Deserialize inner nodes into the sub-graph
        inner_nodes = sub._deserialize(session, adjust_graph_style=False)
        # Build old_id → new_node map
        old_to_new = {}
        old_ids = list(session.get('nodes', {}).keys())
        for old_id, new_node in zip(old_ids, inner_nodes):
            old_to_new[old_id] = new_node

        # Wire PortInputNodes → inner nodes
        port_in_nodes = sub.get_input_port_nodes()
        for i, (_, old_nid, pname) in enumerate(ext_inputs):
            inner = old_to_new.get(old_nid)
            pin = port_in_nodes[i] if i < len(port_in_nodes) else None
            if inner and pin:
                out_p = pin.output_ports()[0] if pin.output_ports() else None
                in_p = inner.inputs().get(pname)
                if out_p and in_p:
                    out_p.connect_to(in_p)

        # Wire inner nodes → PortOutputNodes
        port_out_nodes = sub.get_output_port_nodes()
        for i, (old_nid, pname, _) in enumerate(ext_outputs):
            inner = old_to_new.get(old_nid)
            pout = port_out_nodes[i] if i < len(port_out_nodes) else None
            if inner and pout:
                out_p = inner.outputs().get(pname)
                in_p = pout.input_ports()[0] if pout.input_ports() else None
                if out_p and in_p:
                    out_p.connect_to(in_p)

        # Collapse back — serializes the sub-graph session into the group
        self._graph.collapse_group_node(group)

        # Rewire external connections to group ports
        for i, (ext_port, _, _) in enumerate(ext_inputs):
            gp = group.input_ports()[i] if i < len(group.input_ports()) else None
            if gp:
                ext_port.connect_to(gp)
        for i, (_, _, ext_port) in enumerate(ext_outputs):
            gp = group.output_ports()[i] if i < len(group.output_ports()) else None
            if gp:
                gp.connect_to(ext_port)

    def _show_add_menu(self):
        self._node_menu.exec_(QtGui.QCursor.pos())

    def _show_search_menu(self):
        dlg = _TabSearchDialog(self)
        if dlg.exec_() == QtWidgets.QDialog.Accepted and dlg.selected_node:
            g = self._active_graph()
            viewer = g.viewer()
            pos = viewer.mapFromGlobal(QtGui.QCursor.pos())
            scene_pos = viewer.mapToScene(pos)
            self._create_node(dlg.selected_node, pos=(scene_pos.x(), scene_pos.y()))

    def _on_add_node(self):
        action = self.sender()
        self._create_node(action.data())

    def _create_node(self, node_cls, pos=None, graph=None):
        g = graph or self._active_graph()
        identifier = f"{node_cls.__identifier__}.{node_cls.__name__}"
        kwargs = {"name": node_cls.NODE_NAME}
        if pos:
            kwargs["pos"] = pos
        return g.create_node(identifier, **kwargs)

    # ── Execution engine ──────────────────────
    def set_stage(self, stage):
        self._stage = stage

    def execute_graph(self):
        if not self._stage:
            print("[NodeGraph] No stage set — cannot execute.")
            return

        visited = set()
        order = []

        def _topo(node):
            if not hasattr(node, 'input_ports'):
                return
            nid = node.id
            if nid in visited:
                return
            visited.add(nid)
            for port in node.input_ports():
                for connected in port.connected_ports():
                    _topo(connected.node())
            order.append(node)

        for n in self._graph.all_nodes():
            _topo(n)

        errored = set()
        for node in order:
            upstream_errored = any(
                cp.node().id in errored
                for port in (node.input_ports() if hasattr(node, 'input_ports') else [])
                for cp in port.connected_ports()
            )
            if upstream_errored:
                self._set_exec_state(node, "skipped")
                errored.add(node.id)
                node._output_cache = {}
                continue
            if self._execute_node(node) is False:
                errored.add(node.id)
            # ForEach iteration: re-run downstream nodes per item
            elif isinstance(node, nodes.ForEachNode) and node._iteration_list:
                downstream = self._get_downstream(node)
                for i in range(len(node._iteration_list)):
                    if not node.set_iteration_index(i):
                        break
                    for dn in downstream:
                        self._execute_node(dn)

    def _get_downstream(self, start_node):
        """Collect downstream nodes in topological order from start_node."""
        visited = set()
        order = []
        def _walk(node):
            nid = node.id
            if nid in visited:
                return
            visited.add(nid)
            order.append(node)
            for port in node.output_ports():
                for cp in port.connected_ports():
                    _walk(cp.node())
        for port in start_node.output_ports():
            for cp in port.connected_ports():
                _walk(cp.node())
        return order

    def _layout_nodes(self, node_list=None):
        """Layout nodes left-to-right by depth, vertically aligned to connections."""
        g = self._active_graph()
        if node_list:
            expanded = set()
            def _collect(node):
                if node.id in expanded:
                    return
                expanded.add(node.id)
                for port in node.input_ports():
                    for cp in port.connected_ports():
                        _collect(cp.node())
                for port in node.output_ports():
                    for cp in port.connected_ports():
                        _collect(cp.node())
            for n in node_list:
                _collect(n)
            all_n = [n for n in g.all_nodes() if n.id in expanded]
        else:
            all_n = g.all_nodes()
        if not all_n:
            return
        node_set = set(n.id for n in all_n)
        node_by_id = {n.id: n for n in all_n}

        def _node_size(n):
            try:
                r = n.view.boundingRect()
                return r.width(), r.height()
            except Exception:
                return 160, 80

        def _upstream_ids(node):
            ids = []
            for port in node.input_ports():
                for cp in port.connected_ports():
                    if cp.node().id in node_set:
                        ids.append(cp.node().id)
            return ids

        def _downstream_ids(node):
            ids = []
            for port in node.output_ports():
                for cp in port.connected_ports():
                    if cp.node().id in node_set:
                        ids.append(cp.node().id)
            return ids

        # ── Step 1: compute depth from upstream (longest path from a source) ──
        depth = {}
        def _get_depth(node):
            if node.id in depth:
                return depth[node.id]
            depth[node.id] = 0  # guard against cycles
            max_d = -1
            for uid in _upstream_ids(node):
                max_d = max(max_d, _get_depth(node_by_id[uid]))
            depth[node.id] = max_d + 1
            return depth[node.id]
        for n in all_n:
            _get_depth(n)

        # ── Step 2: pull source nodes (depth 0) closer to their downstream ──
        # If a source only connects to nodes at depth >= 2, move it to depth-1
        for n in all_n:
            if depth[n.id] == 0 and not _upstream_ids(n):
                ds = _downstream_ids(n)
                if ds:
                    min_ds_depth = min(depth[did] for did in ds)
                    if min_ds_depth > 1:
                        depth[n.id] = min_ds_depth - 1

        # ── Step 3: group by depth (columns) ──
        columns = {}
        for n in all_n:
            columns.setdefault(depth[n.id], []).append(n)

        # ── Step 4: compute column X positions using actual max widths ──
        x_gap = 60
        y_gap = 35
        col_x = {}
        sorted_cols = sorted(columns.keys())
        start_x = min(n.pos()[0] for n in all_n)
        cur_x = start_x
        for ci in sorted_cols:
            col_x[ci] = cur_x
            max_w = max(_node_size(n)[0] for n in columns[ci])
            cur_x += max_w + x_gap

        # ── Step 5: place nodes column by column, Y aligned to connections ──
        placed = {}  # node.id -> (x, y, h)

        def _target_y(n):
            """Ideal Y = average Y of connected upstream OR downstream nodes."""
            ups = [placed[uid][1] for uid in _upstream_ids(n) if uid in placed]
            if ups:
                return sum(ups) / len(ups)
            # For sources, align to downstream
            ds = [placed[did][1] for did in _downstream_ids(n) if did in placed]
            if ds:
                return sum(ds) / len(ds)
            return 0

        def _place_column(col_nodes, cx):
            """Place nodes in a column avoiding overlaps, sorted by target Y."""
            # Compute target Y for each node
            targets = []
            for n in col_nodes:
                ty = _target_y(n)
                _, h = _node_size(n)
                targets.append((ty, n, h))
            targets.sort(key=lambda t: t[0])

            # Place with overlap avoidance
            occupied = []  # list of (y_top, y_bottom)
            for ty, n, h in targets:
                y = ty
                # Push down past any overlapping placed node in this column
                for oy_top, oy_bot in occupied:
                    if y < oy_bot + y_gap and y + h > oy_top - y_gap:
                        y = oy_bot + y_gap
                occupied.append((y, y + h))
                placed[n.id] = (cx, y, h)

        # First pass: place columns left to right (upstream → downstream)
        for ci in sorted_cols:
            _place_column(columns[ci], col_x[ci])

        # Second pass: re-place source columns now that downstream positions are known
        if sorted_cols:
            first_col = sorted_cols[0]
            sources = [n for n in columns[first_col] if not _upstream_ids(n)]
            if sources:
                # Remove old placements for sources
                for n in sources:
                    if n.id in placed:
                        del placed[n.id]
                _place_column(sources, col_x[first_col])

        # Apply positions
        for n in all_n:
            if n.id in placed:
                n.set_pos(placed[n.id][0], placed[n.id][1])

    @property
    def graph(self):
        return self._graph


class _TabSearchDialog(QtWidgets.QDialog):
    """Quick search dialog for nodes — press Tab, type, Enter. AI suggestions appear with ⭐."""

    def __init__(self, parent=None, port_type=None):
        super().__init__(parent, QtCore.Qt.Popup)
        self.setStyleSheet(
            "QDialog { background: #0c0e14; border: 1px solid #1e2438; border-radius: 8px; }"
            "QLineEdit { background: rgba(0,0,0,0.4); color: #c8d0e8; border: 1px solid #1e2438;"
            "  border-radius: 5px; padding: 6px 10px; font: 11px 'Consolas'; }"
            "QLineEdit:focus { border-color: #32d2dc; }"
            "QListWidget { background: transparent; color: #c8d0e8; border: none;"
            "  font: 10px 'Consolas'; }"
            "QListWidget::item { padding: 3px 6px; border-radius: 3px; }"
            "QListWidget::item:hover { background: #161b26; }"
            "QListWidget::item:selected { background: #1a2a3a; color: #32d2dc; }"
        )
        self.selected_node = None
        self._suggested = []
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self._search = QtWidgets.QLineEdit()
        self._search.setPlaceholderText("Search nodes...")
        self._search.textChanged.connect(self._filter)
        layout.addWidget(self._search)

        self._list = QtWidgets.QListWidget()
        self._list.itemClicked.connect(self._pick)
        layout.addWidget(self._list)

        # Combine static + discovered nodes
        all_nodes = list(nodes.ALL_NODES)
        if hasattr(parent, '_discovered_chunks'):
            all_nodes += parent._discovered_chunks

        self._all_items = []
        self._node_map = {}  # NODE_NAME -> node_cls
        for node_cls in all_nodes:
            label = f"[{node_cls.CATEGORY}] {node_cls.NODE_NAME}"
            self._all_items.append((label, node_cls))
            self._node_map[node_cls.NODE_NAME] = node_cls
            item = QtWidgets.QListWidgetItem(label)
            item.setData(QtCore.Qt.UserRole, node_cls)
            self._list.addItem(item)

        self._search.returnPressed.connect(self._accept_first)
        self.resize(280, 350)
        self._search.setFocus()

        # Fire AI suggestion in background
        if hasattr(parent, '_graph'):
            try:
                from .ai_suggest import suggest_next_nodes, get_graph_context
                ctx = get_graph_context(parent._graph)
                suggest_next_nodes(ctx, self._on_suggestions)
            except Exception:
                pass

    def _on_suggestions(self, suggested_names):
        """Called from background thread with AI suggestions."""
        self._suggested = suggested_names
        # Update UI on main thread
        from PySide6 import QtCore
        QtCore.QTimer.singleShot(0, self._apply_suggestions)

    def _apply_suggestions(self):
        if not self._suggested or not self._search.text() == "":
            return
        self._search.setPlaceholderText("Search nodes...")
        # Rebuild list with suggestions at top
        self._list.clear()
        added = set()
        for name in self._suggested:
            cls = self._node_map.get(name)
            if cls:
                label = f"⭐ [{cls.CATEGORY}] {cls.NODE_NAME}"
                item = QtWidgets.QListWidgetItem(label)
                item.setData(QtCore.Qt.UserRole, cls)
                self._list.addItem(item)
                added.add(name)
        for label, node_cls in self._all_items:
            if node_cls.NODE_NAME not in added:
                item = QtWidgets.QListWidgetItem(label)
                item.setData(QtCore.Qt.UserRole, node_cls)
                self._list.addItem(item)

    def _filter(self, text):
        text = text.lower()
        self._list.clear()
        for label, node_cls in self._all_items:
            if text in label.lower():
                item = QtWidgets.QListWidgetItem(label)
                item.setData(QtCore.Qt.UserRole, node_cls)
                self._list.addItem(item)

    def _pick(self, item):
        self.selected_node = item.data(QtCore.Qt.UserRole)
        self.accept()

    def _accept_first(self):
        if self._list.count() > 0:
            item = self._list.item(0)
            self.selected_node = item.data(QtCore.Qt.UserRole)
            self.accept()
