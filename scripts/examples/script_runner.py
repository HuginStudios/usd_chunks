"""Script Runner chunk — live Python scripting panel in usdview.

Executes arbitrary Python with stage, prim, and usdviewApi in scope.
"""
from __future__ import annotations

import traceback

from usd_chunks import chunk, chunk_core
from PySide6 import QtWidgets


class _CodeEditorWidget(chunk_core.ChunkParamWidgetBase, QtWidgets.QWidget):
    def __init__(self, param):
        super().__init__(param)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(0)
        self._editor = QtWidgets.QPlainTextEdit()
        self._editor.setMinimumHeight(200)
        self._editor.setPlaceholderText("# stage, prim, usdviewApi are available\n")
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
    if code_param and code_param.custom_widget:
        code = code_param.custom_widget.get_value()
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
