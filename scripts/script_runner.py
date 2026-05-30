"""Script Runner chunk — live Python scripting panel in usdview.

Executes arbitrary Python with stage, prim, and usdviewApi in scope.
"""
from __future__ import annotations

import traceback

from usd_chunks import chunk, chunk_core
from PySide6 import QtWidgets, QtCore, QtGui
from scripts.node_graph._graph_ui import _CodeEditor, _PythonHighlighter


class _UsdPythonHighlighter(_PythonHighlighter):
    """Extends Python highlighter with USD-specific keyword colors."""

    def __init__(self, parent=None):
        super().__init__(parent)
        usd_fmt = QtGui.QTextCharFormat()
        usd_fmt.setForeground(QtGui.QColor("#e5c07b"))
        usd_fmt.setFontWeight(QtGui.QFont.Bold)
        for w in (
            "stage", "prim", "usdviewApi",
            "Usd", "UsdGeom", "UsdShade", "UsdLux", "UsdSkel",
            "Sdf", "Gf", "Vt", "Tf", "Ar",
        ):
            self._rules.append((QtCore.QRegularExpression(rf"\b{w}\b"), usd_fmt))

        method_fmt = QtGui.QTextCharFormat()
        method_fmt.setForeground(QtGui.QColor("#56b6c2"))
        self._rules.append((QtCore.QRegularExpression(r"(?<=\.)\w+(?=\()"), method_fmt))


class _ScriptEditor(_CodeEditor):
    """_CodeEditor subclass with rlcompleter-style dynamic completions."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._namespace: dict = {}
        # Reconnect to our override (base connected to its own _insert_completion)
        try:
            self._completer.activated.disconnect()
        except Exception:
            pass
        self._completer.activated.connect(self._insert_completion)

    def set_namespace(self, ns: dict):
        self._namespace = ns

    def _ensure_namespace(self):
        """Lazily populate namespace with stage if not yet set."""
        if self._namespace:
            return
        try:
            from usd_chunks.chunk_registry import ChunkRegistry
            stage = ChunkRegistry().scene.get_stage()
            if stage:
                self._namespace = {"stage": stage, "prim": None, "usdviewApi": None}
        except Exception as e:
            print(f"[ScriptRunner] _ensure_namespace error: {e}")

    def keyPressEvent(self, event: QtGui.QKeyEvent):
        # Tab triggers dynamic completion when no popup visible
        if event.key() == QtCore.Qt.Key_Tab and not self._completer.popup().isVisible():
            self._ensure_namespace()
            line = self.textCursor().block().text()
            col = self.textCursor().positionInBlock()
            # Walk back to find dotted expression (e.g. "stage.Get")
            i = col - 1
            while i >= 0 and (line[i].isalnum() or line[i] in "._"):
                i -= 1
            dotted = line[i + 1 : col]
            self._tab_start = i + 1  # column where dotted expr starts
            print(f"[ScriptRunner] Tab pressed, dotted='{dotted}', ns keys={list(self._namespace.keys())}")

            if dotted:
                completions = self._get_completions(dotted)
                print(f"[ScriptRunner] completions count={len(completions)}")
                if completions:
                    model = QtCore.QStringListModel(completions)
                    self._completer.setModel(model)
                    self._completer.setCompletionPrefix(dotted)
                    cr = self.cursorRect()
                    cr.setWidth(300)
                    self._completer.complete(cr)
                    return
            # Fall through to normal tab (indent)
            self.textCursor().insertText("    ")
            return

        super().keyPressEvent(event)

    def _insert_completion(self, text: str) -> None:
        tc = self.textCursor()
        # Select back to where the dotted expression started
        block_pos = tc.block().position()
        start = block_pos + getattr(self, "_tab_start", tc.positionInBlock())
        tc.setPosition(start, QtGui.QTextCursor.MoveAnchor)
        tc.setPosition(self.textCursor().position(), QtGui.QTextCursor.KeepAnchor)
        tc.insertText(text)
        self.setTextCursor(tc)

    def _get_completions(self, text: str) -> list[str]:
        ns = dict(self._namespace)
        try:
            import pxr
            ns["pxr"] = pxr
            for mod in ("Usd", "UsdGeom", "UsdShade", "Sdf", "Gf", "Vt", "UsdLux"):
                ns.setdefault(mod, getattr(pxr, mod, None))
        except Exception:
            pass

        # Split "stage.Get" → obj_part="stage", prefix="Get"
        # Split "stage" → obj_part=None, prefix="stage"
        if "." in text:
            obj_part, prefix = text.rsplit(".", 1)
            # Resolve the object through chained dots
            parts = obj_part.split(".")
            obj = ns.get(parts[0])
            for p in parts[1:]:
                if obj is None:
                    return []
                obj = getattr(obj, p, None)
            if obj is None:
                return []
            # dir() on the object, filter by prefix
            matches = [f"{obj_part}.{a}" for a in dir(obj)
                       if a.lower().startswith(prefix.lower()) and not a.startswith("__")]
        else:
            # No dot — complete from namespace keys
            matches = [k for k in ns if k.lower().startswith(text.lower())]

        return matches


class _CodeEditorWidget(chunk_core.ChunkParamWidgetBase, QtWidgets.QWidget):
    def __init__(self, param):
        super().__init__(param)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(0)
        self._editor = _ScriptEditor()
        self._editor.setMinimumHeight(200)
        self._editor.setPlaceholderText("# stage, prim, usdviewApi are available\n")
        _UsdPythonHighlighter(self._editor.document())
        layout.addWidget(self._editor)
        self.setup_chunk_widget()

    def get_value(self):
        return self._editor.toPlainText()

    def set_value(self, value):
        if value is not None:
            self._editor.setPlainText(str(value))

    def set_label(self, text: str):
        pass

    def set_param_layout(self, layout_flags):
        pass


def _run(owner, notice, sender):
    _chunk = owner.get_chunk()

    code_param = _chunk.get_param("code")
    code = ""
    widget = None
    if code_param and code_param.custom_widget:
        widget = code_param.custom_widget
        code = widget.get_value()
    if not code.strip():
        return

    stage = owner.get_stage()

    prim = None
    prim_param = _chunk.get_param("prim_path")
    if prim_param:
        prims = prim_param.get_prims()
        if prims:
            prim = prims[0]

    usdview_api = None
    try:
        from scripts.node_graph.graph_host import GraphHost

        scene = GraphHost.current()._scene()
        usdview_api = getattr(scene, "usdviewAPI", None)
    except Exception:
        pass

    exec_globals = {
        "stage": stage,
        "prim": prim,
        "usdviewApi": usdview_api,
    }

    # Update editor namespace so Tab-completions reflect live objects
    if widget:
        widget._editor.set_namespace(exec_globals)

    try:
        exec(compile(code, "<ScriptRunner>", "exec"), exec_globals)
    except Exception:
        print(f"[ScriptRunner] Error:\n{traceback.format_exc()}")


def main():
    chunk_api = chunk.Chunk("Script Runner")

    prim_path = chunk.LineEdit(
        "prim_path",
        "Prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
    )
    chunk_api.add_param(prim_path)

    code_param = chunk.Custom("code")
    code_param.custom_widget = _CodeEditorWidget(code_param)
    chunk_api.add_param(code_param)

    run_btn = chunk.Button("run", "Run")
    run_btn.add_callback(_run)
    chunk_api.add_param(run_btn)

    return chunk_api


def config():
    return chunk.DefaulConfig(category=chunk.StandardCategory.UTILS)
