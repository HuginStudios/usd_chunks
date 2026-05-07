"""USD inspection/query nodes."""
from __future__ import annotations

from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade, Vt

from ._base import UsdNodeBase, PORT_COLORS


class GetVariantsNode(UsdNodeBase):
    """Lists variant sets and their options for a prim."""
    NODE_NAME = "Get Variants"
    CATEGORY = "USD"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_output("variant_sets", color=PORT_COLORS["list"])
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        if not prim:
            return {"variant_sets": {}, "prim": None}
        vsets = prim.GetVariantSets()
        result = {}
        for name in vsets.GetNames():
            vs = vsets.GetVariantSet(name)
            result[name] = {
                "options": vs.GetVariantNames(),
                "selected": vs.GetVariantSelection(),
            }
        return {"variant_sets": result, "prim": prim}


class SetVariantNode(UsdNodeBase):
    """Sets a variant selection on a prim."""
    NODE_NAME = "Set Variant"
    CATEGORY = "USD"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("set_name", "Variant Set", text="")
        self.add_text_input("selection", "Selection", text="")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        set_name = self.get_property("set_name")
        selection = self.get_property("selection")
        if prim and set_name and selection:
            vsets = prim.GetVariantSets()
            if vsets.HasVariantSet(set_name):
                vsets.GetVariantSet(set_name).SetVariantSelection(selection)
        return {"prim": prim}


class GetPrimInfoNode(UsdNodeBase):
    """Outputs prim type, name, path, and children count."""
    NODE_NAME = "Get Prim Info"
    CATEGORY = "USD"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_output("type", color=PORT_COLORS["string"])
        self.add_output("name", color=PORT_COLORS["string"])
        self.add_output("path", color=PORT_COLORS["string"])
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        if not prim:
            return {"type": "", "name": "", "path": "", "prim": None}
        return {
            "type": prim.GetTypeName(),
            "name": prim.GetName(),
            "path": str(prim.GetPath()),
            "prim": prim,
        }


class GetChildrenNode(UsdNodeBase):
    """Outputs children of a prim."""
    NODE_NAME = "Get Children"
    CATEGORY = "USD"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_output("children", color=PORT_COLORS["list"])

    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        if not prim:
            return {"children": []}
        return {"children": list(prim.GetChildren())}


class GetAttributeNode(UsdNodeBase):
    """Get any attribute value from a prim."""
    NODE_NAME = "Get Attribute"
    CATEGORY = "USD"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("attr_name", "Attribute", text="")
        self.add_output("value", color=PORT_COLORS["any"])
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        prims = self._get_prims("prim")
        attr_name = self.get_property("attr_name")
        if not prims or not attr_name:
            return {"value": None, "prim": None}

        def _get_val(p):
            # Handle pseudo-attributes
            if attr_name == "name":
                return p.GetName()
            if attr_name == "path":
                return str(p.GetPath())
            if attr_name == "type":
                return p.GetTypeName()
            attr = p.GetAttribute(attr_name)
            return attr.Get() if attr and attr.IsValid() else None

        if len(prims) == 1:
            return {"value": _get_val(prims[0]), "prim": prims[0]}
        vals = [_get_val(p) for p in prims]
        return {"value": vals, "prim": prims}

    def to_python(self, var_map: dict) -> str:
        attr = self.get_property("attr_name") or "name"
        pv = var_map.get("prim", "prim")
        vv = var_map.get("value", "value")
        if attr == "name":
            return f'{vv} = {pv}.GetName()'
        if attr == "path":
            return f'{vv} = str({pv}.GetPath())'
        if attr == "type":
            return f'{vv} = {pv}.GetTypeName()'
        return f'{vv} = {pv}.GetAttribute("{attr}").Get()'


class SetAttributeNode(UsdNodeBase):
    """Set any attribute value on a prim."""
    NODE_NAME = "Set Attribute"
    CATEGORY = "USD"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_input("value", color=PORT_COLORS["any"])
        self.add_text_input("attr_name", "Attribute", text="")
        self.add_text_input("manual_value", "Value (if no input)", text="")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        prims = self._get_prims("prim")
        val = self._get_input_value("value")
        attr_name = self.get_property("attr_name")
        if val is None:
            manual = self.get_property("manual_value")
            if manual:
                try:
                    val = eval(manual)
                except Exception:
                    val = manual
        if prims and attr_name and val is not None:
            for prim in prims:
                attr = prim.GetAttribute(attr_name)
                if attr and attr.IsValid():
                    # Convert value to match attribute's expected type
                    type_name = attr.GetTypeName()
                    try:
                        if type_name == Sdf.ValueTypeNames.Float3Array or type_name == Sdf.ValueTypeNames.Color3fArray:
                            if isinstance(val, (tuple, list)):
                                val = Vt.Vec3fArray([Gf.Vec3f(*[float(x) for x in val])])
                        elif type_name == Sdf.ValueTypeNames.Float3 or type_name == Sdf.ValueTypeNames.Color3f:
                            if isinstance(val, (tuple, list)):
                                val = Gf.Vec3f(*[float(x) for x in val])
                        elif type_name == Sdf.ValueTypeNames.Double3:
                            if isinstance(val, (tuple, list)):
                                val = Gf.Vec3d(*[float(x) for x in val])
                    except Exception:
                        pass
                    attr.Set(val)
        return {"prim": prims if len(prims) > 1 else (prims[0] if prims else None)}


class SelectPrimNode(UsdNodeBase):
    """Select the input prim in usdview viewport."""
    NODE_NAME = "Select in Viewport"
    CATEGORY = "USD"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        if prim:
            try:
                from ..graph_host import GraphHost  # usdview-guarded
                GraphHost.current().set_selection(prim)
            except Exception:
                pass
        return {}


class FilterChildrenNode(UsdNodeBase):
    """Filter children of a prim by type name."""
    NODE_NAME = "Filter Children"
    CATEGORY = "USD"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("type_filter", "Type (e.g. Mesh)", text="Mesh")
        self.add_output("children", color=PORT_COLORS["list"])

    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        type_filter = self.get_property("type_filter") or ""
        if not prim:
            return {"children": []}
        children = [c for c in prim.GetChildren()
                    if not type_filter or c.GetTypeName() == type_filter]
        return {"children": children}


class SetVisibilityNode(UsdNodeBase):
    """Set visibility on a prim (inherited/invisible)."""
    NODE_NAME = "Set Visibility"
    CATEGORY = "USD"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("visible", "Visible (true/false)", text="true")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        if not prim:
            return {"prim": None}
        vis = self.get_property("visible") or "true"
        img = UsdGeom.Imageable(prim)
        if vis.lower() in ("false", "0", "invisible"):
            img.MakeInvisible()
        else:
            img.MakeVisible()
        return {"prim": prim}


class SetPurposeNode(UsdNodeBase):
    """Set purpose on a prim (default/render/proxy/guide)."""
    NODE_NAME = "Set Purpose"
    CATEGORY = "USD"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("purpose", "Purpose", text="default")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        if not prim:
            return {"prim": None}
        UsdGeom.Imageable(prim).GetPurposeAttr().Set(self.get_property("purpose") or "default")
        return {"prim": prim}


class IsVisibleNode(UsdNodeBase):
    """Check if a prim is visible."""
    NODE_NAME = "Is Visible"
    CATEGORY = "USD"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_output("visible", color=PORT_COLORS["bool"])
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        if not prim:
            return {"visible": False, "prim": None}
        vis = UsdGeom.Imageable(prim).ComputeVisibility() != UsdGeom.Tokens.invisible
        return {"visible": vis, "prim": prim}


class IsPrimValidNode(UsdNodeBase):
    """Check if a prim exists and is valid."""
    NODE_NAME = "Is Prim Valid"
    CATEGORY = "USD"
    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_output("valid", color=PORT_COLORS["bool"])
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        valid = prim is not None and hasattr(prim, "IsValid") and prim.IsValid()
        return {"valid": valid, "prim": prim if valid else None}


class HasVariantsNode(UsdNodeBase):
    """Check if prim has variant sets. Outputs prim on true or false branch."""
    NODE_NAME = "Has Variants"
    CATEGORY = "USD"
    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_output("true", color=PORT_COLORS["prim"])
        self.add_output("false", color=PORT_COLORS["prim"])
        self.add_output("names", color=PORT_COLORS["list"])
    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        if prim is None or not hasattr(prim, "GetVariantSets"):
            return {"true": None, "false": None, "names": []}
        names = prim.GetVariantSets().GetNames()
        if names:
            return {"true": prim, "false": None, "names": names}
        return {"true": None, "false": prim, "names": []}


class HasChildrenNode(UsdNodeBase):
    """Check if prim has children."""
    NODE_NAME = "Has Children"
    CATEGORY = "USD"
    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_output("true", color=PORT_COLORS["prim"])
        self.add_output("false", color=PORT_COLORS["prim"])
        self.add_output("count", color=PORT_COLORS["float"])
    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        if prim is None:
            return {"true": None, "false": None, "count": 0}
        children = list(prim.GetChildren())
        if children:
            return {"true": prim, "false": None, "count": len(children)}
        return {"true": None, "false": prim, "count": 0}


class HasMaterialNode(UsdNodeBase):
    """Check if prim has a material binding."""
    NODE_NAME = "Has Material"
    CATEGORY = "USD"
    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_output("true", color=PORT_COLORS["prim"])
        self.add_output("false", color=PORT_COLORS["prim"])
        self.add_output("material", color=PORT_COLORS["material"])
    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        if prim is None:
            return {"true": None, "false": None, "material": None}
        binding = UsdShade.MaterialBindingAPI(prim).GetDirectBinding()
        mat_path = binding.GetMaterialPath() if binding else None
        if mat_path and str(mat_path) != "":
            mat_prim = stage.GetPrimAtPath(mat_path)
            return {"true": prim, "false": None, "material": mat_prim if mat_prim.IsValid() else None}
        return {"true": None, "false": prim, "material": None}


class HasAttributeNode(UsdNodeBase):
    """Check if prim has a specific attribute."""
    NODE_NAME = "Has Attribute"
    CATEGORY = "USD"
    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("attr_name", "Attribute Name", text="")
        self.add_output("true", color=PORT_COLORS["prim"])
        self.add_output("false", color=PORT_COLORS["prim"])
        self.add_output("value", color=PORT_COLORS["any"])
    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        attr_name = (self.get_property("attr_name") or "").strip()
        if prim is None or not attr_name:
            return {"true": None, "false": prim, "value": None}
        attr = prim.GetAttribute(attr_name)
        if attr and attr.IsValid() and attr.HasValue():
            return {"true": prim, "false": None, "value": attr.Get()}
        return {"true": None, "false": prim, "value": None}


class HasReferenceNode(UsdNodeBase):
    """Check if prim has references or payloads."""
    NODE_NAME = "Has Reference"
    CATEGORY = "USD"
    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_output("true", color=PORT_COLORS["prim"])
        self.add_output("false", color=PORT_COLORS["prim"])
    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        if prim is None:
            return {"true": None, "false": None}
        has = bool(prim.GetPrimStack()) and any(
            s.referenceList.prependedItems or s.referenceList.appendedItems
            or s.payloadList.prependedItems or s.payloadList.appendedItems
            for s in prim.GetPrimStack())
        return {"true": prim if has else None, "false": None if has else prim}


class FindByTypeNode(UsdNodeBase):
    """Find all prims of a given type."""
    NODE_NAME = "Find by Type"
    CATEGORY = "USD"

    def __init__(self):
        super().__init__()
        self.add_text_input("type_name", "Type (e.g. Mesh)", text="Mesh")
        self.add_output("prims", color=PORT_COLORS["list"])

    def execute(self, stage) -> dict:
        t = self.get_property("type_name") or "Mesh"
        prims = [p for p in stage.Traverse() if p.GetTypeName() == t]
        return {"prims": prims}


class FindByPatternNode(UsdNodeBase):
    """Find prims whose path contains a substring."""
    NODE_NAME = "Find by Pattern"
    CATEGORY = "USD"

    def __init__(self):
        super().__init__()
        self.add_text_input("pattern", "Pattern", text="")
        self.add_output("prims", color=PORT_COLORS["list"])

    def execute(self, stage) -> dict:
        pat = (self.get_property("pattern") or "").lower()
        prims = [p for p in stage.Traverse() if pat in str(p.GetPath()).lower()]
        return {"prims": prims}


class StageInfoNode(UsdNodeBase):
    """Output stage metadata: up axis, meters per unit, default prim."""
    NODE_NAME = "Stage Info"
    CATEGORY = "USD"

    def __init__(self):
        super().__init__()
        self.add_output("up_axis", color=PORT_COLORS["string"])
        self.add_output("meters_per_unit", color=PORT_COLORS["float"])
        self.add_output("default_prim", color=PORT_COLORS["string"])

    def execute(self, stage) -> dict:
        return {
            "up_axis": UsdGeom.GetStageUpAxis(stage),
            "meters_per_unit": UsdGeom.GetStageMetersPerUnit(stage),
            "default_prim": stage.GetDefaultPrim().GetName() if stage.HasDefaultPrim() else "",
        }


class GetRelationshipsNode(UsdNodeBase):
    """List relationships and their targets on a prim."""
    NODE_NAME = "Get Relationships"
    CATEGORY = "USD"
    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("rel_name", "Relationship (empty=all)", text="")
        self.add_output("targets", color=PORT_COLORS["list"])
        self.add_output("names", color=PORT_COLORS["list"])
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        if not prim:
            return {"targets": [], "names": [], "prim": None}
        rel_name = (self.get_property("rel_name") or "").strip()
        if rel_name:
            rel = prim.GetRelationship(rel_name)
            if rel:
                # Resolve to actual prims where possible
                targets = []
                for t in rel.GetTargets():
                    p = stage.GetPrimAtPath(t)
                    targets.append(p if p.IsValid() else str(t))
                return {"targets": targets, "names": [rel_name], "prim": prim}
            return {"targets": [], "names": [], "prim": prim}
        # All relationships
        names = []
        targets = []
        for rel in prim.GetRelationships():
            names.append(rel.GetName())
            for t in rel.GetTargets():
                p = stage.GetPrimAtPath(t)
                targets.append(p if p.IsValid() else str(t))
        return {"targets": targets, "names": names, "prim": prim}


class GetMetadataNode(UsdNodeBase):
    """Get metadata from a prim. Leave Key empty to get ALL metadata as a dict."""
    NODE_NAME = "Get Metadata"
    CATEGORY = "USD"
    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("key", "Key (empty=all)", text="")
        self.add_output("value", color=PORT_COLORS["any"])
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        if not prim:
            return {"value": None, "prim": None}
        key = (self.get_property("key") or "").strip()
        if not key:
            # Return all metadata as dict — same as usdview Metadata tab
            md = dict(prim.GetAllMetadata())
            md["customData"] = dict(prim.GetCustomData())
            md["path"] = str(prim.GetPath())
            md["typeName"] = prim.GetTypeName()
            md["specifier"] = str(prim.GetSpecifier())
            md["active"] = prim.IsActive()
            return {"value": md, "prim": prim}
        if key == "customData":
            return {"value": dict(prim.GetCustomData()), "prim": prim}
        return {"value": prim.GetMetadata(key), "prim": prim}


class SetMetadataNode(UsdNodeBase):
    """Set metadata on a prim."""
    NODE_NAME = "Set Metadata"
    CATEGORY = "USD"
    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_input("value", color=PORT_COLORS["any"])
        self.add_text_input("key", "Key", text="")
        self.add_text_input("manual_value", "Value (if no input)", text="")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        key = (self.get_property("key") or "").strip()
        val = self._get_input_value("value")
        if val is None:
            manual = self.get_property("manual_value") or ""
            if manual:
                try:
                    val = eval(manual)
                except Exception:
                    val = manual
        if prim and key and val is not None:
            if key == "customData":
                prim.SetCustomData(val if isinstance(val, dict) else {"value": val})
            else:
                prim.SetMetadata(key, val)
        return {"prim": prim}


class GetPrimLayerStackNode(UsdNodeBase):
    """Get which layers have opinions about a specific prim."""
    NODE_NAME = "Get Prim Layers"
    CATEGORY = "USD"
    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_output("layers", color=PORT_COLORS["list"])
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        if not prim:
            return {"layers": [], "prim": None}
        stack = prim.GetPrimStack()
        layers = [(s.layer.identifier, str(s.path)) for s in stack]
        return {"layers": layers, "prim": prim}


class ListPrimsNode(UsdNodeBase):
    """Traverse stage with combined filters: type, purpose, visibility, name pattern."""
    NODE_NAME = "List Prims"
    CATEGORY = "USD"
    def __init__(self):
        super().__init__()
        self.add_input("root", color=PORT_COLORS["prim"])
        self.add_text_input("type_filter", "Type (empty=any)", text="")
        self.add_text_input("name_pattern", "Name contains", text="")
        self.add_text_input("purpose_filter", "Purpose (empty=any)", text="")
        self.add_output("prims", color=PORT_COLORS["list"])
        self.add_output("count", color=PORT_COLORS["float"])
    def execute(self, stage) -> dict:
        root = self._get_input_value("root")
        type_f = (self.get_property("type_filter") or "").strip()
        name_f = (self.get_property("name_pattern") or "").strip().lower()
        purp_f = (self.get_property("purpose_filter") or "").strip()
        it = root.GetAllChildren() if root else stage.Traverse()
        if root:
            # Recursive traversal from root
            it = Usd.PrimRange(root)
        result = []
        for p in it:
            if type_f and p.GetTypeName() != type_f:
                continue
            if name_f and name_f not in p.GetName().lower():
                continue
            if purp_f:
                img = UsdGeom.Imageable(p)
                if img and img.GetPurposeAttr().Get() != purp_f:
                    continue
            result.append(p)
        return {"prims": result, "count": len(result)}


class PrintNode(UsdNodeBase):
    """Debug node — prints whatever comes in."""
    NODE_NAME = "Print"
    CATEGORY = "Debug"

    def __init__(self):
        super().__init__()
        self.add_input("value", color=PORT_COLORS["any"])

    def execute(self, stage) -> dict:
        val = self._get_input_value("value")
        print(f"[PrintNode] {val}")
        return {}
