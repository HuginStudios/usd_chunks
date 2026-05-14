"""Scatter Points chunk — distributes instances across a mesh surface."""

from __future__ import annotations

import bisect
import math
import random

from pxr import Gf, Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk

_ORIENT_MODES = ["Align to Normal", "Keep Original", "Up", "Random"]
_RAND_AXES = ["Off", "X", "Y", "Z", "All"]


def _collect_triangles(prim):
    """Collect world-space triangles from prim and its descendants."""
    if not prim or not prim.IsValid():
        return []

    triangles = []
    for p in Usd.PrimRange(prim):
        mesh = UsdGeom.Mesh(p)
        points = mesh.GetPointsAttr().Get()
        if not points:
            continue
        face_counts = mesh.GetFaceVertexCountsAttr().Get()
        face_indices = mesh.GetFaceVertexIndicesAttr().Get()
        if not face_counts or not face_indices:
            continue

        xform_cache = UsdGeom.XformCache()
        world_xf = xform_cache.GetLocalToWorldTransform(p)
        world_pts = []
        for pt in points:
            wp = world_xf.Transform(Gf.Vec3d(pt[0], pt[1], pt[2]))
            world_pts.append(Gf.Vec3f(wp[0], wp[1], wp[2]))

        idx = 0
        for fc in face_counts:
            if fc >= 3:
                v0 = world_pts[face_indices[idx]]
                for i in range(1, fc - 1):
                    v1 = world_pts[face_indices[idx + i]]
                    v2 = world_pts[face_indices[idx + i + 1]]
                    e1 = v1 - v0
                    e2 = v2 - v0
                    n = Gf.Vec3f(
                        e1[1] * e2[2] - e1[2] * e2[1],
                        e1[2] * e2[0] - e1[0] * e2[2],
                        e1[0] * e2[1] - e1[1] * e2[0],
                    )
                    ln = Gf.GetLength(n)
                    if ln > 1e-8:
                        n = n / ln
                    triangles.append((v0, v1, v2, n))
            idx += fc

    if not triangles:
        # Fallback: flat quad on top face of bounding box
        bbox_cache = UsdGeom.BBoxCache(0, [UsdGeom.Tokens.default_])
        bbox = bbox_cache.ComputeWorldBound(prim)
        bbox_range = bbox.ComputeAlignedRange()
        if not bbox_range.IsEmpty():
            mn = bbox_range.GetMin()
            mx = bbox_range.GetMax()
            v0 = Gf.Vec3f(mn[0], mn[1], mx[2])
            v1 = Gf.Vec3f(mx[0], mn[1], mx[2])
            v2 = Gf.Vec3f(mx[0], mx[1], mx[2])
            v3 = Gf.Vec3f(mn[0], mx[1], mx[2])
            up = Gf.Vec3f(0, 0, 1)
            triangles.append((v0, v1, v2, up))
            triangles.append((v0, v2, v3, up))

    return triangles


def _scatter_on_triangles(triangles, count, rng, min_dist=0):
    """Area-weighted random point sampling with optional min-distance rejection."""
    areas = []
    for v0, v1, v2, _ in triangles:
        e1 = v1 - v0
        e2 = v2 - v0
        cross = Gf.Vec3f(
            e1[1] * e2[2] - e1[2] * e2[1],
            e1[2] * e2[0] - e1[0] * e2[2],
            e1[0] * e2[1] - e1[1] * e2[0],
        )
        areas.append(0.5 * Gf.GetLength(cross))

    total_area = sum(areas)
    if total_area < 1e-10:
        return [], []

    cdf = []
    cumulative = 0.0
    for a in areas:
        cumulative += a / total_area
        cdf.append(cumulative)

    positions = []
    normals = []
    min_dist_sq = min_dist * min_dist
    max_attempts = count * 20
    attempts = 0

    while len(positions) < count and attempts < max_attempts:
        attempts += 1
        r = rng.random()
        tri_idx = min(bisect.bisect_left(cdf, r), len(triangles) - 1)
        v0, v1, v2, n = triangles[tri_idx]

        u = rng.random()
        v = rng.random()
        if u + v > 1.0:
            u, v = 1.0 - u, 1.0 - v
        w = 1.0 - u - v
        pt = Gf.Vec3f(
            w * v0[0] + u * v1[0] + v * v2[0],
            w * v0[1] + u * v1[1] + v * v2[1],
            w * v0[2] + u * v1[2] + v * v2[2],
        )

        if min_dist_sq > 0:
            too_close = any(
                (pt[0] - ex[0]) ** 2 + (pt[1] - ex[1]) ** 2 + (pt[2] - ex[2]) ** 2
                < min_dist_sq
                for ex in positions
            )
            if too_close:
                continue

        positions.append(pt)
        normals.append(n)

    return positions, normals


def _normal_to_quat(normal):
    """Convert surface normal to a quaternion rotation (Z-up → normal direction)."""
    up = Gf.Vec3f(0, 0, 1)
    n = Gf.Vec3f(normal[0], normal[1], normal[2])
    ln = Gf.GetLength(n)
    if ln < 1e-8:
        return Gf.Quath(1, 0, 0, 0)
    n = n / ln
    dot = Gf.Dot(up, n)
    if dot > 0.9999:
        return Gf.Quath(1, 0, 0, 0)
    if dot < -0.9999:
        return Gf.Quath(0, 1, 0, 0)
    axis = Gf.Cross(up, n)
    axis_len = Gf.GetLength(axis)
    if axis_len < 1e-8:
        return Gf.Quath(1, 0, 0, 0)
    axis = axis / axis_len
    angle = math.acos(max(-1.0, min(1.0, dot)))
    half = angle * 0.5
    s = math.sin(half)
    return Gf.Quath(math.cos(half), axis[0] * s, axis[1] * s, axis[2] * s)


def _quat_mul(a, b):
    """Multiply two Quath quaternions."""
    aw, ax, ay, az = a.GetReal(), *a.GetImaginary()
    bw, bx, by, bz = b.GetReal(), *b.GetImaginary()
    return Gf.Quath(
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def _compute_orientation(mode, normal):
    if mode == "Align to Normal":
        return _normal_to_quat(normal)
    return Gf.Quath(1, 0, 0, 0)


def _apply_random_rotation(base_quat, axis, amount_deg, rng):
    if axis == "Off":
        return base_quat
    amount_rad = math.radians(amount_deg)
    if axis == "All":
        ax = Gf.Vec3f(rng.gauss(0, 1), rng.gauss(0, 1), rng.gauss(0, 1))
        ln = Gf.GetLength(ax)
        if ln < 1e-8:
            return base_quat
        ax = ax / ln
        angle = rng.uniform(-amount_rad, amount_rad)
    else:
        ax_map = {"X": Gf.Vec3f(1, 0, 0), "Y": Gf.Vec3f(0, 1, 0), "Z": Gf.Vec3f(0, 0, 1)}
        ax = ax_map[axis]
        angle = rng.uniform(-amount_rad, amount_rad)
    half = angle * 0.5
    s = math.sin(half)
    rand_q = Gf.Quath(math.cos(half), ax[0] * s, ax[1] * s, ax[2] * s)
    return _quat_mul(rand_q, base_quat)


def execute(owner, notice, sender):
    """Scatter instances on a mesh surface — triggered by the Scatter button."""
    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage:
        return

    surface_prims = _chunk.get_param("surface_prim").get_prims()
    surface_prim = surface_prims[0] if surface_prims else None

    proto_prims = _chunk.get_param("proto_prim").get_prims()
    proto_prim = proto_prims[0] if proto_prims else None

    inst_path = _chunk.get_param("inst_path").value or "/ScatterInstancer"
    count = int(_chunk.get_param("count").value or 100)
    seed = int(_chunk.get_param("seed").value or 0)
    scale = float(_chunk.get_param("scale").value or 1.0)
    min_dist = float(_chunk.get_param("min_distance").value or 0)
    orient_mode = _chunk.get_param("orient_mode").value or "Align to Normal"
    rand_axis = _chunk.get_param("rand_axis").value or "Off"
    rand_amount = float(_chunk.get_param("rand_amount").value or 360)

    layer = owner.get_edit_context()
    if not layer:
        return
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)

    triangles = _collect_triangles(surface_prim)
    if not triangles:
        rng = random.Random(seed)
        sp = 2.0
        n = int(math.sqrt(count))
        off = (n - 1) * sp / 2
        positions = []
        for x in range(n):
            for y in range(n):
                jx = rng.uniform(-sp * 0.3, sp * 0.3)
                jy = rng.uniform(-sp * 0.3, sp * 0.3)
                positions.append(Gf.Vec3f(x * sp - off + jx, y * sp - off + jy, 0))
        normals = [Gf.Vec3f(0, 0, 1)] * len(positions)
    else:
        rng = random.Random(seed)
        positions, normals = _scatter_on_triangles(triangles, count, rng, min_dist)

    with Usd.EditContext(stage, layer):
        instancer = UsdGeom.PointInstancer.Define(stage, inst_path)

        proto_scope = f"{inst_path}/Prototypes"
        stage.DefinePrim(proto_scope, "Scope")
        proto_path = f"{proto_scope}/proto_0"
        if proto_prim and proto_prim.IsValid():
            ref_prim = stage.DefinePrim(proto_path)
            ref_prim.GetReferences().AddInternalReference(proto_prim.GetPath())
            ref_prim.SetInstanceable(False)
            xform_cache = UsdGeom.XformCache()
            world_xf = xform_cache.GetLocalToWorldTransform(proto_prim)
            inv = world_xf.GetInverse()
            xf = UsdGeom.Xformable(ref_prim)
            xf.ClearXformOpOrder()
            xf.AddTransformOp().Set(Gf.Matrix4d(inv))
        else:
            cube = UsdGeom.Cube.Define(stage, proto_path)
            cube.GetSizeAttr().Set(1.0)

        instancer.GetPrototypesRel().SetTargets([Sdf.Path(proto_path)])

        if positions:
            instancer.GetPositionsAttr().Set(Vt.Vec3fArray(positions))
            instancer.GetProtoIndicesAttr().Set(Vt.IntArray([0] * len(positions)))
            instancer.GetScalesAttr().Set(
                Vt.Vec3fArray([Gf.Vec3f(scale, scale, scale)] * len(positions))
            )

            rng2 = random.Random(seed + 7)
            orientations = [
                _apply_random_rotation(
                    _compute_orientation(orient_mode, nrm), rand_axis, rand_amount, rng2
                )
                for nrm in normals
            ]
            instancer.GetOrientationsAttr().Set(Vt.QuathArray(orientations))

    print(f"[ScatterPoints] Scattered {len(positions)} instances at {inst_path}")


def main():
    chunk_api = chunk.Chunk("Scatter Points")

    surface_prim = chunk.LineEdit(
        "surface_prim",
        "Surface Mesh",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
    )
    chunk_api.add_param(surface_prim)

    proto_prim = chunk.LineEdit(
        "proto_prim",
        "Prototype Prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
    )
    chunk_api.add_param(proto_prim)

    chunk_api.add_param(
        chunk.LineEdit("inst_path", "Instancer Path", default_value="/ScatterInstancer")
    )

    scatter_grp = chunk.Group("Scatter")
    scatter_grp.add_param(
        chunk.Spinbox("count", "Point Count", default_value=100, is_int=True)
    )
    scatter_grp.add_param(chunk.Spinbox("seed", "Seed", default_value=0, is_int=True))
    scatter_grp.add_param(chunk.Spinbox("scale", "Instance Scale", default_value=1.0))
    scatter_grp.add_param(
        chunk.Spinbox("min_distance", "Min Distance", default_value=0.0)
    )
    chunk_api.add_param(scatter_grp)

    orient_grp = chunk.Group("Orientation")
    orient_grp.add_param(
        chunk.Menu(
            "orient_mode",
            "Orientation Mode",
            menu_items=_ORIENT_MODES,
            default_value="Align to Normal",
        )
    )
    orient_grp.add_param(
        chunk.Menu(
            "rand_axis",
            "Random Rotation Axis",
            menu_items=_RAND_AXES,
            default_value="Off",
        )
    )
    orient_grp.add_param(
        chunk.Spinbox("rand_amount", "Random Rotation (deg)", default_value=360.0)
    )
    chunk_api.add_param(orient_grp)

    scatter_btn = chunk.Button("scatter", "Scatter")
    scatter_btn.add_callback(execute)
    chunk_api.add_param(scatter_btn)

    return chunk_api


def config():
    return chunk.DefaulConfig(category="Create")
