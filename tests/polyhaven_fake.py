"""Poly Haven without the network, for tests: downloads make small stand-ins in a temporary cache.

    from tests.polyhaven_fake import fake_polyhaven  # noqa: F401 -- an autouse fixture

A model is a default prim with two parts, each a small box bound to a material whose texture
is <name>_diff_<res>.jpg; a texture set is three small files; an HDRI is one. Shaped like the
real ones, so the chunks author from them the same way, and nothing leaves the machine.
"""
from __future__ import annotations

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade

from usd_chunks import polyhaven

_BOX = [(-0.05, 0, -0.05), (0.05, 0, -0.05), (0.05, 0, 0.05), (-0.05, 0, 0.05),
        (-0.05, 0.1, -0.05), (0.05, 0.1, -0.05), (0.05, 0.1, 0.05), (-0.05, 0.1, 0.05)]
_FACES = [0, 1, 2, 3, 4, 7, 6, 5, 0, 4, 5, 1, 1, 5, 6, 2, 2, 6, 7, 3, 3, 7, 4, 0]


def make_model(name: str, res: str = "1k"):
    dest = polyhaven.model_file(name, res)
    if dest.exists():
        return dest
    folder = dest.parent
    (folder / "textures").mkdir(parents=True, exist_ok=True)
    diffuse = folder / "textures" / f"{name}_diff_{res}.jpg"
    diffuse.write_bytes(b"jpg")
    stage = Usd.Stage.CreateNew(str(dest))
    root = UsdGeom.Xform.Define(stage, f"/{name}")
    stage.SetDefaultPrim(root.GetPrim())
    material = UsdShade.Material.Define(stage, f"/{name}/_materials/{name}")
    tex = UsdShade.Shader.Define(stage, f"/{name}/_materials/{name}/diff")
    tex.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(str(diffuse))
    for i, part in enumerate(("a", "b")):
        xf = UsdGeom.Xform.Define(stage, f"/{name}/{name}_{part}")
        xf.AddTranslateOp().Set(Gf.Vec3d(0.5 * (i + 1), 0, 0))
        mesh = UsdGeom.Mesh.Define(stage, f"/{name}/{name}_{part}/geo")
        mesh.CreatePointsAttr(_BOX)
        mesh.CreateFaceVertexCountsAttr([4] * 6)
        mesh.CreateFaceVertexIndicesAttr(_FACES)
        UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(material)
    stage.Save()
    return dest


def make_texture(name: str, res: str = "1k"):
    files = polyhaven.texture_files(name, res)
    for path in files.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"jpg")
    return files


def make_hdri(name: str, res: str = "2k"):
    dest = polyhaven.cache_dir() / "hdri" / f"{name}_{res}.exr"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"exr")
    return dest


@pytest.fixture(autouse=True)
def fake_polyhaven(tmp_path, monkeypatch):
    """Every Poly Haven download in the test makes a stand-in instead, in its own cache."""
    monkeypatch.setenv(polyhaven.ENV, str(tmp_path / "polyhaven"))
    monkeypatch.setattr(polyhaven, "download_model", make_model)
    monkeypatch.setattr(polyhaven, "download_texture", make_texture)
    monkeypatch.setattr(polyhaven, "download_hdri", make_hdri)
    return tmp_path / "polyhaven"
