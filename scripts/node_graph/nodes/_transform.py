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


class OffsetTransformNode(UsdNodeBase):
    """Add translate/rotate/scale offset to a prim's existing local transform.

    Reads the current xform ops and adds/multiplies the given deltas on top.
    In a chained graph (Offset → Offset → ...) each node adds its own delta
    to whatever the upstream node wrote, yielding correct cumulative results
    when the graph executes top-down.
    """

    NODE_NAME = "Offset Transform"
    CATEGORY = "Transform"

    _ROTATE_TYPES = frozenset(
        [
            UsdGeom.XformOp.TypeRotateXYZ,
            UsdGeom.XformOp.TypeRotateXZY,
            UsdGeom.XformOp.TypeRotateYXZ,
            UsdGeom.XformOp.TypeRotateYZX,
            UsdGeom.XformOp.TypeRotateZXY,
            UsdGeom.XformOp.TypeRotateZYX,
        ]
    )

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
        prim = self._get_input_value("prim")

        if not prim:
            # Wire cut: remove our edits from whichever layer we wrote to
            if getattr(self, "_last_prim_path", None):
                _XFORM_PROPS = (
                    "xformOp:translate",
                    "xformOp:rotateXYZ",
                    "xformOp:scale",
                    "xformOpOrder",
                )
                for layer in stage.GetLayerStack(includeSessionLayers=True):
                    spec = layer.GetPrimAtPath(self._last_prim_path)
                    if spec and any(p in spec.properties for p in _XFORM_PROPS):
                        for prop_name in _XFORM_PROPS:
                            prop = spec.properties.get(prop_name)
                            if prop:
                                spec.RemoveProperty(prop)
                        break
                self._last_prim_path = None
            self._last_applied = None
            return {"prim": None}

        prim_path = prim.GetPath()
        prim_path_str = str(prim_path)
        xformable = UsdGeom.Xformable(prim)

        cur_t = Gf.Vec3d(0.0, 0.0, 0.0)
        cur_r = Gf.Vec3f(0.0, 0.0, 0.0)
        cur_s = Gf.Vec3f(1.0, 1.0, 1.0)
        _found_trs = False
        for op in xformable.GetOrderedXformOps():
            v = op.Get(0)
            if v is None:
                continue
            op_type = op.GetOpType()
            if op_type == UsdGeom.XformOp.TypeTranslate:
                cur_t = Gf.Vec3d(float(v[0]), float(v[1]), float(v[2]))
                _found_trs = True
            elif op_type in self._ROTATE_TYPES:
                cur_r = Gf.Vec3f(float(v[0]), float(v[1]), float(v[2]))
                _found_trs = True
            elif op_type == UsdGeom.XformOp.TypeScale:
                cur_s = Gf.Vec3f(float(v[0]), float(v[1]), float(v[2]))
                _found_trs = True

        if not _found_trs and getattr(self, "_last_prim_path", None) == prim_path:
            # xformOpOrder has been taken over by a matrix-based op (e.g. RBDSolverNode
            # wrote xformOp:transform after we ran). Our own translate/rotateXYZ are still
            # in the session layer but are bypassed by the current xformOpOrder. Read them
            # directly from the session layer so the _last_applied subtraction stays correct.
            _session = stage.GetSessionLayer()
            _spec = _session.GetPrimAtPath(prim_path)
            if _spec:
                _t = _spec.properties.get("xformOp:translate")
                _r = _spec.properties.get("xformOp:rotateXYZ")
                _s = _spec.properties.get("xformOp:scale")
                if _t is not None and _t.default is not None:
                    v = _t.default
                    cur_t = Gf.Vec3d(float(v[0]), float(v[1]), float(v[2]))
                if _r is not None and _r.default is not None:
                    v = _r.default
                    cur_r = Gf.Vec3f(float(v[0]), float(v[1]), float(v[2]))
                if _s is not None and _s.default is not None:
                    v = _s.default
                    cur_s = Gf.Vec3f(float(v[0]), float(v[1]), float(v[2]))

        # Subtract what we applied last run to recover the upstream base transform.
        # Without this, re-execution accumulates our own previous writes.
        last = getattr(self, "_last_applied", None)
        if last and last["path"] == prim_path_str:
            lt, lr, ls = last["t"], last["r"], last["s"]
            base_t = Gf.Vec3d(cur_t[0] - lt[0], cur_t[1] - lt[1], cur_t[2] - lt[2])
            base_r = Gf.Vec3f(cur_r[0] - lr[0], cur_r[1] - lr[1], cur_r[2] - lr[2])
            base_s = Gf.Vec3f(
                cur_s[0] / ls[0] if ls[0] else cur_s[0],
                cur_s[1] / ls[1] if ls[1] else cur_s[1],
                cur_s[2] / ls[2] if ls[2] else cur_s[2],
            )
        else:
            base_t, base_r, base_s = cur_t, cur_r, cur_s

        otx = float(self.get_property("tx") or 0)
        oty = float(self.get_property("ty") or 0)
        otz = float(self.get_property("tz") or 0)
        orx = float(self.get_property("rx") or 0)
        ory = float(self.get_property("ry") or 0)
        orz = float(self.get_property("rz") or 0)
        osx = float(self.get_property("sx") or 1)
        osy = float(self.get_property("sy") or 1)
        osz = float(self.get_property("sz") or 1)

        new_t = Gf.Vec3d(base_t[0] + otx, base_t[1] + oty, base_t[2] + otz)
        new_r = Gf.Vec3f(base_r[0] + orx, base_r[1] + ory, base_r[2] + orz)
        new_s = Gf.Vec3f(base_s[0] * osx, base_s[1] * osy, base_s[2] * osz)

        self._last_applied = {
            "path": prim_path_str,
            "t": (otx, oty, otz),
            "r": (orx, ory, orz),
            "s": (osx, osy, osz),
        }
        self._last_prim_path = prim_path

        xformable.ClearXformOpOrder()
        xformable.AddTranslateOp().Set(new_t)
        xformable.AddRotateXYZOp().Set(new_r)
        if new_s != Gf.Vec3f(1.0, 1.0, 1.0):
            xformable.AddScaleOp().Set(new_s)

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
