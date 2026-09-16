"""Vines grow where a brush painted, and nowhere else.

Two halves, deliberately. The maths — projection, pruning, strand extraction — is exercised
directly, because a hand-written closest-point-on-triangle that is subtly wrong produces vines
that look fine and sit a millimetre inside the wall. The cook is exercised through the registry
with a real Scatter Brush upstream, because the thing worth protecting is not that either chunk
works but that the two *agree*: they talk only through a primvar name and a point cloud, and a
disagreement about either would leave both passing their own tests while nothing grew.

Nothing here draws or clicks. `apply_stroke` takes world-space samples, which is exactly what a
viewport hands over.
"""

from __future__ import annotations

import importlib

import numpy as np
import pytest
from pxr import Gf, Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk_gizmo, chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins.chunk_usd import initialize as chunk_usd_init


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


chunk_registry.ChunkRegistry().initialize(_DummyScene(), "pytest")

vine = importlib.import_module("scripts.modelling.vine")
#: The field name is the brush's to define. Reading it from there rather than restating it
#: is the point: if the two chunks ever stopped agreeing, these tests have to notice.
brush_mod = importlib.import_module("scripts.modelling.scatter_brush")
DENSITY = brush_mod.DENSITY_PRIMVAR


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

WALL = "/World/Wall"
SIZE = 10.0


def _wall(stage, path=WALL, size=SIZE, divisions=30):
    """A vertical grid in the XZ plane — something a vine can plausibly climb."""
    mesh = UsdGeom.Mesh.Define(stage, path)
    points, counts, indices = [], [], []
    step = size / divisions
    for iz in range(divisions + 1):
        for ix in range(divisions + 1):
            points.append(Gf.Vec3f(ix * step, 0.0, iz * step))
    row = divisions + 1
    for iz in range(divisions):
        for ix in range(divisions):
            a = iz * row + ix
            counts.append(4)
            indices.extend([a, a + 1, a + row + 1, a + row])
    mesh.GetPointsAttr().Set(points)
    mesh.GetFaceVertexCountsAttr().Set(counts)
    mesh.GetFaceVertexIndicesAttr().Set(indices)
    mesh.GetExtentAttr().Set([Gf.Vec3f(0, 0, 0), Gf.Vec3f(size, 0, size)])
    return mesh


def _paint(mesh, weight_of, name=None):
    """Author the density primvar from `weight_of(point) -> 0..1`."""
    primvar = UsdGeom.PrimvarsAPI(mesh.GetPrim()).CreatePrimvar(
        name or DENSITY, Sdf.ValueTypeNames.FloatArray, UsdGeom.Tokens.vertex
    )
    primvar.Set(Vt.FloatArray([float(weight_of(p)) for p in mesh.GetPointsAttr().Get()]))
    return primvar


class _Harness:
    """A stage with the chunk framework live on it, and chunks made the way the host makes them."""

    def __init__(self):
        self.stage = Usd.Stage.CreateInMemory()
        UsdGeom.SetStageUpAxis(self.stage, UsdGeom.Tokens.z)
        stage = self.stage

        class _Scene(_DummyScene):
            def get_stage(self):
                return stage

        self.scene = _Scene()
        self.registry = chunk_registry.ChunkRegistry()
        self.registry.initialize(self.scene, "pytest")
        chunk_registry.ChunkLayerManager().initialize(self.stage)
        chunk_usd_init(self.stage, self.scene)
        self.mesh = _wall(self.stage)

    def chunk(self, module_name):
        module = self.registry.get_chunk_module(module_name)
        chunks = module.create() if module.loaded else module.load()
        for c in chunks:
            self.scene.add_chunk(c)
        return chunks[0]

    def vine(self, **params):
        node = self.chunk("vine")
        node.get_param("surface_prim").value = WALL
        node.get_param("density_primvar").value = DENSITY
        for name, value in params.items():
            node.get_param(name).value = value
        return node

    def curves(self, path="/Vine/Curves"):
        return UsdGeom.BasisCurves(self.stage.GetPrimAtPath(path))

    def leaves(self, path="/Vine/Leaves"):
        return UsdGeom.Points(self.stage.GetPrimAtPath(path))

    def points(self, path="/Vine/Curves"):
        return np.asarray(self.curves(path).GetPointsAttr().Get() or [], dtype=np.float64).reshape(-1, 3)


@pytest.fixture
def host():
    return _Harness()


def _stripe(low, high):
    """Full weight between two x values, nothing outside."""
    return lambda p: 1.0 if low <= p[0] <= high else 0.0


# ---------------------------------------------------------------------------
# the maths
# ---------------------------------------------------------------------------


def _brute_closest(point, v0, v1, v2, samples=60):
    """Closest point on one triangle, found by densely sampling it.

    Slow and obviously correct, which is the point: `_closest_on_triangles` is a hand-written
    region test, and the failure it can produce — a vine sitting a hair inside the wall — is
    invisible in a picture and permanent in a shot.
    """
    best, best_d = None, np.inf
    for i in range(samples + 1):
        for j in range(samples + 1 - i):
            u = i / samples
            v = j / samples
            candidate = v0 + (v1 - v0) * u + (v2 - v0) * v
            d = float(np.linalg.norm(candidate - point))
            if d < best_d:
                best, best_d = candidate, d
    return best, best_d


@pytest.mark.parametrize(
    "point",
    [
        (0.3, 0.3, 5.0),      # over the interior
        (-4.0, -4.0, 1.0),    # past a vertex
        (0.5, -3.0, 0.0),     # past an edge
        (2.0, 2.0, -1.0),     # past the far edge
        (0.25, 0.25, 0.0),    # already on it
    ],
)
def test_the_closest_surface_point_matches_a_brute_force_search(point) -> None:
    v0 = np.asarray([[0.0, 0.0, 0.0]])
    v1 = np.asarray([[1.0, 0.0, 0.0]])
    v2 = np.asarray([[0.0, 1.0, 0.0]])

    found = vine._closest_on_triangles(np.asarray(point, dtype=np.float64), v0, v1, v2)[0]
    expected, expected_d = _brute_closest(np.asarray(point, dtype=np.float64), v0[0], v1[0], v2[0])

    # The brute force only samples the triangle, so it is the loose one — ours may only be better.
    assert float(np.linalg.norm(found - np.asarray(point))) <= expected_d + 1e-9
    assert float(np.linalg.norm(found - expected)) < 0.05


def test_a_degenerate_triangle_does_not_produce_a_nan() -> None:
    """A zero-area face is ordinary in scanned geometry, and one NaN poisons the whole curve."""
    zero = np.zeros((1, 3))
    found = vine._closest_on_triangles(np.asarray([1.0, 1.0, 1.0]), zero, zero, zero)
    assert np.isfinite(found).all()


def _chain(count, branches=()):
    """A skeleton: one trunk of `count` nodes, plus (attach_index, length) side branches."""
    nodes = vine._Nodes()
    up = np.asarray([0.0, 0.0, 1.0])
    parent = -1
    for i in range(count):
        parent = nodes.add(np.asarray([0.0, 0.0, float(i)]), parent, up, up)
    for attach, length in branches:
        at = attach
        for j in range(length):
            at = nodes.add(np.asarray([float(j + 1), 0.0, float(attach)]), at, up, up)
    return nodes


def test_a_fork_continues_into_its_longest_branch() -> None:
    """Continuing into the first child instead breaks a vine into stubs wherever a side shoot
    happened to be created first, and a curve renderer draws every break."""
    nodes = _chain(10, branches=[(3, 2)])

    strands = vine._strands(nodes)
    longest = max(strands, key=len)

    assert len(longest) == 10
    assert [nodes.position[i][2] for i in longest] == [float(i) for i in range(10)]


def test_a_side_branch_starts_at_the_fork_it_left() -> None:
    """So the two curves overlap by a point and the branch reads as joined, not floating."""
    nodes = _chain(10, branches=[(3, 4)])

    side = min(vine._strands(nodes), key=len)

    assert side[0] == 3


def test_short_side_branches_are_pruned_and_longer_ones_are_not() -> None:
    nodes = _chain(10, branches=[(2, 2), (5, 3)])

    kept = vine._prune(nodes, 3)
    strands = vine._strands(kept)

    assert len(kept) == 13  # the two-node stub is gone, the three-node branch stays
    assert max(len(s) for s in strands) == 10
    assert len(strands) == 2


def test_the_longest_path_is_the_one_kept_whole() -> None:
    """"Trunk" is not a thing the growth records — it is whatever path runs furthest. A side
    shoot that outgrows the stem it left becomes the vine, and the stem becomes the side shoot,
    which is why pruning is judged on subtree length rather than on creation order."""
    nodes = _chain(10, branches=[(5, 8)])

    kept = vine._prune(nodes, 6)

    # The four trunk nodes past the fork are the shorter branch, and go.
    assert len(kept) == 14
    assert max(len(s) for s in vine._strands(kept)) == 14


def test_pruning_keeps_a_tip_however_short_it_is() -> None:
    """A branch's continuation is never a stub — pruning it would blunt every vine."""
    nodes = _chain(3)

    assert len(vine._prune(nodes, 20)) == 3


def test_a_pruned_skeleton_still_has_every_parent_before_its_child() -> None:
    """Every bottom-up pass in this module is one reversed loop, which only works because of it."""
    kept = vine._prune(_chain(10, branches=[(2, 1), (4, 6), (6, 1)]), 4)

    assert all(parent < index for index, parent in enumerate(kept.parent))


def test_the_pipe_model_makes_a_fork_thicker_than_either_branch() -> None:
    nodes = _chain(6, branches=[(2, 6)])

    radii = vine._radii(nodes, base=1.0, tip=0.1)

    assert radii[0] > radii[5]
    assert radii[-1] == pytest.approx(0.05)  # a tip is exactly the tip width
    assert max(radii) == pytest.approx(0.5)  # and the thickest node is exactly the base


def test_a_vine_that_never_forked_is_its_own_tip_width() -> None:
    """Thickness describes a fork, and an unforked vine has none. Stretching the profile onto
    the range anyway would make a single straight strand as thick as a whole network's trunk."""
    radii = vine._radii(_chain(20), base=1.0, tip=0.1)

    assert radii == [pytest.approx(0.05)] * 20


def test_smoothing_pins_both_ends() -> None:
    """A vine that grew from the ground has to still start there."""
    points = np.asarray([[0.0, 0, 0], [1.0, 1, 0], [2.0, -1, 0], [3.0, 0, 0]])

    smoothed = vine._smooth(points, 3)

    assert smoothed[0] == pytest.approx(points[0])
    assert smoothed[-1] == pytest.approx(points[-1])
    assert abs(smoothed[1][1]) < abs(points[1][1])


# ---------------------------------------------------------------------------
# what the paint decides
# ---------------------------------------------------------------------------


def test_vines_grow_only_where_the_surface_was_painted(host) -> None:
    _paint(host.mesh, _stripe(3.0, 7.0))
    node = host.vine()

    node.execute()
    points = host.points()

    assert len(points)
    # One face of slack at each edge: the weight is interpolated across the boundary triangles,
    # so the paint genuinely reaches a third of a face past the last painted vertex.
    assert points[:, 0].min() > 3.0 - SIZE / 30.0
    assert points[:, 0].max() < 7.0 + SIZE / 30.0


def test_a_wired_but_unpainted_field_grows_nothing(host) -> None:
    """The difference between "no field" and "a field that says no" is the whole point of the
    wire: a scatter or a vine nobody connected a brush to is not quietly governed by one."""
    _paint(host.mesh, lambda p: 0.0)
    node = host.vine()

    node.execute()

    assert not len(host.points())
    assert "nothing painted" in node.get_param("readout").value


def test_with_no_field_wired_the_whole_mesh_is_fair_game(host) -> None:
    node = host.vine(density_primvar="")

    node.execute()
    points = host.points()

    assert len(points)
    assert points[:, 0].max() - points[:, 0].min() > SIZE * 0.5


def test_the_readout_says_what_grew(host) -> None:
    _paint(host.mesh, _stripe(2.0, 8.0))
    node = host.vine(root_count=3)

    node.execute()

    assert "3 vines" in node.get_param("readout").value


# ---------------------------------------------------------------------------
# the curves
# ---------------------------------------------------------------------------


def test_the_curves_are_well_formed(host) -> None:
    _paint(host.mesh, _stripe(2.0, 8.0))
    node = host.vine()

    node.execute()
    curves = host.curves()
    counts = list(curves.GetCurveVertexCountsAttr().Get())
    points = curves.GetPointsAttr().Get()
    widths = curves.GetWidthsAttr().Get()

    assert sum(counts) == len(points) == len(widths)
    assert curves.GetTypeAttr().Get() == UsdGeom.Tokens.cubic
    assert curves.GetBasisAttr().Get() == UsdGeom.Tokens.bspline
    # A cubic B-spline needs four control points before it is a curve at all.
    assert min(counts) >= 4
    assert curves.GetWidthsInterpolation() == UsdGeom.Tokens.vertex


def test_a_smooth_vine_still_starts_where_it_grew(host) -> None:
    """A B-spline stays inside its hull, so without pinned ends every root lifts off the ground."""
    _paint(host.mesh, _stripe(2.0, 8.0))
    node = host.vine()

    node.execute()
    counts = list(host.curves().GetCurveVertexCountsAttr().Get())
    points = host.points()

    first = points[0]
    assert np.allclose(points[1], first)
    assert np.allclose(points[2], first)
    last = points[counts[0] - 1]
    assert np.allclose(points[counts[0] - 2], last)


def test_linear_form_authors_the_polyline_itself(host) -> None:
    _paint(host.mesh, _stripe(2.0, 8.0))
    node = host.vine(curve_form="Linear")

    node.execute()

    assert host.curves().GetTypeAttr().Get() == UsdGeom.Tokens.linear


def test_a_vine_is_thickest_at_its_base(host) -> None:
    _paint(host.mesh, _stripe(2.0, 8.0))
    node = host.vine(thickness=0.5, taper=0.1)

    node.execute()
    widths = list(host.curves().GetWidthsAttr().Get())

    # Thickness and Taper name the two ends of the range, and the vine reaches both of them.
    assert min(widths) < max(widths)
    assert min(widths) == pytest.approx(max(widths) * 0.1, rel=1e-3)


def test_the_vines_sit_on_the_surface_rather_than_in_it(host) -> None:
    """The wall is the plane y=0. A vine has to clear it — a tube lifted by less than its own
    radius is half buried, and on a flat surface seen face-on that is z-fighting."""
    _paint(host.mesh, _stripe(2.0, 8.0))
    node = host.vine()

    node.execute()
    off_wall = np.abs(host.points()[:, 1])
    widest = max(host.curves().GetWidthsAttr().Get())

    assert off_wall.max() > widest * 0.5
    assert off_wall.max() < widest * 3.0


def test_step_decides_how_smoothly_a_vine_curves_and_not_how_thick_it_is(host) -> None:
    """Regression for the reported "cooks but nothing is visible".

    Every visible size used to be a multiple of Step, and Step is a *resolution* — Auto Step is
    the painted region over 150. So growing more finely grew a thinner vine, and at the default
    the stem came out at a five-hundredth of the object: one pixel on screen, a quarter of one
    at the tips. Width belongs to Thickness, which is a share of the region, and Step may not
    touch it.
    """
    _paint(host.mesh, _stripe(2.0, 8.0))
    node = host.vine(auto_step=False, step=0.1)

    node.execute()
    fine = max(host.curves().GetWidthsAttr().Get())
    node.get_param("step").value = 0.4
    node.execute()
    coarse = max(host.curves().GetWidthsAttr().Get())

    assert fine == pytest.approx(coarse)


def test_a_vine_is_a_visible_fraction_of_what_it_grew_on(host) -> None:
    """The figure that was wrong when this could not be seen. A stem near a hundredth of the
    painted region reads as a vine in a viewport; a five-hundredth is a sub-pixel line."""
    _paint(host.mesh, _stripe(2.0, 8.0))
    node = host.vine()

    node.execute()
    points = host.points()
    region = float(np.linalg.norm(points.max(axis=0) - points.min(axis=0)))
    widest = max(host.curves().GetWidthsAttr().Get())

    assert 1 / 250.0 < widest / region < 1 / 30.0


def test_a_recook_replaces_the_previous_vine_rather_than_adding_to_it(host) -> None:
    _paint(host.mesh, _stripe(2.0, 8.0))
    node = host.vine()

    node.execute()
    first = len(host.points())
    node.execute()

    assert len(host.points()) == first


def test_the_same_seed_grows_the_same_vine(host) -> None:
    _paint(host.mesh, _stripe(2.0, 8.0))
    node = host.vine(seed=7)

    node.execute()
    once = host.points()
    node.execute()

    assert np.allclose(host.points(), once)


def test_a_different_seed_grows_a_different_vine(host) -> None:
    _paint(host.mesh, _stripe(2.0, 8.0))
    node = host.vine(seed=1)

    node.execute()
    first = host.points()
    node.get_param("seed").value = 2
    node.execute()
    second = host.points()

    assert first.shape != second.shape or not np.allclose(first, second)


def test_the_vine_path_moves_the_result(host) -> None:
    _paint(host.mesh, _stripe(2.0, 8.0))
    node = host.vine(vine_path="/Growth/Ivy")

    node.execute()

    assert node.get_param("out_curves").value == "/Growth/Ivy/Curves"
    assert len(host.points("/Growth/Ivy/Curves"))


# ---------------------------------------------------------------------------
# leaves
# ---------------------------------------------------------------------------


def test_every_leaf_point_carries_the_surface_normal(host) -> None:
    """The same shape a Scatter Brush publishes, which is what lets Scatter Points instance
    along a vine with the code it already has."""
    _paint(host.mesh, _stripe(2.0, 8.0))
    node = host.vine()

    node.execute()
    cloud = host.leaves()
    points = cloud.GetPointsAttr().Get()
    normals = cloud.GetNormalsAttr().Get()

    assert len(points) > 0
    assert len(normals) == len(points)
    assert all(abs(Gf.Vec3f(n).GetLength() - 1.0) < 1e-4 for n in normals)


def test_wider_leaf_spacing_leaves_fewer_leaves(host) -> None:
    _paint(host.mesh, _stripe(2.0, 8.0))
    node = host.vine(leaf_spacing=2.0)

    node.execute()
    dense = len(host.leaves().GetPointsAttr().Get())
    node.get_param("leaf_spacing").value = 8.0
    node.execute()

    assert len(host.leaves().GetPointsAttr().Get()) < dense


def test_the_leaf_cloud_exists_even_with_leaves_turned_off(host) -> None:
    """Wiring an output is how you decide to use it, so it cannot require having used it —
    the same reason the brush publishes its points path before anything is painted."""
    _paint(host.mesh, _stripe(2.0, 8.0))
    node = host.vine(leaves=False)

    node.execute()

    assert host.leaves().GetPrim().IsValid()
    assert not len(host.leaves().GetPointsAttr().Get() or [])


def test_the_outputs_are_published_before_anything_grows(host) -> None:
    node = host.vine()
    node.get_param("surface_prim").value = ""

    node.execute()

    assert node.get_param("out_curves").value == "/Vine/Curves"
    assert node.get_param("out_leaves").value == "/Vine/Leaves"


# ---------------------------------------------------------------------------
# the brush, wired up for real
# ---------------------------------------------------------------------------


def test_a_brush_stroke_decides_where_the_vine_grows(host) -> None:
    """The integration that matters. The two chunks share nothing but a primvar name, carried
    over a wire — so this is what would fail if either end changed its mind about it."""
    brush = host.chunk("scatter_brush")
    brush.get_param("surface_prim").value = WALL
    brush.get_param("radius").value = 1.5
    brush.get_param("strength").value = 1.0
    brush.get_param("show_paint").value = False

    node = host.vine()
    node.add_dependency(brush)
    node.get_param("density_primvar").value = brush.get_param("density_name").value

    chunk_gizmo.apply_stroke(
        brush, "paint", [(Gf.Vec3d(7.5, 0.0, 0.5 + t * 0.2), 1.0) for t in range(40)]
    )
    node.execute()
    points = host.points()

    assert len(points)
    # Painted a vertical band on the right of the wall; nothing may have grown on the left.
    assert points[:, 0].min() > 5.0


def test_painted_points_become_the_roots(host) -> None:
    """Every dab is a vine. Otherwise the roots are chosen for you, and placing them by hand is
    the reason to reach for Points mode on the brush at all."""
    _paint(host.mesh, _stripe(0.0, SIZE))
    cloud = UsdGeom.Points.Define(host.stage, "/Painted")
    seeds = [Gf.Vec3f(2.0, 0.0, 1.0), Gf.Vec3f(5.0, 0.0, 1.0), Gf.Vec3f(8.0, 0.0, 1.0)]
    cloud.CreatePointsAttr().Set(Vt.Vec3fArray(seeds))

    node = host.vine(seed_points="/Painted", root_count=99)
    node.execute()

    assert "3 vines from painted points" in node.get_param("readout").value
    # Each root is where it was painted, give or take the lift off the wall.
    points = host.points()
    for seed in seeds:
        reach = np.linalg.norm(points - np.asarray(seed, dtype=np.float64), axis=1)
        assert reach.min() < 0.2


def test_the_leaf_cloud_drives_a_scatter_points_chunk(host) -> None:
    """The whole reason the cloud has that shape.

    Leaves are somebody's asset, so this chunk grows none — it publishes points with a normal,
    which is exactly what a Scatter Brush publishes, and Scatter Points already knows how to
    place one instance per point and lay it flat. That contract is two chunks wide and written
    down nowhere but here, so it is worth a test that actually runs both.
    """
    _paint(host.mesh, _stripe(2.0, 8.0))
    leaf = UsdGeom.Mesh.Define(host.stage, "/Assets/Leaf")
    leaf.GetPointsAttr().Set([
        Gf.Vec3f(-0.15, -0.05, 0), Gf.Vec3f(0.15, -0.05, 0),
        Gf.Vec3f(0.15, 0.25, 0), Gf.Vec3f(-0.15, 0.25, 0),
    ])
    leaf.GetFaceVertexCountsAttr().Set([4])
    leaf.GetFaceVertexIndicesAttr().Set([0, 1, 2, 3])

    node = host.vine()
    node.execute()

    scatter = host.chunk("scatter_points")
    scatter.add_dependency(node)
    scatter.get_param("points_prim").value = node.get_param("out_leaves").value
    scatter.get_param("inst_path").value = "/LeafInstancer"
    scatter.get_param("orient_mode").value = "Align to Normal"
    rows = scatter.get_param("prototypes")
    rows.size = 1
    rows.get_indexed_param(rows.get_param("proto_path"), 0).value = "/Assets/Leaf"
    scatter.execute()

    cloud = np.asarray(host.leaves().GetPointsAttr().Get(), dtype=np.float64)
    instancer = UsdGeom.PointInstancer(host.stage.GetPrimAtPath("/LeafInstancer"))
    placed = np.asarray(instancer.GetPositionsAttr().Get(), dtype=np.float64)

    assert len(cloud) > 0
    assert len(placed) == len(cloud)
    # One instance per leaf, standing exactly where the leaf was.
    assert np.linalg.norm(placed[:, None, :] - cloud[None, :, :], axis=2).min(axis=1).max() < 1e-5
    assert len(instancer.GetOrientationsAttr().Get()) == len(cloud)


def test_wiring_painted_points_hides_the_root_count(host) -> None:
    """A control that cannot affect the result is worse than an absent one."""
    from usd_chunks import chunk

    _paint(host.mesh, _stripe(0.0, SIZE))
    node = host.vine()
    node.execute()
    assert not node.get_param("root_count").has_state(chunk.ChunkFlags.State.HIDDEN)

    node.get_param("seed_points").value = "/Painted"
    node.execute()

    assert node.get_param("root_count").has_state(chunk.ChunkFlags.State.HIDDEN)
