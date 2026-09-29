"""Set Attributes chunk -- author named attributes on prims, and apply API schemas.

The general-purpose edit for anything no dedicated chunk covers: a renderer's own settings
on a light, a custom tag another tool reads, a flag on a collider. Each row is one attribute
-- name, type, value -- written on every target prim in this chunk's own layer; nothing is
hidden in a script. API schemas listed in Apply Schemas are prepended to the targets'
``apiSchemas`` as they are, known to this USD build or not -- a renderer's schema is
typically only registered where that renderer is.

Values are typed text: ``1`` / ``true`` for bool, ``0.5`` for float, ``1 0 0`` or
``(1, 0, 0)`` for vectors and colours, and the text itself for string, token and asset.
"""

from __future__ import annotations

import re

from pxr import Gf, Sdf, Usd

from usd_chunks import chunk

_TYPES = {
    "bool": Sdf.ValueTypeNames.Bool,
    "int": Sdf.ValueTypeNames.Int,
    "float": Sdf.ValueTypeNames.Float,
    "double": Sdf.ValueTypeNames.Double,
    "float3": Sdf.ValueTypeNames.Float3,
    "color3f": Sdf.ValueTypeNames.Color3f,
    "string": Sdf.ValueTypeNames.String,
    "token": Sdf.ValueTypeNames.Token,
    "asset": Sdf.ValueTypeNames.Asset,
}


def parse(type_name: str, text: str):
    """The typed value for *text*, or raise ValueError saying what was expected."""
    text = (text or "").strip()
    if type_name == "bool":
        if text.lower() in ("1", "true", "yes", "on"):
            return True
        if text.lower() in ("0", "false", "no", "off", ""):
            return False
        raise ValueError(f"expected true/false, got {text!r}")
    if type_name == "int":
        return int(float(text))
    if type_name in ("float", "double"):
        return float(text)
    if type_name in ("float3", "color3f"):
        parts = [float(p) for p in re.split(r"[\s,()]+", text) if p]
        if len(parts) != 3:
            raise ValueError(f"expected three numbers, got {text!r}")
        return Gf.Vec3f(*parts)
    if type_name == "asset":
        return Sdf.AssetPath(text)
    return text


def _report(node, text: str) -> None:
    if (readout := node.get_param("readout")) is not None and readout.value != text:
        readout.value = text


def execute(node) -> None:
    stage = node.get_stage()
    layer = node.get_edit_context()
    if stage is None or layer is None:
        return
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)
    targets = [p for p in node.get_param("prim_path").get_prims()]
    if not targets:
        _report(node, "no target prims")
        return
    schemas = [s.strip() for s in (node.get_param("schemas").value or "").split(",") if s.strip()]
    rows = node.get_param("attributes", chunk.is_inst(chunk.ListGroup))
    name_t, type_t, value_t = rows.get_param("attr_name"), rows.get_param("attr_type"), rows.get_param("attr_value")
    written, problems = 0, []
    with Usd.EditContext(stage, layer):
        for prim in targets:
            over = stage.OverridePrim(prim.GetPath())
            if schemas:
                # Prepended, never explicit: an explicit list in this stronger layer would
                # replace every schema the prim already has (a light's LightAPI among them).
                over.SetMetadata("apiSchemas", Sdf.TokenListOp.Create(prependedItems=schemas))
            for i in range(rows.size):
                name = (rows.get_indexed_param(name_t, i).value or "").strip()
                if not name:
                    continue
                type_name = rows.get_indexed_param(type_t, i).value or "string"
                try:
                    value = parse(type_name, rows.get_indexed_param(value_t, i).value)
                except ValueError as e:
                    problems.append(f"{name}: {e}")
                    continue
                over.CreateAttribute(name, _TYPES[type_name]).Set(value)
                written += 1
    text = f"{written} value{'s' if written != 1 else ''} on {len(targets)} prim{'s' if len(targets) != 1 else ''}"
    if schemas:
        text += f" · schemas {', '.join(schemas)}"
    if problems:
        text += "\n" + "\n".join(problems)
    _report(node, text)


def main() -> chunk.Chunk:
    chunk_api = chunk.Chunk("Set Attributes")
    flags = chunk.ChunkFlags.ParamFlags
    chunk_api.add_param(chunk.LineEdit(
        "prim_path", "Targets", flags=flags.LABELED | flags.PRIM_SELECTION,
        tooltip="The prims to write on; several comma separated, globs work.",
    ))
    chunk_api.add_param(chunk.LineEdit(
        "schemas", "Apply Schemas", flags=flags.LABELED,
        tooltip="API schemas to apply to the targets, comma separated -- e.g. VRayLightAPI.",
    ))
    rows = chunk.ListGroup("attributes", "Attributes", tooltip="One attribute per row.")
    rows.add_param(chunk.LineEdit("attr_name", "Name", flags=flags.LABELED,
                                  tooltip="Full attribute name, namespaces included."))
    rows.add_param(chunk.Menu("attr_type", "Type", menu_items=list(_TYPES), default_value="float"))
    rows.add_param(chunk.LineEdit("attr_value", "Value", flags=flags.LABELED,
                                  tooltip="1/true for bool; 1 0 0 for vectors and colours."))
    chunk_api.add_param(rows)
    rows.size = 1
    chunk_api.add_param(chunk.Label("readout", "nothing set yet", default_value="nothing set yet",
                                    tooltip="What was written."))
    return chunk_api


def config() -> chunk.DefaultConfig:
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.PROPERTIES,
        description=(
            "Writes named, typed attributes on prims and applies API schemas -- the general edit\n"
            "for a renderer's settings, a custom tag or a flag no other chunk covers."
        ),
    )
