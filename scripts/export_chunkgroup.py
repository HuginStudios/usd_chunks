"""Export ChunkGroup chunk — exports the current chunk graph as one clean, portable
USD layer: every chunk's own bookkeeping (wiring, node-graph position, chunk type)
freshly authored into a brand-new layer, with no subLayerPaths at all.

Requires: pxr (OpenUSD), usd_chunks.
"""
from __future__ import annotations

import logging
import os
import traceback

from usd_chunks import chunk, chunk_registry
from usd_chunks.plugins.chunk_usd import export_chunks

logger = logging.getLogger(__name__)


def export(owner, notice, sender):
    _chunk = owner.get_chunk()
    if not _chunk:
        return

    is_s = chunk.is_param_t(str)
    output_path = _chunk.get_param("output_file", is_s).value.strip()
    if not output_path:
        logger.warning("No output path set.")
        return

    _, ext = os.path.splitext(output_path)
    if ext.lower() not in (".usd", ".usda", ".usdc", ".usdz"):
        output_path += ".usda"

    scene = chunk_registry.ChunkRegistry().scene
    try:
        export_chunks(scene, output_path)
    except Exception:
        # A NoticeConnection callback's exceptions are caught and only logged
        # (chunk_notice.NoticeConnection.handle_callback) — never surfaced in the UI,
        # so without this print a crash here looks identical to nothing happening.
        logger.error("Export failed:\n%s", traceback.format_exc())
        return
    logger.info("Chunk graph exported to %s", output_path)


def main():
    chunk_api = chunk.Chunk("Export ChunkGroup")

    chunk_api.add_param(
        chunk.FileBrowser(
            "output_file",
            "Output File",
            extensions=["usd", "usda", "usdc", "usdz"],
            tooltip="Destination file. The extension picks the format: .usda (text), "
            ".usdc (binary), .usd (either), or .usdz (package).",
        )
    )

    export_btn = chunk.Button(
        "export",
        "Export",
        tooltip="Write every chunk currently in the graph to Output File as one "
        "self-contained layer — no sublayer references, so it's portable outside "
        "this session. Reloading it regenerates every chunk's driven content by "
        "re-running execute().",
    )
    export_btn.add_callback(export)
    chunk_api.add_param(export_btn)

    return chunk_api


def config():
    return chunk.DefaulConfig(
        category="Composition",
        description=(
            "Exports the current chunk graph as one clean, portable USD file — every\n"
            "chunk's own bookkeeping (wiring, node-graph position, chunk type) freshly\n"
            "authored into a brand-new layer, with no subLayerPaths at all.\n"
            "\n"
            "Unlike picking Session Layer in the Export USD chunk (a raw copy of the\n"
            "live session layer, which can carry forward subLayerPaths referencing\n"
            "in-memory anon: layers that don't resolve once the session ends), this\n"
            "rebuilds every chunk prim from scratch via the same authoring path used\n"
            "when chunks are first created — always self-contained.\n"
            "\n"
            "Reloading the exported file regenerates every chunk's driven content\n"
            "automatically, by re-running each chunk's execute()."
        ),
    )
