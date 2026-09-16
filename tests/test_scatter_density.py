"""Scatter reads a painted density field off the surface.

The first slice of the brush, and deliberately the half with no viewport in it. The brush
paints a per-vertex weight; scatter samples it through the `mask` hook that has been sitting
in the sampler since it was written, with a docstring saying a future paint gizmo would use
it. If the field is the wrong abstraction, this is where that shows up -- cheaply, and before
any painting exists.

Nothing here draws or clicks. What is asserted is that a weight authored as ordinary USD
changes where instances land, and that an unpainted surface behaves exactly as it always did.
"""
from __future__ import annotations

import importlib

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


chunk_registry.ChunkRegistry().initialize(_DummyScene(), "pytest")

scatter = importlib.import_module("scripts.modelling.scatter_points")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _grid_mesh(stage, path="/World/Floor", size=10.0, divisions=10):
    """A flat grid in XY, fine enough that a painted region has an edge worth testing."""
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


def _paint(mesh, weight_of):
    """Author the density primvar from `weight_of(point) -> 0..1`."""
    primvar = UsdGeom.PrimvarsAPI(mesh.GetPrim()).CreatePrimvar(
        scatter.DENSITY_PRIMVAR, Sdf.ValueTypeNames.FloatArray, UsdGeom.Tokens.vertex
    )
    primvar.Set(Vt.FloatArray([float(weight_of(p)) for p in mesh.GetPointsAttr().Get()]))
    return primvar


def _scatter_points(stage, path="/World/Floor", count=400, seed=3):
    """Run the surface sampler exactly as the cook does, mask included."""
    import random

    prim = stage.GetPrimAtPath(path)
    triangles = scatter._collect_triangles(prim)
    mask = scatter._density_mask(prim, scatter.DENSITY_PRIMVAR)
    positions, _ = scatter._scatter_on_triangles(
        triangles, count, random.Random(seed), 0, mask=mask
    )
    return positions


@pytest.fixture
def stage():
    return Usd.Stage.CreateInMemory()


# ---------------------------------------------------------------------------
# the mask itself
# ---------------------------------------------------------------------------

def test_an_unpainted_surface_has_no_mask_at_all(stage) -> None:
    """None, not a function returning 1.0. The sampler skips the hook entirely when there is
    no mask, so an unpainted scatter costs what it always did -- and the hook runs per
    candidate, which outnumber instances by the rejection rate."""
    _grid_mesh(stage)

    assert scatter._density_mask(stage.GetPrimAtPath("/World/Floor"), scatter.DENSITY_PRIMVAR) is None


def test_a_painted_surface_reports_the_weight_under_a_point(stage) -> None:
    mesh = _grid_mesh(stage)
    _paint(mesh, lambda p: 1.0 if p[0] < 5.0 else 0.0)

    mask = scatter._density_mask(stage.GetPrimAtPath("/World/Floor"), scatter.DENSITY_PRIMVAR)

    assert mask(Gf.Vec3f(1.0, 5.0, 0.0), None) == pytest.approx(1.0)
    assert mask(Gf.Vec3f(9.0, 5.0, 0.0), None) == pytest.approx(0.0)


def test_the_weight_is_read_in_world_space(stage) -> None:
    """The sampler works in world space -- `_collect_triangles` says so in its first line --
    so a mask reading local coordinates would be offset by the surface's own transform, which
    is exactly the bug that would look like 'the paint is in the wrong place'."""
    mesh = _grid_mesh(stage)
    _paint(mesh, lambda p: 1.0 if p[0] < 5.0 else 0.0)
    UsdGeom.Xformable(mesh).AddTranslateOp().Set(Gf.Vec3d(100.0, 0.0, 0.0))

    mask = scatter._density_mask(stage.GetPrimAtPath("/World/Floor"), scatter.DENSITY_PRIMVAR)

    assert mask(Gf.Vec3f(101.0, 5.0, 0.0), None) == pytest.approx(1.0)
    assert mask(Gf.Vec3f(109.0, 5.0, 0.0), None) == pytest.approx(0.0)


def test_a_constant_primvar_dials_the_whole_surface(stage) -> None:
    """One number for the whole mesh is a legitimate way to thin a surface without painting
    it, and it has no per-vertex values to index."""
    mesh = _grid_mesh(stage)
    primvar = UsdGeom.PrimvarsAPI(mesh.GetPrim()).CreatePrimvar(
        scatter.DENSITY_PRIMVAR, Sdf.ValueTypeNames.FloatArray, UsdGeom.Tokens.constant
    )
    primvar.Set(Vt.FloatArray([0.25]))

    mask = scatter._density_mask(stage.GetPrimAtPath("/World/Floor"), scatter.DENSITY_PRIMVAR)

    assert mask(Gf.Vec3f(1.0, 1.0, 0.0), None) == pytest.approx(0.25)
    assert mask(Gf.Vec3f(9.0, 9.0, 0.0), None) == pytest.approx(0.25)


def test_weights_are_clamped(stage) -> None:
    """Nothing stops a bake or a script authoring 5.0 or -1.0, and the sampler compares the
    weight against a 0..1 random -- an out-of-range value would silently mean always or never."""
    mesh = _grid_mesh(stage)
    _paint(mesh, lambda p: 5.0 if p[0] < 5.0 else -1.0)

    mask = scatter._density_mask(stage.GetPrimAtPath("/World/Floor"), scatter.DENSITY_PRIMVAR)

    assert mask(Gf.Vec3f(1.0, 5.0, 0.0), None) == 1.0
    assert mask(Gf.Vec3f(9.0, 5.0, 0.0), None) == 0.0


def test_a_short_primvar_is_ignored_rather_than_indexed_off_the_end(stage) -> None:
    mesh = _grid_mesh(stage)
    primvar = UsdGeom.PrimvarsAPI(mesh.GetPrim()).CreatePrimvar(
        scatter.DENSITY_PRIMVAR, Sdf.ValueTypeNames.FloatArray, UsdGeom.Tokens.vertex
    )
    primvar.Set(Vt.FloatArray([1.0, 1.0, 1.0]))

    assert scatter._density_mask(stage.GetPrimAtPath("/World/Floor"), scatter.DENSITY_PRIMVAR) is None


# ---------------------------------------------------------------------------
# what it does to the scatter
# ---------------------------------------------------------------------------

def test_painting_half_the_floor_keeps_the_instances_on_that_half(stage) -> None:
    """The point of the whole slice."""
    mesh = _grid_mesh(stage)
    _paint(mesh, lambda p: 1.0 if p[0] < 5.0 else 0.0)

    positions = _scatter_points(stage)

    assert positions, "the mask must not reject everything"
    assert all(p[0] < 5.5 for p in positions), "instances landed on the unpainted half"


def test_an_unpainted_floor_scatters_everywhere(stage) -> None:
    """The behaviour every existing scene has: nothing changes until something is painted."""
    _grid_mesh(stage)

    positions = _scatter_points(stage)

    assert any(p[0] < 5.0 for p in positions)
    assert any(p[0] > 5.0 for p in positions)


def test_a_fully_unpainted_weight_yields_nothing(stage) -> None:
    """Zero everywhere means zero instances, not 'fall back to scattering everywhere'. The
    difference between an empty field and an absent one has to survive."""
    mesh = _grid_mesh(stage)
    _paint(mesh, lambda p: 0.0)

    assert _scatter_points(stage) == []


def test_a_gradient_puts_more_instances_where_it_is_stronger(stage) -> None:
    """Weight is a probability, not a switch, so a soft edge has to thin out rather than cut."""
    mesh = _grid_mesh(stage)
    _paint(mesh, lambda p: p[0] / 10.0)

    positions = _scatter_points(stage, count=600)

    low = sum(1 for p in positions if p[0] < 5.0)
    high = sum(1 for p in positions if p[0] >= 5.0)
    assert high > low * 1.5, f"expected the strong side to dominate, got {low} vs {high}"


def test_the_same_seed_and_the_same_paint_give_the_same_points(stage) -> None:
    """Scatter's whole contract is that its output is a function of its inputs. A painted
    field is an input, so it must not introduce any order-dependence of its own."""
    mesh = _grid_mesh(stage)
    _paint(mesh, lambda p: 1.0 if p[0] < 5.0 else 0.0)

    first = _scatter_points(stage)
    second = _scatter_points(stage)

    assert [tuple(p) for p in first] == [tuple(p) for p in second]


# ---------------------------------------------------------------------------
# the wire, not a convention
# ---------------------------------------------------------------------------

def test_an_unconnected_scatter_is_unaffected_by_paint(stage) -> None:
    """The point of making this a port. Two chunks that merely named the same mesh agreed by
    convention, invisibly -- so a brush anywhere in the graph silently governed every scatter
    pointed at that surface, and nothing on screen said why.

    With no field named, there is no mask, and a painted surface changes nothing.
    """
    mesh = _grid_mesh(stage)
    _paint(mesh, lambda p: 1.0 if p[0] < 5.0 else 0.0)
    prim = stage.GetPrimAtPath("/World/Floor")

    assert scatter._density_mask(prim, "") is None
    assert scatter._density_mask(prim, None) is None


def test_the_wire_names_which_field_to_read(stage) -> None:
    """Two brushes on one graph write two fields; a scatter reads the one it is wired to."""
    mesh = _grid_mesh(stage)
    _paint(mesh, lambda p: 1.0 if p[0] < 5.0 else 0.0)
    other = UsdGeom.PrimvarsAPI(mesh.GetPrim()).CreatePrimvar(
        "scatterDensityB", Sdf.ValueTypeNames.FloatArray, UsdGeom.Tokens.vertex
    )
    other.Set(Vt.FloatArray([1.0 if p[0] > 5.0 else 0.0 for p in mesh.GetPointsAttr().Get()]))
    prim = stage.GetPrimAtPath("/World/Floor")

    left = scatter._density_mask(prim, scatter.DENSITY_PRIMVAR)
    right = scatter._density_mask(prim, "scatterDensityB")

    assert left(Gf.Vec3f(1, 5, 0), None) == pytest.approx(1.0)
    assert right(Gf.Vec3f(1, 5, 0), None) == pytest.approx(0.0)
    assert right(Gf.Vec3f(9, 5, 0), None) == pytest.approx(1.0)


def test_a_field_that_does_not_exist_is_not_an_error(stage) -> None:
    """A wire pointing at a brush that has not painted yet, or a typo in the field name. Both
    mean "no field", not "no instances" -- silently placing nothing would be indistinguishable
    from the chunk being broken."""
    _grid_mesh(stage)

    assert scatter._density_mask(stage.GetPrimAtPath("/World/Floor"), "nope") is None
