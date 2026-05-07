"""Shape/primitive creation nodes."""
from __future__ import annotations

import bisect
import math
import random

from pxr import Gf, Sdf, Usd, UsdGeom, Vt

from ._base import UsdNodeBase, PORT_COLORS


class CreateCubeNode(UsdNodeBase):
    NODE_NAME = "Create Cube"
    CATEGORY = "Create"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Path", text="/Cube")
        self.add_text_input("size", "Size", text="1.0")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage) -> dict:
        p = self.get_property("path") or "/Cube"
        prim = UsdGeom.Cube.Define(stage, p)
        prim.GetSizeAttr().Set(float(self.get_property("size") or 1))
        return {"prim": prim.GetPrim()}


class CreateSphereNode(UsdNodeBase):
    NODE_NAME = "Create Sphere"
    CATEGORY = "Create"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Path", text="/Sphere")
        self.add_text_input("radius", "Radius", text="1.0")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage) -> dict:
        p = self.get_property("path") or "/Sphere"
        prim = UsdGeom.Sphere.Define(stage, p)
        prim.GetRadiusAttr().Set(float(self.get_property("radius") or 1))
        return {"prim": prim.GetPrim()}


class CreateCylinderNode(UsdNodeBase):
    NODE_NAME = "Create Cylinder"
    CATEGORY = "Create"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Path", text="/Cylinder")
        self.add_text_input("radius", "Radius", text="1.0")
        self.add_text_input("h", "Height", text="2.0")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage) -> dict:
        p = self.get_property("path") or "/Cylinder"
        prim = UsdGeom.Cylinder.Define(stage, p)
        prim.GetRadiusAttr().Set(float(self.get_property("radius") or 1))
        prim.GetHeightAttr().Set(float(self.get_property("h") or 2))
        return {"prim": prim.GetPrim()}


class CreateConeNode(UsdNodeBase):
    NODE_NAME = "Create Cone"
    CATEGORY = "Create"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Path", text="/Cone")
        self.add_text_input("radius", "Radius", text="1.0")
        self.add_text_input("h", "Height", text="2.0")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage) -> dict:
        p = self.get_property("path") or "/Cone"
        prim = UsdGeom.Cone.Define(stage, p)
        prim.GetRadiusAttr().Set(float(self.get_property("radius") or 1))
        prim.GetHeightAttr().Set(float(self.get_property("h") or 2))
        return {"prim": prim.GetPrim()}


class CreateCapsuleNode(UsdNodeBase):
    NODE_NAME = "Create Capsule"
    CATEGORY = "Create"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Path", text="/Capsule")
        self.add_text_input("radius", "Radius", text="0.5")
        self.add_text_input("h", "Height", text="2.0")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage) -> dict:
        p = self.get_property("path") or "/Capsule"
        prim = UsdGeom.Capsule.Define(stage, p)
        prim.GetRadiusAttr().Set(float(self.get_property("radius") or 0.5))
        prim.GetHeightAttr().Set(float(self.get_property("h") or 2))
        return {"prim": prim.GetPrim()}


class CreatePlaneNode(UsdNodeBase):
    NODE_NAME = "Create Plane"
    CATEGORY = "Create"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Path", text="/Plane")
        self.add_text_input("w", "Width", text="10")
        self.add_text_input("length", "Length", text="10")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage) -> dict:
        p = self.get_property("path") or "/Plane"
        w = float(self.get_property("w") or 10) / 2
        length = float(self.get_property("length") or 10) / 2
        mesh = UsdGeom.Mesh.Define(stage, p)
        mesh.GetPointsAttr().Set(Vt.Vec3fArray([
            Gf.Vec3f(-w, -length, 0), Gf.Vec3f(w, -length, 0),
            Gf.Vec3f(w, length, 0), Gf.Vec3f(-w, length, 0)]))
        mesh.GetFaceVertexCountsAttr().Set([4])
        mesh.GetFaceVertexIndicesAttr().Set([0, 1, 2, 3])
        mesh.GetNormalsAttr().Set(Vt.Vec3fArray([Gf.Vec3f(0, 0, 1)] * 4))
        return {"prim": mesh.GetPrim()}


class CreateXformNode(UsdNodeBase):
    NODE_NAME = "Create Xform"
    CATEGORY = "Create"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Path", text="/Group")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage) -> dict:
        p = self.get_property("path") or "/Group"
        prim = UsdGeom.Xform.Define(stage, p)
        return {"prim": prim.GetPrim()}


class PointInstancerNode(UsdNodeBase):
    """Create a PointInstancer that scatters prototypes at given positions."""
    NODE_NAME = "Point Instancer"
    CATEGORY = "Create"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Instancer Path", text="/Instancer")
        self.add_input("prototype", color=PORT_COLORS["prim"])
        self.add_input("positions", color=PORT_COLORS["list"])
        self.add_text_input("grid", "Grid NxN (if no positions)", text="")
        self.add_text_input("spacing", "Grid Spacing", text="2.0")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage) -> dict:
        inst_path = self.get_property("path") or "/Instancer"
        proto_prim = self._get_input_value("prototype")
        positions = self._get_input_value("positions")

        instancer = UsdGeom.PointInstancer.Define(stage, inst_path)

        # Setup prototype
        stage.DefinePrim(f"{inst_path}/Prototypes", "Scope")
        if proto_prim and proto_prim.IsValid():
            proto_path = f"{inst_path}/Prototypes/proto_0"
            ref_prim = stage.DefinePrim(proto_path)
            ref_prim.GetReferences().AddInternalReference(proto_prim.GetPath())
        else:
            # Default: create a small cube as prototype
            proto_path = f"{inst_path}/Prototypes/proto_0"
            cube = UsdGeom.Cube.Define(stage, proto_path)
            cube.GetSizeAttr().Set(1.0)

        rel = instancer.GetPrototypesRel()
        rel.SetTargets([Sdf.Path(proto_path)])

        # Build positions
        if not positions:
            grid = (self.get_property("grid") or "").strip()
            if grid:
                n = int(grid)
                sp = float(self.get_property("spacing") or 2)
                positions = []
                off = (n - 1) * sp / 2
                for x in range(n):
                    for y in range(n):
                        positions.append((x * sp - off, y * sp - off, 0))

        if positions:
            pts = Vt.Vec3fArray([Gf.Vec3f(*p) if isinstance(p, (list, tuple))
                                 else Gf.Vec3f(p, 0, 0) for p in positions])
            instancer.GetPositionsAttr().Set(pts)
            instancer.GetProtoIndicesAttr().Set(Vt.IntArray([0] * len(pts)))
        return {"prim": instancer.GetPrim()}


class MakeInstanceableNode(UsdNodeBase):
    """Mark a prim as instanceable (native instancing)."""
    NODE_NAME = "Make Instanceable"
    CATEGORY = "Create"
    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("instanceable", "Instanceable (true/false)", text="true")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        if prim:
            v = (self.get_property("instanceable") or "true").lower() in ("true", "1")
            prim.SetInstanceable(v)
        return {"prim": prim}


class ScatterPointsNode(UsdNodeBase):
    """Scatter points on a mesh surface and instance a prototype at each point.

    Like Houdini's Scatter SOP: distributes points across the surface of the
    input mesh with roughly uniform density, then creates a UsdGeom.PointInstancer.
    """
    NODE_NAME = "Scatter Points"
    CATEGORY = "Create"

    _ORIENT_MODES = ["Align to Normal", "Keep Original", "Up", "Random"]
    _RAND_AXES = ["Off", "X", "Y", "Z", "All"]

    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Instancer Path", text="/ScatterInstancer")
        self.add_input("surface", color=PORT_COLORS["prim"])
        self.add_input("prototype", color=PORT_COLORS["prim"])
        self.add_text_input("count", "Point Count", text="100")
        self.add_text_input("seed", "Seed", text="0")
        self.add_text_input("scale", "Instance Scale", text="1.0")
        self.add_combo_menu("orient_mode", "Orientation", items=self._ORIENT_MODES)
        self.add_combo_menu("rand_axis", "Random Rotation Axis", items=self._RAND_AXES)
        self.add_text_input("rand_amount", "Random Rotation (deg)", text="360")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        inst_path = self.get_property("path") or "/ScatterInstancer"
        count = int(self.get_property("count") or 100)
        seed = int(self.get_property("seed") or 0)
        scale = float(self.get_property("scale") or 1.0)
        orient_mode = self.get_property("orient_mode") or "Align to Normal"
        rand_axis = self.get_property("rand_axis") or "Off"
        rand_amount = float(self.get_property("rand_amount") or 360)

        # Clear previous instancer so stale data doesn't persist
        old = stage.GetPrimAtPath(inst_path)
        if old and old.IsValid():
            stage.RemovePrim(inst_path)

        surface_prim = self._get_input_value("surface")
        proto_prim = self._get_input_value("prototype")

        # Gather triangles from surface
        triangles = self._collect_triangles(stage, surface_prim)
        if not triangles:
            # Fallback: jittered grid
            positions = []
            rng = random.Random(seed)
            sp = 2.0
            n = int(math.sqrt(count))
            off = (n - 1) * sp / 2
            for x in range(n):
                for y in range(n):
                    jx = rng.uniform(-sp * 0.3, sp * 0.3)
                    jy = rng.uniform(-sp * 0.3, sp * 0.3)
                    positions.append(Gf.Vec3f(x * sp - off + jx, y * sp - off + jy, 0))
            normals = [Gf.Vec3f(0, 0, 1)] * len(positions)
        else:
            rng = random.Random(seed)
            positions, normals = self._scatter_on_triangles(triangles, count, rng)

        # Create the PointInstancer
        instancer = UsdGeom.PointInstancer.Define(stage, inst_path)

        # Setup prototype
        proto_scope = f"{inst_path}/Prototypes"
        stage.DefinePrim(proto_scope, "Scope")
        proto_path = f"{proto_scope}/proto_0"
        if proto_prim and proto_prim.IsValid():
            ref_prim = stage.DefinePrim(proto_path)
            ref_prim.GetReferences().AddInternalReference(proto_prim.GetPath())
            ref_prim.SetInstanceable(False)
            # Negate the prototype's world transform so it's centered at origin
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
        instancer.GetPositionsAttr().Set(Vt.Vec3fArray(positions))
        instancer.GetProtoIndicesAttr().Set(Vt.IntArray([0] * len(positions)))

        # Scales
        scales = Vt.Vec3fArray([Gf.Vec3f(scale, scale, scale)] * len(positions))
        instancer.GetScalesAttr().Set(scales)

        # Orientations
        rng2 = random.Random(seed + 7)  # separate rng so seed changes don't shift positions
        orientations = []
        for i, n in enumerate(normals):
            q = self._compute_orientation(orient_mode, n, math)
            q = self._apply_random_rotation(q, rand_axis, rand_amount, rng2, math)
            orientations.append(q)

        if orientations:
            instancer.GetOrientationsAttr().Set(Vt.QuathArray(orientations))

        return {"prim": instancer.GetPrim()}

    def _compute_orientation(self, mode, normal, math):
        """Compute base orientation quaternion based on mode."""
        if mode == "Keep Original":
            return Gf.Quath(1, 0, 0, 0)
        elif mode == "Up":
            return Gf.Quath(1, 0, 0, 0)
        elif mode == "Random":
            # Will be fully randomized by _apply_random_rotation with All axis
            return Gf.Quath(1, 0, 0, 0)
        else:  # Align to Normal
            return self._normal_to_quat(normal)

    def _apply_random_rotation(self, base_quat, axis, amount_deg, rng, math):
        """Apply random rotation on top of base orientation."""
        if axis == "Off":
            return base_quat

        amount_rad = math.radians(amount_deg)

        if axis == "All":
            # Random axis + random angle
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
        rand_q = Gf.Quath(math.cos(half), ax[0]*s, ax[1]*s, ax[2]*s)

        # Multiply: rand_q * base_quat
        return self._quat_mul(rand_q, base_quat)

    def _quat_mul(self, a, b):
        """Multiply two Quath quaternions."""
        aw, ax, ay, az = a.GetReal(), *a.GetImaginary()
        bw, bx, by, bz = b.GetReal(), *b.GetImaginary()
        return Gf.Quath(
            aw*bw - ax*bx - ay*by - az*bz,
            aw*bx + ax*bw + ay*bz - az*by,
            aw*by - ax*bz + ay*bw + az*bx,
            aw*bz + ax*by - ay*bx + az*bw)

    def _collect_triangles(self, stage, prim):
        """Collect all triangulated faces from the prim (and descendants) as world-space triangles.
        Returns list of (p0, p1, p2, normal) tuples.
        Falls back to bounding box if no mesh data found."""
        if not prim or not prim.IsValid():
            return []

        triangles = []
        # Use the prim's own stage (not the passed-in stage) to ensure we see
        # authored data from the same execution pass

        # Traverse this prim and all descendants for meshes
        for p in Usd.PrimRange(prim):
            mesh = UsdGeom.Mesh(p)
            points = mesh.GetPointsAttr().Get()
            if not points:
                continue
            face_counts = mesh.GetFaceVertexCountsAttr().Get()
            face_indices = mesh.GetFaceVertexIndicesAttr().Get()
            if not face_counts or not face_indices:
                continue

            # Get world transform
            xform_cache = UsdGeom.XformCache()
            world_xf = xform_cache.GetLocalToWorldTransform(p)

            # Transform points to world space
            world_pts = []
            for pt in points:
                wp = world_xf.Transform(Gf.Vec3d(pt[0], pt[1], pt[2]))
                world_pts.append(Gf.Vec3f(wp[0], wp[1], wp[2]))

            # Triangulate faces
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
                            e1[1]*e2[2] - e1[2]*e2[1],
                            e1[2]*e2[0] - e1[0]*e2[2],
                            e1[0]*e2[1] - e1[1]*e2[0])
                        ln = Gf.GetLength(n)
                        if ln > 1e-8:
                            n = n / ln
                        triangles.append((v0, v1, v2, n))
                idx += fc

        # Fallback: if no mesh triangles found, use bounding box as a flat quad
        if not triangles:
            bbox_cache = UsdGeom.BBoxCache(0, [UsdGeom.Tokens.default_])
            bbox = bbox_cache.ComputeWorldBound(prim)
            rng = bbox.ComputeAlignedRange()
            if not rng.IsEmpty():
                mn = rng.GetMin()
                mx = rng.GetMax()
                # Create a flat quad on the top face of the bbox
                v0 = Gf.Vec3f(mn[0], mn[1], mx[2])
                v1 = Gf.Vec3f(mx[0], mn[1], mx[2])
                v2 = Gf.Vec3f(mx[0], mx[1], mx[2])
                v3 = Gf.Vec3f(mn[0], mx[1], mx[2])
                up = Gf.Vec3f(0, 0, 1)
                triangles.append((v0, v1, v2, up))
                triangles.append((v0, v2, v3, up))

        return triangles

    def _scatter_on_triangles(self, triangles, count, rng):
        """Area-weighted random point sampling on triangles."""
        # Compute areas for weighting
        areas = []
        for v0, v1, v2, _ in triangles:
            e1 = v1 - v0
            e2 = v2 - v0
            cross = Gf.Vec3f(
                e1[1]*e2[2] - e1[2]*e2[1],
                e1[2]*e2[0] - e1[0]*e2[2],
                e1[0]*e2[1] - e1[1]*e2[0])
            areas.append(0.5 * Gf.GetLength(cross))

        total_area = sum(areas)
        if total_area < 1e-10:
            return [], []

        # Build CDF for weighted sampling
        cdf = []
        cumulative = 0.0
        for a in areas:
            cumulative += a / total_area
            cdf.append(cumulative)

        positions = []
        normals = []
        for _ in range(count):
            r = rng.random()
            tri_idx = bisect.bisect_left(cdf, r)
            tri_idx = min(tri_idx, len(triangles) - 1)
            v0, v1, v2, n = triangles[tri_idx]

            # Random point in triangle (barycentric)
            u = rng.random()
            v = rng.random()
            if u + v > 1.0:
                u = 1.0 - u
                v = 1.0 - v
            w = 1.0 - u - v
            pt = Gf.Vec3f(
                w*v0[0] + u*v1[0] + v*v2[0],
                w*v0[1] + u*v1[1] + v*v2[1],
                w*v0[2] + u*v1[2] + v*v2[2])
            positions.append(pt)
            normals.append(n)

        return positions, normals

    def _normal_to_quat(self, normal):
        """Convert a surface normal to a quaternion rotation (Z-up → normal)."""
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
        return Gf.Quath(math.cos(half), axis[0]*s, axis[1]*s, axis[2]*s)
