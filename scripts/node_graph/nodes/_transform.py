"""Transform nodes — translate, rotate, scale, bounding box, constraint."""

from __future__ import annotations

import json

from pxr import Gf, UsdGeom

from ._base import UsdNodeBase, PORT_COLORS


class SetTransformNode(UsdNodeBase):
    """Set translate/rotate/scale on a prim."""

    NODE_NAME = "Set Transform"
    CATEGORY = "Transform"

    def __init__(self) -> None:
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("tx", "Translate X", text="0")
        self.add_text_input("ty", "Translate Y", text="0")
        self.add_text_input("tz", "Translate Z", text="0")
        self.add_text_input("rx", "Rotate X", text="0")
        self.add_text_input("ry", "Rotate Y", text="0")
        self.add_text_input("rz", "Rotate Z", text="0")
        self.add_text_input("sx", "Scale X", text="1")
        self.add_text_input("sy", "Scale Y", text="1")
        self.add_text_input("sz", "Scale Z", text="1")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        from pxr import Sdf

        prim = self._get_input_value("prim")
        if not prim:
            # Clean up previous edits if we had a prim before
            if hasattr(self, "_last_prim_path") and self._last_prim_path:
                session = stage.GetSessionLayer()
                spec = session.GetPrimAtPath(self._last_prim_path)
                if spec:
                    for prop_name in [
                        "xformOp:translate",
                        "xformOp:rotateXYZ",
                        "xformOp:scale",
                        "xformOpOrder",
                    ]:
                        if session.GetPropertyAtPath(
                            self._last_prim_path.AppendProperty(prop_name)
                        ):
                            spec.RemoveProperty(spec.properties[prop_name])
                self._last_prim_path = None
            return {"prim": None}
        self._last_prim_path = prim.GetPath()
        xform = UsdGeom.Xformable(prim)
        xform.ClearXformOpOrder()
        xform.AddTranslateOp().Set(
            Gf.Vec3d(
                float(self.get_property("tx") or 0),
                float(self.get_property("ty") or 0),
                float(self.get_property("tz") or 0),
            )
        )
        xform.AddRotateXYZOp().Set(
            Gf.Vec3f(
                float(self.get_property("rx") or 0),
                float(self.get_property("ry") or 0),
                float(self.get_property("rz") or 0),
            )
        )
        s = Gf.Vec3f(
            float(self.get_property("sx") or 1),
            float(self.get_property("sy") or 1),
            float(self.get_property("sz") or 1),
        )
        if s != Gf.Vec3f(1, 1, 1):
            xform.AddScaleOp().Set(Gf.Vec3f(s))
        return {"prim": prim}


class GetWorldTransformNode(UsdNodeBase):
    """Get world-space transform of a prim — individual components for easy referencing."""

    NODE_NAME = "Get World Transform"
    CATEGORY = "Transform"

    def __init__(self) -> None:
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_output("tx", color=PORT_COLORS["float"])
        self.add_output("ty", color=PORT_COLORS["float"])
        self.add_output("tz", color=PORT_COLORS["float"])
        self.add_output("rx", color=PORT_COLORS["float"])
        self.add_output("ry", color=PORT_COLORS["float"])
        self.add_output("rz", color=PORT_COLORS["float"])
        self.add_output("sx", color=PORT_COLORS["float"])
        self.add_output("sy", color=PORT_COLORS["float"])
        self.add_output("sz", color=PORT_COLORS["float"])
        self.add_output("matrix", color=PORT_COLORS["any"])
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        if prim is None:
            return {
                "tx": 0,
                "ty": 0,
                "tz": 0,
                "rx": 0,
                "ry": 0,
                "rz": 0,
                "sx": 1,
                "sy": 1,
                "sz": 1,
                "matrix": None,
                "prim": None,
            }
        xf = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(0)
        t = xf.ExtractTranslation()
        r = xf.ExtractRotation()
        rot = r.Decompose(Gf.Vec3d.XAxis(), Gf.Vec3d.YAxis(), Gf.Vec3d.ZAxis())
        # Extract scale from matrix
        m = xf
        sx = Gf.Vec3d(m[0][0], m[0][1], m[0][2]).GetLength()
        sy = Gf.Vec3d(m[1][0], m[1][1], m[1][2]).GetLength()
        sz = Gf.Vec3d(m[2][0], m[2][1], m[2][2]).GetLength()
        return {
            "tx": t[0],
            "ty": t[1],
            "tz": t[2],
            "rx": rot[0],
            "ry": rot[1],
            "rz": rot[2],
            "sx": sx,
            "sy": sy,
            "sz": sz,
            "matrix": xf,
            "prim": prim,
        }


class ResetTransformNode(UsdNodeBase):
    """Clear all xform ops on a prim."""

    NODE_NAME = "Reset Transform"
    CATEGORY = "Transform"

    def __init__(self) -> None:
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        if prim:
            UsdGeom.Xformable(prim).ClearXformOpOrder()
        return {"prim": prim}


class GetBoundingBoxNode(UsdNodeBase):
    """Get bounding box of a prim."""

    NODE_NAME = "Get Bounding Box"
    CATEGORY = "Transform"

    def __init__(self) -> None:
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_output("min", color=PORT_COLORS["any"])
        self.add_output("max", color=PORT_COLORS["any"])
        self.add_output("size", color=PORT_COLORS["any"])
        self.add_output("center", color=PORT_COLORS["any"])

    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        if not prim:
            return {"min": None, "max": None, "size": None, "center": None}
        bbox = UsdGeom.BBoxCache(0, [UsdGeom.Tokens.default_]).ComputeWorldBound(prim)
        r = bbox.ComputeAlignedRange()
        mn, mx = r.GetMin(), r.GetMax()
        sz = mx - mn
        ct = (mn + mx) / 2
        return {
            "min": tuple(mn),
            "max": tuple(mx),
            "size": tuple(sz),
            "center": tuple(ct),
        }


class ConstrainTransformNode(UsdNodeBase):
    """Constrain target prim(s) to follow source prim's world transform.
    Unchecked: snap all targets to source. Checked: each target maintains
    its own relative offset, captured automatically on first execute.
    Clear the offset_data field to force a re-capture on all targets."""

    NODE_NAME = "Constrain Transform"
    CATEGORY = "Transform"

    def __init__(self) -> None:
        super().__init__()
        self.add_input("source", color=PORT_COLORS["prim"])
        self.add_input("target", color=PORT_COLORS["list"])
        self.add_checkbox(
            "maintain_offset",
            label="Maintain Offset",
            text="Maintain Offset",
            state=False,
        )
        # JSON dict: {"/prim/path": [tx, ty, tz, rx, ry, rz], ...} — one entry per target
        self.add_text_input("offset_data", "Offsets (auto)", text="")
        self.add_output("prim", color=PORT_COLORS["prim"])
        self.add_output("prims", color=PORT_COLORS["list"])

    def execute(self, stage) -> dict:
        source = self._get_input_value("source")
        targets = self._get_prims("target")
        if not source or not targets:
            return {"prim": None, "prims": []}

        maintain = bool(self.get_property("maintain_offset"))

        src_xf = UsdGeom.Xformable(source).ComputeLocalToWorldTransform(0)
        src_t = src_xf.ExtractTranslation()
        src_rot = src_xf.ExtractRotation().Decompose(
            Gf.Vec3d.XAxis(), Gf.Vec3d.YAxis(), Gf.Vec3d.ZAxis()
        )

        if maintain:
            raw = self.get_property("offset_data") or ""
            try:
                offsets = json.loads(raw) if raw.strip() else {}
            except Exception:
                offsets = {}

            # Auto-capture any target not yet in the dict
            new_captures = False
            for target in targets:
                path = str(target.GetPath())
                if path not in offsets:
                    tgt_xf = UsdGeom.Xformable(target).ComputeLocalToWorldTransform(0)
                    tgt_t = tgt_xf.ExtractTranslation()
                    tgt_rot = tgt_xf.ExtractRotation().Decompose(
                        Gf.Vec3d.XAxis(), Gf.Vec3d.YAxis(), Gf.Vec3d.ZAxis()
                    )
                    offsets[path] = [
                        round(tgt_t[0] - src_t[0], 4),
                        round(tgt_t[1] - src_t[1], 4),
                        round(tgt_t[2] - src_t[2], 4),
                        round(tgt_rot[0] - src_rot[0], 4),
                        round(tgt_rot[1] - src_rot[1], 4),
                        round(tgt_rot[2] - src_rot[2], 4),
                    ]
                    new_captures = True
            if new_captures:
                self.set_property("offset_data", json.dumps(offsets))

            for target in targets:
                path = str(target.GetPath())
                o = offsets.get(path, [0, 0, 0, 0, 0, 0])
                xform = UsdGeom.Xformable(target)
                xform.ClearXformOpOrder()
                xform.AddTranslateOp().Set(
                    Gf.Vec3d(src_t[0] + o[0], src_t[1] + o[1], src_t[2] + o[2])
                )
                xform.AddRotateXYZOp().Set(
                    Gf.Vec3f(
                        float(src_rot[0] + o[3]),
                        float(src_rot[1] + o[4]),
                        float(src_rot[2] + o[5]),
                    )
                )
        else:
            for target in targets:
                xform = UsdGeom.Xformable(target)
                xform.ClearXformOpOrder()
                xform.AddTranslateOp().Set(Gf.Vec3d(src_t[0], src_t[1], src_t[2]))
                xform.AddRotateXYZOp().Set(
                    Gf.Vec3f(float(src_rot[0]), float(src_rot[1]), float(src_rot[2]))
                )

        return {"prim": targets[0], "prims": targets}
