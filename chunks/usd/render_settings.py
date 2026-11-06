"""Render Settings chunk -- creates a UsdRender.Settings prim: the resolution,
camera and products that describe one render.

Requires: pxr (OpenUSD), usd_chunks.
"""

from __future__ import annotations

import logging

from pxr import Gf, Sdf, Usd, UsdRender

from usd_chunks import chunk, chunk_notice, chunk_registry, chunk_utils, render_defs, renderer_params

# Parented under "usd_chunks" so this module's messages reach the package handler and
# its INFO level; a chunk module imported under its own file name otherwise logs from
# outside the package tree, where only WARNING and above escape, unformatted, through
# logging's lastResort. See the note in render.py.
logger = logging.getLogger(f"usd_chunks.{__name__}")

_CONFORM_POLICIES = [
    "expandAperture",
    "cropAperture",
    "adjustApertureWidth",
    "adjustApertureHeight",
    "adjustPixelAspectRatio",
]

_STAGE_SETTINGS_METADATA = "renderSettingsPrimPath"

def _delegate_defs():
    """Renderer settings definitions by label."""
    return {d.label: d for d in render_defs.iter_render_settings_defs()}


#: Where the chunk remembers what it last sent the viewport, as (renderer id, mapping).
#: A delegate restarts its render on every setting handed to it whether or not the value
#: is new, so re-sending an unchanged one is not free -- it stalls the viewport.
_SENT = "_sent_viewport_settings"


def _loft_delegate_params(owner):
    """Give every installed renderer a group of its own settings.

    All of them at once, not one selected renderer: a RenderSettings prim is meant to
    carry several renderers' settings side by side, each in its own namespace and each
    delegate ignoring the rest. That is how Houdini shows Karma and RenderMan together,
    and it means nothing has to be switched here to match what the Render chunk is
    actually rendering with.

    Whether a delegate reads back what is authored for it is the delegate's business,
    not this chunk's -- hdCycles declares its settings but does not yet support the
    renderSettings Bprim that would deliver them, so its group edits scene description
    that its own delegate currently ignores.

    Affordable only because the params inside are built on first expand -- a group per
    renderer costs a header, not its settings.
    """
    renderer_params.loft(owner, render_defs.iter_render_settings_defs())


def _chunk_loaded(owner, notice, sender):
    """Put saved settings values back, building whichever groups hold them.

    Needed as its own hook because load suppresses reactive notices while restoring
    values, and because the params those values belong to are built lazily -- a setting
    restored before its group was ever expanded would otherwise have nowhere to land.
    """
    _chunk = owner.get_chunk() if hasattr(owner, "get_chunk") else owner
    if _chunk is None:
        return

    # Cook again if the values were not back yet. execute() restores them itself before
    # authoring, so on a load this finds nothing to do; only a cook that could not see the
    # chunk prim leaves them for here -- and wrote nothing for params that did not exist.
    if _restore_saved_settings(_chunk):
        _chunk.execute()


def _restore_saved_settings(node) -> bool:
    """Build the groups holding saved settings and put the saved values back. True if any
    value changed (renderer_params.restore_saved)."""
    return renderer_params.restore_saved(node, render_defs.iter_render_settings_defs())


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
    """Apply each renderer's API schema and author the settings that differ from their
    default (renderer_params.author)."""
    renderer_params.author(node, prim, render_defs.iter_render_settings_defs(), "Render Settings")


def _settings_for_renderer(node, renderer_id):
    """The current values of the settings the running delegate actually owns.

    Only that renderer's, because a delegate is handed every key it is sent and redraws
    for each one; the rest would be redraws for settings it discards.

    Only settings whose param exists, too. A param is built the first time its group is
    expanded, so one that does not exist has never been touched and the delegate is
    already sitting at the same fallback the param would have been born with. That
    bounds a push to what someone has actually looked at rather than to all 63 of
    Cycles'.

    Values that have gone back to their default are kept rather than dropped. Nothing
    resolves a fallback on the delegate's side the way USD does for an unauthored
    attribute -- a setting stays wherever it was last put -- so returning one to its
    default has to be said just as explicitly as moving it away.

    The key sent is attr_name, which for a delegate-sourced def is the descriptor key
    the renderer declared, since that is where discovery read it from.
    """
    definition = render_defs.def_for_plugin(renderer_id)
    if definition is None:
        return {}

    settings = {}
    for prop in definition.props:
        param = node.get_param(prop.name)
        if param is None or param.value is None:
            continue

        # A menu-backed enum holds a readable label; the delegate wants the number.
        value = render_defs.authored_value(prop, param.value)
        if value is None:
            continue
        settings[prop.attr_name] = value
    return settings


def _push_to_viewport(node):
    """Send what changed since last time to the viewport's render delegate.

    The delegate is the only thing most renderers listen to. Cycles, RPR, Aurora and
    OSPRay all decline Hydra's renderSettings prim, so the attributes this chunk authors
    reach them through nothing at all and the settings appear to do nothing. V-Ray and
    Arnold do read the prim, and for them this is the viewport agreeing with the file
    sooner rather than a different answer.

    Diffed rather than sent wholesale because each setting costs the viewport a restart,
    and execute() runs on every param drag. A renderer switch resets the record, since
    what the last delegate was told says nothing about what this one already has.
    """
    scene = chunk_registry.ChunkRegistry().scene
    if scene is None:
        return

    renderer_id = scene.get_viewport_renderer()
    if not renderer_id:
        return  # no viewport, so nothing to keep in step

    wanted = _settings_for_renderer(node, renderer_id)
    last_renderer, last_sent = getattr(node, _SENT, ("", {}))
    if last_renderer != renderer_id:
        last_sent = {}

    changed = {k: v for k, v in wanted.items() if k not in last_sent or last_sent[k] != v}
    setattr(node, _SENT, (renderer_id, dict(wanted)))

    if changed:
        scene.set_viewport_renderer_settings(changed)


def _viewport_settings_changed(owner, notice, sender):
    """Take renderer settings set in the host's own UI (usdview's Hydra Settings) into the
    params, so the chunk and the viewport's delegate never disagree.

    The other half of _push_to_viewport: there the params drive the delegate, here the
    delegate, changed behind this chunk's back, drives the params. Each value is also
    recorded as sent -- the delegate has it already -- so the cook its param change starts
    does not hand it straight back and restart the render for nothing. The group holding a
    setting is built if it was not yet, so the value has a param to land in.
    """
    node = owner.get_chunk() if hasattr(owner, "get_chunk") else owner
    if node is None:
        return
    apply_param = node.get_param("apply_to_viewport")
    if apply_param is None or not apply_param.value:
        return

    renderer_id, settings = notice.data
    definition = render_defs.def_for_plugin(renderer_id)
    if definition is None:
        return
    props = [prop for prop in definition.props if prop.attr_name in settings]
    if not props:
        return
    renderer_params.populate_groups_holding(node, {prop.name for prop in props})

    last_renderer, last_sent = getattr(node, _SENT, ("", {}))
    if last_renderer != renderer_id:
        last_sent = {}
    for prop in props:
        last_sent[prop.attr_name] = settings[prop.attr_name]
    setattr(node, _SENT, (renderer_id, last_sent))

    for prop in props:
        param = node.get_param(prop.name)
        value = render_defs.displayed_value(prop, settings[prop.attr_name])
        if param is not None and value is not None and not chunk_utils.values_match(param.value, value):
            param.value = value


def _token_list(value):
    """Comma-separated tokens, trimmed, with empties dropped."""
    return [token.strip() for token in (value or "").split(",") if token.strip()]


def main():
    chunk_api = chunk.Chunk("Render Settings")

    # A literal, not get_unique_path(stage, ...). A default computed from whatever the stage
    # holds at construction is a value wearing a default's clothes: the save path leaves
    # defaults out on the grounds that running main() again reproduces them, and running
    # main() again does not reproduce this one -- on reload no prim exists yet, so every
    # instance is handed the same path back. execute() does the uniquifying and records the
    # result as a value, which is what survives a save.
    prim_path = "/Render/settings"

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
            # The path a first Camera chunk takes (chunks/modelling/camera.py).
            default_value="/cameras/camera1",
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
        chunk.Checkbox("apply_to_viewport", "Apply to Viewport", default_value=True),
    ])

    _loft_delegate_params(chunk_api)
    chunk_api.add_callback(
        _chunk_loaded,
        notice_alias=chunk_notice.ChunkNoticeAlias.CHUNK_LOADED,
    )
    chunk_api.add_callback(
        _viewport_settings_changed,
        notice_alias=chunk_notice.ChunkNoticeAlias.VIEWPORT_RENDERER_SETTINGS_CHANGED,
        listen_globally=True,
    )

    return chunk_api


def execute(node):
    stage = node.get_stage()

    raw_path = node.get_param("primpath_location").value
    if not raw_path:
        return

    # A loaded chunk's renderer settings are params built lazily, so on the first cook
    # after a load they do not exist yet and nothing would author them. Built and filled
    # here, the first cook writes them -- otherwise CHUNK_LOADED restores them afterwards
    # and cooks the chunk, and everything after it, a second time. During a load a value
    # lands without notices, so this schedules no cook of its own.
    if chunk_notice._loading_stage:
        _restore_saved_settings(node)

    authored = None
    primpath_param = node.get_param("primpath_location")
    for target_path in chunk_utils.author_in_variant_contexts(stage, primpath_param, unique=True):
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
        if camera_targets := chunk_utils.target_paths(node.get_param("camera"))[:1]:
            settings.CreateCameraRel().SetTargets(camera_targets)

        if product_paths := chunk_utils.target_paths(node.get_param("products")):
            settings.CreateProductsRel().SetTargets(product_paths)

        authored = settings.GetPrim().GetPath().pathString

        _author_delegate_settings(node, settings.GetPrim())

    # Renderers find the settings prim through this stage metadatum rather than by
    # path convention. Outside the variant context and the chunk's edit target,
    # since stage metadata is only accepted on the root or session layer.
    _set_stage_settings_path(stage, authored if node.get_param("set_stage_settings").value else None)

    # Last, and outside the edit target: this writes nothing to any layer. It hands the
    # running delegate the same values that were just authored, because most delegates
    # never read them off the prim.
    if node.get_param("apply_to_viewport").value:
        _push_to_viewport(node)


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD_RENDER,
        icon="film",
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
          - Camera: the camera prim to render from, /cameras/camera1 unless a
            prim was selected when the chunk was made. Products may override it.
          - Products: the RenderProduct prims this render writes, as paths or glob
            patterns, naming the Render Product chunks' prim paths.
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
          - Apply to Viewport: hands these settings straight to the render delegate
            the viewport is drawing with, as well as authoring them. On by default,
            because for most renderers it is the only thing that has any effect:
            Cycles, RPR, Aurora and OSPRay all decline Hydra's renderSettings prim,
            so what is authored here reaches them through nothing at all. V-Ray and
            Arnold do read the prim, and for those two this only makes the viewport
            agree sooner. Only the running renderer's settings are sent, only the
            ones that changed, and only from groups that have been opened — a
            delegate restarts its render on every setting it is handed. It is the
            same channel as usdview's own Hydra Settings menu, and the two are kept
            in step: a setting changed in that menu (or its More... dialog) updates
            this chunk's param, and one changed here shows in the menu. The delegate
            side touches no layer and does not reach a batch render; the params
            themselves are authored and saved as usual.

        Every installed renderer also gets a group of its own settings alongside the
        standard ones -- all of them at once, not one selected renderer. A RenderSettings
        prim is meant to carry several renderers side by side, each in its own namespace
        and each delegate ignoring the rest, which is how Houdini shows Karma and
        RenderMan together. So nothing has to be switched here to match whatever the
        Render chunk renders with, and one renderer's settings are never lost by looking
        at another's.

        Those settings are grouped the way the renderer's own UI groups them and
        collapsed by default; V-Ray declares 258. The params inside a group are built the
        first time it is expanded, since building all of them up front took twenty
        seconds before a single one was visible.

        Whether a delegate reads any of that back is up to the delegate. Reading a
        RenderSettings prim means supporting Hydra's renderSettings Bprim, and of the
        renderers surveyed only V-Ray and Arnold do; Cycles, RPR, Aurora and OSPRay take
        settings solely through the delegate API, which is what Apply to Viewport is for.
        Authoring is still the portable half — it is what husk, Solaris and any future
        delegate read — but on its own it is not what makes a viewport change.

        Only values changed from their default are authored, as namespaced attributes on
        the RenderSettings prim (vray:..., ri:...) -- Hydra gathers those and hands each
        delegate the namespace it owns. A renderer's API schema is applied only once it
        actually has a setting here, so an installed but unused renderer leaves no trace.

        Nothing here is renderer-specific. A renderer appears because it declares its
        settings in a way USD can read -- V-Ray ships an applied API schema, RenderMan
        registers .args files into the shader registry, and Cycles, which ships neither,
        declares them to Hydra at runtime where loading the delegate is the only way to
        ask. So installing a renderer is all it takes for it to show up.

        Authoring them is standard either way, but consuming them is up to the delegate.
        A delegate reads a RenderSettings prim by supporting Hydra's renderSettings
        Bprim; hdCycles does not yet, so its settings are authored correctly and read by
        nothing -- changing Sample Count here leaves the render identical. They are
        still worth writing: the scene description is the portable part and outlives the
        delegate that cannot read it. Until hdCycles catches up, set Cycles' settings in
        usdview's own Hydra Settings, which drives the delegate directly.

        Everything authored is standard UsdRender scene description, readable by
        any renderer whether or not usd_chunks is installed.
        """,
    )
