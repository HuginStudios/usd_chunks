"""Merge Instancers: instancers that share assets become one, and nothing that renders moves.

* two instancers reaching the same asset files -- even through two libraries -- become one
  with each asset once, and the sources are deactivated;
* every instance keeps its world transform, the sources' own transforms baked in;
* instances switched off are dropped;
* a prototype with an opinion of its own (a scatter's viewport proxy switched off, an
  override) is its own prototype, and the merged one carries the opinion over;
* a different purpose, primvars or animation keeps an instancer out, named in the readout;
  For Final Render merges default with render purpose;
* instancers that share no asset are left as they are.
"""

from __future__ import annotations

import numpy as np
import pytest
from pxr import Gf, Sdf, Usd, UsdGeom

from usd_chunks import Scene

T = Usd.TimeCode.Default()


@pytest.fixture
def assets(tmp_path):
    """Two asset files, a cube and a sphere, each with defaultPrim /Asset."""
    paths = {}
    for name, kind in (("bush", "Cube"), ("fern", "Sphere")):
        st = Usd.Stage.CreateNew(str(tmp_path / f"{name}.usda"))
        root = UsdGeom.Xform.Define(st, "/Asset")
        st.SetDefaultPrim(root.GetPrim())
        st.DefinePrim("/Asset/geo", kind)
        st.GetRootLayer().Save()
        paths[name] = str(tmp_path / f"{name}.usda")
    return paths


def _library(stage, root, assets):
    for name, path in assets.items():
        entry = stage.DefinePrim(f"{root}/{name}", "Xform")
        entry.GetReferences().AddReference(path)
        entry.SetActive(False)


def _instancer(stage, path, library, names, positions, indices, translate=None, rotate_z=None, **attrs):
    pi = UsdGeom.PointInstancer.Define(stage, path)
    targets = []
    for k, name in enumerate(names):
        proto = stage.DefinePrim(f"{path}/Prototypes/proto_{k}", "Xform")
        proto.GetReferences().AddInternalReference(f"{library}/{name}")
        proto.SetActive(True)
        targets.append(proto.GetPath())
    pi.CreatePrototypesRel().SetTargets(targets)
    pi.CreatePositionsAttr([Gf.Vec3f(*p) for p in positions])
    pi.CreateProtoIndicesAttr(indices)
    pi.CreateOrientationsAttr([Gf.Quath(1, 0, 0, 0)] * len(positions))
    pi.CreateScalesAttr([Gf.Vec3f(1.5, 1.5, 1.5)] * len(positions))
    xf = UsdGeom.Xformable(pi)
    if translate:
        xf.AddTranslateOp().Set(Gf.Vec3d(*translate))
    if rotate_z:
        xf.AddRotateZOp().Set(rotate_z)
    for name, value in attrs.items():
        pi.GetPrim().CreateAttribute(name, value[0]).Set(value[1])
    return pi


def _world(pi):
    """Every instance that is on, as world matrices, with the asset file its prototype reaches."""
    xf = UsdGeom.Xformable(pi.GetPrim()).ComputeLocalToWorldTransform(T)
    mats = pi.ComputeInstanceTransformsAtTime(T, T, UsdGeom.PointInstancer.IncludeProtoXform,
                                              UsdGeom.PointInstancer.ApplyMask)
    return [np.asarray(m * xf) for m in mats]


def _merge(stage, inspect=None, **params):
    """Cook a Merge Instancers; the readout, the instancers left on (paths), what is active,
    and *inspect(stage)* -- all read while the scene is open, as its layer leaves with it."""
    with Scene(stage) as scene:
        chunk = scene.create.merge_instancers(**params)
        s = scene.get_stage()
        live = [str(p.GetPath()) for p in s.Traverse() if p.IsA(UsdGeom.PointInstancer)]
        active = {str(p.GetPath()): p.IsActive() for p in s.Traverse(Usd.PrimAllPrimsPredicate)}
        return chunk.get_param("readout").value, live, active, inspect(s) if inspect else None


MERGED = "/World/merged_instancers/bushes_merged"


def _two_layers(assets):
    """The road forest's and the bathroom's situation: two libraries, same asset files."""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/World")
    _library(stage, "/LibA", assets)
    _library(stage, "/LibB", assets)
    _instancer(stage, "/World/bushes", "/LibA", ["bush", "fern"], [(0, 0, 0), (2, 0, 0), (4, 0, 0)], [0, 1, 0])
    _instancer(stage, "/World/hedge", "/LibB", ["fern", "bush"], [(0, 5, 0), (2, 5, 0)], [0, 1],
               translate=(10, 0, 0), rotate_z=90.0)
    return stage


def test_instancers_sharing_assets_become_one_with_each_asset_once(assets):
    stage = _two_layers(assets)
    before = [m for p in ("/World/bushes", "/World/hedge") for m in _world(UsdGeom.PointInstancer(stage.GetPrimAtPath(p)))]
    def inspect(s):
        merged = UsdGeom.PointInstancer(s.GetPrimAtPath(MERGED))
        return len(merged.GetPrototypesRel().GetTargets()), list(merged.GetProtoIndicesAttr().Get()), _world(merged)

    readout, live, active, (n_protos, indices, after) = _merge(stage, inspect)
    assert not active["/World/bushes"] and not active["/World/hedge"]
    assert live == [MERGED]
    assert n_protos == 2
    assert indices == [0, 1, 0, 1, 0]   # hedge's fern -> 1, its bush -> 0
    assert len(after) == len(before)
    for a, b in zip(after, before):
        assert np.allclose(a, b, atol=2e-3), (a, b)
    assert readout.startswith("2 instancers → 1") and "prototypes 4 → 2" in readout


def test_each_merged_prototype_is_the_asset(assets):
    def kinds(s):
        targets = UsdGeom.PointInstancer(s.GetPrimAtPath(MERGED)).GetPrototypesRel().GetTargets()
        return [s.GetPrimAtPath(t).GetChild("geo").GetTypeName() for t in targets]

    assert _merge(_two_layers(assets), kinds)[3] == ["Cube", "Sphere"]


def test_instances_switched_off_are_dropped(assets):
    stage = _two_layers(assets)
    UsdGeom.PointInstancer(stage.GetPrimAtPath("/World/bushes")).DeactivateIds([1])
    count = _merge(stage, lambda s: len(UsdGeom.PointInstancer(s.GetPrimAtPath(MERGED)).GetPositionsAttr().Get()))[3]
    assert count == 4


@pytest.mark.parametrize("change,reason", [
    ("purpose", None),
    ("primvar", "has primvars"),
    ("animated", "positions is animated"),
])
def test_what_would_change_the_render_stays_out(assets, change, reason):
    stage = _two_layers(assets)
    hedge = UsdGeom.PointInstancer(stage.GetPrimAtPath("/World/hedge"))
    if change == "purpose":
        hedge.CreatePurposeAttr(UsdGeom.Tokens.proxy)
    elif change == "primvar":
        UsdGeom.PrimvarsAPI(hedge).CreatePrimvar("displayColor", Sdf.ValueTypeNames.Color3fArray).Set([(1, 0, 0)] * 2)
    else:
        hedge.GetPositionsAttr().Set([Gf.Vec3f(0, 5, 0), Gf.Vec3f(2, 5, 0)], 1.0)
    readout, live, active, _ = _merge(stage)
    # Nothing left to merge with: both stay as they are.
    assert active["/World/bushes"] and active["/World/hedge"]
    assert not any("merged" in p for p in live)
    if reason:
        assert f"hedge ({reason})" in readout


def test_instancers_sharing_no_asset_are_left_alone(assets):
    stage = Usd.Stage.CreateInMemory()
    _library(stage, "/Lib", assets)
    _instancer(stage, "/World/bushes", "/Lib", ["bush"], [(0, 0, 0)], [0])
    _instancer(stage, "/World/ferns", "/Lib", ["fern"], [(0, 1, 0)], [0])
    readout, _, active, _ = _merge(stage)
    assert active["/World/bushes"] and active["/World/ferns"]
    assert readout.startswith("nothing to merge")


def test_a_prototype_with_opinions_of_its_own_is_its_own_and_keeps_them(assets):
    # The hedge's fern is red and has its geo switched off -- a scatter's proxy, say: it is not
    # the bushes' fern. The hedge's bush still is the bushes' bush.
    stage = _two_layers(assets)
    geo = stage.OverridePrim("/World/hedge/Prototypes/proto_0/geo")
    geo.CreateAttribute("primvars:displayColor", Sdf.ValueTypeNames.Color3fArray).Set([(1, 0, 0)])
    geo.SetActive(False)

    def protos(s):
        targets = UsdGeom.PointInstancer(s.GetPrimAtPath(MERGED)).GetPrototypesRel().GetTargets()
        out = []
        for t in targets:
            g = s.GetPrimAtPath(t.AppendChild("geo"))
            colour = g.GetAttribute("primvars:displayColor")
            out.append((g.GetTypeName(), g.IsActive(), list(colour.Get()) if colour and colour.HasAuthoredValue() else None))
        return out

    readout, _, _, kinds = _merge(stage, protos)
    assert kinds == [("Cube", True, None), ("Sphere", True, None), ("Sphere", False, [Gf.Vec3f(1, 0, 0)])]
    assert "prototypes 4 → 3" in readout


def test_for_final_render_merges_default_with_render_purpose(assets):
    stage = _two_layers(assets)
    UsdGeom.PointInstancer(stage.GetPrimAtPath("/World/hedge")).CreatePurposeAttr(UsdGeom.Tokens.render)
    _, live, _, _ = _merge(stage)
    assert "/World/hedge" in live and "/World/bushes" in live          # left apart by default
    _, live, _, purpose = _merge(_two_layers_render(assets), lambda s: UsdGeom.Imageable(
        s.GetPrimAtPath(MERGED)).ComputePurpose(), final_render=True)
    assert live == [MERGED] and purpose == UsdGeom.Tokens.render


def _two_layers_render(assets):
    stage = _two_layers(assets)
    UsdGeom.PointInstancer(stage.GetPrimAtPath("/World/hedge")).CreatePurposeAttr(UsdGeom.Tokens.render)
    return stage
