"""A param a chunk assigns inside its own execute() has to reach the chunk prim.

A saved scene is chunk prims and nothing else (chunk_stage.export_chunks), so a value that
never gets an attr on the chunk prim is a value that does not survive a save. Params written
by the user reach it through ChunkStageSync._on_attr_changed; params written by the chunk
itself, during its cook, did not -- that handler skipped while chunk_notice._usd_write_active
was set, and executing a chunk sets exactly that flag for the whole call.

The flag is there to stop USD changes bouncing *back* into params while a chunk authors
content. Suppressing the outbound record as well was collateral, and it lost two things
users noticed. The reported one: a Camera placed with Lock to Free Camera, whose placement
was taken into the transform params by camera.py's `_adopt_existing_transform` on the cook
after the lock came off. The live camera was right, the saved file carried no placement at
all, and reopening put it back at the origin. (The lock now writes the params itself, outside
any cook -- camera.py's `_follow_freecam` -- and the tests below keep the round trip honest.)
"""

from __future__ import annotations

import pathlib

from pxr import Gf, Usd, UsdGeom

from usd_chunks import chunk_notice, chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import (
    ChunkStageSync,
    initialize as chunk_usd_initialize,
    save_scene,
)


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _scene_for(stage: Usd.Stage) -> SceneSender:
    """A scene wired to `stage`, initialised the way opening a file does.

    chunk_stage.initialize (not a bare load_chunks) because the cook pass that regenerates a
    reopened scene's content only runs when ChunkLayerManager owns this exact stage, which is
    what initialize arranges. Without it every chunk reloads with its params and authors
    nothing, and every assertion here would pass on an empty stage.
    """

    class _Staged(_DummyScene):
        def get_stage(self):
            return stage

    scene = _Staged()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_usd_initialize(stage, scene)
    return scene


def _new_stage() -> Usd.Stage:
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    return stage


def _add(scene: SceneSender, module: str):
    mod = chunk_registry.ChunkRegistry().get_chunk_module(module)
    chunk_obj = (mod.create() if mod.loaded else mod.load())[0]
    scene.add_chunk(chunk_obj)
    return chunk_obj


def _reopen(scene: SceneSender, module: str, tmp_path) -> tuple[Usd.Stage, list]:
    """Save the scene, open the file fresh, and hand back the stage and its chunks."""
    out = pathlib.Path(tmp_path) / "scene.usda"
    save_scene(scene, out)

    chunk_registry.ChunkLayerManager().reset()
    reopened = Usd.Stage.Open(str(out))
    reopened_scene = _scene_for(reopened)
    chunks = [
        c for c in reopened_scene.get_chunks()
        if c.get_config() and c.get_config().module == module
    ]
    return reopened, chunks


def _translation(stage: Usd.Stage, path: str):
    prim = stage.GetPrimAtPath(path)
    if not (prim and prim.IsDefined()):
        return None
    xform = UsdGeom.Xformable(prim)
    return xform.ComputeLocalToWorldTransform(Usd.TimeCode.Default()).ExtractTranslation()


# ---------------------------------------------------------------------------
# a camera placed by flying there
# ---------------------------------------------------------------------------

def _flown(scene: SceneSender, translation) -> None:
    """Make the host's freecam sit at `translation`, the way flying the viewport does."""
    camera = Gf.Camera()
    camera.transform = Gf.Matrix4d().SetTranslate(Gf.Vec3d(*translation))
    type(scene).get_active_camera = lambda self: camera
    type(scene).set_viewport_camera = lambda self, prim: None


def test_a_camera_flown_into_place_is_still_there_after_a_reopen(tmp_path) -> None:
    """Fly, tick Lock to Free Camera, untick it to freeze the shot, save.

    The placement is in the transform params (camera.py's `_follow_freecam`), so the live
    chunk is correct either way -- what was missing
    is that a param set during that cook never got an attr on the chunk prim, so the file
    carried no placement at all and the camera reopened at the origin.
    """
    stage = _new_stage()
    scene = _scene_for(stage)
    _flown(scene, (11, 22, 33))

    camera = _add(scene, "camera")
    camera.get_param("lock_to_freecam").value = True
    camera.get_param("lock_to_freecam").value = False

    assert _translation(stage, "/cameras/camera1") == Gf.Vec3d(11, 22, 33), (
        "the live camera has to be where the freecam was, or this tests nothing"
    )

    reopened, chunks = _reopen(scene, "camera", tmp_path)

    assert len(chunks) == 1
    assert _translation(reopened, "/cameras/camera1") == Gf.Vec3d(11, 22, 33)


def test_a_camera_flown_into_place_writes_its_placement_into_the_file(tmp_path) -> None:
    """The same thing one level down: the placement is on the chunk prim, which is the only
    part of the scene a save carries."""
    stage = _new_stage()
    scene = _scene_for(stage)
    _flown(scene, (11, 22, 33))

    camera = _add(scene, "camera")
    camera.get_param("lock_to_freecam").value = True
    camera.get_param("lock_to_freecam").value = False

    out = pathlib.Path(tmp_path) / "scene.usda"
    save_scene(scene, out)

    written = out.read_text()
    for attr, value in (
        ("inputs:translate_grp:tx", 11),
        ("inputs:translate_grp:ty", 22),
        ("inputs:translate_grp:tz", 33),
    ):
        assert f"{attr} = {value}" in written, f"{attr} missing from the saved scene"


# ---------------------------------------------------------------------------
# the rule itself
# ---------------------------------------------------------------------------

def test_a_chunks_own_param_write_is_recorded_while_it_cooks() -> None:
    """The guard says what it is for: our own USD writes must not bounce back through the
    param path as if they were edits. A chunk assigning its own param is not that -- it is the
    edit -- so it is recorded even though the same flag is set for the whole cook."""
    stage = _new_stage()
    scene = _scene_for(stage)
    camera = _add(scene, "camera")
    sync = ChunkStageSync()
    param = camera.get_param("tx")

    chunk_notice._usd_write_active = True
    chunk_notice._executing_chunk = camera
    try:
        assert sync._records_param_writes(param)
    finally:
        chunk_notice._usd_write_active = False
        chunk_notice._executing_chunk = None


def test_a_write_from_somewhere_else_mid_cook_is_still_ignored() -> None:
    """The other half, and the reason this isn't just "drop the guard": one chunk reaching
    into another's params during its own cook is a push, not the writer recording its own
    state, and neither is the ObjectsChanged echo of a plain USD write."""
    stage = _new_stage()
    scene = _scene_for(stage)
    camera = _add(scene, "camera")
    other = _add(scene, "prim")
    sync = ChunkStageSync()
    param = camera.get_param("tx")

    chunk_notice._usd_write_active = True
    try:
        chunk_notice._executing_chunk = other
        assert not sync._records_param_writes(param)
        chunk_notice._executing_chunk = None
        assert not sync._records_param_writes(param)
    finally:
        chunk_notice._usd_write_active = False
        chunk_notice._executing_chunk = None


# ---------------------------------------------------------------------------
# the exception: session state
# ---------------------------------------------------------------------------

def test_a_transient_param_is_kept_out_of_the_saved_scene(tmp_path) -> None:
    """Recording what a chunk writes during its cook is right for a value and wrong for a
    handle. Layer keeps the identifier of the anonymous layer it owns in a param, and
    an anonymous identifier names an in-memory object -- saving one records an address that is
    already dead by the time anything reads it back. Reported by the layer stack coming up
    with two layers called "B" after a reload.
    """
    stage = _new_stage()
    scene = _scene_for(stage)
    layer_chunk = _add(scene, "layer")
    layer_chunk.get_param("layer_name").value = "B"

    assert layer_chunk.get_param("_new_layer_id").value, (
        "the chunk has to have made a layer for this to test anything"
    )

    out = pathlib.Path(tmp_path) / "scene.usda"
    save_scene(scene, out)

    written = out.read_text()
    for name in ("_new_layer_id", "_parent_layer_id", "_layer_name_used"):
        assert name not in written, f"{name} is session state and must not be saved"
    assert "inputs:layer_name" in written, "the name the user typed still has to be saved"


# ---------------------------------------------------------------------------
# the feedback loop this does not open
# ---------------------------------------------------------------------------

def test_a_chunk_that_writes_its_own_params_still_cooks_once_per_edit() -> None:
    """Raised in review: recording a param written during a cook could feed back into the
    DAG that scheduled the cook, and a chunk that assigns its own params every time would
    then cook forever.

    It does not, and the reason is structural rather than lucky: chunk_notice refuses to
    re-enter a chunk's execute while `prop._executing` is set, and this change touches only
    whether a USD attr gets authored -- not whether PARAM_CHANGED fires. A chunk assigning
    its own params already sent that notice before.

    Asserted over a run of edits rather than one, because a loop that settles need not run
    away on the first: it would show as the cost per edit climbing.
    """
    stage = _new_stage()
    scene = _scene_for(stage)
    chunk_obj = _add(scene, "random_value")

    module = chunk_registry.ChunkRegistry().get_chunk_module("random_value").module
    original = module.execute
    cooks = []

    def counted(node):
        cooks.append(node)
        return original(node)

    module.execute = counted
    try:
        per_edit = []
        for i in range(10):
            before = len(cooks)
            chunk_obj.get_param("seed").value = float(i + 1)
            per_edit.append(len(cooks) - before)
    finally:
        module.execute = original

    assert per_edit == [1] * 10, f"cooks per edit drifted: {per_edit}"
