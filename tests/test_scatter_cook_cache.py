"""Scatter Points in the cook cache: a reopen reads the scatter back, and a change to anything
it reads besides its params -- the surface, its density field, what Keep Away From and Grow
Near read, a prototype's asset file -- cooks it again.
"""

from __future__ import annotations

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom, Vt

from usd_chunks import Scene, cook_cache


@pytest.fixture(autouse=True)
def _cache_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv(cook_cache.ENV_DIR, str(tmp_path / "cook_cache"))
    monkeypatch.delenv(cook_cache.ENV, raising=False)
    monkeypatch.setattr(cook_cache, "_MIN_COOK_S", 0.0)


@pytest.fixture
def restores(monkeypatch):
    """Each restore the cache attempts, as True (read back) or False (cooked)."""
    seen = []
    real = cook_cache.CookEntry.restore

    def spy(self, chunk, layer):
        result = real(self, chunk, layer)
        if chunk.name == "Forest":
            seen.append(result)
        return result

    monkeypatch.setattr(cook_cache.CookEntry, "restore", spy)
    return seen


def _network(tmp_path):
    """A ground with a density field, an asset in a library, a blocker and a source to grow
    near, and a Scatter Points reading all of them. Returns the saved network's path."""
    asset = Usd.Stage.CreateNew(str(tmp_path / "bush.usda"))
    asset.SetDefaultPrim(UsdGeom.Xform.Define(asset, "/Asset").GetPrim())
    UsdGeom.Cube.Define(asset, "/Asset/geo").CreateSizeAttr(0.2)
    asset.GetRootLayer().Save()

    # The scene in a file of its own, sublayered under the network: what a save writes is the
    # network, so a scene made in memory would not be there to read on the reopen.
    base_path = tmp_path / "base.usda"
    stage = Usd.Stage.CreateNew(str(base_path))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    n = 6
    pts = [Gf.Vec3f(x * 2.0 - 5, y * 2.0 - 5, 0) for y in range(n) for x in range(n)]
    quads = [i for y in range(n - 1) for x in range(n - 1)
             for i in (y * n + x, y * n + x + 1, (y + 1) * n + x + 1, (y + 1) * n + x)]
    ground = UsdGeom.Mesh.Define(stage, "/World/ground")
    ground.CreatePointsAttr(pts)
    ground.CreateFaceVertexCountsAttr([4] * ((n - 1) ** 2))
    ground.CreateFaceVertexIndicesAttr(quads)
    UsdGeom.PrimvarsAPI(ground).CreatePrimvar("d", Sdf.ValueTypeNames.FloatArray, UsdGeom.Tokens.vertex).Set(
        Vt.FloatArray([1.0] * len(pts)))
    blocker = UsdGeom.Points.Define(stage, "/World/blocker")
    blocker.CreatePointsAttr([Gf.Vec3f(0, 0, 0)])
    source = UsdGeom.PointInstancer.Define(stage, "/World/source")
    source.CreatePositionsAttr([Gf.Vec3f(1, 1, 0)])
    source.CreateProtoIndicesAttr([0])
    entry = stage.DefinePrim("/Library/bush", "Xform")
    entry.GetReferences().AddReference(str(tmp_path / "bush.usda"))
    entry.SetActive(False)
    stage.GetRootLayer().Save()

    root = Sdf.Layer.CreateAnonymous("net.usda")
    root.subLayerPaths.append(str(base_path))
    path = tmp_path / "net.usda"
    with Scene(Usd.Stage.Open(root)) as scene:
        scene.create.scatter_points(
            name="Forest", surface_prim="/World/ground", inst_path="/World/forest", max_count=40,
            auto_spacing=False, spacing=0.0, density_primvar="d", avoid_prims="/World/blocker",
            avoid_distance=0.5, near_prims="/World/source", near_radius=4.0,
            **{"prototypes.0.proto_path": "/Library/*"})
        scene.save(str(path))
    return path


def _positions(path):
    with Scene.open(str(path)) as scene:
        attr = UsdGeom.PointInstancer(scene.get_stage().GetPrimAtPath("/World/forest")).GetPositionsAttr()
        return [tuple(p) for p in attr.Get()]


def _edit(path, fn):
    """Change the scene the network sublayers, beside it."""
    layer = Sdf.Layer.FindOrOpen(str(path.parent / "base.usda"))
    layer.Reload()
    fn(Usd.Stage.Open(layer))
    layer.Save()


def test_a_reopen_reads_the_scatter_back(tmp_path, restores):
    path = _network(tmp_path)
    first = _positions(path)
    assert restores and restores[-1] is True
    assert _positions(path) == first


@pytest.mark.parametrize("change", ["surface", "density", "blocker", "source"])
def test_a_change_to_what_it_reads_cooks_again(tmp_path, restores, change):
    path = _network(tmp_path)
    _positions(path)                      # read back: the cache holds this network's scatter
    assert restores[-1] is True

    def edit(stage):
        if change == "surface":
            attr = UsdGeom.Mesh(stage.GetPrimAtPath("/World/ground")).GetPointsAttr()
            attr.Set([Gf.Vec3f(p[0], p[1], 0.5) for p in attr.Get()])
        elif change == "density":
            pv = UsdGeom.PrimvarsAPI(stage.GetPrimAtPath("/World/ground")).GetPrimvar("d")
            pv.Set(Vt.FloatArray([0.5] * len(pv.Get())))
        elif change == "blocker":
            UsdGeom.Points(stage.GetPrimAtPath("/World/blocker")).GetPointsAttr().Set([Gf.Vec3f(2, 2, 0)])
        else:
            UsdGeom.PointInstancer(stage.GetPrimAtPath("/World/source")).GetPositionsAttr().Set(
                [Gf.Vec3f(-3, -3, 0)])

    _edit(path, edit)
    _positions(path)
    assert restores[-1] is False, f"a changed {change} must cook again"


def test_a_re_exported_asset_cooks_again(tmp_path, restores):
    path = _network(tmp_path)
    _positions(path)
    assert restores[-1] is True
    asset = Sdf.Layer.FindOrOpen(str(tmp_path / "bush.usda"))
    asset_stage = Usd.Stage.Open(asset)
    asset_stage.GetPrimAtPath("/Asset/geo").GetAttribute("size").Set(0.4)
    asset.Save()
    _positions(path)
    assert restores[-1] is False
