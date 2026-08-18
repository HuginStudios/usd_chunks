"""Render Settings chunk — creates a UsdRender.Settings prim: the resolution,
camera and products that describe one render.

Requires: pxr (OpenUSD), usd_chunks.
"""

from __future__ import annotations

import logging

from pxr import Gf, Sdf, Usd, UsdRender

from usd_chunks import chunk, chunk_notice, chunk_utils, render_defs
from usd_chunks.plugins import chunk_usd

logger = logging.getLogger(__name__)

_CONFORM_POLICIES = [
    "expandAperture",
    "cropAperture",
    "adjustApertureWidth",
    "adjustApertureHeight",
    "adjustPixelAspectRatio",
]

_STAGE_SETTINGS_METADATA = "renderSettingsPrimPath"

_DELEGATE_NONE = "None"

#: Params that belong to the chunk itself and survive a delegate change. Everything else
#: on the chunk was lofted from a renderer's settings definition and gets rebuilt.
_FIXED = {
    "primpath_location", "resolution", "pixel_aspect_ratio", "aspect_ratio_conform_policy",
    "camera", "products", "included_purposes", "material_binding_purposes",
    "rendering_color_space", "data_window_ndc", "disable_motion_blur",
    "disable_depth_of_field", "set_stage_settings", "reference", "delegate",
}


def _delegate_defs():
    """Renderer settings definitions by label, for the delegate menu."""
    return {d.label: d for d in render_defs.iter_render_settings_defs()}


def _clear_delegate_params(owner):
    """Remove every param lofted from the previously selected delegate."""
    for param in owner.get_my_params():
        if param.name not in _FIXED:
            owner.remove_param_or_group(param)


#: Where a group parks the settings it has not built params for yet.
_PENDING = "_pending_settings"

#: Namespace every chunk param's saved value lives under on the chunk prim.
_INPUTS_PREFIX = "inputs:"


def _authored_settings(owner):
    """Saved values for lofted settings, as (param path, leaf name, value).

    A param nested in groups is stored under its whole path -- a setting inside
    BakeView is ``inputs:BakeView:vray_BakeView_enabled``, not
    ``inputs:vray_BakeView_enabled`` -- so the leaf name is what identifies which
    setting it is, while the dotted path is what get_param needs to find it once the
    groups exist.

    Read straight off the chunk prim rather than from params, because the params in
    question may not exist yet -- that is the whole point of building them lazily. Used
    both to decide which groups must be built up front and to re-apply their values
    afterwards, since load restores inputs: attributes in whatever order the prim lists
    them and may well reach a setting before the delegate that creates it.
    """
    stage = owner.get_stage()
    if not stage:
        return {}

    # Several paths may identify a cooking or loading chunk and they do not always
    # agree -- _exec_prim_path is what execute() matches on, while get_chunk_prim_path
    # derives one, and inside a chunkgroup or mid-load they differ. Checking only one
    # silently finds nothing, which here means quietly dropping every saved value.
    candidates = {
        getattr(owner, "_exec_prim_path", None),
        getattr(owner, "prim_path", None),
        chunk_usd.get_chunk_prim_path(owner),
    }

    seen = set()
    entries = []
    for path in (str(p) for p in candidates if p):
        prim = stage.GetPrimAtPath(path)
        if not prim or not prim.IsValid():
            continue
        for attr in prim.GetAttributes():
            name = attr.GetName()
            if not name.startswith(_INPUTS_PREFIX) or not attr.HasAuthoredValue():
                continue
            param_path = name[len(_INPUTS_PREFIX):]
            if param_path in seen:
                continue
            seen.add(param_path)
            entries.append((param_path.replace(":", "."), param_path.rpartition(":")[2], attr.Get()))
    return entries


def _populate(group):
    """Build the params a group was holding, if it still has any."""
    pending = getattr(group, _PENDING, None)
    if not pending:
        return
    setattr(group, _PENDING, None)

    for prop in pending:
        param = render_defs.param_for_setting(prop)
        if param is None:
            continue
        if prop.help:
            param.description = prop.help
        group.add_param(param)


def _group_expanded(owner, notice, sender):
    if not owner.has_state(chunk.ChunkFlags.State.COLLAPSED):
        _populate(owner)


def _new_group(name):
    group = chunk.Group(
        name, name,
        flags=chunk.ChunkFlags.GroupFlags.COLLAPSABLE | chunk.ChunkFlags.GroupFlags.LABELED,
    )
    # Collapsed by default: a renderer declares hundreds of settings, and every group
    # expanded makes a node taller than the graph.
    group.set_state(chunk.ChunkFlags.State.COLLAPSED)
    return group


def _loft_group(owner, name, contents, authored):
    """Add one group of settings to owner, recursing into nested groups.

    A leaf group keeps its settings pending rather than building params for them: every
    group starts collapsed, and building all 258 of V-Ray's up front took twenty seconds
    before a single one was visible. The params appear when the group is first expanded,
    which is the moment they are wanted and the only moment their cost is noticed.

    A group whose settings already have saved values is built immediately, since those
    values have to land somewhere whether or not anyone opens it.

    Returns True when the group holds anything.
    """
    group = _new_group(name)

    if isinstance(contents, dict):
        added = False
        for sub_name, sub_contents in contents.items():
            added |= _loft_group(group, sub_name, sub_contents, authored)
        if added:
            owner.add_param(group)
        return added

    if not contents:
        return False

    setattr(group, _PENDING, list(contents))
    if any(prop.name in authored for prop in contents):
        # Built before attaching, so one group arrives complete rather than a param at a
        # time -- the same reason the lazy path is worth having at all.
        _populate(group)

    group.add_callback(
        _group_expanded,
        notice_alias=chunk.ChunkNoticeAlias.STATE_CHANGED,
        send_on_create=False,
    )
    owner.add_param(group)
    return True


def _loft_delegate_params(owner, definition):
    """Create the groups for a renderer's settings, filling in what is already needed.

    render_defs.group_props follows the structure already in the names -- Settings splits
    into Camera, ImageSampler, Options, and Options again into geom, light, mtl -- and
    returns a tree, so this walks it. Params themselves are deferred to first expand.
    """
    authored = _authored_settings(owner)
    authored_leaves = {leaf for _path, leaf, _value in authored}

    for group_name, contents in render_defs.group_props(definition.props).items():
        _loft_group(owner, group_name, contents, authored_leaves)

    # Re-apply saved values now the params exist. Load restores inputs: attributes in
    # prim order, so a setting may well have been restored before the delegate that
    # creates it, and that write would have found no param to land on.
    for param_path, _leaf, value in authored:
        param = owner.get_param(param_path)
        if param is not None and value is not None and param.value != value:
            param.value = value


def _chunk_loaded(owner, notice, sender):
    """Loft the delegate's settings for a chunk restored from a file.

    Needed as well as the param callback: load suppresses reactive notices while it
    restores values, so setting delegate from the saved inputs:delegate never reaches
    _delegate_changed, and the settings would come back as a bare standard chunk.
    """
    _chunk = owner.get_chunk() if hasattr(owner, "get_chunk") else owner
    if _chunk is None or _chunk.get_param("delegate") is None:
        return
    _delegate_changed(_chunk, notice, sender)


def _delegate_changed(owner, notice, sender):
    _chunk = owner.get_chunk()
    if _chunk is None:
        return

    _clear_delegate_params(_chunk)

    label = _chunk.get_param("delegate").value
    if not label or label == _DELEGATE_NONE:
        return

    definition = _delegate_defs().get(label)
    if definition is None:
        logger.warning("Render Settings: no renderer settings named %r are installed here.", label)
        return

    _loft_delegate_params(_chunk, definition)


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


def _author_delegate_settings(node, prim):
    """Apply the selected renderer's API schema and author the settings that differ.

    Only non-default values are written. The schema already carries every fallback, so
    authoring all 258 would bury the handful that were actually changed and make the
    layer unreadable in a diff -- the same reason chunk params only create an inputs:
    attribute once their value leaves the default.
    """
    delegate_param = node.get_param("delegate")
    if delegate_param is None:
        return

    label = delegate_param.value
    if not label or label == _DELEGATE_NONE:
        return

    definition = _delegate_defs().get(label)
    if definition is None:
        return

    if definition.schema_name:
        # AddAppliedSchema rather than ApplyAPI: the schema's library is not loaded
        # until something uses it, so the TfType ApplyAPI wants is not resolvable here.
        prim.AddAppliedSchema(definition.schema_name)

    for prop in definition.props:
        param = node.get_param(prop.name)
        if param is None:
            continue

        value = param.value
        if value is None or value == prop.default:
            continue

        value_type = Sdf.ValueTypeNames.Find(prop.sdf_type)
        if not value_type:
            continue

        attr = prim.CreateAttribute(prop.attr_name, value_type)
        try:
            attr.Set(value)
        except Exception as exc:  # noqa: BLE001 - a param value this attr cannot hold
            logger.debug("Render Settings: could not author %s: %s", prop.attr_name, exc)


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

    delegate = chunk.Menu(
        "delegate", "Delegate",
        menu_items=[_DELEGATE_NONE] + sorted(_delegate_defs()),
        default_value=_DELEGATE_NONE,
    )
    delegate.add_callback(_delegate_changed, send_on_create=False)
    chunk_api.add_param(delegate)
    chunk_api.add_callback(
        _chunk_loaded,
        notice_alias=chunk_notice.ChunkNoticeAlias.CHUNK_LOADED,
    )

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

        # Resolved, not taken verbatim: the camera param carries PRIM_SELECTION, so it is
        # seeded from whatever happened to be selected when the chunk was created -- which
        # may be several paths at once, or none of them a prim. A relationship target has
        # to be a single absolute path, and USD raises rather than ignoring a bad one.
        if camera_targets := _resolve_targets(stage, node.get_param("camera"))[:1]:
            settings.CreateCameraRel().SetTargets(camera_targets)

        if product_paths := _resolve_targets(stage, node.get_param("products")):
            settings.CreateProductsRel().SetTargets(product_paths)

        authored = settings.GetPrim().GetPath().pathString
        if not var_selections and authored != target_path:
            node.get_param("primpath_location").value = authored

        node.get_param("reference").value = authored

        _author_delegate_settings(node, settings.GetPrim())

    # Renderers find the settings prim through this stage metadatum rather than by
    # path convention. Outside the variant context and the chunk's edit target,
    # since stage metadata is only accepted on the root or session layer.
    _set_stage_settings_path(stage, authored if node.get_param("set_stage_settings").value else None)


def config():
    # One menu entry per renderer whose settings this USD can describe, so V-Ray shows
    # up because Chaos ships a schema rather than because anything here knows about
    # V-Ray. Empty in a runtime with no renderer plugins, which is the right answer
    # there rather than an error.
    variants = [(label, {"delegate": label}) for label in sorted(_delegate_defs())]

    return chunk.DefaulConfig(
        category=chunk.StandardCategory.UTILS,
        variants=variants,
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
          - Delegate: which renderer's own settings to expose alongside the standard
            ones. The menu lists every renderer this USD build can describe, and each
            also appears as its own entry in the chunk menu.

            Selecting one lofts that renderer's settings onto the chunk, grouped the
            way its own UI groups them and collapsed by default -- V-Ray declares 258
            of them. Only values changed from their default are authored, as
            namespaced attributes on the RenderSettings prim (vray:..., ri:...), which
            is how every renderer reads its settings: Hydra gathers them and each
            delegate picks out the namespace it owns. The matching API schema is
            applied at the same time where there is one.

            Nothing here is renderer-specific. A renderer appears because it declares
            its settings in a way USD can read -- V-Ray ships an applied API schema,
            RenderMan registers .args files into the shader registry -- so installing
            one is all it takes for it to show up.
          - Reference (output): the authored prim path, for wiring into a Render
            chunk.

        Everything authored is standard UsdRender scene description, readable by
        any renderer whether or not usd_chunks is installed.
        """,
    )
