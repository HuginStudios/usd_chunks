"""Set Metadata chunk -- authors metadata on a prim using structured UI widgets.

Supports standard metadata (kind, hidden, active, documentation, purpose)
and custom metadata (customData). If the metadata key doesn't exist as
standard metadata, it creates a custom entry under customData.

All edits authored on the chunk's own layer.
"""

from __future__ import annotations

from pxr import Usd, UsdGeom

from usd_chunks import chunk, chunk_utils


# Default metadata keys shown when no prim is selected
_METADATA_KEYS = [
    "kind",
    "active",
    "hidden",
    "documentation",
    "purpose",
    "customData",
]

# Valid Kind tokens
_KIND_VALUES = ["", "model", "group", "assembly", "component", "subcomponent"]

# Valid Purpose tokens
_PURPOSE_VALUES = ["default", "render", "proxy", "guide"]


def _set_metadata_value(stage: Usd.Stage, prim: Usd.Prim, key: str,
                        value_str: str, custom_key: str = "") -> bool:
    """Set a metadata value on a prim. Returns True on success."""
    if not prim.IsValid():
        return False

    try:
        if key == "kind":
            model = Usd.ModelAPI(prim)
            model.SetKind(value_str)
            return True

        elif key == "active":
            active = value_str.lower() in ("true", "1", "yes")
            prim.SetActive(active)
            return True

        elif key == "hidden":
            hidden = value_str.lower() in ("true", "1", "yes")
            prim.SetMetadata("hidden", hidden)
            return True

        elif key == "documentation":
            prim.SetMetadata("documentation", value_str)
            return True

        elif key == "purpose":
            imageable = UsdGeom.Imageable(prim)
            if imageable:
                imageable.GetPurposeAttr().Set(value_str)
                return True
            return False

        elif key == "customData":
            if not custom_key:
                return False
            # Get existing customData or start fresh
            existing = prim.GetMetadata("customData") or {}
            existing = dict(existing)

            # Try to parse value as Python literal for non-string types
            parsed_value = _parse_custom_value(value_str)
            existing[custom_key] = parsed_value
            prim.SetMetadata("customData", existing)
            return True

        else:
            # Attempt generic metadata set
            prim.SetMetadata(key, value_str)
            return True

    except Exception:
        return False


def _parse_custom_value(value_str: str):
    """Try to parse a value string into an appropriate Python type for customData.

    Tries: bool, int, float, then falls back to string.
    """
    v = value_str.strip()

    # Bool
    if v.lower() in ("true", "false"):
        return v.lower() == "true"

    # Int
    try:
        return int(v)
    except ValueError:
        pass

    # Float
    try:
        return float(v)
    except ValueError:
        pass

    # String fallback
    return v


def _get_prim_metadata_keys(prim: Usd.Prim) -> list[str]:
    """Get settable metadata keys from a prim, combining authored + standard."""
    if not prim or not prim.IsValid():
        return list(_METADATA_KEYS)

    authored = prim.GetAllAuthoredMetadata()
    keys = list(authored.keys()) if authored else []

    # Always include common settable keys
    for essential in ("kind", "active", "hidden", "documentation", "purpose", "customData"):
        if essential not in keys:
            keys.append(essential)

    keys.sort()
    return keys


def _update_menu_items(chunk_obj, prim: Usd.Prim):
    """Update the metadata dropdown with keys from the actual prim."""
    menu_param = chunk_obj.get_param("metadata_key")
    if not menu_param:
        return

    keys = _get_prim_metadata_keys(prim)
    current = menu_param.value
    menu_param.menu_items = keys
    if current and current in keys:
        menu_param.value = current
    elif keys:
        menu_param.value = keys[0]


def execute(node):
    stage = node.get_stage()

    primpath_param = node.get_param("prim_path")
    meta_key = node.get_param("metadata_key").value or "kind"
    value_str = node.get_param("metadata_value").value or ""
    custom_key = node.get_param("custom_key").value or ""

    if not primpath_param.value or not value_str:
        return

    for target_path in chunk_utils.author_in_variant_contexts(stage, primpath_param):
        prim = stage.GetPrimAtPath(target_path)
        if not prim.IsValid():
            prim = stage.OverridePrim(target_path)
        _set_metadata_value(stage, prim, meta_key, value_str, custom_key)


def _on_prim_change(owner, notice, sender):
    """When prim selection changes, update the menu with that prim's metadata keys."""
    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage or not _chunk:
        return

    prim_param = _chunk.get_param("prim_path")
    prims = prim_param.get_prims()
    if prims:
        _update_menu_items(_chunk, prims[0])


def _on_change(owner, notice, sender):
    """Callback for the chunk panel -- author the metadata value."""
    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage or not _chunk:
        return

    prim_param = _chunk.get_param("prim_path")
    meta_key = _chunk.get_param("metadata_key").value or "kind"
    value_str = _chunk.get_param("metadata_value").value or ""
    custom_key = _chunk.get_param("custom_key").value or ""

    if not value_str:
        return

    prims = prim_param.get_prims()
    if not prims:
        return

    layer = owner.get_edit_context()
    if not layer:
        return
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)

    for prim in prims:
        _set_metadata_value(stage, prim, meta_key, value_str, custom_key)


def _on_key_change(owner, notice, sender):
    """Show/hide the custom key field and adjust value hints based on metadata key."""
    _chunk = owner.get_chunk()
    if not _chunk:
        return

    meta_key = _chunk.get_param("metadata_key").value or "kind"
    custom_key_param = _chunk.get_param("custom_key")
    value_param = _chunk.get_param("metadata_value")

    # Show custom key only for customData
    if meta_key == "customData":
        custom_key_param.unset_state(chunk.ChunkFlags.State.HIDDEN)
    else:
        custom_key_param.set_state(chunk.ChunkFlags.State.HIDDEN)

    # Update tooltip hints based on key
    tooltips = {
        "kind": "Kind value: model, group, assembly, component, subcomponent",
        "active": "Boolean: true or false",
        "hidden": "Boolean: true or false",
        "documentation": "Free-form documentation string",
        "purpose": "Purpose: default, render, proxy, guide",
        "customData": "Value for the custom key (auto-detects type: bool, int, float, string)",
    }
    value_param.tooltip = tooltips.get(meta_key, "Value to set")


def main():
    chunk_api = chunk.Chunk("Set Metadata")

    prim_path = chunk.LineEdit(
        "prim_path",
        "Prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="Select the prim(s) to set metadata on. The dropdown updates with its keys.",
    )
    prim_path.add_callback(_on_prim_change)
    chunk_api.add_param(prim_path)

    metadata_key = chunk.Menu(
        "metadata_key",
        "Metadata",
        menu_items=_METADATA_KEYS,
        default_value="kind",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="Which metadata field to set.",
    )
    metadata_key.add_callback(_on_key_change)
    chunk_api.add_param(metadata_key)

    custom_key = chunk.LineEdit(
        "custom_key",
        "Custom Key",
        default_value="",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="The customData key name to set (only when 'customData' is selected).",
    )
    custom_key.set_state(chunk.ChunkFlags.State.HIDDEN)
    custom_key.add_callback(_on_change)
    chunk_api.add_param(custom_key)

    metadata_value = chunk.LineEdit(
        "metadata_value",
        "Value",
        default_value="",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="The value to set for the selected metadata field.",
    )
    metadata_value.add_callback(_on_change)
    chunk_api.add_param(metadata_value)

    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD,
        icon="tag",
        description="""
        Set Metadata — authors metadata on a prim using structured UI widgets.

        Parameters:
          - Prim: select prim(s) to author metadata on (supports globs, multi).
          - Metadata: dropdown to select which metadata to set (kind, active,
            hidden, documentation, purpose, customData).
          - Custom Key: when 'customData' is selected, the key name for the
            custom metadata entry.
          - Value: the value to author. Type is auto-detected:
            • kind: model, group, assembly, component, subcomponent
            • active/hidden: true or false
            • documentation: free-form string
            • purpose: default, render, proxy, guide
            • customData: auto-detects bool, int, float, or string

        If a non-standard key is needed, select 'customData' and provide
        the key name — it will be created automatically.

        All edits go to the chunk's own layer (non-destructive).
        """,
    )
