"""Remesh Proxy: what a stand-in mesh is judged on.

One closed, outward-facing surface where the asset was; gaps bridged without the outline
growing; hidden internals gone; the asset's colour on it; and every copy of an instanced
part counted. Plus the usual: it never reads its own output back.
"""

from __future__ import annotations

import importlib

import numpy as np
import pytest
from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade, Vt

from usd_chunks import chunk_registry, cook_cache
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import initialize as _chunk_usd_init

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
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    edges = np.concatenate([triangles[:, [0, 1]], triangles[:, [1, 2]]])
    n = len(points)
    graph = coo_matrix((np.ones(len(edges)), (edges[:, 0], edges[:, 1])), shape=(n, n))
    return connected_components(graph)[0]


def _signed_volume(points, triangles) -> float:
    a, b, c = points[triangles[:, 0]], points[triangles[:, 1]], points[triangles[:, 2]]
    return float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)


def _settings(**overrides):
    values = dict(resolution=64, thickness=1.0, close=0.0, fill=True, smooth=0.5, bake=True)
    values.update(overrides)
    return rp._Settings(**values)


def _proxy(stage, path="/Asset", **overrides):
    return rp.build_proxy(rp.gather_sources(stage.GetPrimAtPath(path)), _settings(**overrides))


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

    def make():
        module = reg.get_chunk_module("remesh_proxy")
        chunks = module.create() if module.loaded else module.load()
        for c in chunks:
            scene.add_chunk(c)
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

    sources = rp.gather_sources(stage.GetPrimAtPath("/Asset"))
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

    assert [s.path for s in rp.gather_sources(stage.GetPrimAtPath("/Asset"))] == ["/Asset/Body"]


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
    from PIL import Image

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

    node.get_param("smooth").value = 0.25
    node.get_param("smooth").value = 0.5
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


def test_purpose_is_authored_on_the_proxy(wired) -> None:
    stage, make = wired
    _box(stage, "/Asset")
    node = make()
    node.get_param("resolution").value = 32
    node.get_param("purpose").value = rp.PURPOSE_PROXY
    node.get_param("prim_path").value = "/Asset"
    assert UsdGeom.Imageable(stage.GetPrimAtPath("/Asset_proxy")).GetPurposeAttr().Get() == UsdGeom.Tokens.proxy


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
