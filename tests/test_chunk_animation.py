from __future__ import annotations

import pytest
from pxr import Sdf, Usd

from plugins.chunk_usd import _author_prim, _CHUNKS_ROOT, _INPUTS_PREFIX
from usd_chunks import chunk, chunk_animation, chunk_registry
from usd_chunks.chunk_core import ChunkConfig, SceneSender


class _DummyScene(SceneSender):
    """Minimal scene stub whose get_stage()/get_current_frame() are settable per test,
    so chunk_animation's stage lookups (chunk_registry.ChunkRegistry().scene.get_stage())
    resolve to whatever real in-memory stage a given test builds."""

    def __init__(self, stage=None, current_frame: float = 0.0, start_frame: float = 0.0, end_frame: float = 0.0):
        super().__init__()
        self._stage = stage
        self._current_frame = current_frame
        self._start_frame = start_frame
        self._end_frame = end_frame

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []

    def get_current_frame(self) -> float:
        return self._current_frame

    def get_start_frame(self) -> float:
        return self._start_frame

    def get_end_frame(self) -> float:
        return self._end_frame

    def set_start_frame(self, frame: float) -> None:
        self._start_frame = frame

    def set_end_frame(self, frame: float) -> None:
        self._end_frame = frame


chunk_registry.ChunkRegistry().initialize(_DummyScene(), "pytest")


@pytest.fixture(autouse=True)
def reset_animation_manager():
    yield
    chunk_animation.ChunkAnimationManager().clear()


def _chunk_with_module(name: str, module: str = "prim") -> chunk.Chunk:
    c = chunk.Chunk(name)
    c._config = ChunkConfig(module=module)
    return c


def _bind_param_to_stage(monkeypatch, param, value) -> tuple[Usd.Stage, Usd.Attribute, _DummyScene]:
    """Build a real in-memory stage with a chunk prim, give `param` a real, live
    _param_attr_path bound to that prim's storage attribute (mirroring _author_prim +
    _connect_param_attr, the same pattern test_chunk_usd.py's scalar-roundtrip tests use),
    and point the registry's scene at that stage for the duration of the test.

    Returns the scene too -- once a param is animated, a plain `param.value = x` assignment
    auto-keys at whatever get_current_frame() reports (slice 02), so tests that assign a
    value between two explicit keyframe_param() calls need to control that frame."""
    param.value = value
    c = _chunk_with_module("n")
    c.add_param(param)

    stage = Usd.Stage.CreateInMemory()
    _author_prim(stage, c)

    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/n")
    attr = prim.GetAttribute(f"{_INPUTS_PREFIX}{param.name}")
    assert attr.IsValid()
    param._connect_param_attr(attr)

    # get_edit_context() (used by both chunk_animation.keyframe_param and chunk_core.py's
    # own _write_driven_attrs, called on every plain edit once animated) returns None unless
    # ChunkLayerManager is initialized -- without this, auto-keyed driven-attr mirroring on a
    # plain edit would silently no-op (conftest.py's reset_chunk_layer_manager fixture cleans
    # this up between tests).
    chunk_registry.ChunkLayerManager().initialize(stage)

    scene = _DummyScene(stage)
    # param.get_chunk() (used by get_edit_context()) caches its owning chunk as a *weakref* --
    # keep a strong reference alive on the scene for the caller's lifetime, since `c` would
    # otherwise be garbage-collected the moment this helper returns.
    scene._chunk = c
    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "scene", scene)
    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "initialized", True)

    # In real usdview usage the stage's edit target is already the chunk's own layer for the
    # whole duration of editing that chunk (an outer mechanism this test doesn't reproduce) --
    # without this, data_changed()'s plain-edit write (chunk_core.py, no explicit EditContext)
    # would land in the stage's default root-layer edit target while keyframe_param's write
    # (explicit EditContext onto param.get_edit_context()) lands in the chunk's anon layer, and
    # composition would keep resolving to whichever layer is stronger regardless of which one
    # was written to most recently.
    if edit_layer := param.get_edit_context():
        stage.SetEditTarget(Usd.EditTarget(edit_layer))

    return stage, attr, scene


# ---------------------------------------------------------------------------
# is_animatable_param / is_expression_driven
# ---------------------------------------------------------------------------

def test_animatable_types_are_slider_spinbox_checkbox_vector() -> None:
    assert chunk_animation.is_animatable_param(chunk.Slider("s"))
    assert chunk_animation.is_animatable_param(chunk.Spinbox("sp"))
    assert chunk_animation.is_animatable_param(chunk.Checkbox("c"))
    assert chunk_animation.is_animatable_param(chunk.Vector("v", param=chunk.Slider(), length=3))


def test_menu_lineedit_button_are_not_animatable() -> None:
    assert not chunk_animation.is_animatable_param(chunk.Menu("m"))
    assert not chunk_animation.is_animatable_param(chunk.LineEdit("l"))
    assert not chunk_animation.is_animatable_param(chunk.Button("b"))


def test_is_expression_driven_detects_live_expression() -> None:
    slider = chunk.Slider("s")
    slider.value = 1.0
    assert not chunk_animation.is_expression_driven(slider)

    slider.value = "@OtherNode.otherParam@"
    assert chunk_animation.is_expression_driven(slider)


# ---------------------------------------------------------------------------
# keyframe_param
# ---------------------------------------------------------------------------

def test_keyframe_param_authors_time_sample_on_storage_and_driven_attrs(monkeypatch) -> None:
    slider = chunk.Slider("myParam")
    stage, attr, scene = _bind_param_to_stage(monkeypatch, slider, 1.0)

    # Simulate a real prim attribute this param drives (e.g. xformOp:translate), the way
    # a Transform chunk's connection-authoring machinery would populate _driven_attr_paths.
    driven_prim = stage.DefinePrim("/driven", "Xform")
    driven_attr = driven_prim.CreateAttribute("myDriven", Sdf.ValueTypeNames.Float)
    slider._driven_attr_paths.append(str(driven_attr.GetPath()))

    assert chunk_animation.keyframe_param(slider, 1.0) is True
    # Once animated, this plain assignment auto-keys at get_current_frame() (slice 02) --
    # match it to the frame the next keyframe_param() call targets so they land on the
    # same sample instead of adding an extra one.
    scene._current_frame = 24.0
    slider.value = 24.0
    assert chunk_animation.keyframe_param(slider, 24.0) is True

    assert attr.GetNumTimeSamples() == 2
    assert attr.Get(Usd.TimeCode(1)) == pytest.approx(1.0)
    assert attr.Get(Usd.TimeCode(24)) == pytest.approx(24.0)
    mid = attr.Get(Usd.TimeCode(12))
    assert 1.0 < mid < 24.0

    assert driven_attr.GetNumTimeSamples() == 2
    assert driven_attr.Get(Usd.TimeCode(1)) == pytest.approx(1.0)
    assert driven_attr.Get(Usd.TimeCode(24)) == pytest.approx(24.0)
    driven_mid = driven_attr.Get(Usd.TimeCode(12))
    assert driven_mid == pytest.approx(mid)


def test_keyframe_param_survives_the_owning_chunks_own_layer_being_cleared(monkeypatch) -> None:
    """Regression test for the real live-usdview bug: a "procedural" chunk with a reactive
    execute(node) hook (e.g. scripts/transform.py) runs that hook with the stage's edit
    target set to chunk.get_edit_context()'s own per-chunk layer (chunk_notice.py's
    handle_callback: "with Usd.EditContext(stage, ctx): meth(*args)"), and execute() itself
    unconditionally does chunk_layer.Clear() = stage.GetEditTarget().GetLayer().Clear() on
    every re-run. Frame-change scrubbing re-triggers that reactive hook (via
    ChunkAnimationManager.on_frame_changed pushing a new value into a tracked param) --
    if keyframe_param wrote into param.get_edit_context()'s layer (same layer!), the very
    next execute() would wipe the keyframe. keyframe_param must write to the stage's
    *ambient* edit target instead (matching how a normal, non-animated param edit already
    behaves) precisely so it survives that layer being cleared."""
    slider = chunk.Slider("myParam")
    stage, attr, scene = _bind_param_to_stage(monkeypatch, slider, 1.0)

    # Simulate the chunk's own per-chunk "procedural" layer -- what get_edit_context() would
    # return for a procedural chunk, and what execute() would Clear() on every re-run. Must
    # be a *different* layer from the stage's actual ambient edit target for this test to
    # prove anything.
    procedural_layer = Sdf.Layer.CreateAnonymous("fake-chunk-execute-layer")
    monkeypatch.setattr(type(slider), "get_edit_context", lambda self: procedural_layer, raising=False)

    assert chunk_animation.keyframe_param(slider, 1.0) is True
    assert attr.GetNumTimeSamples() == 1

    # Simulate the reactive execute() hook re-running and clearing its own layer -- the bug
    # scenario is scrubbing/any param edit re-triggering this while animated.
    procedural_layer.Clear()

    assert attr.GetNumTimeSamples() == 1, "keyframe was wiped by the chunk's own layer clearing"
    assert attr.Get(Usd.TimeCode(1)) == pytest.approx(1.0)

    assert chunk_animation.ChunkAnimationManager().is_tracked(slider)


def test_keyframe_param_skips_expression_driven_param(monkeypatch) -> None:
    slider = chunk.Slider("myParam")
    stage, attr, scene = _bind_param_to_stage(monkeypatch, slider, 1.0)

    slider.value = "@OtherNode.otherParam@"
    assert chunk_animation.keyframe_param(slider, 1.0) is False
    assert attr.GetNumTimeSamples() == 0
    assert not chunk_animation.ChunkAnimationManager().is_tracked(slider)


def test_keyframe_param_skips_non_animatable_param(monkeypatch) -> None:
    menu = chunk.Menu("myMenu")
    menu.menu_items = ["a", "b"]
    stage, attr, scene = _bind_param_to_stage(monkeypatch, menu, "a")

    assert chunk_animation.keyframe_param(menu, 1.0) is False
    assert attr.GetNumTimeSamples() == 0


def test_keyframe_param_without_attr_and_no_force_author_is_noop() -> None:
    """A param still at its default has no _param_attr_path at all (chunk_usd.py's
    _sync_param_input only creates one on first non-default change) -- without a
    force_author hook, keyframe_param must stay a no-op rather than raise."""
    slider = chunk.Slider("tx", default_value=0.0)
    assert slider._param_attr_path is None
    assert chunk_animation.keyframe_param(slider, 1.0) is False


def test_keyframe_param_calls_force_author_when_attr_missing() -> None:
    """force_author is only invoked when there's no attr yet, and only once."""
    slider = chunk.Slider("tx", default_value=0.0)
    calls = []

    def fake_force_author(param):
        calls.append(param)
        # Doesn't actually author anything -- keyframe_param must still report failure.

    assert chunk_animation.keyframe_param(slider, 1.0, force_author=fake_force_author) is False
    assert calls == [slider]


# ---------------------------------------------------------------------------
# keyframe_chunk
# ---------------------------------------------------------------------------

def test_keyframe_chunk_only_keyframes_animatable_params(monkeypatch) -> None:
    stage = Usd.Stage.CreateInMemory()

    c = _chunk_with_module("n")
    slider = chunk.Slider("s")
    slider.value = 1.0
    menu = chunk.Menu("m")
    menu.menu_items = ["a", "b"]
    menu.value = "a"
    line = chunk.LineEdit("l")
    line.value = "hello"
    button = chunk.Button("btn")
    c.add_param(slider)
    c.add_param(menu)
    c.add_param(line)
    c.add_param(button)

    _author_prim(stage, c)
    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/n")
    for p in (slider, menu, line):
        attr = prim.GetAttribute(f"{_INPUTS_PREFIX}{p.name}")
        assert attr.IsValid()
        p._connect_param_attr(attr)

    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "scene", _DummyScene(stage))
    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "initialized", True)

    keyframed = chunk_animation.keyframe_chunk(c, 1.0)

    assert keyframed == [slider]
    slider_attr = prim.GetAttribute(f"{_INPUTS_PREFIX}s")
    assert slider_attr.GetNumTimeSamples() == 1
    menu_attr = prim.GetAttribute(f"{_INPUTS_PREFIX}m")
    assert menu_attr.GetNumTimeSamples() == 0
    line_attr = prim.GetAttribute(f"{_INPUTS_PREFIX}l")
    assert line_attr.GetNumTimeSamples() == 0


# ---------------------------------------------------------------------------
# ChunkAnimationManager.on_frame_changed
# ---------------------------------------------------------------------------

def test_on_frame_changed_refreshes_tracked_param_value(monkeypatch) -> None:
    slider = chunk.Slider("myParam")
    stage, attr, scene = _bind_param_to_stage(monkeypatch, slider, 1.0)

    chunk_animation.keyframe_param(slider, 1.0)
    # Once animated, this plain assignment auto-keys at get_current_frame() (slice 02) --
    # match it to the frame the next keyframe_param() call targets so they land on the
    # same sample instead of adding an extra one.
    scene._current_frame = 24.0
    slider.value = 24.0
    chunk_animation.keyframe_param(slider, 24.0)

    chunk_animation.ChunkAnimationManager().on_frame_changed(12.0)

    expected = attr.Get(Usd.TimeCode(12.0))
    assert slider.value == pytest.approx(expected)
    # The pull-refresh must not re-author a sample at the frame it just read from.
    assert attr.GetNumTimeSamples() == 2


def test_on_frame_changed_ignores_untracked_params(monkeypatch) -> None:
    slider = chunk.Slider("myParam")
    stage, attr, scene = _bind_param_to_stage(monkeypatch, slider, 1.0)
    # Never keyframed -> never tracked.
    assert not chunk_animation.ChunkAnimationManager().is_tracked(slider)

    chunk_animation.ChunkAnimationManager().on_frame_changed(12.0)

    assert slider.value == pytest.approx(1.0)
    assert attr.GetNumTimeSamples() == 0


# ---------------------------------------------------------------------------
# Auto-key: once a param is animated, plain value edits become keyframes
# ---------------------------------------------------------------------------

def test_plain_edit_before_any_keyframe_still_writes_default_value(monkeypatch) -> None:
    """A never-animated param's edits must stay exactly as before -- a plain default-value
    Set(), zero time samples. This is the regression this slice must not introduce."""
    slider = chunk.Slider("myParam")
    stage, attr, scene = _bind_param_to_stage(monkeypatch, slider, 1.0)

    slider.value = 2.0

    assert attr.GetNumTimeSamples() == 0
    assert attr.Get() == pytest.approx(2.0)


def test_plain_edit_after_keyframe_inserts_new_sample_at_current_frame(monkeypatch) -> None:
    slider = chunk.Slider("myParam")
    stage, attr, scene = _bind_param_to_stage(monkeypatch, slider, 1.0)
    chunk_animation.keyframe_param(slider, 1.0)

    scene._current_frame = 10.0
    slider.value = 5.0

    assert attr.GetNumTimeSamples() == 2
    assert attr.Get(Usd.TimeCode(1.0)) == pytest.approx(1.0)
    assert attr.Get(Usd.TimeCode(10.0)) == pytest.approx(5.0)


def test_plain_edit_at_existing_keyframe_overwrites_it_not_duplicates(monkeypatch) -> None:
    slider = chunk.Slider("myParam")
    stage, attr, scene = _bind_param_to_stage(monkeypatch, slider, 1.0)
    chunk_animation.keyframe_param(slider, 1.0)
    scene._current_frame = 10.0
    slider.value = 5.0
    chunk_animation.keyframe_param(slider, 10.0)
    assert attr.GetNumTimeSamples() == 2

    # Editing again at the same (already-keyed) frame must overwrite, not add a third sample.
    slider.value = 9.0

    assert attr.GetNumTimeSamples() == 2
    assert attr.Get(Usd.TimeCode(10.0)) == pytest.approx(9.0)
    assert attr.Get(Usd.TimeCode(1.0)) == pytest.approx(1.0)


def test_auto_key_mirrors_onto_driven_attrs(monkeypatch) -> None:
    slider = chunk.Slider("myParam")
    stage, attr, scene = _bind_param_to_stage(monkeypatch, slider, 1.0)

    driven_prim = stage.DefinePrim("/driven", "Xform")
    driven_attr = driven_prim.CreateAttribute("myDriven", Sdf.ValueTypeNames.Float)
    slider._driven_attr_paths.append(str(driven_attr.GetPath()))

    chunk_animation.keyframe_param(slider, 1.0)
    scene._current_frame = 10.0
    slider.value = 5.0

    assert driven_attr.GetNumTimeSamples() == 2
    assert driven_attr.Get(Usd.TimeCode(10.0)) == pytest.approx(5.0)


def test_vector_auto_key_uses_current_frame_not_hardcoded_zero(monkeypatch) -> None:
    """ChunkVectorBase.data_changed() has its own separate write branch (doesn't reuse
    ChunkUsdAttributeSupport.data_changed()) with the same historical Get(0)/Set(val)
    pattern -- this must get the identical current-frame-aware fix."""
    vec = chunk.Vector("v", param=chunk.Slider(), length=3)
    stage, attr, scene = _bind_param_to_stage(monkeypatch, vec, (1.0, 2.0, 3.0))
    chunk_animation.keyframe_param(vec, 1.0)

    scene._current_frame = 10.0
    vec.value = (4.0, 5.0, 6.0)

    assert attr.GetNumTimeSamples() == 2
    assert tuple(attr.Get(Usd.TimeCode(1.0))) == pytest.approx((1.0, 2.0, 3.0))
    assert tuple(attr.Get(Usd.TimeCode(10.0))) == pytest.approx((4.0, 5.0, 6.0))


def test_scrubbing_away_from_edited_frame_does_not_disturb_other_keyframes(monkeypatch) -> None:
    slider = chunk.Slider("myParam")
    stage, attr, scene = _bind_param_to_stage(monkeypatch, slider, 1.0)
    chunk_animation.keyframe_param(slider, 1.0)
    scene._current_frame = 10.0
    slider.value = 5.0

    chunk_animation.ChunkAnimationManager().on_frame_changed(20.0)
    chunk_animation.ChunkAnimationManager().on_frame_changed(1.0)

    assert attr.GetNumTimeSamples() == 2
    assert attr.Get(Usd.TimeCode(1.0)) == pytest.approx(1.0)
    assert attr.Get(Usd.TimeCode(10.0)) == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# clear_keyframes_for_param / clear_keyframes_for_chunk
# ---------------------------------------------------------------------------

def test_clear_keyframes_removes_samples_and_freezes_resolved_value(monkeypatch) -> None:
    slider = chunk.Slider("myParam")
    stage, attr, scene = _bind_param_to_stage(monkeypatch, slider, 1.0)
    chunk_animation.keyframe_param(slider, 1.0)
    scene._current_frame = 24.0
    slider.value = 24.0
    chunk_animation.keyframe_param(slider, 24.0)
    assert attr.GetNumTimeSamples() == 2

    mid_at_12 = attr.Get(Usd.TimeCode(12.0))
    assert chunk_animation.clear_keyframes_for_param(slider, 12.0) is True

    assert attr.GetNumTimeSamples() == 0
    assert attr.Get() == pytest.approx(mid_at_12)
    assert not chunk_animation.ChunkAnimationManager().is_tracked(slider)


def test_clear_keyframes_mirrors_onto_driven_attrs(monkeypatch) -> None:
    slider = chunk.Slider("myParam")
    stage, attr, scene = _bind_param_to_stage(monkeypatch, slider, 1.0)
    driven_prim = stage.DefinePrim("/driven", "Xform")
    driven_attr = driven_prim.CreateAttribute("myDriven", Sdf.ValueTypeNames.Float)
    slider._driven_attr_paths.append(str(driven_attr.GetPath()))

    chunk_animation.keyframe_param(slider, 1.0)
    scene._current_frame = 24.0
    slider.value = 24.0
    chunk_animation.keyframe_param(slider, 24.0)
    assert driven_attr.GetNumTimeSamples() == 2

    driven_mid = driven_attr.Get(Usd.TimeCode(12.0))
    chunk_animation.clear_keyframes_for_param(slider, 12.0)

    assert driven_attr.GetNumTimeSamples() == 0
    assert driven_attr.Get() == pytest.approx(driven_mid)


def test_clear_keyframes_for_param_with_no_samples_is_noop(monkeypatch) -> None:
    slider = chunk.Slider("myParam")
    stage, attr, scene = _bind_param_to_stage(monkeypatch, slider, 1.0)

    assert chunk_animation.clear_keyframes_for_param(slider, 12.0) is False
    assert attr.GetNumTimeSamples() == 0
    assert attr.Get() == pytest.approx(1.0)


def test_clear_keyframes_for_chunk_only_clears_animated_params(monkeypatch) -> None:
    stage = Usd.Stage.CreateInMemory()
    c = _chunk_with_module("n")
    animated_slider = chunk.Slider("animated")
    animated_slider.value = 1.0
    # Menu is never animatable (chunk_animation.ANIMATABLE_PARAM_TYPES) -- keyframe_chunk
    # must skip it entirely, so clear_keyframes_for_chunk has nothing to clear on it either.
    static_menu = chunk.Menu("staticP")
    static_menu.menu_items = ["a", "b"]
    static_menu.value = "a"
    c.add_param(animated_slider)
    c.add_param(static_menu)

    _author_prim(stage, c)
    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/n")
    for p in (animated_slider, static_menu):
        attr = prim.GetAttribute(f"{_INPUTS_PREFIX}{p.name}")
        assert attr.IsValid()
        p._connect_param_attr(attr)

    chunk_registry.ChunkLayerManager().initialize(stage)
    scene = _DummyScene(stage)
    scene._chunk = c
    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "scene", scene)
    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "initialized", True)

    chunk_animation.keyframe_chunk(c, 1.0)
    animated_attr = prim.GetAttribute(f"{_INPUTS_PREFIX}animated")
    static_attr = prim.GetAttribute(f"{_INPUTS_PREFIX}staticP")
    assert animated_attr.GetNumTimeSamples() == 1
    assert static_attr.GetNumTimeSamples() == 0

    cleared = chunk_animation.clear_keyframes_for_chunk(c, 1.0)

    assert cleared == [animated_slider]
    assert animated_attr.GetNumTimeSamples() == 0
    assert animated_attr.Get() == pytest.approx(1.0)
    assert static_attr.GetNumTimeSamples() == 0
    assert static_attr.Get() == "a"


# ---------------------------------------------------------------------------
# chunk_has_animated_params (drives the graph node's animated glyph)
# ---------------------------------------------------------------------------

def test_chunk_has_animated_params_reflects_keyframe_and_clear(monkeypatch) -> None:
    slider = chunk.Slider("myParam")
    stage, attr, scene = _bind_param_to_stage(monkeypatch, slider, 1.0)
    c = scene._chunk

    assert chunk_animation.chunk_has_animated_params(c) is False

    chunk_animation.keyframe_param(slider, 1.0)
    assert chunk_animation.chunk_has_animated_params(c) is True

    chunk_animation.clear_keyframes_for_param(slider, 1.0)
    assert chunk_animation.chunk_has_animated_params(c) is False


def test_chunk_has_animated_params_false_for_chunk_with_no_stage(monkeypatch) -> None:
    """A chunk never bound to a live stage (e.g. mid-construction) must not raise."""
    c = _chunk_with_module("n")
    slider = chunk.Slider("s")
    c.add_param(slider)
    assert chunk_animation.chunk_has_animated_params(c) is False


# ---------------------------------------------------------------------------
# ensure_frame_range_covers
# ---------------------------------------------------------------------------

def test_ensure_frame_range_covers_normalizes_a_timecode_frame_to_plain_float(monkeypatch) -> None:
    """Regression test for the real live-usdview crash: UsdviewScene.get_current_frame()
    returned a raw Usd.TimeCode (dataModel.currentFrame's actual type, despite its own
    -> float annotation) instead of a plain float. Usd.TimeCode(already_a_timecode) tolerates
    this almost everywhere (e.g. keyframe_param's own Usd.TimeCode(frame) call), but
    Stage.SetStartTimeCode/SetEndTimeCode reject a TimeCode outright with a Boost.Python
    ArgumentError. ensure_frame_range_covers must normalize before ever calling
    set_start_frame/set_end_frame, regardless of what the caller happens to pass."""
    scene = _DummyScene(start_frame=None, end_frame=None)
    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "scene", scene)
    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "initialized", True)

    chunk_animation.ensure_frame_range_covers(Usd.TimeCode(1.0))

    assert type(scene.get_start_frame()) is float
    assert type(scene.get_end_frame()) is float
    assert scene.get_start_frame() == 1.0
    assert scene.get_end_frame() == 100.0


def test_ensure_frame_range_covers_establishes_default_range_when_none_exists(monkeypatch) -> None:
    """Regression test for the real live-usdview crash: a brand-new stage with no
    authored startTimeCode/endTimeCode leaves usdview's own realStartTimeCode/
    realEndTimeCode at None (appController.py's _UpdateTimeSamples), which
    get_start_frame/get_end_frame surface as None -- comparing frame < None used to
    raise TypeError. Must establish a real, generous scrubbable window instead of
    just the single keyed frame."""
    scene = _DummyScene(start_frame=None, end_frame=None)
    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "scene", scene)
    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "initialized", True)

    chunk_animation.ensure_frame_range_covers(1.0)

    assert scene.get_start_frame() == 1.0
    assert scene.get_end_frame() == 100.0


def test_ensure_frame_range_covers_default_range_widens_for_an_out_of_window_frame(monkeypatch) -> None:
    scene = _DummyScene(start_frame=None, end_frame=None)
    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "scene", scene)
    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "initialized", True)

    chunk_animation.ensure_frame_range_covers(500.0)

    assert scene.get_start_frame() == 1.0
    assert scene.get_end_frame() == 500.0


def test_ensure_frame_range_covers_grows_end_frame(monkeypatch) -> None:
    scene = _DummyScene(start_frame=0.0, end_frame=0.0)
    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "scene", scene)
    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "initialized", True)

    chunk_animation.ensure_frame_range_covers(24.0)

    assert scene.get_start_frame() == 0.0
    assert scene.get_end_frame() == 24.0


def test_ensure_frame_range_covers_grows_start_frame(monkeypatch) -> None:
    scene = _DummyScene(start_frame=1.0, end_frame=24.0)
    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "scene", scene)
    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "initialized", True)

    chunk_animation.ensure_frame_range_covers(-5.0)

    assert scene.get_start_frame() == -5.0
    assert scene.get_end_frame() == 24.0


def test_ensure_frame_range_covers_noop_when_frame_already_in_range(monkeypatch) -> None:
    scene = _DummyScene(start_frame=1.0, end_frame=24.0)
    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "scene", scene)
    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "initialized", True)

    chunk_animation.ensure_frame_range_covers(12.0)

    assert scene.get_start_frame() == 1.0
    assert scene.get_end_frame() == 24.0


def test_ensure_frame_range_covers_tolerates_host_without_frame_range_api(monkeypatch) -> None:
    """A host SceneSender that never overrides get_start_frame/etc (chunk_core.py's base
    raises NotImplementedError) must not crash K -- this feature is usdview-only for v1."""
    class _NoFrameRangeScene(SceneSender):
        def get_stage(self):
            return None

        def get_selected_prims(self):
            return []

    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "scene", _NoFrameRangeScene())
    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "initialized", True)

    chunk_animation.ensure_frame_range_covers(24.0)  # must not raise


# ---------------------------------------------------------------------------
# bake_procedural_output
# ---------------------------------------------------------------------------

class _FakeModuleWithBake:
    def __init__(self):
        self.bake_calls: list = []

    def bake(self, node, start_frame, end_frame):
        self.bake_calls.append((node, start_frame, end_frame))


class _FakeModuleWithoutBake:
    """Stands in for a chunk module with no bake() hook at all (most chunks)."""
    pass


class _FakeProceduralChunk:
    def __init__(self, module=None):
        self._module = module
        self.executed = 0

    def execute(self):
        self.executed += 1


def test_bake_procedural_output_calls_module_bake_hook_with_frame_range(monkeypatch) -> None:
    """The whole point of this design (vs. an earlier version that scrubbed the timeline
    to force reactive recomputation): bake() is a plain, direct call with the frame
    range -- no touching the current frame, no re-triggering any OTHER chunk's own
    reactive hooks along the way (which was exactly the earlier version's problem: an
    expensive "auto-simulate" chunk downstream would re-run on every single frame of the
    scrub, not just the one chunk actually being baked)."""
    module = _FakeModuleWithBake()
    chunk_obj = _FakeProceduralChunk(module)
    scene = _DummyScene(start_frame=1.0, end_frame=24.0, current_frame=3.0)
    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "scene", scene)
    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "initialized", True)
    monkeypatch.setattr(chunk_animation, "chunk_has_animated_params", lambda c: True)

    chunk_animation.bake_procedural_output(chunk_obj)

    assert module.bake_calls == [(chunk_obj, 1.0, 24.0)]
    assert chunk_obj.executed == 0  # execute() itself must not be touched by baking
    assert scene.get_current_frame() == 3.0  # untouched -- no scrubbing


def test_bake_procedural_output_noop_when_module_has_no_bake_hook(monkeypatch) -> None:
    """Most chunks won't define bake() -- must be a clean no-op, not an AttributeError."""
    chunk_obj = _FakeProceduralChunk(_FakeModuleWithoutBake())
    scene = _DummyScene(start_frame=1.0, end_frame=24.0)
    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "scene", scene)
    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "initialized", True)
    monkeypatch.setattr(chunk_animation, "chunk_has_animated_params", lambda c: True)

    chunk_animation.bake_procedural_output(chunk_obj)  # must not raise

    assert chunk_obj.executed == 0


def test_bake_procedural_output_settles_via_direct_execute_when_not_animated(monkeypatch) -> None:
    """Once nothing is animated anymore (e.g. right after Clear Keyframes removed the
    last sample), one direct execute() call is enough to settle the output back to a
    static value via the module's own normal (non-animated) code path."""
    chunk_obj = _FakeProceduralChunk(_FakeModuleWithBake())
    scene = _DummyScene()
    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "scene", scene)
    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "initialized", True)
    monkeypatch.setattr(chunk_animation, "chunk_has_animated_params", lambda c: False)

    chunk_animation.bake_procedural_output(chunk_obj)

    assert chunk_obj.executed == 1
    assert chunk_obj._module.bake_calls == []  # bake() itself must not run


def test_bake_procedural_output_noop_when_registry_not_initialized(monkeypatch) -> None:
    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "initialized", False)
    chunk_animation.bake_procedural_output(None)  # must not raise
