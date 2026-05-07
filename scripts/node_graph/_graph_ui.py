"""Standalone UI helper widgets and dialogs for the node graph."""
from __future__ import annotations
from PySide6 import QtWidgets, QtCore, QtGui
from . import nodes


class _LineNumberArea(QtWidgets.QWidget):
    def __init__(self, editor: _CodeEditor) -> None:
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(self._editor._line_number_width(), 0)

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        self._editor._paint_line_numbers(event)


class _CodeEditor(QtWidgets.QPlainTextEdit):
    """PlainTextEdit with line numbers and autocomplete."""
    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._line_area = _LineNumberArea(self)
        self.blockCountChanged.connect(lambda: self._update_margin())
        self.updateRequest.connect(self._update_line_area)
        self._update_margin()

        self._completer = QtWidgets.QCompleter([
            "stage", "inputs", "output", "prim",
            "stage.GetPrimAtPath", "stage.Traverse", "stage.DefinePrim",
            "stage.GetRootLayer", "stage.GetSessionLayer", "stage.GetLayerStack",
            "prim.GetPath", "prim.GetName", "prim.GetTypeName", "prim.GetChildren",
            "prim.GetAttribute", "prim.GetAttributes", "prim.GetReferences",
            "prim.GetVariantSets", "prim.IsValid", "prim.IsActive",
            "prim.GetParent", "prim.GetPrimAtPath",
            "UsdGeom.Xformable", "UsdGeom.Mesh", "UsdGeom.BBoxCache",
            "UsdGeom.Imageable", "UsdGeom.Gprim",
            "UsdShade.Material", "UsdShade.Shader", "UsdShade.MaterialBindingAPI",
            "Gf.Vec3f", "Gf.Vec3d", "Gf.Matrix4d", "Vt.Vec3fArray",
            "Sdf.Path", "Sdf.Reference", "Sdf.Payload", "Sdf.ValueTypeNames",
            "from pxr import", "from pxr import Usd, UsdGeom, Gf, Sdf",
            "for p in stage.Traverse():",
            "output = ", "inputs['in_1']", "inputs['in_2']", "inputs['in_3']",
        ])
        self._completer.setWidget(self)
        self._completer.setCompletionMode(QtWidgets.QCompleter.PopupCompletion)
        self._completer.setCaseSensitivity(QtCore.Qt.CaseInsensitive)
        self._completer.activated.connect(self._insert_completion)

    def _insert_completion(self, text: str) -> None:
        tc = self.textCursor()
        tc.movePosition(QtGui.QTextCursor.StartOfWord, QtGui.QTextCursor.KeepAnchor)
        tc.insertText(text)
        self.setTextCursor(tc)

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        if self._completer.popup().isVisible():
            if event.key() in (QtCore.Qt.Key_Enter, QtCore.Qt.Key_Return,
                               QtCore.Qt.Key_Escape, QtCore.Qt.Key_Tab):
                event.ignore()
                return

        if event.key() == QtCore.Qt.Key_Space and event.modifiers() == QtCore.Qt.ControlModifier:
            self._request_ai_suggestion()
            return

        super().keyPressEvent(event)

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

    def _request_ai_suggestion(self) -> None:
        tc = self.textCursor()
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

        from .ai_suggest import suggest_python_completion
        suggest_python_completion(code_before, code_after, inputs_desc, self._on_ai_code)

    def _on_ai_code(self, suggested_code: str) -> None:
        self._pending_suggestion = suggested_code
        QtCore.QTimer.singleShot(0, self._apply_ai_code)

    def _apply_ai_code(self) -> None:
        code = getattr(self, '_pending_suggestion', '')
        self.setPlaceholderText("")
        if not code:
            return
        tc = self.textCursor()
        tc.setPosition(self._cursor_pos)
        tc.insertText(code)
        self.setTextCursor(tc)

    def _line_number_width(self) -> int:
        digits = max(1, len(str(self.blockCount())))
        return 8 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_margin(self) -> None:
        self.setViewportMargins(self._line_number_width(), 0, 0, 0)

    def _update_line_area(self, rect: QtCore.QRect, dy: int) -> None:
        if dy:
            self._line_area.scroll(0, dy)
        else:
            self._line_area.update(0, rect.y(), self._line_area.width(), rect.height())

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._line_area.setGeometry(cr.left(), cr.top(), self._line_number_width(), cr.height())

    def _paint_line_numbers(self, event: QtGui.QPaintEvent) -> None:
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
    """QLabel that rescales its pixmap instantly on resize."""
    def __init__(self) -> None:
        super().__init__()
        self._raw_px: QtGui.QPixmap | None = None
        self.setAlignment(QtCore.Qt.AlignCenter)

    def set_raw_pixmap(self, px: QtGui.QPixmap | QtGui.QImage) -> None:
        if isinstance(px, QtGui.QImage):
            px = QtGui.QPixmap.fromImage(px)
        self._raw_px = px
        self._rescale()

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self) -> None:
        px = self._raw_px
        if px and not px.isNull() and self.width() > 0 and self.height() > 0:
            w = max(self.width() - 4, 100)
            h = max(self.height() - 4, 80)
            super().setPixmap(px.scaled(w, h, QtCore.Qt.KeepAspectRatio,
                                        QtCore.Qt.FastTransformation))


def add_field_completer(edit: QtWidgets.QLineEdit, field_name: str, node: object) -> None:
    """Add context-aware autocomplete to a QLineEdit based on field type."""
    items = []
    try:
        from .graph_host import GraphHost
        stage = GraphHost.current().get_stage()

        if field_name == "attr_name":
            prim = None
            if hasattr(node, '_output_cache'):
                prim = node._output_cache.get("prim")
            if not prim:
                port = node.get_input("prim")
                if port and port.connected_ports():
                    src = port.connected_ports()[0].node()
                    if hasattr(src, '_output_cache'):
                        prim = src._output_cache.get("prim")
                    if not prim and hasattr(src, 'get_property'):
                        pp = src.get_property("prim_path")
                        if pp:
                            prim = stage.GetPrimAtPath(pp)
            if prim and hasattr(prim, 'IsValid') and prim.IsValid():
                items = [a.GetName() for a in prim.GetAttributes()]

        elif field_name == "prim_path":
            items = [str(p.GetPath()) for p in stage.Traverse()][:200]

        elif field_name == "set_name":
            prim = None
            port = node.get_input("prim")
            if port and port.connected_ports():
                src = port.connected_ports()[0].node()
                if hasattr(src, '_output_cache'):
                    prim = src._output_cache.get("prim")
            if prim and prim.IsValid():
                items = prim.GetVariantSets().GetNames()

        elif field_name in ("type_filter", "prim_type"):
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


class _TabSearchDialog(QtWidgets.QDialog):
    """Quick search dialog for nodes — press Tab, type, Enter. AI suggestions appear with ⭐."""

    def __init__(self, parent: QtWidgets.QWidget | None = None, port_type: str | None = None, dragged_dtype: str | None = None, dragged_is_output: bool | None = None) -> None:
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

        all_node_list = list(nodes.ALL_NODES)
        if hasattr(parent, '_discovered_chunks'):
            all_node_list += parent._discovered_chunks

        if dragged_dtype and dragged_dtype != 'any' and dragged_is_output is not None:
            def _compat(cls):
                pt = nodes._get_node_port_types(cls)
                target = pt["inputs"] if dragged_is_output else pt["outputs"]
                return dragged_dtype in target or 'any' in target
            all_node_list = [c for c in all_node_list if _compat(c)]

        self._all_items = []
        self._node_map = {}
        for node_cls in all_node_list:
            label = f"[{node_cls.CATEGORY}] {node_cls.NODE_NAME}"
            self._all_items.append((label, node_cls))
            self._node_map[node_cls.NODE_NAME] = node_cls
            item = QtWidgets.QListWidgetItem(label)
            item.setData(QtCore.Qt.UserRole, node_cls)
            self._list.addItem(item)

        self._search.returnPressed.connect(self._accept_first)
        self.resize(280, 350)
        self._search.setFocus()

        if hasattr(parent, '_graph'):
            try:
                from .ai_suggest import suggest_next_nodes, get_graph_context
                ctx = get_graph_context(parent._graph)
                suggest_next_nodes(ctx, self._on_suggestions)
            except Exception:
                pass

    def _on_suggestions(self, suggested_names: list[str]) -> None:
        self._suggested = suggested_names
        QtCore.QTimer.singleShot(0, self._apply_suggestions)

    def _apply_suggestions(self) -> None:
        if not self._suggested or self._search.text() != "":
            return
        self._search.setPlaceholderText("Search nodes...")
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

    def _filter(self, text: str) -> None:
        text = text.lower()
        self._list.clear()
        for label, node_cls in self._all_items:
            if text in label.lower():
                item = QtWidgets.QListWidgetItem(label)
                item.setData(QtCore.Qt.UserRole, node_cls)
                self._list.addItem(item)

    def _pick(self, item: QtWidgets.QListWidgetItem) -> None:
        self.selected_node = item.data(QtCore.Qt.UserRole)
        self.accept()

    def _accept_first(self) -> None:
        if self._list.count() > 0:
            item = self._list.item(0)
            self.selected_node = item.data(QtCore.Qt.UserRole)
            self.accept()
