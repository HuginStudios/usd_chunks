"""Render Product chunk -- creates a UsdRender.Product prim: one rendered output
file, its camera, and the ordered AOVs it contains.

Requires: pxr (OpenUSD), usd_chunks.
"""

from __future__ import annotations

import logging
import os
import re

from pxr import Sdf, UsdRender

from usd_chunks import chunk, chunk_utils

# Parented under "usd_chunks" so this module's messages reach the package handler and
# its INFO level; a chunk module imported under its own file name otherwise logs from
# outside the package tree, where only WARNING and above escape, unformatted, through
# logging's lastResort. See the note in render.py.
logger = logging.getLogger(f"usd_chunks.{__name__}")

#: Formats that hold one image, so a product written to one cannot carry several AOVs.
#: EXR is the notable absence -- multi-part and multi-channel, which is why AOVs
#: conventionally go there as layers.
_SINGLE_IMAGE_FORMATS = {".png", ".jpg", ".jpeg", ".tga", ".bmp", ".gif", ".ppm"}

#: A run of hashes, or a {frame} field -- the two ways a product path asks for a frame
#: number. Renderers expand them; USD's own check for a written product does not.
_FRAME_PLACEHOLDER = re.compile(r"#+|\{frame\}")


def _warn_if_usd_cannot_find_it(product_name):
    """Say so when USD will not recognise a product the renderer did write.

    After rendering, UsdAppUtils.FrameRecorder decides whether the delegate produced a
    product by opening productName -- verbatim, with ArchOpenFile. It expands nothing
    and resolves nothing, so two shapes make that check fail even when the file exists:
    a relative path, which it looks for under the process's working directory rather
    than the scene's, and a frame placeholder, which never matches the numbered file a
    renderer actually writes. Either way the render is fine and the report is wrong --
    "Missing generated Product" with the image sitting on disk.
    """
    if not product_name:
        return

    if _FRAME_PLACEHOLDER.search(product_name):
        logger.warning(
            "Render Product: %r contains a frame placeholder. USD checks for a written "
            "product by opening the name exactly as written, so it will report this one "
            "missing even when the renderer wrote the numbered file.",
            product_name,
        )

    if not os.path.isabs(product_name):
        logger.warning(
            "Render Product: %r is relative, and USD looks for a written product under "
            "the working directory rather than beside the scene. An absolute path is "
            "checked where it actually lands.",
            product_name,
        )


def _warn_if_format_cannot_hold(product_name, var_paths):
    """Say so when a product asks a format to carry more AOVs than it can.

    In USD one RenderProduct is one file, so several AOVs in one product means several
    layers in one image. PNG has nowhere to put them: what a renderer does with that is
    undefined and differs between them, and the usual outcome is quietly losing all but
    the first. Caught at cook time rather than at render time.
    """
    if len(var_paths) < 2 or not product_name:
        return

    extension = os.path.splitext(product_name)[1].lower()
    if extension not in _SINGLE_IMAGE_FORMATS:
        return

    logger.warning(
        "Render Product: %r is %s, which holds one image, but this product has %d AOVs "
        "(%s). Give each AOV its own Render Product, or write to .exr to keep them as "
        "layers in one file.",
        product_name,
        extension,
        len(var_paths),
        ", ".join(Sdf.Path(p).name for p in var_paths),
    )


def _existing_vars(stage, var_paths):
    """The var paths that hold a real UsdRenderVar, with the rest reported and dropped.

    A disabled Render Var chunk never runs, so its prim is never authored -- but its
    path stays in this chunk's Ordered Vars, and authoring it anyway leaves orderedVars
    pointing at nothing. Renderers cope, each in its own way: V-Ray Standalone skips
    each one with a "no suitable UsdRenderVar prim was found" runtime error, which
    buries the real diagnostics under one message per dangling target. Dropping them
    here keeps the authored relationship to vars that exist, and disabling a var chunk
    then means what it looks like it means.

    Reported at INFO rather than WARNING: a var is usually disabled on purpose, and
    saying so once per cook at warning level would make the ordinary case look broken.
    """
    live = [path for path in var_paths if UsdRender.Var(stage.GetPrimAtPath(path))]
    kept = set(live)
    if dropped := [path for path in var_paths if path not in kept]:
        logger.info(
            "Render Product: %d of %d Ordered Vars have no RenderVar prim and were left "
            "out of orderedVars (%s). A disabled Render Var chunk authors nothing.",
            len(dropped),
            len(var_paths),
            ", ".join(Sdf.Path(p).name for p in dropped),
        )
    return live


def main():
    chunk_api = chunk.Chunk("Render Product")

    # A literal, not get_unique_path(stage, ...). A default computed from whatever the stage
    # holds at construction is a value wearing a default's clothes: the save path leaves
    # defaults out on the grounds that running main() again reproduces them, and running
    # main() again does not reproduce this one -- on reload no prim exists yet, so every
    # instance is handed the same path back. execute() does the uniquifying and records the
    # result as a value, which is what survives a save.
    prim_path = "/Render/Products/beauty"

    chunk_api.add_params([
        chunk.LineEdit(
            "primpath_location", "Prim Path",
            default_value=prim_path,
            flags=chunk.ChunkFlags.ParamFlags.LABELED,
        ),
        chunk.LineEdit("product_name", "Output File"),
        chunk.Menu("product_type", "Product Type", menu_items=["raster"], default_value="raster"),
        chunk.LineEdit(
            "camera", "Camera",
            flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        ),
        chunk.LineEdit("ordered_vars", "Ordered Vars"),
    ])
    return chunk_api


def execute(node):
    stage = node.get_stage()

    raw_path = node.get_param("primpath_location").value
    if not raw_path:
        return

    primpath_param = node.get_param("primpath_location")
    for target_path in chunk_utils.author_in_variant_contexts(stage, primpath_param, unique=True):
        product = UsdRender.Product.Define(stage, Sdf.Path(target_path))
        if not product:
            return

        if product_name := node.get_param("product_name").value:
            product.CreateProductNameAttr().Set(product_name)
            _warn_if_usd_cannot_find_it(product_name)
        product.CreateProductTypeAttr().Set(node.get_param("product_type").value)

        # Resolved rather than taken verbatim -- see render_settings.py: a PRIM_SELECTION
        # param can hold several paths, and a relationship target must be one absolute path.
        if camera_targets := chunk_utils.target_paths(node.get_param("camera"))[:1]:
            product.CreateCameraRel().SetTargets(camera_targets)

        if var_paths := _existing_vars(stage, chunk_utils.target_paths(node.get_param("ordered_vars"))):
            product.CreateOrderedVarsRel().SetTargets(var_paths)
            _warn_if_format_cannot_hold(node.get_param("product_name").value, var_paths)


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD_RENDER,
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

            Empty by default, and empty authors no productName at all: a product
            that has not been told where to write should say nothing rather than
            quietly claim a filename in whichever directory the render happened to
            run from. Name one before expecting this product to produce a file.

            One product is one file, so several Ordered Vars means several layers
            in one image. EXR holds them; PNG and the other single-image formats
            do not, and what a renderer does when asked anyway is undefined. That
            case is warned about at cook time — give each AOV its own Render
            Product to write separate files.

            Two shapes are warned about for a different reason. After rendering,
            USD decides whether the delegate produced a product by opening this
            name verbatim: it expands no frame placeholder and resolves nothing
            against the scene. So a relative path is looked for under the working
            directory, and a ####-style name never matches the numbered file a
            renderer writes — reporting "Missing generated Product" while the
            image sits on disk. An absolute path without a placeholder is the one
            USD can confirm.
          - Product Type: what kind of output this is. "raster" is the only type
            defined by UsdRender itself; renderers may understand others.
          - Camera: the camera prim to render from. Leave empty to inherit the
            camera from the Render Settings that references this product.
          - Ordered Vars: the RenderVar prims making up this product's channels,
            as paths or glob patterns. Order is preserved and duplicates dropped,
            because orderedVars is an ordered relationship. Name the Render Var
            chunks' prim paths in here. A path with no RenderVar prim behind it —
            a disabled Render Var chunk authors none — is left out rather than
            written as a dangling target.

        Everything authored is standard UsdRender scene description, readable by
        any renderer whether or not usd_chunks is installed.
        """,
    )
