"""An animated painted cloud shows each instance only once its point exists.

A Vine with Animate Growth on writes its leaf points as time samples, each leaf from the
frame the vine reaches it, with a stable id. Scatter Points instances leaves on that cloud;
without this, every leaf hung in the air from the first frame while the stems grew in under
them.
"""
from __future__ import annotations

from pxr import UsdGeom

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
