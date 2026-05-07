"""Composition nodes — references, payloads, layer stack, merge layers."""

from __future__ import annotations

from pxr import Sdf

from ._base import UsdNodeBase, PORT_COLORS


class AddReferenceNode(UsdNodeBase):
    """Add a reference to a prim from a USD file."""

    NODE_NAME = "Add Reference"
    CATEGORY = "Composition"

    def __init__(self) -> None:
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("file_path", "USD File", text="")
        self.add_text_input("prim_path", "Prim Path (optional)", text="")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        if not prim:
            return {"prim": None}
        fp = self.get_property("file_path") or ""
        pp = self.get_property("prim_path") or ""
        ref = Sdf.Reference(fp, Sdf.Path(pp)) if pp else Sdf.Reference(fp)
        prim.GetReferences().AddReference(ref)
        return {"prim": prim}


class AddPayloadNode(UsdNodeBase):
    """Add a payload to a prim."""

    NODE_NAME = "Add Payload"
    CATEGORY = "Composition"

    def __init__(self) -> None:
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("file_path", "USD File", text="")
        self.add_text_input("prim_path", "Prim Path (optional)", text="")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        if not prim:
            return {"prim": None}
        fp = self.get_property("file_path") or ""
        pp = self.get_property("prim_path") or ""
        payload = Sdf.Payload(fp, Sdf.Path(pp)) if pp else Sdf.Payload(fp)
        prim.GetPayloads().AddPayload(payload)
        return {"prim": prim}


class GetLayerStackNode(UsdNodeBase):
    """List all layers in the stage's layer stack."""

    NODE_NAME = "Get Layer Stack"
    CATEGORY = "Composition"

    def __init__(self) -> None:
        super().__init__()
        self.add_output("layers", color=PORT_COLORS["list"])

    def execute(self, stage) -> dict:
        layers = [(layer.identifier, layer.realPath) for layer in stage.GetLayerStack()]
        return {"layers": layers}


class MergeLayersNode(UsdNodeBase):
    """Sublayers two Sdf.Layers into a new anonymous composite layer."""

    NODE_NAME = "Merge Layers"
    CATEGORY = "Composition"

    def __init__(self) -> None:
        super().__init__()
        self.add_input("layer_a", color=PORT_COLORS["layer"])
        self.add_input("layer_b", color=PORT_COLORS["layer"])
        self.add_output("layer", color=PORT_COLORS["layer"])

    def execute(self, stage) -> dict:
        layer_a = self._get_input_value("layer_a")
        layer_b = self._get_input_value("layer_b")
        if not layer_a and not layer_b:
            return {"layer": None}
        merged = Sdf.Layer.CreateAnonymous("merged")
        if layer_a:
            merged.subLayerPaths.append(layer_a.identifier)
        if layer_b:
            merged.subLayerPaths.append(layer_b.identifier)
        return {"layer": merged}


class ReferenceAtNode(UsdNodeBase):
    """References a layer onto a prim at a given path — reuse authored content at any scene location."""

    NODE_NAME = "Reference At"
    CATEGORY = "Composition"

    def __init__(self) -> None:
        super().__init__()
        self.add_input("layer", color=PORT_COLORS["layer"])
        self.add_text_input("prim_path", "Prim Path", text="/ReferenceAt")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        layer = self._get_input_value("layer")
        path = self.get_property("prim_path") or "/ReferenceAt"
        if not layer:
            return {"prim": None}
        prim = stage.OverridePrim(path)
        prim.GetReferences().AddReference(layer.identifier)
        return {"prim": prim if prim.IsValid() else None}
