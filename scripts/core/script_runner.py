"""Python chunk — live Python scripting panel in usdview.

Executes arbitrary Python with stage, prim, node, and usdviewApi in scope. Runs like any
other chunk in the node graph's exec chain (via the reserved `execute(node)` hook) rather
than behind a manual Run button, so it re-cooks whenever its own params change or an
upstream node it's wired to changes.

Whatever the script assigns to `result` is exposed on an output param, so it can be wired
into a downstream chunk's input (e.g. a computed prim name feeding a Duplicate Prim chunk's
destination path).

The output is available in four types — string, int, float, bool — each on its OWN output
param, because a USD attribute's value type is fixed when the attribute is created and
cannot be retyped in place: one param cannot be a String one moment and an Int the next.
The "Output Type" menu picks which one the script's `result` is coerced into and populated
on; the other three are cleared. Wire the port whose type matches the downstream input --
a numeric input silently drops a String value (a String into a non-String attr is a type
mismatch USD refuses), which is exactly why the typed outputs exist.
"""
from __future__ import annotations

import logging
import traceback

from usd_chunks import chunk
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks import chunk_registry

logger = logging.getLogger(__name__)


# Output-type menu value -> (result param name, coercion). The coercion turns whatever the
# script assigned into the Python type the matching OUTPUT param stores; a failure leaves
# that output empty rather than raising, keeping a bad script from crashing the cook.
def _coerce_str(v):
    return "" if v is None else str(v)


def _coerce_int(v):
    return int(v)


def _coerce_float(v):
    return float(v)


def _coerce_bool(v):
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


_OUTPUTS = {
    "string": ("result", _coerce_str),
    "int": ("result_int", _coerce_int),
    "float": ("result_float", _coerce_float),
    "bool": ("result_bool", _coerce_bool),
}


def _get_usdview_api():
    try:
        scene = chunk_registry.ChunkRegistry().scene
        if hasattr(scene, "usdviewAPI"):
            return scene.usdviewAPI
    except Exception:
        pass
    return None


# The empty value each output falls back to. A float output blanks to 0.0, not 0: the
# node graph reads a port's colour off the param's CURRENT value
# (chunk_qt_nodegraph._param_port_type), so an int here paints the float port with the int
# colour until someone assigns a real float to it.
_EMPTY = {
    "result": "",
    "result_int": 0,
    "result_float": 0.0,
    "result_bool": False,
}


def _empty_for(name):
    return _EMPTY.get(name, "")


def _clear_outputs(chunk_obj, keep=None):
    """Blank every output param except `keep`.

    Only the selected type carries a value; the rest are reset to their empty/zero default
    so a downstream wire never reads a stale value left by a previous type selection.
    """
    for name, _ in _OUTPUTS.values():
        if name == keep:
            continue
        param = chunk_obj.get_param(name)
        if param is None:
            continue
        param.value = _empty_for(name)


def _show_only_selected_output(chunk_obj):
    """Leave just the output for the selected type on show.

    Four typed outputs is what the port list needs; it is not what the panel needs. Only
    one of them ever carries a value, so the other three are noise on the node and three
    more ports to mis-wire. Hiding them keeps the visible output matched to output_type.
    """
    type_param = chunk_obj.get_param("output_type")
    out_type = str(type_param.value) if type_param and type_param.value else "string"
    keep, _ = _OUTPUTS.get(out_type, _OUTPUTS["string"])

    for name, _ in _OUTPUTS.values():
        param = chunk_obj.get_param(name)
        if param is None:
            continue
        if name == keep:
            param.unset_state(ChunkFlags.State.HIDDEN)
        else:
            param.set_state(ChunkFlags.State.HIDDEN)


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

    type_param = chunk_obj.get_param("output_type")
    out_type = str(type_param.value) if type_param and type_param.value else "string"
    out_name, coerce = _OUTPUTS.get(out_type, _OUTPUTS["string"])
    result_param = chunk_obj.get_param(out_name)

    # An empty script is the user clearing it out -- that genuinely means "nothing to
    # output", not a transient state to preserve the last result through.
    if not code.strip():
        _clear_outputs(chunk_obj)
        if result_param:
            result_param.value = _empty_for(out_name)
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
        logger.error("Error:\n%s", traceback.format_exc())
        return

    if result_param:
        value = exec_globals.get("result")
        # Populate only the output whose type is selected; clear the rest so a downstream
        # wire never reads a value left over from a previous type choice.
        _clear_outputs(chunk_obj, keep=out_name)
        if value is None:
            result_param.value = _empty_for(out_name)
        else:
            try:
                result_param.value = coerce(value)
            except (TypeError, ValueError):
                # The script produced something that can't become the chosen type (e.g.
                # "int" selected but result is "abc"). Leave the output empty and say so,
                # rather than raising and aborting the whole cook.
                logger.warning("result %r is not a valid %s", value, out_type)
                result_param.value = _empty_for(out_name)


def execute(node):
    # Also on the exec-chain cook, so a reopened scene shows the output matching its saved
    # output_type without anyone having to touch the menu first.
    _show_only_selected_output(node)
    _run_script(node)


def _on_change(owner, notice, sender):
    """Callback for the chunk panel -- run the script when used outside the node graph,
    where the reserved `execute` hook isn't reached (a chunk not part of an active view's
    upstream tree is blocked from the generic exec dispatch)."""
    chunk_obj = owner.get_chunk()
    if chunk_obj is None:
        return
    _show_only_selected_output(chunk_obj)
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

    output_type = chunk.Menu(
        "output_type",
        "Output Type",
        default_value="string",
        menu_items=["string", "int", "float", "bool"],
        flags=ChunkFlags.ParamFlags.LABELED,
        tooltip="Which typed output the script's `result` is coerced onto. Wire the port "
                "whose type matches the downstream input.",
    )
    # Changing the type has to re-cook so `result` lands on the newly-selected output
    # (and the old one is cleared).
    output_type.add_callback(_on_change)
    chunk_api.add_param(output_type)

    result = chunk.LineEdit(
        "result",
        "Result (string)",
        default_value="",
        flags=ChunkFlags.ParamFlags.LABELED | ChunkFlags.ParamFlags.OUTPUT,
        tooltip="str(result). Active when Output Type is 'string'.",
    )
    chunk_api.add_param(result)

    result_int = chunk.Slider(
        "result_int",
        "Result (int)",
        default_value=0,
        is_int=True,
        flags=ChunkFlags.ParamFlags.LABELED | ChunkFlags.ParamFlags.OUTPUT,
        tooltip="int(result). Active when Output Type is 'int'.",
    )
    chunk_api.add_param(result_int)

    result_float = chunk.Slider(
        "result_float",
        "Result (float)",
        default_value=0.0,
        flags=ChunkFlags.ParamFlags.LABELED | ChunkFlags.ParamFlags.OUTPUT,
        tooltip="float(result). Active when Output Type is 'float'.",
    )
    chunk_api.add_param(result_float)

    result_bool = chunk.Checkbox(
        "result_bool",
        "Result (bool)",
        default_value=False,
        flags=ChunkFlags.ParamFlags.LABELED | ChunkFlags.ParamFlags.OUTPUT,
        tooltip="bool(result). Active when Output Type is 'bool'.",
    )
    chunk_api.add_param(result_bool)

    return chunk_api


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.CORE,
        description="""
        Python — runs a script with stage, prim, node, and usdviewApi in scope.

        Parameters:
          - Prim: optional prim input (viewport picking or a wired-in path).
          - Code: the script to run. Set the `result` variable to expose a value.
          - Output Type: string | int | float | bool. Picks which output `result`
            is coerced onto.
          - Result (string/int/float/bool): read-only outputs. Only the one matching
            Output Type carries a value; the rest are cleared. Wire the port whose
            type matches the downstream input (a String value won't flow into a
            numeric input, which is why the typed outputs exist).

        Runs automatically like any other node in the exec chain: on its own
        param changes, and on anything upstream it's wired to. No Run button.
        """,
    )
