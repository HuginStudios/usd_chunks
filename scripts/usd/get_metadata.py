"""Get Metadata chunk — reads metadata from a prim using structured UI widgets.

Dynamically populates the metadata dropdown from the prim's actual authored
metadata keys. Supports all standard and custom metadata — whatever is on
the prim shows up in the menu.
"""

from __future__ import annotations

from pxr import Usd, UsdGeom

from usd_chunks import chunk


# Fallback keys shown when no prim is selected yet
_DEFAULT_KEYS = [
    "kind",
    "active",
    "hidden",
    "documentation",
    "typeName",
    "specifier",
    "customData",
]


def _get_prim_metadata_keys(prim: Usd.Prim) -> list[str]:
    """Get all metadata keys authored on a prim, plus essential composed ones."""
    if not prim or not prim.IsValid():
        return list(_DEFAULT_KEYS)

    # GetAllAuthoredMetadata returns a dict of all authored metadata
    authored = prim.GetAllAuthoredMetadata()
    keys = list(authored.keys()) if authored else []

    # Also include essential composed metadata that might not be "authored"
    # but are always queryable
    for essential in ("kind", "active", "hidden", "typeName", "specifier",
                      "documentation", "customData", "assetInfo"):
        if essential not in keys:
            keys.append(essential)

    keys.sort()
    return keys


def _get_metadata_value(prim: Usd.Prim, key: str, custom_key: str = "") -> str:
    """Read a metadata value from a prim and return its string representation."""
    if not prim.IsValid():
        return "<invalid prim>"

    try:
        if key == "active":
            return str(prim.IsActive())

        elif key == "hidden":
            return str(prim.IsHidden())

        elif key == "kind":
            model = Usd.ModelAPI(prim)
            kind = model.GetKind()
            return kind if kind else "<not set>"

        elif key == "purpose":
            imageable = UsdGeom.Imageable(prim)
            if imageable:
                purpose = imageable.GetPurposeAttr().Get()
                return str(purpose) if purpose else "default"
            return "<not imageable>"

        elif key == "customData":
            data = prim.GetMetadata("customData")
            if data:
                if custom_key:
                    val = data.get(custom_key)
                    return str(val) if val is not None else f"<key '{custom_key}' not found>"
                return str(dict(data))
            return "<not set>"

        elif key == "assetInfo":
            info = prim.GetMetadata("assetInfo")
            if info:
                return str(dict(info))
            return "<not set>"

        else:
            # Generic metadata access — works for any key
            val = prim.GetMetadata(key)
            return str(val) if val is not None else "<not set>"

    except Exception as e:
        return f"<error: {e}>"


def _update_menu_items(chunk_obj, prim: Usd.Prim):
    """Update the metadata dropdown menu with keys from the actual prim."""
    menu_param = chunk_obj.get_param("metadata_key")
    if not menu_param:
        return

    keys = _get_prim_metadata_keys(prim)

    # Preserve current selection if it still exists in the new key list
    current = menu_param.value
    menu_param.menu_items = keys
    if current and current in keys:
        menu_param.value = current
    elif keys:
        menu_param.value = keys[0]


def execute(node):
    stage = node.get_stage()
    if not stage:
        return

    # Get Metadata doesn't author anything — clear layer for cleanliness
    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    primpath_param = node.get_param("prim_path")
    meta_key = node.get_param("metadata_key").value or "kind"
    custom_key = node.get_param("custom_key").value or ""

    output_param = node.get_param("output_value")

    if not primpath_param.value:
        output_param.value = ""
        return

    # Use first resolved prim
    prims = primpath_param.get_prims() if hasattr(primpath_param, "get_prims") else []
    if not prims:
        prim = stage.GetPrimAtPath(primpath_param.value.split(",")[0].strip())
    else:
        prim = prims[0]

    if not prim or not prim.IsValid():
        output_param.value = "<invalid prim>"
        return

    output_param.value = _get_metadata_value(prim, meta_key, custom_key)


def _on_prim_change(owner, notice, sender):
    """When the prim selection changes, update the menu with that prim's metadata keys."""
    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage or not _chunk:
        return

    prim_param = _chunk.get_param("prim_path")
    prims = prim_param.get_prims()

    if prims:
        _update_menu_items(_chunk, prims[0])
        # Also refresh the output value
        _on_value_refresh(_chunk, stage)
    else:
        _chunk.get_param("output_value").value = ""


def _on_key_change(owner, notice, sender):
    """Show/hide the custom key field based on metadata key selection."""
    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not _chunk:
        return

    meta_key = _chunk.get_param("metadata_key").value or ""
    custom_key_param = _chunk.get_param("custom_key")

    if meta_key == "customData":
        custom_key_param.unset_state(chunk.ChunkFlags.State.HIDDEN)
    else:
        custom_key_param.set_state(chunk.ChunkFlags.State.HIDDEN)

    # Refresh the value
    if stage:
        _on_value_refresh(_chunk, stage)


def _on_custom_key_change(owner, notice, sender):
    """Refresh value when the custom key changes."""
    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage or not _chunk:
        return
    _on_value_refresh(_chunk, stage)


def _on_value_refresh(chunk_obj, stage):
    """Shared logic to refresh the output value."""
    prim_param = chunk_obj.get_param("prim_path")
    meta_key = chunk_obj.get_param("metadata_key").value or ""
    custom_key = chunk_obj.get_param("custom_key").value or ""
    output_param = chunk_obj.get_param("output_value")

    prims = prim_param.get_prims()
    if not prims:
        output_param.value = ""
        return

    output_param.value = _get_metadata_value(prims[0], meta_key, custom_key)


def main():
    chunk_api = chunk.Chunk("Get Metadata")

    prim_path = chunk.LineEdit(
        "prim_path",
        "Prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="Select the prim to read metadata from. The dropdown updates with its keys.",
    )
    prim_path.add_callback(_on_prim_change)
    chunk_api.add_param(prim_path)

    metadata_key = chunk.Menu(
        "metadata_key",
        "Metadata",
        menu_items=_DEFAULT_KEYS,
        default_value="kind",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="Which metadata field to read. Populated from the selected prim's actual keys.",
    )
    metadata_key.add_callback(_on_key_change)
    chunk_api.add_param(metadata_key)

    custom_key = chunk.LineEdit(
        "custom_key",
        "Custom Key",
        default_value="",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="The customData key name to read (only when 'customData' is selected).",
    )
    custom_key.set_state(chunk.ChunkFlags.State.HIDDEN)
    custom_key.add_callback(_on_custom_key_change)
    chunk_api.add_param(custom_key)

    output_value = chunk.LineEdit(
        "output_value",
        "Value",
        default_value="",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.OUTPUT,
        tooltip="The resolved metadata value (read-only output).",
    )
    chunk_api.add_param(output_value)

    return chunk_api


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.USD,
        description="""
        Get Metadata — reads metadata from a prim using structured UI widgets.

        Parameters:
          - Prim: select the prim to inspect. When a prim is selected, the
            Metadata dropdown is dynamically populated with that prim's actual
            authored metadata keys (typeName, specifier, kind, customData, etc.).
          - Metadata: dropdown showing available metadata keys on the prim.
          - Custom Key: when 'customData' is selected, specifies the key name
            to read. If empty, shows all customData.
          - Value: read-only output displaying the resolved metadata value.

        In the node graph, the output_value port carries the metadata value
        for wiring into downstream nodes.
        """,
    )
