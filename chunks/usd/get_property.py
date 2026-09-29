"""Get Property chunk -- reads an attribute or relationship value from a prim.

Input is dot-notation: /Prim/Path.propertyName
Supports attributes (returns resolved value at current time) and relationships
(returns target path list).

In the node graph this becomes a data source -- the output port carries the
property value that can be wired into downstream nodes.
"""

from __future__ import annotations

from pxr import Usd

from usd_chunks import chunk


def _parse_property_path(path_str: str) -> tuple[str, str]:
    """Split '/Prim/Path.propertyName' into (prim_path, property_name).

    Handles namespaced properties like 'xformOp:translate' and
    relationship-style names like 'material:binding'.
    The split is on the LAST dot that is preceded by a path component
    (i.e. after the last '/'-separated segment).
    """
    path_str = path_str.strip()
    if not path_str:
        return ("", "")

    # Find the last '.' that separates the prim path from the property name.
    # USD prim paths never contain dots, so the first dot after the last '/' is our separator.
    last_slash = path_str.rfind("/")
    dot_pos = path_str.find(".", last_slash + 1)

    if dot_pos == -1:
        # No property name -- treat entire string as prim path
        return (path_str, "")

    return (path_str[:dot_pos], path_str[dot_pos + 1:])


def _resolve_value(stage: Usd.Stage, prim_path: str, prop_name: str) -> str:
    """Resolve the property value and return a string representation."""
    prim = stage.GetPrimAtPath(prim_path)
    if not prim.IsValid():
        return f"<invalid prim: {prim_path}>"

    # Try as attribute first
    attr = prim.GetAttribute(prop_name)
    if attr and attr.IsValid():
        time_code = Usd.TimeCode.Default()
        val = attr.Get(time_code)
        if val is None:
            return "<no value>"
        return str(val)

    # Try as relationship
    rel = prim.GetRelationship(prop_name)
    if rel and rel.IsValid():
        targets = rel.GetTargets()
        if not targets:
            return "<no targets>"
        return ", ".join(str(t) for t in targets)

    return f"<property not found: {prop_name}>"


def execute(node):
    stage = node.get_stage()

    path_str = node.get_param("property_path").value or ""
    prim_path, prop_name = _parse_property_path(path_str)

    if not prim_path or not prop_name:
        node.get_param("output_value").value = ""
        return

    result = _resolve_value(stage, prim_path, prop_name)
    node.get_param("output_value").value = result


def _on_change(owner, notice, sender):
    """Callback for the chunk panel -- resolve and display the value."""
    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage or not _chunk:
        return

    path_str = _chunk.get_param("property_path").value or ""
    prim_path, prop_name = _parse_property_path(path_str)

    output_param = _chunk.get_param("output_value")
    if not prim_path or not prop_name:
        output_param.value = ""
        return

    output_param.value = _resolve_value(stage, prim_path, prop_name)


def main():
    chunk_api = chunk.Chunk("Get Property")

    property_path = chunk.LineEdit(
        "property_path",
        "Property Path",
        default_value="",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="Prim path + property in dot notation, e.g. /World/Cube.size or /World/Cube.material:binding",
    )
    property_path.add_callback(_on_change)
    chunk_api.add_param(property_path)

    output_value = chunk.LineEdit(
        "output_value",
        "Value",
        default_value="",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.OUTPUT,
        tooltip="The resolved property value (read-only output).",
    )
    chunk_api.add_param(output_value)

    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD,
        description="""
        Get Property — reads the current value of an attribute or relationship
        from a prim on the stage.

        Parameters:
          - Property Path: dot-notation path, e.g. /World/Cube.size,
            /World/Mesh.xformOp:translate, /World/Cube.material:binding.
          - Value: read-only output displaying the resolved value.

        Attributes return their value at the default time code.
        Relationships return their target path list as a comma-separated string.

        In the node graph, the output_value port carries the resolved value
        for wiring into downstream nodes.
        """,
    )
