"""Attribute animation (keyframes) for chunk params.

Design record: CONTEXT.md's "Attribute Animation (keyframes)" section (grill-me session).

Keyframes are native USD time-samples authored directly onto a param's existing
``_param_attr_path`` storage attribute (and mirrored onto any ``_driven_attr_paths``) —
no separate keyframe data model. Once authored, USD's own time-varying resolution
handles interpolation for anything that reads the attribute directly (viewport,
renderer) with zero help from this module.

What this module *does* own:
- Deciding which params are animatable at all (``is_animatable_param``) and which are
  currently expression-driven and therefore off-limits (``is_expression_driven``).
- Authoring a single keyframe across a param's storage + driven attrs (``keyframe_param``,
  ``keyframe_chunk``).
- Tracking which live param objects currently carry keyframes (``ChunkAnimationManager``),
  so a frame-change tick only touches those params instead of rescanning every param in
  the scene.
- Reacting to a frame change by refreshing each tracked param's displayed value from the
  attr it's keyed on (``ChunkAnimationManager.on_frame_changed``), reusing the normal
  ``param.value = ...`` assignment path (same DAG-dirty/notice propagation a user edit
  would trigger) guarded by ``_suppress_storage_write`` so the pulled value isn't
  redundantly re-authored back onto the attr it was just read from.
"""

from __future__ import annotations

from typing import Iterable, List, Set

from pxr import Sdf, Usd

from usd_chunks import chunk, chunk_core, chunk_notice, chunk_registry, chunk_utils

ANIMATABLE_PARAM_TYPES = (chunk.Slider, chunk.Spinbox, chunk.Checkbox, chunk_core.ChunkVectorBase)


def is_animatable_param(param) -> bool:
    """Whether K (or auto-key) should ever touch this param at all.

    Menu (token/string), LineEdit (arbitrary string incl. expression/prim-path refs) and
    Button (no stored value) are deliberately excluded — see CONTEXT.md decision.
    """
    return isinstance(param, ANIMATABLE_PARAM_TYPES)


def is_expression_driven(param) -> bool:
    """Whether param's raw_value is currently a live @Node.attr@ expression.

    An expression-driven param already has an upstream chunk deciding its value every
    cook (see ChunkUsdAttributeSupport.data_changed's connection-authoring branch) —
    keyframing on top would create two competing authorities over the same attribute.
    """
    raw = getattr(param, "raw_value", None)
    return isinstance(raw, str) and chunk_utils.has_expression(raw)


def _get_stage() -> Usd.Stage | None:
    registry = chunk_registry.ChunkRegistry()
    if not registry.initialized:
        return None
    return registry.scene.get_stage()


def keyframe_param(param, frame: float, force_author=None) -> bool:
    """Author a time sample at `frame` on param's storage attr and every driven attr,
    using the param's currently-displayed value.

    A param still at its default value has no live USD attribute yet — chunk_usd.py's
    _sync_param_input only creates one on first non-default change (or via force=True,
    the same mechanism "Loft up" already relies on for the identical reason). This module
    stays host-agnostic and can't import plugins.chunk_usd directly, so a caller that can
    (e.g. ChunkGraphView's K handler) may pass force_author=callable(param) -> None to
    force-create the attr before we give up on a param with no _param_attr_path.

    Returns False (no-op) if the param isn't animatable, is expression-driven, or still
    has no live USD attribute after force_author (or none was given).
    """
    if not is_animatable_param(param) or is_expression_driven(param):
        return False

    stage = _get_stage()
    attr_path = getattr(param, "_param_attr_path", None)
    if not attr_path and force_author is not None:
        force_author(param)
        attr_path = getattr(param, "_param_attr_path", None)
    if not stage or not attr_path:
        return False

    usd_attr = stage.GetAttributeAtPath(Sdf.Path(attr_path))
    if not usd_attr:
        return False

    val = param.value
    if val is None:
        return False

    time_code = Usd.TimeCode(frame)

    # Deliberately NOT wrapped in Usd.EditContext(stage, param.get_edit_context()) -- that
    # layer is the chunk's own per-chunk edit-target layer, which for a "procedural" chunk
    # (one with a reactive execute(node) hook, e.g. scripts/transform.py) is the SAME layer
    # chunk_notice.py's callback dispatch (handle_callback/data_changed's callback path,
    # "with Usd.EditContext(stage, ctx): meth(*args)") makes the stage's edit target for the
    # duration of that hook's own run. transform.py's execute() starts with
    # chunk_layer.Clear() on every reactive re-run (e.g. triggered by the frame-change hook
    # pushing a new value into any of its params) -- writing keyframes into that same layer
    # means the very next execute() wipes them. A plain Set() with no explicit context lands
    # on the stage's ambient edit target instead, exactly like a normal (non-animated) param
    # edit already does (ChunkUsdAttributeSupport.data_changed's storage-attr write has no
    # explicit EditContext either) -- stable, not cleared by any chunk's own recompute.
    chunk_notice._usd_write_active = True
    try:
        usd_attr.Set(val, time_code)
        for driven_path in list(getattr(param, "_driven_attr_paths", None) or []):
            driven_attr = stage.GetAttributeAtPath(Sdf.Path(driven_path))
            if driven_attr:
                driven_attr.Set(val, time_code)
    finally:
        chunk_notice._usd_write_active = False

    ChunkAnimationManager().track(param)
    return True


def iter_animatable_params(chunk_obj) -> Iterable:
    """Walk chunk_obj's own param hierarchy, yielding only animatable leaf params.

    Uses iter_hierarchy() (chunk_core.py's ChunkContainerBase._iter_hierarchy), which
    already treats a ChunkVectorBase as a single leaf node rather than descending into
    its per-axis scalar children (ChunkVectorBase doesn't subclass ChunkGroupBase, the
    only type _iter_hierarchy recurses into) — so a Vector param is yielded here once,
    as itself, matching K's intent to keyframe the vector's own storage attr as one unit.
    """
    for _, node in chunk_obj.iter_hierarchy():
        if is_animatable_param(node):
            yield node


_DEFAULT_RANGE_START = 1.0
_DEFAULT_RANGE_END = 100.0


def ensure_frame_range_covers(frame: float) -> None:
    """Grow the host's playback range so `frame` is scrubbable.

    usdview only enables its frame slider/play button once a real (>1 frame) start/end
    range exists (RootDataModel._setPlaybackAvailability, gated on len(timeSamples) > 1,
    itself derived purely from the start/end frame fields — not from whether any
    attribute actually has time samples). A brand-new scene has no such range at all
    (get_start_frame/get_end_frame return None -- usdview's own realStartTimeCode/
    realEndTimeCode stay None until the stage has an authored startTimeCode/endTimeCode
    or a keyframe forces one), so a keyframe authored outside [start, end] would be
    unreachable by scrubbing or by typing into usdview's own frame field. Called after
    every successful K / per-param keyframe.

    When there's no range at all yet, establishes a generous default window (not just the
    single frame just keyed) so there's actually something to scrub across immediately,
    rather than requiring a second keyframe at a different frame first.

    Best-effort: SceneSender.get_start_frame/get_end_frame/set_start_frame/set_end_frame
    are host-provided (chunk_core.py's base raises NotImplementedError) and this feature
    is usdview-only for v1 (CONTEXT.md), so a host without them is left untouched.
    """
    registry = chunk_registry.ChunkRegistry()
    if not registry.initialized:
        return
    scene = registry.scene
    # Defensive: SceneSender.get_current_frame() is documented to return a plain float, but
    # UsdviewScene's own implementation returned a raw Usd.TimeCode until this was caught live
    # (Stage.SetStartTimeCode/SetEndTimeCode reject a TimeCode outright, unlike most USD calls
    # which tolerate one). Normalizing here too means a future caller making the same mistake
    # can't reintroduce this exact crash.
    frame = frame.GetValue() if hasattr(frame, "GetValue") else float(frame)
    try:
        start = scene.get_start_frame()
        end = scene.get_end_frame()
        if start is None or end is None:
            scene.set_start_frame(min(_DEFAULT_RANGE_START, frame))
            scene.set_end_frame(max(_DEFAULT_RANGE_END, frame))
            return
        if frame < start:
            scene.set_start_frame(frame)
        if frame > end:
            scene.set_end_frame(frame)
    except NotImplementedError:
        pass


def is_param_animated(param) -> bool:
    """Whether param currently carries >=1 time sample on its storage attr.

    Computed fresh from live USD attribute state (not the tracked set — this needs to
    stay correct even if time samples were removed by some path other than
    clear_keyframes_for_param, e.g. a hand-edited USD file). Drives both the graph node's
    animated-glyph indicator and the Params-panel per-param "animated" style.
    """
    stage = _get_stage()
    if not stage:
        return False
    attr_path = getattr(param, "_param_attr_path", None)
    if not attr_path:
        return False
    usd_attr = stage.GetAttributeAtPath(Sdf.Path(attr_path))
    return bool(usd_attr and usd_attr.GetNumTimeSamples() > 0)


def chunk_has_animated_params(chunk_obj) -> bool:
    """Whether chunk_obj has >=1 animatable param that currently carries time samples.

    See is_param_animated's docstring re: why this is computed fresh rather than from the
    tracked set. Cheap: only iterates one chunk's own params, not the scene.
    """
    return any(is_param_animated(param) for param in iter_animatable_params(chunk_obj))


def bake_procedural_output(chunk_obj) -> None:
    """Author real per-frame time samples for a procedural chunk's computed output, by
    calling its module-level `bake(node, start_frame, end_frame)` reserved hook if the
    module defines one (see scripts/transform.py's bake() for the reference
    implementation). See CONTEXT.md's "Baking procedural chunk output" section for why
    this matters -- e.g. the RBD solver's mocap-collider path needs real
    GetNumTimeSamples() > 1 on the collider's own xformOps, not just a live per-frame
    recompute that only ever shows up in this interactive session.

    Deliberately does NOT scrub the timeline / re-trigger the reactive execute() cascade
    to force recomputation (an earlier version of this function did, and it had real
    problems: it re-triggered *every* animated chunk's own reactive hooks on every frame
    of the scrub -- not just this one -- which is exactly the kind of thing that makes an
    expensive reactive chunk elsewhere in the graph (e.g. an "auto-simulate" RBD solver)
    re-run on every single frame of the bake, and it temporarily moved the user's own
    playhead. bake() is expected to be a pure, self-contained function that resolves each
    animated input directly from its own already-keyframed storage attribute at each
    frame and writes the result with an explicit Usd.TimeCode -- no dependency on
    execute()'s own per-run Clear() idiom, no transient "currently baking" flag needed.

    Called automatically after every keyframe / clear-keyframe action (K and the
    per-param menu). No-op if chunk_obj's module has no bake() hook at all, or the host
    has no current-frame API (chunk_core.py's SceneSender base raises NotImplementedError;
    this feature is usdview-only for v1, per CONTEXT.md). Falls back to one direct
    chunk_obj.execute() call (settling the output to a static value via the module's
    normal, non-animated code path) if nothing is animated anymore -- e.g. right after
    Clear Keyframes removes the last remaining sample.
    """
    registry = chunk_registry.ChunkRegistry()
    if not registry.initialized:
        return
    scene = registry.scene

    if not chunk_has_animated_params(chunk_obj):
        try:
            chunk_obj.execute()
        except NotImplementedError:
            pass
        return

    module = getattr(chunk_obj, "_module", None)
    bake_fn = getattr(module, "bake", None)
    if not callable(bake_fn):
        return

    try:
        start = scene.get_start_frame()
        end = scene.get_end_frame()
    except NotImplementedError:
        return
    if start is None or end is None:
        return

    bake_fn(chunk_obj, start, end)


def keyframe_chunk(chunk_obj, frame: float, force_author=None) -> List:
    """Keyframe every animatable, non-expression-driven param on chunk_obj at `frame`.

    force_author, if given, is passed through to keyframe_param for each param still at
    its default (no USD attr yet) -- see keyframe_param's docstring.

    Returns the list of params that were actually keyframed (skips get silently
    dropped, matching K's design as "keyframe everything animatable, skip the rest").
    """
    keyframed = []
    for param in iter_animatable_params(chunk_obj):
        if keyframe_param(param, frame, force_author=force_author):
            keyframed.append(param)
    return keyframed


def _clear_time_samples(attr: Usd.Attribute, resolved) -> None:
    for t in list(attr.GetTimeSamples()):
        attr.ClearAtTime(Usd.TimeCode(t))
    if resolved is not None:
        attr.Set(resolved)


def clear_keyframes_for_param(param, frame: float) -> bool:
    """Remove every time sample from param's storage attr and its driven attrs, re-authoring
    the value currently resolved at `frame` as the new static default, and untrack param.

    Returns False (no-op, though still untracks defensively) if param has no time samples
    to clear.
    """
    stage = _get_stage()
    attr_path = getattr(param, "_param_attr_path", None)
    if not stage or not attr_path:
        return False

    usd_attr = stage.GetAttributeAtPath(Sdf.Path(attr_path))
    if not usd_attr or usd_attr.GetNumTimeSamples() == 0:
        ChunkAnimationManager().untrack(param)
        return False

    time_code = Usd.TimeCode(frame)

    # No explicit Usd.EditContext here either -- see keyframe_param's comment. The samples
    # being cleared were authored on the stage's ambient edit target (not
    # param.get_edit_context()'s per-chunk layer), so clearing must target the same place.
    chunk_notice._usd_write_active = True
    try:
        _clear_time_samples(usd_attr, usd_attr.Get(time_code))
        for driven_path in list(getattr(param, "_driven_attr_paths", None) or []):
            driven_attr = stage.GetAttributeAtPath(Sdf.Path(driven_path))
            if driven_attr:
                resolved = driven_attr.Get(time_code)
                _clear_time_samples(driven_attr, resolved)
    finally:
        chunk_notice._usd_write_active = False

    ChunkAnimationManager().untrack(param)
    return True


def clear_keyframes_for_chunk(chunk_obj, frame: float) -> List:
    """Clear keyframes from every animatable param on chunk_obj. Returns the params that
    actually had keyframes cleared (params with no time samples are silently skipped, per
    CONTEXT.md: "clearing a chunk with no animated params is a no-op")."""
    cleared = []
    for param in iter_animatable_params(chunk_obj):
        if clear_keyframes_for_param(param, frame):
            cleared.append(param)
    return cleared


class ChunkAnimationManager(metaclass=chunk_notice.RegistrySingleton):
    """Tracks which live param objects currently carry keyframes.

    A frame-change tick only refreshes params in this set — O(animated params), not
    O(every param in the scene). track()/untrack() are the only mutators; keyframe_param
    tracks on first successful author, clear-keyframes (a later slice) untracks.
    """

    def __init__(self):
        self._tracked: Set = set()

    def track(self, param) -> None:
        self._tracked.add(param)

    def untrack(self, param) -> None:
        self._tracked.discard(param)

    def is_tracked(self, param) -> bool:
        return param in self._tracked

    def tracked_params(self) -> List:
        return list(self._tracked)

    def clear(self) -> None:
        self._tracked.clear()

    def on_frame_changed(self, frame: float) -> None:
        """Refresh every tracked param's displayed value from its attr at `frame`.

        Reuses the normal param.value = ... assignment path — the same DAG-dirty/notice
        propagation a user edit triggers — so widgets refresh and downstream chunks
        recompute exactly like they would for a manual edit. _suppress_storage_write
        prevents that assignment from writing the just-read value back onto the attr.
        """
        stage = _get_stage()
        if not stage:
            return

        time_code = Usd.TimeCode(frame)
        stale = []
        for param in list(self._tracked):
            attr_path = getattr(param, "_param_attr_path", None)
            if not attr_path:
                stale.append(param)
                continue
            usd_attr = stage.GetAttributeAtPath(Sdf.Path(attr_path))
            if not usd_attr or usd_attr.GetNumTimeSamples() == 0:
                stale.append(param)
                continue

            new_val = usd_attr.Get(time_code)
            if new_val is None:
                continue

            param._suppress_storage_write = True
            try:
                param.value = new_val
            finally:
                param._suppress_storage_write = False

        for param in stale:
            self._tracked.discard(param)
