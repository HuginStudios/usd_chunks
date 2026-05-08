"""Animation nodes — set keys, get time range, set time codes."""

from __future__ import annotations

from pxr import Usd

from ._base import UsdNodeBase, PORT_COLORS


class SetKeyNode(UsdNodeBase):
    """Set an attribute value at a specific timecode."""

    NODE_NAME = "Set Key"
    CATEGORY = "Animation"

    def __init__(self) -> None:
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("attr_name", "Attribute", text="xformOp:translate")
        self.add_text_input("time", "Time", text="0")
        self.add_text_input("value", "Value", text="(0,0,0)")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        from pxr import UsdGeom, Gf, Sdf, Vt

        prim = self._get_input_value("prim")
        if not prim:
            return {"prim": None}
        attr_name = self.get_property("attr_name") or ""
        attr = prim.GetAttribute(attr_name)

        # If it's an xformOp that doesn't exist yet, create it via Xformable
        if not attr or not attr.IsValid():
            if attr_name.startswith("xformOp:"):
                xformable = UsdGeom.Xformable(prim)
                op_type = attr_name.replace("xformOp:", "")
                if op_type == "translate":
                    xformable.AddTranslateOp()
                elif op_type == "rotateXYZ":
                    xformable.AddRotateXYZOp()
                elif op_type == "scale":
                    xformable.AddScaleOp()
                elif op_type == "transform":
                    xformable.AddTransformOp()
                attr = prim.GetAttribute(attr_name)

        if attr and attr.IsValid():
            t = float(self.get_property("time") or 0)
            v = self.get_property("value") or ""
            try:
                raw = eval(v)
                # Convert tuples to appropriate Gf types based on attribute type
                type_name = attr.GetTypeName()
                if isinstance(raw, tuple):
                    if len(raw) == 3:
                        if "double" in str(type_name) or "d" in str(type_name):
                            raw = Gf.Vec3d(*raw)
                        else:
                            raw = Gf.Vec3f(*raw)
                val = raw
            except Exception:
                val = v
            attr.Set(val, Usd.TimeCode(t))
        return {"prim": prim}


class GetTimeRangeNode(UsdNodeBase):
    """Get stage start/end frame."""

    NODE_NAME = "Get Time Range"
    CATEGORY = "Animation"

    def __init__(self) -> None:
        super().__init__()
        self.add_output("start", color=PORT_COLORS["float"])
        self.add_output("end", color=PORT_COLORS["float"])

    def execute(self, stage) -> dict:
        return {"start": stage.GetStartTimeCode(), "end": stage.GetEndTimeCode()}


class SetTimeCodeNode(UsdNodeBase):
    """Set stage start and end time codes."""

    NODE_NAME = "Set Time Codes"
    CATEGORY = "Animation"

    def __init__(self) -> None:
        super().__init__()
        self.add_text_input("start", "Start Frame", text="0")
        self.add_text_input("end", "End Frame", text="100")

    def execute(self, stage) -> dict:
        start = float(self.get_property("start") or 0)
        end = float(self.get_property("end") or 100)
        stage.SetStartTimeCode(start)
        stage.SetEndTimeCode(end)
        return {}
