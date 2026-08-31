"""Render Var chunk — creates a UsdRender.Var prim describing a single AOV.

Requires: pxr (OpenUSD), usd_chunks.
"""

from __future__ import annotations

from pxr import Sdf, UsdRender

from usd_chunks import chunk, chunk_notice, chunk_utils


_DATA_TYPES = ["color3f", "float", "normal3f", "vector3f", "point3f", "int"]
_SOURCE_TYPES = ["raw", "primvar", "lpe", "intrinsic"]


def main():
    chunk_api = chunk.Chunk("Render Var")

    stage = chunk_api.get_stage()
    prim_path = chunk_utils.get_unique_path(stage, "/Render/Vars/beauty")

    chunk_api.add_params([
        chunk.LineEdit(
            "primpath_location", "Prim Path",
            default_value=prim_path,
            flags=chunk.ChunkFlags.ParamFlags.LABELED,
        ),
        chunk.Menu("data_type", "Data Type", menu_items=_DATA_TYPES, default_value="color3f"),
        chunk.LineEdit("source_name", "Source Name", default_value="Ci"),
        chunk.Menu("source_type", "Source Type", menu_items=_SOURCE_TYPES, default_value="raw"),
        chunk.LineEdit(
            "reference", "Reference",
            flags=chunk.ChunkFlags.ParamFlags.OUTPUT | chunk.ChunkFlags.ParamFlags.LABELED,
        ),
    ])
    return chunk_api


def execute(node):
    stage = node.get_stage()
    if not stage:
        return

    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    raw_path = node.get_param("primpath_location").value
    if not raw_path:
        return

    _, _, var_selections = chunk_utils.split_variant_path(raw_path)
    if not var_selections and not chunk_notice._loading_stage:
        raw_path = chunk_utils.get_unique_path(stage, raw_path)

    with chunk_utils.author_in_variant_context(stage, raw_path) as target_path:
        render_var = UsdRender.Var.Define(stage, Sdf.Path(target_path))
        if not render_var:
            return

        render_var.CreateDataTypeAttr().Set(node.get_param("data_type").value)
        render_var.CreateSourceNameAttr().Set(node.get_param("source_name").value)
        render_var.CreateSourceTypeAttr().Set(node.get_param("source_type").value)

        authored = render_var.GetPrim().GetPath().pathString
        if not var_selections and authored != target_path:
            node.get_param("primpath_location").value = authored

        node.get_param("reference").value = authored


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.UTILS,
        description="""
        Render Var chunk — creates a UsdRender.Var prim, the USD description of a
        single AOV (arbitrary output variable).

        Parameters:
          - Prim Path: stage path for the RenderVar prim. Auto-initialised to a
            unique /Render/Vars/beauty path.
          - Data Type: the type of the channel this var produces (color3f for a
            beauty or colour AOV, float for depth or alpha, normal3f/vector3f for
            geometric AOVs).
          - Source Name: the renderer-side name of the value being written. What
            is valid here depends on the render delegate — "Ci" and "a" for
            RenderMan-style output, an LPE string, or a primvar name.
          - Source Type: how Source Name should be interpreted — raw (a direct
            renderer output), primvar, lpe (light path expression), or intrinsic.
          - Reference (output): the authored prim path, for wiring into a Render
            Product's Ordered Vars.

        Wire the Reference output into a Render Product to include this AOV in
        that product's output. The result is standard UsdRender scene
        description, readable by any renderer whether or not usd_chunks is
        installed.
        """,
    )
