"""Export USD chunk — exports the current stage or chunk layer to disk."""

from __future__ import annotations

import os

from usd_chunks import chunk

_MODES = ["Stage", "Layer"]


def export(owner, notice, sender):
    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage:
        return

    output_path = (_chunk.get_param("output_file").value or "").strip()
    if not output_path:
        print("[ExportUSD] No output path set.")
        return

    _, ext = os.path.splitext(output_path)
    if ext.lower() not in (".usd", ".usda", ".usdc", ".usdz"):
        output_path += ".usda"

    mode = _chunk.get_param("mode").value or "Stage"

    if mode == "Layer":
        layer = owner.get_edit_context()
        if not layer:
            print("[ExportUSD] No edit layer available.")
            return
        layer.Export(output_path)
        print(f"[ExportUSD] Layer exported to {output_path}")
    else:
        stage.Export(output_path)
        print(f"[ExportUSD] Stage exported to {output_path}")


def main():
    chunk_api = chunk.Chunk("Export USD")

    chunk_api.add_param(
        chunk.FileBrowser(
            "output_file",
            "Output File",
            extensions=["usd", "usda", "usdc", "usdz"],
        )
    )

    chunk_api.add_param(
        chunk.Menu(
            "mode",
            "Export Mode",
            menu_items=_MODES,
            default_value="Stage",
        )
    )

    export_btn = chunk.Button("export", "Export")
    export_btn.add_callback(export)
    chunk_api.add_param(export_btn)

    return chunk_api


def config():
    return chunk.DefaulConfig(category="Composition")
