"""Code nodes — Python scripting inside the graph."""

from __future__ import annotations

from ._base import UsdNodeBase, PORT_COLORS


class PythonNode(UsdNodeBase):
    """Execute custom Python code. Available variables: stage, inputs (dict of all input values).
    Set 'output' variable to pass a result downstream.

    REMEMBER: THE "inputs" IS A DICT OF ALL INPUT PORT VALUES, {"in_1": value1, "in_2": value2, ...}
    """

    NODE_NAME = "Python"
    CATEGORY = "Code"

    _OUTPUT_TYPES = ["any", "prim", "layer", "string", "float", "list", "bool"]

    def __init__(self) -> None:
        super().__init__()
        self.add_input("in_1", color=PORT_COLORS["any"])
        self.add_input("in_2", color=PORT_COLORS["any"])
        self.add_input("in_3", color=PORT_COLORS["any"])
        self.add_text_input("code", "Code", text="output = inputs")
        self.add_combo_menu("output_type", "Output Type", items=self._OUTPUT_TYPES)
        self.add_output("result", color=PORT_COLORS["any"])

    def execute(self, stage) -> dict:
        inputs = {}
        for port in self.input_ports():
            inputs[port.name()] = self._get_input_value(port.name())
        code = self.get_property("code") or ""
        local_vars = {"stage": stage, "inputs": inputs, "output": None}
        try:
            exec(code, {"__builtins__": __builtins__}, local_vars)
        except Exception as e:
            print(f"[PythonNode] Error: {e}")
            raise  # Let execution engine catch it for red indicator

        dtype = self.get_property("output_type") or "any"
        port = self.get_output("result")
        if port:
            new_color = PORT_COLORS.get(dtype, PORT_COLORS["any"])
            if port.connected_ports() and dtype != "any":
                print(
                    f"[PythonNode] Warning: output type changed to '{dtype}' "
                    "while connections exist — existing wires are not re-validated"
                )
            try:
                port.view.color = new_color
                port.view._data_type = dtype
            except Exception:
                pass

        return {"result": local_vars.get("output")}

    def to_python(self, var_map: dict) -> str:
        code = self.get_property("code") or ""
        return f"# Python node\n{code}"
