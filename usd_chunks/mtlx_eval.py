"""Evaluate a MaterialX shading network in USD, per surface sample, with numpy.

A surface sample needs the value a material *shows* at that point: the base colour after
the network's texture, tint, hue/saturation and mix nodes, not just the texture file a
UsdPreviewSurface fallback points at. Exporters (Blender's among them) write the real look
as a MaterialX graph and a simplified UsdPreviewSurface beside it -- and the two disagree
whenever the graph does anything to the texture: a sofa's fabric tinted toward white came
out blue from the preview's raw texture, a tree's darkened leaves bright green.

`compile_input(shader_input)` turns one connected shader input into a `Program`: a function
of the per-sample primvars it reads (`Program.primvars`) that returns one value per sample.
Only the node types listed in `_NODES` are understood -- what DCC exporters actually emit
for texturing (images, texcoords, maths, mixes, hsv adjustment, conversions). A graph with
any other node raises `Unsupported` at compile time, and the caller falls back to the
preview surface: a partial evaluation would be wrong without saying so.

Values are numpy arrays of shape (n, k) -- k = 1 for float, 2/3/4 for vectors and colours.
"""

from __future__ import annotations

import logging
from typing import NamedTuple
from collections.abc import Callable

import numpy as np
from pxr import Sdf, UsdShade

logger = logging.getLogger(__name__)


class Unsupported(Exception):
    """The network uses something this evaluator can't compute."""


class Program(NamedTuple):
    """One compiled input: `run(primvars, images, n) -> (n, k) float array`."""

    run: Callable
    primvars: frozenset  # names of the 2-vector primvars it reads (texture coordinates)
    width: int  # k


# ---------------------------------------------------------------------------
# value helpers
# ---------------------------------------------------------------------------


_WIDTH = {
    "float": 1,
    "integer": 1,
    "boolean": 1,
    "vector2": 2,
    "vector3": 3,
    "vector4": 4,
    "color3": 3,
    "color4": 4,
}


def _type_of(shader_id: str) -> str:
    """The output type in a node definition name: ND_mix_color3 -> color3."""
    for name in sorted(_WIDTH, key=len, reverse=True):
        if shader_id.endswith("_" + name) or f"_{name}_" in shader_id:
            return name
    return "float"


def _constant(value, width):
    array = np.asarray(value if value is not None else 0.0, dtype=np.float64).reshape(-1)
    if array.size == 1 and width > 1:
        array = np.repeat(array, width)
    array = array[:width] if array.size >= width else np.pad(array, (0, width - array.size))

    def run(primvars, images, n):
        return np.broadcast_to(array, (n, width))

    return Program(run, frozenset(), width)


def _fit(values, width):
    """Pad with zeros or truncate the last axis to `width`, and broadcast a 1-wide value."""
    k = values.shape[1]
    if k == width:
        return values
    if k == 1:
        return np.repeat(values, width, axis=1)
    if k > width:
        return values[:, :width]
    pad = np.zeros((values.shape[0], width - k))
    if width == 4 and k == 3:
        pad[:] = 1.0  # colour3 -> colour4: opaque alpha
    return np.concatenate([values, pad], axis=1)


def srgb_to_linear(values):
    return np.where(values <= 0.04045, values / 12.92, ((values + 0.055) / 1.055) ** 2.4)


def _rgb_to_hsv(rgb):
    r, g, b = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    high = rgb.max(axis=1)
    low = rgb.min(axis=1)
    delta = high - low
    safe = np.where(delta > 0, delta, 1.0)
    hue = np.where(
        high == r, ((g - b) / safe) % 6.0, np.where(high == g, (b - r) / safe + 2.0, (r - g) / safe + 4.0)
    )
    hue = np.where(delta > 0, hue / 6.0, 0.0)
    saturation = np.where(high > 0, delta / np.where(high > 0, high, 1.0), 0.0)
    return np.stack([hue, saturation, high], axis=1)


def _hsv_to_rgb(hsv):
    h, s, v = hsv[:, 0] % 1.0, hsv[:, 1], hsv[:, 2]
    i = np.floor(h * 6.0)
    f = h * 6.0 - i
    p, q, t = v * (1 - s), v * (1 - f * s), v * (1 - (1 - f) * s)
    i = i.astype(np.int64) % 6
    r = np.choose(i, [v, q, p, p, t, v])
    g = np.choose(i, [t, v, v, q, p, p])
    b = np.choose(i, [p, p, t, v, v, q])
    return np.stack([r, g, b], axis=1)


def _rotate(vectors, degrees, axis):
    """Rodrigues rotation of (n, 3) vectors by per-sample angles about per-sample axes."""
    angle = np.radians(degrees[:, 0])
    k = axis / np.maximum(np.linalg.norm(axis, axis=1, keepdims=True), 1e-12)
    cos, sin = np.cos(angle)[:, None], np.sin(angle)[:, None]
    return vectors * cos + np.cross(k, vectors) * sin + k * np.sum(k * vectors, axis=1, keepdims=True) * (1 - cos)


# ---------------------------------------------------------------------------
# nodes: (shader, compiled inputs, output type) -> run function
# ---------------------------------------------------------------------------


def _binary(op):
    def make(shader, inp, out):
        a, b = inp("in1"), inp("in2")
        width = _WIDTH[out]

        def run(primvars, images, n):
            return op(_fit(a.run(primvars, images, n), width), _fit(b.run(primvars, images, n), width))

        return run

    return make


def _unary(op):
    def make(shader, inp, out):
        a = inp("in")
        width = _WIDTH[out]

        def run(primvars, images, n):
            return op(_fit(a.run(primvars, images, n), width))

        return run

    return make


def _mix(shader, inp, out):
    fg, bg, amount = inp("fg"), inp("bg"), inp("mix")
    width = _WIDTH[out]

    def run(primvars, images, n):
        m = _fit(amount.run(primvars, images, n), 1)
        return _fit(bg.run(primvars, images, n), width) * (1 - m) + _fit(fg.run(primvars, images, n), width) * m

    return run


def _clamp(shader, inp, out):
    value, low, high = inp("in"), inp("low", 0.0), inp("high", 1.0)
    width = _WIDTH[out]

    def run(primvars, images, n):
        return np.clip(
            _fit(value.run(primvars, images, n), width),
            _fit(low.run(primvars, images, n), width),
            _fit(high.run(primvars, images, n), width),
        )

    return run


def _convert(shader, inp, out):
    source = inp("in")
    width = _WIDTH[out]

    def run(primvars, images, n):
        return _fit(source.run(primvars, images, n), width)

    return run


def _extract(shader, inp, out):
    source = inp("in")
    index = int(np.asarray(shader.GetInput("index").Get() if shader.GetInput("index") else 0))

    def run(primvars, images, n):
        values = source.run(primvars, images, n)
        return values[:, index : index + 1]

    return run


def _combine(shader, inp, out):
    width = _WIDTH[out]
    parts = [inp(f"in{i + 1}") for i in range(width)]

    def run(primvars, images, n):
        return np.concatenate([_fit(p.run(primvars, images, n), 1) for p in parts], axis=1)

    return run


def _hsvadjust(shader, inp, out):
    source, amount = inp("in"), inp("amount", (0.0, 1.0, 1.0))
    width = _WIDTH[out]

    def run(primvars, images, n):
        colour = _fit(source.run(primvars, images, n), width)
        shift = _fit(amount.run(primvars, images, n), 3)
        hsv = _rgb_to_hsv(colour[:, :3])
        hsv[:, 0] += shift[:, 0]
        hsv[:, 1] *= shift[:, 1]
        hsv[:, 2] *= shift[:, 2]
        rgb = _hsv_to_rgb(hsv)
        return np.concatenate([rgb, colour[:, 3:]], axis=1)

    return run


def _rotate3d(shader, inp, out):
    source, amount, axis = inp("in"), inp("amount", 0.0), inp("axis", (0.0, 1.0, 0.0))

    def run(primvars, images, n):
        return _rotate(
            _fit(source.run(primvars, images, n), 3), _fit(amount.run(primvars, images, n), 1), _fit(axis.run(primvars, images, n), 3)
        )

    return run


def _texcoord(shader, inp, out):
    index = int(shader.GetInput("index").Get() or 0) if shader.GetInput("index") else 0
    name = "st" if index == 0 else f"st{index}"
    return _primvar_reader(name, _WIDTH[out])


def _geompropvalue(shader, inp, out):
    prop = shader.GetInput("geomprop")
    name = str(prop.Get() or "st") if prop else "st"
    if _WIDTH[out] != 2:
        raise Unsupported(f"geompropvalue of type {out}")
    return _primvar_reader(name, 2)


def _primvar_reader(name, width):
    def run(primvars, images, n):
        values = primvars.get(name)
        if values is None:
            return np.zeros((n, width))
        return _fit(values, width)

    run.reads = name  # picked up by _compile to record the dependency
    return run


def _image(shader, inp, out):
    file_input = shader.GetInput("file")
    asset = file_input.Get() if file_input else None
    path = (asset.resolvedPath or asset.path) if asset else ""
    if not path or "<UDIM>" in path:
        raise Unsupported(f"image without a readable file: {path!r}")
    space = file_input.GetAttr().GetColorSpace()
    width = _WIDTH[out]
    # The file's own colorSpace decides, as it does for the renderer: Blender tags colour
    # images (even roughness ones) srgb_texture and vector images nothing, i.e. linear.
    linearize = space in ("srgb_texture", "sRGB", "srgb")
    coords = inp("texcoord", None)
    if coords is None:
        coords = Program(_primvar_reader("st", 2), frozenset({"st"}), 2)
    default = inp("default", 0.0)
    wrap_u = str(shader.GetInput("uaddressmode").Get() or "periodic") if shader.GetInput("uaddressmode") else "periodic"
    wrap_v = str(shader.GetInput("vaddressmode").Get() or "periodic") if shader.GetInput("vaddressmode") else "periodic"

    def run(primvars, images, n):
        st = _fit(coords.run(primvars, images, n), 2)
        texels = images.texels(path, st, wrap_u, wrap_v)
        if texels is None:
            return _fit(default.run(primvars, images, n), width)
        rgba = texels.astype(np.float64)
        if linearize:
            rgba[:, :3] = srgb_to_linear(rgba[:, :3])
        if width == 1:
            return rgba[:, :1]
        return rgba[:, :width]

    return run


_NODES = {
    "image": _image,
    "texcoord": _texcoord,
    "geompropvalue": _geompropvalue,
    "add": _binary(np.add),
    "subtract": _binary(np.subtract),
    "multiply": _binary(np.multiply),
    "divide": _binary(lambda a, b: np.divide(a, np.where(b == 0, 1.0, b))),
    "max": _binary(np.maximum),
    "min": _binary(np.minimum),
    "power": _binary(lambda a, b: np.power(np.maximum(a, 0.0), b)),
    "mix": _mix,
    "clamp": _clamp,
    "convert": _convert,
    "extract": _extract,
    "combine2": _combine,
    "combine3": _combine,
    "combine4": _combine,
    "hsvadjust": _hsvadjust,
    "rotate3d": _rotate3d,
    "invert": _unary(lambda a: 1.0 - a),
    "absval": _unary(np.abs),
    "dot": _unary(lambda a: a),  # MaterialX's pass-through node, not a dot product
}


def _node_kind(shader_id: str) -> str:
    """ND_mix_color3 -> mix; ND_convert_color4_color3 -> convert; ND_image_color4 -> image."""
    body = shader_id[3:] if shader_id.startswith("ND_") else shader_id
    for kind in sorted(_NODES, key=len, reverse=True):
        if body == kind or body.startswith(kind + "_"):
            return kind
    return body


# ---------------------------------------------------------------------------
# compiling a connection into a Program
# ---------------------------------------------------------------------------


def _source_of(shader_input):
    """What feeds an input: (prim, output name, is an output) or None for a value."""
    if not shader_input or not shader_input.HasConnectedSource():
        return None
    sources, _invalid = shader_input.GetConnectedSources()
    if not sources:
        return None
    info = sources[0]
    return info.source.GetPrim(), str(info.sourceName), info.sourceType == UsdShade.AttributeType.Output


def compile_input(shader_input, _cache=None) -> Program | None:
    """The Program computing `shader_input`, or None if it holds a plain value.

    Raises Unsupported if the network uses a node this module doesn't evaluate.
    """
    cache = {} if _cache is None else _cache
    source = _source_of(shader_input)
    if source is None:
        return None
    return _compile_source(source, cache)


def _compile_source(source, cache) -> Program:
    prim, name, is_output = source
    key = (str(prim.GetPath()), name, is_output)
    if key in cache:
        return cache[key]

    if prim.IsA(UsdShade.NodeGraph) and not prim.IsA(UsdShade.Material):
        graph = UsdShade.NodeGraph(prim)
        port = graph.GetOutput(name) if is_output else graph.GetInput(name)
        program = _through(port, cache)
    elif prim.IsA(UsdShade.Material):
        port = UsdShade.Material(prim).GetInput(name)  # a material's interface input
        program = _through(port, cache)
    else:
        program = _compile_node(UsdShade.Shader(prim), cache)
    cache[key] = program
    return program


def _through(port, cache) -> Program:
    """A node graph's output or interface input: whatever it connects to, or its value."""
    if port is None:
        raise Unsupported("missing node graph port")
    upstream = _source_of(port)
    if upstream is not None:
        return _compile_source(upstream, cache)
    value = port.Get()
    return _constant(value, _width_of_value(value))


def _width_of_value(value):
    return int(np.asarray(value if value is not None else 0.0).size) or 1


def _compile_node(shader, cache) -> Program:
    shader_id = str(shader.GetIdAttr().Get() or "")
    kind = _node_kind(shader_id)
    maker = _NODES.get(kind)
    if maker is None:
        raise Unsupported(f"node {shader_id}")
    out = _type_of(shader_id)
    reads: set = set()

    def inp(name, default=Ellipsis):
        port = shader.GetInput(name)
        upstream = _source_of(port) if port else None
        if upstream is not None:
            program = _compile_source(upstream, cache)
            reads.update(program.primvars)
            return program
        if port is not None and port.Get() is not None:
            value = port.Get()
            return _constant(value, _width_of_value(value) if not isinstance(value, Sdf.AssetPath) else 1)
        if default is Ellipsis:
            return _constant(0.0, 1)
        if default is None:
            return None
        return _constant(default, _width_of_value(default))

    run = maker(shader, inp, out)
    if getattr(run, "reads", None):
        reads.add(run.reads)
    return Program(run, frozenset(reads), _WIDTH[out])
