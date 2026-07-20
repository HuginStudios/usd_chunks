"""Python chunk — live Python scripting panel in usdview.

Executes arbitrary Python with stage, prim, and usdviewApi in scope.
"""
from __future__ import annotations

import traceback

from usd_chunks import chunk
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks import chunk_registry


def _run(owner, notice, sender):
    _chunk = owner.get_chunk()

    code_param = _chunk.get_param("code")
    code = str(code_param.value) if code_param and code_param.value else ""
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
        scene = chunk_registry.ChunkRegistry().scene
        if hasattr(scene, "usdviewAPI"):
            usdview_api = scene.usdviewAPI
    except Exception:
        pass

    exec_globals = {
        "stage": stage,
        "prim": prim,
        "usdviewApi": usdview_api,
    }

    try:
        exec(compile(code, "<Python>", "exec"), exec_globals)
    except Exception:
        print(f"[Python] Error:\n{traceback.format_exc()}")


def main():
    chunk_api = chunk.Chunk("Python")

    prim_path = chunk.LineEdit(
        "prim_path",
        "Prim",
        flags=ChunkFlags.ParamFlags.LABELED | ChunkFlags.ParamFlags.PRIM_SELECTION,
    )
    chunk_api.add_param(prim_path)

    code = chunk.TextEdit("code", default_value="# stage, prim, usdviewApi are available\n", rows=12)
    code.set_flag(ChunkFlags.ParamFlags.PY_SYNTAX_HIGHLIGHT)
    chunk_api.add_param(code)

    run_btn = chunk.Button("run", "Run")
    run_btn.add_callback(_run)
    chunk_api.add_param(run_btn)

    return chunk_api


def config():
    return chunk.DefaulConfig(category=chunk.StandardCategory.UTILS)
