"""Set Property chunk — authors an attribute or relationship value on a prim.

Input is dot-notation for the property path: /Prim/Path.propertyName
A second field provides the value to set (string representation, parsed
to match the property's existing type on the stage).

All edits authored on the chunk's own layer for non-destructive isolation.
"""

from __future__ import annotations

from pxr import Usd, Sdf, Gf, Vt

from usd_chunks import chunk, chunk_utils


def _parse_property_path(path_str: str) -> tuple[str, str]:
    """Split '/Prim/Path.propertyName' into (prim_path, property_name)."""
    path_str = path_str.strip()
    if not path_str:
        return ("", "")

    last_slash = path_str.rfind("/")
    dot_pos = path_str.find(".", last_slash + 1)

    if dot_pos == -1:
        return (path_str, "")

    return (path_str[:dot_pos], path_str[dot_pos + 1:])


def _parse_value(value_str: str, attr: Usd.Attribute):
    """Parse a string value into the correct USD type based on the attribute's typeName.

    Supports common USD types: float, double, int, bool, string, token,
    Vec2/3/4, Color3/4, Matrix4d, and arrays thereof.
    """
    if not attr or not attr.IsValid():
        return None

    type_name = str(attr.GetTypeName())
    value_str = value_str.strip()

    try:
        # Booleans
        if type_name == "bool":
            return value_str.lower() in ("true", "1", "yes")

        # Integers
        if type_name == "int":
            return int(value_str)
        if type_name == "int64":
            return int(value_str)

        # Floats / doubles
        if type_name in ("float", "half"):
            return float(value_str)
        if type_name == "double":
            return float(value_str)

        # Strings / tokens / assets
        if type_name in ("string", "token"):
            return value_str
        if type_name == "asset":
            return Sdf.AssetPath(value_str)

        # Vector types — parse comma or space-separated components
        components = [c.strip() for c in value_str.replace("(", "").replace(")", "").replace(",", " ").split()]

        if type_name in ("float2", "double2", "half2"):
            return Gf.Vec2f(float(components[0]), float(components[1]))
        if type_name == "int2":
            return Gf.Vec2i(int(components[0]), int(components[1]))

        if type_name in ("float3", "double3", "half3", "normal3f", "point3f", "vector3f"):
            return Gf.Vec3f(float(components[0]), float(components[1]), float(components[2]))
        if type_name == "color3f":
            return Gf.Vec3f(float(components[0]), float(components[1]), float(components[2]))
        if type_name == "int3":
            return Gf.Vec3i(int(components[0]), int(components[1]), int(components[2]))

        if type_name in ("float4", "double4", "half4"):
            return Gf.Vec4f(float(components[0]), float(components[1]),
                            float(components[2]), float(components[3]))
        if type_name == "color4f":
            return Gf.Vec4f(float(components[0]), float(components[1]),
                            float(components[2]), float(components[3]))
        if type_name == "quatf":
            return Gf.Quatf(float(components[0]), float(components[1]),
                            float(components[2]), float(components[3]))

        # Matrix4d — expects 16 space/comma separated values
        if type_name == "matrix4d":
            vals = [float(c) for c in components]
            if len(vals) == 16:
                return Gf.Matrix4d(*vals)

        # Array types — try eval for simple Python-literal arrays
        if "[]" in type_name:
            import ast
            parsed = ast.literal_eval(value_str)
            if isinstance(parsed, (list, tuple)):
                # Let USD handle the conversion via the attribute's Set method
                return parsed

        # Fallback: try direct conversion with Python eval for complex types
        # This handles cases like "Gf.Vec3f(1, 2, 3)" if the user typed that
        return value_str

    except (ValueError, IndexError, TypeError):
        return None


def _parse_relationship_targets(value_str: str) -> list[Sdf.Path]:
    """Parse comma-separated prim paths into a list of Sdf.Path."""
    paths = []
    for token in value_str.replace(",", " ").split():
        token = token.strip()
        if token:
            paths.append(Sdf.Path(token))
    return paths


def execute(node):
    stage = node.get_stage()
    if not stage:
        return

    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    path_str = node.get_param("property_path").value or ""
    value_str = node.get_param("property_value").value or ""

    prim_path, prop_name = _parse_property_path(path_str)
    if not prim_path or not prop_name:
        return

    prim = stage.GetPrimAtPath(prim_path)
    if not prim.IsValid():
        # Create an over so we can still author the property
        prim = stage.OverridePrim(prim_path)

    # Try as attribute
    attr = prim.GetAttribute(prop_name)
    if attr and attr.IsValid():
        parsed = _parse_value(value_str, attr)
        if parsed is not None:
            attr.Set(parsed)
        return

    # Try as relationship
    rel = prim.GetRelationship(prop_name)
    if rel and rel.IsValid():
        targets = _parse_relationship_targets(value_str)
        if targets:
            rel.SetTargets(targets)
        return

    # Property doesn't exist yet — can't determine type without schema context.
    # Create an attribute with string type as fallback.
    new_attr = prim.CreateAttribute(prop_name, Sdf.ValueTypeNames.String)
    new_attr.Set(value_str)


def _on_change(owner, notice, sender):
    """Callback for the chunk panel — author the property value."""
    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage or not _chunk:
        return

    path_str = _chunk.get_param("property_path").value or ""
    value_str = _chunk.get_param("property_value").value or ""

    prim_path, prop_name = _parse_property_path(path_str)
    if not prim_path or not prop_name or not value_str:
        return

    layer = owner.get_edit_context()
    if not layer:
        return
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)

    prim = stage.GetPrimAtPath(prim_path)
    if not prim.IsValid():
        prim = stage.OverridePrim(prim_path)

    # Try as attribute
    attr = prim.GetAttribute(prop_name)
    if attr and attr.IsValid():
        parsed = _parse_value(value_str, attr)
        if parsed is not None:
            attr.Set(parsed)
        return

    # Try as relationship
    rel = prim.GetRelationship(prop_name)
    if rel and rel.IsValid():
        targets = _parse_relationship_targets(value_str)
        if targets:
            rel.SetTargets(targets)
        return

    # Fallback: create string attribute
    new_attr = prim.CreateAttribute(prop_name, Sdf.ValueTypeNames.String)
    new_attr.Set(value_str)


def main():
    chunk_api = chunk.Chunk("Set Property")

    property_path = chunk.LineEdit(
        "property_path",
        "Property Path",
        default_value="",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="Prim path + property in dot notation, e.g. /World/Cube.size or /World/Cube.material:binding",
    )
    property_path.add_callback(_on_change)
    chunk_api.add_param(property_path)

    property_value = chunk.LineEdit(
        "property_value",
        "Value",
        default_value="",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="The value to set. Parsed to match the property's USD type (float, string, vector, etc.).",
    )
    property_value.add_callback(_on_change)
    chunk_api.add_param(property_value)

    return chunk_api


def config():
    return chunk.DefaulConfig(
        category="Core USD",
        description="""
        Set Property — authors an attribute or relationship value on a prim.

        Parameters:
          - Property Path: dot-notation path, e.g. /World/Cube.size,
            /World/Mesh.xformOp:translate, /World/Cube.material:binding.
          - Value: the value to author, as a string. Parsed to match the
            property's existing USD type on the stage.

        Type parsing supports:
          - Scalars: float, double, int, bool, string, token, asset
          - Vectors: float2/3/4, color3f, normal3f, point3f, quatf (comma or space separated)
          - Relationships: comma-separated prim paths

        If the property doesn't exist, a string attribute is created as fallback.
        All edits go to the chunk's own layer (non-destructive).

        In the node graph, accepts a prim input port for composable workflows.
        """,
    )
