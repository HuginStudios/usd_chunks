import math

from usd_chunks import chunk, chunk_registry, chunk_utils
from plugins.chunk_usd import ChunkStageSync

Vec3 = tuple


def _add(a: Vec3, b: Vec3) -> Vec3:
    return tuple(x + y for x, y in zip(a, b))


def _sub(a: Vec3, b: Vec3) -> Vec3:
    return tuple(x - y for x, y in zip(a, b))


def _mul(a: Vec3, b: Vec3) -> Vec3:
    return tuple(x * y for x, y in zip(a, b))


def _div(a: Vec3, b: Vec3) -> Vec3:
    return tuple(x / y if y != 0.0 else 0.0 for x, y in zip(a, b))


def _dot(a: Vec3, b: Vec3) -> float:
    return sum(x * y for x, y in zip(a, b))


def _length(a: Vec3) -> float:
    return math.sqrt(_dot(a, a))


def _scale(a: Vec3, s: float) -> Vec3:
    return tuple(x * s for x in a)


def _normalize(a: Vec3) -> Vec3:
    length = _length(a)
    return _scale(a, 1.0 / length) if length > 0.0 else (0.0, 0.0, 0.0)


def _cross(a: Vec3, b: Vec3) -> Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _project(a: Vec3, b: Vec3) -> Vec3:
    d = _dot(b, b)
    return _scale(b, _dot(a, b) / d) if d != 0.0 else (0.0, 0.0, 0.0)


def _reflect(a: Vec3, b: Vec3) -> Vec3:
    n = _normalize(b)
    return _sub(a, _scale(n, 2.0 * _dot(a, n)))


def _refract(a: Vec3, b: Vec3, ior: float) -> Vec3:
    i = _normalize(a)
    n = _normalize(b)
    eta = ior if ior != 0.0 else 1.0
    d = _dot(n, i)
    k = 1.0 - eta * eta * (1.0 - d * d)
    if k < 0.0:
        return (0.0, 0.0, 0.0)
    return _sub(_scale(i, eta), _scale(n, eta * d + math.sqrt(k)))


def _faceforward(vec: Vec3, incident: Vec3, reference: Vec3) -> Vec3:
    return vec if _dot(reference, incident) < 0.0 else _scale(vec, -1.0)


def _distance(a: Vec3, b: Vec3) -> float:
    return _length(_sub(a, b))


def _floor(a: Vec3) -> Vec3:
    return tuple(math.floor(x) for x in a)


def _ceil(a: Vec3) -> Vec3:
    return tuple(math.ceil(x) for x in a)


def _fraction(a: Vec3) -> Vec3:
    return tuple(x - math.floor(x) for x in a)


def _absolute(a: Vec3) -> Vec3:
    return tuple(abs(x) for x in a)


def _minimum(a: Vec3, b: Vec3) -> Vec3:
    return tuple(min(x, y) for x, y in zip(a, b))


def _maximum(a: Vec3, b: Vec3) -> Vec3:
    return tuple(max(x, y) for x, y in zip(a, b))


def _modulo(a: Vec3, b: Vec3) -> Vec3:
    return tuple(math.fmod(x, y) if y != 0.0 else 0.0 for x, y in zip(a, b))


def _wrap_scalar(value: float, min_v: float, max_v: float) -> float:
    if min_v == max_v:
        return min_v
    range_ = max_v - min_v
    return value - range_ * math.floor((value - min_v) / range_)


def _wrap(a: Vec3, max_v: Vec3, min_v: Vec3) -> Vec3:
    return tuple(_wrap_scalar(x, lo, hi) for x, hi, lo in zip(a, max_v, min_v))


def _snap_scalar(value: float, increment: float) -> float:
    return math.floor(value / increment + 0.5) * increment if increment != 0.0 else value


def _snap(a: Vec3, increment: Vec3) -> Vec3:
    return tuple(_snap_scalar(x, s) for x, s in zip(a, increment))


def _sine(a: Vec3) -> Vec3:
    return tuple(math.sin(x) for x in a)


def _cosine(a: Vec3) -> Vec3:
    return tuple(math.cos(x) for x in a)


def _tangent(a: Vec3) -> Vec3:
    return tuple(math.tan(x) for x in a)


# Operation name -> (inputs shown as (param_name, label) pairs, output kind "vector"|"float", func(a, b, c, scale)).
# Mirrors Blender's Geometry Nodes "Vector Math" node.
_OPS: dict = {
    "Add": ((("a", "A"), ("b", "B")), "vector", lambda a, b, c, s: _add(a, b)),
    "Subtract": ((("a", "A"), ("b", "B")), "vector", lambda a, b, c, s: _sub(a, b)),
    "Multiply": ((("a", "A"), ("b", "B")), "vector", lambda a, b, c, s: _mul(a, b)),
    "Divide": ((("a", "A"), ("b", "B")), "vector", lambda a, b, c, s: _div(a, b)),
    "Multiply Add": ((("a", "A"), ("b", "B"), ("c", "C")), "vector", lambda a, b, c, s: _add(_mul(a, b), c)),
    "Cross Product": ((("a", "A"), ("b", "B")), "vector", lambda a, b, c, s: _cross(a, b)),
    "Project": ((("a", "A"), ("b", "B")), "vector", lambda a, b, c, s: _project(a, b)),
    "Reflect": ((("a", "A"), ("b", "B")), "vector", lambda a, b, c, s: _reflect(a, b)),
    "Refract": ((("a", "A"), ("b", "B"), ("scale", "IOR")), "vector", lambda a, b, c, s: _refract(a, b, s)),
    "Faceforward": (
        (("a", "Vector"), ("b", "Incident"), ("c", "Reference")),
        "vector",
        lambda a, b, c, s: _faceforward(a, b, c),
    ),
    "Dot Product": ((("a", "A"), ("b", "B")), "float", lambda a, b, c, s: _dot(a, b)),
    "Distance": ((("a", "A"), ("b", "B")), "float", lambda a, b, c, s: _distance(a, b)),
    "Length": ((("a", "A"),), "float", lambda a, b, c, s: _length(a)),
    "Scale": ((("a", "Vector"), ("scale", "Scale")), "vector", lambda a, b, c, s: _scale(a, s)),
    "Normalize": ((("a", "A"),), "vector", lambda a, b, c, s: _normalize(a)),
    "Absolute": ((("a", "A"),), "vector", lambda a, b, c, s: _absolute(a)),
    "Minimum": ((("a", "A"), ("b", "B")), "vector", lambda a, b, c, s: _minimum(a, b)),
    "Maximum": ((("a", "A"), ("b", "B")), "vector", lambda a, b, c, s: _maximum(a, b)),
    "Floor": ((("a", "A"),), "vector", lambda a, b, c, s: _floor(a)),
    "Ceil": ((("a", "A"),), "vector", lambda a, b, c, s: _ceil(a)),
    "Fraction": ((("a", "A"),), "vector", lambda a, b, c, s: _fraction(a)),
    "Modulo": ((("a", "A"), ("b", "B")), "vector", lambda a, b, c, s: _modulo(a, b)),
    "Wrap": ((("a", "Vector"), ("b", "Max"), ("c", "Min")), "vector", lambda a, b, c, s: _wrap(a, b, c)),
    "Snap": ((("a", "Vector"), ("b", "Increment")), "vector", lambda a, b, c, s: _snap(a, b)),
    "Sine": ((("a", "A"),), "vector", lambda a, b, c, s: _sine(a)),
    "Cosine": ((("a", "A"),), "vector", lambda a, b, c, s: _cosine(a)),
    "Tangent": ((("a", "A"),), "vector", lambda a, b, c, s: _tangent(a)),
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
        ChunkStageSync().clear_attr_connections(attr)


def _clear_matching_params(obj, target: str) -> None:
    """Recursively walk obj's params (into groups/vectors) and clear any whose raw_value
    references target (an "@OwnerName.attrName@" token)."""
    get_children = getattr(obj, "get_my_params", None)
    if not callable(get_children):
        return
    for param in get_children():
        raw = getattr(param, "raw_value", None)
        if isinstance(raw, str) and target in raw:
            _clear_connection(param)
        else:
            _clear_matching_params(param, target)


def _clear_downstream_references(owner: "chunk.Chunk", attr_name: str) -> None:
    """Find every OTHER chunk in the scene with a param currently wired to
    @owner.name.attr_name@ and clear it. Needed because switching this chunk's own
    operation can make one of its two outputs (result vs result_value) inactive —
    execute() stops writing to it, so a downstream connection to it silently goes
    stale instead of erroring, and would reappear untouched if the op is switched
    back even though nothing recomputed it in the meantime."""
    scene = chunk_registry.ChunkRegistry().scene
    if not scene:
        return
    target = f"@{owner.name}.{attr_name}@"
    for other in scene.get_chunks():
        if other is owner:
            continue
        _clear_matching_params(other, target)


def _update_inputs(owner: "chunk.Chunk", operation: str) -> None:
    entry = _OPS.get(operation)
    inputs = entry[0] if entry else ()
    labels = dict(inputs)

    for name in ("a", "b", "c", "scale"):
        param = owner.get_param(name)
        if param is None:
            continue
        if name in labels:
            param.label = labels[name]
            param.unset_state(chunk.ChunkFlags.State.HIDDEN)
        else:
            _clear_connection(param)
            param.set_state(chunk.ChunkFlags.State.HIDDEN)

    is_float_output = entry[1] == "float" if entry else False
    result_vector = owner.get_param("result")
    result_value = owner.get_param("result_value")
    if result_vector is not None:
        if is_float_output:
            if not result_vector.has_state(chunk.ChunkFlags.State.HIDDEN):
                _clear_downstream_references(owner, "result")
            result_vector.set_state(chunk.ChunkFlags.State.HIDDEN)
        else:
            result_vector.unset_state(chunk.ChunkFlags.State.HIDDEN)
    if result_value is not None:
        if is_float_output:
            result_value.unset_state(chunk.ChunkFlags.State.HIDDEN)
        else:
            if not result_value.has_state(chunk.ChunkFlags.State.HIDDEN):
                _clear_downstream_references(owner, "result_value")
            result_value.set_state(chunk.ChunkFlags.State.HIDDEN)


def _operation_changed(owner: "chunk.Chunk", notice, sender: "chunk.ChunkParamBase"):
    _update_inputs(owner, sender.value)


def execute(node):
    op = node.get_param("operation").value or "Add"
    entry = _OPS.get(op)
    if entry is None:
        return

    _, output_kind, func = entry
    a = tuple(float(v) for v in (node.get_param("a").value or (0.0, 0.0, 0.0)))
    b = tuple(float(v) for v in (node.get_param("b").value or (0.0, 0.0, 0.0)))
    c = tuple(float(v) for v in (node.get_param("c").value or (0.0, 0.0, 0.0)))
    scale = float(node.get_param("scale").value or 0.0)

    try:
        result = func(a, b, c, scale)
    except (ValueError, ZeroDivisionError, OverflowError):
        result = (0.0, 0.0, 0.0) if output_kind == "vector" else 0.0

    if output_kind == "vector":
        node.get_param("result").value = result
    else:
        node.get_param("result_value").value = float(result)


def main():
    operation = chunk.Menu(
        "operation",
        "Operation",
        default_value="Add",
        menu_items=list(_OPS.keys()),
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
    )

    vec_flags = chunk.ChunkFlags.GroupFlags.LABELED | chunk.ChunkFlags.GroupFlags.FRAMED

    chunk_api = chunk.Chunk("Vector Math Ops")
    chunk_api.add_params([
        operation,
        chunk.Vector("a", "A", param=chunk.Slider(), length=3, suffixes="XYZ", default_value=(0.0, 0.0, 0.0), flags=vec_flags),
        chunk.Vector("b", "B", param=chunk.Slider(), length=3, suffixes="XYZ", default_value=(0.0, 0.0, 0.0), flags=vec_flags),
        chunk.Vector("c", "C", param=chunk.Slider(), length=3, suffixes="XYZ", default_value=(0.0, 0.0, 0.0), flags=vec_flags),
        chunk.Slider("scale", "Scale", default_value=1.0, flags=chunk.ChunkFlags.ParamFlags.LABELED),
        chunk.Vector(
            "result", "Result",
            param=chunk.Slider(), length=3, suffixes="XYZ", default_value=(0.0, 0.0, 0.0),
            flags=vec_flags, is_output=True,
        ),
        chunk.Slider(
            "result_value", "Result",
            flags=chunk.ChunkFlags.ParamFlags.OUTPUT | chunk.ChunkFlags.ParamFlags.LABELED,
        ),
    ])
    chunk_api.add_callback(_operation_changed, sender=operation)
    _update_inputs(chunk_api, operation.value)

    return chunk_api


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.UTILS,
        description=(
            "Applies a vector math operation to A/B/C (and Scale for Scale/Refract).\n"
            "Inputs shown depend on the selected\n"
            "operation. Outputs a Vector for most operations, or a single float Result for\n"
            "Dot Product, Distance, and Length."
        ),
    )
