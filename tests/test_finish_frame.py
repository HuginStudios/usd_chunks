"""Finish Frame: sky, haze, sun and grade on a rendered image, from the stage's camera and lights."""
from __future__ import annotations

import importlib

import numpy as np
import pytest
from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux

from usd_chunks import Scene

ff = importlib.import_module("chunks.usd.finish_frame")
Image = pytest.importorskip("PIL.Image")


def test_srgb_round_trips() -> None:
    x = np.linspace(0.0, 1.0, 11)
    assert np.allclose(ff.linear_to_srgb(ff.srgb_to_linear(x)), x)


def test_the_sky_image_centre_is_the_domes_plus_z() -> None:
    """Where hdVRay puts it: a pole's shadow lined up only with the centre at the dome's +Z."""
    hdri = np.zeros((8, 16, 3))
    hdri[:, 8] = 1.0  # the centre column
    along_z = ff.sky_radiance(np.array([[0.0, 0.0, 1.0]]), hdri, np.eye(4))
    along_x = ff.sky_radiance(np.array([[1.0, 0.0, 0.0]]), hdri, np.eye(4))
    assert along_z[0, 0] == 1.0 and along_x[0, 0] == 0.0
    turned = ff.sky_radiance(np.array([[0.0, 0.0, 1.0]]), hdri, np.eye(4), turn_deg=90.0)
    assert turned[0, 0] == 0.0


def test_haze_grows_with_distance_and_spares_what_is_near() -> None:
    image = np.full((1, 3, 3), 0.1)
    depth = np.array([[1.0, 400.0, np.inf]])
    dirs = np.array([[[0.0, 1.0, 0.0], [0.0, 1.0, 0.0], [0.0, 1.0, 0.0]]])
    out = ff.add_haze(image, depth, dirs, up=np.array([0.0, 0.0, 1.0]), to_sun=None, distance=400.0,
                      color=(1.0, 0.5, 0.25), level=0.6, sun_glow=0.0)
    near, far = out[0, 0], out[0, 1]
    assert np.allclose(near, image[0, 0], atol=0.002)
    assert far[0] > near[0] and far[0] == pytest.approx(0.1 + (0.6 - 0.1) * (1 - np.exp(-1)), abs=1e-6)


def test_grade_at_rest_only_rolls_highlights_off() -> None:
    lin = ff.srgb_to_linear(np.array([[[0.2, 0.4, 0.6]]]))
    out = ff.grade(lin)
    assert np.all(out <= np.array([0.2, 0.4, 0.6]) + 1e-9)  # the soft shoulder only ever darkens
    assert np.allclose(out, [0.2, 0.4, 0.6], atol=0.06)


def _shot(tmp_path):
    """A camera looking down +Y at a near cube and a far one, a sun, and a red-sky dome."""
    sky = tmp_path / "sky.png"
    Image.fromarray(np.full((16, 32, 3), (200, 40, 40), dtype=np.uint8)).save(sky)
    stage = Usd.Stage.CreateNew(str(tmp_path / "shot.usda"))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    for name, y, size in (("near", 6.0, 2.0), ("far", 300.0, 60.0)):
        cube = UsdGeom.Cube.Define(stage, f"/World/{name}")
        cube.GetSizeAttr().Set(size)
        cube.AddTranslateOp().Set(Gf.Vec3d(0, y, 0))
    camera = UsdGeom.Camera.Define(stage, "/World/cam")
    camera.AddRotateXOp().Set(90.0)  # look down +Y, Z up
    camera.GetFocalLengthAttr().Set(10.0)
    dome = UsdLux.DomeLight.Define(stage, "/World/sky")
    dome.GetTextureFileAttr().Set(Sdf.AssetPath(str(sky)))
    sun = UsdLux.DistantLight.Define(stage, "/World/sun")
    sun.AddRotateXOp().Set(80.0)  # shining down and forward, so the sun is up ahead
    stage.GetRootLayer().Save()
    # The "render": grey where something was hit, transparent sky everywhere else.
    w, h = 64, 48
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    rgba[h // 2 - 4:h // 2 + 4, w // 2 - 4:w // 2 + 4] = (90, 90, 90, 255)  # near cube, middle
    render = tmp_path / "render.png"
    Image.fromarray(rgba).save(render)
    return stage, render


def test_a_frame_comes_out_finished(tmp_path) -> None:
    pytest.importorskip("trimesh")
    pytest.importorskip("embreex")
    stage_, render = _shot(tmp_path)
    out = tmp_path / "final.png"
    with Scene.open(str(tmp_path / "shot.usda")) as scene:
        node = scene.create.finish_frame(source_image=str(render), output_image=str(out), camera="/World/cam",
                                         depth_from="/World", rays=0.0, bloom=0.0, sun_disc=0.0)
        module = node.get_chunk_module().module
        module.run(node)
        readout = node.get_param("readout").value
    assert out.exists(), readout
    assert "sky" in readout and "haze" in readout, readout
    pixels = np.asarray(Image.open(out).convert("RGB"), dtype=np.float64)
    corner = pixels[2, 2]  # sky: the dome's red, under a little haze
    centre = pixels[24, 32]  # the near cube: still grey
    assert corner[0] > corner[2] + 40, corner
    assert abs(centre[0] - centre[2]) < 25, centre


def test_nothing_rendered_yet_says_so(tmp_path) -> None:
    _shot(tmp_path)
    with Scene.open(str(tmp_path / "shot.usda")) as scene:
        node = scene.create.finish_frame(source_image=str(tmp_path / "missing.png"))
        node.get_chunk_module().module.run(node)
        assert node.get_param("readout").value == "no rendered image to finish yet"


def test_simple_shapes_and_nested_instancers_are_in_the_depth_pass() -> None:
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Sphere.Define(stage, "/World/ball").GetRadiusAttr().Set(1.0)
    outer = UsdGeom.PointInstancer.Define(stage, "/World/outer")
    inner = UsdGeom.PointInstancer.Define(stage, "/World/outer/proto/inner")
    UsdGeom.Cube.Define(stage, "/World/outer/proto/inner/proto/box")
    inner.GetPrototypesRel().SetTargets(["/World/outer/proto/inner/proto"])
    inner.GetProtoIndicesAttr().Set([0, 0, 0])
    inner.GetPositionsAttr().Set([(0, 0, 0), (3, 0, 0), (6, 0, 0)])
    outer.GetPrototypesRel().SetTargets(["/World/outer/proto"])
    outer.GetProtoIndicesAttr().Set([0, 0])
    outer.GetPositionsAttr().Set([(0, 10, 0), (0, 20, 0)])
    tris, capped = ff.depth_triangles(stage, ["/World"])
    sphere = len(ff._local_triangles(stage.GetPrimAtPath("/World/ball")))
    # The sphere, plus 2 outer x 3 inner copies of a 12-triangle box. The prototypes themselves
    # are under the instancers, so they only count as copies.
    assert len(tris) == sphere + 2 * 3 * 12 and not capped


def _haze_at(tmp_path, meters_per_unit, distance_m=300.0):
    """Finish a grey render of a far cube on a stage in the given units; the centre pixel."""
    stage = Usd.Stage.CreateNew(str(tmp_path / f"u{meters_per_unit}.usda"))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, meters_per_unit)
    scale = 1.0 / meters_per_unit
    # A mesh wall facing the camera (a Mesh, so the depth pass sees it either way).
    wall = UsdGeom.Mesh.Define(stage, "/World/far")
    half, y = 60.0 * scale, distance_m * scale
    wall.GetPointsAttr().Set([(-half, y, -half), (half, y, -half), (half, y, half), (-half, y, half)])
    wall.GetFaceVertexCountsAttr().Set([4])
    wall.GetFaceVertexIndicesAttr().Set([0, 1, 2, 3])
    camera = UsdGeom.Camera.Define(stage, "/World/cam")
    camera.AddRotateXOp().Set(90.0)
    camera.GetFocalLengthAttr().Set(10.0)
    stage.GetRootLayer().Save()
    render = tmp_path / f"r{meters_per_unit}.png"
    Image.fromarray(np.full((24, 32, 4), (90, 90, 90, 255), dtype=np.uint8)).save(render)
    out = tmp_path / f"o{meters_per_unit}.png"
    with Scene.open(str(tmp_path / f"u{meters_per_unit}.usda")) as scene:
        node = scene.create.finish_frame(source_image=str(render), output_image=str(out), camera="/World/cam",
                                         sky_enabled=False, ground_below=False, rays=0.0, bloom=0.0, sun_disc=0.0)
        node.get_chunk_module().module.run(node)
    return np.asarray(Image.open(out).convert("RGB"), dtype=np.float64)[12, 16]


def test_haze_distance_is_in_metres_whatever_the_stage_units(tmp_path) -> None:
    pytest.importorskip("embreex")
    metres = _haze_at(tmp_path, 1.0)
    centimetres = _haze_at(tmp_path, 0.01)
    assert np.allclose(metres, centimetres, atol=2.0), (metres, centimetres)


def test_an_exr_goes_in_and_out_linear(tmp_path) -> None:
    pytest.importorskip("OpenEXR")
    _shot(tmp_path)
    source = tmp_path / "render.exr"
    bright = np.zeros((48, 64, 3))
    bright[..., 0] = 4.0  # brighter than an 8-bit image can hold
    ff.write_exr(str(source), bright, np.ones((48, 64)))
    out = tmp_path / "final.exr"
    with Scene.open(str(tmp_path / "shot.usda")) as scene:
        node = scene.create.finish_frame(source_image=str(source), output_image=str(out), camera="/World/cam",
                                         sky_enabled=False, haze_enabled=False, rays=0.0, bloom=0.0, sun_disc=0.0,
                                         exposure=0.5)
        node.get_chunk_module().module.run(node)
    rgb, alpha = ff.read_image(str(out))
    assert rgb[24, 32, 0] == pytest.approx(2.0, abs=1e-3), "linear and unclipped, exposure applied"
