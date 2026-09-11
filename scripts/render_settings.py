"""Render Settings chunk — creates a UsdRender.Settings prim: the resolution,
camera and products that describe one render.

Requires: pxr (OpenUSD), usd_chunks.
"""

from __future__ import annotations

import logging

from pxr import Gf, Sdf, Usd, UsdRender

from usd_chunks import chunk, chunk_notice, chunk_registry, chunk_utils, render_defs
from usd_chunks.plugins import chunk_usd

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


#: Where a group parks the settings it has not built params for yet.
_PENDING = "_pending_settings"

#: Where the chunk remembers what it last sent the viewport, as (renderer id, mapping).
#: A delegate restarts its render on every setting handed to it whether or not the value
#: is new, so re-sending an unchanged one is not free -- it stalls the viewport.
_SENT = "_sent_viewport_settings"

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
    for definition in render_defs.iter_render_settings_defs():
        _loft_group(owner, definition.label, render_defs.group_props(definition.props), set())


def _populate_groups_holding(node, leaves):
    """Build the params of any group still holding one of these settings."""
    for child in node.get_my_params():
        if not hasattr(child, "get_my_params"):
            continue
        pending = getattr(child, _PENDING, None)
        if pending:
            if any(prop.name in leaves for prop in pending):
                _populate(child)
        else:
            _populate_groups_holding(child, leaves)


def _chunk_loaded(owner, notice, sender):
    """Put saved settings values back, building whichever groups hold them.

    Needed as its own hook because load suppresses reactive notices while restoring
    values, and because the params those values belong to are built lazily -- a setting
    restored before its group was ever expanded would otherwise have nowhere to land.
    """
    _chunk = owner.get_chunk() if hasattr(owner, "get_chunk") else owner
    if _chunk is None:
        return

    entries = _authored_settings(_chunk)
    if not entries:
        return

    _populate_groups_holding(_chunk, {leaf for _path, leaf, _value in entries})

    # By leaf name, not by the stored path. A group's name is uniquified when the chunk
    # already has one by that name -- V-Ray has both a top-level Settings and an
    # ipr/Settings, so one of them is stored as "Settings1" -- while get_param resolves
    # paths by the original name, so the stored path can never be looked up. Leaf names
    # carry their renderer's namespace (vray_..., ri_...) and are unique on the chunk,
    # which is what makes them safe to resolve on their own.
    restored = False
    for _param_path, leaf, value in entries:
        param = _chunk.get_param(leaf)
        if param is not None and value is not None and param.value != value:
            param.value = value
            restored = True

    # Cook again now the values are back. The chunk has already executed by this point,
    # and it authors a renderer setting by reading the param that holds it -- params
    # that did not exist yet, so that pass wrote nothing for them.
    if restored:
        _chunk.execute()


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
    for definition in render_defs.iter_render_settings_defs():
        touched = False

        for prop in definition.props:
            param = node.get_param(prop.name)
            if param is None:
                continue  # its group has never been opened, so it cannot have changed

            value = param.value
            if value is None or value == prop.default:
                continue

            # Translated after the default comparison, not before: the param and the
            # default are both labels for a menu-backed enum, and comparing a number
            # against a label would author every one of them on every cook.
            value = render_defs.authored_value(prop, value)
            if value is None:
                continue

            value_type = Sdf.ValueTypeNames.Find(prop.sdf_type)
            if not value_type:
                continue

            attr = prim.CreateAttribute(prop.attr_name, value_type)
            try:
                attr.Set(value)
                touched = True
            except Exception as exc:  # noqa: BLE001 - a param value this attr cannot hold
                logger.debug("Render Settings: could not author %s: %s", prop.attr_name, exc)

        # Only claim the schema for a renderer that actually has settings here, so an
        # installed-but-unused renderer leaves no trace on the prim.
        if touched and definition.schema_name:
            # AddAppliedSchema rather than ApplyAPI: the schema's library is not loaded
            # until something uses it, so the TfType ApplyAPI wants is not resolvable.
            prim.AddAppliedSchema(definition.schema_name)


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
        # Recorded as a value, not left as a local. This is the only thing telling this
        # chunk apart from another of the same kind, and a saved scene carries chunk-prim
        # values -- without it two cameras reopened both claiming /cameras/camera1 and
        # authored over one prim. Guarded so a path that did not move is not written back
        # on every cook.
        if raw_path != node.get_param("primpath_location").value:
            node.get_param("primpath_location").value = raw_path

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
            delegate restarts its render on every setting it is handed. Note this is
            the same channel as usdview's own Hydra Settings menu, so the two
            overwrite each other, and that it touches no layer: nothing about it is
            saved, and it does not reach a batch render.

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
