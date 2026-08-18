"""Render Settings chunk — creates a UsdRender.Settings prim: the resolution,
camera and products that describe one render.

Requires: pxr (OpenUSD), usd_chunks.
"""

from __future__ import annotations

from pxr import Gf, Sdf, Usd, UsdRender

from usd_chunks import chunk, chunk_notice, chunk_utils


_CONFORM_POLICIES = [
    "expandAperture",
    "cropAperture",
    "adjustApertureWidth",
    "adjustApertureHeight",
    "adjustPixelAspectRatio",
]

_STAGE_SETTINGS_METADATA = "renderSettingsPrimPath"


def _set_stage_settings_path(stage, prim_path):
    """Point the stage's renderSettingsPrimPath at prim_path, or clear it.

    Written to the session layer, not the chunk's own layer: USD only accepts stage
    metadata on the root or session layer, and authoring it into the root layer would
    put a chunk's bookkeeping into the user's scene file, where nothing would ever
    clear it. Session-layer state is re-established on every cook and never saved.
    """
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        if prim_path:
            stage.SetMetadata(_STAGE_SETTINGS_METADATA, prim_path)
        elif stage.HasAuthoredMetadata(_STAGE_SETTINGS_METADATA):
            stage.ClearMetadata(_STAGE_SETTINGS_METADATA)


def _token_list(value):
    """Comma-separated tokens, trimmed, with empties dropped."""
    return [token.strip() for token in (value or "").split(",") if token.strip()]


def _resolve_targets(stage, param):
    """Prim paths named by a path/glob param, order preserved, duplicates dropped."""
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
    chunk_api = chunk.Chunk("Render Settings")

    stage = chunk_api.get_stage()
    prim_path = chunk_utils.get_unique_path(stage, "/Render/settings")

    chunk_api.add_params([
        chunk.LineEdit(
            "primpath_location", "Prim Path",
            default_value=prim_path,
            flags=chunk.ChunkFlags.ParamFlags.LABELED,
        ),
        chunk.Vector(
            "resolution", "Resolution",
            param=chunk.Slider(is_int=True, min_value=1, max_value=16384),
            length=2, suffixes="XY", default_value=(1920, 1080),
        ),
        chunk.Slider("pixel_aspect_ratio", "Pixel Aspect Ratio", default_value=1.0, min_value=0.1, max_value=4.0),
        chunk.Menu(
            "aspect_ratio_conform_policy", "Conform Policy",
            menu_items=_CONFORM_POLICIES, default_value="expandAperture",
        ),
        chunk.LineEdit(
            "camera", "Camera",
            flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        ),
        chunk.LineEdit("products", "Products"),
        chunk.LineEdit("included_purposes", "Included Purposes", default_value="default,render"),
        chunk.LineEdit("material_binding_purposes", "Material Binding Purposes"),
        chunk.LineEdit("rendering_color_space", "Rendering Color Space"),
        chunk.Vector(
            "data_window_ndc", "Data Window NDC",
            param=chunk.Slider(min_value=-1.0, max_value=2.0),
            length=4, suffixes=("min X", "min Y", "max X", "max Y"),
            default_value=(0.0, 0.0, 1.0, 1.0),
        ),
        chunk.Checkbox("disable_motion_blur", "Disable Motion Blur", default_value=False),
        chunk.Checkbox("disable_depth_of_field", "Disable Depth of Field", default_value=False),
        chunk.Checkbox("set_stage_settings", "Set as Stage Render Settings", default_value=True),
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
        settings = UsdRender.Settings.Define(stage, Sdf.Path(target_path))
        if not settings:
            return

        res = node.get_param("resolution").value
        settings.CreateResolutionAttr().Set(Gf.Vec2i(int(res[0]), int(res[1])))
        settings.CreatePixelAspectRatioAttr().Set(float(node.get_param("pixel_aspect_ratio").value))
        settings.CreateAspectRatioConformPolicyAttr().Set(
            node.get_param("aspect_ratio_conform_policy").value
        )

        purposes = _token_list(node.get_param("included_purposes").value)
        if purposes:
            settings.CreateIncludedPurposesAttr().Set(purposes)

        if binding_purposes := _token_list(node.get_param("material_binding_purposes").value):
            settings.CreateMaterialBindingPurposesAttr().Set(binding_purposes)

        if color_space := node.get_param("rendering_color_space").value.strip():
            settings.CreateRenderingColorSpaceAttr().Set(color_space)

        window = node.get_param("data_window_ndc").value
        settings.CreateDataWindowNDCAttr().Set(
            Gf.Vec4f(float(window[0]), float(window[1]), float(window[2]), float(window[3]))
        )

        settings.CreateDisableMotionBlurAttr().Set(bool(node.get_param("disable_motion_blur").value))
        settings.CreateDisableDepthOfFieldAttr().Set(bool(node.get_param("disable_depth_of_field").value))

        if camera_path := node.get_param("camera").value:
            settings.CreateCameraRel().SetTargets([Sdf.Path(camera_path)])

        if product_paths := _resolve_targets(stage, node.get_param("products")):
            settings.CreateProductsRel().SetTargets(product_paths)

        authored = settings.GetPrim().GetPath().pathString
        if not var_selections and authored != target_path:
            node.get_param("primpath_location").value = authored

        node.get_param("reference").value = authored

    # Renderers find the settings prim through this stage metadatum rather than by
    # path convention. Outside the variant context and the chunk's edit target,
    # since stage metadata is only accepted on the root or session layer.
    _set_stage_settings_path(stage, authored if node.get_param("set_stage_settings").value else None)


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.UTILS,
        description="""
        Render Settings chunk — creates a UsdRender.Settings prim describing one
        render: its resolution, the camera it uses, and the products it writes.

        Parameters:
          - Prim Path: stage path for the RenderSettings prim. Auto-initialised to
            a unique /Render/settings path.
          - Resolution: output resolution in pixels.
          - Pixel Aspect Ratio: non-square-pixel ratio. 1.0 for square pixels.
          - Conform Policy: how the camera aperture is reconciled with the output
            aspect ratio when the two disagree — expandAperture, cropAperture,
            adjustApertureWidth, adjustApertureHeight, or adjustPixelAspectRatio.
          - Camera: the camera prim to render from. Products may override it.
          - Products: the RenderProduct prims this render writes, as paths or glob
            patterns. Wire Render Product chunks' Reference outputs in here.
          - Included Purposes: comma-separated purposes to include in the render,
            defaulting to "default,render". This is what excludes proxy and guide
            geometry from a final render.
          - Material Binding Purposes: comma-separated material purposes to resolve
            bindings against — typically "full", or "preview" for a lighter render.
            Empty means all purposes.
          - Rendering Color Space: the color space renderers should work in, e.g.
            lin_rec709 or acescg. Empty leaves it to the renderer.
          - Data Window NDC: the region of the image to render, in normalised
            device coordinates as (min X, min Y, max X, max Y). The default
            (0, 0, 1, 1) is the full frame; a smaller window renders a crop.
          - Disable Motion Blur / Disable Depth of Field: switch off those effects
            for this render regardless of what the camera and scene specify.
            (UsdRender also carries the older instantaneousShutter for motion blur;
            it is deprecated in favour of Disable Motion Blur, and exposing both
            would only invite the two disagreeing, so it is deliberately absent.)
          - Set as Stage Render Settings: writes the stage's renderSettingsPrimPath
            metadatum so renderers and usdrecord find this prim without being told
            where it is. On by default; turn it off when a network authors more
            than one settings prim and something else chooses between them.
            Written to the session layer rather than this chunk's own layer, because
            USD only accepts stage metadata on the root or session layer — so it is
            runtime state that never lands in a saved scene file. A handed-off file
            therefore carries the RenderSettings prim but not the pointer to it;
            tell the renderer the path explicitly in that case.
          - Reference (output): the authored prim path, for wiring into a Render
            chunk.

        Everything authored is standard UsdRender scene description, readable by
        any renderer whether or not usd_chunks is installed.
        """,
    )
