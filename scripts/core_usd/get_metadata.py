"""Get Metadata chunk — reads metadata from a prim using structured UI widgets.

Supports standard metadata (kind, hidden, active, documentation, purpose,
assetInfo) and custom metadata (customData keys). The user selects a prim
and picks which metadata field to read from a dropdown.
"""

from __future__ import annotations

from pxr import Usd, UsdGeom

from usd_chunks import chunk


# Standard metadata keys that can be read from any prim
_METADATA_KEYS = [
    "kind",
    "active",
    "hidden",
    "documentation",
    "purpose",
    "assetInfo",
    "customData",
]


def _get_metadata_value(prim: Usd.Prim, key: str, custom_key: str = "") -> str:
    """Read a metadata value from a prim and return its string representation."""
    if not prim.IsValid():
        return "<invalid prim>"

    try:
        if key == "kind":
            model = Usd.ModelAPI(prim)
            kind = model.GetKind()
            return kind if kind else "<not set>"

        elif key == "active":
            return str(prim.IsActive())

        elif key == "hidden":
            return str(prim.IsHidden())

        elif key == "documentation":
            doc = prim.GetMetadata("documentation")
            return doc if doc else "<not set>"

        elif key == "purpose":
            imageable = UsdGeom.Imageable(prim)
            if imageable:
                purpose = imageable.GetPurposeAttr().Get()
                return str(purpose) if purpose else "default"
            return "<not imageable>"

        elif key == "assetInfo":
            info = prim.GetMetadata("assetInfo")
            if info:
                return str(dict(info))
            return "<not set>"

        elif key == "customData":
            data = prim.GetMetadata("customData")
            if data:
                if custom_key:
                    val = data.get(custom_key)
                    return str(val) if val is not None else f"<key '{custom_key}' not found>"
                return str(dict(data))
            return "<not set>"

        else:
            # Generic metadata access
            val = prim.GetMetadata(key)
            return str(val) if val is not None else "<not set>"

    except Exception as e:
        return f"<error: {e}>"


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


def _on_change(owner, notice, sender):
    """Callback for the chunk panel — resolve and display the metadata value."""
    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage or not _chunk:
        return

    prim_param = _chunk.get_param("prim_path")
    meta_key = _chunk.get_param("metadata_key").value or "kind"
    custom_key = _chunk.get_param("custom_key").value or ""
    output_param = _chunk.get_param("output_value")

    prims = prim_param.get_prims()
    if not prims:
        output_param.value = ""
        return

    # Show value from first selected prim
    output_param.value = _get_metadata_value(prims[0], meta_key, custom_key)


def _on_key_change(owner, notice, sender):
    """Show/hide the custom key field based on metadata key selection."""
    _chunk = owner.get_chunk()
    if not _chunk:
        return

    meta_key = _chunk.get_param("metadata_key").value or "kind"
    custom_key_param = _chunk.get_param("custom_key")

    if meta_key == "customData":
        custom_key_param.unset_state(chunk.ChunkFlags.State.HIDDEN)
    else:
        custom_key_param.set_state(chunk.ChunkFlags.State.HIDDEN)

    # Also trigger value refresh
    _on_change(owner, notice, sender)


def main():
    chunk_api = chunk.Chunk("Get Metadata")

    prim_path = chunk.LineEdit(
        "prim_path",
        "Prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="Select the prim to read metadata from.",
    )
    prim_path.add_callback(_on_change)
    chunk_api.add_param(prim_path)

    metadata_key = chunk.Menu(
        "metadata_key",
        "Metadata",
        menu_items=_METADATA_KEYS,
        default_value="kind",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="Which metadata field to read.",
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
    custom_key.add_callback(_on_change)
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
        category="Core USD",
        description="""
        Get Metadata — reads metadata from a prim using structured UI widgets.

        Parameters:
          - Prim: select the prim to inspect (supports viewport picking).
          - Metadata: dropdown to select which metadata to read (kind, active,
            hidden, documentation, purpose, assetInfo, customData).
          - Custom Key: when 'customData' is selected, specifies the key name
            to read. If empty, shows all customData.
          - Value: read-only output displaying the resolved metadata value.

        In the node graph, the output_value port carries the metadata value
        for wiring into downstream nodes.
        """,
    )
