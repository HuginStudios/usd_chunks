"""A gizmo on one chunk driving a param on another.

Not a nice-to-have. scripts/camera.py has no transform params at all -- a camera's position
comes from a separate upstream Transform chunk, or from lock_to_freecam -- so without this
a camera body gizmo cannot exist, and the camera case is half the reason the schema has the
shape it does.

Authoring costs nothing, because `driver` was already an attribute connection and a
connection can target any prim's property. The work is on the write-back side: resolving a
connection target back to the chunk that owns the param, and surviving that chunk being
deleted, disabled or renamed underneath us.
"""
from __future__ import annotations

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom

from usd_chunks import chunk, chunk_gizmo, chunk_registry
from usd_chunks.chunk_core import ChunkConfig, ChunkFlags, SceneSender
from usd_chunks.plugins.chunk_usd import (
    _CHUNKS_ROOT,
    _author_prim,
    ChunkStageSync,
)


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


chunk_registry.ChunkRegistry().initialize(_DummyScene(), "pytest")

TX_PATH = f"{_CHUNKS_ROOT}/Transform.inputs:translate_grp:tx"


def _scene():
    stage = Usd.Stage.CreateInMemory()
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        UsdGeom.Camera.Define(stage, "/World/Cam")

    class _StagedScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StagedScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    ChunkStageSync().initialize(stage, scene)
    return stage, scene


def _transform_chunk():
    """A Transform-shaped chunk: nested translate params, like the real one."""
    c = chunk.Chunk("Transform")
    c.add_param(chunk.LineEdit("primpath_location", default_value="/World/Cam"))
    grp = chunk.Group("translate_grp", "Translate")
    for axis in ("tx", "ty", "tz"):
        grp.add_param(chunk.Slider(axis, axis.upper(), min_value=-100.0, max_value=100.0, default_value=0.0))
    c.add_param(grp)
    c._config = ChunkConfig(module="transform")
    return c


def _camera_chunk(driver=None):
    """camera.py's real problem: a body handle with no transform params of its own."""
    c = chunk.Chunk("Camera")
    c.add_param(chunk.LineEdit("primpath_location", default_value="/World/Cam"))
    c.add_param(chunk.Slider("focusDistance", min_value=0.0, max_value=1000.0))
    c._config = ChunkConfig(module="camera", gizmos=[
        chunk.Gizmo("body", type="cube", anchor="primpath_location",
                    driver=driver if driver is not None else TX_PATH),
    ])
    return c


def _both():
    stage, scene = _scene()
    transform = _transform_chunk()
    scene.add_chunk(transform)
    camera = _camera_chunk()
    scene.add_chunk(camera)
    return stage, scene, transform, camera


# ---------------------------------------------------------------------------
# authoring
# ---------------------------------------------------------------------------

def test_a_cross_chunk_driver_authors_a_connection_to_the_other_prim() -> None:
    stage, scene, transform, camera = _both()
    _author_prim(stage, transform)
    _author_prim(stage, camera)

    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/Camera")
    conns = prim.GetAttribute("ui:gizmo:body:driver").GetConnections()
    assert [str(c) for c in conns] == [TX_PATH]


def test_the_connection_survives_a_round_trip_through_resolve() -> None:
    stage, scene, transform, camera = _both()
    _author_prim(stage, transform)
    _author_prim(stage, camera)

    desc = chunk_gizmo.resolve_gizmos(stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/Camera"))[0]
    assert [str(p) for p in desc.driver["value"]] == [TX_PATH]


def test_own_chunk_and_cross_chunk_slots_mix() -> None:
    """One handle can perfectly well drive some of its own params and some of another's."""
    stage, scene = _scene()
    transform = _transform_chunk()
    scene.add_chunk(transform)

    camera = _camera_chunk(driver={"value": ("focusDistance",), "translate": (TX_PATH,)})
    scene.add_chunk(camera)
    _author_prim(stage, transform)
    _author_prim(stage, camera)

    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/Camera")
    assert prim.GetAttribute("ui:gizmo:body:driver").GetConnections()
    assert prim.GetAttribute("ui:gizmo:body:driver:translate").GetConnections()


# ---------------------------------------------------------------------------
# write-back lands on the other chunk
# ---------------------------------------------------------------------------

def test_write_back_sets_the_param_on_the_owning_chunk() -> None:
    stage, scene, transform, camera = _both()

    chunk_gizmo.apply_bindings(camera, "body", {"value": 7.5})

    assert transform.get_param("tx").value == pytest.approx(7.5)


def test_the_gizmos_own_chunk_is_left_alone() -> None:
    stage, scene, transform, camera = _both()

    chunk_gizmo.apply_bindings(camera, "body", {"value": 7.5})

    assert camera.get_param("focusDistance").value != pytest.approx(7.5)


def test_a_cross_chunk_write_is_indistinguishable_from_setting_the_param() -> None:
    """The entire reason write-back goes through params instead of prims: whatever normally
    follows a param write -- dirtying, recook, downstream propagation -- follows this one
    too, because it *is* one. Asserted as equivalence rather than by reaching into the
    scheduler, so the test keeps meaning what it says if the scheduler changes."""
    stage, scene, transform, camera = _both()

    chunk_gizmo.apply_bindings(camera, "body", {"value": 3.0})
    via_gizmo = transform.get_param("tx").value

    transform.get_param("tx").value = 0.0
    transform.get_param("tx").value = 3.0

    assert via_gizmo == pytest.approx(transform.get_param("tx").value)


def test_a_mixed_drag_writes_both_chunks() -> None:
    stage, scene = _scene()
    transform = _transform_chunk()
    scene.add_chunk(transform)
    camera = _camera_chunk(driver={"value": ("focusDistance",), "translate": (TX_PATH,)})
    scene.add_chunk(camera)

    chunk_gizmo.apply_bindings(camera, "body", {"value": 12.0, "translate": 4.0})

    assert camera.get_param("focusDistance").value == pytest.approx(12.0)
    assert transform.get_param("tx").value == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# the target going away
# ---------------------------------------------------------------------------

def test_a_dangling_target_does_not_take_the_drag_down() -> None:
    """The target chunk can be deleted independently at any time; the handle is still good."""
    stage, scene = _scene()
    camera = _camera_chunk()  # no Transform chunk in the scene at all
    scene.add_chunk(camera)

    chunk_gizmo.apply_bindings(camera, "body", {"value": 1.0})  # must not raise


def test_a_dangling_slot_leaves_the_others_working() -> None:
    stage, scene = _scene()
    camera = _camera_chunk(driver={"value": ("focusDistance",), "translate": (TX_PATH,)})
    scene.add_chunk(camera)

    chunk_gizmo.apply_bindings(camera, "body", {"value": 9.0, "translate": 4.0})

    assert camera.get_param("focusDistance").value == pytest.approx(9.0)


def test_a_missing_param_on_a_live_chunk_is_also_survivable() -> None:
    stage, scene = _scene()
    transform = _transform_chunk()
    scene.add_chunk(transform)
    camera = _camera_chunk(driver=f"{_CHUNKS_ROOT}/Transform.inputs:nonexistent")
    scene.add_chunk(camera)

    chunk_gizmo.apply_bindings(camera, "body", {"value": 1.0})  # must not raise


def test_a_missing_own_param_still_raises() -> None:
    """The other half of the rule: a bare name that does not resolve is the chunk's own
    config naming a param it does not have, which is a declaration bug and should be loud."""
    stage, scene = _scene()
    camera = _camera_chunk(driver="nonexistent")
    scene.add_chunk(camera)

    with pytest.raises(ValueError, match="has no param"):
        chunk_gizmo.apply_bindings(camera, "body", {"value": 1.0})


def test_a_disabled_target_chunk_still_takes_the_write() -> None:
    """Documented rather than special-cased: the value lands on the param and the chunk cooks
    it whenever it is next enabled, which is how every other write to a disabled chunk
    behaves. Silently dropping it would make the handle lie about what it did."""
    stage, scene, transform, camera = _both()
    transform.set_state(ChunkFlags.State.DISABLED)

    chunk_gizmo.apply_bindings(camera, "body", {"value": 5.0})

    assert transform.get_param("tx").value == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# renames
# ---------------------------------------------------------------------------

def test_renaming_the_target_chunk_repoints_the_driver() -> None:
    """Chunk-prim renames already repoint every chunk-prim connection, and a gizmo driver is
    just another one -- so this needs no new code, but it does need proving: a dangling driver
    after a rename would look exactly like a broken gizmo.

    Runs inside the session layer because that is where chunk-prim bookkeeping is authored,
    and _fixup_connection_targets edits whatever the ambient target happens to be. Authoring
    the gizmo a second time into the root layer would leave the stronger session opinion in
    place and make this test quietly assert nothing.
    """
    stage, scene, transform, camera = _both()

    sync = ChunkStageSync()
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        sync._fixup_connection_targets(f"{_CHUNKS_ROOT}/Transform", f"{_CHUNKS_ROOT}/Moved")

    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/Camera")
    conns = [str(c) for c in prim.GetAttribute("ui:gizmo:body:driver").GetConnections()]
    assert conns == [f"{_CHUNKS_ROOT}/Moved.inputs:translate_grp:tx"]


def test_chunk_at_prim_path_finds_nothing_for_an_unknown_path() -> None:
    stage, scene, transform, camera = _both()
    assert chunk_gizmo.chunk_at_prim_path(Sdf.Path(f"{_CHUNKS_ROOT}/Nope")) is None


def test_chunk_at_prim_path_finds_the_chunk() -> None:
    stage, scene, transform, camera = _both()
    found = chunk_gizmo.chunk_at_prim_path(Sdf.Path(f"{_CHUNKS_ROOT}/Transform"))
    assert found is transform
