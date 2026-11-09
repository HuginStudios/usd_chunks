"""Fence chunk -- builds a parametric picket/rail fence as a single mesh.

The run is a polyline: either a straight length along +X, or the points of a
Path Prim (a curve, points, or mesh you feed in) so the fence follows a shape
and its terrain height. Posts are placed at an even spacing along that run;
each span between two posts gets its rails and its pickets.

Everything is built in a local frame where +X runs along the fence, +Y is up
and +Z is the fence's thickness direction, then mapped onto the stage's up
axis on the way out.

Requires: pxr (OpenUSD), usd_chunks.
"""

from __future__ import annotations

import math
import random

from pxr import Gf, Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk, chunk_utils
from usd_chunks import chunk_stage

_PICKET_TOPS = ["Flat", "Pointed", "Dog Ear"]

# Quad faces of a box whose corners are listed bottom ring (0-3) then top ring (4-7),
# wound so every normal points outward.
_BOX_FACES = ((4, 5, 6, 7), (1, 0, 3, 2), (5, 1, 2, 6), (0, 4, 7, 3), (3, 7, 6, 2), (0, 1, 5, 4))


class _MeshData:
    """Accumulates points/faces for the one mesh the whole fence is built into."""

    def __init__(self):
        self.points = []
        self.counts = []
        self.indices = []

    def add(self, corners, faces):
        base = len(self.points)
        self.points.extend(corners)
        for f in faces:
            self.counts.append(len(f))
            self.indices.extend(base + i for i in f)


def _normalized(v):
    v = Gf.Vec3f(v)
    ln = Gf.GetLength(v)
    return v / ln if ln > 1e-9 else Gf.Vec3f(0, 0, 0)


class _Frame:
    """A right-handed (u, v, w) frame anchored at a point -- u along the fence,
    v up, w through the fence's thickness.
    """

    def __init__(self, origin, u, v):
        self.o = Gf.Vec3f(origin)
        self.u = _normalized(u)
        self.v = _normalized(v)
        self.w = _normalized(Gf.Cross(self.u, self.v))

    def at(self, a, b, c):
        return Gf.Vec3f(self.o + self.u * a + self.v * b + self.w * c)

    def leaned(self, angle_deg):
        """Rotate about w (tilt along the fence line), pivoting on the origin."""
        if abs(angle_deg) < 1e-6:
            return self
        r = math.radians(angle_deg)
        cos_r, sin_r = math.cos(r), math.sin(r)
        return _Frame(self.o, self.u * cos_r + self.v * sin_r, self.v * cos_r - self.u * sin_r)


def _add_box(data, frame, a0, a1, b0, b1, c0, c1):
    if a1 - a0 < 1e-9 or b1 - b0 < 1e-9 or c1 - c0 < 1e-9:
        return
    coords = ((a0, b0, c0), (a1, b0, c0), (a1, b1, c0), (a0, b1, c0),
              (a0, b0, c1), (a1, b0, c1), (a1, b1, c1), (a0, b1, c1))
    data.add([frame.at(*c) for c in coords], _BOX_FACES)


def _add_frustum(data, frame, a0, a1, b0, b1, c0, c1, inset):
    """A box whose top rect is pulled in by *inset* on each side -- the dog-ear cut."""
    if a1 - a0 < 1e-9 or b1 - b0 < 1e-9:
        return
    inset = min(inset, (a1 - a0) * 0.5 - 1e-6)
    ta0, ta1 = a0 + inset, a1 - inset
    coords = ((a0, b0, c0), (a1, b0, c0), (ta1, b1, c0), (ta0, b1, c0),
              (a0, b0, c1), (a1, b0, c1), (ta1, b1, c1), (ta0, b1, c1))
    data.add([frame.at(*c) for c in coords], _BOX_FACES)


def _add_wedge(data, frame, a0, a1, b0, b1, c0, c1):
    """Gable top: a rect at b0 rising to a ridge at b1, running through the thickness."""
    if a1 - a0 < 1e-9 or b1 - b0 < 1e-9:
        return
    am = (a0 + a1) * 0.5
    corners = [frame.at(a0, b0, c0), frame.at(a1, b0, c0), frame.at(a1, b0, c1), frame.at(a0, b0, c1),
               frame.at(am, b1, c0), frame.at(am, b1, c1)]
    # Two sloping faces, two triangular gable ends, and the underside.
    data.add(corners, ((0, 3, 5, 4), (1, 4, 5, 2), (0, 4, 1), (3, 2, 5), (0, 1, 2, 3)))


def _add_picket(data, frame, a0, a1, b0, b1, c0, c1, top_mode, point_height):
    if top_mode == "Pointed":
        point_height = max(0.0, min(point_height, (b1 - b0) * 0.95))
        shoulder = b1 - point_height
        _add_box(data, frame, a0, a1, b0, shoulder, c0, c1)
        _add_wedge(data, frame, a0, a1, shoulder, b1, c0, c1)
    elif top_mode == "Dog Ear":
        cut = max(0.0, min(point_height, (b1 - b0) * 0.95))
        shoulder = b1 - cut
        _add_box(data, frame, a0, a1, b0, shoulder, c0, c1)
        _add_frustum(data, frame, a0, a1, shoulder, b1, c0, c1, cut)
    else:
        _add_box(data, frame, a0, a1, b0, b1, c0, c1)


def _to_world(p, up_is_z):
    """Fence-local (x along, y up, z thick) -> world. On a Z-up stage this is a
    +90 degree turn about X, not a y/z swap: a swap mirrors the fence and would
    flip every face normal inward.
    """
    if up_is_z:
        return Gf.Vec3f(p[0], -p[2], p[1])
    return Gf.Vec3f(p[0], p[1], p[2])


def _to_local(p, up_is_z):
    """World -> fence-local. Inverse of _to_world."""
    if up_is_z:
        return Gf.Vec3f(p[0], p[2], -p[1])
    return Gf.Vec3f(p[0], p[1], p[2])


def _path_points(prim, up_is_z):
    """World-space points of a curve/points/mesh prim, in fence-local coordinates."""
    if not prim or not prim.IsValid():
        return []
    points_attr = UsdGeom.PointBased(prim).GetPointsAttr()
    pts = points_attr.Get() if points_attr else None
    if not pts:
        return []
    world_xf = UsdGeom.XformCache(Usd.TimeCode.Default()).GetLocalToWorldTransform(prim)
    out = []
    for p in pts:
        wp = world_xf.Transform(Gf.Vec3d(p[0], p[1], p[2]))
        lp = _to_local(Gf.Vec3f(wp[0], wp[1], wp[2]), up_is_z)
        # Coincident points make zero-length segments the sampler would trip over.
        if out and Gf.GetLength(lp - out[-1]) <= 1e-6:
            continue
        out.append(lp)
    return out


class _Polyline:
    """Arc-length sampling over the fence run."""

    def __init__(self, points):
        self.points = points
        self.cumulative = [0.0]
        for a, b in zip(points, points[1:]):
            self.cumulative.append(self.cumulative[-1] + Gf.GetLength(b - a))

    @property
    def length(self):
        return self.cumulative[-1]

    def sample(self, dist):
        """(point, tangent) at *dist* along the run."""
        dist = max(0.0, min(dist, self.length))
        for i in range(len(self.points) - 1):
            seg_len = self.cumulative[i + 1] - self.cumulative[i]
            if seg_len < 1e-9:
                continue
            if dist <= self.cumulative[i + 1] or i == len(self.points) - 2:
                t = (dist - self.cumulative[i]) / seg_len
                a, b = self.points[i], self.points[i + 1]
                return a + (b - a) * t, _normalized(b - a)
        return self.points[0], Gf.Vec3f(1, 0, 0)


def _build_span(data, start, end, cfg, rng):
    """Rails and pickets filling the gap between two neighbouring posts."""
    span_vec = end - start
    span_len = Gf.GetLength(span_vec)
    if span_len < 1e-6:
        return

    frame = _Frame(start, span_vec, Gf.Vec3f(0, 1, 0))
    half_post = cfg["post_width"] * 0.5
    a0, a1 = half_post, span_len - half_post
    if a1 - a0 < 1e-6:
        return

    height = cfg["height"]
    rail_h = cfg["rail_height"]
    rail_d = cfg["rail_depth"]
    # Rails are centred on the offset through the fence, so 0 puts them on the
    # post's centreline and the pickets' own offset decides which face they sit on.
    rail_c0 = cfg["rail_offset"] - rail_d * 0.5
    lowest = cfg["rail_bottom"]
    # The top rail's *top* edge lands on rail_top * height, so it never pokes
    # above a flush-topped picket. Never below the bottom rail, whatever the user typed.
    highest = max(height * cfg["rail_top"] - rail_h, lowest)
    for i in range(cfg["rail_count"]):
        t = 0.0 if cfg["rail_count"] == 1 else i / (cfg["rail_count"] - 1)
        b0 = lowest + (highest - lowest) * t
        _add_box(data, frame, a0, a1, b0, b0 + rail_h, rail_c0, rail_c0 + rail_d)

    if not cfg["pickets"]:
        return

    pitch = cfg["picket_width"] + cfg["picket_gap"]
    usable = a1 - a0
    count = int(usable // pitch)
    if count < 1:
        return
    # Whatever is left over after a whole number of pickets is split evenly at each end.
    margin = (usable - (count * pitch - cfg["picket_gap"])) * 0.5
    picket_c0 = cfg["picket_offset"] - cfg["picket_depth"] * 0.5

    for i in range(count):
        if rng.random() < cfg["missing"]:
            continue
        pa0 = a0 + margin + i * pitch
        top = height * cfg["picket_height"] + rng.uniform(-cfg["height_jitter"], cfg["height_jitter"])
        top = max(top, cfg["picket_bottom"] + 0.001)
        lean = rng.uniform(-cfg["lean"], cfg["lean"])
        # Each picket gets its own frame pivoting on its bottom-left corner so the lean
        # tilts it about where it meets the ground, not about the span's origin.
        base = _Frame(frame.at(pa0, cfg["picket_bottom"], 0.0), frame.u, frame.v).leaned(lean)
        _add_picket(data, base, 0.0, cfg["picket_width"], 0.0, top - cfg["picket_bottom"],
                    picket_c0, picket_c0 + cfg["picket_depth"], cfg["picket_top"], cfg["point_height"])


def build_fence(stage, path, cfg, points):
    """Author the whole fence as one Mesh at *path*.

    *points* is the run polyline in fence-local coordinates (x along, y up).
    """
    line = _Polyline(points)
    if line.length < 1e-6:
        return None

    spacing = max(cfg["spacing"], 0.001)
    spans = max(1, int(round(line.length / spacing)))
    step = line.length / spans

    data = _MeshData()
    rng = random.Random(cfg["seed"])
    post_top = cfg["height"] + cfg["post_extra"]
    half_w = cfg["post_width"] * 0.5
    half_d = cfg["post_depth"] * 0.5

    post_positions = []
    for i in range(spans + 1):
        pos, tangent = line.sample(i * step)
        post_positions.append(pos)
        frame = _Frame(pos, tangent, Gf.Vec3f(0, 1, 0))
        lean = rng.uniform(-cfg["post_lean"], cfg["post_lean"])
        _add_box(data, frame.leaned(lean), -half_w, half_w, -cfg["post_sink"], post_top, -half_d, half_d)

    for start, end in zip(post_positions, post_positions[1:]):
        _build_span(data, start, end, cfg, rng)

    if not data.points:
        return None

    world_pts = Vt.Vec3fArray([_to_world(p, cfg["up_is_z"]) for p in data.points])

    mesh = UsdGeom.Mesh.Define(stage, Sdf.Path(path))
    mesh.CreatePointsAttr(world_pts)
    mesh.CreateFaceVertexCountsAttr(Vt.IntArray(data.counts))
    mesh.CreateFaceVertexIndicesAttr(Vt.IntArray(data.indices))
    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
    mesh.CreateExtentAttr(UsdGeom.PointBased(mesh).ComputeExtent(world_pts))
    return mesh


def execute(node):
    """Rebuild the fence whenever a param or an upstream dependency changes.

    The path prim is read through get_upstream_stage() for the same reason Scatter
    Points does it: a chunk wired *after* this one (a Transform on the same curve,
    say) must not leak into what this fence is built from.
    """
    stage = node.get_stage()

    is_f = chunk.is_param_t(float)
    is_i = chunk.is_param_t(int)
    is_s = chunk.is_param_t(str)
    is_b = chunk.is_param_t(bool)

    path_paths = node.get_param("path_prim").resolve_paths()
    up_is_z = UsdGeom.GetStageUpAxis(stage).upper() != "Y"

    points = []
    if path_paths:
        scratch = chunk_stage.get_upstream_stage(stage, node.get_scene(), node)
        points = _path_points(scratch.GetPrimAtPath(path_paths[0]), up_is_z)
    if len(points) < 2:
        points = [Gf.Vec3f(0, 0, 0), Gf.Vec3f(max(node.get_param("length", is_f).value, 0.001), 0, 0)]
    elif node.get_param("close_loop", is_b).value and Gf.GetLength(points[-1] - points[0]) > 1e-6:
        points = points + [points[0]]

    cfg = {
        "up_is_z": up_is_z,
        "height": max(node.get_param("height", is_f).value, 0.001),
        "spacing": node.get_param("spacing", is_f).value,
        "post_width": max(node.get_param("post_width", is_f).value, 0.0001),
        "post_depth": max(node.get_param("post_depth", is_f).value, 0.0001),
        "post_extra": node.get_param("post_extra", is_f).value,
        "post_sink": node.get_param("post_sink", is_f).value,
        "post_lean": node.get_param("post_lean", is_f).value,
        "rail_count": max(0, node.get_param("rail_count", is_i).value),
        "rail_height": max(node.get_param("rail_height", is_f).value, 0.0001),
        "rail_depth": max(node.get_param("rail_depth", is_f).value, 0.0001),
        "rail_bottom": node.get_param("rail_bottom", is_f).value,
        "rail_top": node.get_param("rail_top", is_f).value,
        "rail_offset": node.get_param("rail_offset", is_f).value,
        "pickets": node.get_param("pickets", is_b).value,
        "picket_width": max(node.get_param("picket_width", is_f).value, 0.0001),
        "picket_depth": max(node.get_param("picket_depth", is_f).value, 0.0001),
        "picket_gap": max(node.get_param("picket_gap", is_f).value, 0.0),
        "picket_height": node.get_param("picket_height", is_f).value,
        "picket_bottom": node.get_param("picket_bottom", is_f).value,
        "picket_offset": node.get_param("picket_offset", is_f).value,
        "picket_top": node.get_param("picket_top", is_s).value,
        "point_height": node.get_param("point_height", is_f).value,
        "seed": node.get_param("seed", is_i).value,
        "height_jitter": node.get_param("height_jitter", is_f).value,
        "lean": node.get_param("lean", is_f).value,
        "missing": node.get_param("missing", is_f).value,
    }

    prim_path_param = node.get_param("prim_path", is_s)
    mesh = None
    for target in chunk_utils.author_in_variant_contexts(stage, prim_path_param):
        mesh = build_fence(stage, target, cfg, points)

    if mesh:
        print(f"[Fence] Built {len(mesh.GetFaceVertexCountsAttr().Get())} faces at {prim_path_param.value}")


def main():
    chunk_api = chunk.Chunk("Fence")

    _GF = (
        chunk.ChunkFlags.GroupFlags.COLLAPSABLE
        | chunk.ChunkFlags.GroupFlags.LABELED
        | chunk.ChunkFlags.GroupFlags.FRAMED
    )

    chunk_api.add_param(
        chunk.LineEdit(
            "prim_path",
            "Prim Path",
            default_value="/Fence",
            tooltip="USD path for the generated fence mesh.",
        )
    )
    chunk_api.add_param(
        chunk.LineEdit(
            "path_prim",
            "Path Prim",
            flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
            tooltip="Optional curve/points/mesh whose points the fence follows, including their\n"
                    "height. Empty = a straight run of Length along +X.",
        )
    )

    run_grp = chunk.Group("Run", icon="ruler", flags=_GF)
    run_grp.add_param(
        chunk.Slider(
            "length",
            "Length",
            min_value=0.1,
            max_value=100.0,
            default_value=10.0,
            tooltip="Length of the straight run, used when no Path Prim is set.",
        )
    )
    run_grp.add_param(
        chunk.Slider(
            "height",
            "Height",
            min_value=0.05,
            max_value=10.0,
            default_value=1.2,
            tooltip="Fence height — the top of the pickets and the ceiling for the rails.",
        )
    )
    run_grp.add_param(
        chunk.Slider(
            "spacing",
            "Post Spacing",
            min_value=0.1,
            max_value=20.0,
            default_value=2.0,
            tooltip="Target distance between posts. The run is divided evenly, so the actual\n"
                    "spacing lands near this rather than exactly on it.",
        )
    )
    run_grp.add_param(
        chunk.Checkbox(
            "close_loop",
            "Close Loop",
            default_value=False,
            tooltip="Join the last Path Prim point back to the first, enclosing an area.",
        )
    )
    chunk_api.add_param(run_grp)

    post_grp = chunk.Group("Posts", icon="posts", flags=_GF)
    post_grp.add_param(
        chunk.Slider(
            "post_width",
            "Width",
            min_value=0.005,
            max_value=1.0,
            default_value=0.12,
            tooltip="Post size along the fence line.",
        )
    )
    post_grp.add_param(
        chunk.Slider(
            "post_depth",
            "Depth",
            min_value=0.005,
            max_value=1.0,
            default_value=0.12,
            tooltip="Post size through the fence.",
        )
    )
    post_grp.add_param(
        chunk.Slider(
            "post_extra",
            "Extra Height",
            min_value=0.0,
            max_value=2.0,
            default_value=0.1,
            tooltip="How far the posts stand proud above the fence Height.",
        )
    )
    post_grp.add_param(
        chunk.Slider(
            "post_sink",
            "Sink",
            min_value=0.0,
            max_value=2.0,
            default_value=0.05,
            tooltip="How far the posts continue below the run, so they bed into uneven ground.",
        )
    )
    post_grp.add_param(
        chunk.Slider(
            "post_lean",
            "Lean",
            min_value=0.0,
            max_value=20.0,
            default_value=0.0,
            tooltip="Maximum random post tilt in degrees — a little goes a long way on an old fence.",
        )
    )
    chunk_api.add_param(post_grp)

    rail_grp = chunk.Group("Rails", icon="rails", flags=_GF)
    rail_grp.add_param(
        chunk.Slider(
            "rail_count",
            "Count",
            min_value=0,
            max_value=10,
            default_value=2,
            is_int=True,
            tooltip="Horizontal rails per span, spread between Bottom and the fence Height. 0 = none.",
        )
    )
    rail_grp.add_param(
        chunk.Slider(
            "rail_height",
            "Thickness",
            min_value=0.005,
            max_value=1.0,
            default_value=0.09,
            tooltip="Rail size in the vertical direction.",
        )
    )
    rail_grp.add_param(
        chunk.Slider(
            "rail_depth",
            "Depth",
            min_value=0.005,
            max_value=1.0,
            default_value=0.04,
            tooltip="Rail size through the fence.",
        )
    )
    rail_grp.add_param(
        chunk.Slider(
            "rail_bottom",
            "Bottom",
            min_value=0.0,
            max_value=5.0,
            default_value=0.25,
            tooltip="Height of the lowest rail above the run.",
        )
    )
    rail_grp.add_param(
        chunk.Slider(
            "rail_top",
            "Top",
            min_value=0.0,
            max_value=1.0,
            default_value=1.0,
            tooltip="Height of the top rail, as a fraction of the fence Height.\n"
                    "1 = flush with the top; lower it to drop the top rail down the pickets.",
        )
    )
    rail_grp.add_param(
        chunk.Slider(
            "rail_offset",
            "Offset",
            min_value=-1.0,
            max_value=1.0,
            default_value=0.0,
            tooltip="Shift the rails through the fence — negative puts them behind the pickets.",
        )
    )
    chunk_api.add_param(rail_grp)

    picket_grp = chunk.Group("Pickets", icon="bars", flags=_GF)
    picket_grp.add_param(
        chunk.Checkbox(
            "pickets",
            "Pickets",
            default_value=True,
            tooltip="Off leaves posts and rails only — a ranch fence.",
        )
    )
    picket_grp.add_param(
        chunk.Slider(
            "picket_width",
            "Width",
            min_value=0.005,
            max_value=1.0,
            default_value=0.09,
            tooltip="Picket size along the fence line.",
        )
    )
    picket_grp.add_param(
        chunk.Slider(
            "picket_depth",
            "Depth",
            min_value=0.005,
            max_value=1.0,
            default_value=0.025,
            tooltip="Picket size through the fence.",
        )
    )
    picket_grp.add_param(
        chunk.Slider(
            "picket_gap",
            "Gap",
            min_value=0.0,
            max_value=1.0,
            default_value=0.06,
            tooltip="Space between neighbouring pickets. 0 = a solid board fence.",
        )
    )
    picket_grp.add_param(
        chunk.Slider(
            "picket_height",
            "Height Scale",
            min_value=0.1,
            max_value=1.5,
            default_value=1.0,
            tooltip="Picket top as a fraction of the fence Height.",
        )
    )
    picket_grp.add_param(
        chunk.Slider(
            "picket_bottom",
            "Ground Clearance",
            min_value=0.0,
            max_value=2.0,
            default_value=0.08,
            tooltip="Gap left between the picket's bottom edge and the run.",
        )
    )
    picket_grp.add_param(
        chunk.Slider(
            "picket_offset",
            "Offset",
            min_value=-1.0,
            max_value=1.0,
            default_value=0.045,
            tooltip="Shift the pickets through the fence, so they sit on one face of the rails.",
        )
    )
    picket_grp.add_param(
        chunk.Menu(
            "picket_top",
            "Top Shape",
            menu_items=_PICKET_TOPS,
            default_value="Pointed",
            tooltip="Flat: a plain board.\nPointed: a gable point.\nDog Ear: the top corners cut off.",
        )
    )
    picket_grp.add_param(
        chunk.Slider(
            "point_height",
            "Top Size",
            min_value=0.0,
            max_value=1.0,
            default_value=0.1,
            tooltip="Height of the pointed or dog-eared portion of the picket.",
        )
    )
    chunk_api.add_param(picket_grp)

    var_grp = chunk.Group("Variation", icon="shuffle", flags=_GF)
    var_grp.add_param(
        chunk.Slider(
            "seed",
            "Seed",
            min_value=0,
            max_value=10000,
            default_value=0,
            is_int=True,
            tooltip="Random seed — change it for a different fence from the same settings.",
        )
    )
    var_grp.add_param(
        chunk.Slider(
            "height_jitter",
            "Height Jitter",
            min_value=0.0,
            max_value=1.0,
            default_value=0.0,
            tooltip="Random variation on each picket's top, in scene units.",
        )
    )
    var_grp.add_param(
        chunk.Slider(
            "lean",
            "Picket Lean",
            min_value=0.0,
            max_value=30.0,
            default_value=0.0,
            tooltip="Maximum random picket tilt in degrees.",
        )
    )
    var_grp.add_param(
        chunk.Slider(
            "missing",
            "Missing Pickets",
            min_value=0.0,
            max_value=1.0,
            default_value=0.0,
            tooltip="Chance of any given picket being knocked out. 0 = none missing.",
        )
    )
    chunk_api.add_param(var_grp)

    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.MODELLING,
        icon="posts",
        description=(
            """
        Builds a fence (posts, rails, pickets) along a line or curve, from new-built to derelict.

        Builds a parametric fence — posts, rails and pickets — as a single mesh.
        Runs straight, or follows a curve/points prim including its terrain height.
        Picket tops can be flat, pointed or dog-eared, and per-piece lean, height
        jitter and missing pickets take it from new-built to derelict.
        """
        ),
        variants=[
            ("Picket Fence", {"picket_top": "Pointed", "rail_count": 2}),
            ("Ranch Fence", {"pickets": False, "rail_count": 3, "height": 1.3, "spacing": 2.5}),
            ("Board Fence", {"picket_gap": 0.0, "picket_top": "Flat", "picket_height": 1.0}),
            ("Derelict Fence", {"missing": 0.25, "lean": 8.0, "height_jitter": 0.06, "post_lean": 4.0}),
        ],
    )
