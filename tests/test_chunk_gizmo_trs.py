"""The TRS round trip: gizmo matrix -> one value per handle -> params -> chunk recomposes.

This closes only if `chunk_gizmo.decompose` inverts `transform.py`'s composition exactly.
A mismatch does not raise -- it shows up as the handle drifting further from the object on
each successive drag, so the tests here assert the property (no accumulating drift) rather
than a single pass, which can round-trip cleanly while still wandering over ten.
"""
from __future__ import annotations

import importlib
import math

import pytest
from pxr import Gf, Usd, UsdGeom

from usd_chunks import chunk, chunk_gizmo, chunk_registry
from usd_chunks.chunk_core import ChunkConfig, ChunkFlags, SceneSender
from usd_chunks.plugins.chunk_usd import _CHUNKS_ROOT, _author_prim


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


chunk_registry.ChunkRegistry().initialize(_DummyScene(), "pytest")

transform_mod = importlib.import_module("scripts.transform")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _compose(translate, rotate, scale) -> Gf.Matrix4d:
    """Exactly what transform.py's execute() builds, so the tests cannot drift from it by
    quietly reimplementing a different convention."""
    rotation = (
        Gf.Rotation(Gf.Vec3d(1, 0, 0), rotate[0])
        * Gf.Rotation(Gf.Vec3d(0, 1, 0), rotate[1])
        * Gf.Rotation(Gf.Vec3d(0, 0, 1), rotate[2])
    )
    xform = Gf.Transform()
    xform.SetTranslation(Gf.Vec3d(*translate))
    xform.SetRotation(rotation)
    xform.SetScale(Gf.Vec3d(*[v or 1.0 for v in scale]))
    return xform.GetMatrix()


def _matrices_close(a: Gf.Matrix4d, b: Gf.Matrix4d, tol: float = 1e-7) -> bool:
    return all(abs(a[i][j] - b[i][j]) < tol for i in range(4) for j in range(4))


def _recompose(slots: dict) -> Gf.Matrix4d:
    return _compose(slots["translate"], slots["rotate"], slots["scale"])


# decompose() is keyed by gizmo *type*, so this maps each part of a matrix onto the handle
# that owns it. transform.py declares three separate gizmos, one per param group, so a full
# TRS drag is three calls -- which is the viewport's shape anyway: you grab the arrows or
# the ring, never a single thing that is somehow both.
_HANDLES = {"translate": "translate_grp", "rotate": "rotate_grp", "scale": "scale_grp"}


def _apply(chunk_obj, parts: dict) -> None:
    for gizmo_type, value in parts.items():
        chunk_gizmo.apply_bindings(chunk_obj, _HANDLES[gizmo_type], value)


CASES = [
    ((0, 0, 0), (0, 0, 0), (1, 1, 1)),
    ((1, -2, 3), (10, 20, 30), (1, 1, 1)),
    ((0, 0, 0), (-45, 80, 15), (2, 0.5, 3)),
    ((5, 0, -2), (179, -12, 44), (1, 1, 1)),
    ((0, 0, 0), (12, -89.9, 177), (1, 1, 1)),
    ((-7, 7, 0), (0, 0, 90), (0.25, 4, 1)),
]


# ---------------------------------------------------------------------------
# decompose
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("translate, rotate, scale", CASES)
def test_decompose_recovers_the_original_params(translate, rotate, scale) -> None:
    slots = chunk_gizmo.decompose(_compose(translate, rotate, scale))

    assert slots["translate"] == pytest.approx(Gf.Vec3d(*translate), abs=1e-9)
    assert slots["rotate"] == pytest.approx(Gf.Vec3d(*rotate), abs=1e-6)
    assert slots["scale"] == pytest.approx(Gf.Vec3d(*scale), abs=1e-9)


@pytest.mark.parametrize("translate, rotate, scale", CASES)
def test_matrix_round_trips(translate, rotate, scale) -> None:
    original = _compose(translate, rotate, scale)
    assert _matrices_close(original, _recompose(chunk_gizmo.decompose(original)))


@pytest.mark.parametrize("translate, rotate, scale", CASES)
def test_repeated_application_does_not_drift(translate, rotate, scale) -> None:
    """The failure that matters. A single pass can round-trip cleanly while still wandering
    a fraction of a degree each time -- which is exactly what a user feels as the handle
    sliding away from the object over a few drags."""
    original = _compose(translate, rotate, scale)

    matrix = original
    for _ in range(50):
        matrix = _recompose(chunk_gizmo.decompose(matrix))

    assert _matrices_close(original, matrix, tol=1e-6)


def test_gimbal_lock_is_stable_rather_than_exact() -> None:
    """At ry = +-90 the X and Z rotations act about the same world axis, so the split between
    them is genuinely arbitrary. The angles need not come back as authored -- but the matrix
    must, and repeating must not wander."""
    original = _compose((0, 0, 0), (30, 90, 45), (1, 1, 1))

    first = chunk_gizmo.decompose(original)
    assert _matrices_close(original, _recompose(first))
    assert first["rotate"][2] == pytest.approx(0.0, abs=1e-9), "Z is folded into X by convention"

    second = chunk_gizmo.decompose(_recompose(first))
    assert second["rotate"] == pytest.approx(first["rotate"], abs=1e-9)


def test_negative_scale_round_trips_the_matrix_not_the_params() -> None:
    """Gf.Transform cannot tell a negative scale on one axis from a rotation plus a negative
    scale on all three -- they are the same matrix. Worth pinning down: the params coming
    back differently is correct behaviour here, not a decomposition bug."""
    original = _compose((0, 0, 0), (30, 0, 0), (1, -1, 1))
    slots = chunk_gizmo.decompose(original)

    assert _matrices_close(original, _recompose(slots))
    assert slots["scale"] != pytest.approx(Gf.Vec3d(1, -1, 1))


def test_decompose_needs_no_stage_or_qt() -> None:
    """Pure Gf, so it is unit-testable in isolation and usable by a non-Qt host."""
    slots = chunk_gizmo.decompose(Gf.Matrix4d(1))

    assert slots["translate"] == Gf.Vec3d(0, 0, 0)
    assert slots["scale"] == pytest.approx(Gf.Vec3d(1, 1, 1))


def test_falsy_scale_param_means_one_not_zero() -> None:
    """execute() reads `sx or 1.0`, so a zero scale param composes as 1.0. A decomposition
    that returned 0.0 here would collapse the prim on the next cook."""
    matrix = _compose((0, 0, 0), (0, 0, 0), (0, 0, 0))
    assert chunk_gizmo.decompose(matrix)["scale"] == pytest.approx(Gf.Vec3d(1, 1, 1))


# ---------------------------------------------------------------------------
# the full drag, through params, into the cook
# ---------------------------------------------------------------------------

def _transform_chunk(target: str = "/World/Cube"):
    c = transform_mod.main()
    c._config = ChunkConfig(module="transform", gizmos=transform_mod.config().gizmos)
    c.get_param("primpath_location").value = target
    return c


class _FakeNode:
    def __init__(self, chunk_obj, stage):
        self._chunk = chunk_obj
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_param(self, name):
        return self._chunk.get_param(name)


def _stage_with_cube(path: str = "/World/Cube") -> Usd.Stage:
    """Cube in the session layer: execute() clears whatever layer is the edit target, so a
    cube in the root layer would be wiped by the cook these tests are trying to observe.

    Also points the registry's scene at this stage. transform.py resolves its targets through
    `param.resolve_paths()`, which reads `get_stage()` off the scene -- with the default
    stageless scene it silently resolves to no targets, the cook authors nothing, and the
    assertion fails against an identity matrix for reasons that look nothing like the cause.
    """
    stage = Usd.Stage.CreateInMemory()
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        UsdGeom.Cube.Define(stage, path)

    class _StagedScene(_DummyScene):
        def get_stage(self):
            return stage

    chunk_registry.ChunkRegistry().initialize(_StagedScene(), "pytest")
    return stage


def test_full_trs_drag_lands_as_the_same_matrix() -> None:
    """A host hands over a matrix; the prim ends up carrying that matrix, having gone through
    nine float params on the way."""
    stage = _stage_with_cube()
    c = _transform_chunk()
    dragged = _compose((3, -1, 2), (25, -40, 65), (2, 2, 0.5))

    _apply(c, chunk_gizmo.decompose(dragged))
    transform_mod.execute(_FakeNode(c, stage))

    authored = UsdGeom.Xformable(stage.GetPrimAtPath("/World/Cube")).GetLocalTransformation()
    assert _matrices_close(dragged, authored, tol=1e-6)


def test_rotate_slot_writes_the_euler_params() -> None:
    c = _transform_chunk()

    chunk_gizmo.apply_bindings(c, "rotate_grp", Gf.Vec3d(10.0, 20.0, 30.0))

    assert c.get_param("rx").value == pytest.approx(10.0)
    assert c.get_param("ry").value == pytest.approx(20.0)
    assert c.get_param("rz").value == pytest.approx(30.0)


def test_scale_slot_writes_the_scale_params() -> None:
    c = _transform_chunk()

    chunk_gizmo.apply_bindings(c, "scale_grp", Gf.Vec3d(2.0, 3.0, 4.0))

    assert c.get_param("sx").value == pytest.approx(2.0)
    assert c.get_param("sz").value == pytest.approx(4.0)


def test_partial_slot_dicts_leave_other_params_alone() -> None:
    """A host dragging a translate-only handle must not silently zero the rotation."""
    c = _transform_chunk()
    c.get_param("rx").value = 45.0

    chunk_gizmo.apply_bindings(c, "translate_grp", Gf.Vec3d(1.0, 0.0, 0.0))

    assert c.get_param("rx").value == pytest.approx(45.0)


def test_repeated_drags_through_the_chunk_do_not_drift() -> None:
    """The end-to-end version of the drift test: read the prim, decompose, write back, cook,
    fifty times. This is the shape of a real drag."""
    stage = _stage_with_cube()
    c = _transform_chunk()
    original = _compose((1, 2, 3), (15, 35, -20), (1, 1, 1))

    _apply(c, chunk_gizmo.decompose(original))
    transform_mod.execute(_FakeNode(c, stage))

    xformable = UsdGeom.Xformable(stage.GetPrimAtPath("/World/Cube"))
    for _ in range(50):
        current = xformable.GetLocalTransformation()
        _apply(c, chunk_gizmo.decompose(current))
        transform_mod.execute(_FakeNode(c, stage))

    assert _matrices_close(original, xformable.GetLocalTransformation(), tol=1e-6)


# ---------------------------------------------------------------------------
# authoring
# ---------------------------------------------------------------------------

def test_each_group_authors_its_own_handle() -> None:
    stage = _stage_with_cube()
    c = _transform_chunk()
    _author_prim(stage, c)
    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/Transform")

    for handle in _HANDLES.values():
        attr = prim.GetAttribute(f"ui:gizmo:{handle}:driver")
        assert attr.IsValid(), handle
        assert len(attr.GetConnections()) == 3, handle


def test_resolve_returns_one_handle_per_group() -> None:
    stage = _stage_with_cube()
    c = _transform_chunk()
    _author_prim(stage, c)

    descs = chunk_gizmo.resolve_gizmos(stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/Transform"))
    by_name = {d.name: d for d in descs}

    assert set(by_name) == set(_HANDLES.values())
    for gizmo_type, handle in _HANDLES.items():
        assert by_name[handle].type == gizmo_type
        assert len(by_name[handle].driver) == 3


def test_the_shipped_transform_chunk_declares_its_handles_on_the_groups() -> None:
    """No gizmos block in config() at all -- the declaration is one kwarg per group, which is
    the whole point of the group form. Driver paths stay nested (rotate_grp.rx, not rx),
    since that is what makes the authored connection resolve to anything."""
    gizmos = {g.name: g for g in chunk_gizmo.get_gizmos(_transform_chunk())}

    assert transform_mod.config().gizmos == []
    assert set(gizmos) == {"translate_grp", "rotate_grp", "scale_grp"}
    assert gizmos["rotate_grp"].driver == ("rotate_grp.rx", "rotate_grp.ry", "rotate_grp.rz")
    assert gizmos["scale_grp"].driver == ("scale_grp.sx", "scale_grp.sy", "scale_grp.sz")
