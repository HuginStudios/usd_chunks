"""Math nodes — arithmetic on float values."""
from __future__ import annotations

from ._base import UsdNodeBase, PORT_COLORS


class MathAddNode(UsdNodeBase):
    NODE_NAME = "Add"
    CATEGORY = "Math"

    def __init__(self):
        super().__init__()
        self.add_input("a", color=PORT_COLORS["float"])
        self.add_input("b", color=PORT_COLORS["float"])
        self.add_text_input("b_val", "B (if no input)", text="0")
        self.add_output("result", color=PORT_COLORS["float"])

    def execute(self, stage) -> dict:
        a = self._get_input_value("a") or 0
        b = self._get_input_value("b")
        if b is None:
            b = float(self.get_property("b_val") or 0)
        return {"result": float(a) + float(b)}


class MathSubtractNode(UsdNodeBase):
    NODE_NAME = "Subtract"
    CATEGORY = "Math"

    def __init__(self):
        super().__init__()
        self.add_input("a", color=PORT_COLORS["float"])
        self.add_input("b", color=PORT_COLORS["float"])
        self.add_text_input("b_val", "B (if no input)", text="0")
        self.add_output("result", color=PORT_COLORS["float"])

    def execute(self, stage) -> dict:
        a = self._get_input_value("a") or 0
        b = self._get_input_value("b")
        if b is None:
            b = float(self.get_property("b_val") or 0)
        return {"result": float(a) - float(b)}


class MathMultiplyNode(UsdNodeBase):
    NODE_NAME = "Multiply"
    CATEGORY = "Math"

    def __init__(self):
        super().__init__()
        self.add_input("a", color=PORT_COLORS["float"])
        self.add_input("b", color=PORT_COLORS["float"])
        self.add_text_input("b_val", "B (if no input)", text="1")
        self.add_output("result", color=PORT_COLORS["float"])

    def execute(self, stage) -> dict:
        a = self._get_input_value("a") or 0
        b = self._get_input_value("b")
        if b is None:
            b = float(self.get_property("b_val") or 1)
        return {"result": float(a) * float(b)}


class MathDivideNode(UsdNodeBase):
    NODE_NAME = "Divide"
    CATEGORY = "Math"

    def __init__(self):
        super().__init__()
        self.add_input("a", color=PORT_COLORS["float"])
        self.add_input("b", color=PORT_COLORS["float"])
        self.add_text_input("b_val", "B (if no input)", text="1")
        self.add_output("result", color=PORT_COLORS["float"])

    def execute(self, stage) -> dict:
        a = self._get_input_value("a") or 0
        b = self._get_input_value("b")
        if b is None:
            b = float(self.get_property("b_val") or 1)
        return {"result": float(a) / float(b) if float(b) != 0 else 0.0}


class MathClampNode(UsdNodeBase):
    NODE_NAME = "Clamp"
    CATEGORY = "Math"

    def __init__(self):
        super().__init__()
        self.add_input("value", color=PORT_COLORS["float"])
        self.add_text_input("min_val", "Min", text="0")
        self.add_text_input("max_val", "Max", text="1")
        self.add_output("result", color=PORT_COLORS["float"])

    def execute(self, stage) -> dict:
        v = float(self._get_input_value("value") or 0)
        mn = float(self.get_property("min_val") or 0)
        mx = float(self.get_property("max_val") or 1)
        return {"result": max(mn, min(mx, v))}


class MathRemapNode(UsdNodeBase):
    NODE_NAME = "Remap"
    CATEGORY = "Math"

    def __init__(self):
        super().__init__()
        self.add_input("value", color=PORT_COLORS["float"])
        self.add_text_input("in_min", "In Min", text="0")
        self.add_text_input("in_max", "In Max", text="1")
        self.add_text_input("out_min", "Out Min", text="0")
        self.add_text_input("out_max", "Out Max", text="100")
        self.add_output("result", color=PORT_COLORS["float"])

    def execute(self, stage) -> dict:
        v = float(self._get_input_value("value") or 0)
        i0 = float(self.get_property("in_min") or 0)
        i1 = float(self.get_property("in_max") or 1)
        o0 = float(self.get_property("out_min") or 0)
        o1 = float(self.get_property("out_max") or 100)
        if i1 == i0:
            return {"result": o0}
        t = (v - i0) / (i1 - i0)
        return {"result": o0 + t * (o1 - o0)}
