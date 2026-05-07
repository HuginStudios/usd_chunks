"""Utility/logic nodes — list ops, ForEach, merge, compare, switch, export, counter, strings, booleans, coalesce."""
from __future__ import annotations

from ._base import UsdNodeBase, PORT_COLORS


class ListIndexNode(UsdNodeBase):
    """Get item at index from a list."""
    NODE_NAME = "List Index"
    CATEGORY = "Utility"

    def __init__(self):
        super().__init__()
        self.add_input("list", color=PORT_COLORS["list"])
        self.add_text_input("index", "Index", text="0")
        self.add_output("item", color=PORT_COLORS["any"])

    def execute(self, stage) -> dict:
        lst = self._get_input_value("list") or []
        idx = int(self.get_property("index") or 0)
        return {"item": lst[idx] if 0 <= idx < len(lst) else None}


class ListLengthNode(UsdNodeBase):
    """Get length of a list."""
    NODE_NAME = "List Length"
    CATEGORY = "Utility"

    def __init__(self):
        super().__init__()
        self.add_input("list", color=PORT_COLORS["list"])
        self.add_output("length", color=PORT_COLORS["float"])

    def execute(self, stage) -> dict:
        lst = self._get_input_value("list") or []
        return {"length": len(lst)}


class ForEachNode(UsdNodeBase):
    """Iterate a list — the execution engine re-runs downstream nodes per item."""
    NODE_NAME = "For Each"
    CATEGORY = "Utility"

    def __init__(self):
        super().__init__()
        self.add_input("list", color=PORT_COLORS["list"])
        self.add_output("item", color=PORT_COLORS["any"])
        self.add_output("index", color=PORT_COLORS["float"])
        self._iteration_list = []
        self._current_index = 0

    def execute(self, stage) -> dict:
        lst = self._get_input_value("list") or []
        self._iteration_list = lst
        # Default: output first item. Engine calls set_iteration_index() for each.
        if lst:
            return {"item": lst[self._current_index], "index": self._current_index}
        return {"item": None, "index": -1}

    def set_iteration_index(self, idx):
        self._current_index = idx
        if 0 <= idx < len(self._iteration_list):
            self._output_cache = {"item": self._iteration_list[idx], "index": idx}
        return idx < len(self._iteration_list)


class MergeNode(UsdNodeBase):
    """Merge multiple prims into a list. Connect prim nodes to inputs."""
    NODE_NAME = "Merge"
    CATEGORY = "Utility"

    def __init__(self):
        super().__init__()
        for i in range(6):
            self.add_input(f"in_{i}", color=PORT_COLORS["prim"])
        self.add_output("prims", color=PORT_COLORS["list"])

    def execute(self, stage) -> dict:
        prims = []
        for i in range(6):
            v = self._get_input_value(f"in_{i}")
            if v is not None:
                if isinstance(v, list):
                    prims.extend(v)
                else:
                    prims.append(v)
        return {"prims": prims}


class CompareNode(UsdNodeBase):
    """Compare two values. Outputs bool result."""
    NODE_NAME = "Compare"
    CATEGORY = "Utility"

    def __init__(self):
        super().__init__()
        self.add_input("a", color=PORT_COLORS["any"])
        self.add_input("b", color=PORT_COLORS["any"])
        self.add_text_input("op", "Operator (==, !=, <, >, <=, >=)", text="==")
        self.add_output("result", color=PORT_COLORS["bool"])

    def execute(self, stage) -> dict:
        a = self._get_input_value("a")
        b = self._get_input_value("b")
        op = (self.get_property("op") or "==").strip()
        ops = {"==": lambda x, y: x == y, "!=": lambda x, y: x != y,
               "<": lambda x, y: x < y, ">": lambda x, y: x > y,
               "<=": lambda x, y: x <= y, ">=": lambda x, y: x >= y}
        try:
            return {"result": ops.get(op, ops["=="])(a, b)}
        except Exception:
            return {"result": False}


class SwitchNode(UsdNodeBase):
    """Route one of two inputs based on a bool condition."""
    NODE_NAME = "Switch"
    CATEGORY = "Utility"

    def __init__(self):
        super().__init__()
        self.add_input("condition", color=PORT_COLORS["bool"])
        self.add_input("if_true", color=PORT_COLORS["any"])
        self.add_input("if_false", color=PORT_COLORS["any"])
        self.add_output("output", color=PORT_COLORS["any"])

    def execute(self, stage) -> dict:
        cond = self._get_input_value("condition")
        if cond:
            return {"output": self._get_input_value("if_true")}
        return {"output": self._get_input_value("if_false")}


class ExportUsdNode(UsdNodeBase):
    """Export the current stage (or edit target layer) to a USD file."""
    NODE_NAME = "Export USD"
    CATEGORY = "Utility"

    def __init__(self):
        super().__init__()
        self.add_text_input("file_path", "File Path", text="")
        self.add_text_input("mode", "Mode (stage/layer)", text="stage")
        self.add_output("path", color=PORT_COLORS["string"])

    def execute(self, stage) -> dict:
        fp = (self.get_property("file_path") or "").strip()
        if not fp:
            return {"path": ""}
        mode = (self.get_property("mode") or "stage").lower()
        if mode == "layer":
            stage.GetEditTarget().GetLayer().Export(fp)
        else:
            stage.Export(fp)
        return {"path": fp}


class CounterNode(UsdNodeBase):
    """Increments each time the graph executes. Reset via the text field."""
    NODE_NAME = "Counter"
    CATEGORY = "Utility"
    _counters = {}  # node_id -> count

    def __init__(self):
        super().__init__()
        self.add_text_input("reset_to", "Reset To", text="0")
        self.add_output("count", color=PORT_COLORS["float"])

    def execute(self, stage) -> dict:
        nid = self.id
        reset = self.get_property("reset_to")
        if nid not in CounterNode._counters:
            CounterNode._counters[nid] = int(reset or 0)
        else:
            CounterNode._counters[nid] += 1
        return {"count": CounterNode._counters[nid]}


class StringFormatNode(UsdNodeBase):
    """Format string with {0}, {1}, {2} placeholders from inputs."""
    NODE_NAME = "String Format"
    CATEGORY = "Utility"
    def __init__(self):
        super().__init__()
        self.add_text_input("template", "Template", text="{0}")
        self.add_input("in_0", color=PORT_COLORS["any"])
        self.add_input("in_1", color=PORT_COLORS["any"])
        self.add_input("in_2", color=PORT_COLORS["any"])
        self.add_output("string", color=PORT_COLORS["string"])
    def execute(self, stage) -> dict:
        t = self.get_property("template") or "{0}"
        args = [self._get_input_value(f"in_{i}") or "" for i in range(3)]
        try:
            return {"string": t.format(*args)}
        except Exception:
            return {"string": t}


class StringSplitNode(UsdNodeBase):
    NODE_NAME = "String Split"
    CATEGORY = "Utility"
    def __init__(self):
        super().__init__()
        self.add_input("string", color=PORT_COLORS["string"])
        self.add_text_input("delimiter", "Delimiter", text="/")
        self.add_output("parts", color=PORT_COLORS["list"])
    def execute(self, stage) -> dict:
        s = str(self._get_input_value("string") or "")
        d = self.get_property("delimiter") or "/"
        return {"parts": s.split(d)}


class StringJoinNode(UsdNodeBase):
    NODE_NAME = "String Join"
    CATEGORY = "Utility"
    def __init__(self):
        super().__init__()
        self.add_input("list", color=PORT_COLORS["list"])
        self.add_text_input("delimiter", "Delimiter", text=", ")
        self.add_output("string", color=PORT_COLORS["string"])
    def execute(self, stage) -> dict:
        lst = self._get_input_value("list") or []
        d = self.get_property("delimiter") or ", "
        return {"string": d.join(str(x) for x in lst)}


class StringReplaceNode(UsdNodeBase):
    NODE_NAME = "String Replace"
    CATEGORY = "Utility"
    def __init__(self):
        super().__init__()
        self.add_input("string", color=PORT_COLORS["string"])
        self.add_text_input("find", "Find", text="")
        self.add_text_input("replace", "Replace", text="")
        self.add_output("string", color=PORT_COLORS["string"])
    def execute(self, stage) -> dict:
        s = str(self._get_input_value("string") or "")
        f = self.get_property("find") or ""
        r = self.get_property("replace") or ""
        return {"string": s.replace(f, r) if f else s}


class BoolAndNode(UsdNodeBase):
    NODE_NAME = "And"
    CATEGORY = "Utility"
    def __init__(self):
        super().__init__()
        self.add_input("a", color=PORT_COLORS["bool"])
        self.add_input("b", color=PORT_COLORS["bool"])
        self.add_output("result", color=PORT_COLORS["bool"])
    def execute(self, stage) -> dict:
        return {"result": bool(self._get_input_value("a")) and bool(self._get_input_value("b"))}


class BoolOrNode(UsdNodeBase):
    NODE_NAME = "Or"
    CATEGORY = "Utility"
    def __init__(self):
        super().__init__()
        self.add_input("a", color=PORT_COLORS["bool"])
        self.add_input("b", color=PORT_COLORS["bool"])
        self.add_output("result", color=PORT_COLORS["bool"])
    def execute(self, stage) -> dict:
        return {"result": bool(self._get_input_value("a")) or bool(self._get_input_value("b"))}


class BoolNotNode(UsdNodeBase):
    NODE_NAME = "Not"
    CATEGORY = "Utility"
    def __init__(self):
        super().__init__()
        self.add_input("value", color=PORT_COLORS["bool"])
        self.add_output("result", color=PORT_COLORS["bool"])
    def execute(self, stage) -> dict:
        return {"result": not bool(self._get_input_value("value"))}


class CoalesceNode(UsdNodeBase):
    """Returns the first non-null / valid input. For prims, checks IsValid()."""
    NODE_NAME = "Coalesce"
    CATEGORY = "Utility"
    def __init__(self):
        super().__init__()
        self.add_input("a", color=PORT_COLORS["prim"])
        self.add_input("b", color=PORT_COLORS["prim"])
        self.add_input("c", color=PORT_COLORS["prim"])
        self.add_output("prim", color=PORT_COLORS["prim"])
        self.add_output("index", color=PORT_COLORS["float"])
    def execute(self, stage) -> dict:
        for i, name in enumerate(["a", "b", "c"]):
            v = self._get_input_value(name)
            if v is None:
                continue
            if hasattr(v, "IsValid") and not v.IsValid():
                continue
            return {"prim": v, "index": i}
        return {"prim": None, "index": -1}
