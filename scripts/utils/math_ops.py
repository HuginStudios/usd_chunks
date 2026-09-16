import math

from usd_chunks import chunk, chunk_utils
from usd_chunks.chunk_stage import clear_attr_connections


def _safe_div(a: float, b: float) -> float:
    return a / b if b != 0.0 else 0.0


def _wrap(value: float, min_v: float, max_v: float) -> float:
    if min_v == max_v:
        return min_v
    range_ = max_v - min_v
    return value - range_ * math.floor((value - min_v) / range_)


def _snap(value: float, increment: float) -> float:
    return math.floor(value / increment + 0.5) * increment if increment != 0.0 else value


def _pingpong(value: float, scale: float) -> float:
    if scale == 0.0:
        return 0.0
    value = math.fmod(value, scale * 2.0)
    if value < 0.0:
        value += scale * 2.0
    return value if value <= scale else scale * 2.0 - value


def _sign(value: float) -> float:
    return float((value > 0.0) - (value < 0.0))


# Operation name -> (labels shown for a/b/c, in order; function taking (a, b, c)).
# Mirrors Blender's Geometry Nodes "Math" node, grouped the same way it is there.
_OPS: dict = {
    # Basic Arithmetic
    "Add": (("A", "B"), lambda a, b, c: a + b),
    "Subtract": (("A", "B"), lambda a, b, c: a - b),
    "Multiply": (("A", "B"), lambda a, b, c: a * b),
    "Divide": (("A", "B"), lambda a, b, c: _safe_div(a, b)),
    "Multiply Add": (("A", "B", "C"), lambda a, b, c: a * b + c),
    "Power": (("Base", "Exponent"), lambda a, b, c: a**b),

    # Exponents & Logs
    "Logarithm": (("A", "Base"), lambda a, b, c: math.log(a, b) if a > 0.0 and b > 0.0 and b != 1.0 else 0.0),
    "Square Root": (("A",), lambda a, b, c: math.sqrt(a) if a >= 0.0 else 0.0),
    "Inverse Square Root": (("A",), lambda a, b, c: 1.0 / math.sqrt(a) if a > 0.0 else 0.0),
    "Absolute": (("A",), lambda a, b, c: abs(a)),

    # Trimming & Rounding
    "Round": (("A",), lambda a, b, c: math.floor(a + 0.5)),
    "Floor": (("A",), lambda a, b, c: math.floor(a)),
    "Ceil": (("A",), lambda a, b, c: math.ceil(a)),
    "Truncate": (("A",), lambda a, b, c: math.trunc(a)),
    "Fraction": (("A",), lambda a, b, c: a - math.floor(a)),
    "Modulo": (("A", "B"), lambda a, b, c: math.fmod(a, b) if b != 0.0 else 0.0),
    "Wrap": (("Value", "Min", "Max"), lambda a, b, c: _wrap(a, b, c)),
    "Snap": (("Value", "Increment"), lambda a, b, c: _snap(a, b)),
    "Ping-pong": (("Value", "Scale"), lambda a, b, c: _pingpong(a, b)),

    # Comparison
    "Compare": (("A", "B", "Epsilon"), lambda a, b, c: 1.0 if abs(a - b) <= c else 0.0),
    "Minimum": (("A", "B"), lambda a, b, c: min(a, b)),
    "Maximum": (("A", "B"), lambda a, b, c: max(a, b)),
    "Less Than": (("A", "B"), lambda a, b, c: 1.0 if a < b else 0.0),
    "Greater Than": (("A", "B"), lambda a, b, c: 1.0 if a > b else 0.0),
    "Sign": (("A",), lambda a, b, c: _sign(a)),

    # Trigonometry
    "Sine": (("A",), lambda a, b, c: math.sin(a)),
    "Cosine": (("A",), lambda a, b, c: math.cos(a)),
    "Tangent": (("A",), lambda a, b, c: math.tan(a)),
    "Arcsine": (("A",), lambda a, b, c: math.asin(a) if -1.0 <= a <= 1.0 else 0.0),
    "Arccosine": (("A",), lambda a, b, c: math.acos(a) if -1.0 <= a <= 1.0 else 0.0),
    "Arctangent": (("A",), lambda a, b, c: math.atan(a)),
    "Arctan2": (("A", "B"), lambda a, b, c: math.atan2(a, b)),
    "Sinh": (("A",), lambda a, b, c: math.sinh(a)),
    "Cosh": (("A",), lambda a, b, c: math.cosh(a)),
    "Tanh": (("A",), lambda a, b, c: math.tanh(a)),
    "To Radians": (("A",), lambda a, b, c: math.radians(a)),
    "To Degrees": (("A",), lambda a, b, c: math.degrees(a)),
}


def _clear_connection(param: "chunk.ChunkParamBase") -> None:
    """Reset param to its default and drop any USD-level connection. A param hidden by
    the current operation is no longer meaningful — keeping a stale connection around
    would silently reappear (with possibly wrong semantics) if the op is switched back."""
    if param is None:
        return
    raw = getattr(param, "raw_value", None)
    if not (isinstance(raw, str) and chunk_utils.has_expression(raw)):
        return
    param.value = param.default_value
    if attr := param.get_usd_prop():
        clear_attr_connections(attr)


def _update_inputs(owner: "chunk.Chunk", operation: str) -> None:
    labels = _OPS.get(operation, ((), None))[0]
    for i, name in enumerate(("a", "b", "c")):
        param = owner.get_param(name)
        if param is None:
            continue
        if i < len(labels):
            param.label = labels[i]
            param.unset_state(chunk.ChunkFlags.State.HIDDEN)
        else:
            _clear_connection(param)
            param.set_state(chunk.ChunkFlags.State.HIDDEN)


def _operation_changed(owner: "chunk.Chunk", notice, sender: "chunk.ChunkParamBase"):
    _update_inputs(owner, sender.value)


def execute(node):
    op = node.get_param("operation").value or "Add"
    entry = _OPS.get(op)
    if entry is None:
        return

    a = float(node.get_param("a").value or 0.0)
    b = float(node.get_param("b").value or 0.0)
    c = float(node.get_param("c").value or 0.0)

    try:
        result = float(entry[1](a, b, c))
    except (ValueError, ZeroDivisionError, OverflowError):
        result = 0.0

    if node.get_param("clamp").value:
        result = max(0.0, min(1.0, result))

    node.get_param("result").value = result


def main():
    operation = chunk.Menu(
        "operation",
        "Operation",
        default_value="Add",
        menu_items=list(_OPS.keys()),
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
    )

    chunk_api = chunk.Chunk("Math Ops")
    chunk_api.add_params([
        operation,
        chunk.Slider("a", "A", default_value=0.0, flags=chunk.ChunkFlags.ParamFlags.LABELED),
        chunk.Slider("b", "B", default_value=0.0, flags=chunk.ChunkFlags.ParamFlags.LABELED),
        chunk.Slider("c", "C", default_value=0.0, flags=chunk.ChunkFlags.ParamFlags.LABELED),
        chunk.Checkbox("clamp", "Clamp", default_value=False, flags=chunk.ChunkFlags.ParamFlags.LABELED),
        chunk.Slider("result", "Result", flags=chunk.ChunkFlags.ParamFlags.OUTPUT),
    ])
    chunk_api.add_callback(_operation_changed, sender=operation)
    _update_inputs(chunk_api, operation.value)

    return chunk_api


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.UTILS + "/math",
        # Every operation gets its own menu entry, so "Sine" is searchable and creatable
        # directly instead of being reachable only by creating Math Ops and re-picking the
        # operation. Each still creates this one chunk -- the entry just presets the param.
        variants=[(op, {"operation": op}) for op in _OPS],
        description=(
            "Applies a math operation to A/B/C, matching Blender's Geometry Nodes Math node.\n"
            "Inputs shown depend on the selected operation's arity (1 to 3).\n"
            "Clamp restricts the result to the 0..1 range."
        ),
    )
