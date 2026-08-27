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
from pxr import Gf, Usd, UsdGeom, Vt

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
    mask = scatter_mod._density_mask(prim, brush_mod.DENSITY_PRIMVAR)
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
        mask=scatter_mod._density_mask(prim, brush_mod.DENSITY_PRIMVAR),
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
        mask=scatter_mod._density_mask(prim, brush_mod.DENSITY_PRIMVAR),
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


# ---------------------------------------------------------------------------
# the surface is a hierarchy, not a mesh
# ---------------------------------------------------------------------------

def _table(stage, root="/Kitchen_set/KitchenTable_1"):
    """The shape a real asset actually has: an Xform whose meshes are children.

    Reported live against Kitchen_set. Reading points off the picked prim found none and
    painted nothing -- silently -- while the viewport's hit test walked the descendants
    happily and reported 2392 triangles.
    """
    UsdGeom.Xform.Define(stage, root)
    for name, offset in (("Top", 0.0), ("Legs", 40.0)):
        mesh = _grid_mesh(stage, f"{root}/{name}", size=20.0, divisions=6)
        UsdGeom.Xformable(mesh).AddTranslateOp().Set(Gf.Vec3d(offset, 0.0, 0.0))
    return stage.GetPrimAtPath(root)


def _painted_meshes(stage, root="/Kitchen_set/KitchenTable_1"):
    found = {}
    for prim in Usd.PrimRange(stage.GetPrimAtPath(root)):
        primvar = UsdGeom.PrimvarsAPI(prim).GetPrimvar(brush_mod.DENSITY_PRIMVAR)
        if primvar and primvar.HasValue():
            found[prim.GetName()] = [float(v) for v in primvar.ComputeFlattened()]
    return found


def _hierarchy_stage():
    stage = Usd.Stage.CreateInMemory()
    _table(stage)

    class _Staged(_DummyScene):
        def get_stage(self):
            return stage

    chunk_registry.ChunkRegistry().initialize(_Staged(), "pytest")
    return stage


def test_painting_an_xform_reaches_the_meshes_under_it() -> None:
    """The reported failure. An Xform has no points of its own, so the brush found none and
    returned False without a word."""
    stage = _hierarchy_stage()
    c = _brush(stage, target="/Kitchen_set/KitchenTable_1", radius=8.0, strength=1.0)

    assert _stroke(c, (5.0, 5.0, 0.0)) is True

    painted = _painted_meshes(stage)
    assert "Top" in painted, "the mesh under the stroke was never painted"
    assert max(painted["Top"]) > 0.5


def test_a_stroke_only_changes_the_meshes_it_reached() -> None:
    """A table is a dozen prims and a brush touches one.

    Both meshes carry a primvar already -- the cook authors an empty field on each so a
    scatter downstream resolves something -- so what matters is that a stroke leaves the ones
    it did not touch alone, rather than rewriting every vertex array on every mouse-move.
    """
    stage = _hierarchy_stage()
    c = _brush(stage, target="/Kitchen_set/KitchenTable_1", radius=5.0, strength=1.0)

    _stroke(c, (5.0, 5.0, 0.0))

    painted = _painted_meshes(stage)
    assert max(painted["Top"]) > 0.0, "the mesh under the stroke should be painted"
    assert max(painted["Legs"]) == pytest.approx(0.0), "a mesh 40 units away was painted"


def test_fill_and_clear_cover_the_whole_hierarchy() -> None:
    stage = _hierarchy_stage()
    c = _brush(stage, target="/Kitchen_set/KitchenTable_1")

    brush_mod._fill_paint(c.get_param("fill_paint"), None, None)
    painted = _painted_meshes(stage)
    assert set(painted) == {"Top", "Legs"}
    assert all(min(v) == pytest.approx(1.0) for v in painted.values())

    brush_mod._clear_paint(c.get_param("clear_paint"), None, None)
    assert all(max(v) == pytest.approx(0.0) for v in _painted_meshes(stage).values())


def test_scatter_reads_a_hierarchy_the_brush_painted() -> None:
    """The seam again, on the shape a real asset has rather than on one flat mesh."""
    stage = _hierarchy_stage()
    c = _brush(stage, target="/Kitchen_set/KitchenTable_1", radius=8.0, strength=1.0)
    for _ in range(3):
        _stroke(c, (5.0, 5.0, 0.0))

    mask = scatter_mod._density_mask(stage.GetPrimAtPath("/Kitchen_set/KitchenTable_1"), brush_mod.DENSITY_PRIMVAR)

    assert mask is not None
    assert mask(Gf.Vec3f(5.0, 5.0, 0.0), None) > 0.5


# ---------------------------------------------------------------------------
# seeing the paint
# ---------------------------------------------------------------------------

def _tint(stage, path="/World/Floor"):
    attr = UsdGeom.Gprim(stage.GetPrimAtPath(path)).GetDisplayColorAttr()
    return list(attr.Get()) if attr and attr.IsValid() and attr.Get() is not None else None


def test_painting_tints_the_surface_so_the_stroke_is_visible() -> None:
    """Reported live: "the paint doesn't give away at all that I have hit it". The field was
    invisible until instances appeared downstream -- and invisible entirely before a scatter
    was wired up."""
    stage = _stage_with_floor()
    c = _brush(stage, radius=5.0, strength=1.0)

    _stroke(c, (5.0, 5.0, 0.0))

    colours = _tint(stage)
    assert colours is not None, "nothing showed that a stroke had landed"
    assert len(set(tuple(v) for v in colours)) > 1, "a gradient, not one flat colour"


def test_the_tint_follows_the_weight() -> None:
    """It has to show the gradient rather than a silhouette -- that is the whole difference
    between a density and a mask."""
    stage = _stage_with_floor()
    c = _brush(stage, radius=8.0, strength=1.0)
    _stroke(c, (5.0, 5.0, 0.0))

    weights = _weights(stage)
    colours = _tint(stage)
    paint = Gf.Vec3f(*c.get_param("paint_color").value)

    def distance_to_paint(index):
        return (Gf.Vec3f(*colours[index]) - paint).GetLength()

    # Colour-agnostic: whatever the picker is set to, the painted end is nearer to it.
    assert distance_to_paint(weights.index(max(weights))) <         distance_to_paint(weights.index(min(weights)))


def test_unticking_show_paint_puts_the_colours_back() -> None:
    """The opinion was only ever ours, so it has to be removable -- a tint that outlived the
    toggle would look like the asset had been recoloured."""
    stage = _stage_with_floor()
    c = _brush(stage, radius=5.0, strength=1.0)
    _stroke(c, (5.0, 5.0, 0.0))
    assert _tint(stage) is not None

    c.get_param("show_paint").value = False

    assert _tint(stage) is None


def test_the_tint_never_disturbs_the_field() -> None:
    """It is feedback, not data. Scatter reads the weight, and toggling a display option must
    not change where instances land."""
    stage = _stage_with_floor()
    c = _brush(stage, radius=5.0, strength=1.0)
    _stroke(c, (5.0, 5.0, 0.0))
    before = _weights(stage)

    c.get_param("show_paint").value = False
    c.get_param("show_paint").value = True

    assert _weights(stage) == before


def test_unpainted_vertices_keep_the_surface_colour() -> None:
    """Reported live as not being able to get the original look back. Authoring a flat colour
    on every vertex greys out the whole model to show a stroke in one corner of it -- so the
    tint now blends *from* whatever the surface already had, and weight zero is the original
    exactly."""
    stage = _stage_with_floor()
    mesh = stage.GetPrimAtPath("/World/Floor")
    original = Gf.Vec3f(0.8, 0.1, 0.1)
    points = UsdGeom.Mesh(mesh).GetPointsAttr().Get()
    UsdGeom.Gprim(mesh).CreateDisplayColorAttr().Set(Vt.Vec3fArray([original] * len(points)))

    c = _brush(stage, radius=4.0, strength=1.0)
    _stroke(c, (2.0, 2.0, 0.0))

    weights = _weights(stage)
    colours = _tint(stage)
    untouched = colours[weights.index(min(weights))]

    assert min(weights) == pytest.approx(0.0), "the fixture needs an unpainted vertex"
    assert Gf.Vec3f(*untouched) == pytest.approx(original, abs=1e-6)


def test_the_tint_does_not_blend_into_itself_over_a_drag() -> None:
    """The base colour is read off the property stack, skipping our own layer. Reading the
    composed value instead would fold the previous tint back in and drift a little further
    from the asset on every tick of a drag."""
    stage = _stage_with_floor()
    mesh = stage.GetPrimAtPath("/World/Floor")
    original = Gf.Vec3f(0.8, 0.1, 0.1)
    points = UsdGeom.Mesh(mesh).GetPointsAttr().Get()
    UsdGeom.Gprim(mesh).CreateDisplayColorAttr().Set(Vt.Vec3fArray([original] * len(points)))

    c = _brush(stage, radius=4.0, strength=0.2)
    for _ in range(6):
        _stroke(c, (2.0, 2.0, 0.0))

    weights = _weights(stage)
    colours = _tint(stage)
    untouched = colours[weights.index(min(weights))]

    assert Gf.Vec3f(*untouched) == pytest.approx(original, abs=1e-6), "the tint crept outward"


# ---------------------------------------------------------------------------
# what recooks the chain, and what does not
# ---------------------------------------------------------------------------

def _routing_of(change):
    """Which way a change is routed: 'dirty' recooks readers, 'presentation' does not.

    Asked at the branch itself rather than by reading a dirty flag afterwards. The flag is
    cleared again the moment the node solves, so a test that checked it later would report
    "not dirtied" for everything and pass for entirely the wrong reason.
    """
    from usd_chunks import chunk_scheduler

    seen = []
    real_dirty = chunk_scheduler.propagate_dirty
    real_presentation = chunk_scheduler.propagate_presentation

    def spy_dirty(seed):
        prop = seed.propagator
        if prop is not None and prop.is_presentation_change():
            seen.append("presentation")
        else:
            seen.append("dirty")
        return real_dirty(seed)

    chunk_scheduler.propagate_dirty = spy_dirty
    try:
        change()
    finally:
        chunk_scheduler.propagate_dirty = real_dirty
        chunk_scheduler.propagate_presentation = real_presentation
    return seen


def test_brush_settings_do_not_recook_the_chain() -> None:
    """Reported live: dragging Radius with a Scatter Points connected was unusably sticky,
    because every tick dirtied the chain and the scatter recooked.

    Nothing downstream computes from these. They steer a *stroke*, which happens outside any
    cook -- execute() never reads them, and what a scatter sees is the painted field, not the
    settings that produced it.
    """
    stage = _stage_with_floor()
    c = _brush(stage)

    for name, value in (("radius", 12.0), ("strength", 0.8), ("mode", "Subtract")):
        routes = _routing_of(lambda n=name, v=value: setattr(c.get_param(n), "value", v))
        assert "dirty" not in routes, f"{name} recooked the chain: {routes}"
        assert "presentation" in routes, f"{name} routed nowhere: {routes}"


def test_display_settings_do_not_recook_the_chain() -> None:
    """A tint is feedback. Recooking a scatter because somebody picked a different colour to
    look at would be the same bug wearing a different hat."""
    stage = _stage_with_floor()
    c = _brush(stage)

    for name, value in (("paint_color", (1.0, 0.0, 0.0)), ("show_paint", False)):
        assert "dirty" not in _routing_of(
            lambda n=name, v=value: setattr(c.get_param(n), "value", v)
        ), f"{name} recooked the chain"


def test_the_settings_still_take_effect() -> None:
    """Presentation-only stops the *dirtying*, not the change. The value still lands, and its
    callbacks still run -- a radius that did not reach the stroke would be worse than a slow
    one."""
    stage = _stage_with_floor()
    c = _brush(stage, strength=1.0)

    c.get_param("radius").value = 3.0
    _stroke(c, (5.0, 5.0, 0.0))
    small = sum(1 for w in _weights(stage) if w > 0.0)

    brush_mod._clear_paint(c.get_param("clear_paint"), None, None)
    c.get_param("radius").value = 9.0
    _stroke(c, (5.0, 5.0, 0.0))
    large = sum(1 for w in _weights(stage) if w > 0.0)

    assert large > small, "the radius change never reached the stroke"


def test_the_surface_still_recooks_the_chain() -> None:
    """The opt-out has to be earned per param. Which mesh is painted is a real input -- a
    scatter reading a field off a prim this chunk no longer paints is stale."""
    stage = _stage_with_floor()
    c = _brush(stage)

    assert "dirty" in _routing_of(
        lambda: setattr(c.get_param("surface_prim"), "value", "/World/Other")
    )


def test_finishing_a_stroke_recooks_the_chain() -> None:
    """The one moment it should. Painting writes USD rather than params, so without this
    nothing downstream ever notices."""
    stage = _stage_with_floor()
    c = _brush(stage, radius=5.0, strength=1.0)
    _stroke(c, (5.0, 5.0, 0.0))

    before = c.get_param("stroke_count").value

    routes = _routing_of(c.stop_gizmo)

    # A real param write, which is the only route observed to actually reach a connected
    # Scatter Points. Dirtying the node by hand, and solving its outputs, both looked correct
    # from inside and changed nothing in a live session.
    assert c.get_param("stroke_count").value == before + 1
    assert "dirty" in routes, "the write must recook readers, not be presentation-only"


def test_only_finishing_a_stroke_bumps_the_counter() -> None:
    """It is what a stroke *is* from the graph's point of view. Bumping it per tick would put
    the recook back on every mouse-move, which is the sticky drag all over again."""
    stage = _stage_with_floor()
    c = _brush(stage, radius=5.0, strength=1.0)
    before = c.get_param("stroke_count").value

    for _ in range(4):
        _stroke(c, (5.0, 5.0, 0.0))

    assert c.get_param("stroke_count").value == before
    c.stop_gizmo()
    assert c.get_param("stroke_count").value == before + 1


# ---------------------------------------------------------------------------
# brush mode: a field, or actual points
# ---------------------------------------------------------------------------

def _points(stage, chunk_obj):
    path = brush_mod._points_path(chunk_obj)
    prim = stage.GetPrimAtPath(path) if path else None
    values = UsdGeom.Points(prim).GetPointsAttr().Get() if prim and prim.IsValid() else None
    return [Gf.Vec3f(*p) for p in values] if values else []


def _points_brush(stage, **params):
    c = _brush(stage, **params)
    c._exec_prim_path = "/chunks/ScatterBrush"
    c.get_param("output").value = brush_mod._OUTPUT_POINTS
    return c


def test_points_mode_drops_a_point_where_you_click() -> None:
    """The other tool. Density says 'roughly this many, scattered'; Points says 'one, here'."""
    stage = _stage_with_floor()
    c = _points_brush(stage, radius=2.0, strength=1.0)

    assert _stroke(c, (5.0, 5.0, 0.0)) is True

    placed = _points(stage, c)
    assert len(placed) == 1
    assert placed[0] == pytest.approx(Gf.Vec3f(5.0, 5.0, 0.0), abs=1e-5)


def test_points_mode_writes_no_density_field() -> None:
    """The two outputs must not both fire, or a scatter would be governed by a field the user
    thought they had switched away from."""
    stage = _stage_with_floor()
    c = _points_brush(stage, radius=2.0, strength=1.0)
    before = _weights(stage)

    _stroke(c, (5.0, 5.0, 0.0))

    assert _weights(stage) == before


def test_points_are_spaced_so_a_slow_drag_does_not_pile_up() -> None:
    """A stroke arrives as one sample per mouse-move, so without spacing a careful drag leaves
    hundreds of instances inside a centimetre and a fast one leaves four -- the result would
    depend on how quickly you moved rather than on where you went."""
    stage = _stage_with_floor()
    c = _points_brush(stage, radius=4.0, strength=1.0)

    for step in range(12):
        _stroke(c, (5.0 + step * 0.2, 5.0, 0.0))    # a slow drag over 2.4 units

    assert len(_points(stage, c)) == 1, "a slow drag piled points on top of each other"


def test_a_drag_across_the_surface_leaves_a_trail() -> None:
    stage = _stage_with_floor()
    c = _points_brush(stage, radius=2.0, strength=1.0)

    for step in range(5):
        _stroke(c, (2.0 + step * 5.0, 5.0, 0.0))

    assert len(_points(stage, c)) == 5


def test_clear_removes_placed_points() -> None:
    stage = _stage_with_floor()
    c = _points_brush(stage, radius=2.0, strength=1.0)
    _stroke(c, (5.0, 5.0, 0.0))
    assert _points(stage, c)

    brush_mod._clear_paint(c.get_param("clear_paint"), None, None)

    assert _points(stage, c) == []


def test_switching_back_to_density_paints_a_field_again() -> None:
    """The modes share a cursor and nothing else; switching must not leave the brush half in
    the other one."""
    stage = _stage_with_floor()
    c = _points_brush(stage, radius=5.0, strength=1.0)
    _stroke(c, (5.0, 5.0, 0.0))

    c.get_param("output").value = brush_mod._OUTPUT_DENSITY
    _stroke(c, (5.0, 5.0, 0.0))

    assert max(_weights(stage)) > 0.0


def test_a_stroke_does_not_re_read_the_surface_every_tick() -> None:
    """Measured, not assumed: a tick on a 14.6k-vertex floor cost 173ms -- six frames a
    second -- and 77% of it was rebuilding the base colour from the property stack, fourteen
    thousand Gf.Vec3f at a time. Nothing about the surface can change mid-stroke: we are the
    only writer of the weights, and a mesh does not repaint itself while you drag on it."""
    stage = _stage_with_floor()
    c = _brush(stage, radius=5.0, strength=0.4)

    calls = []
    real = brush_mod._base_colour
    brush_mod._base_colour = lambda *a, **k: calls.append(1) or real(*a, **k)
    try:
        for i in range(6):
            _stroke(c, (5.0 + i * 0.1, 5.0, 0.0))
    finally:
        brush_mod._base_colour = real

    assert len(calls) <= 1, f"the surface colour was re-read {len(calls)} times in one stroke"


def test_the_cached_tint_still_matches_a_full_rebuild() -> None:
    """The colours are mutated in place during a stroke rather than rebuilt. That is only safe
    if it lands on the same answer -- a drift here would show as the tint slowly disagreeing
    with the weights."""
    stage = _stage_with_floor()
    c = _brush(stage, radius=6.0, strength=0.35)
    for i in range(4):
        _stroke(c, (5.0, 5.0, 0.0))

    incremental = _tint(stage)
    weights = _weights(stage)
    rebuilt = brush_mod._tint_colours(c, weights, brush_mod._base_colour(None, None))

    for a, b in zip(incremental, rebuilt):
        assert Gf.Vec3f(*a) == pytest.approx(Gf.Vec3f(*b), abs=1e-6)


# ---------------------------------------------------------------------------
# painted points reaching the scatter
# ---------------------------------------------------------------------------

def test_the_brush_publishes_where_it_put_the_points() -> None:
    """The other half of the wire. Without a path to carry, Points mode has no port."""
    stage = _stage_with_floor()
    c = _points_brush(stage, radius=2.0, strength=1.0)

    _stroke(c, (5.0, 5.0, 0.0))

    assert c.get_param("points_path").value == str(brush_mod._points_path(c))


def test_each_placed_point_records_the_surface_facing() -> None:
    """A scatter has the triangle it sampled and takes the facing from that. A placed point
    has no triangle, so the brush records it -- the job Houdini's N does on a point."""
    stage = _stage_with_floor()
    c = _points_brush(stage, radius=2.0, strength=1.0)
    tilted = Gf.Vec3f(0.0, 0.6, 0.8)

    chunk_gizmo.apply_stroke(c, "paint", [(Gf.Vec3d(5, 5, 0), 1.0, tilted)])

    _points_list, normals = brush_mod._current_points(c, stage)
    assert normals[0] == pytest.approx(tilted, abs=1e-6)


def test_scatter_places_one_instance_per_painted_point() -> None:
    """The end of the slice: paint points, get an instance at each, at exactly those spots."""
    stage = _stage_with_floor()
    c = _points_brush(stage, radius=3.0, strength=1.0)
    for x in (2.0, 8.0, 14.0):
        _stroke(c, (x, 5.0, 0.0))

    placed, normals = scatter_mod._painted_points(stage, c.get_param("points_path").value)

    assert len(placed) == 3
    assert sorted(round(p[0], 3) for p in placed) == [2.0, 8.0, 14.0]
    assert len(normals) == len(placed)


def test_nothing_wired_means_no_placed_points() -> None:
    """The common case, and it must cost nothing but a path lookup."""
    stage = _stage_with_floor()

    assert scatter_mod._painted_points(stage, "") == ([], [])
    assert scatter_mod._painted_points(stage, "/nope/missing") == ([], [])


def test_points_placed_before_normals_were_recorded_still_load() -> None:
    """Migration: a scene painted an hour ago has points and no normals. They lie flat rather
    than failing to place."""
    stage = _stage_with_floor()
    c = _points_brush(stage, radius=2.0, strength=1.0)
    _stroke(c, (5.0, 5.0, 0.0))
    UsdGeom.Points(stage.GetPrimAtPath(brush_mod._points_path(c))).GetNormalsAttr().Clear()

    placed, normals = scatter_mod._painted_points(stage, c.get_param("points_path").value)

    assert len(placed) == 1
    assert len(normals) == 1


def test_clear_resets_the_stroke_count() -> None:
    """Reported twice: clearing left every instance standing, and then left the count standing.

    Clear does not merely change the paint, it takes it all away -- a count of strokes that
    survived having nothing left to count read as the button not having worked. It is still a
    param write, so the chain hears about it.
    """
    stage = _stage_with_floor()
    c = _brush(stage, radius=5.0, strength=1.0)
    for _ in range(3):
        _stroke(c, (5.0, 5.0, 0.0))
        c.stop_gizmo()
    assert c.get_param("stroke_count").value > 0

    brush_mod._clear_paint(c.get_param("clear_paint"), None, None)

    assert c.get_param("stroke_count").value == 0


def test_fill_recooks_the_chain() -> None:
    stage = _stage_with_floor()
    c = _brush(stage)
    before = c.get_param("stroke_count").value

    brush_mod._fill_paint(c.get_param("fill_paint"), None, None)

    assert c.get_param("stroke_count").value > before


def test_show_paint_hides_the_placed_points_too() -> None:
    """Show Paint means "let me see what I painted", and in Points mode what you painted *is*
    the points. Leaving them lit while the tint went away turned off only half of it."""
    stage = _stage_with_floor()
    c = _points_brush(stage, radius=2.0, strength=1.0)
    _stroke(c, (5.0, 5.0, 0.0))
    prim = stage.GetPrimAtPath(brush_mod._points_path(c))
    assert UsdGeom.Imageable(prim).ComputeVisibility() != UsdGeom.Tokens.invisible

    c.get_param("show_paint").value = False

    prim = stage.GetPrimAtPath(brush_mod._points_path(c))
    assert UsdGeom.Imageable(prim).ComputeVisibility() == UsdGeom.Tokens.invisible


def test_hiding_the_points_does_not_delete_them() -> None:
    """Visibility, not deletion -- unticking a display option must lose nothing."""
    stage = _stage_with_floor()
    c = _points_brush(stage, radius=2.0, strength=1.0)
    _stroke(c, (5.0, 5.0, 0.0))

    c.get_param("show_paint").value = False

    placed, _normals = brush_mod._current_points(c, stage)
    assert len(placed) == 1


def test_mode_sits_above_the_settings_it_governs() -> None:
    """Layout, reported twice. Mode decides what Radius and Strength even do."""
    c = _brush(_stage_with_floor())
    names = [p.name for p in c.get_param("brush_grp").get_my_params()]

    assert names.index("mode") < names.index("radius") < names.index("strength")


def test_wiring_painted_points_hides_the_distribution_settings() -> None:
    """Reported: with Painted Points connected, Auto Spacing, Spacing, Max Count and Seed all
    still sat there doing nothing. A control that cannot affect the result is worse than an
    absent one -- it invites you to reach for it when something looks wrong."""
    import importlib

    from usd_chunks import chunk as chunk_api
    scatter_points = importlib.import_module("scripts.scatter_points")
    sc = scatter_points.main()

    def hidden(name):
        return sc.get_param(name).has_state(chunk_api.ChunkFlags.State.HIDDEN)

    assert not any(hidden(n) for n in scatter_points._SAMPLING_ONLY), "nothing wired yet"

    sc.get_param("points_prim").value = "/chunks/ScatterBrush/PaintedPoints"
    assert all(hidden(n) for n in scatter_points._SAMPLING_ONLY)

    sc.get_param("points_prim").value = ""
    assert not any(hidden(n) for n in scatter_points._SAMPLING_ONLY), "they must come back"


def test_subtract_erases_placed_points() -> None:
    """Reported: Subtract quietly went on adding in Points mode -- the mode was read only by
    the density path -- so the only way to remove anything was Clear, which removes everything."""
    stage = _stage_with_floor()
    c = _points_brush(stage, radius=3.0, strength=1.0)
    for x in (2.0, 8.0, 14.0):
        _stroke(c, (x, 5.0, 0.0))
    assert len(_points(stage, c)) == 3

    c.get_param("mode").value = "Subtract"
    _stroke(c, (8.0, 5.0, 0.0))

    remaining = [round(p[0], 3) for p in _points(stage, c)]
    assert remaining == [2.0, 14.0], "the point under the brush should have gone"


def test_subtract_leaves_points_outside_the_brush() -> None:
    stage = _stage_with_floor()
    c = _points_brush(stage, radius=2.0, strength=1.0)
    for x in (2.0, 12.0):
        _stroke(c, (x, 5.0, 0.0))

    c.get_param("mode").value = "Subtract"
    assert _stroke(c, (30.0, 30.0, 0.0)) is False, "nothing was under the brush"
    assert len(_points(stage, c)) == 2


def test_clearing_points_recooks_after_they_are_gone() -> None:
    """Reported as having to press Clear twice: the chain was told to recook *before* the
    points were removed, so the first press emptied the cloud and left every instance
    standing."""
    stage = _stage_with_floor()
    c = _points_brush(stage, radius=3.0, strength=1.0)
    _stroke(c, (5.0, 5.0, 0.0))
    c.stop_gizmo()

    seen = []
    counter = c.get_param("stroke_count")
    counter.add_callback(
        lambda owner, notice, sender: seen.append(len(_points(stage, c))),
        send_on_create=False,
    )
    brush_mod._clear_paint(c.get_param("clear_paint"), None, None)

    assert seen, "clearing never told the chain anything"
    assert seen[-1] == 0, "the chain was told to recook while the points were still there"
