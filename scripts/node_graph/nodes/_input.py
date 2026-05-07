"""Input nodes — constant values and prim selection."""
from __future__ import annotations

import fnmatch

from PySide6 import QtCore

from ._base import UsdNodeBase, PORT_COLORS
from ._base import _NodePreviewWidget


class PrimPathNode(UsdNodeBase):
    """Resolves a prim path to a Usd.Prim. Supports glob patterns (* ?) to match multiple prims."""
    NODE_NAME = "Prim Path"
    CATEGORY = "Input"

    def __init__(self):
        super().__init__()
        self.add_text_input("prim_path", "Prim Path", text="")
        self.view.setToolTip(
            "Resolves a prim path to a prim.\n"
            "Tip: add an OnStageChanged or OnAttributeChanged node\n"
            "and connect its trigger to re-execute when the prim moves."
        )
        # Compact preview chip
        self._preview = _NodePreviewWidget(self.view, name="preview")
        self.add_custom_widget(self._preview)
        self.add_output("prim", color=PORT_COLORS["prim"])
        self.add_output("prims", color=PORT_COLORS["list"])
        self.add_output("count", color=PORT_COLORS["float"])
        QtCore.QTimer.singleShot(0, lambda: self._on_resize(260))

    def execute(self, stage) -> dict:
        path = self.get_property("prim_path") or ""
        if not path:
            self._preview.set_value("")
            return {"prim": None, "prims": [], "count": 0}
        # Glob mode: */ChairB_* or /Kitchen_set/**/Mesh*
        if "*" in path or "?" in path:
            matched = [p for p in stage.Traverse()
                       if fnmatch.fnmatch(str(p.GetPath()), path)
                       or fnmatch.fnmatch(p.GetName(), path.split("/")[-1])]
            count = len(matched)
            name = path.split("/")[-1] or path
            self._preview.set_value(f"● {name}  ×{count}" if count else "✗  no match")
            return {
                "prim": matched[0] if matched else None,
                "prims": matched,
                "count": count,
            }
        prim = stage.GetPrimAtPath(path)
        valid = prim and prim.IsValid()
        if valid:
            name = path.rstrip("/").rsplit("/", 1)[-1] or "/"
            self._preview.set_value(f"● {name}")
        else:
            self._preview.set_value("✗  no prim")
        return {"prim": prim if valid else None, "prims": [prim] if valid else [], "count": 1 if valid else 0}

    def to_python(self, var_map: dict) -> str:
        path = self.get_property("prim_path") or "/"
        v = var_map.get("prim", "prim")
        return f'{v} = stage.GetPrimAtPath("{path}")'


class SelectedPrimNode(UsdNodeBase):
    """Outputs the currently selected prim(s) from usdview."""
    NODE_NAME = "Selected Prim"
    CATEGORY = "Input"

    def __init__(self):
        super().__init__()
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        try:
            from ..graph_host import GraphHost  # usdview-guarded
            prims = GraphHost.current().get_selection()
            return {"prim": prims[0] if prims else None}
        except Exception:
            return {"prim": None}


class StringNode(UsdNodeBase):
    """A simple string constant."""
    NODE_NAME = "String"
    CATEGORY = "Input"

    def __init__(self):
        super().__init__()
        self.add_text_input("value", "Value", text="")
        self._preview = _NodePreviewWidget(self.view, name="preview")
        self.add_custom_widget(self._preview)
        self.add_output("string", color=PORT_COLORS["string"])

    def execute(self, stage) -> dict:
        val = self.get_property("value") or ""
        self._preview.set_value(f'"{val[:36]}"' if val else '""')
        return {"string": val}


class FloatNode(UsdNodeBase):
    """A simple float constant."""
    NODE_NAME = "Float"
    CATEGORY = "Input"

    def __init__(self):
        super().__init__()
        self.add_text_input("value", "Value", text="0.0")
        self.add_output("float", color=PORT_COLORS["float"])

    def execute(self, stage) -> dict:
        try:
            return {"float": float(self.get_property("value") or 0)}
        except ValueError:
            return {"float": 0.0}


class BoolNode(UsdNodeBase):
    """A boolean toggle constant."""
    NODE_NAME = "Bool"
    CATEGORY = "Input"

    def __init__(self):
        super().__init__()
        self.add_text_input("value", "Value (true/false)", text="true")
        self.add_output("bool", color=PORT_COLORS["bool"])

    def execute(self, stage) -> dict:
        v = (self.get_property("value") or "").lower()
        return {"bool": v in ("true", "1", "yes")}
