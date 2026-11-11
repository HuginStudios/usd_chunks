"""The `box` handle: the faces of a box are the handles, and a drag moves one face.

A box drives six params, the distance from the anchor to each face. A host picks the face
under the mouse with pick_box_face -- against the box those six values describe, never the
geometry inside it -- and turns the drag into six values with drag_box_face.
"""
from __future__ import annotations

import pytest
from pxr import Gf, Usd, UsdGeom

from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_core import ChunkConfig, ChunkFlags
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import _CHUNKS_ROOT
from usd_chunks.gizmo import runtime as chunk_gizmo
from chunk_prim_authoring import _author_prim

from chunk_stubs import attach_stub_module

FACES = ("px", "nx", "py", "ny", "pz", "nz")
PX, NX, PY, NY, PZ, NZ = range(6)


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _stage(path: str = "/World/Box") -> Usd.Stage:
    stage = Usd.Stage.CreateInMemory()
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        UsdGeom.Xform.Define(stage, path)

    class _StagedScene(_DummyScene):
        def get_stage(self):
            return stage

    chunk_registry.ChunkRegistry().initialize(_StagedScene(), "pytest")
    return stage


def _volume(driver=None):
    c = chunk.Chunk("Volume")
    c.add_param(chunk.LineEdit("primpath_location", "Location", default_value="/World/Box",
                               flags=ChunkFlags.ParamFlags.LABELED))
    for name in FACES:
        c.add_param(chunk.Slider(name, name, default_value=1.0, min_value=0.0, max_value=50.0))
    c._config = ChunkConfig(module="volume", gizmos=[
        chunk.Gizmo("box", type="box", driver=list(driver or FACES)),
    ])
    attach_stub_module(c)
    return c


# ---------------------------------------------------------------------------
# the declaration
# ---------------------------------------------------------------------------

def test_a_box_handle_drives_exactly_six_params() -> None:
    with pytest.raises(ValueError, match="drives 6 params"):
        chunk.Gizmo("box", type="box", driver=["px", "nx", "py"])


def test_a_box_handle_authors_its_six_faces_in_order() -> None:
    stage = _stage()
    c = _volume()
    _author_prim(stage, c)
    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/{c.name}")

    assert prim.GetAttribute("ui:gizmo:box:type").Get() == "box"
    conns = prim.GetAttribute("ui:gizmo:box:driver").GetConnections()
    assert [str(p).rsplit(":", 1)[-1] for p in conns] == list(FACES)


def test_six_values_write_back_through_the_params() -> None:
    _stage()
    c = _volume()

    chunk_gizmo.apply_bindings(c, "box", [4.0, 1.0, 1.0, 1.0, 2.5, 1.0])

    assert [c.get_param(n).value for n in FACES] == [4.0, 1.0, 1.0, 1.0, 2.5, 1.0]


# ---------------------------------------------------------------------------
# picking: the box's own faces
# ---------------------------------------------------------------------------

UNIT = [1.0] * 6


def test_a_ray_from_plus_x_picks_the_plus_x_face() -> None:
    face, t = chunk_gizmo.pick_box_face(UNIT, (10, 0.2, -0.3), (-1, 0, 0))

    assert face == PX
    assert t == pytest.approx(9.0)


@pytest.mark.parametrize("origin, direction, expected", [
    ((-10, 0, 0), (1, 0, 0), NX),
    ((0, 10, 0), (0, -1, 0), PY),
    ((0, -10, 0), (0, 1, 0), NY),
    ((0, 0, 10), (0, 0, -1), PZ),
    ((0, 0, -10), (0, 0, 1), NZ),
])
def test_each_face_is_picked_from_its_own_side(origin, direction, expected) -> None:
    assert chunk_gizmo.pick_box_face(UNIT, origin, direction)[0] == expected


def test_a_slanted_ray_picks_the_face_it_enters_through() -> None:
    # Coming down steeply onto the top, near the +X edge: the top, not the side.
    face, _ = chunk_gizmo.pick_box_face([2, 1, 1, 1, 1, 1], (3, 0, 10), (-0.2, 0, -1))

    assert face == PZ


def test_a_lopsided_box_is_picked_where_its_faces_are() -> None:
    # +X face at 5: a ray at x=3 hits the top, though a unit box would have been missed.
    face, t = chunk_gizmo.pick_box_face([5, 1, 1, 1, 1, 1], (3, 0, 10), (0, 0, -1))

    assert (face, t) == (PZ, pytest.approx(9.0))


def test_a_ray_that_misses_picks_nothing() -> None:
    assert chunk_gizmo.pick_box_face(UNIT, (10, 5, 0), (-1, 0, 0)) is None
    assert chunk_gizmo.pick_box_face(UNIT, (10, 0, 0), (1, 0, 0)) is None, "box behind the ray"


def test_a_ray_starting_inside_the_box_picks_nothing() -> None:
    assert chunk_gizmo.pick_box_face(UNIT, (0, 0, 0), (1, 0, 0)) is None


# ---------------------------------------------------------------------------
# dragging: one face, along its normal
# ---------------------------------------------------------------------------

def test_dragging_a_face_moves_only_that_face_along_its_normal() -> None:
    # Looking down on the +X face from above, grabbed at its middle, mouse moved 3 along X.
    out = chunk_gizmo.drag_box_face(UNIT, PX, (1, 0, 0), (4, 0.7, 10), (0, 0, -1))

    assert out == pytest.approx([4.0, 1.0, 1.0, 1.0, 1.0, 1.0])


def test_sideways_mouse_motion_does_not_move_the_face() -> None:
    out = chunk_gizmo.drag_box_face(UNIT, PX, (1, 0, 0), (1, 5, 10), (0, 0, -1))

    assert out == pytest.approx(UNIT)


def test_a_minus_face_grows_when_pulled_outward() -> None:
    out = chunk_gizmo.drag_box_face(UNIT, NZ, (0, 0, -1), (10, 0, -3), (-1, 0, 0))

    assert out == pytest.approx([1.0, 1.0, 1.0, 1.0, 1.0, 3.0])


def test_a_face_is_never_pulled_past_the_opposite_one() -> None:
    out = chunk_gizmo.drag_box_face(UNIT, PX, (1, 0, 0), (-9, 0, 10), (0, 0, -1))

    assert out[PX] == pytest.approx(-1.0), "flush with the -X face, not beyond it"


def test_looking_straight_down_the_normal_leaves_the_face_where_it_is() -> None:
    out = chunk_gizmo.drag_box_face(UNIT, PX, (1, 0, 0), (10, 0, 0), (-1, 0, 0))

    assert out == pytest.approx(UNIT)


def test_a_whole_drag_picks_then_moves_the_face() -> None:
    """The host loop: pick on press, then each tick from the press's values and grab."""
    start = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    origin, direction = Gf.Vec3d(3, 0.5, 10), Gf.Vec3d(-0.4, 0, -1)  # meets the top at x=-0.6
    face, t = chunk_gizmo.pick_box_face(start, origin, direction)
    grab = origin + direction * t
    assert face == PZ

    out = chunk_gizmo.drag_box_face(start, face, grab, Gf.Vec3d(-10, 0.5, 3), Gf.Vec3d(1, 0, 0))

    assert out == pytest.approx([1.0, 1.0, 1.0, 1.0, 3.0, 1.0])
