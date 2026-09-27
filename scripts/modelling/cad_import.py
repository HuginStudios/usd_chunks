"""CAD Import chunk -- a STEP or IGES assembly read straight into the stage as USD.

The geometry half is OpenCASCADE (`cadquery-ocp`, the OCCT kernel as a binary wheel), read
through **XCAF** rather than the plain shape reader. That distinction is the whole point of
this chunk: `STEPControl_Reader` hands back one anonymous blob of B-rep, while
`STEPCAFControl_Reader` fills a document that still knows the assembly tree, the part names,
the per-part and per-face colours, and -- crucially -- *which components are the same part
placed twice*. Conversion is the commodity; the structure is what is worth having.

So the four things a converter usually throws away all survive here:

* **Hierarchy.** Every assembly node becomes an `Xform`, every component a child of it, each
  carrying its own local matrix. A CAD tree arrives as a USD tree, not as a flat bag of
  meshes with baked transforms.
* **Instancing.** A part is tessellated *once*, into a prim under an invisible `Prototypes`
  scope, and every placement of it is an internal reference to that prim. Where a part is
  used more than once those references are marked `instanceable`, so a hundred identical
  screws cost one mesh. Parts are matched both by XCAF's own identity *and* by a digest of
  the tessellated geometry, since plenty of exporters give each occurrence of a bolt its own
  PRODUCT. What the digest does **not** yet catch is the exporter that also bakes each
  placement into the part's own coordinates: those copies are the same shape sitting at
  different positions and rotations, so their points differ and the digest separates them.
  Catching those needs rigid-transform matching (a rotation-invariant signature, then Kabsch
  against a candidate, then folding the recovered transform into the placement) and is not
  implemented -- on a file exported that way, expect one prototype per occurrence. Note also
  that direct material binding does not compose inside an instanceable prim: bind through a
  `UsdCollection` (the `collection` chunk) when you need per-instance materials.
* **Colour, as something you can bind to.** STEP carries a colour per face, not a material.
  Faces are grouped by colour into `GeomSubset`s in the `materialBind` family, so the
  colours become the handles a material library binds against -- the shiny-black region of a
  part is already a subset waiting for a MaterialX surface. `displayColor` is authored too
  (constant when the part is one colour, `uniform` when it is not) so it looks right before
  anyone binds anything.
* **UVs with even texel density.** OCCT gives each node its position in the surface's own
  parameter domain, which is free but unusable as it stands: a cylinder's u is an angle in
  radians while a plane's is a length in metres, so on one real part the metres-per-UV-unit
  varied by 204x between faces. Each face is rescaled by its own 3D-area-to-UV-area ratio,
  which brings that spread to about 2x and makes one UV unit one metre of surface. A tiling
  material then has consistent texel density over the whole part -- and without any UVs at
  all, a textured MaterialX surface renders as a flat colour, which is what these are for.
  `Texture Size` says how many metres one tile covers. This is not an atlas: faces are
  independent islands that overlap, right for a repeating material and wrong for baking.

**Points are deliberately not welded across face boundaries.** Each B-rep face contributes
its own vertices, so a renderer computing normals averages them *within* a face and creases
*between* faces. That is exactly the CAD look -- smooth cylinders, sharp edges -- for free,
with no authored normals to go stale. `subdivisionScheme` is set to `none` for the same
reason: the default would round every hard edge off.

Units: the file is read with OCCT's target unit set to metres, so whatever the STEP declares
is converted for us, and the result is then divided by the stage's `metersPerUnit`. The
`Extra Scale` slider exists only for files that lie about their units, which in CAD is not
rare.

Tessellation is cached against the file's mtime and the three meshing params, so changing
Extra Scale, or the hierarchy and instancing toggles, re-authors without re-meshing. That
matters: meshing a real assembly is the slow part, and OCCT through Python bindings is not
fast.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
from dataclasses import dataclass, field

from pxr import Gf, Sdf, Tf, Usd, UsdGeom, Vt

from usd_chunks import chunk, chunk_utils

logger = logging.getLogger(__name__)

try:
    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.IGESCAFControl import IGESCAFControl_Reader
    from OCP.Interface import Interface_Static
    from OCP.Quantity import Quantity_Color
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.TCollection import TCollection_AsciiString, TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.collections import Sequence_TDF_Label
    from OCP.TDF import TDF_Label, TDF_Tool
    from OCP.TDocStd import TDocStd_Document
    from OCP.TopAbs import TopAbs_Orientation, TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS
    from OCP.XCAFApp import XCAFApp_Application
    from OCP.XCAFDoc import (
        XCAFDoc_ColorTool,
        XCAFDoc_ColorType,
        XCAFDoc_DocumentTool,
        XCAFDoc_ShapeTool,
    )

    #: Surface colour first, then the generic one -- XCAF stores a shape's colour under
    #: either. Built here rather than at module scope so the module still imports, and the
    #: chunk still appears with a 'not installed' readout, on a machine with no OCP.
    _COLOR_TYPES = (XCAFDoc_ColorType.XCAFDoc_ColorSurf, XCAFDoc_ColorType.XCAFDoc_ColorGen)
    _HAS_OCP = True
except ImportError:  # pragma: no cover - exercised only on a machine without the wheel
    _COLOR_TYPES = ()
    _HAS_OCP = False


_PF = chunk.ChunkFlags.ParamFlags
_GF = (
    chunk.ChunkFlags.GroupFlags.COLLAPSABLE
    | chunk.ChunkFlags.GroupFlags.LABELED
    | chunk.ChunkFlags.GroupFlags.FRAMED
)

#: What the file browser will open. STEP is the safe assumption everywhere; IGES comes free
#: because it is the same XCAF reader underneath.
_EXTENSIONS = [".step", ".stp", ".STEP", ".STP", ".iges", ".igs", ".IGES", ".IGS"]

#: Tessellated scenes, keyed by the chunk layer's identifier. See `_fingerprint` -- only the
#: file and the meshing params are in the key, so the cheap params re-author without
#: re-meshing.
_CACHE: dict = {}


@dataclass
class _Proto:
    """One tessellated part, authored once and referenced by every placement of it."""

    name: str
    points: list
    counts: list
    indices: list
    #: (r, g, b) per face, or None where that face carried no colour.
    face_colors: list
    #: Surface distance in metres, one per point. See `_face_uvs`.
    uvs: list = field(default_factory=list)
    uses: int = 0


@dataclass
class _Node:
    """A node of the assembly tree: an Xform, a part placement, or both."""

    name: str
    matrix: Gf.Matrix4d
    proto: int | None = None
    color: tuple | None = None
    children: list = field(default_factory=list)


@dataclass
class _Scene:
    protos: list
    roots: list
    #: Set when the file read, or a part of it, did not work out.
    note: str = ""


@dataclass
class _Settings:
    linear_deflection: float
    relative: bool
    angular_deflection: float
    parallel: bool


# ---------------------------------------------------------------------------
# OpenCASCADE side: file -> _Scene
# ---------------------------------------------------------------------------


def _ascii(ext_string) -> str:
    """An OCCT `TCollection_ExtendedString` as a Python str, whatever the binding offers."""
    for attempt in (
        lambda: TCollection_AsciiString(ext_string).ToCString(),
        lambda: TCollection_AsciiString(ext_string, ord("_")).ToCString(),
        lambda: str(ext_string.ToExtString()),
    ):
        try:
            return attempt()
        except Exception:
            continue
    return ""


def _as_face(shape):
    """Downcast a `TopoDS_Shape` to a face. OCCT 8.0's binding dropped the `_s` suffix."""
    caster = getattr(TopoDS, "Face_s", None) or TopoDS.Face
    return caster(shape)


def _label_name(label) -> str:
    attr = TDataStd_Name()
    try:
        if label.FindAttribute(TDataStd_Name.GetID_s(), attr):
            return _ascii(attr.Get()).strip()
    except Exception:
        pass
    return ""


def _label_entry(label) -> str:
    """The label's `0:1:1:3`-style document path -- a stable identity for a prototype."""
    entry = TCollection_AsciiString()
    TDF_Tool.Entry_s(label, entry)
    return entry.ToCString()


def _shape_color(color_tool, shape) -> tuple | None:
    """The colour attached to a `TopoDS_Shape` -- a solid, a face, or a whole part."""
    col = Quantity_Color()
    for ctype in _COLOR_TYPES:
        if color_tool.GetColor(shape, ctype, col):
            return (col.Red(), col.Green(), col.Blue())
    return None


def _label_color(label) -> tuple | None:
    """The colour attached to a document label.

    A separate function because the instance method takes a `TopoDS_Shape` and *only* a
    shape: handing it a `TDF_Label` raises TypeError rather than returning False, so the
    two lookups cannot share a code path. The label form is the static one.
    """
    col = Quantity_Color()
    for ctype in _COLOR_TYPES:
        if XCAFDoc_ColorTool.GetColor_s(label, ctype, col):
            return (col.Red(), col.Green(), col.Blue())
    return None


def _coloured_faces(color_tool, shape, fallback):
    """Yield every face of *shape* with the colour that applies to it.

    STEP hangs colour wherever the exporting CAD package felt like it, and on a real
    assembly that is usually the **solid**, not the face -- which is why looking only at
    faces finds nothing on a file that plainly has a dozen colours in it. So solids are
    walked first and each one's colour becomes the fallback for its own faces, with a face's
    own colour still winning where it has one. The second pass picks up faces that belong to
    no solid (open shells and loose surfaces), which the first pass cannot reach.
    """
    solids = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_SOLID)
    while solids.More():
        solid = solids.Current()
        solid_colour = _shape_color(color_tool, solid) or fallback
        faces = TopExp_Explorer(solid, TopAbs_ShapeEnum.TopAbs_FACE)
        while faces.More():
            face = _as_face(faces.Current())
            yield face, (_shape_color(color_tool, face) or solid_colour)
            faces.Next()
        solids.Next()

    loose = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_FACE, TopAbs_ShapeEnum.TopAbs_SOLID)
    while loose.More():
        face = _as_face(loose.Current())
        yield face, (_shape_color(color_tool, face) or fallback)
        loose.Next()


def _matrix_of(location) -> Gf.Matrix4d:
    """A `TopLoc_Location` as a USD row-major matrix."""
    trsf = location.Transformation()
    m = Gf.Matrix4d(1.0)
    for row in range(3):
        for col in range(3):
            # OCCT's Value() is 1-based and row-major; USD's Matrix4d is row-vector
            # convention, so the rotation block transposes on the way in.
            #
            # Assigned with the TUPLE subscript. `m[col][row] = x` looks equivalent and is
            # not: Gf.Matrix4d.__getitem__ with a single index hands back a *copy* of the
            # row, so that form writes into a temporary and is silently discarded, leaving
            # the identity. Nothing raises, and a CAD file whose components all sit at the
            # identity -- which is most of them, since many exporters bake each placement
            # into its part's geometry -- reads back perfectly, so the bug hides until a
            # file arrives that does use component placements.
            m[col, row] = trsf.Value(row + 1, col + 1)
        m[3, row] = trsf.Value(row + 1, 4)
    return m


def _open_document(path: str):
    """Read *path* into an XCAF document and return its shape and colour tools."""
    # GetApplication_s is what registers XCAF's document drivers; without it the transfer
    # below reports success into a document that holds no shapes. The format has to be one
    # OCCT actually knows -- the legacy "MDTV-XCAF" is not registered in 8.0, and a document
    # made with it reads back empty rather than failing. NewDocument hands the real document
    # back in a tuple rather than filling the one it is given, so take what it returns.
    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("BinXCAF"))
    returned = app.NewDocument(TCollection_ExtendedString("BinXCAF"), doc)
    if isinstance(returned, tuple) and returned:
        doc = returned[0]

    suffix = os.path.splitext(path)[1].lower()
    if suffix in (".iges", ".igs"):
        reader = IGESCAFControl_Reader()
    else:
        reader = STEPCAFControl_Reader()
        # Metres out, whatever the file declares in, so everything downstream -- the absolute
        # deflection included -- is in one known unit. This has to come *after* the reader is
        # constructed: the static does not exist until the STEP reader's class initialises,
        # and setting it before returns False and silently leaves the default of millimetres.
        Interface_Static.SetCVal_s("xstep.cascade.unit", "M")

    reader.SetColorMode(True)
    reader.SetNameMode(True)
    reader.SetLayerMode(True)

    if reader.ReadFile(path) != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise RuntimeError(f"OpenCASCADE could not read {os.path.basename(path)}")
    if not reader.Transfer(doc):
        raise RuntimeError(f"nothing transferred out of {os.path.basename(path)}")

    main = doc.Main()
    return XCAFDoc_DocumentTool.ShapeTool_s(main), XCAFDoc_DocumentTool.ColorTool_s(main)


def _face_uvs(triangulation, points, indices, base, node_count, first_triangle) -> list:
    """This face's UVs, rescaled so one UV unit is one metre of surface.

    OCCT hands back each node's position in the *surface's own* parameter domain, which is
    free but not directly usable: a cylinder's u is an angle in radians while a plane's is a
    length in metres, so on one real part the metres-per-UV-unit varies by two orders of
    magnitude (measured: 204x across 637 faces). A tiling texture mapped through that shows
    up 200 times coarser on one face than its neighbour.

    So each face is rescaled by its own ratio of 3D area to UV area, which is the average
    metres-per-UV-unit over the face, and shifted to start at its own origin. Every face then
    carries surface distance in metres and a tiling material has consistent texel density
    across the whole part, which is what makes the UVs worth having.

    Two things this deliberately is not. It is not an atlas: each face is an independent
    island in an unbounded space and islands overlap, which is exactly right for a repeating
    material and exactly wrong for baking, where a packer is needed instead. And it does not
    make textures continuous ACROSS a face boundary -- the faces are separate islands, so a
    directional pattern can step at an edge. For the brushed metals, castings and noise that
    CAD parts are shown in, neither matters.

    A face whose surface has no parameterisation to offer comes back at (0, 0); nothing
    downstream has to care, because the array still lines up one-to-one with the points.
    """
    if not triangulation.HasUVNodes():
        return [Gf.Vec2f(0.0, 0.0)] * node_count

    raw = [triangulation.UVNode(i) for i in range(1, node_count + 1)]
    area_3d = 0.0
    area_uv = 0.0
    for triangle in range(first_triangle, len(indices) // 3):
        a, b, c = indices[triangle * 3], indices[triangle * 3 + 1], indices[triangle * 3 + 2]
        p0, p1, p2 = points[a], points[b], points[c]
        area_3d += Gf.Cross(p1 - p0, p2 - p0).GetLength() * 0.5
        u0, u1, u2 = raw[a - base], raw[b - base], raw[c - base]
        area_uv += abs(
            (u1.X() - u0.X()) * (u2.Y() - u0.Y()) - (u2.X() - u0.X()) * (u1.Y() - u0.Y())
        ) * 0.5

    scale = math.sqrt(area_3d / area_uv) if area_uv > 1e-20 and area_3d > 1e-20 else 1.0
    min_u = min(node.X() for node in raw)
    min_v = min(node.Y() for node in raw)
    return [Gf.Vec2f((node.X() - min_u) * scale, (node.Y() - min_v) * scale) for node in raw]


def _tessellate(shape_tool, color_tool, label, settings: _Settings) -> _Proto | None:
    """Mesh one part in its own local space, faces kept as separate vertex islands."""
    shape = shape_tool.GetShape_s(label)
    if shape is None or shape.IsNull():
        return None

    BRepMesh_IncrementalMesh(
        shape,
        settings.linear_deflection,
        settings.relative,
        math.radians(settings.angular_deflection),
        settings.parallel,
    )

    points: list = []
    counts: list = []
    indices: list = []
    face_colors: list = []
    uvs: list = []

    part_colour = _label_color(label) or _shape_color(color_tool, shape)

    for face, colour in _coloured_faces(color_tool, shape, part_colour):
        location = TopLoc_Location()
        triangulation = BRep_Tool.Triangulation_s(face, location)
        if triangulation is None:
            continue

        trsf = location.Transformation()
        base = len(points)
        node_count = triangulation.NbNodes()
        for i in range(1, node_count + 1):
            node = triangulation.Node(i).Transformed(trsf)
            points.append(Gf.Vec3f(node.X(), node.Y(), node.Z()))

        reversed_face = face.Orientation() == TopAbs_Orientation.TopAbs_REVERSED
        first_triangle = len(counts)
        for i in range(1, triangulation.NbTriangles() + 1):
            tri = triangulation.Triangle(i)
            a, b, c = tri.Value(1), tri.Value(2), tri.Value(3)
            if reversed_face:
                b, c = c, b
            indices.extend((base + a - 1, base + b - 1, base + c - 1))
            counts.append(3)
            face_colors.append(colour)

        uvs.extend(_face_uvs(triangulation, points, indices, base, node_count, first_triangle))

    if not counts:
        return None

    name = _label_name(label) or "part"
    return _Proto(
        name=name, points=points, counts=counts, indices=indices, face_colors=face_colors, uvs=uvs
    )


def _geometry_digest(proto: _Proto) -> str:
    """A content hash of a tessellated part: same shape, same colours, same digest.

    Coordinates are rounded before hashing. Two occurrences of one part tessellate through
    identical code from identical B-rep and do come out bit-identical, but rounding costs
    nothing and stops a last-bit difference from quietly splitting a prototype in two.
    """
    hasher = hashlib.blake2b(digest_size=16)
    hasher.update(repr(proto.counts).encode())
    hasher.update(repr(proto.indices).encode())
    for point in proto.points:
        hasher.update(b"%.9f,%.9f,%.9f;" % (point[0], point[1], point[2]))
    for colour in proto.face_colors:
        hasher.update(b"-" if colour is None else b"%.5f,%.5f,%.5f;" % colour)
    return hasher.hexdigest()


def _extract(path: str, settings: _Settings) -> _Scene:
    """Walk the XCAF assembly tree, tessellating each distinct part exactly once."""
    shape_tool, color_tool = _open_document(path)

    protos: list = []
    by_entry: dict = {}
    by_geometry: dict = {}

    def proto_for(label) -> int | None:
        entry = _label_entry(label)
        if entry in by_entry:
            return by_entry[entry]
        proto = _tessellate(shape_tool, color_tool, label, settings)
        index = None
        if proto is not None:
            # Two routes to the same prototype. XCAF's own identity catches a part the file
            # genuinely *references* twice; the geometry digest catches the far commoner
            # case of a part exported as a separate PRODUCT per occurrence, which is what a
            # bolt used sixteen times usually looks like coming out of a mid-range CAD
            # package. Without the second one, "instance repeated parts" finds nothing to do
            # on most real files. Colour is part of the digest, so two identical shapes
            # finished differently stay separate prototypes.
            digest = _geometry_digest(proto)
            index = by_geometry.get(digest)
            if index is None:
                index = len(protos)
                protos.append(proto)
                by_geometry[digest] = index
        by_entry[entry] = index
        return index

    def walk(label, name: str, matrix: Gf.Matrix4d, instance_label=None) -> _Node | None:
        node = _Node(name=name, matrix=matrix, color=None)
        if instance_label is not None:
            # A component may override the colour of the part it places; that override is
            # what makes an otherwise identical part a different-looking one.
            node.color = _label_color(instance_label)
        if node.color is None:
            node.color = _label_color(label)

        if XCAFDoc_ShapeTool.IsAssembly_s(label):
            components = Sequence_TDF_Label()
            XCAFDoc_ShapeTool.GetComponents_s(label, components)
            for i in range(1, components.Length() + 1):
                component = components.Value(i)
                target = component
                if XCAFDoc_ShapeTool.IsReference_s(component):
                    referred = TDF_Label()
                    XCAFDoc_ShapeTool.GetReferredShape_s(component, referred)
                    target = referred
                child_name = _label_name(component) or _label_name(target) or f"part_{i}"
                child = walk(
                    target,
                    child_name,
                    _matrix_of(XCAFDoc_ShapeTool.GetLocation_s(component)),
                    instance_label=component,
                )
                if child is not None:
                    node.children.append(child)
            return node if node.children else None

        node.proto = proto_for(label)
        if node.proto is None:
            return None
        protos[node.proto].uses += 1
        return node

    free_shapes = Sequence_TDF_Label()
    shape_tool.GetFreeShapes(free_shapes)
    roots: list = []
    for i in range(1, free_shapes.Length() + 1):
        label = free_shapes.Value(i)
        name = _label_name(label) or f"root_{i}"
        root = walk(label, name, _matrix_of(XCAFDoc_ShapeTool.GetLocation_s(label)))
        if root is not None:
            roots.append(root)

    note = "" if roots else "the file read, but held no meshable solids"
    return _Scene(protos=protos, roots=roots, note=note)


# ---------------------------------------------------------------------------
# USD side: _Scene -> prims
# ---------------------------------------------------------------------------


def _unique(name: str, taken: set) -> str:
    base = Tf.MakeValidIdentifier(name) or "part"
    candidate = base
    suffix = 1
    while candidate in taken:
        suffix += 1
        candidate = f"{base}_{suffix}"
    taken.add(candidate)
    return candidate


def prototype_scope(stage, path: Sdf.Path):
    """The Scope the shared parts live under, marked so it is never drawn.

    The invisibility is authored HERE, on the scope, and not on the prototypes themselves.
    A reference composes the opinions at and below the prim it targets, so an opinion on
    this scope -- an ancestor of every prototype -- is not carried across to the placements;
    they take their visibility from their own ancestors and render normally. Put it on a
    prototype instead and every placement of it would inherit the invisibility.
    """
    scope = UsdGeom.Scope.Define(stage, path)
    UsdGeom.Imageable(scope).CreateVisibilityAttr(UsdGeom.Tokens.invisible)
    return scope


def _author_proto(stage, path: Sdf.Path, proto: _Proto, scale: float, texture_size: float = 0.0) -> None:
    """One Xform holding one Mesh -- the shared half of every placement.

    An ordinary `def`, not a `class`. A class prim is the traditional way to hold something
    meant only to be referenced, but everything beneath one is abstract, and abstract prims
    are filtered out of the default child predicate -- so a prototype's own GeomSubsets
    become invisible to `UsdGeom.Subset.GetAllGeomSubsets` and to anything else that walks
    children normally. Keeping them concrete and hiding the scope above them costs nothing
    and leaves the prototypes inspectable with the ordinary APIs.
    """
    UsdGeom.Xform.Define(stage, path)
    mesh = UsdGeom.Mesh.Define(stage, path.AppendChild("mesh"))

    points = Vt.Vec3fArray([Gf.Vec3f(p[0] * scale, p[1] * scale, p[2] * scale) for p in proto.points])
    mesh.CreatePointsAttr(points)
    mesh.CreateFaceVertexCountsAttr(Vt.IntArray(proto.counts))
    mesh.CreateFaceVertexIndicesAttr(Vt.IntArray(proto.indices))
    # Without this the default catmullClark would round off every edge the CAD kernel was
    # asked to keep sharp.
    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
    mesh.CreateExtentAttr(UsdGeom.PointBased(mesh).ComputeExtent(points))

    primvars = UsdGeom.PrimvarsAPI(mesh.GetPrim())

    # `vertex`, not `faceVarying`: points are never welded across face boundaries, so every
    # point already belongs to exactly one face and one UV. That is half the data of a
    # faceVarying primvar and needs no index array.
    if texture_size > 0.0 and len(proto.uvs) == len(proto.points):
        repeats = 1.0 / texture_size
        st = primvars.CreatePrimvar(
            "st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.vertex
        )
        st.Set(Vt.Vec2fArray([Gf.Vec2f(uv[0] * repeats, uv[1] * repeats) for uv in proto.uvs]))

    distinct = {c for c in proto.face_colors if c is not None}
    if len(distinct) == 1:
        colour = next(iter(distinct))
        pv = primvars.CreatePrimvar("displayColor", Sdf.ValueTypeNames.Color3fArray, UsdGeom.Tokens.constant)
        pv.Set(Vt.Vec3fArray([Gf.Vec3f(*colour)]))
    elif len(distinct) > 1:
        fallback = Gf.Vec3f(0.5, 0.5, 0.5)
        pv = primvars.CreatePrimvar("displayColor", Sdf.ValueTypeNames.Color3fArray, UsdGeom.Tokens.uniform)
        pv.Set(Vt.Vec3fArray([Gf.Vec3f(*c) if c is not None else fallback for c in proto.face_colors]))

    # Colour groups as bindable subsets: in STEP the colour IS the material assignment, so
    # these are the handles a shading library binds against later.
    if len(distinct) > 1:
        taken: set = set()
        for colour in sorted(distinct):
            faces = [i for i, c in enumerate(proto.face_colors) if c == colour]
            name = _unique("mat_" + "".join(f"{int(round(c * 255)):02x}" for c in colour), taken)
            UsdGeom.Subset.CreateGeomSubset(
                mesh, name, UsdGeom.Tokens.face, Vt.IntArray(faces), familyName="materialBind"
            )


def _author_node(stage, parent: Sdf.Path, node: _Node, proto_paths: list, settings_instanceable: bool,
                 scale: float, taken: set, counters: dict) -> None:
    path = parent.AppendChild(_unique(node.name, taken))
    xform = UsdGeom.Xform.Define(stage, path)

    matrix = Gf.Matrix4d(node.matrix)
    translation = matrix.ExtractTranslation()
    matrix.SetTranslateOnly(translation * scale)
    if not Gf.IsClose(matrix, Gf.Matrix4d(1.0), 1e-12):
        xform.AddTransformOp().Set(matrix)

    if node.proto is not None:
        prim = xform.GetPrim()
        prim.GetReferences().AddInternalReference(proto_paths[node.proto])
        counters["parts"] += 1
        # Instancing only earns its keep where the part is placed more than once, and it
        # costs the ability to override anything inside -- including the per-placement colour
        # a component label may carry.
        shared = counters["uses"][node.proto] > 1
        if settings_instanceable and shared and node.color is None:
            prim.SetInstanceable(True)
        elif node.color is not None:
            pv = UsdGeom.PrimvarsAPI(prim).CreatePrimvar(
                "displayColor", Sdf.ValueTypeNames.Color3fArray, UsdGeom.Tokens.constant
            )
            pv.Set(Vt.Vec3fArray([Gf.Vec3f(*node.color)]))

    child_taken: set = set()
    for child in node.children:
        _author_node(stage, path, child, proto_paths, settings_instanceable, scale, child_taken, counters)


def _flatten(node: _Node, matrix: Gf.Matrix4d, out: list) -> None:
    combined = node.matrix * matrix
    if node.proto is not None:
        out.append(_Node(name=node.name, matrix=combined, proto=node.proto, color=node.color))
    for child in node.children:
        _flatten(child, combined, out)


# ---------------------------------------------------------------------------
# Chunk
# ---------------------------------------------------------------------------


def _fingerprint(path: str, settings: _Settings) -> tuple:
    try:
        stat = os.stat(path)
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = (0, 0)
    return (
        path,
        stamp,
        settings.linear_deflection,
        settings.relative,
        settings.angular_deflection,
        settings.parallel,
    )


def _report(node, note: str, parts: int = 0, triangles: int = 0) -> None:
    if readout := node.get_param("readout"):
        readout.value = note
    if out_parts := node.get_param("out_parts"):
        out_parts.value = parts
    if out_tris := node.get_param("out_triangles"):
        out_tris.value = triangles


def file_changed(owner, notice, sender):
    """Suggest a prim path from the file name, the way the Reference chunk does."""
    param = notice.data
    _chunk = param.get_chunk()
    if not _chunk:
        return

    file_param = _chunk.get_param("file_path")
    cad_path = file_param.value if file_param else None
    if not cad_path:
        return

    primpath_param = _chunk.get_param("primpath_location")
    if not primpath_param or chunk_utils.has_expression(primpath_param.raw_value or ""):
        return

    asset_name = Tf.MakeValidIdentifier(os.path.splitext(os.path.basename(cad_path))[0])
    primpath_param.value = chunk_utils.get_unique_path(param.get_stage(), f"/{asset_name}")


def execute(node):
    stage = node.get_stage()
    if not stage:
        return

    layer = node.get_edit_context()
    if not layer:
        return
    if manager := chunk.get_layer_manager():
        manager.clear_layer(layer.identifier)

    is_f = chunk.is_param_t(float)
    is_b = chunk.is_param_t(bool)

    cad_path = node.get_param("file_path").value
    if not cad_path:
        _report(node, "pick a STEP or IGES file")
        return
    if not _HAS_OCP:
        _report(node, "cadquery-ocp is not installed — pip install cadquery-ocp")
        return
    if not os.path.isfile(cad_path):
        _report(node, f"no such file: {os.path.basename(cad_path)}")
        return

    settings = _Settings(
        linear_deflection=node.get_param("linear_deflection", is_f).value,
        relative=node.get_param("relative_deflection", is_b).value,
        angular_deflection=node.get_param("angular_deflection", is_f).value,
        parallel=node.get_param("parallel", is_b).value,
    )

    fingerprint = _fingerprint(cad_path, settings)
    cached = _CACHE.get(layer.identifier)
    if cached is not None and cached[0] == fingerprint:
        scene = cached[1]
    else:
        try:
            scene = _extract(cad_path, settings)
        except Exception as exc:
            logger.exception("CAD import failed")
            _report(node, f"read failed: {exc}")
            return
        if len(_CACHE) > 32:
            # A deleted chunk leaves its tessellation behind, and a CAD scene is not small.
            _CACHE.clear()
        _CACHE[layer.identifier] = (fingerprint, scene)

    if not scene.roots:
        _report(node, scene.note or "nothing to import")
        return

    raw_path = node.get_param("primpath_location").value
    if not raw_path:
        _report(node, "set a location to import to")
        return

    scale = node.get_param("extra_scale", is_f).value / UsdGeom.GetStageMetersPerUnit(stage)
    texture_size = node.get_param("texture_size", is_f).value if node.get_param("generate_uvs", is_b).value else 0.0
    keep_hierarchy = node.get_param("keep_hierarchy", is_b).value
    instance_parts = node.get_param("instance_parts", is_b).value

    counters = {"parts": 0, "uses": [p.uses for p in scene.protos]}

    with Usd.EditContext(stage, layer):
        with chunk_utils.author_in_variant_context(stage, raw_path) as target_path:
            root = chunk_utils.define_with_ancestors(stage, target_path, "Xform")
            root_path = root.GetPath()

            proto_scope = root_path.AppendChild("Prototypes")
            prototype_scope(stage, proto_scope)
            proto_paths = []
            taken: set = set()
            for proto in scene.protos:
                proto_path = proto_scope.AppendChild(_unique(proto.name, taken))
                _author_proto(stage, proto_path, proto, scale, texture_size)
                proto_paths.append(proto_path)

            taken = set()
            if keep_hierarchy:
                for tree in scene.roots:
                    _author_node(stage, root_path, tree, proto_paths, instance_parts, scale, taken, counters)
            else:
                flat: list = []
                for tree in scene.roots:
                    _flatten(tree, Gf.Matrix4d(1.0), flat)
                for leaf in flat:
                    _author_node(stage, root_path, leaf, proto_paths, instance_parts, scale, taken, counters)

    triangles = sum(len(p.counts) * p.uses for p in scene.protos)
    unique = sum(len(p.counts) for p in scene.protos)
    shared = f", {unique:,} unique" if unique != triangles else ""
    _report(
        node,
        f"{counters['parts']:,} parts · {len(scene.protos):,} distinct · {triangles:,} tris{shared}",
        parts=counters["parts"],
        triangles=triangles,
    )


def main():
    cad_chunk = chunk.Chunk("CAD Import")

    file_browser = chunk.FileBrowser("file_path", "CAD File", extensions=_EXTENSIONS)
    # Open, never save: this chunk only ever reads the file it is pointed at.
    file_browser.set_flag(_PF.FILE_EXISTING)
    file_browser.add_callback(file_changed)
    cad_chunk.add_param(file_browser)

    cad_chunk.add_param(
        chunk.LineEdit(
            "primpath_location",
            "Insert to location",
            tooltip="Where the assembly's root Xform goes. Filled in from the file name\n"
            "when you pick one.",
        )
    )

    cad_chunk.add_param(
        chunk.Label(
            "readout",
            "nothing imported yet",
            default_value="nothing imported yet",
            tooltip="What the last import produced.",
        )
    )

    tessellation = chunk.Group("Tessellation", flags=_GF)
    tessellation.add_params(
        [
            chunk.Slider(
                "linear_deflection",
                "Linear Deflection",
                min_value=0.0001,
                max_value=0.5,
                default_value=0.01,
                tooltip="How far the triangles may sag away from the true B-rep surface —\n"
                "the tessellation rate. Smaller is finer and slower.\n"
                "Relative: a fraction of each edge's own size, so small parts in a\n"
                "big assembly still get enough triangles. Absolute: metres.",
            ),
            chunk.Checkbox(
                "relative_deflection",
                "Relative",
                default_value=True,
                tooltip="Scale the deflection by each edge's size instead of taking it as an\n"
                "absolute distance. Leave this on for an assembly whose parts differ\n"
                "wildly in size; turn it off when you want one guaranteed tolerance.",
            ),
            chunk.Slider(
                "angular_deflection",
                "Angular Deflection",
                min_value=1.0,
                max_value=60.0,
                default_value=20.0,
                tooltip="Largest angle, in degrees, between neighbouring facets around a\n"
                "curve. This is what stops a small hole from becoming a hexagon\n"
                "however tight the linear deflection is.",
            ),
            chunk.Checkbox(
                "parallel",
                "Parallel Meshing",
                default_value=True,
                tooltip="Mesh faces on all cores. Off is slower but deterministic in its\n"
                "memory use, which matters on a very large assembly.",
            ),
        ]
    )
    cad_chunk.add_param(tessellation)

    structure = chunk.Group("Structure", flags=_GF)
    structure.add_params(
        [
            chunk.Checkbox(
                "keep_hierarchy",
                "Keep Assembly Hierarchy",
                default_value=True,
                tooltip="Mirror the CAD assembly tree as nested Xforms. Off flattens every\n"
                "part directly under the root with its transform baked into place —\n"
                "simpler to select, but the structure is gone.",
            ),
            chunk.Checkbox(
                "instance_parts",
                "Instance Repeated Parts",
                default_value=True,
                tooltip="Mark a placement instanceable when the same part appears more than\n"
                "once. A hundred identical fasteners then cost one mesh. Note that\n"
                "direct material binding does not compose inside an instance — bind\n"
                "through a collection instead.",
            ),
            chunk.Checkbox(
                "generate_uvs",
                "Generate UVs",
                default_value=True,
                tooltip="Author an `st` primvar from each B-rep face's own surface\n"
                "parameterisation, rescaled so texel density is even across the part.\n"
                "Needed by any material with texture maps in it -- without UVs a\n"
                "tiling MaterialX surface renders as a flat colour.",
            ),
            chunk.Slider(
                "texture_size",
                "Texture Size (m)",
                min_value=0.001,
                max_value=10.0,
                default_value=0.25,
                tooltip="How much of the real part one tile of the texture covers, in\n"
                "metres. 0.25 means a repeating material tiles every 250 mm. This is\n"
                "the knob that decides whether a brushed-metal finish reads as\n"
                "machining marks or as corduroy.",
            ),
            chunk.Slider(
                "extra_scale",
                "Extra Scale",
                min_value=0.0001,
                max_value=1000.0,
                default_value=1.0,
                tooltip="Multiplier applied on top of the file's declared units, which are\n"
                "already converted to the stage's units. Only needed when a file\n"
                "lies about what it is in — which, in CAD, happens.",
            ),
        ]
    )
    cad_chunk.add_param(structure)

    out_parts = chunk.Slider(
        "out_parts",
        "Parts Out",
        is_int=True,
        max_value=100_000_000,
        flags=_PF.OUTPUT,
        tooltip="Read-only. Part placements the last import authored.",
    )
    out_parts.set_state(chunk.ChunkFlags.State.DISABLED)
    cad_chunk.add_param(out_parts)

    out_triangles = chunk.Slider(
        "out_triangles",
        "Triangles Out",
        is_int=True,
        max_value=1_000_000_000,
        flags=_PF.OUTPUT,
        tooltip="Read-only. Triangles in the imported assembly, counting every placement.",
    )
    out_triangles.set_state(chunk.ChunkFlags.State.DISABLED)
    cad_chunk.add_param(out_triangles)

    return cad_chunk


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.MODELLING,
        requirements=["cadquery-ocp>=7.8,<9"],
        description=(
            "Imports a STEP or IGES assembly as USD, through OpenCASCADE's XCAF reader.\n\n"
            "Keeps the three things a converter usually drops: the assembly hierarchy (as\n"
            "nested Xforms), part instancing (a repeated part is tessellated once into a\n"
            "class prim and referenced), and per-face colour (as GeomSubsets in the\n"
            "materialBind family, ready for a material library to bind against).\n\n"
            "Points are not welded across B-rep face boundaries, so renderer-computed\n"
            "normals smooth within a face and crease between them — curved surfaces stay\n"
            "smooth and machined edges stay sharp with no authored normals."
        ),
    )
