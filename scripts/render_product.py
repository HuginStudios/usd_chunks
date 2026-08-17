"""Render Product chunk — creates a UsdRender.Product prim: one rendered output
file, its camera, and the ordered AOVs it contains.

Requires: pxr (OpenUSD), usd_chunks.
"""

from __future__ import annotations

from pxr import Sdf, UsdRender

from usd_chunks import chunk, chunk_notice, chunk_utils


def _resolve_targets(stage, param):
    """Prim paths named by a path/glob param, in the order given.

    iter_prim_targets resolves globs and %collection tokens the same way the rest
    of the toolkit does; ordering matters here because orderedVars is an ordered
    relationship, so duplicates are dropped rather than re-sorted.
    """
    seen = set()
    paths = []
    for path in chunk_utils.iter_prim_targets(stage, param):
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        paths.append(Sdf.Path(key))
    return paths


def main():
    chunk_api = chunk.Chunk("Render Product")

    stage = chunk_api.get_stage()
    prim_path = chunk_utils.get_unique_path(stage, "/Render/Products/beauty")

    chunk_api.add_params([
        chunk.LineEdit(
            "primpath_location", "Prim Path",
            default_value=prim_path,
            flags=chunk.ChunkFlags.ParamFlags.LABELED,
        ),
        chunk.LineEdit("product_name", "Output File", default_value="render.exr"),
        chunk.Menu("product_type", "Product Type", menu_items=["raster"], default_value="raster"),
        chunk.LineEdit(
            "camera", "Camera",
            flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        ),
        chunk.LineEdit("ordered_vars", "Ordered Vars"),
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
        product = UsdRender.Product.Define(stage, Sdf.Path(target_path))
        if not product:
            return

        if product_name := node.get_param("product_name").value:
            product.CreateProductNameAttr().Set(product_name)
        product.CreateProductTypeAttr().Set(node.get_param("product_type").value)

        if camera_path := node.get_param("camera").value:
            product.CreateCameraRel().SetTargets([Sdf.Path(camera_path)])

        if var_paths := _resolve_targets(stage, node.get_param("ordered_vars")):
            product.CreateOrderedVarsRel().SetTargets(var_paths)

        authored = product.GetPrim().GetPath().pathString
        if not var_selections and authored != target_path:
            node.get_param("primpath_location").value = authored

        node.get_param("reference").value = authored


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.UTILS,
        description="""
        Render Product chunk — creates a UsdRender.Product prim: one rendered
        output, the camera it is rendered from, and the ordered list of AOVs it
        contains.

        Parameters:
          - Prim Path: stage path for the RenderProduct prim. Auto-initialised to
            a unique /Render/Products/beauty path.
          - Output File: the productName attribute — the file the renderer writes.
            Frame-numbering conventions (render.####.exr) are passed through
            untouched for the renderer to expand.
          - Product Type: what kind of output this is. "raster" is the only type
            defined by UsdRender itself; renderers may understand others.
          - Camera: the camera prim to render from. Leave empty to inherit the
            camera from the Render Settings that references this product.
          - Ordered Vars: the RenderVar prims making up this product's channels,
            as paths or glob patterns. Order is preserved and duplicates dropped,
            because orderedVars is an ordered relationship. Wire Render Var
            chunks' Reference outputs in here.
          - Reference (output): the authored prim path, for wiring into a Render
            Settings chunk's Products.

        Everything authored is standard UsdRender scene description, readable by
        any renderer whether or not usd_chunks is installed.
        """,
    )
