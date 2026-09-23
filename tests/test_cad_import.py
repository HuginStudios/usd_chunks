"""CAD Import: the structure a converter is actually judged on.

Turning B-rep into triangles is the easy half, and every converter does it. What separates
a useful CAD import from a dump of meshes is what survives the trip — the assembly tree,
which components are the same part placed twice, and the colours that are the only material
intent a STEP file carries. Those are what these pin.

The reading tests build their own STEP file with OCCT's own writer rather than committing a
binary fixture: the file is then small, readable, regenerable, and — the point — authored
with known names, a known colour and a part deliberately placed twice, so an assertion about
instancing is an assertion about something we put there on purpose.

The authoring tests need no OCP at all. The USD half of this chunk is reachable without the
wheel, and it is the half with the composition rules in it, so it is tested unconditionally.
"""

from __future__ import annotations

import importlib

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom

ci = importlib.import_module("scripts.modelling.cad_import")

needs_ocp = pytest.mark.skipif(not ci._HAS_OCP, reason="needs cadquery-ocp")


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def _cube_proto(name="part", colour=(0.5, 0.5, 0.5), faces=6, uvs=True):
    """A stand-in tessellated part: `faces` triangles, one colour, no real geometry.

    The authoring half never looks at where the points are, only at how many arrays there
    are and what is authored around them, so a plausible shape would only be scenery.
    """
    points = [Gf.Vec3f(i, 0.0, 0.0) for i in range(faces * 3)]
    indices = list(range(faces * 3))
    return ci._Proto(
        name=name,
        points=points,
        counts=[3] * faces,
        indices=indices,
        face_colors=[colour] * faces,
        uvs=[Gf.Vec2f(i * 0.5, i * 0.25) for i in range(faces * 3)] if uvs else [],
    )


def _author(stage, scene_protos, roots, instance=True, scale=1.0, keep_hierarchy=True,
            texture_size=0.0):
    """Run the chunk's authoring half the way execute() does, and hand back the counters."""
    root = Sdf.Path("/Asset")
    stage.DefinePrim(root, "Xform")
    scope = root.AppendChild("Prototypes")
    ci.prototype_scope(stage, scope)

    paths, taken = [], set()
    for proto in scene_protos:
        path = scope.AppendChild(ci._unique(proto.name, taken))
        ci._author_proto(stage, path, proto, scale, texture_size)
        paths.append(path)

    counters = {"parts": 0, "uses": [p.uses for p in scene_protos]}
    taken = set()
    if keep_hierarchy:
        for node in roots:
            ci._author_node(stage, root, node, paths, instance, scale, taken, counters)
    else:
        flat: list = []
        for node in roots:
            ci._flatten(node, Gf.Matrix4d(1.0), flat)
        for leaf in flat:
            ci._author_node(stage, root, leaf, paths, instance, scale, taken, counters)
    return counters


def _placed(name, proto, matrix=None, children=None):
    return ci._Node(
        name=name,
        matrix=matrix if matrix is not None else Gf.Matrix4d(1.0),
        proto=proto,
        children=list(children or []),
    )


# ---------------------------------------------------------------------------
# authoring — no OCP required
# ---------------------------------------------------------------------------


def test_the_prototype_is_hidden_but_its_placements_are_not() -> None:
    """The shared copy must not render on its own — only through its references.

    It is an ordinary prim, hidden by the scope above it. That the invisibility does *not*
    travel down the reference onto the placement is the whole reason it is authored there,
    so both halves are asserted together.
    """
    stage = Usd.Stage.CreateInMemory()
    proto = _cube_proto()
    proto.uses = 1
    _author(stage, [proto], [_placed("a", 0)])

    prim = stage.GetPrimAtPath("/Asset/Prototypes/part")
    assert prim.GetSpecifier() == Sdf.SpecifierDef
    assert not prim.IsAbstract()
    assert UsdGeom.Imageable(prim).ComputeVisibility() == UsdGeom.Tokens.invisible
    assert stage.GetPrimAtPath("/Asset/Prototypes/part/mesh").IsA(UsdGeom.Mesh)

    placement = stage.GetPrimAtPath("/Asset/a")
    assert UsdGeom.Imageable(placement).ComputeVisibility() == UsdGeom.Tokens.inherited


def test_a_part_placed_twice_is_instanced_and_a_part_placed_once_is_not() -> None:
    """Instancing where it pays, and not where it only costs the ability to override."""
    stage = Usd.Stage.CreateInMemory()
    repeated, lonely = _cube_proto("repeated"), _cube_proto("lonely")
    repeated.uses, lonely.uses = 2, 1
    _author(stage, [repeated, lonely], [_placed("a", 0), _placed("b", 0), _placed("c", 1)])

    assert stage.GetPrimAtPath("/Asset/a").IsInstance()
    assert stage.GetPrimAtPath("/Asset/b").IsInstance()
    assert not stage.GetPrimAtPath("/Asset/c").IsInstance()


def test_instancing_off_still_shares_the_mesh_through_a_reference() -> None:
    """Turning instancing off buys back per-placement overrides, not a duplicated mesh."""
    stage = Usd.Stage.CreateInMemory()
    proto = _cube_proto()
    proto.uses = 2
    _author(stage, [proto], [_placed("a", 0), _placed("b", 0)], instance=False)

    assert not any(p.IsInstance() for p in stage.Traverse())
    for name in ("a", "b"):
        prim = stage.GetPrimAtPath(f"/Asset/{name}")
        assert prim.GetPrim().HasAuthoredReferences()
        assert stage.GetPrimAtPath(f"/Asset/{name}/mesh").IsA(UsdGeom.Mesh)


def test_a_placement_with_its_own_colour_is_not_instanced() -> None:
    """A per-placement colour cannot survive instancing, so the colour wins.

    Nothing inside an instance can be overridden, so authoring the override *and* marking
    the prim instanceable would silently drop the colour. Left un-instanced, both hold.
    """
    stage = Usd.Stage.CreateInMemory()
    proto = _cube_proto()
    proto.uses = 2
    plain = _placed("plain", 0)
    tinted = _placed("tinted", 0)
    tinted.color = (1.0, 0.0, 0.0)
    _author(stage, [proto], [plain, tinted])

    assert stage.GetPrimAtPath("/Asset/plain").IsInstance()
    assert not stage.GetPrimAtPath("/Asset/tinted").IsInstance()
    colour = UsdGeom.PrimvarsAPI(stage.GetPrimAtPath("/Asset/tinted")).GetPrimvar("displayColor")
    assert tuple(colour.Get()[0]) == pytest.approx((1.0, 0.0, 0.0))


def test_flattening_keeps_every_part_where_it_was() -> None:
    """Dropping the hierarchy must move nothing — the parent's transform is composed in."""
    child_matrix = Gf.Matrix4d().SetTranslate(Gf.Vec3d(1.0, 0.0, 0.0))
    parent_matrix = Gf.Matrix4d().SetRotate(Gf.Rotation(Gf.Vec3d(0, 1, 0), 90.0))
    tree = ci._Node(
        name="group", matrix=parent_matrix,
        children=[_placed("leaf", 0, child_matrix)],
    )

    nested = Usd.Stage.CreateInMemory()
    proto = _cube_proto()
    proto.uses = 1
    _author(nested, [proto], [tree])

    flat = Usd.Stage.CreateInMemory()
    proto_flat = _cube_proto()
    proto_flat.uses = 1
    _author(flat, [proto_flat], [tree], keep_hierarchy=False)

    assert flat.GetPrimAtPath("/Asset/leaf")
    assert not flat.GetPrimAtPath("/Asset/group")

    def world(stage, path):
        return UsdGeom.Xformable(stage.GetPrimAtPath(path)).ComputeLocalToWorldTransform(
            Usd.TimeCode.Default()
        )

    nested_world = world(nested, "/Asset/group/leaf")
    flat_world = world(flat, "/Asset/leaf")
    for row in range(4):
        for col in range(4):
            assert nested_world[row][col] == pytest.approx(flat_world[row][col], abs=1e-9)


def test_one_colour_is_constant_and_several_become_uniform_plus_subsets() -> None:
    """A colour per face is the only material intent STEP carries, so it becomes bindable."""
    stage = Usd.Stage.CreateInMemory()
    plain = _cube_proto("plain", colour=(0.2, 0.3, 0.4), faces=4)
    mixed = _cube_proto("mixed", faces=4)
    mixed.face_colors = [(1.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 1.0, 0.0)]
    plain.uses = mixed.uses = 1
    _author(stage, [plain, mixed], [_placed("a", 0), _placed("b", 1)])

    single = UsdGeom.PrimvarsAPI(
        stage.GetPrimAtPath("/Asset/Prototypes/plain/mesh")
    ).GetPrimvar("displayColor")
    assert single.GetInterpolation() == UsdGeom.Tokens.constant
    assert len(single.Get()) == 1

    mesh = UsdGeom.Mesh(stage.GetPrimAtPath("/Asset/Prototypes/mixed/mesh"))
    many = UsdGeom.PrimvarsAPI(mesh.GetPrim()).GetPrimvar("displayColor")
    assert many.GetInterpolation() == UsdGeom.Tokens.uniform
    assert len(many.Get()) == 4

    # Findable on the prototype itself, which is the point of it being an ordinary prim
    # rather than a class: under a class every descendant is abstract and the default child
    # predicate hides the subsets from this very query.
    subsets = UsdGeom.Subset.GetAllGeomSubsets(mesh)
    assert len(UsdGeom.Subset.GetAllGeomSubsets(
        UsdGeom.Mesh(stage.GetPrimAtPath("/Asset/b/mesh"))
    )) == 2
    assert len(subsets) == 2
    for subset in subsets:
        assert subset.GetFamilyNameAttr().Get() == "materialBind"
        assert subset.GetElementTypeAttr().Get() == UsdGeom.Tokens.face
    assert sorted(len(s.GetIndicesAttr().Get()) for s in subsets) == [2, 2]


def test_meshes_are_not_subdivided() -> None:
    """The default scheme would round off every edge the CAD kernel was told to keep."""
    stage = Usd.Stage.CreateInMemory()
    proto = _cube_proto()
    proto.uses = 1
    _author(stage, [proto], [_placed("a", 0)])

    mesh = UsdGeom.Mesh(stage.GetPrimAtPath("/Asset/Prototypes/part/mesh"))
    assert mesh.GetSubdivisionSchemeAttr().Get() == UsdGeom.Tokens.none
    assert mesh.GetExtentAttr().Get() is not None


def test_sibling_names_never_collide() -> None:
    """Two CAD parts may share a name; two USD siblings may not."""
    taken: set = set()
    assert [ci._unique(n, taken) for n in ("bolt", "bolt", "bolt")] == ["bolt", "bolt_2", "bolt_3"]
    assert ci._unique("2 bad/name", set()).isidentifier()


# ---------------------------------------------------------------------------
# reading — needs the OCCT wheel
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def step_file(tmp_path_factory):
    """A STEP assembly written by OCCT itself: two placements of one part, plus a red pin.

    Built in millimetres, which is what STEP files are overwhelmingly written in, so the
    unit conversion has something real to do.
    """
    pytest.importorskip("OCP", reason="needs cadquery-ocp")
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Trsf, gp_Vec
    from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.STEPControl import STEPControl_StepModelType
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDocStd import TDocStd_Document
    from OCP.TopLoc import TopLoc_Location
    from OCP.XCAFApp import XCAFApp_Application
    from OCP.XCAFDoc import XCAFDoc_ColorType, XCAFDoc_DocumentTool

    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("BinXCAF"))
    doc = app.NewDocument(TCollection_ExtendedString("BinXCAF"), doc)[0]
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    color_tool = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())

    bracket = shape_tool.AddShape(BRepPrimAPI_MakeBox(100.0, 50.0, 20.0).Shape(), False)
    pin = shape_tool.AddShape(BRepPrimAPI_MakeCylinder(20.0, 60.0).Shape(), False)
    TDataStd_Name.Set_s(bracket, TCollection_ExtendedString("Bracket"))
    TDataStd_Name.Set_s(pin, TCollection_ExtendedString("Pin"))
    color_tool.SetColor(
        pin, Quantity_Color(0.8, 0.1, 0.1, Quantity_TOC_RGB), XCAFDoc_ColorType.XCAFDoc_ColorSurf
    )

    def at(x, y, z):
        trsf = gp_Trsf()
        trsf.SetTranslation(gp_Vec(x, y, z))
        return TopLoc_Location(trsf)

    # A THIRD product, geometrically identical to the bracket but added separately, so it
    # gets its own PRODUCT in the file. This is how most CAD packages export a part used
    # several times, and XCAF has no way to know it is the same thing.
    twin = shape_tool.AddShape(BRepPrimAPI_MakeBox(100.0, 50.0, 20.0).Shape(), False)
    TDataStd_Name.Set_s(twin, TCollection_ExtendedString("Bracket_copy"))

    assembly = shape_tool.NewShape()
    TDataStd_Name.Set_s(assembly, TCollection_ExtendedString("Widget"))
    for label, name, where in (
        (bracket, "left", at(0, 0, 0)),
        (bracket, "right", at(200, 0, 0)),
        (pin, "pin", at(0, 0, 100)),
        (twin, "twin", at(0, 200, 0)),
    ):
        component = shape_tool.AddComponent(assembly, label, where)
        TDataStd_Name.Set_s(component, TCollection_ExtendedString(name))
    shape_tool.UpdateAssemblies()

    path = tmp_path_factory.mktemp("cad") / "widget.stp"
    writer = STEPCAFControl_Writer()
    writer.Transfer(doc, STEPControl_StepModelType.STEPControl_AsIs)
    writer.Write(str(path))
    return str(path)


@pytest.fixture(scope="module")
def widget(step_file):
    return ci._extract(step_file, ci._Settings(0.1, True, 30.0, False))


@needs_ocp
def test_the_assembly_tree_and_its_names_survive(widget) -> None:
    assert len(widget.roots) == 1
    root = widget.roots[0]
    assert root.name == "Widget"
    assert [child.name for child in root.children] == ["left", "right", "pin", "twin"]


@needs_ocp
def test_the_same_part_placed_twice_is_tessellated_once(widget) -> None:
    """The whole reason to read XCAF rather than the plain shape reader."""
    left, right = widget.roots[0].children[:2]
    assert left.proto == right.proto


@needs_ocp
def test_identical_geometry_exported_as_two_products_becomes_one_prototype(widget) -> None:
    """The case that actually decides whether instancing does anything on a real file.

    XCAF identity alone sees `Bracket` and `Bracket_copy` as two unrelated parts, because in
    the file they are. Only the geometry digest collapses them — and on a real assembly that
    is the difference between instancing sixteen bolts and instancing none of them.
    """
    assert len(widget.protos) == 2  # the box (x3 placements) and the pin

    box = max(widget.protos, key=lambda proto: proto.uses)
    assert box.uses == 3

    left, right, _pin, twin = widget.roots[0].children
    assert left.proto == right.proto == twin.proto


def _proto_of(widget, name):
    node = next(n for n in widget.roots[0].children if n.name == name)
    return widget.protos[node.proto]


@needs_ocp
def test_colour_survives_the_round_trip(widget) -> None:
    pin_colours = {c for c in _proto_of(widget, "pin").face_colors if c is not None}
    assert len(pin_colours) == 1
    assert next(iter(pin_colours)) == pytest.approx((0.8, 0.1, 0.1), abs=1e-3)


@needs_ocp
def test_millimetres_in_metres_out(widget) -> None:
    """A 100 mm box is 0.1 in a metres stage, not 100 — the commonest CAD import bug."""
    box = _proto_of(widget, "left")
    span = max(p[0] for p in box.points) - min(p[0] for p in box.points)
    assert span == pytest.approx(0.1, abs=1e-6)


@needs_ocp
def test_placements_keep_their_own_transforms(widget) -> None:
    left, right, pin, _twin = widget.roots[0].children
    assert left.matrix.ExtractTranslation() == pytest.approx(Gf.Vec3d(0, 0, 0), abs=1e-9)
    assert right.matrix.ExtractTranslation() == pytest.approx(Gf.Vec3d(0.2, 0, 0), abs=1e-6)
    assert pin.matrix.ExtractTranslation() == pytest.approx(Gf.Vec3d(0, 0, 0.1), abs=1e-6)


@needs_ocp
def test_faces_are_not_welded_together(widget) -> None:
    """Each B-rep face keeps its own vertices, which is what creases the edges for free.

    A welded box has 8 points. This one must have 24 — six faces of four — or a renderer
    averaging normals across the corners would round the box off.
    """
    box = _proto_of(widget, "left")
    assert len(box.points) == 24
    assert len(box.counts) == 12


@needs_ocp
def test_a_file_that_is_not_there_is_reported_not_raised() -> None:
    with pytest.raises(RuntimeError):
        ci._extract("no_such_file.stp", ci._Settings(0.1, True, 30.0, False))


# ---------------------------------------------------------------------------
# UVs
# ---------------------------------------------------------------------------


def test_no_st_primvar_when_uvs_are_turned_off() -> None:
    stage = Usd.Stage.CreateInMemory()
    proto = _cube_proto()
    proto.uses = 1
    _author(stage, [proto], [_placed("a", 0)], texture_size=0.0)

    mesh = stage.GetPrimAtPath("/Asset/Prototypes/part/mesh")
    assert not UsdGeom.PrimvarsAPI(mesh).GetPrimvar("st")


def test_st_is_authored_per_vertex() -> None:
    """One UV per point, with no index array — which only works because faces are unwelded.

    Every point belongs to exactly one face, so `vertex` carries the same information a
    `faceVarying` primvar would at half the size.
    """
    stage = Usd.Stage.CreateInMemory()
    proto = _cube_proto()
    proto.uses = 1
    _author(stage, [proto], [_placed("a", 0)], texture_size=1.0)

    st = UsdGeom.PrimvarsAPI(stage.GetPrimAtPath("/Asset/Prototypes/part/mesh")).GetPrimvar("st")
    assert st
    assert st.GetInterpolation() == UsdGeom.Tokens.vertex
    assert st.GetTypeName() == Sdf.ValueTypeNames.TexCoord2fArray
    assert len(st.Get()) == len(proto.points)
    assert not st.GetIndicesAttr().HasAuthoredValue()


def test_texture_size_is_metres_per_tile() -> None:
    """Halving the tile size doubles the UVs, so the texture repeats twice as often."""
    values = {}
    for size in (1.0, 0.5):
        stage = Usd.Stage.CreateInMemory()
        proto = _cube_proto()
        proto.uses = 1
        _author(stage, [proto], [_placed("a", 0)], texture_size=size)
        st = UsdGeom.PrimvarsAPI(
            stage.GetPrimAtPath("/Asset/Prototypes/part/mesh")
        ).GetPrimvar("st")
        values[size] = st.Get()[-1]

    assert values[0.5][0] == pytest.approx(values[1.0][0] * 2.0)
    assert values[0.5][1] == pytest.approx(values[1.0][1] * 2.0)


@needs_ocp
def test_uvs_measure_surface_distance_in_metres(widget) -> None:
    """The claim the whole rescaling exists to make, checked against a known shape.

    OCCT hands back each face's UVs in its surface's own parameter domain, whose scale is
    arbitrary — on a real part the metres-per-UV-unit varied by 204x between faces. After
    rescaling, a UV unit is a metre of surface everywhere. The bracket is a 100x50x20 mm box,
    so its faces must measure 0.1, 0.05 and 0.02 in UV space and nothing else.
    """
    box = _proto_of(widget, "left")
    assert len(box.uvs) == len(box.points)

    spans = set()
    for face in range(len(box.counts) // 2):  # two triangles per box face
        corners = [box.indices[face * 6 + k] for k in range(6)]
        us = [box.uvs[i][0] for i in corners]
        vs = [box.uvs[i][1] for i in corners]
        spans.add((round(max(us) - min(us), 4), round(max(vs) - min(vs), 4)))

    for width, height in spans:
        assert {width, height} <= {0.1, 0.05, 0.02}, f"unexpected face span {width}x{height}"
    assert len(spans) == 3  # 100x50, 100x20 and 50x20, each appearing twice


@needs_ocp
def test_every_point_gets_a_uv_even_without_a_parameterisation(widget) -> None:
    """The arrays must stay the same length whatever the surface offered, or `vertex`
    interpolation is invalid and the mesh will not load."""
    for proto in widget.protos:
        assert len(proto.uvs) == len(proto.points)
