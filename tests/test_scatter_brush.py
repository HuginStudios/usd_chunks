"""The brush half: a gesture becomes a field, and scatter reads that field.

`test_scatter_density.py` covers the reading end with a hand-authored primvar. This covers the
writing end, and — more importantly — that the two agree. They are separate chunks that talk
only through `primvars:scatterDensity`, so a disagreement about interpolation, space or
clamping would leave each half passing its own tests while painting did nothing.

Nothing here draws or clicks. `apply_stroke` takes world-space samples, which is exactly what
a viewport hands over, so the whole path except the raycast is exercised.
"""
from __future__ import annotations

import importlib
import random

import pytest
from pxr import Gf, Usd, UsdGeom

from usd_chunks import chunk_gizmo, chunk_registry
from usd_chunks.chunk_core import ChunkConfig, SceneSender


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


chunk_registry.ChunkRegistry().initialize(_DummyScene(), "pytest")

brush_mod = importlib.import_module("scripts.scatter_brush")
scatter_mod = importlib.import_module("scripts.scatter_points")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _grid_mesh(stage, path="/World/Floor", size=20.0, divisions=20):
    mesh = UsdGeom.Mesh.Define(stage, path)
    points, counts, indices = [], [], []
    step = size / divisions
    for iy in range(divisions + 1):
        for ix in range(divisions + 1):
            points.append(Gf.Vec3f(ix * step, iy * step, 0.0))
    row = divisions + 1
    for iy in range(divisions):
        for ix in range(divisions):
            a = iy * row + ix
            counts.append(4)
            indices.extend([a, a + 1, a + row + 1, a + row])
    mesh.GetPointsAttr().Set(points)
    mesh.GetFaceVertexCountsAttr().Set(counts)
    mesh.GetFaceVertexIndicesAttr().Set(indices)
    mesh.GetExtentAttr().Set([Gf.Vec3f(0, 0, 0), Gf.Vec3f(size, size, 0)])
    return mesh


def _stage_with_floor():
    stage = Usd.Stage.CreateInMemory()
    _grid_mesh(stage)

    class _Staged(_DummyScene):
        def get_stage(self):
            return stage

    chunk_registry.ChunkRegistry().initialize(_Staged(), "pytest")
    return stage


def _brush(stage, target="/World/Floor", **params):
    c = brush_mod.main()
    c._config = ChunkConfig(module="scatter_brush", gizmos=brush_mod.config().gizmos)
    c._module = brush_mod
    c.get_param("surface_prim").value = target
    for name, value in params.items():
        c.get_param(name).value = value
    return c


def _weights(stage, path="/World/Floor"):
    primvar = UsdGeom.PrimvarsAPI(stage.GetPrimAtPath(path)).GetPrimvar(brush_mod.DENSITY_PRIMVAR)
    if not primvar or not primvar.HasValue():
        return None
    return [float(v) for v in primvar.ComputeFlattened()]


def _stroke(chunk_obj, *points, pressure=1.0):
    return chunk_gizmo.apply_stroke(
        chunk_obj, "paint", [(Gf.Vec3d(*p), pressure) for p in points]
    )


# ---------------------------------------------------------------------------
# the declaration
# ---------------------------------------------------------------------------

def test_the_brush_declares_one_brush_handle() -> None:
    _stage_with_floor()
    c = _brush(None)

    gizmos = chunk_gizmo.get_gizmos(c)
    assert [(g.name, g.type) for g in gizmos] == [("paint", "brush")]


def test_the_handle_names_the_radius_so_a_host_can_draw_the_ring() -> None:
    """Unusually for this schema the driver is what the handle *reads*: a brush writes a
    field, not params, but a viewport still has to size its ring from somewhere."""
    _stage_with_floor()
    c = _brush(None)

    driver = chunk_gizmo.get_gizmos(c)[0].driver
    assert "brush_grp.radius" in driver


# ---------------------------------------------------------------------------
# a stroke becomes a field
# ---------------------------------------------------------------------------

def test_a_stroke_paints_where_it_touched(stage=None) -> None:
    stage = _stage_with_floor()
    c = _brush(stage, radius=5.0, strength=1.0)

    assert _stroke(c, (5.0, 5.0, 0.0)) is True

    weights = _weights(stage)
    assert weights is not None
    points = UsdGeom.Mesh(stage.GetPrimAtPath("/World/Floor")).GetPointsAttr().Get()
    near = [w for w, p in zip(weights, points) if (Gf.Vec3f(5, 5, 0) - p).GetLength() < 2.0]
    far = [w for w, p in zip(weights, points) if (Gf.Vec3f(5, 5, 0) - p).GetLength() > 8.0]

    assert min(near) > 0.5, "the centre of the stroke should be strongly painted"
    assert max(far) == 0.0, "nothing outside the radius should be touched"


def test_the_stroke_falls_off_rather_than_cutting() -> None:
    """A hard edge is the thing you are painting to avoid -- the instances it places have a
    boundary the eye picks out immediately."""
    stage = _stage_with_floor()
    c = _brush(stage, radius=8.0, strength=1.0)

    _stroke(c, (10.0, 10.0, 0.0))

    weights = _weights(stage)
    points = UsdGeom.Mesh(stage.GetPrimAtPath("/World/Floor")).GetPointsAttr().Get()
    by_distance = sorted(
        ((Gf.Vec3f(10, 10, 0) - p).GetLength(), w) for p, w in zip(points, weights)
    )
    inside = [w for d, w in by_distance if d < 7.5]

    assert inside[0] > inside[-1], "weight must decrease with distance from the centre"
    assert len(set(round(w, 3) for w in inside)) > 3, "a falloff, not a step"


def test_strokes_accumulate_across_ticks() -> None:
    """A drag arrives as many ticks, so one pass at half strength then another has to build
    up -- otherwise painting would only ever be as strong as the last sample."""
    stage = _stage_with_floor()
    c = _brush(stage, radius=5.0, strength=0.3)

    _stroke(c, (5.0, 5.0, 0.0))
    first = max(_weights(stage))
    _stroke(c, (5.0, 5.0, 0.0))
    second = max(_weights(stage))

    assert second > first


def test_weights_never_exceed_one() -> None:
    """The value is used as a probability, so anything above 1 silently means "always"."""
    stage = _stage_with_floor()
    c = _brush(stage, radius=5.0, strength=1.0)

    for _ in range(10):
        _stroke(c, (5.0, 5.0, 0.0))

    assert max(_weights(stage)) <= 1.0


def test_subtract_takes_paint_away_without_going_negative() -> None:
    stage = _stage_with_floor()
    c = _brush(stage, radius=6.0, strength=1.0)
    _stroke(c, (5.0, 5.0, 0.0))

    c.get_param("mode").value = "Subtract"
    for _ in range(5):
        _stroke(c, (5.0, 5.0, 0.0))

    assert min(_weights(stage)) >= 0.0
    assert max(_weights(stage)) == pytest.approx(0.0, abs=1e-6)


def test_pressure_scales_the_stroke() -> None:
    """A tablet sends it and a plain raycast does not, so both have to work."""
    stage = _stage_with_floor()
    c = _brush(stage, radius=5.0, strength=1.0)

    _stroke(c, (5.0, 5.0, 0.0), pressure=0.25)
    light = max(_weights(stage))
    brush_mod._clear_paint(c.get_param("clear_paint"), None, None)
    _stroke(c, (5.0, 5.0, 0.0), pressure=1.0)

    assert max(_weights(stage)) > light


def test_a_bare_point_without_pressure_is_accepted() -> None:
    """A plain raycast hit has no pressure; sending one must not be a special case a host has
    to know about."""
    stage = _stage_with_floor()
    c = _brush(stage, radius=5.0, strength=1.0)

    assert chunk_gizmo.apply_stroke(c, "paint", [Gf.Vec3d(5.0, 5.0, 0.0)]) is True
    assert max(_weights(stage)) > 0.0


def test_a_stroke_that_misses_the_surface_changes_nothing() -> None:
    """False, and the field is left alone. Note the primvar may already exist and be empty --
    the cook authors one so a scatter downstream resolves something the moment the two are
    wired together, so "unchanged" is the claim here, not "absent"."""
    stage = _stage_with_floor()
    c = _brush(stage, radius=5.0, strength=1.0)
    before = _weights(stage)

    assert _stroke(c, (500.0, 500.0, 0.0)) is False
    assert _weights(stage) == before


def test_a_zero_radius_brush_does_nothing() -> None:
    stage = _stage_with_floor()
    c = _brush(stage, radius=0.0, strength=1.0)

    assert _stroke(c, (5.0, 5.0, 0.0)) is False


def test_fill_and_clear_reach_both_ends() -> None:
    stage = _stage_with_floor()
    c = _brush(stage)

    brush_mod._fill_paint(c.get_param("fill_paint"), None, None)
    assert min(_weights(stage)) == pytest.approx(1.0)

    brush_mod._clear_paint(c.get_param("clear_paint"), None, None)
    assert max(_weights(stage)) == pytest.approx(0.0)


def test_the_cook_does_not_erase_the_paint() -> None:
    """The field IS this chunk's output and lives in its layer, so `execute` must not clear
    that layer the way most chunks do -- or a slider nudge would wipe the painting."""
    stage = _stage_with_floor()
    c = _brush(stage, radius=5.0, strength=1.0)
    _stroke(c, (5.0, 5.0, 0.0))
    painted = max(_weights(stage))

    brush_mod.execute(c)

    assert max(_weights(stage)) == pytest.approx(painted)


# ---------------------------------------------------------------------------
# the two chunks agreeing
# ---------------------------------------------------------------------------

def test_what_the_brush_paints_is_what_scatter_reads() -> None:
    """The seam. Two chunks that talk only through a primvar, so a disagreement about
    interpolation, space or clamping would leave both halves passing their own tests."""
    stage = _stage_with_floor()
    c = _brush(stage, radius=8.0, strength=1.0)
    for _ in range(3):
        _stroke(c, (5.0, 5.0, 0.0))

    prim = stage.GetPrimAtPath("/World/Floor")
    mask = scatter_mod._density_mask(prim)
    assert mask is not None, "scatter could not see what the brush painted"

    assert mask(Gf.Vec3f(5.0, 5.0, 0.0), None) > 0.5
    assert mask(Gf.Vec3f(18.0, 18.0, 0.0), None) == pytest.approx(0.0)


def test_painting_a_corner_puts_the_instances_in_that_corner() -> None:
    """End to end, as a user would see it: paint here, instances appear here."""
    stage = _stage_with_floor()
    c = _brush(stage, radius=6.0, strength=1.0)
    for _ in range(4):
        _stroke(c, (4.0, 4.0, 0.0))

    prim = stage.GetPrimAtPath("/World/Floor")
    positions, _ = scatter_mod._scatter_on_triangles(
        scatter_mod._collect_triangles(prim), 200, random.Random(5), 0,
        mask=scatter_mod._density_mask(prim),
    )

    assert positions, "the painted region should hold instances"
    assert all(p[0] < 12.0 and p[1] < 12.0 for p in positions), \
        "instances landed outside the painted corner"


def test_an_unpainted_brush_places_nothing_rather_than_everything() -> None:
    """Adding the chunk has to have a visible, undoable effect. Its cook authors an empty
    field, and empty means empty -- scatter's "no opinion" is an *absent* primvar."""
    stage = _stage_with_floor()
    c = _brush(stage)

    brush_mod.execute(c)

    prim = stage.GetPrimAtPath("/World/Floor")
    positions, _ = scatter_mod._scatter_on_triangles(
        scatter_mod._collect_triangles(prim), 100, random.Random(5), 0,
        mask=scatter_mod._density_mask(prim),
    )
    assert positions == []


# ---------------------------------------------------------------------------
# the hook contract
# ---------------------------------------------------------------------------

def test_a_chunk_without_the_hook_declines() -> None:
    _stage_with_floor()
    c = _brush(None)

    class _NoHook:
        pass

    c._module = _NoHook
    assert chunk_gizmo.apply_stroke(c, "paint", [Gf.Vec3d(0, 0, 0)]) is False


def test_a_failing_hook_is_treated_as_handled() -> None:
    _stage_with_floor()
    c = _brush(None)

    class _Boom:
        @staticmethod
        def gizmo_stroke(node, gizmo, samples):
            raise RuntimeError("nope")

    c._module = _Boom
    assert chunk_gizmo.apply_stroke(c, "paint", [Gf.Vec3d(0, 0, 0)]) is True


def test_an_unknown_handle_name_raises() -> None:
    _stage_with_floor()
    c = _brush(None)

    with pytest.raises(ValueError, match="declares no gizmo"):
        chunk_gizmo.apply_stroke(c, "not_a_handle", [])


def test_brush_is_a_known_gizmo_type() -> None:
    from usd_chunks import chunk as chunk_api

    assert chunk_api.Gizmo("g", type="brush", driver="radius").type == "brush"
