"""Python chunk — live Python scripting panel in usdview.

Executes arbitrary Python with stage, prim, node, and usdviewApi in scope. Runs like any
other chunk in the node graph's exec chain (via the reserved `execute(node)` hook) rather
than behind a manual Run button, so it re-cooks whenever its own params change or an
upstream node it's wired to changes.

Whatever the script assigns to `result` is exposed on the `result` output param, so it can
be wired into a downstream chunk's input (e.g. a computed prim name feeding a Duplicate
Prim chunk's destination path).
"""
from __future__ import annotations

import traceback

from usd_chunks import chunk
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks import chunk_registry


def _get_usdview_api():
    try:
        scene = chunk_registry.ChunkRegistry().scene
        if hasattr(scene, "usdviewAPI"):
            return scene.usdviewAPI
    except Exception:
        pass
    return None


def _run_script(chunk_obj):
    stage = chunk_obj.get_stage()
    if not stage:
        return

    layer = chunk_obj.get_edit_context()
    if not layer:
        return
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)

    code_param = chunk_obj.get_param("code")
    code = str(code_param.value) if code_param and code_param.value else ""

    result_param = chunk_obj.get_param("result")

    # An empty script is the user clearing it out -- that genuinely means "nothing to
    # output", not a transient state to preserve the last result through.
    if not code.strip():
        if result_param:
            result_param.value = ""
        return

    prim = None
    prim_param = chunk_obj.get_param("prim_path")
    if prim_param:
        prims = prim_param.get_prims()
        if prims:
            prim = prims[0]

    exec_globals = {
        "stage": stage,
        "prim": prim,
        "node": chunk_obj,
        "usdviewApi": _get_usdview_api(),
        "result": None,
    }

    try:
        exec(compile(code, "<Python>", "exec"), exec_globals)
    except Exception:
        print(f"[Python] Error:\n{traceback.format_exc()}")
        return

    if result_param:
        value = exec_globals.get("result")
        result_param.value = "" if value is None else str(value)


def execute(node):
    _run_script(node)


def _on_change(owner, notice, sender):
    """Callback for the chunk panel -- run the script when used outside the node graph,
    where the reserved `execute` hook isn't reached (a chunk not part of an active view's
    upstream tree is blocked from the generic exec dispatch)."""
    chunk_obj = owner.get_chunk()
    if chunk_obj is None:
        return
    _run_script(chunk_obj)


def main():
    chunk_api = chunk.Chunk("Python")

    prim_path = chunk.LineEdit(
        "prim_path",
        "Prim",
        flags=ChunkFlags.ParamFlags.LABELED | ChunkFlags.ParamFlags.PRIM_SELECTION,
    )
    prim_path.add_callback(_on_change)
    chunk_api.add_param(prim_path)

    code = chunk.TextEdit(
        "code",
        default_value="# stage, prim, node, usdviewApi are available\n# set `result` to expose a value downstream\n",
        rows=12,
    )
    code.set_flag(ChunkFlags.ParamFlags.PY_SYNTAX_HIGHLIGHT)
    code.add_callback(_on_change)
    chunk_api.add_param(code)

    result = chunk.LineEdit(
        "result",
        "Result",
        default_value="",
        flags=ChunkFlags.ParamFlags.LABELED | ChunkFlags.ParamFlags.OUTPUT,
        tooltip="Whatever the script assigns to `result`, stringified. Wire this into a "
                "downstream chunk's input to use it.",
    )
    chunk_api.add_param(result)

    return chunk_api


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.UTILS,
        description="""
        Python — runs a script with stage, prim, node, and usdviewApi in scope.

        Parameters:
          - Prim: optional prim input (viewport picking or a wired-in path).
          - Code: the script to run. Set the `result` variable to expose a value
            on this chunk's Result output.
          - Result: read-only output -- str(result), or empty if the script
            didn't set it. Wire it into another chunk's input.

        Runs automatically like any other node in the exec chain: on its own
        param changes, and on anything upstream it's wired to. No Run button.
        """,
    )
