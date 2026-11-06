"""Remesh Proxy: what a stand-in mesh is judged on.

One closed, outward-facing surface where the asset was; gaps bridged without the outline
growing; hidden internals gone; the asset's colour on it; and every copy of an instanced
part counted. Plus the usual: it never reads its own output back.
"""

from __future__ import annotations

import importlib
import time

import numpy as np
import pytest
from chunk_stubs import register_stub_module
from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade, Vt
from PySide6 import QtCore

from usd_chunks import chunk, chunk_registry, cook_cache, sim_bake, surface_samples
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import initialize as _chunk_usd_init

Image = pytest.importorskip("PIL.Image")
sparse = pytest.importorskip("scipy.sparse")
csgraph = pytest.importorskip("scipy.sparse.csgraph")
rp = importlib.import_module("chunks.modelling.remesh_proxy")

pytestmark = pytest.mark.skipif(not rp._HAS_DEPS, reason="needs scipy and scikit-image")


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


_BOX_FACES = [0, 2, 3, 1, 4, 5, 7, 6, 0, 1, 5, 4, 2, 6, 7, 3, 0, 4, 6, 2, 1, 3, 7, 5]


def _box_points(lo, hi):
    return np.array(
        [[x, y, z] for z in (lo[2], hi[2]) for y in (lo[1], hi[1]) for x in (lo[0], hi[0])], dtype=np.float32
    )


def _define(stage, path, points, counts, indices, colour=None):
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.GetPointsAttr().Set(Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(points, dtype=np.float32)))
    mesh.GetFaceVertexCountsAttr().Set(list(map(int, counts)))
    mesh.GetFaceVertexIndicesAttr().Set(list(map(int, indices)))
    if colour is not None:
        mesh.CreateDisplayColorAttr([Gf.Vec3f(*colour)])
    return mesh


def _box(stage, path, lo=(-1, -1, -1), hi=(1, 1, 1), colour=None):
    return _define(stage, path, _box_points(lo, hi), [4] * 6, _BOX_FACES, colour)


def _cards(stage, path, centres, size=0.15, seed=1, colour=(0.1, 0.5, 0.1)):
    """One mesh of loose, randomly oriented square cards -- a stand-in for leaves."""
    rng = np.random.default_rng(seed)
    points, indices = [], []
    for c in centres:
        a, b = rng.normal(size=3) * size, rng.normal(size=3) * size
        base = len(points)
        points += [c - a - b, c + a - b, c + a + b, c - a + b]
        indices += [base, base + 1, base + 2, base + 3]
    return _define(stage, path, np.asarray(points), [4] * len(centres), indices, colour)


def _canopy_centres(n=400, seed=1, centre=(0.0, 0.0, 2.0)):
    rng = np.random.default_rng(seed)
    d = rng.normal(size=(n, 3))
    d /= np.linalg.norm(d, axis=1, keepdims=True)
    return d * rng.uniform(0.3, 1.0, size=(n, 1)) + np.asarray(centre)


def _components(points, triangles) -> int:
    edges = np.concatenate([triangles[:, [0, 1]], triangles[:, [1, 2]]])
    n = len(points)
    graph = sparse.coo_matrix((np.ones(len(edges)), (edges[:, 0], edges[:, 1])), shape=(n, n))
    return csgraph.connected_components(graph)[0]


def _signed_volume(points, triangles) -> float:
    a, b, c = points[triangles[:, 0]], points[triangles[:, 1]], points[triangles[:, 2]]
    return float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)


def _settings(**overrides):
    values = dict(resolution=64, thickness=1.0, close=0.0, fill=True, smooth=0.5, bake=True)
    values.update(overrides)
    return rp._Settings(**values)


def _proxy(stage, path="/Asset", **overrides):
    return rp.build_proxy(surface_samples.gather_sources(stage.GetPrimAtPath(path)), _settings(**overrides))


class _DummyScene(SceneSender):
    def __init__(self, stage=None):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture
def wired(tmp_path, monkeypatch):
    # A private cook cache: the user's would answer a cook from an earlier run, and these
    # tests are about what the chunk itself does.
    monkeypatch.setenv(cook_cache.ENV_DIR, str(tmp_path / "cook_cache"))
    stage = Usd.Stage.CreateInMemory()
    scene = _DummyScene(stage)
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)

    def make(budget=0):
        module = reg.get_chunk_module("remesh_proxy")
        chunks = module.create() if module.loaded else module.load()
        for c in chunks:
            scene.add_chunk(c)
        # The test assets are a few dozen triangles: remesh them anyway, unless a test is
        # about keeping a small source as it is.
        chunks[0].get_param("budget").value = budget
        return chunks[0]

    return stage, make


# ---------------------------------------------------------------------------
# the volume and its surface
# ---------------------------------------------------------------------------


def test_a_box_comes_back_as_one_closed_outward_facing_surface() -> None:
    stage = Usd.Stage.CreateInMemory()
    _box(stage, "/Asset")
    proxy = _proxy(stage)

    assert _components(proxy.points, proxy.triangles) == 1
    volume = _signed_volume(proxy.points, proxy.triangles)
    assert volume > 0.0  # positive: faces wound counter-clockwise seen from outside
    # A 2-unit box, filled solid and grown by about a voxel all round.
    assert 8.0 < volume < (2.0 + 4.0 * proxy.voxel) ** 3


def test_closing_merges_scattered_cards_into_one_mass() -> None:
    """The reason the chunk exists: a canopy of loose cards is hundreds of pieces until the
    gaps are closed, and one piece after."""
    stage = Usd.Stage.CreateInMemory()
    _cards(stage, "/Asset/Leaves", _canopy_centres(150, seed=3))

    open_proxy = _proxy(stage, resolution=96, close=0.0)
    closed_proxy = _proxy(stage, resolution=96, close=6.0)
    assert _components(open_proxy.points, open_proxy.triangles) > 1
    assert _components(closed_proxy.points, closed_proxy.triangles) == 1


def test_closing_bridges_gaps_without_growing_the_outline() -> None:
    stage = Usd.Stage.CreateInMemory()
    _box(stage, "/Asset")
    plain = _proxy(stage, close=0.0)
    closed = _proxy(stage, close=6.0)
    voxel = plain.voxel
    assert np.allclose(plain.points.max(axis=0), closed.points.max(axis=0), atol=1.5 * voxel)


def test_fill_interior_removes_hidden_internals_and_double_walls() -> None:
    """A CAD assembly: an outer shell with a part sealed inside it. Filled, the inner part
    and the shell's inside wall are gone; unfilled, both show up as extra surfaces."""
    stage = Usd.Stage.CreateInMemory()
    _box(stage, "/Asset/Shell")
    _box(stage, "/Asset/Inside", lo=(-0.3, -0.3, -0.3), hi=(0.3, 0.3, 0.3))

    filled = _proxy(stage, fill=True)
    hollow = _proxy(stage, fill=False)
    assert _components(filled.points, filled.triangles) == 1
    assert _components(hollow.points, hollow.triangles) >= 3  # outside, inside wall, part
    assert len(filled.triangles) < len(hollow.triangles)


def test_an_empty_asset_builds_nothing() -> None:
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/Asset")
    assert _proxy(stage) is None


# ---------------------------------------------------------------------------
# what gets sampled
# ---------------------------------------------------------------------------


def test_every_point_instancer_copy_is_covered() -> None:
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/Asset")
    instancer = UsdGeom.PointInstancer.Define(stage, "/Asset/Leaves")
    _box(stage, "/Asset/Leaves/Protos/Box", lo=(-0.2, -0.2, -0.2), hi=(0.2, 0.2, 0.2))
    instancer.CreatePrototypesRel().SetTargets([Sdf.Path("/Asset/Leaves/Protos/Box")])
    instancer.CreatePositionsAttr([Gf.Vec3f(-3, 0, 0), Gf.Vec3f(0, 0, 0), Gf.Vec3f(3, 0, 0)])
    instancer.CreateProtoIndicesAttr([0, 0, 0])

    sources = surface_samples.gather_sources(stage.GetPrimAtPath("/Asset"))
    assert len(sources) == 1 and len(sources[0].placements) == 3  # prototype read once, placed 3x
    proxy = rp.build_proxy(sources, _settings(resolution=96))
    assert _components(proxy.points, proxy.triangles) == 3
    assert proxy.points[:, 0].min() < -3.1 and proxy.points[:, 0].max() > 3.1


def test_hidden_and_guide_geometry_is_left_out() -> None:
    stage = Usd.Stage.CreateInMemory()
    _box(stage, "/Asset/Body")
    hidden = _box(stage, "/Asset/Hidden", lo=(5, 5, 5), hi=(6, 6, 6))
    hidden.CreateVisibilityAttr(UsdGeom.Tokens.invisible)
    guide = _box(stage, "/Asset/Guide", lo=(-6, -6, -6), hi=(-5, -5, -5))
    guide.CreatePurposeAttr(UsdGeom.Tokens.guide)

    assert [s.path for s in surface_samples.gather_sources(stage.GetPrimAtPath("/Asset"))] == ["/Asset/Body"]


# ---------------------------------------------------------------------------
# colour
# ---------------------------------------------------------------------------


def test_each_part_bakes_its_own_display_colour() -> None:
    stage = Usd.Stage.CreateInMemory()
    _box(stage, "/Asset/Trunk", lo=(-0.2, -0.2, 0.0), hi=(0.2, 0.2, 1.0), colour=(0.4, 0.25, 0.1))
    _cards(stage, "/Asset/Leaves", _canopy_centres(300), colour=(0.1, 0.5, 0.1))
    proxy = _proxy(stage, resolution=96, close=4.0)

    trunk = proxy.colours[proxy.points[:, 2] < 0.6].mean(axis=0)
    canopy = proxy.colours[proxy.points[:, 2] > 2.0].mean(axis=0)
    assert np.allclose(trunk, (0.4, 0.25, 0.1), atol=0.03)
    assert np.allclose(canopy, (0.1, 0.5, 0.1), atol=0.03)


def _preview_material(stage, path, colour=None, texture=None, cutout=False):
    material = UsdShade.Material.Define(stage, path)
    shader = UsdShade.Shader.Define(stage, f"{path}/Surface")
    shader.CreateIdAttr("UsdPreviewSurface")
    material.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
    if colour is not None:
        shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*colour))
    if texture is not None:
        tex = UsdShade.Shader.Define(stage, f"{path}/Texture")
        tex.CreateIdAttr("UsdUVTexture")
        tex.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(texture)
        tex.CreateInput("sourceColorSpace", Sdf.ValueTypeNames.Token).Set("raw")
        shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(
            tex.ConnectableAPI(), "rgb"
        )
        if cutout:
            shader.CreateInput("opacity", Sdf.ValueTypeNames.Float).ConnectToSource(tex.ConnectableAPI(), "a")
            shader.CreateInput("opacityThreshold", Sdf.ValueTypeNames.Float).Set(0.5)
    return material


def test_a_bound_material_wins_over_display_colour() -> None:
    stage = Usd.Stage.CreateInMemory()
    mesh = _box(stage, "/Asset/Body", colour=(1.0, 0.0, 0.0))
    material = _preview_material(stage, "/Looks/Blue", colour=(0.0, 0.0, 1.0))
    UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(material)

    proxy = _proxy(stage)
    assert np.allclose(proxy.colours.mean(axis=0), (0.0, 0.0, 1.0), atol=1e-4)


def test_a_cut_out_card_only_becomes_volume_where_it_is_opaque(tmp_path) -> None:
    """A leaf card's texture is mostly transparent. Sampling the whole card would give the
    proxy a slab where the renderer shows a leaf; the cut-away half must contribute nothing."""
    rgba = np.zeros((16, 16, 4), dtype=np.uint8)
    rgba[..., 1] = 200  # green
    rgba[:, :8, 3] = 255  # left half (u < 0.5) opaque, right half transparent
    png = tmp_path / "leaf.png"
    Image.fromarray(rgba, "RGBA").save(png)

    stage = Usd.Stage.CreateInMemory()
    card = _define(
        stage, "/Asset/Card", np.array([[0, 0, 0], [4, 0, 0], [4, 1, 0], [0, 1, 0]]), [4], [0, 1, 2, 3]
    )
    UsdGeom.PrimvarsAPI(card).CreatePrimvar("st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.vertex).Set(
        Vt.Vec2fArray([(0, 0), (1, 0), (1, 1), (0, 1)])
    )
    material = _preview_material(stage, "/Looks/Leaf", texture=str(png), cutout=True)
    UsdShade.MaterialBindingAPI.Apply(card.GetPrim()).Bind(material)

    proxy = _proxy(stage, resolution=64)
    assert proxy.points[:, 0].max() < 2.0 + 4.0 * proxy.voxel  # stops where the alpha does
    assert np.allclose(proxy.colours.mean(axis=0), (0.0, 200 / 255, 0.0), atol=0.02)


# ---------------------------------------------------------------------------
# the chunk
# ---------------------------------------------------------------------------


def test_the_chunk_writes_a_coloured_proxy_beside_the_asset(wired) -> None:
    stage, make = wired
    _box(stage, "/World/Asset", colour=(0.2, 0.4, 0.6))

    node = make()
    node.get_param("resolution").value = 48
    node.get_param("prim_path").value = "/World/Asset"

    proxy = UsdGeom.Mesh(stage.GetPrimAtPath("/World/Asset_proxy"))
    assert proxy
    assert node.get_param("out_mesh").value == "/World/Asset_proxy"
    counts = proxy.GetFaceVertexCountsAttr().Get()
    assert set(counts) == {3} and node.get_param("out_triangles").value == len(counts)
    colours = np.asarray(proxy.GetDisplayColorPrimvar().Get())
    assert len(colours) == len(proxy.GetPointsAttr().Get())
    assert np.allclose(colours.mean(axis=0), (0.2, 0.4, 0.6), atol=1e-4)
    # The asset itself is untouched.
    assert len(UsdGeom.Mesh(stage.GetPrimAtPath("/World/Asset")).GetFaceVertexCountsAttr().Get()) == 6


def test_a_proxy_inside_the_asset_is_not_sampled_on_the_next_cook(wired) -> None:
    """Written under the asset it covers, a composed read would sample the last proxy too,
    and it would thicken on every cook."""
    stage, make = wired
    UsdGeom.Xform.Define(stage, "/Asset")
    _box(stage, "/Asset/Body")

    node = make()
    node.get_param("resolution").value = 48
    node.get_param("out_path").value = "/Asset/Proxy"
    node.get_param("prim_path").value = "/Asset"
    first = np.asarray(UsdGeom.Mesh(stage.GetPrimAtPath("/Asset/Proxy")).GetPointsAttr().Get())

    smooth = node.get_param("smooth").value
    node.get_param("smooth").value = smooth / 2
    node.get_param("smooth").value = smooth
    again = np.asarray(UsdGeom.Mesh(stage.GetPrimAtPath("/Asset/Proxy")).GetPointsAttr().Get())
    assert first.shape == again.shape and np.allclose(first, again)


def test_the_proxy_lands_in_its_parents_space(wired) -> None:
    stage, make = wired
    _box(stage, "/Asset")
    parent = UsdGeom.Xform.Define(stage, "/Moved")
    parent.AddTranslateOp().Set(Gf.Vec3d(10, 0, 0))

    node = make()
    node.get_param("resolution").value = 32
    node.get_param("out_path").value = "/Moved/Proxy"
    node.get_param("prim_path").value = "/Asset"

    local = np.asarray(UsdGeom.Mesh(stage.GetPrimAtPath("/Moved/Proxy")).GetPointsAttr().Get())
    assert abs(local[:, 0].mean() + 10.0) < 0.2  # world box at x=0, seen from x=10


def _purpose(stage, path):
    return UsdGeom.Imageable(stage.GetPrimAtPath(path)).ComputePurpose()


def test_the_proxy_is_set_up_as_the_assets_proxy(wired) -> None:
    """Viktor's ticket: purpose=proxy on the new mesh, and the asset pointing at it with
    proxyPrim. The asset turns render, so a viewport that draws proxies shows only one."""
    stage, make = wired
    _box(stage, "/Asset")
    node = make()
    node.get_param("resolution").value = 32
    node.get_param("prim_path").value = "/Asset"

    assert _purpose(stage, "/Asset_proxy") == UsdGeom.Tokens.proxy
    assert _purpose(stage, "/Asset") == UsdGeom.Tokens.render
    proxy, owner = UsdGeom.Imageable(stage.GetPrimAtPath("/Asset")).ComputeProxyPrim()
    assert proxy.GetPath() == Sdf.Path("/Asset_proxy") and owner.GetPath() == Sdf.Path("/Asset")


def test_a_child_with_its_own_default_purpose_turns_render_too(wired) -> None:
    """A child's authored purpose wins over its parent's, so `default` on a part would leave
    it on screen beside the proxy."""
    stage, make = wired
    UsdGeom.Xform.Define(stage, "/Asset")
    _box(stage, "/Asset/Body").CreatePurposeAttr().Set(UsdGeom.Tokens.default_)
    _box(stage, "/Asset/Guide", lo=(2, 2, 2), hi=(3, 3, 3)).CreatePurposeAttr().Set(UsdGeom.Tokens.guide)
    node = make()
    node.get_param("resolution").value = 32
    node.get_param("prim_path").value = "/Asset"

    assert _purpose(stage, "/Asset/Body") == UsdGeom.Tokens.render
    assert _purpose(stage, "/Asset/Guide") == UsdGeom.Tokens.guide, "other purposes are left alone"


def test_unticked_the_asset_is_left_as_it_is(wired) -> None:
    """The mesh is still a proxy: with no purpose it rendered over the asset (a car proxy
    covered the real car in every render)."""
    stage, make = wired
    _box(stage, "/Asset")
    node = make()
    node.get_param("resolution").value = 32
    node.get_param("as_proxy").value = False
    node.get_param("prim_path").value = "/Asset"

    assert stage.GetPrimAtPath("/Asset_proxy")
    assert _purpose(stage, "/Asset_proxy") == UsdGeom.Tokens.proxy
    assert _purpose(stage, "/Asset") == UsdGeom.Tokens.default_
    assert not stage.GetPrimAtPath("/Asset").GetRelationship(UsdGeom.Tokens.proxyPrim).HasAuthoredTargets()


def test_a_kept_source_is_set_up_as_a_proxy_of_itself(wired) -> None:
    """The kept proxy is a reference to the asset, so it takes what is authored on the asset
    too. Its children turned render would hide the proxy's own parts in a proxy view."""
    stage, make = wired
    UsdGeom.Xform.Define(stage, "/World")
    UsdGeom.Xform.Define(stage, "/World/Asset")
    _box(stage, "/World/Asset/Body").CreatePurposeAttr().Set(UsdGeom.Tokens.default_)
    node = make(budget=3000)
    node.get_param("prim_path").value = "/World/Asset"

    assert "kept as is" in node.get_param("readout").value
    assert _purpose(stage, "/World/Asset/Body") == UsdGeom.Tokens.render
    assert _purpose(stage, "/World/Asset_proxy") == UsdGeom.Tokens.proxy
    assert _purpose(stage, "/World/Asset_proxy/Body") == UsdGeom.Tokens.proxy
    assert stage.GetPrimAtPath("/World/Asset_proxy").GetRelationship(UsdGeom.Tokens.proxyPrim).GetTargets() == []
    proxy, _owner = UsdGeom.Imageable(stage.GetPrimAtPath("/World/Asset")).ComputeProxyPrim()
    assert proxy.GetPath() == Sdf.Path("/World/Asset_proxy")


def test_an_unchanged_recook_does_not_rebuild(wired, monkeypatch) -> None:
    stage, make = wired
    _box(stage, "/Asset")
    node = make()
    node.get_param("resolution").value = 32
    node.get_param("prim_path").value = "/Asset"

    # The registry's copy of the module, not the one imported above: a second import is an
    # independent module with its own _LAST_COOK.
    module = chunk_registry.ChunkRegistry().get_chunk_module("remesh_proxy").module
    calls = []
    real = module.build_proxy
    monkeypatch.setattr(module, "build_proxy", lambda *a: calls.append(1) or real(*a))
    assert node._cook()
    assert calls == []
    node.get_param("thickness").value = 2.0
    assert calls == [1]


def test_a_source_under_the_budget_is_kept_as_is_not_remeshed(wired) -> None:
    """A 12-triangle box remeshed is thousands of triangles of worse box. Below the budget
    the proxy is the source itself, and the chain downstream is told to leave it alone."""
    stage, make = wired
    UsdGeom.Xform.Define(stage, "/World")
    _box(stage, "/World/Asset/Body", colour=(0.2, 0.4, 0.6))

    node = make(budget=3000)
    node.get_param("prim_path").value = "/World/Asset"

    proxy = stage.GetPrimAtPath("/World/Asset_proxy")
    assert proxy.GetCustomDataByKey(surface_samples.PASSTHROUGH_KEY)
    body = UsdGeom.Mesh(stage.GetPrimAtPath("/World/Asset_proxy/Body"))
    assert len(body.GetFaceVertexCountsAttr().Get()) == 6  # the source's own quads
    assert surface_samples.is_passthrough(body.GetPrim())
    assert "kept as is" in node.get_param("readout").value
    assert node.get_param("out_triangles").value == 12


def test_unticking_keep_small_assets_remeshes_a_small_one_too(wired) -> None:
    """Viktor's ticket: a low-poly asset never got a proxy, because the budget that caps the
    proxy also decided which assets to leave alone. The two are separate settings now."""
    stage, make = wired
    UsdGeom.Xform.Define(stage, "/World")
    _box(stage, "/World/Asset/Body")

    node = make(budget=3000)
    node.get_param("resolution").value = 32
    node.get_param("keep_small").value = False
    node.get_param("prim_path").value = "/World/Asset"

    proxy = UsdGeom.Mesh(stage.GetPrimAtPath("/World/Asset_proxy"))
    assert proxy, "a mesh of its own, not a reference to the asset"
    assert not surface_samples.is_passthrough(proxy.GetPrim())
    assert "kept as is" not in node.get_param("readout").value
    assert 0 < node.get_param("out_triangles").value <= 3000


def test_downstream_chunks_leave_a_kept_source_alone(wired) -> None:
    stage, make = wired
    UsdGeom.Xform.Define(stage, "/World")
    _box(stage, "/World/Asset/Body")
    node = make(budget=3000)
    node.get_param("prim_path").value = "/World/Asset"

    uv = importlib.import_module("chunks.modelling.uv_unwrap")
    pr = importlib.import_module("chunks.modelling.poly_reduce")
    proxy = stage.GetPrimAtPath("/World/Asset_proxy")
    assert uv._mesh_prims(proxy, True) == []
    assert pr._mesh_prims(proxy, True) == []


def test_the_proxy_is_double_sided_so_folded_sheets_do_not_read_as_holes(wired) -> None:
    stage, make = wired
    _box(stage, "/Asset")
    node = make()
    node.get_param("resolution").value = 32
    node.get_param("prim_path").value = "/Asset"
    assert UsdGeom.Mesh(stage.GetPrimAtPath("/Asset_proxy")).GetDoubleSidedAttr().Get() is True


def test_corner_normals_smooth_a_gentle_bend_and_keep_a_sharp_fold() -> None:
    # Two triangles hinged on the edge 0-1, folded by `angle` degrees.
    def hinge(angle):
        t = np.radians(angle)
        points = np.array([[0, 0, 0], [1, 0, 0], [0.5, 1, 0], [0.5, -np.cos(t), np.sin(t)]], dtype=np.float32)
        return points, np.array([[0, 1, 2], [1, 0, 3]])

    points, tris = hinge(20.0)
    normals = rp._corner_normals(points, tris).reshape(2, 3, 3)
    # The shared corners of a gentle bend agree across the edge...
    assert np.allclose(normals[0, 0], normals[1, 1], atol=1e-6)
    assert np.allclose(normals[0, 1], normals[1, 0], atol=1e-6)
    # ...and the unshared ones stay their own face's.
    assert np.allclose(normals[0, 2], (0, 0, 1), atol=1e-6)

    points, tris = hinge(100.0)
    normals = rp._corner_normals(points, tris).reshape(2, 3, 3)
    face = np.cross(points[0] - points[1], points[3] - points[1])
    face /= np.linalg.norm(face)
    # Past the crease angle each face keeps its own normal: no averaged, backward seam.
    assert np.allclose(normals[0], (0, 0, 1), atol=1e-6)
    assert np.allclose(normals[1], face, atol=1e-6)


def test_the_proxy_carries_smooth_normals_not_flat_facets(wired) -> None:
    stage, make = wired
    _box(stage, "/Asset")
    node = make()
    node.get_param("resolution").value = 32
    node.get_param("prim_path").value = "/Asset"
    proxy = UsdGeom.Mesh(stage.GetPrimAtPath("/Asset_proxy"))
    normals = np.asarray(proxy.GetNormalsAttr().Get())
    assert proxy.GetNormalsInterpolation() == UsdGeom.Tokens.faceVarying
    assert len(normals) == len(proxy.GetFaceVertexIndicesAttr().Get())
    assert np.allclose(np.linalg.norm(normals, axis=1), 1.0, atol=1e-5)


def test_the_budget_caps_the_proxy_and_a_box_needs_far_less(wired) -> None:
    stage, make = wired
    _box(stage, "/Asset")
    node = make()
    node.get_param("resolution").value = 64
    node.get_param("max_error").value = 0.0  # exactly the budget
    node.get_param("budget").value = 11  # a 12-triangle box is above it: remesh it
    node.get_param("prim_path").value = "/Asset"
    capped = len(UsdGeom.Mesh(stage.GetPrimAtPath("/Asset_proxy")).GetFaceVertexCountsAttr().Get())
    assert capped <= 11

    node.get_param("budget").value = 0
    dense = len(UsdGeom.Mesh(stage.GetPrimAtPath("/Asset_proxy")).GetFaceVertexCountsAttr().Get())
    assert dense > 1000  # 0: marching cubes' output as it is


# ---------------------------------------------------------------------------
# a big volume, in a separate process
# ---------------------------------------------------------------------------


def _live_module():
    """The registry's copy of the module, the one whose execute runs."""
    return chunk_registry.ChunkRegistry().get_chunk_module("remesh_proxy").module


@pytest.fixture
def in_a_ui(wired, qapp, monkeypatch):
    """`wired`, as a UI host with an event loop would run it: big volumes go to a process.

    Every volume counts as big here, so a box takes the path a tree at 384 does.
    """
    monkeypatch.setattr(sim_bake, "in_background", lambda: True)
    stage, make = wired

    def make_live(budget=0):
        node = make(budget)
        monkeypatch.setattr(_live_module(), "_PROCESS_CELLS", 0)
        return node

    return stage, make_live


def _reader(cooks: list):
    """A chunk that notes what the proxy was each time it cooks."""

    def main():
        c = chunk.Chunk("Reader")
        c.add_param(chunk.Slider("gain", default_value=1.0))
        return c

    def execute(node):
        prim = node.get_stage().GetPrimAtPath("/Asset_proxy")
        cooks.append(len(UsdGeom.Mesh(prim).GetFaceVertexCountsAttr().Get() or []) if prim else None)

    return register_stub_module(main=main, execute=execute)


def _with_reader(stage, make):
    node = make()
    node.get_param("resolution").value = 16
    cooks: list = []
    (reader,) = _reader(cooks).load()
    chunk_registry.ChunkRegistry().scene.add_chunk(reader)
    reader.add_dependency(node)
    return node, cooks


def test_in_a_ui_the_cook_waits_for_the_process_and_the_chunks_below_cook_once(in_a_ui, qapp) -> None:
    """The volume is built in a separate process, so running out of memory does not take the
    host down. The chunks below read the proxy: they cook once, against the new one. Built
    in the background, they cooked against nothing first and again when it landed."""
    stage, make = in_a_ui
    _box(stage, "/Asset", colour=(0.2, 0.4, 0.6))
    node, cooks = _with_reader(stage, make)
    node.get_param("prim_path").value = "/Asset"
    cooks.clear()

    node.get_param("resolution").value = 32
    qapp.processEvents()

    proxy = UsdGeom.Mesh(stage.GetPrimAtPath("/Asset_proxy"))
    triangles = len(proxy.GetFaceVertexCountsAttr().Get())
    assert cooks == [triangles], f"the chunk below cooked {cooks}"
    assert node.get_param("out_mesh").value == "/Asset_proxy"
    assert node.get_param("out_triangles").value == triangles
    colours = np.asarray(proxy.GetDisplayColorPrimvar().Get())
    assert np.allclose(colours.mean(axis=0), (0.2, 0.4, 0.6), atol=1e-4)
    assert UsdGeom.Imageable(stage.GetPrimAtPath("/Asset")).ComputePurpose() == UsdGeom.Tokens.render
    assert not node.has_state(chunk.ChunkFlags.State.FAILED)


def test_cancel_keeps_the_last_proxy_and_the_chunks_below_wait(in_a_ui, qapp, monkeypatch) -> None:
    stage, make = in_a_ui
    _box(stage, "/Asset")
    node, cooks = _with_reader(stage, make)
    node.get_param("prim_path").value = "/Asset"
    before = np.asarray(UsdGeom.Mesh(stage.GetPrimAtPath("/Asset_proxy")).GetPointsAttr().Get())
    cooks.clear()

    def runs_until_cancelled(ctx, function, **kwargs):
        while True:
            ctx.check()
            time.sleep(0.01)

    monkeypatch.setattr(sim_bake, "in_subprocess", runs_until_cancelled)
    QtCore.QTimer.singleShot(300, qapp, lambda: sim_bake.cancel(node))
    node.get_param("thickness").value = 4.0  # cancelled: nothing raised

    after = np.asarray(UsdGeom.Mesh(stage.GetPrimAtPath("/Asset_proxy")).GetPointsAttr().Get())
    assert np.array_equal(before, after), "the last proxy left the stage"
    assert node.has_state(chunk.ChunkFlags.State.FAILED)
    assert node.get_param("readout").value == "cancelled"
    assert cooks == [], "a chunk below cooked after Cancel"


def _count_reads(monkeypatch) -> list:
    """One entry for every read of an asset the chunk makes from here on."""
    reads: list = []
    real = surface_samples.gather_sources
    monkeypatch.setattr(surface_samples, "gather_sources", lambda *a, **k: reads.append(1) or real(*a, **k))
    return reads


def test_a_remesh_in_a_process_reads_the_asset_once(in_a_ui, qapp, monkeypatch) -> None:
    """On a 1.4M-triangle asset each read is half a second on the main thread, and a
    settings change read it five times: the cook cache's key and the cook, then both again
    when the remesh landed, and once more as that cook ran a second time. Once is enough,
    and a settings change reuses it."""
    stage, make = in_a_ui
    _box(stage, "/Asset")
    node = make()
    node.get_param("resolution").value = 32
    reads = _count_reads(monkeypatch)
    node.get_param("prim_path").value = "/Asset"
    assert stage.GetPrimAtPath("/Asset_proxy")
    assert reads == [1]

    node.get_param("thickness").value = 4.0
    assert reads == [1]


def test_a_process_that_dies_fails_the_cook_and_keeps_the_host(in_a_ui, qapp, monkeypatch) -> None:
    """A process that dies (out of memory, say) must not take the host with it."""
    stage, make = in_a_ui
    _box(stage, "/Asset")

    def dies(ctx, function, **kwargs):
        raise RuntimeError("MemoryError")

    monkeypatch.setattr(sim_bake, "in_subprocess", dies)
    node = make()
    node.get_param("resolution").value = 32
    with pytest.raises(RuntimeError, match="MemoryError"):
        node.get_param("prim_path").value = "/Asset"

    assert node.get_param("readout").value == "remesh failed: MemoryError"
    assert node.has_state(chunk.ChunkFlags.State.FAILED)
    assert not stage.GetPrimAtPath("/Asset_proxy")


def test_cancel_stops_the_sampling_part_way(in_a_ui, qapp, monkeypatch) -> None:
    """Sampling a 7M-triangle tree takes eleven seconds: Cancel must not wait for the end."""
    stage, make = in_a_ui
    for i in range(12):
        _box(stage, f"/Asset/Box{i}", lo=(3 * i - 1, -1, -1), hi=(3 * i + 1, 1, 1))
    node = make()
    node.get_param("resolution").value = 16
    sampled: list = []
    real = surface_samples._sample_source

    def slow(*args, **kwargs):
        sampled.append(1)
        time.sleep(0.1)
        return real(*args, **kwargs)

    monkeypatch.setattr(surface_samples, "_sample_source", slow)
    QtCore.QTimer.singleShot(250, qapp, lambda: sim_bake.cancel(node))
    node.get_param("prim_path").value = "/Asset"

    assert node.get_param("readout").value == "cancelled"
    assert len(sampled) < 8, f"sampled {len(sampled)} of 12 meshes after Cancel"


def test_a_remesh_in_a_process_reports_its_steps_in_order(in_a_ui, qapp, monkeypatch) -> None:
    """The progress window says which step it is on: no true percentage can be had, as how
    long each one takes depends on the asset."""
    stage, make = in_a_ui
    _box(stage, "/Asset", colour=(0.2, 0.4, 0.6))
    node = make(budget=500)
    node.get_param("keep_small").value = False
    node.get_param("resolution").value = 32
    notes: list = []
    real = _live_module()._report

    def recording(chunk_node, proxy, note=""):
        if note.startswith("step") and (not notes or notes[-1] != note):
            notes.append(note)
        return real(chunk_node, proxy, note)

    monkeypatch.setattr(_live_module(), "_report", recording)
    node.get_param("prim_path").value = "/Asset"

    assert notes == [
        "step 1 of 5: sampling the surface",
        "step 2 of 5: building the volume",
        "step 3 of 5: meshing the surface",
        "step 4 of 5: reducing to 500 tris",
        "step 5 of 5: baking colours",
    ]
    assert stage.GetPrimAtPath("/Asset_proxy")


def test_the_first_cook_remeshes_once(in_a_ui, qapp, monkeypatch) -> None:
    """A first cook runs on a stage of the chunk's inputs alone (skip_cook), where the
    proxy's prim can't be defined, and then again on the whole stage. The second run took
    the whole remesh again: on a 7M-triangle tree, two waits of twenty seconds in a row."""
    stage, make = in_a_ui
    _box(stage, "/Asset")
    node = make()
    node.get_param("resolution").value = 32
    waits: list = []
    real = sim_bake.wait
    monkeypatch.setattr(sim_bake, "wait", lambda *a, **k: waits.append(1) or real(*a, **k))

    node.get_param("prim_path").value = "/Asset"

    assert stage.GetPrimAtPath("/Asset_proxy")
    assert waits == [1], f"remeshed {len(waits)} times"


# ---------------------------------------------------------------------------
# the volume's arithmetic, against scipy's own
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reach", [0.75, 1.75, 3.0, 4.5])
def test_the_ball_dilation_grows_exactly_what_a_distance_threshold_does(reach) -> None:
    rng = np.random.default_rng(3)
    occupied = rng.random((30, 24, 20)) > 0.995
    expected = rp.ndimage.distance_transform_edt(~occupied) <= reach
    assert np.array_equal(rp.ndimage.binary_dilation(occupied, structure=rp._ball(reach)), expected)


def test_squared_distances_match_scipy_slab_by_slab(monkeypatch) -> None:
    """Worked out a few slabs at a time, to hold a third of the memory; it must not differ
    from the whole-grid answer at the slab joins."""
    monkeypatch.setattr(rp, "_SLAB_VOXELS", 24 * 20 * 3)  # three slabs of x at a time
    rng = np.random.default_rng(4)
    mask = rng.random((31, 24, 20)) > 0.02
    got = rp.squared_distance_to_false(mask)
    assert got.dtype == np.int32
    assert np.array_equal(np.sqrt(got, dtype=np.float64), rp.ndimage.distance_transform_edt(mask))


def _field_as_it_was(samples, voxel, thickness, close, fill, smooth):
    """build_field before it was made lighter: scipy's distances over the whole grid."""
    pad = int(np.ceil(thickness + close + 3.0 * smooth)) + 2
    lo = samples.min(axis=0)
    origin = lo - pad * voxel
    shape = np.ceil((samples.max(axis=0) - lo) / voxel).astype(np.int64) + 1 + 2 * pad
    occupied = np.zeros(tuple(shape), dtype=bool)
    cells = np.rint((samples - origin) / voxel).astype(np.int64)
    occupied[cells[:, 0], cells[:, 1], cells[:, 2]] = True
    grown = rp.ndimage.distance_transform_edt(~occupied) <= (thickness + close)
    if fill:
        grown = rp.ndimage.binary_fill_holes(grown)
    inside = rp.ndimage.distance_transform_edt(grown)
    outside = rp.ndimage.distance_transform_edt(~grown)
    values = (inside - outside - close).astype(np.float32)
    if smooth > 0.0:
        values = rp.ndimage.gaussian_filter(values, sigma=smooth)
    return values


@pytest.mark.parametrize("thickness, close", [(0.75, 1.0), (2.0, 4.0)])
def test_the_volume_is_bit_for_bit_what_it_was(monkeypatch, thickness, close) -> None:
    """A proxy must not change shape from an update that only made it cheaper: the reduction
    after it is sensitive enough that float32 rounding alone took a rock from 138 to 110
    triangles."""
    monkeypatch.setattr(rp, "_SLAB_VOXELS", 5_000)
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/Asset")
    _box(stage, "/Asset/Body")
    _cards(stage, "/Asset/Leaves", _canopy_centres(150))
    samples = rp.sample(surface_samples.gather_sources(stage.GetPrimAtPath("/Asset")), _settings())
    voxel = 4.0 / 48
    field = rp.build_field(samples.points, voxel, thickness, close, True, 1.0)
    assert np.array_equal(field.values, _field_as_it_was(samples.points, voxel, thickness, close, True, 1.0))


# ---------------------------------------------------------------------------
# reading the asset only when it changed
# ---------------------------------------------------------------------------


def test_the_asset_is_read_again_only_when_it_changed(wired, monkeypatch) -> None:
    """Every cook upstream recooks this one, and almost none change what it reads; a
    settings change does not either. Telling so must not cost the read."""
    stage, make = wired
    mesh = _box(stage, "/Asset")
    node = make()
    node.get_param("resolution").value = 32
    node.get_param("prim_path").value = "/Asset"
    reads = _count_reads(monkeypatch)

    assert node._cook()
    node.get_param("thickness").value = 2.0
    assert reads == []

    mesh.GetPointsAttr().Set(Vt.Vec3fArray.FromNumpy(_box_points((-1, -1, -1), (2, 1, 1))))
    assert node._cook()
    assert reads == [1]
    points = np.asarray(UsdGeom.Mesh(stage.GetPrimAtPath("/Asset_proxy")).GetPointsAttr().Get())
    assert points[:, 0].max() > 2.0, "the proxy of the asset as it is now"


def _hsv_material(stage, path, amount):
    """A MaterialX surface whose base colour is a hue/saturation/value node's output."""
    material = UsdShade.Material.Define(stage, path)
    surface = UsdShade.Shader.Define(stage, f"{path}/Surface")
    surface.CreateIdAttr("ND_open_pbr_surface_surfaceshader")
    material.CreateSurfaceOutput("mtlx").ConnectToSource(surface.ConnectableAPI(), "surface")
    hsv = UsdShade.Shader.Define(stage, f"{path}/Hsv")
    hsv.CreateIdAttr("ND_hsvadjust_color3")
    hsv.CreateInput("in", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(0.2, 0.6, 0.1))
    hsv.CreateInput("amount", Sdf.ValueTypeNames.Vector3f).Set(Gf.Vec3f(*amount))
    hsv.CreateOutput("out", Sdf.ValueTypeNames.Color3f)
    surface.CreateInput("base_color", Sdf.ValueTypeNames.Color3f).ConnectToSource(hsv.ConnectableAPI(), "out")
    return material, hsv


def test_an_edit_inside_a_materialx_network_reaches_the_proxy(wired) -> None:
    """The check that a recook changed nothing compared what was read of the material --
    its constants and textures -- and not the network the colour is computed by: darkening
    a hue/saturation/value node left the proxy its old colour."""
    stage, make = wired
    mesh = _box(stage, "/Asset")
    material, hsv = _hsv_material(stage, "/Looks/Mat", (0.0, 1.0, 1.0))
    UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(material)
    node = make()
    node.get_param("resolution").value = 32
    node.get_param("prim_path").value = "/Asset"

    def colour():
        return np.asarray(UsdGeom.Mesh(stage.GetPrimAtPath("/Asset_proxy")).GetDisplayColorPrimvar().Get()).mean(axis=0)

    assert np.allclose(colour(), (0.2, 0.6, 0.1), atol=1e-3)
    hsv.GetInput("amount").Set(Gf.Vec3f(0.0, 1.0, 0.3))
    assert node._cook()
    assert np.allclose(colour(), np.array([0.2, 0.6, 0.1]) * 0.3, atol=1e-3)


def test_ticking_keep_small_assets_after_a_remesh_keeps_the_asset(wired) -> None:
    """The last remesh is reused only for the same settings, and Keep Small Assets is one."""
    stage, make = wired
    UsdGeom.Xform.Define(stage, "/World")
    _box(stage, "/World/Asset/Body")
    node = make(budget=3000)
    node.get_param("resolution").value = 32
    node.get_param("keep_small").value = False
    node.get_param("prim_path").value = "/World/Asset"
    assert "kept as is" not in node.get_param("readout").value

    node.get_param("keep_small").value = True
    assert "kept as is" in node.get_param("readout").value
