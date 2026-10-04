"""Semantic Labels' render primvars and the KITTI Writer's pure parts: labels becoming the
primvars the render vars read, and the writer turning anti-aliased passes and a camera
into labels a trainer can trust (mask decoding, camera model, KITTI boxes, rejected
frames).

Rendering is not exercised here -- it needs a render delegate.
"""
from __future__ import annotations

import importlib
import math

import numpy as np
import pytest
from pxr import Gf, Sdf, Usd, UsdGeom

from usd_chunks import Scene


@pytest.fixture
def scene():
    with Scene(host_name="pytest") as scene:
        yield scene


# --------------------------------------------------------------------------------------
# Semantic Labels: render primvars


def _labels(scene, previous, name, paths, label, **extra):
    args = dict(primpath_location=paths, **extra)
    node = scene.create.semantics(previous, name=name, **args) if previous else scene.create.semantics(name=name, **args)
    rows = node.get_param("labels")
    rows.get_indexed_param(rows.get_param("taxonomy"), 0).value = "class"
    rows.get_indexed_param(rows.get_param("label_values"), 0).value = label
    return node


def _primvar(stage, path, name):
    pv = UsdGeom.PrimvarsAPI(stage.GetPrimAtPath(path)).GetPrimvar(name)
    return pv.Get() if pv else None


@pytest.mark.parametrize("all_on", [False, True])
def test_render_primvars_number_every_label_across_chunks(scene, all_on):
    # One Semantic Labels chunk per class, as a network labels. Ids must still be unique
    # across the scene: the last chunk numbers every label it sees, and its layer wins.
    stage = scene.get_stage()
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        UsdGeom.Sphere.Define(stage, "/W/sphere_a")
        UsdGeom.Sphere.Define(stage, "/W/sphere_b")
        UsdGeom.Cube.Define(stage, "/W/cube_a")
        UsdGeom.Xform.Define(stage, "/W/torus_a")
        UsdGeom.Mesh.Define(stage, "/W/torus_a/geo")

    render = dict(render_primvars=True, classes="sphere,cube,torus")
    node = None
    for shape in ("sphere", "cube", "torus"):
        node = _labels(scene, node, f"L_{shape}", f"/W/{shape}_*", shape,
                       **(render if all_on or shape == "torus" else {}))
    scene.execute()

    got = {p: (_primvar(stage, p, "semantic_id"), _primvar(stage, p, "instance_id"))
           for p in ("/W/sphere_a", "/W/sphere_b", "/W/cube_a", "/W/torus_a/geo")}
    assert [cls for cls, _ in got.values()] == [1, 1, 2, 3]
    ids = [inst for _, inst in got.values()]
    assert None not in ids and len(set(ids)) == len(ids)
    # Labelled on the Xform, the torus's mesh carries the primvars: the label covers it.


def test_a_descendant_with_its_own_label_is_its_own_instance(scene):
    stage = scene.get_stage()
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        UsdGeom.Xform.Define(stage, "/W/chair")
        UsdGeom.Cube.Define(stage, "/W/chair/seat")
        UsdGeom.Cube.Define(stage, "/W/chair/leg")
        UsdGeom.Cube.Define(stage, "/W/chair/cushion")
    node = _labels(scene, None, "Chair", "/W/chair", "cube")
    _labels(scene, node, "Cushion", "/W/chair/cushion", "sphere", render_primvars=True, classes="sphere,cube")
    scene.execute()

    # The chair is one instance, however many gprims it is built from...
    assert _primvar(stage, "/W/chair/seat", "instance_id") == _primvar(stage, "/W/chair/leg", "instance_id")
    assert _primvar(stage, "/W/chair/seat", "semantic_id") == 2
    # ...except where a descendant carries a label of its own.
    assert _primvar(stage, "/W/chair/cushion", "semantic_id") == 1
    assert _primvar(stage, "/W/chair/cushion", "instance_id") != _primvar(stage, "/W/chair/seat", "instance_id")


def test_render_primvars_are_off_by_default(scene):
    stage = scene.get_stage()
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        UsdGeom.Sphere.Define(stage, "/W/ball")
    _labels(scene, None, "L", "/W/ball", "sphere")
    scene.execute()
    assert _primvar(stage, "/W/ball", "instance_id") is None


def test_instance_colours_are_distinct_and_never_dark():
    mod = importlib.import_module("chunks.usd.semantics")
    colours = np.array([mod.instance_colour(i) for i in range(1, mod.DISTINCT_INSTANCES + 1)])
    assert colours.max(axis=1).min() >= 0.5  # far from the black background
    # Every pair apart by far more than the writer's 0.02 exact-match tolerance.
    gaps = np.abs(colours[:, None, :] - colours[None, :, :]).max(axis=2)
    np.fill_diagonal(gaps, np.inf)
    assert gaps.min() >= 0.1 - 1e-6


# --------------------------------------------------------------------------------------
# KITTI Writer


@pytest.fixture
def writer():
    pytest.importorskip("OpenEXR")
    pytest.importorskip("PIL")
    return importlib.import_module("chunks.datasets.kitti_writer")


def test_writer_roots_keep_their_defaults_when_nothing_is_selected(scene, writer):
    # A PRIM_SELECTION LineEdit pre-fills from the selection on every build, a reload
    # included: with nothing selected the roots came back empty and nothing was labelled.
    node = scene.create.kitti_writer()
    assert node.get_param("root").value == "/World"
    assert node.get_param("camera").value == "/World/camera"


def test_decoder_resolves_antialiased_edges(writer):
    palette = np.array([(0, 0, 0), (1, 0, 0), (0, 1, 0), (0.5, 0.5, 1.0)], dtype=np.float32)
    truth = np.zeros((6, 6), dtype=np.int32)
    truth[:, :3], truth[:, 3:] = 1, 2
    truth[4:, 4:] = 3
    pixels = palette[truth].copy()
    # Blend the column edge 70/30 and a background edge 40/60, the way AA does.
    pixels[:4, 2] = 0.7 * palette[1] + 0.3 * palette[2]
    pixels[:4, 3] = 0.3 * palette[1] + 0.7 * palette[2]
    pixels[5, 5] = 0.4 * palette[3]
    decoded = writer.decode_ids(pixels, palette)
    expected = truth.copy()
    expected[5, 5] = 0
    assert (decoded == expected).all()


def test_camera_projects_its_own_aim_point_to_the_principal_point(writer):
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)  # the fallback is centimetres; this test is in metres
    camera = UsdGeom.Camera.Define(stage, "/cam")
    camera.CreateFocalLengthAttr(35.0)
    camera.CreateHorizontalApertureAttr(36.0)
    camera.CreateVerticalApertureAttr(24.0)
    model = writer.Camera(stage, Sdf.Path("/cam"), Usd.TimeCode.Default(), 1242, 375)
    ahead = model.to_camera(np.array([[0.0, 0.0, -10.0]]))
    assert ahead[0] == pytest.approx([0.0, 0.0, 10.0])  # KITTI: z forward
    assert model.project(ahead)[0] == pytest.approx([model.cx, model.cy])
    # The image centre, with pixel centres at integers: ((W - 1) / 2, (H - 1) / 2).
    assert (model.cx, model.cy) == pytest.approx((620.5, 187.0))
    # expandAperture on a 3.31:1 image keeps the vertical field of view.
    assert model.fy == pytest.approx(35.0 / 24.0 * 375)


def test_yaw_box_of_a_turned_cube(writer):
    corners = np.array([(x, y, z) for x in (-1, 1) for y in (-0.5, 0.5) for z in (-2, 2)], dtype=np.float64)
    yaw = math.radians(30.0)
    # Ry(yaw) in KITTI's camera frame, then 10 m ahead.
    ry = np.array([[math.cos(yaw), 0, math.sin(yaw)], [0, 1, 0], [-math.sin(yaw), 0, math.cos(yaw)]])
    points = corners @ ry.T + np.array([0.0, 0.0, 10.0])
    h, w, length, location, rotation_y = writer._yaw_box(points, ry @ np.array([1.0, 0.0, 0.0]))
    assert rotation_y == pytest.approx(yaw)
    assert (h, w, length) == pytest.approx((1.0, 4.0, 2.0))
    assert location == pytest.approx([0.0, 0.5, 10.0])  # bottom-face centre, y down


@pytest.mark.parametrize("position, inside", [((5, 0.5, 0), True), ((5, 0, 3), False), ((0, 0, 0), False)])
def test_a_camera_inside_an_object_is_caught(writer, position, inside):
    # A camera placed near objects at random sometimes lands in one; that frame is the
    # inside of a surface and its labels are nonsense, so the writer rejects it.
    stage = Usd.Stage.CreateInMemory()
    cube = UsdGeom.Cube.Define(stage, "/box")
    cube.CreateSizeAttr(2.0)
    UsdGeom.Xformable(cube).AddTranslateOp().Set(Gf.Vec3d(5, 0, 0))
    UsdGeom.Xformable(UsdGeom.Camera.Define(stage, "/cam")).AddTranslateOp().Set(Gf.Vec3d(*position))
    instance = writer._Instance(1, 1, "cube", Sdf.Path("/box"), None)
    instance.gprims = [cube.GetPrim()]
    model = writer.Camera(stage, Sdf.Path("/cam"), Usd.TimeCode.Default(), 100, 100)
    found = writer._camera_inside(model, [instance], Usd.TimeCode.Default())
    assert (found == Sdf.Path("/box")) if inside else found is None


def _yolo_settings(**extra):
    settings = {"classes": ["sphere", "cube"], "detect_classes": [], "rename": {"cube": "Cube"},
                "bbox_height_threshold": 25, "val_fraction": 0.3, "split_seed": 0}
    settings.update(extra)
    return settings


def _record(cls, box):
    return {"class": cls, "bbox2d": box}


def test_yolo_lines_are_the_kitti_boxes_relative_to_the_image(writer):
    # KITTI boxes are inclusive pixel indices; YOLO's are centre and size over the image.
    result = {"width": 200, "height": 100, "records": [
        _record("cube", (10, 20, 49, 59)),     # 40 x 40 px
        _record("sphere", (100, 0, 199, 99)),  # the whole right half
        _record("cube", (0, 0, 9, 9)),         # 10 px tall: DontCare in KITTI, so left out
        _record("cube", None),                 # fully occluded: no box at all
    ]}
    lines = writer.yolo_lines(result, _yolo_settings())
    assert lines == ["1 0.150000 0.400000 0.200000 0.400000", "0 0.750000 0.500000 0.500000 1.000000"]


def test_yolo_ids_count_only_the_detected_classes(writer):
    result = {"width": 100, "height": 100, "records": [_record("sphere", (0, 0, 49, 49)), _record("cube", (50, 50, 99, 99))]}
    settings = _yolo_settings(detect_classes=["cube"])
    assert writer.yolo_lines(result, settings) == ["0 0.750000 0.750000 0.500000 0.500000"]
    assert writer.yolo_names(settings) == ["Cube"]  # renamed, as in label_2


def test_the_split_is_a_pure_function_of_the_frame(writer):
    settings = _yolo_settings()
    splits = [writer.split_of(f"{i:06d}", settings) for i in range(1000)]
    assert splits == [writer.split_of(f"{i:06d}", settings) for i in range(1000)]
    assert 250 < splits.count("val") < 350  # about Validation Fraction


def test_the_index_writes_yolo_data_yaml_and_both_split_folders(writer, tmp_path):
    settings = _yolo_settings(write_yolo=True, class_colours={1: np.zeros(3), 2: np.ones(3)})
    writer._json(str(tmp_path / "qa" / "000001.json"), {"frame": "000001", "image_size": [200, 100]})
    writer.write_index(str(tmp_path), settings)
    assert (tmp_path / "yolo" / "data.yaml").read_text() == (
        'train: images/train\nval: images/val\nnames:\n  0: "sphere"\n  1: "Cube"\n')
    for kind in ("images", "labels"):
        for split in ("train", "val"):
            assert (tmp_path / "yolo" / kind / split).is_dir()  # a missing folder fails a trainer


def test_frame_path_matches_the_render_chunks_spelling(writer, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert writer.frame_path("raw/frame.####.exr", 7) == str(tmp_path / "raw" / "frame.0007.exr")
    assert writer.frame_path("raw/f{frame}.exr", 12.0) == str(tmp_path / "raw" / "f12.exr")
