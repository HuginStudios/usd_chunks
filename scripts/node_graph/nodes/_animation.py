"""Animation nodes — set keys, get time range, set time codes."""
from __future__ import annotations

from pxr import Usd

from ._base import UsdNodeBase, PORT_COLORS


class SetKeyNode(UsdNodeBase):
    """Set an attribute value at a specific timecode."""
    NODE_NAME = "Set Key"
    CATEGORY = "Animation"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("attr_name", "Attribute", text="xformOp:translate")
        self.add_text_input("time", "Time", text="0")
        self.add_text_input("value", "Value", text="(0,0,0)")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        if not prim:
            return {"prim": None}
        attr = prim.GetAttribute(self.get_property("attr_name") or "")
        if attr and attr.IsValid():
            t = float(self.get_property("time") or 0)
            v = self.get_property("value") or ""
            try:
                val = eval(v)
            except Exception:
                val = v
            attr.Set(val, Usd.TimeCode(t))
        return {"prim": prim}


class GetTimeRangeNode(UsdNodeBase):
    """Get stage start/end frame."""
    NODE_NAME = "Get Time Range"
    CATEGORY = "Animation"

    def __init__(self):
        super().__init__()
        self.add_output("start", color=PORT_COLORS["float"])
        self.add_output("end", color=PORT_COLORS["float"])

    def execute(self, stage) -> dict:
        return {"start": stage.GetStartTimeCode(), "end": stage.GetEndTimeCode()}


class SetTimeCodeNode(UsdNodeBase):
    """Set stage start and end time codes."""
    NODE_NAME = "Set Time Codes"
    CATEGORY = "Animation"

    def __init__(self):
        super().__init__()
        self.add_text_input("start", "Start Frame", text="0")
        self.add_text_input("end", "End Frame", text="100")

    def execute(self, stage) -> dict:
        start = float(self.get_property("start") or 0)
        end = float(self.get_property("end") or 100)
        stage.SetStartTimeCode(start)
        stage.SetEndTimeCode(end)
        return {}
