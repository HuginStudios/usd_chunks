"""CAD Import, DXF: blocks become instanced parts, layers become Xforms, units become metres.

Every drawing here is written by ezdxf itself in the test, so each one holds exactly the
thing an assertion is about -- a block inserted three times, a layer switched off, a face
with its own colour -- and nothing else.
"""

from __future__ import annotations

import importlib

import pytest
from pxr import Gf, Usd, UsdGeom

ezdxf = pytest.importorskip("ezdxf", reason="needs ezdxf")

from ezdxf.acis import api as acis  # noqa: E402
from ezdxf.render import forms  # noqa: E402

from usd_chunks.chunk_script import Scene  # noqa: E402

ci = importlib.import_module("chunks.modelling.cad_import")

SETTINGS = ci._Settings(0.01, True, 20.0, False)

MM = 4
METRES = 6


def _new(units=MM):
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = units
    return doc, doc.modelspace()


def _square(layout, size=100.0, **attribs):
    return layout.add_3dface([(0, 0, 0), (size, 0, 0), (size, size, 0), (0, size, 0)], dxfattribs=attribs)


def _read(tmp_path, doc, name="drawing.dxf"):
    path = tmp_path / name
    doc.saveas(path)
    return ci._extract(str(path), SETTINGS)


def _layer(scene, name):
    return next(root for root in scene.roots if root.name == name)


def _leaves(node):
    if node.proto is not None:
        yield node
    for child in node.children:
        yield from _leaves(child)


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------


def test_a_block_inserted_three_times_is_one_prototype_placed_three_times(tmp_path) -> None:
    doc, msp = _new()
    _square(doc.blocks.new("CHAIR"))
    for x in (0, 1000, 2000):
        msp.add_blockref("CHAIR", (x, 0), dxfattribs={"layer": "Furniture"})

    scene = _read(tmp_path, doc)

    assert len(scene.protos) == 1
    assert scene.protos[0].name == "CHAIR"
    assert scene.protos[0].uses == 3
    furniture = _layer(scene, "Furniture")
    assert [child.name for child in furniture.children] == ["CHAIR"] * 3


def test_an_insert_keeps_its_own_transform_in_metres(tmp_path) -> None:
    doc, msp = _new()
    _square(doc.blocks.new("TILE"))
    msp.add_blockref("TILE", (1500, 250, 0), dxfattribs={"rotation": 90})

    scene = _read(tmp_path, doc)
    placement = scene.roots[0].children[0]

    assert placement.matrix.ExtractTranslation() == Gf.Vec3d(1.5, 0.25, 0.0)
    # Rotated a quarter turn about Z: the block's +X now points along +Y.
    x_axis = placement.matrix.TransformDir(Gf.Vec3d(1, 0, 0))
    assert Gf.IsClose(x_axis, Gf.Vec3d(0, 1, 0), 1e-9)


def test_a_minsert_array_is_one_placement_per_cell(tmp_path) -> None:
    doc, msp = _new()
    _square(doc.blocks.new("TILE"))
    grid = msp.add_blockref("TILE", (0, 0))
    grid.grid(size=(2, 3), spacing=(200, 300))

    scene = _read(tmp_path, doc)

    assert scene.protos[0].uses == 6
    origins = sorted(tuple(n.matrix.ExtractTranslation()) for n in scene.roots[0].children)
    assert origins[0] == (0.0, 0.0, 0.0)
    assert origins[-1] == pytest.approx((0.6, 0.2, 0.0))


def test_a_block_with_geometry_and_nested_blocks_keeps_its_geometry_a_level_down(tmp_path) -> None:
    """An instanceable prim ignores the children authored under it, so the two cannot share one."""
    doc, msp = _new()
    _square(doc.blocks.new("LEG"), size=10)
    table = doc.blocks.new("TABLE")
    _square(table, size=500)
    table.add_blockref("LEG", (0, 0))
    table.add_blockref("LEG", (490, 0))
    msp.add_blockref("TABLE", (0, 0))
    msp.add_blockref("TABLE", (2000, 0))

    scene = _read(tmp_path, doc)
    first = scene.roots[0].children[0]

    assert first.name == "TABLE"
    assert first.proto is None
    assert [child.name for child in first.children] == ["geometry", "LEG", "LEG"]
    by_name = {p.name: p for p in scene.protos}
    assert by_name["TABLE"].uses == 2
    assert by_name["LEG"].uses == 4


def test_model_space_is_split_by_layer_and_hidden_layers_are_left_out(tmp_path) -> None:
    doc, msp = _new()
    doc.layers.add("Walls")
    doc.layers.add("Notes").off()
    doc.layers.add("Old").freeze()
    _square(msp, layer="Walls")
    _square(msp, layer="Notes")
    _square(msp, layer="Old")

    scene = _read(tmp_path, doc)

    assert [root.name for root in scene.roots] == ["Walls"]


def test_a_block_that_inserts_itself_does_not_recurse_forever(tmp_path) -> None:
    doc, msp = _new()
    loop = doc.blocks.new("LOOP")
    _square(loop)
    loop.add_blockref("LOOP", (100, 0))
    msp.add_blockref("LOOP", (0, 0))

    scene = _read(tmp_path, doc)

    assert scene.protos[0].name == "LOOP"


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------


def test_millimetres_in_metres_out(tmp_path) -> None:
    doc, msp = _new(MM)
    _square(msp, size=2000)
    points = _read(tmp_path, doc).protos[0].points
    assert max(p[0] for p in points) == pytest.approx(2.0)

    doc, msp = _new(METRES)
    _square(msp, size=2)
    points = _read(tmp_path, doc, "metres.dxf").protos[0].points
    assert max(p[0] for p in points) == pytest.approx(2.0)


def test_a_unitless_drawing_is_taken_as_millimetres(tmp_path) -> None:
    doc, msp = _new(0)
    _square(msp, size=1000)
    points = _read(tmp_path, doc).protos[0].points
    assert max(p[0] for p in points) == pytest.approx(1.0)


def test_a_concave_mesh_face_is_triangulated_without_losing_area(tmp_path) -> None:
    doc, msp = _new(METRES)
    mesh = msp.add_mesh()
    with mesh.edit_data() as data:
        # A 2 x 1 rectangle with a notch to (1, 0.5): area 1.5, three triangles. A fan from
        # the first corner would get this wrong; so would a polyface, which stops at four.
        data.vertices = [(0, 0, 0), (2, 0, 0), (2, 1, 0), (1, 0.5, 0), (0, 1, 0)]
        data.faces = [(0, 1, 2, 3, 4)]

    proto = _read(tmp_path, doc).protos[0]

    assert proto.counts == [3, 3, 3]
    assert len(proto.points) == 5
    area = 0.0
    for t in range(3):
        a, b, c = (proto.points[i] for i in proto.indices[t * 3:t * 3 + 3])
        area += Gf.Cross(b - a, c - a).GetLength() / 2
    assert area == pytest.approx(1.5)


def test_a_polyface_keeps_its_quads_and_its_shared_points(tmp_path) -> None:
    """Two quads sharing an edge: six points, not eight, and not four triangles.

    The file's topology is the modeller's. Pulling every face apart leaves a mesh that
    cannot be smoothed, subdivided or edited as one surface.
    """
    doc, msp = _new(METRES)
    polyface = msp.add_polyface()
    polyface.append_face([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)])
    polyface.append_face([(1, 0, 0), (2, 0, 0), (2, 1, 0), (1, 1, 0)])

    proto = _read(tmp_path, doc).protos[0]

    assert proto.counts == [4, 4]
    assert len(proto.points) == 6
    assert len(set(proto.indices[:4]) & set(proto.indices[4:])) == 2


def test_a_flat_faced_acis_solid_is_meshed(tmp_path) -> None:
    doc, msp = _new(METRES)
    solid = msp.add_3dsolid()
    acis.export_dxf(solid, [acis.body_from_mesh(forms.cube())])

    proto = _read(tmp_path, doc).protos[0]

    assert proto.counts == [4] * 6
    assert len(proto.points) == 8
    assert proto.double_sided


def test_linework_becomes_curves_flattened_to_the_deflection(tmp_path) -> None:
    doc, msp = _new(METRES)
    msp.add_line((0, 0), (1, 0))
    msp.add_circle((0, 0), 1.0)

    proto = _read(tmp_path, doc).protos[0]

    assert not proto.counts
    assert len(proto.curve_counts) == 2
    assert proto.curve_counts[0] == 2
    assert proto.curve_counts[1] > 16


def test_annotation_is_skipped_and_counted(tmp_path) -> None:
    doc, msp = _new()
    _square(msp)
    msp.add_text("hello")
    msp.add_text("again")
    msp.add_mtext("note")

    scene = _read(tmp_path, doc)

    assert scene.skipped == "2 TEXT, 1 MTEXT"


def test_uvs_are_planar_and_in_metres(tmp_path) -> None:
    doc, msp = _new(MM)
    _square(msp, size=3000)

    proto = _read(tmp_path, doc).protos[0]

    assert proto.uv_face_varying
    assert len(proto.uvs) == len(proto.indices)
    assert max(uv[0] for uv in proto.uvs) == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# colour
# ---------------------------------------------------------------------------


def test_colour_comes_from_the_entity_or_its_layer(tmp_path) -> None:
    doc, msp = _new()
    doc.layers.add("Red", color=1)
    _square(msp, layer="Red")
    _square(msp, layer="Red", true_color=ezdxf.rgb2int((0, 255, 0)))

    proto = _read(tmp_path, doc).protos[0]

    assert set(proto.face_colors) == {(1.0, 0.0, 0.0), (0.0, 1.0, 0.0)}


def test_layer_zero_inside_a_block_takes_the_placements_colour(tmp_path) -> None:
    doc, msp = _new()
    _square(doc.blocks.new("PART"), layer="0")
    msp.add_blockref("PART", (0, 0))

    proto = _read(tmp_path, doc).protos[0]

    assert set(proto.face_colors) == {None}


# ---------------------------------------------------------------------------
# the whole chunk
# ---------------------------------------------------------------------------


@pytest.fixture
def scene():
    s = Scene(Usd.Stage.CreateInMemory(), "pytest")
    yield s
    s.close()


def test_the_chunk_imports_a_drawing_onto_the_stage(scene, tmp_path) -> None:
    doc, msp = _new(MM)
    _square(doc.blocks.new("CHAIR"), size=500)
    for x in (0, 1000):
        msp.add_blockref("CHAIR", (x, 0), dxfattribs={"layer": "Furniture"})
    msp.add_line((0, 0), (5000, 0), dxfattribs={"layer": "Walls"})
    msp.add_text("Room 1", dxfattribs={"layer": "Walls"})
    path = tmp_path / "plan.dxf"
    doc.saveas(path)

    node = scene.add("cad_import", file_path=str(path), primpath_location="/plan")
    stage = scene.get_stage()

    chair_mesh = UsdGeom.Mesh(stage.GetPrimAtPath("/plan/Prototypes/CHAIR/mesh"))
    assert chair_mesh
    assert chair_mesh.GetDoubleSidedAttr().Get() is True
    assert list(chair_mesh.GetFaceVertexCountsAttr().Get()) == [4]
    st = UsdGeom.PrimvarsAPI(chair_mesh).GetPrimvar("st")
    assert st.GetInterpolation() == UsdGeom.Tokens.faceVarying
    assert len(st.Get()) == 4
    placements = [stage.GetPrimAtPath(f"/plan/Furniture/{name}") for name in ("CHAIR", "CHAIR_2")]
    assert all(p.IsInstance() for p in placements)
    assert UsdGeom.BasisCurves(stage.GetPrimAtPath("/plan/Prototypes/Walls/curves"))
    assert stage.GetPrimAtPath("/plan/Walls").HasAuthoredReferences()

    readout = node.get_param("readout").value
    assert "1 curves" in readout
    assert "skipped 1 TEXT" in readout
