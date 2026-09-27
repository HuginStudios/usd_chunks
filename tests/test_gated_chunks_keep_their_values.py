"""A light gated off when the scene opens comes back with its own values, not defaults.

Live-found on demo scene 10 (two sequences, ten shots): a note light on a shot-gated branch
rendered at intensity **1** -- the UsdLux schema default -- on every shot that switched it
on, with the real 68 sitting in the chunk prim the whole time. The note was in the render,
technically, and invisible.

Why it happens, and why the existing repair did not catch it:

* a muted layer composes as nothing, so the `stage.DefinePrim` a Light runs while gated off
  hands back an **invalid** prim;
* `show_attribute` takes an invalid prim as "no prim", clears the chunk's params and
  returns, so nothing is bound to the light's attributes;
* the values are still restored from the chunk prim, so the chunk *looks* right in the UI --
  they simply drive nothing;
* `push_chunk_values`, the repair that exists for exactly this, walks the bindings. With
  none, it writes nothing;
* and the chunk's own "this type is already at this path" cache was set by that cook, so it
  never rebuilds the binding either. The light is stuck at its default forever.

The fix is in the two chunks that build params from the prim they author: don't cook against
a muted layer at all, and don't trust the cache unless the params it built are still bound
(`chunk_stage.params_are_bound`). The Camera chunk has the same shape -- a per-shot camera
on a gated branch is the same story -- so it is covered here too.

Assertions read the composed stage: the value a renderer would get, not the chunk's UI.
"""

from __future__ import annotations

import importlib
import pathlib
import sys

import pytest
from pxr import Usd, UsdGeom

from usd_chunks import chunk, chunk_registry, chunk_stage, chunk_utils
from usd_chunks.chunk_core import ChunkConfig
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_registry import ChunkLayerManager
from usd_chunks.chunk_stage import initialize as chunk_stage_initialize, save_scene

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "chunks"
for _chunk_dir in (_SCRIPTS / "utils", _SCRIPTS / "modelling"):
    if str(_chunk_dir) not in sys.path:
        sys.path.insert(0, str(_chunk_dir))
switch_mod = importlib.import_module("stage_expression_switch")
light_mod = importlib.import_module("light")
camera_mod = importlib.import_module("camera")

#: Nothing like a schema default, so "it came back wrong" cannot pass by coincidence.
INTENSITY = 68.0
FOCAL_LENGTH = 24.0

NIGHT = "/lights/night_note"
SHARED = "/lights/shared"
SHOT_CAM = "/cameras/shot_cam"


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


def _scene_for(stage):
    sender = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(sender, "pytest")
    ChunkLayerManager().initialize(stage)
    chunk_stage_initialize(stage, sender)
    return sender


def _mk(sender, module, name, config_module, **config_kwargs):
    node = module.main()
    node._config = ChunkConfig(module=config_module, **config_kwargs)
    node._module = module
    node.name = name
    sender.add_chunk(node)
    return node


def _light(sender, name, path, intensity):
    node = _mk(sender, light_mod, name, "light")
    node.get_param("primpath_location").value = path
    node.get_param("light_type").value = "SphereLight"
    node.execute()
    node.get_param("attr_grp.inputs.intensity").value = intensity
    return node


def _camera(sender, name, path, focal_length):
    node = _mk(sender, camera_mod, name, "camera")
    node.get_param("primpath_location").value = path
    node.execute()
    node.get_param("focalLength").value = focal_length
    return node


def _wire(stage, dst, *sources):
    """Exec edges, in input order -- normal path first, matched path second.

    add_dependency rather than authoring inputs:chunk by hand: the DAG edge is what carries
    the change downstream, so this is the wiring a real scene has. Authoring the connection
    alone leaves every gate needing to be executed by name, which is a test that cannot
    catch a gate failing to re-run.
    """
    for src in sources:
        dst.add_dependency(src)


def _declare(stage, **variables):
    current = dict(chunk_utils.stage_expression_variables(stage))
    current.update(variables)
    chunk_utils.set_stage_expression_variables(stage, current)


def _value(stage, prim_path, attr_name):
    """What a renderer would read, or None when the prim is not in the scene at all."""
    prim = stage.GetPrimAtPath(prim_path)
    if not (prim and prim.IsDefined()):
        return None
    attr = prim.GetAttribute(attr_name)
    return attr.Get() if attr else None


def _set_shot(sender, shot):
    ctx = next(c for c in sender.get_chunks() if c.name == "ShotContext")
    ctx.get_param("variables").get_indexed_param("var_value", 0).value = shot
    ctx.execute()


def _build(tmp_path) -> pathlib.Path:
    """A shared light, and a note branch holding a light and a camera, gated on SHOT.

    Saved on a shot the note does NOT name, so the branch is muted in the file -- which is
    the state that broke it. A scene saved with the branch live never hit this.
    """
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    sender = _scene_for(stage)

    ctx = _mk(sender, importlib.import_module("stage_expression"), "ShotContext",
              "stage_expression")
    rows = ctx.get_param("variables")
    rows.size = 1
    rows.get_indexed_param("var_name", 0).value = "SHOT"
    rows.get_indexed_param("var_type", 0).value = "string"
    rows.get_indexed_param("var_value", 0).value = "sh010"
    ctx.execute()

    shared = _light(sender, "Shared", SHARED, INTENSITY)
    # The context feeds the chain, so changing SHOT carries downstream to the gate the way
    # it does in a real scene. Without the edge nothing re-cooks and the gate never moves.
    _wire(stage, shared, ctx)
    note_light = _light(sender, "NightNote", NIGHT, INTENSITY)
    note_cam = _camera(sender, "ShotCam", SHOT_CAM, FOCAL_LENGTH)
    _wire(stage, note_light, shared)
    _wire(stage, note_cam, note_light)

    switch = _mk(sender, switch_mod, "NoteSwitch", "stage_expression_switch", exec_inputs=2)
    switch.get_param(switch_mod.VARIABLE_PARAM).value = "SHOT"
    switch.get_param(switch_mod.PATTERN_PARAM).value = "sh030 sh040"
    _wire(stage, switch, shared, note_cam)

    # A View on the end, which is not decoration: view-driven cooking MUTES first and cooks
    # afterwards, so the gated-off chunks are cooked while their layer is already muted --
    # the exact order that loses the binding. Without a view they cook first and are muted
    # after, the binding survives, and none of this reproduces.
    view = _mk(sender, importlib.import_module("view"), "View", "view")
    _wire(stage, view, switch)
    switch.execute()

    assert _value(stage, NIGHT, "inputs:intensity") is None, "the note is gated off to start"

    out = pathlib.Path(tmp_path) / "gated.usda"
    save_scene(sender, out)
    return out


def _reopen(path):
    ChunkLayerManager().reset()
    stage = Usd.Stage.Open(str(path))
    return stage, _scene_for(stage)


@pytest.fixture
def reopened(tmp_path):
    return _reopen(_build(tmp_path))


def test_a_gated_light_comes_back_with_its_own_intensity(reopened):
    """The reported failure: it came back at 1.0, the schema default."""
    stage, sender = reopened

    _set_shot(sender, "sh030")

    assert _value(stage, NIGHT, "inputs:intensity") == pytest.approx(INTENSITY)


def test_a_gated_camera_comes_back_with_its_own_focal_length(reopened):
    """Same shape as the light: a per-shot camera on a gated branch."""
    stage, sender = reopened

    _set_shot(sender, "sh030")

    assert _value(stage, SHOT_CAM, "focalLength") == pytest.approx(FOCAL_LENGTH)


def test_it_is_still_gated_off_on_a_shot_the_note_does_not_name(reopened):
    """The fix must not leave the branch permanently live -- it only restores the values."""
    stage, sender = reopened

    _set_shot(sender, "sh030")
    _set_shot(sender, "sh020")

    assert _value(stage, NIGHT, "inputs:intensity") is None
    assert _value(stage, SHOT_CAM, "focalLength") is None


def test_the_values_survive_being_gated_off_and_on_again(reopened):
    stage, sender = reopened

    for shot in ("sh030", "sh010", "sh040", "sh020", "sh030"):
        _set_shot(sender, shot)

    assert _value(stage, NIGHT, "inputs:intensity") == pytest.approx(INTENSITY)


def test_the_shared_light_was_never_affected(reopened):
    """It is never muted, so it kept its binding throughout -- the control for the others."""
    stage, sender = reopened

    assert _value(stage, SHARED, "inputs:intensity") == pytest.approx(INTENSITY)
    _set_shot(sender, "sh030")
    assert _value(stage, SHARED, "inputs:intensity") == pytest.approx(INTENSITY)


def test_a_gated_chunks_params_are_left_alone_while_it_waits(reopened):
    """Cooking a Light against a muted layer would run show_attribute against an invalid
    prim, which clears the chunk's params. The values must still be there to come back to."""
    stage, sender = reopened
    note = next(c for c in sender.get_chunks() if c.name == "NightNote")

    assert note.is_blocked(), "test setup: it is gated off in the file"
    note.execute()

    param = note.get_param("attr_grp.inputs.intensity")
    assert param is not None, "the params were cleared while the chunk was gated off"
    assert param.value == pytest.approx(INTENSITY)


def test_params_are_bound_reports_what_it_says(reopened):
    """The predicate the two chunks now gate their cache on."""
    stage, sender = reopened
    shared = next(c for c in sender.get_chunks() if c.name == "Shared")

    assert chunk_stage.params_are_bound(shared), "a live chunk drives its prim"

    _set_shot(sender, "sh030")
    note = next(c for c in sender.get_chunks() if c.name == "NightNote")
    assert chunk_stage.params_are_bound(note), "and so does one whose gate has opened"


def _unbind(chunk):
    """Put a chunk in the state a reopened, gated-off one is in: values present, bound to
    nothing.

    Not a contrivance -- it is what `_restore_dynamic_params` leaves behind by design. Its
    own comment says so: "a chunk gated off already has its layer muted, so the real prim is
    not on the stage to hand over as an attribute", so `stage.GetAttributeAtPath` finds
    nothing and the rebind is skipped, with `set_chunk_muted` expected to push the value
    later. Pushing walks the bindings, so with none there is nothing to push.

    Reached here directly because the load ORDER that produces it needs the whole cast --
    a view driving the cook, a gate that mutes before the restore runs. Scene 10 has them
    and every gated light in it reopened unbound; a scene small enough for a test does not.
    """
    for _, param in chunk_stage._iter_chunk_params(chunk):
        if getattr(param, "_driven_attr_paths", None):
            param._driven_attr_paths = []


def test_a_cook_rebuilds_a_binding_the_load_could_not_restore(reopened):
    """The heart of it: cooking again must rebuild what the reopen could not.

    Without the fix the chunk's own "this type is already at this path" cache answers yes
    and returns, so the binding is never rebuilt and the light drives nothing for the life
    of the scene -- which is how a note light rendered at intensity 1 with 68 in its params.
    """
    stage, sender = reopened
    _set_shot(sender, "sh030")
    note = next(c for c in sender.get_chunks() if c.name == "NightNote")
    assert chunk_stage.params_are_bound(note), "test setup: it starts bound"

    _unbind(note)
    note.execute()

    assert chunk_stage.params_are_bound(note), "the cache claimed a cook that did not happen"
    assert _value(stage, NIGHT, "inputs:intensity") == pytest.approx(INTENSITY)


def test_the_same_for_a_camera(reopened):
    stage, sender = reopened
    _set_shot(sender, "sh030")
    cam = next(c for c in sender.get_chunks() if c.name == "ShotCam")

    _unbind(cam)
    cam.execute()

    assert chunk_stage.params_are_bound(cam)
    assert _value(stage, SHOT_CAM, "focalLength") == pytest.approx(FOCAL_LENGTH)


def test_a_bound_chunk_still_takes_the_shortcut(reopened):
    """The cache is an optimisation worth keeping: a cook that has nothing to redo must
    still do nothing, or every light re-runs show_attribute on every cook of the graph."""
    stage, sender = reopened
    shared = next(c for c in sender.get_chunks() if c.name == "Shared")
    calls = []
    real = chunk.show_attribute
    chunk.show_attribute = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
    try:
        shared.execute()
    finally:
        chunk.show_attribute = real

    assert calls == [], "a bound, unchanged light rebuilt its params anyway"
