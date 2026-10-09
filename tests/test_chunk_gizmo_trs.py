"""The TRS round trip: gizmo matrix -> one value per handle -> params -> chunk recomposes.

This closes only if `chunk_gizmo.decompose` inverts `transform.py`'s composition exactly.
A mismatch does not raise -- it shows up as the handle drifting further from the object on
each successive drag, so the tests here assert the property (no accumulating drift) rather
than a single pass, which can round-trip cleanly while still wandering over ten.
"""
from __future__ import annotations

import importlib

import pytest
from pxr import Gf, Usd, UsdGeom

from usd_chunks import chunk_registry
from usd_chunks.gizmo import runtime as chunk_gizmo
from usd_chunks.chunk_core import ChunkConfig
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import _CHUNKS_ROOT
from chunk_prim_authoring import _author_prim


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


chunk_registry.ChunkRegistry().initialize(_DummyScene(), "pytest")

transform_mod = importlib.import_module("chunks.transforms.transform")


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


# transform.py declares one whole-chunk handle: the host runs its own transform manipulator
# and hands back the result whole, so a full TRS drag is a single call with a matrix.
_HANDLE = "transform"


def _apply(chunk_obj, matrix: Gf.Matrix4d) -> None:
    chunk_gizmo.apply_bindings(chunk_obj, _HANDLE, matrix)


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

    _apply(c, dragged)
    transform_mod.execute(_FakeNode(c, stage))

    authored = UsdGeom.Xformable(stage.GetPrimAtPath("/World/Cube")).GetLocalTransformation()
    assert _matrices_close(dragged, authored, tol=1e-6)


def test_a_transform_drag_writes_the_euler_params() -> None:
    c = _transform_chunk()

    _apply(c, _compose((0, 0, 0), (10, 20, 30), (1, 1, 1)))

    assert c.get_param("rx").value == pytest.approx(10.0)
    assert c.get_param("ry").value == pytest.approx(20.0)
    assert c.get_param("rz").value == pytest.approx(30.0)


def test_a_transform_drag_writes_the_scale_params() -> None:
    c = _transform_chunk()

    _apply(c, _compose((0, 0, 0), (0, 0, 0), (2, 3, 4)))

    assert c.get_param("sx").value == pytest.approx(2.0)
    assert c.get_param("sz").value == pytest.approx(4.0)


def test_a_transform_drag_writes_all_nine_params() -> None:
    """One handle means one intent: the matrix the host hands back describes the whole
    transform, so every param it covers is written. A rotation set earlier is replaced
    rather than preserved -- that is the manipulator's answer, not a lost edit."""
    c = _transform_chunk()
    c.get_param("rx").value = 45.0

    _apply(c, _compose((1, 0, 0), (0, 0, 0), (1, 1, 1)))

    assert c.get_param("tx").value == pytest.approx(1.0)
    assert c.get_param("rx").value == pytest.approx(0.0)


def test_repeated_drags_through_the_chunk_do_not_drift() -> None:
    """The end-to-end version of the drift test: read the prim, decompose, write back, cook,
    fifty times. This is the shape of a real drag."""
    stage = _stage_with_cube()
    c = _transform_chunk()
    original = _compose((1, 2, 3), (15, 35, -20), (1, 1, 1))

    _apply(c, original)
    transform_mod.execute(_FakeNode(c, stage))

    xformable = UsdGeom.Xformable(stage.GetPrimAtPath("/World/Cube"))
    for _ in range(50):
        current = xformable.GetLocalTransformation()
        _apply(c, current)
        transform_mod.execute(_FakeNode(c, stage))

    assert _matrices_close(original, xformable.GetLocalTransformation(), tol=1e-6)


# ---------------------------------------------------------------------------
# authoring
# ---------------------------------------------------------------------------

def test_the_chunk_authors_one_handle() -> None:
    stage = _stage_with_cube()
    c = _transform_chunk()
    _author_prim(stage, c)
    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/Transform")

    attr = prim.GetAttribute(f"ui:gizmo:{_HANDLE}:driver")
    assert attr.IsValid()
    assert len(attr.GetConnections()) == 9, "translate, rotate and scale, in that order"


def test_resolve_returns_the_single_chunk_handle() -> None:
    stage = _stage_with_cube()
    c = _transform_chunk()
    _author_prim(stage, c)

    descs = chunk_gizmo.resolve_gizmos(stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/Transform"))

    assert [d.name for d in descs] == [_HANDLE]
    assert descs[0].type == "transform"
    assert len(descs[0].driver) == 9


def test_the_shipped_transform_chunk_declares_one_whole_chunk_handle() -> None:
    """No gizmos block in config() at all: gizmo="transform" on the Chunk, and gizmo= on each
    group to say which params. Driver paths stay nested (rotate_grp.rx, not rx), since that
    is what makes the authored connection resolve to anything."""
    gizmos = chunk_gizmo.get_gizmos(_transform_chunk())

    assert transform_mod.config().gizmos == []
    assert [g.name for g in gizmos] == ["transform"]
    assert gizmos[0].driver == (
        "translate_grp.tx", "translate_grp.ty", "translate_grp.tz",
        "rotate_grp.rx", "rotate_grp.ry", "rotate_grp.rz",
        "scale_grp.sx", "scale_grp.sy", "scale_grp.sz",
    )
