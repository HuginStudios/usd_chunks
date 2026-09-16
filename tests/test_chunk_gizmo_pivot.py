"""The Transform chunk's pivot: what a rotation turns about, and where the handle sits.

Three separate promises, and each has its own failure mode:

* **Composition** -- the pivot is a fixed point of rotate and scale. A wrong sign here does
  not raise; the object swings around the wrong point, which reads as "rotate is broken".
* **Write-back** -- a drag reported by the host has to come back through nine params and
  recompose to the *same* matrix. Solving without the pivot leaves the object short of the
  mouse by exactly how far the pivot swings, which reads as lag.
* **Moving the pivot moves nothing** -- the pivot says what a rotation turns about, it is
  not itself a placement. This is the promise a user notices immediately when it breaks,
  because their prop teleports.

The last one is also the migration guarantee: a scene saved before pivots existed loads with
a zero pivot, and nothing about it changes until somebody sets one.
"""
from __future__ import annotations

import importlib

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom

from usd_chunks import chunk_gizmo, chunk_registry
from usd_chunks.chunk_core import ChunkConfig, SceneSender
from usd_chunks.plugins.chunk_usd import _CHUNKS_ROOT, _author_prim


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


chunk_registry.ChunkRegistry().initialize(_DummyScene(), "pytest")

transform_mod = importlib.import_module("scripts.transforms.transform")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _compose(translate, rotate, scale, pivot=None) -> Gf.Matrix4d:
    """Exactly what transform.py's execute() builds, so these tests cannot drift from it by
    quietly reimplementing a different convention."""
    xform = Gf.Transform()
    xform.SetTranslation(Gf.Vec3d(*translate))
    xform.SetRotation(
        Gf.Rotation(Gf.Vec3d(1, 0, 0), rotate[0])
        * Gf.Rotation(Gf.Vec3d(0, 1, 0), rotate[1])
        * Gf.Rotation(Gf.Vec3d(0, 0, 1), rotate[2])
    )
    xform.SetScale(Gf.Vec3d(*[v or 1.0 for v in scale]))
    if pivot is not None:
        xform.SetPivotPosition(Gf.Vec3d(*pivot))
    return xform.GetMatrix()


def _matrices_close(a: Gf.Matrix4d, b: Gf.Matrix4d, tol: float = 1e-7) -> bool:
    return all(abs(a[i][j] - b[i][j]) < tol for i in range(4) for j in range(4))


def _stage_with_cube(path: str = "/World/Cube") -> Usd.Stage:
    """Cube in the session layer: execute() clears whatever layer is the edit target, so a
    cube in the root layer would be wiped by the cook these tests are trying to observe."""
    stage = Usd.Stage.CreateInMemory()
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        UsdGeom.Cube.Define(stage, path)

    class _StagedScene(_DummyScene):
        def get_stage(self):
            return stage

    chunk_registry.ChunkRegistry().initialize(_StagedScene(), "pytest")
    return stage


def _transform_chunk(target: str = "/World/Cube"):
    c = transform_mod.main()
    c._config = ChunkConfig(module="transform", gizmos=getattr(transform_mod.config(), "gizmos", None) or [])
    # The registry's copy of the module is what the hooks are looked up on. Setting it is
    # what makes gizmo_changed / gizmo_offset dispatch at all -- without it the write-back
    # silently falls through to the declarative path and the pivot is never consulted.
    c._module = transform_mod
    c.get_param("primpath_location").value = target
    return c


class _FakeNode:
    """What execute() gets in a real session: something with a stage and params.

    Attribute writes land on the chunk rather than on the wrapper, because that is where
    they land in production -- execute()'s node *is* the chunk. A wrapper that swallowed
    them would hide the pivot bookkeeping from every other code path.
    """

    def __init__(self, chunk_obj, stage):
        object.__setattr__(self, "_chunk", chunk_obj)
        object.__setattr__(self, "_stage", stage)

    def get_stage(self):
        return self._stage

    def get_param(self, name):
        return self._chunk.get_param(name)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_chunk"), name)

    def __setattr__(self, name, value):
        setattr(self._chunk, name, value)


def _set(chunk_obj, **values) -> None:
    for name, value in values.items():
        chunk_obj.get_param(name).value = float(value)


def _authored_local(chunk_obj, stage, path: str = "/World/Cube") -> Gf.Matrix4d:
    transform_mod.execute(_FakeNode(chunk_obj, stage))
    return UsdGeom.Xformable(stage.GetPrimAtPath(path)).GetLocalTransformation()


# ---------------------------------------------------------------------------
# decompose, solved about a pivot
# ---------------------------------------------------------------------------

PIVOTS = [(0, 2, 0), (1, -3, 4), (-10, 0, 0.5)]
CASES = [
    ((0, 0, 0), (0, 0, 45), (1, 1, 1)),
    ((3, -1, 2), (10, 20, 30), (2, 3, 4)),
    ((-7, 7, 0), (0, 0, 90), (0.25, 4, 1)),
    ((5, 0, -2), (179, -12, 44), (1, 1, 1)),
]


@pytest.mark.parametrize("translate, rotate, scale", CASES)
@pytest.mark.parametrize("pivot", PIVOTS)
def test_decompose_about_a_pivot_recovers_the_params(translate, rotate, scale, pivot) -> None:
    slots = chunk_gizmo.decompose(_compose(translate, rotate, scale, pivot), pivot=pivot)

    assert slots["translate"] == pytest.approx(Gf.Vec3d(*translate), abs=1e-6)
    assert slots["rotate"] == pytest.approx(Gf.Vec3d(*rotate), abs=1e-6)
    assert slots["scale"] == pytest.approx(Gf.Vec3d(*scale), abs=1e-9)


@pytest.mark.parametrize("translate, rotate, scale", CASES)
@pytest.mark.parametrize("pivot", PIVOTS)
def test_pivoted_round_trip_does_not_drift(translate, rotate, scale, pivot) -> None:
    """A single pass can round-trip cleanly while still wandering a fraction each time --
    which is what a user feels as the handle sliding away from the object over a few drags."""
    original = _compose(translate, rotate, scale, pivot)

    matrix = original
    for _ in range(50):
        slots = chunk_gizmo.decompose(matrix, pivot=pivot)
        matrix = _compose(slots["translate"], slots["rotate"], slots["scale"], pivot)

    assert _matrices_close(original, matrix, tol=1e-6)


def test_decompose_without_a_pivot_is_unchanged() -> None:
    """Every caller predating pivots passes no pivot and must get exactly what it got before."""
    matrix = _compose((3, -1, 2), (10, 20, 30), (2, 3, 4))
    assert chunk_gizmo.decompose(matrix) == chunk_gizmo.decompose(matrix, pivot=None)


def test_a_zero_pivot_decomposes_like_no_pivot() -> None:
    matrix = _compose((3, -1, 2), (10, 20, 30), (2, 3, 4))
    plain = chunk_gizmo.decompose(matrix)
    zeroed = chunk_gizmo.decompose(matrix, pivot=(0, 0, 0))

    assert zeroed["translate"] == pytest.approx(plain["translate"], abs=1e-12)


# ---------------------------------------------------------------------------
# what the pivot means when the chunk cooks
# ---------------------------------------------------------------------------

def test_rotating_about_a_pivot_leaves_the_pivot_where_it_is() -> None:
    """The whole promise, stated as geometry: the pivot is a fixed point of the rotation."""
    stage = _stage_with_cube()
    c = _transform_chunk()
    _set(c, px=0, py=2, pz=0, rx=0, ry=0, rz=90)

    local = _authored_local(c, stage)

    assert local.Transform(Gf.Vec3d(0, 2, 0)) == pytest.approx(Gf.Vec3d(0, 2, 0), abs=1e-9)


def test_without_a_pivot_the_origin_is_the_fixed_point() -> None:
    stage = _stage_with_cube()
    c = _transform_chunk()
    _set(c, rx=0, ry=0, rz=90)

    local = _authored_local(c, stage)

    assert local.Transform(Gf.Vec3d(0, 0, 0)) == pytest.approx(Gf.Vec3d(0, 0, 0), abs=1e-9)


def test_translate_still_moves_the_whole_object_pivot_included() -> None:
    """A pivot is a fixed point of rotate and scale, never of translate -- otherwise the
    translate sliders would stop meaning what they read."""
    stage = _stage_with_cube()
    c = _transform_chunk()
    _set(c, px=0, py=2, pz=0, tx=5)

    local = _authored_local(c, stage)

    assert local.Transform(Gf.Vec3d(0, 2, 0)) == pytest.approx(Gf.Vec3d(5, 2, 0), abs=1e-9)


def test_an_untouched_pivot_authors_the_same_matrix_as_before() -> None:
    """Migration, and the reason zero means unset: an existing scene composes identically."""
    stage = _stage_with_cube()
    c = _transform_chunk()
    _set(c, tx=3, ty=-1, tz=2, rx=10, ry=20, rz=30, sx=2, sy=3, sz=4)

    local = _authored_local(c, stage)

    assert _matrices_close(local, _compose((3, -1, 2), (10, 20, 30), (2, 3, 4)), tol=1e-9)


# ---------------------------------------------------------------------------
# the drag, end to end
# ---------------------------------------------------------------------------

def test_a_drag_lands_on_the_dragged_matrix_with_a_pivot_set() -> None:
    """The host reports where the object should end up. With a pivot in play the params that
    come back have to recompose to exactly that, or the object trails the mouse."""
    stage = _stage_with_cube()
    c = _transform_chunk()
    _set(c, px=1, py=-3, pz=4)
    dragged = _compose((3, -1, 2), (25, -40, 65), (2, 2, 0.5))

    chunk_gizmo.apply_bindings(c, "transform", dragged)
    local = _authored_local(c, stage)

    assert _matrices_close(dragged, local, tol=1e-6)


def test_a_drag_ignoring_the_pivot_would_miss() -> None:
    """Guards the test above from passing for the wrong reason: with a rotation in the drag,
    the pivot correction is not a no-op, so a solve that dropped it would land elsewhere."""
    dragged = _compose((3, -1, 2), (25, -40, 65), (2, 2, 0.5))

    solved = chunk_gizmo.decompose(dragged, pivot=(1, -3, 4))
    naive = chunk_gizmo.decompose(dragged)

    assert solved["translate"] != pytest.approx(naive["translate"], abs=1e-3)


# ---------------------------------------------------------------------------
# moving the pivot must not move the object
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rotate, scale", [
    ((0, 0, 0), (1, 1, 1)),
    ((0, 0, 90), (1, 1, 1)),
    ((10, 20, 30), (2, 3, 4)),
    ((179, -12, 44), (0.25, 4, 1)),
])
def test_moving_the_pivot_leaves_the_object_exactly_where_it_was(rotate, scale) -> None:
    stage = _stage_with_cube()
    c = _transform_chunk()
    _set(c, tx=3, ty=-1, tz=2,
         rx=rotate[0], ry=rotate[1], rz=rotate[2],
         sx=scale[0], sy=scale[1], sz=scale[2])
    before = _authored_local(c, stage)

    transform_mod._set_pivot(c, (5, -3, 1))
    after = _authored_local(c, stage)

    assert _matrices_close(before, after, tol=1e-6)


def test_moving_the_pivot_twice_still_lands_in_the_same_place() -> None:
    """Compensation is relative to the pivot last composed with, so a second move that
    measured from zero again would shunt the object by the first move's worth."""
    stage = _stage_with_cube()
    c = _transform_chunk()
    _set(c, tx=3, ty=-1, tz=2, rx=10, ry=20, rz=30, sx=2, sy=3, sz=4)
    before = _authored_local(c, stage)

    transform_mod._set_pivot(c, (5, -3, 1))
    transform_mod._set_pivot(c, (-2, 8, 0.5))
    after = _authored_local(c, stage)

    assert _matrices_close(before, after, tol=1e-6)
    assert transform_mod.pivot(c) == pytest.approx((-2, 8, 0.5))


def test_setting_the_pivot_back_to_zero_restores_the_original_params() -> None:
    stage = _stage_with_cube()
    c = _transform_chunk()
    _set(c, tx=3, ty=-1, tz=2, rx=10, ry=20, rz=30)
    before = _authored_local(c, stage)

    transform_mod._set_pivot(c, (5, -3, 1))
    transform_mod._set_pivot(c, (0, 0, 0))

    assert transform_mod.pivot(c) is None
    assert _matrices_close(before, _authored_local(c, stage), tol=1e-6)
    assert c.get_param("tx").value == pytest.approx(3, abs=1e-6)


def test_a_pivot_move_places_the_object_once() -> None:
    """Applying a move writes all three pivot params, and each write fires the callback that
    started it. Unguarded that ran four compensations, re-placing the object three times on
    the way -- and every one of those cooks, so it visibly jumps before settling.

    Counted as writes to translate rather than as calls, because the guarded calls still
    happen; what must not happen is each of them moving the object. Nothing else here catches
    this: the correction telescopes, so the final answer was right either way.
    """
    stage = _stage_with_cube()
    c = _transform_chunk()
    _set(c, tx=3, rx=10, ry=20, rz=30, sx=2, sy=3, sz=4)
    _authored_local(c, stage)

    placements = []
    c.get_param("tx").add_callback(
        lambda owner, notice, sender: placements.append(owner.value), send_on_create=False
    )

    transform_mod._set_pivot(c, (5.0, -3.0, 1.0))

    assert len(placements) == 1, f"the object was re-placed {len(placements)} times"
    assert transform_mod.pivot(c) == pytest.approx((5.0, -3.0, 1.0))


def test_the_pivot_slider_callback_compensates() -> None:
    """The path a user actually takes -- typing into the slider, not calling _set_pivot."""
    stage = _stage_with_cube()
    c = _transform_chunk()
    _set(c, tx=3, rx=10, ry=20, rz=30)
    _authored_local(c, stage)          # first cook, which is what seeds the remembered pivot
    before = _authored_local(c, stage)

    c.get_param("py").value = 4.0

    assert transform_mod.pivot(c) == pytest.approx((0.0, 4.0, 0.0))
    assert _matrices_close(before, _authored_local(c, stage), tol=1e-6)


def test_a_reloaded_scene_compensates_from_its_saved_pivot_not_from_zero() -> None:
    """Param restore on load deliberately does not fire callbacks, so the remembered pivot is
    seeded by the first cook instead. Miss that and the first pivot edit after a reload
    shunts the object by the whole saved pivot."""
    stage = _stage_with_cube()
    c = _transform_chunk()
    _set(c, tx=3, ty=-1, tz=2, rx=10, ry=20, rz=30, px=5, py=-3, pz=1)
    # Load restores values in bulk and fires no callbacks, so a reloaded chunk holds its saved
    # translate and remembers no pivot at all. Writing the params here does fire them, so put
    # translate back and forget the pivot -- otherwise this tests the path it means to skip.
    _set(c, tx=3, ty=-1, tz=2)
    delattr(c, transform_mod._PIVOT_APPLIED)
    assert not hasattr(c, transform_mod._PIVOT_APPLIED)

    before = _authored_local(c, stage)
    assert getattr(c, transform_mod._PIVOT_APPLIED) == pytest.approx((5.0, -3.0, 1.0))

    transform_mod._set_pivot(c, (6, -3, 1))

    assert _matrices_close(before, _authored_local(c, stage), tol=1e-6)


# ---------------------------------------------------------------------------
# centering
# ---------------------------------------------------------------------------

def _offset_cube(stage, path: str, translate) -> None:
    """A cube whose geometry does not straddle its own origin, so the centroid is not zero
    and a test cannot pass by accident."""
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        cube = UsdGeom.Cube.Define(stage, path)
        cube.CreateSizeAttr(2.0)
        UsdGeom.Xformable(cube).AddTranslateOp().Set(Gf.Vec3d(*translate))


def test_center_pivot_lands_on_the_untransformed_centroid() -> None:
    """The prim's *own* space, not the world: an appended op is the innermost one, so this
    chunk composes before the asset's own translate. A world centroid would be off by it."""
    stage = _stage_with_cube()
    _offset_cube(stage, "/World/Offset", (100, 0, 0))
    c = _transform_chunk("/World/Offset")
    _authored_local(c, stage, "/World/Offset")

    transform_mod._center_pivot(c.get_param("center_pivot"), None, None)

    # A cube straddles its own origin, so its untransformed centroid is zero -- which is
    # exactly the point: the 100-unit translate on the prim is not this chunk's to see.
    assert [c.get_param(name).value for name in transform_mod._PIVOT_PARAMS] == \
        pytest.approx([0.0, 0.0, 0.0])


def test_center_pivot_does_not_move_the_object() -> None:
    stage = _stage_with_cube()
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        mesh = UsdGeom.Mesh.Define(stage, "/World/Slab")
        mesh.CreatePointsAttr([Gf.Vec3f(0, 0, 0), Gf.Vec3f(4, 2, 6)])
        mesh.CreateExtentAttr([Gf.Vec3f(0, 0, 0), Gf.Vec3f(4, 2, 6)])
    c = _transform_chunk("/World/Slab")
    _set(c, tx=3, rx=10, ry=20, rz=30, sx=2, sy=3, sz=4)
    before = _authored_local(c, stage, "/World/Slab")

    transform_mod._center_pivot(c.get_param("center_pivot"), None, None)

    assert transform_mod.pivot(c) == pytest.approx((2.0, 1.0, 3.0))
    assert _matrices_close(before, _authored_local(c, stage, "/World/Slab"), tol=1e-6)


def test_center_pivot_on_no_target_does_nothing() -> None:
    _stage_with_cube()
    c = _transform_chunk("")

    transform_mod._center_pivot(c.get_param("center_pivot"), None, None)

    assert transform_mod.pivot(c) is None


# ---------------------------------------------------------------------------
# the handle follows the pivot
# ---------------------------------------------------------------------------

#: Prim handles expire across the layer.Clear() every cook starts with, so these carry the
#: path and re-fetch. The offset attribute itself has to be re-read for the same reason.
_CHUNK_PATH = Sdf.Path(f"{_CHUNKS_ROOT}/Transform")
_OFFSET_ATTR = "ui:gizmo:transform:offset"


def _author_chunk_prim(stage, chunk_obj) -> None:
    """Into the session layer, because a cook clears the root layer and would take the chunk
    prim with it -- and a cook runs on every param write these tests make. In a session the
    two really are different layers (bookkeeping versus the chunk's own output), so this
    mirrors production rather than dodging it."""
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        _author_prim(stage, chunk_obj)


def _authored_offset(stage):
    attr = stage.GetPrimAtPath(_CHUNK_PATH).GetAttribute(_OFFSET_ATTR)
    return attr.Get() if attr and attr.IsValid() else None


def _refresh(stage, chunk_obj) -> None:
    """What the param-changed path does in a real session, on every value the user moves."""
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        chunk_gizmo.refresh_gizmo_anchors(stage, stage.GetPrimAtPath(_CHUNK_PATH), chunk_obj)


def _resolved_transform_desc(stage, chunk_obj):
    """The record a host reads, resolved off the chunk prim.

    Reading from the *scene* prim is the usual route and is covered elsewhere; it needs a
    scene that registers chunks, which is machinery this file has no reason to stand up. The
    offset travels the same last step either way.
    """
    _author_chunk_prim(stage, chunk_obj)
    return next(d for d in chunk_gizmo.resolve_gizmos(stage.GetPrimAtPath(_CHUNK_PATH))
                if d.type == "transform")


def test_no_pivot_authors_no_offset_so_the_host_places_the_handle() -> None:
    stage = _stage_with_cube()
    c = _transform_chunk()

    desc = _resolved_transform_desc(stage, c)

    assert _authored_offset(stage) is None
    assert desc.offset is None


def test_a_pivot_is_authored_as_the_handle_offset() -> None:
    stage = _stage_with_cube()
    c = _transform_chunk()
    _set(c, px=1, py=-3, pz=4)

    desc = _resolved_transform_desc(stage, c)

    assert desc.offset == pytest.approx((1.0, -3.0, 4.0))


def test_zeroing_the_pivot_clears_the_offset_rather_than_leaving_it_stale() -> None:
    """A handle stuck at a pivot the user has since zeroed is worse than one that never moved."""
    stage = _stage_with_cube()
    c = _transform_chunk()
    _set(c, px=1, py=-3, pz=4)
    _author_chunk_prim(stage, c)
    assert _authored_offset(stage) is not None

    _set(c, px=0, py=0, pz=0)
    _refresh(stage, c)

    assert _authored_offset(stage) is None


def test_the_offset_follows_the_pivot_without_a_re_author() -> None:
    """A pivot is dragged, so the handle has to move within the same gesture -- the refresh
    that hangs off every param change is what makes that happen."""
    stage = _stage_with_cube()
    c = _transform_chunk()
    _set(c, px=1, py=-3, pz=4)
    _author_chunk_prim(stage, c)

    _set(c, px=8)
    _refresh(stage, c)

    assert _authored_offset(stage) == pytest.approx(Gf.Vec3f(8, -3, 4))


# ---------------------------------------------------------------------------
# a real asset, with a real op stack
# ---------------------------------------------------------------------------
#
# A UsdGeom.Cube has no xformOps of its own, so every test above exercises the case where
# this chunk's op is the only one on the prim and upstream composes to the identity. That
# hides two things at once: the write-back's upstream division is a no-op, and so is the
# layer strength the appended op depends on.
#
# The asset here carries separate translate / rotateXYZ / scale ops under a placed and
# rotated parent -- the apple-in-a-bowl the pivot was asked for. It lives in a sublayer
# *weaker* than the one the chunk cooks into, which is not decoration: with the asset in a
# stronger layer the chunk's xformOpOrder loses, its op is never listed, and it contributes
# nothing. Every assertion below still passed in that state, against an inert chunk.

def _stage_with_asset():
    """A placed parent and an asset with its own three ops, composed the way a session does.

    The edit target is set before any chunk exists because a cook clears it, and a cook runs
    on every param write -- pointed at the root layer it would take the sublayer list with
    it and the asset would vanish mid-test.
    """
    stage = Usd.Stage.CreateInMemory()
    cook = Sdf.Layer.CreateAnonymous("cook.usda")
    asset = Sdf.Layer.CreateAnonymous("asset.usda")
    stage.GetRootLayer().subLayerPaths = [cook.identifier, asset.identifier]

    with Usd.EditContext(stage, asset):
        bowl = UsdGeom.Xformable(UsdGeom.Xform.Define(stage, "/World/Bowl"))
        bowl.AddTranslateOp().Set(Gf.Vec3d(10, 0, 5))
        bowl.AddRotateXYZOp().Set(Gf.Vec3f(0, 30, 0))
        bowl.AddScaleOp().Set(Gf.Vec3f(2, 2, 2))

        apple = UsdGeom.Mesh.Define(stage, "/World/Bowl/Apple")
        ops = UsdGeom.Xformable(apple)
        ops.AddTranslateOp().Set(Gf.Vec3d(0, 1, 0))
        ops.AddRotateXYZOp().Set(Gf.Vec3f(-90, 0, 0))
        ops.AddScaleOp().Set(Gf.Vec3f(100, 100, 100))
        apple.GetPointsAttr().Set([
            Gf.Vec3f(-0.1, 0, -0.1), Gf.Vec3f(0.1, 0, 0.1), Gf.Vec3f(0, 0.2, 0),
        ])
        apple.GetExtentAttr().Set([Gf.Vec3f(-0.1, 0, -0.1), Gf.Vec3f(0.1, 0.2, 0)])

    stage.SetEditTarget(cook)

    class _StagedScene(_DummyScene):
        def get_stage(self):
            return stage

    chunk_registry.ChunkRegistry().initialize(_StagedScene(), "pytest")
    return stage


_APPLE = "/World/Bowl/Apple"


def _apple_local(stage) -> Gf.Matrix4d:
    return UsdGeom.Xformable(stage.GetPrimAtPath(_APPLE)).GetLocalTransformation()


def test_the_chunks_op_is_appended_after_the_assets_own() -> None:
    """The precondition every test below rests on. Without it the chunk is inert and the
    rest of this section passes while proving nothing."""
    stage = _stage_with_asset()
    c = _transform_chunk(_APPLE)

    transform_mod.execute(_FakeNode(c, stage))

    order = list(stage.GetPrimAtPath(_APPLE).GetAttribute("xformOpOrder").Get())
    assert order[:3] == ["xformOp:translate", "xformOp:rotateXYZ", "xformOp:scale"]
    assert order[3].startswith("xformOp:transform:Transform")


def test_center_pivot_ignores_everything_upstream_of_the_chunk() -> None:
    """The apple is scaled 100 and rotated under a placed parent. The pivot is in the apple's
    own space, so it is the raw centroid of the points -- not that centroid placed anywhere."""
    stage = _stage_with_asset()
    c = _transform_chunk(_APPLE)
    transform_mod.execute(_FakeNode(c, stage))

    transform_mod._center_pivot(c.get_param("center_pivot"), None, None)

    assert transform_mod.pivot(c) == pytest.approx((0.0, 0.1, -0.05), abs=1e-6)


def test_a_typed_rotation_turns_the_asset_about_its_pivot() -> None:
    stage = _stage_with_asset()
    c = _transform_chunk(_APPLE)
    transform_mod.execute(_FakeNode(c, stage))
    transform_mod._center_pivot(c.get_param("center_pivot"), None, None)
    transform_mod.execute(_FakeNode(c, stage))
    point = Gf.Vec3d(*transform_mod.pivot(c))
    before = _apple_local(stage)

    _set(c, ry=45)
    transform_mod.execute(_FakeNode(c, stage))
    after = _apple_local(stage)

    assert not _matrices_close(before, after, tol=1e-9), "the chunk contributed nothing"
    assert after.Transform(point) == pytest.approx(before.Transform(point), abs=1e-6)


def test_a_drag_on_a_scaled_rotated_asset_lands_exactly() -> None:
    """The write-back has to divide out an upstream that is genuinely not the identity: the
    apple is scaled 100, so a 3-unit move is 0.03 in the space this chunk's params live in.
    Getting the division backwards conjugates the drag instead of undoing it."""
    stage = _stage_with_asset()
    c = _transform_chunk(_APPLE)
    transform_mod.execute(_FakeNode(c, stage))
    transform_mod._center_pivot(c.get_param("center_pivot"), None, None)
    transform_mod.execute(_FakeNode(c, stage))

    target = _apple_local(stage) * Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(0, 3, 0))
    chunk_gizmo.apply_bindings(c, "transform", target)
    transform_mod.execute(_FakeNode(c, stage))

    assert _matrices_close(target, _apple_local(stage), tol=1e-6)
    assert c.get_param("tz").value == pytest.approx(0.03, abs=1e-6)


def test_centering_the_pivot_on_a_real_asset_moves_nothing() -> None:
    stage = _stage_with_asset()
    c = _transform_chunk(_APPLE)
    _set(c, tx=0.05, rx=15, ry=-30, sx=2, sy=2, sz=2)
    transform_mod.execute(_FakeNode(c, stage))
    before = _apple_local(stage)

    transform_mod._center_pivot(c.get_param("center_pivot"), None, None)
    transform_mod.execute(_FakeNode(c, stage))

    assert _matrices_close(before, _apple_local(stage), tol=1e-6)


# ---------------------------------------------------------------------------
# dragging the pivot itself
# ---------------------------------------------------------------------------
#
# A host holds a world position under the mouse and knows nothing about the space a chunk
# keeps its pivot in. apply_offset() is where that gap is closed, so these are all about the
# conversion surviving a real ancestor chain -- and about the object not moving while it does.

def _to_world(stage, path=_APPLE) -> Gf.Matrix4d:
    return UsdGeom.Xformable(stage.GetPrimAtPath(path)).ComputeLocalToWorldTransform(
        Usd.TimeCode.Default()
    )


def test_a_dragged_pivot_ends_up_under_the_mouse() -> None:
    """The promise, round tripped: hand over a world point, and the handle reports back a
    pivot that maps to that same world point. Everything between is space conversion, so an
    error either way shows as the pivot landing somewhere other than where it was dropped."""
    stage = _stage_with_asset()
    c = _transform_chunk(_APPLE)
    _set(c, rx=15, ry=-30)
    transform_mod.execute(_FakeNode(c, stage))
    dropped = Gf.Vec3d(3, 7, -2)

    assert chunk_gizmo.apply_offset(c, "transform", dropped) is True
    transform_mod.execute(_FakeNode(c, stage))

    reported = Gf.Vec3d(*transform_mod.gizmo_offset(c, "transform"))
    assert _to_world(stage).Transform(reported) == pytest.approx(dropped, abs=1e-6)


def test_dragging_the_pivot_does_not_move_the_object() -> None:
    """The same guarantee the sliders give, through the other entry point -- they share
    _set_pivot precisely so this cannot drift between them."""
    stage = _stage_with_asset()
    c = _transform_chunk(_APPLE)
    _set(c, tx=0.05, rx=15, ry=-30, sx=2, sy=2, sz=2)
    transform_mod.execute(_FakeNode(c, stage))
    before = _apple_local(stage)

    chunk_gizmo.apply_offset(c, "transform", Gf.Vec3d(3, 7, -2))
    transform_mod.execute(_FakeNode(c, stage))

    assert _matrices_close(before, _apple_local(stage), tol=1e-6)


def test_the_conversion_undoes_the_whole_ancestor_chain() -> None:
    """The apple sits under a parent that is translated, rotated and scaled, and carries its
    own scale of 100 on top. A world point taken raw would be out by all of it.

    (10, 2, 5) is where the apple's own origin sits in the world, so the pivot that reaches
    it is the object-space origin. Asserted as "essentially zero" rather than as None,
    because it comes back as float dust rather than an exact zero -- see the test below.
    """
    stage = _stage_with_asset()
    c = _transform_chunk(_APPLE)
    transform_mod.execute(_FakeNode(c, stage))

    chunk_gizmo.apply_offset(c, "transform", Gf.Vec3d(10, 2, 5))

    assert [c.get_param(n).value for n in transform_mod._PIVOT_PARAMS] == \
        pytest.approx([0.0, 0.0, 0.0], abs=1e-9)


def test_a_pivot_dragged_to_the_origin_counts_as_set() -> None:
    """Worth pinning rather than leaving to be discovered. "Unset" is exactly zero, and a
    drag lands on float dust instead -- so dropping the pivot on the object's origin leaves
    the handle *there* rather than falling back to the geometry centroid.

    That is the right answer for a drag (the handle stays where it was dropped) but it does
    mean dragging to the origin and typing three zeros are not quite the same thing. No
    epsilon, deliberately: the pivot is in object space, whose scale is whatever the asset
    says, so there is no absolute tolerance that is right for both a 0.01-unit prop and a
    1000-unit set piece.
    """
    stage = _stage_with_asset()
    c = _transform_chunk(_APPLE)
    transform_mod.execute(_FakeNode(c, stage))

    chunk_gizmo.apply_offset(c, "transform", Gf.Vec3d(10, 2, 5))

    assert transform_mod.pivot(c) is not None
    assert transform_mod.pivot(c) == pytest.approx((0.0, 0.0, 0.0), abs=1e-9)


def test_a_chunk_with_no_offset_hook_declines() -> None:
    """False, so a host leaves the pivot alone. It must not fall back to moving the prim the
    way commit_transform does -- there is nowhere on a prim to put a pivot."""
    _stage_with_cube()   # for the side effect: points the registry's scene at a stage
    c = _transform_chunk()

    class _NoHook:
        pass

    c._module = _NoHook

    assert chunk_gizmo.apply_offset(c, "transform", Gf.Vec3d(1, 2, 3)) is False


def test_an_unresolvable_anchor_declines_rather_than_guessing() -> None:
    _stage_with_cube()   # for the side effect: points the registry's scene at a stage
    c = _transform_chunk("/World/NotThere")

    assert chunk_gizmo.apply_offset(c, "transform", Gf.Vec3d(1, 2, 3)) is False
    assert transform_mod.pivot(c) is None


def test_a_failing_offset_hook_is_treated_as_handled(caplog) -> None:
    """Same rule as gizmo_changed: falling through after a half-finished write would combine
    two intents in one frame, and the next tick of the drag retries anyway."""
    _stage_with_cube()   # for the side effect: points the registry's scene at a stage
    c = _transform_chunk()

    class _Boom:
        @staticmethod
        def gizmo_offset_changed(node, gizmo, value):
            raise RuntimeError("nope")

    c._module = _Boom

    assert chunk_gizmo.apply_offset(c, "transform", Gf.Vec3d(1, 2, 3)) is True


def test_the_offset_hook_only_answers_for_its_own_handle() -> None:
    _stage_with_cube()   # for the side effect: points the registry's scene at a stage
    c = _transform_chunk()

    assert transform_mod.gizmo_offset_changed(c, "zdist", Gf.Vec3d(1, 2, 3)) is False
    assert transform_mod.pivot(c) is None


def test_a_dragged_pivot_is_written_every_tick() -> None:
    """Never batched to mouse-release. The chunk cooking per write is what holds the object
    still under a pivot moving on screen, and there is a test in the gizmo suite whose only
    job is to fail if someone optimises that away.

    Read off the pivot rather than off one param's callback: a world-Y drag lands on object
    *Z* here, because the apple carries a -90 X rotation. Watching a named axis tests the
    fixture's arithmetic rather than the behaviour.
    """
    stage = _stage_with_asset()
    c = _transform_chunk(_APPLE)
    transform_mod.execute(_FakeNode(c, stage))

    seen = []
    for step in range(1, 4):
        chunk_gizmo.apply_offset(c, "transform", Gf.Vec3d(0, float(step), 0))
        seen.append(transform_mod.pivot(c))

    assert len(set(seen)) == 3, "the drag was batched instead of written per tick"


# ---------------------------------------------------------------------------
# the hook contract
# ---------------------------------------------------------------------------

def test_a_chunk_without_the_hook_keeps_its_declared_offset() -> None:
    """Returning None means "no opinion", not "clear it" -- a chunk with several handles
    would otherwise wipe the static offset on the ones its hook does not answer for."""
    c = _transform_chunk()

    assert chunk_gizmo.gizmo_offset(c, "something_else", declared=(0, 0, 7)) == (0, 0, 7)


def test_a_failing_hook_falls_back_to_the_declaration(caplog) -> None:
    class _Boom:
        @staticmethod
        def gizmo_offset(node, gizmo):
            raise RuntimeError("nope")

    c = _transform_chunk()
    c._module = _Boom

    assert chunk_gizmo.gizmo_offset(c, "transform", declared=(0, 0, 7)) == (0, 0, 7)


def test_gizmo_offset_only_answers_for_the_transform_handle() -> None:
    c = _transform_chunk()
    _set(c, px=1, py=-3, pz=4)

    assert transform_mod.gizmo_offset(c, "transform") == pytest.approx((1.0, -3.0, 4.0))
    assert transform_mod.gizmo_offset(c, "zdist") is None
