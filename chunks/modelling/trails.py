"""Trails chunk -- the path each point took over time, as curves.

Houdini's Trail SOP: take anything whose points move over frames -- a Surface Flow, an RBD
bake, a Scatter's instancer animated by something else -- and draw one curve per point through
where it has been. The input is only read, never changed, and the output is a plain
``BasisCurves`` with ``widths``, so Curves To Mesh can sweep it into a tube that renders
anywhere, or a curve shader can draw it directly.

* **Min Step** drops samples closer together than this, so a point that sat still for twenty
  frames adds one vertex, not twenty.
* **Taper** thins the start of each trail relative to its head: a run of water is a thread
  behind the drop that made it.
* **Grow** makes the curves themselves animated -- at each frame a trail reaches only as far
  as its point has got -- so the runs draw themselves on screen.

Requires: pxr (OpenUSD), usd_chunks, numpy.
"""

from __future__ import annotations

import numpy as np
from pxr import Usd, UsdGeom, Vt

from usd_chunks import chunk, chunk_stage, sim_bake, chunk_utils


def _report(node, text: str) -> None:
    if (readout := node.get_param("readout")) is not None and readout.value != text:
        readout.value = text


def _points_attr(prim):
    if prim.IsA(UsdGeom.PointInstancer):
        return UsdGeom.PointInstancer(prim).GetPositionsAttr()
    if prim.IsA(UsdGeom.PointBased):
        return UsdGeom.PointBased(prim).GetPointsAttr()
    return None


def _widths(prim, time, count):
    """Per-point widths the source carries, or None."""
    if prim.IsA(UsdGeom.Points):
        w = UsdGeom.Points(prim).GetWidthsAttr().Get(time)
        if w is not None and len(w) == count:
            return np.asarray(w, dtype=np.float64)
        if w is not None and len(w) == 1:
            return np.full(count, float(w[0]))
    return None


def sample_paths(stage, prim, frames):
    """(F, N, 3) world positions of *prim*'s points at *frames* (N from the first frame)."""
    attr = _points_attr(prim)
    xf = UsdGeom.Xformable(prim)
    out = []
    n = None
    for f in frames:
        v = attr.Get(f)
        p = np.asarray(v, dtype=np.float64).reshape(-1, 3) if v is not None else np.zeros((0, 3))
        n = len(p) if n is None else n
        if len(p) != n:  # a count change mid-way: pad or cut to the first frame's points
            p = np.concatenate([p, np.repeat(p[-1:] if len(p) else np.zeros((1, 3)), max(0, n - len(p)), 0)])[:n]
        m = np.array(xf.ComputeLocalToWorldTransform(f), dtype=np.float64)
        out.append(p @ m[:3, :3] + m[3, :3])
    return np.stack(out) if out else np.zeros((0, 0, 3))


def sample_normals(stage, prim, frames, count):
    """(F, N, 3) world normals of a Points prim over *frames*, or None when it has none."""
    if not prim.IsA(UsdGeom.PointBased):
        return None
    attr = UsdGeom.PointBased(prim).GetNormalsAttr()
    if not attr or not attr.HasValue():
        return None
    xf = UsdGeom.Xformable(prim)
    out = []
    for f in frames:
        v = attr.Get(f)
        n = np.asarray(v, dtype=np.float64).reshape(-1, 3) if v is not None else np.zeros((0, 3))
        if len(n) != count:
            return None
        m = np.array(xf.ComputeLocalToWorldTransform(f), dtype=np.float64)[:3, :3]
        n = n @ m
        out.append(n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12))
    return np.stack(out)


def build_trails(paths, min_step):
    """Per point, the indices of the frames kept along its trail (a point that never moved
    further than *min_step* gets a single index)."""
    keep = []
    for i in range(paths.shape[1]):
        idx = [0]
        for f in range(1, paths.shape[0]):
            if np.linalg.norm(paths[f, i] - paths[idx[-1], i]) >= min_step:
                idx.append(f)
        keep.append(idx)
    return keep


def _wobble(arc, phases, variation, frequency):
    """Smooth width multiplier along a trail's length: 1 +- *variation*, *frequency* bulges
    per scene unit. Two detuned sines with per-trail phases -- a run swells and thins where
    the water gathered, never in a pattern shared with its neighbours."""
    if variation <= 0.0:
        return np.ones_like(arc)
    x = 2.0 * np.pi * frequency * arc
    n = 0.6 * np.sin(x + phases[0]) + 0.4 * np.sin(2.37 * x + phases[1])
    return np.maximum(0.15, 1.0 + variation * n)


def _curves_at(paths, keep, upto, widths, taper, normals=None, variation=0.0, frequency=1.0, sink=0.0,
               phases=None):
    """Points, vertex counts and widths of every trail with at least two vertices by frame *upto*.

    *variation* / *frequency* swell and thin each trail along its length; *sink* pushes it
    into the surface along *normals* by that fraction of its half-width, so a tube reads as a
    flat film of water lying on the surface rather than a wire on top of it.
    """
    pts, counts, wid = [], [], []
    for i, idx in enumerate(keep):
        idx = [f for f in idx if f <= upto]
        if upto not in idx and upto < paths.shape[0] and len(idx) and np.linalg.norm(
                paths[upto, i] - paths[idx[-1], i]) > 1e-12:
            idx = idx + [upto]  # the head, where the point is right now
        if len(idx) < 2:
            continue
        p = paths[idx, i]
        arc = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(p, axis=0), axis=1))])
        ramp = np.linspace(taper, 1.0, len(idx))
        ph = phases[i] if phases is not None else (0.0, 0.0)
        w = widths[i] * ramp * _wobble(arc, ph, variation, frequency)
        if sink > 0.0 and normals is not None:
            p = p - normals[idx, i] * (0.5 * w * sink)[:, None]
        pts.append(p)
        counts.append(len(idx))
        wid.append(w)
    if not counts:
        return np.zeros((0, 3)), [], np.zeros(0)
    return np.concatenate(pts), counts, np.concatenate(wid)


def execute(node) -> None:
    stage = node.get_stage()
    layer = node.get_edit_context()
    if stage is None or layer is None:
        return
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)
    is_f, is_i, is_s = chunk.is_param_t(float), chunk.is_param_t(int), chunk.is_param_t(str)
    source = node.get_param("source", is_s).value.strip()
    out_path = node.get_param("output_path", is_s).value.strip() or "/Trails"
    scene = node.get_scene()
    upstream = chunk_stage.get_upstream_stage(stage, scene, node) if scene is not None else stage
    prim = upstream.GetPrimAtPath(source) if source else None
    attr = _points_attr(prim) if prim else None
    if attr is None:
        _report(node, "set a Source with points that move over time")
        return
    times = attr.GetTimeSamples()
    if len(times) < 2:
        _report(node, f"{source} is not animated -- nothing to trail")
        return
    first = node.get_param("start_frame", is_i).value
    last = node.get_param("end_frame", is_i).value
    first = int(times[0]) if first <= 0 else first
    last = int(times[-1]) if last <= 0 else last
    frames = list(range(first, max(first + 1, last) + 1))
    paths = sample_paths(upstream, prim, frames)
    n = paths.shape[1]
    base = max(1e-6, node.get_param("width", is_f).value)
    src_w = _widths(prim, first, n) if node.get_param("use_source_width", chunk.is_param_t(bool)).value else None
    widths = src_w if src_w is not None else np.full(n, base)
    taper = min(1.0, max(0.0, node.get_param("taper", is_f).value))
    keep = build_trails(paths, max(0.0, node.get_param("min_step", is_f).value))
    grow = node.get_param("grow", chunk.is_param_t(bool)).value
    sink = min(1.0, max(0.0, node.get_param("sink", is_f).value))
    normals = sample_normals(upstream, prim, frames, n) if sink > 0.0 else None
    shape = dict(normals=normals, sink=sink,
                 variation=max(0.0, node.get_param("width_variation", is_f).value),
                 frequency=max(0.0, node.get_param("width_frequency", is_f).value),
                 phases=np.random.default_rng(node.get_param("seed", is_i).value).uniform(0, 2 * np.pi, (n, 2)))
    with Usd.EditContext(stage, chunk_utils.edit_target(stage, layer)):
        curves = UsdGeom.BasisCurves.Define(stage, out_path)
        curves.CreateTypeAttr().Set(UsdGeom.Tokens.linear)
        curves.SetWidthsInterpolation(UsdGeom.Tokens.vertex)
        pa, ca, wa = curves.CreatePointsAttr(), curves.CreateCurveVertexCountsAttr(), curves.CreateWidthsAttr()
        full = _curves_at(paths, keep, len(frames) - 1, widths, taper, **shape)
        for attr_, value in zip((pa, ca, wa), (Vt.Vec3fArray.FromNumpy(full[0].astype(np.float32)),
                                               Vt.IntArray(full[1]),
                                               Vt.FloatArray.FromNumpy(full[2].astype(np.float32)))):
            attr_.Set(value)
        if grow:
            grown = [(f, _curves_at(paths, keep, k, widths, taper, **shape)) for k, f in enumerate(frames)]
            sim_bake.write_samples(pa, ((f, Vt.Vec3fArray.FromNumpy(p.astype(np.float32))) for f, (p, _c, _w) in grown))
            sim_bake.write_samples(ca, ((f, Vt.IntArray(c)) for f, (_p, c, _w) in grown))
            sim_bake.write_samples(wa, ((f, Vt.FloatArray.FromNumpy(w.astype(np.float32))) for f, (_p, _c, w) in grown))
        lo, hi = (full[0].min(axis=0), full[0].max(axis=0)) if len(full[0]) else (np.zeros(3), np.zeros(3))
        curves.CreateExtentAttr().Set(Vt.Vec3fArray([tuple(lo - base), tuple(hi + base)]))
    _report(node, f"{len(full[1])} trails from {n} points · frames {frames[0]}–{frames[-1]}"
                  + (" · growing" if grow else ""))


def main() -> chunk.Chunk:
    chunk_api = chunk.Chunk("Trails")
    labeled = chunk.ChunkFlags.ParamFlags.LABELED
    chunk_api.add_param(chunk.LineEdit(
        "source", "Source", flags=labeled | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="Anything whose points move over time: a Surface Flow, a Points cloud, a "
        "PointInstancer (an RBD bake), a deforming mesh.",
    ))
    chunk_api.add_param(chunk.LineEdit("output_path", "Output", default_value="/Trails", flags=labeled,
                                       tooltip="The BasisCurves written, one curve per point."))
    chunk_api.add_param(chunk.Spinbox("start_frame", "Start Frame", default_value=0, is_int=True,
                                      tooltip="0 = the source's first time sample."))
    chunk_api.add_param(chunk.Spinbox("end_frame", "End Frame", default_value=0, is_int=True,
                                      tooltip="0 = the source's last time sample."))
    chunk_api.add_param(chunk.Spinbox("min_step", "Min Step", default_value=0.001,
                                      tooltip="Samples closer than this to the last kept one are dropped."))
    chunk_api.add_param(chunk.Spinbox("width", "Width", default_value=0.002,
                                      tooltip="Curve width at the head of each trail."))
    chunk_api.add_param(chunk.Checkbox("use_source_width", "Use Source Widths", default_value=True,
                                       tooltip="Take each trail's width from its point's widths, when the "
                                       "source is a Points prim that has them."))
    chunk_api.add_param(chunk.Slider("taper", "Taper", min_value=0.0, max_value=1.0, default_value=0.5,
                                     tooltip="Width at the start of a trail, relative to its head."))
    chunk_api.add_param(chunk.Slider("width_variation", "Width Variation", min_value=0.0, max_value=1.0,
                                     default_value=0.0,
                                     tooltip="How much each trail swells and thins along its length."))
    chunk_api.add_param(chunk.Spinbox("width_frequency", "Width Frequency", default_value=1.0,
                                      tooltip="Swellings per scene unit of length."))
    chunk_api.add_param(chunk.Slider("sink", "Sink", min_value=0.0, max_value=1.0, default_value=0.0,
                                     tooltip="Push each trail into the surface it ran over (needs the source's "
                                     "normals, which Surface Flow writes), as a fraction of its half-width: "
                                     "a tube then reads as a flat film lying on the surface."))
    chunk_api.add_param(chunk.Spinbox("seed", "Seed", default_value=0, is_int=True))
    chunk_api.add_param(chunk.Checkbox("grow", "Grow", default_value=False,
                                       tooltip="Animate the curves: each reaches only as far as its point "
                                       "has got at that frame."))
    chunk_api.add_param(chunk.Label("readout", "nothing traced yet", default_value="nothing traced yet"))
    return chunk_api


def config() -> chunk.DefaultConfig:
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.MODELLING,
        icon="curve",
        description="One curve per point, through everywhere the point has been over time (Houdini's Trail).",
        requirements=["numpy>=2,<3"],
    )
