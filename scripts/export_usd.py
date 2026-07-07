"""Export USD chunk — exports the current stage or a specific layer to disk.

Requires: pxr (OpenUSD), usd_chunks.
"""

from __future__ import annotations

import os

from pxr import Sdf

from usd_chunks import chunk


def _refresh_layers(owner, notice, sender):
    """Rebuild the layer menu from the current stage."""
    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage:
        return

    items = ["Stage (Flattened)"]
    items.append("Session Layer")
    for layer in stage.GetLayerStack(includeSessionLayers=False):
        items.append(layer.identifier)

    menu = _chunk.get_param("layer")
    menu.menu_items = items
    menu.value = items[0]


def export(owner, notice, sender):
    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage:
        return

    is_s = chunk.is_param_t(str)
    output_path = _chunk.get_param("output_file", is_s).value.strip()
    if not output_path:
        print("[ExportUSD] No output path set.")
        return

    _, ext = os.path.splitext(output_path)
    if ext.lower() not in (".usd", ".usda", ".usdc", ".usdz"):
        output_path += ".usda"

    selection = _chunk.get_param("layer", is_s).value

    if selection == "Stage (Flattened)":
        stage.Export(output_path)
        print(f"[ExportUSD] Stage exported to {output_path}")
    elif selection == "Session Layer":
        stage.GetSessionLayer().Export(output_path)
        print(f"[ExportUSD] Session layer exported to {output_path}")
    else:
        layer = Sdf.Layer.Find(selection)
        if layer:
            layer.Export(output_path)
            print(f"[ExportUSD] Layer '{selection}' exported to {output_path}")
        else:
            print(f"[ExportUSD] Could not find layer: {selection}")


def main():
    chunk_api = chunk.Chunk("Export USD")

    chunk_api.add_param(
        chunk.FileBrowser(
            "output_file",
            "Output File",
            extensions=["usd", "usda", "usdc", "usdz"],
            tooltip="Destination file. The extension picks the format: .usda (text), "
            ".usdc (binary), .usd (either), or .usdz (package).",
        )
    )

    layer_menu = chunk.Menu(
        "layer",
        "Layer",
        menu_items=["Stage (Flattened)"],
        default_value="Stage (Flattened)",
        tooltip="What to export: the whole flattened stage, the session layer, or a "
        "specific sublayer. Press Refresh Layers to populate this list from the stage.",
    )
    chunk_api.add_param(layer_menu)

    refresh_btn = chunk.Button(
        "refresh", "↻ Refresh Layers", tooltip="Rebuild the Layer menu from the current stage's layers."
    )
    refresh_btn.add_callback(_refresh_layers)
    chunk_api.add_param(refresh_btn)

    export_btn = chunk.Button("export", "Export", tooltip="Write the selected layer/stage to the output file.")
    export_btn.add_callback(export)
    chunk_api.add_param(export_btn)

    return chunk_api


def config():
    return chunk.DefaulConfig(
        category="Composition",
        description=(
            "Exports the current stage — fully flattened, the session layer, or a single\n"
            "chosen sublayer — to a USD file on disk. Press Refresh Layers to populate\n"
            "the Layer menu, pick a target, then Export."
        ),
    )
