"""An animated painted cloud shows each instance only once its point exists.

A Vine with Animate Growth on writes its leaf points as time samples, each leaf from the
frame the vine reaches it, with a stable id. Scatter Points instances leaves on that cloud;
without this, every leaf hung in the air from the first frame while the stems grew in under
them.
"""
from __future__ import annotations

import importlib

import numpy as np
import pytest
from pxr import Gf, UsdGeom

from usd_chunks import Scene

#: Three points with ids 0, 1, 2, appearing one per frame from frame 2, and a prototype.
_CLOUD = """#usda 1.0
def Points "Cloud"
{
    point3f[] points = [(0, 0, 0), (1, 0, 0), (2, 0, 0)]
    point3f[] points.timeSamples = {
        1: [],
        2: [(0, 0, 0)],
        3: [(0, 0, 0), (1, 0, 0)],
        4: [(0, 0, 0), (1, 0, 0), (2, 0, 0)],
    }
    int64[] ids = [0, 1, 2]
    int64[] ids.timeSamples = {1: [], 2: [0], 3: [0, 1], 4: [0, 1, 2]}
}

def Mesh "Leaf"
{
    int[] faceVertexCounts = [3]
    int[] faceVertexIndices = [0, 1, 2]
    point3f[] points = [(0, 0, 0), (0.1, 0, 0), (0, 0, 0.1)]
}
"""

_STILL = _CLOUD.split("    point3f[] points.timeSamples")[0] + "}\n" + _CLOUD.split("}\n\n", 1)[1]


def _scatter(code):
    scene = Scene.new()
    source = scene.create.inline_usd(code=code)
    scene.create.scatter_points(
        source, points_prim="/Cloud", inst_path="/Leaves", prototypes=[{"proto_path": "/Leaf"}],
    )
    return scene


def test_each_instance_shows_from_the_frame_its_point_appears() -> None:
    with _scatter(_CLOUD) as scene:
        instancer = UsdGeom.PointInstancer(scene.get_stage().GetPrimAtPath("/Leaves"))
        hidden = instancer.GetInvisibleIdsAttr()

        assert len(instancer.GetPositionsAttr().Get()) == 3
        assert hidden.GetTimeSamples() == [1.0, 2.0, 3.0, 4.0]
        assert [list(hidden.Get(f)) for f in (1, 2, 3, 4)] == [[0, 1, 2], [1, 2], [2], []]


def test_a_still_cloud_hides_nothing() -> None:
    with _scatter(_STILL) as scene:
        instancer = UsdGeom.PointInstancer(scene.get_stage().GetPrimAtPath("/Leaves"))

        assert len(instancer.GetPositionsAttr().Get()) == 3
        assert instancer.GetInvisibleIdsAttr().GetNumTimeSamples() == 0


def test_an_animated_cloud_is_not_handed_the_still_clouds_cached_cook() -> None:
    """Both clouds have the same default points. The cook cache's key has to see the time
    samples and ids too, or the animated one reuses the still one's cook, with no instance
    ever hidden."""
    module = importlib.import_module("chunks.modelling.scatter_points")
    keys = []
    for code in (_STILL, _CLOUD):
        scene = _scatter(code)
        with scene:
            chunk_obj = next(c for c in scene.get_chunks() if c.get_param("points_prim") is not None)
            keys.append(module.cache_key(chunk_obj))

    assert keys[0] != keys[1]


#: Two points on the ground facing up, with a direction and a size each.
_DIRECTED = """#usda 1.0
def Points "Cloud"
{
    point3f[] points = [(0, 0, 0), (1, 0, 0)]
    normal3f[] normals = [(0, 0, 1), (0, 0, 1)]
    vector3f[] primvars:direction = [(1, 0, 0), (0, 1, 0)] (interpolation = "vertex")
    float[] primvars:scale = [1, 0.5] (interpolation = "vertex")
}

def Mesh "Leaf"
{
    int[] faceVertexCounts = [3]
    int[] faceVertexIndices = [0, 1, 2]
    point3f[] points = [(0, 0, 0), (0.1, 0, 0), (0, 0.1, 0)]
}
"""


def test_a_painted_direction_turns_each_instance_and_a_painted_scale_sizes_it() -> None:
    """A Vine's leaf points say which way each leaf leaves its stem and how big it is."""
    scene = Scene.new()
    source = scene.create.inline_usd(code=_DIRECTED)
    scene.create.scatter_points(
        source, points_prim="/Cloud", inst_path="/Leaves", prototypes=[{"proto_path": "/Leaf"}],
        orient_mode="Align to Normal", align_up="+X", rand_axis="Off",
    )
    with scene:
        stage = scene.get_stage()
        instancer = UsdGeom.PointInstancer(stage.GetPrimAtPath("/Leaves"))
        orients = instancer.GetOrientationsAttr().Get()
        scales = [s[0] for s in instancer.GetScalesAttr().Get()]

        # The prototype's +X ends up along each point's direction.
        for orient, want in zip(orients, ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0))):
            axis = Gf.Rotation(Gf.Quatd(float(orient.GetReal()), Gf.Vec3d(orient.GetImaginary()))).TransformDir(
                Gf.Vec3d(1, 0, 0))
            assert np.allclose(tuple(axis), want, atol=2e-2)
        assert scales[1] == pytest.approx(scales[0] * 0.5, rel=1e-3)
