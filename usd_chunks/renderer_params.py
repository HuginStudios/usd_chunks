"""Renderer-specific settings as param groups on a chunk, built lazily and authored sparsely.

Shared by the chunks that show them: Render Settings carries every renderer's
RenderSettings schema (render_defs.iter_render_settings_defs), Camera every renderer's
Camera schema (render_defs.iter_camera_defs). Each passes in its own definitions; what
happens to them -- a collapsed group per renderer, params built on first expand, saved
values put back on load, only changed values authored -- is the same for both.

Nothing here imports Qt.

``` py
renderer_params.loft(chunk_api, render_defs.iter_camera_defs())
...
renderer_params.author(node, prim, render_defs.iter_camera_defs(), "Camera")
```
"""

from __future__ import annotations

import logging

from pxr import Sdf

from usd_chunks import chunk, chunk_scheduler, chunk_stage, chunk_utils, render_defs

logger = logging.getLogger(__name__)

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
        chunk_stage.get_chunk_prim_path(owner),
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

    # Presentation only: every param arrives at the default the last cook already authored
    # as nothing, so cooking for them would only restart the viewport render.
    with chunk_scheduler.presentation_changes():
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


def loft(owner, definitions):
    """Give every renderer in definitions a group of its own settings on owner.

    All of them at once, not one selected renderer: a prim is meant to carry several
    renderers' settings side by side, each in its own namespace and each delegate
    ignoring the rest. That is how Houdini shows Karma and RenderMan together, and it
    means nothing has to be switched here to match what is actually rendering.

    Affordable only because the params inside are built on first expand -- a group per
    renderer costs a header, not its settings.

    Args:
        owner: The chunk (or group) the renderer groups go on.
        definitions: RenderSettingsDefs, one group each.
    """
    for definition in definitions:
        _loft_group(owner, definition.label, render_defs.group_props(definition.props), set())


def populate_groups_holding(node, leaves):
    """Build the params of any group still holding one of these settings.

    Args:
        node: The chunk to search.
        leaves: Param names (RenderSettingProp.name) that must have params.
    """
    for child in node.get_my_params():
        if not hasattr(child, "get_my_params"):
            continue
        pending = getattr(child, _PENDING, None)
        if pending:
            if any(prop.name in leaves for prop in pending):
                _populate(child)
        else:
            populate_groups_holding(child, leaves)


def restore_saved(node, definitions) -> bool:
    """Build the groups holding saved settings and put the saved values back.

    By leaf name, not by the stored path. A group's name is uniquified when the chunk
    already has one by that name -- V-Ray has both a top-level Settings and a
    viewport/Settings, so one of them is stored as "Settings1" -- while get_param resolves
    paths by the original name, so the stored path can never be looked up. Leaf names
    carry their renderer's namespace (vray_..., ri_...) and are unique on the chunk,
    which is what makes them safe to resolve on their own.

    Args:
        node: The chunk whose saved values to restore.
        definitions: The RenderSettingsDefs its groups were lofted from.

    Returns:
        True if any value changed.
    """
    # Renderer settings only: those are the params built lazily. Every other input is
    # restored by the load itself -- and one holding an expression (a camera wired as
    # @Camera.primpath_location@) never equals its saved text, so compared here it read as
    # a change on every load, and was cooked again for it.
    lazy = {prop.name for definition in definitions for prop in definition.props}
    entries = [entry for entry in _authored_settings(node) if entry[1] in lazy]
    if not entries:
        return False

    populate_groups_holding(node, {leaf for _path, leaf, _value in entries})

    # values_match, not !=: a float setting reads back off its float attribute as its
    # nearest float32, so compared exactly it never matched and every load cooked again.
    restored = False
    for _param_path, leaf, value in entries:
        param = node.get_param(leaf)
        if param is not None and value is not None and not chunk_utils.values_match(param.value, value):
            param.value = value
            restored = True
    return restored


def author(node, prim, definitions, label=""):
    """Author the settings that differ from their default, applying each renderer's schema.

    Only non-default values are written. The schema already carries every fallback, so
    authoring all 258 would bury the handful that were actually changed and make the
    layer unreadable in a diff -- the same reason chunk params only create an inputs:
    attribute once their value leaves the default.

    Args:
        node: The chunk whose params hold the values.
        prim: The prim to author onto.
        definitions: The RenderSettingsDefs to author.
        label: The chunk's name, for the debug message when a value cannot be authored.
    """
    for definition in definitions:
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
                logger.debug("%s: could not author %s: %s", label, prop.attr_name, exc)

        # Only claim the schema for a renderer that actually has settings here, so an
        # installed-but-unused renderer leaves no trace on the prim.
        if touched and definition.schema_name:
            # AddAppliedSchema rather than ApplyAPI: the schema's library is not loaded
            # until something uses it, so the TfType ApplyAPI wants is not resolvable.
            prim.AddAppliedSchema(definition.schema_name)
