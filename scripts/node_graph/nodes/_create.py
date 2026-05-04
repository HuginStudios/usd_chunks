"""Shape/primitive creation nodes."""
from ._base import UsdNodeBase, PORT_COLORS


class CreateCubeNode(UsdNodeBase):
    NODE_NAME = "Create Cube"
    CATEGORY = "Create"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Path", text="/Cube")
        self.add_text_input("size", "Size", text="1.0")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage):
        from pxr import UsdGeom
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
    def execute(self, stage):
        from pxr import UsdGeom
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
    def execute(self, stage):
        from pxr import UsdGeom
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
    def execute(self, stage):
        from pxr import UsdGeom
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
    def execute(self, stage):
        from pxr import UsdGeom
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
    def execute(self, stage):
        from pxr import UsdGeom, Vt, Gf
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
    def execute(self, stage):
        from pxr import UsdGeom
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
    def execute(self, stage):
        from pxr import UsdGeom, Vt, Gf, Sdf
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
    def execute(self, stage):
        prim = self._get_input_value("prim")
        if prim:
            v = (self.get_property("instanceable") or "true").lower() in ("true", "1")
            prim.SetInstanceable(v)
        return {"prim": prim}
