"""Fracture chunk -- pre-break a model (or procedural rocks) into a tree of pieces.

The RBD Solver breaks a body by swapping it for the pieces it was cut into, so every piece
a simulation can produce has to exist before it starts. This chunk makes them, for any
closed mesh:

* each piece is cut in two by a plane, recursively, down *Generations* levels. The cut
  runs across the piece's longest axis, tilted by *Break Angle* -- how rock and most brittle
  things actually fail, and it keeps pieces from turning into ever thinner slivers;
* a half that falls apart into separate islands (cut a U across its arms) becomes one
  child per island, so a node has two *or more* children;
* the two halves of a cut tile their parent exactly, so pieces always add up to the whole:
  a break neither makes nor loses material.

Cutting is done by `manifold3d`, which keeps both halves closed and says which triangles
came from a cut. That is what the surfacing hangs on:

* **UVs** -- the model's own UVs on its outside, carried through every cut; a planar
  projection on the cut faces, taken in the model's frame so the grain runs on across a
  crack instead of restarting in every piece. Procedural rocks get the projection
  everywhere.
* **Inside and outside** -- a ``GeomSubset`` each, so the fracture surface can take its own
  material (Inside Material) while the outside keeps the model's.

Output is a PointInstancer: one prototype per piece and, to start with, one instance per
source placed exactly where it was, with the source hidden. Hand that instancer to the RBD
Solver and turn on Breakable. *Show Generation* starts it pre-broken instead: the
instances become that generation's pieces, pulled apart by *Explode*.

The tree lives on the prototypes as ``fracture:`` attributes -- ``tree``, ``node``,
``children``, ``generation``, ``offset`` (centre minus the parent's centre, in the tree's
frame) and ``volume`` -- so anything can read it back, and anything that writes them can
feed the solver.

A model that is not a closed surface cannot be cut cleanly; it is fractured as its convex
hull instead, with a warning. MuJoCo collides every piece as its convex hull either way,
so a piece of a concave model collides a little fuller than it looks.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import numpy as np
from pxr import Gf, Sdf, Tf, Usd, UsdGeom, UsdShade, Vt

from usd_chunks import chunk, chunk_stage

try:
    import manifold3d as _mf

    _HAS_MANIFOLD = True
except ImportError:  # pragma: no cover - exercised only on a machine without the wheel
    _mf = None
    _HAS_MANIFOLD = False

logger = logging.getLogger(__name__)

NS = "fracture:"

SOURCE_ROCKS = "Procedural Rocks"
SOURCE_PRIMS = "Selected Meshes"

# Rock colours, one per rock: greys and warm browns, so a pile reads as many rocks.
_PALETTE = np.array(
    [
        (0.30, 0.29, 0.27), (0.36, 0.33, 0.30), (0.25, 0.24, 0.23), (0.40, 0.35, 0.29),
        (0.33, 0.29, 0.25), (0.22, 0.21, 0.20), (0.42, 0.39, 0.35), (0.31, 0.26, 0.22),
    ]
)


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


class Piece:
    """One node of a fracture tree."""

    __slots__ = ("tree", "node", "parent", "children", "generation", "solid", "centre", "volume", "offset")

    def __init__(self, tree, node, parent, generation, solid, centre, volume):
        self.tree = tree
        self.node = node
        self.parent = parent
        self.children: list[int] = []
        self.generation = generation
        #: The piece as a closed manifold, in the tree's frame (world, for a model).
        self.solid = solid
        self.centre = centre
        self.volume = volume
        #: Centre minus the parent's centre; zero for a root.
        self.offset = np.zeros(3)


class Tree:
    """Every piece one source breaks into, plus what its surface needs."""

    def __init__(self, index, name, pieces, surface_id, has_uv, colour=None, material=None):
        self.index = index
        self.name = name
        self.pieces: list[Piece] = pieces
        #: manifold3d's original id of the source surface: a triangle whose run carries any
        #: other id was made by a cut, and is inside.
        self.surface_id = surface_id
        self.has_uv = has_uv
        self.colour = colour
        self.material = material


def _centroid(verts: np.ndarray, tris: np.ndarray) -> tuple[float, np.ndarray]:
    """Volume and centre of mass of a closed triangle mesh (tetrahedra from the origin)."""
    a, b, c = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
    vols = np.einsum("ij,ij->i", a, np.cross(b, c)) / 6.0
    total = float(vols.sum())
    if abs(total) < 1e-12:
        return 0.0, verts.mean(axis=0)
    return total, ((a + b + c) / 4.0 * vols[:, None]).sum(axis=0) / total


def _solid_centre(solid) -> tuple[float, np.ndarray]:
    mesh = solid.to_mesh()
    v = np.asarray(mesh.vert_properties, dtype=np.float64)[:, :3]
    t = np.asarray(mesh.tri_verts, dtype=np.int64)
    return _centroid(v, t)


def random_rotation(rng: np.random.Generator) -> np.ndarray:
    q = rng.normal(size=4)
    q /= np.linalg.norm(q)
    w, x, y, z = q
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )


def random_rock_points(rng, size: float, detail: int = 28, roughness: float = 0.3, flatness: float = 0.35):
    """Points whose convex hull is one random rock about *size* across, centred near 0."""
    dirs = rng.normal(size=(max(8, detail), 3))
    dirs /= np.linalg.norm(dirs, axis=1, keepdims=True)
    radii = 1.0 + roughness * (rng.random(len(dirs)) * 2.0 - 1.0)
    # Rock is rarely round: stretch one axis, squash another.
    axes = np.array([1.0, 1.0 - flatness * rng.random(), 1.0 - flatness * (0.5 + 0.5 * rng.random())])
    pts = (dirs * radii[:, None] * axes) @ random_rotation(rng).T
    solid = _mf.Manifold.hull_points(pts)
    vol, centre = _solid_centre(solid)
    # Scaled to the equivalent-sphere diameter, so rocks of one size weigh about the same
    # whatever their proportions.
    diameter = (6.0 * abs(vol) / np.pi) ** (1.0 / 3.0)
    return (pts - centre) * (size / diameter)


def chunkiness(solid) -> float:
    """How far from a sliver a piece is: its smallest spread over its largest, 0..1."""
    v = np.asarray(solid.to_mesh().vert_properties, dtype=np.float64)[:, :3]
    if len(v) < 4:
        return 0.0
    sv = np.linalg.svd(v - v.mean(axis=0), compute_uv=False)
    return float(sv[-1] / max(sv[0], 1e-12))


def _cut(solid, rng, jaggedness: float, min_fraction: float, candidates: int = 1, attempts: int = 10):
    """Split *solid* by a plane into islands. Returns [(solid, volume, centre)], or None.

    The plane crosses the longest axis, off-centre by up to an eighth of the length so the
    halves come out unequal. With *candidates* > 1 that many valid planes are tried and the
    one whose worst half is chunkiest wins: cut after cut, a random plane leaves wedges and
    needles, and those read as shards rather than gravel however they are shaded.

    An island below *min_fraction* of the parent is a sliver -- unstable to simulate, and
    invisible anyway -- so a cut that makes one does not count, unless nothing else works.
    """
    mesh = solid.to_mesh()
    verts = np.asarray(mesh.vert_properties, dtype=np.float64)[:, :3]
    vol, centre = _centroid(verts, np.asarray(mesh.tri_verts, dtype=np.int64))
    centred = verts - verts.mean(axis=0)
    _, _, vt = np.linalg.svd(centred, full_matrices=False)
    extent = float(np.ptp(centred @ vt[0]))
    fallback = None
    best = None
    tries = max(attempts, candidates * 3)
    found = 0
    for attempt in range(tries):
        wobble = rng.normal(size=3)
        normal = vt[0] + jaggedness * 1.5 * wobble / max(np.linalg.norm(wobble), 1e-9)
        normal /= np.linalg.norm(normal)
        shift = extent * 0.12 * (rng.random() * 2.0 - 1.0)
        halves = solid.split_by_plane(tuple(map(float, normal)), float(normal @ centre + shift))
        islands = []
        for half in halves:
            if half.is_empty():
                continue
            for isl in half.decompose():
                v, c = _solid_centre(isl)
                if v > 0.0:
                    islands.append((isl, v, c))
        if len(islands) < 2:
            continue
        smallest = min(i[1] for i in islands) / max(vol, 1e-12)
        if smallest < min_fraction:
            if fallback is None or smallest > fallback[0]:
                fallback = (smallest, islands)
            continue
        if candidates <= 1:
            return islands
        score = min(chunkiness(i[0]) for i in islands)
        if best is None or score > best[0]:
            best = (score, islands)
        found += 1
        if found >= candidates:
            break
    if best is not None:
        return best[1]
    return fallback[1] if fallback is not None else None


def fracture(tree_index: int, root, rng, generations: int, jaggedness: float,
             min_fraction: float = 0.04, candidates: int = 1) -> list[Piece]:
    """Cut *root* down *generations* levels. Pieces are listed parents-first."""
    vol, centre = _solid_centre(root)
    pieces = [Piece(tree_index, 0, -1, 0, root, centre, vol)]
    frontier = [0]
    for gen in range(generations):
        nxt = []
        for n in frontier:
            parent = pieces[n]
            islands = _cut(parent.solid, rng, jaggedness, min_fraction, candidates)
            if islands is None:
                continue
            for solid, v, c in islands:
                child = Piece(tree_index, len(pieces), n, gen + 1, solid, c, v)
                child.offset = c - parent.centre
                parent.children.append(child.node)
                pieces.append(child)
                nxt.append(child.node)
        frontier = nxt
    return pieces


def _box_uv(p: np.ndarray, n: np.ndarray, scale: float) -> np.ndarray:
    """Planar UVs, projected along each face's dominant axis (a box projection)."""
    axis = np.argmax(np.abs(n), axis=1)
    u_axis = np.where(axis == 0, 2, 0)
    v_axis = np.where(axis == 1, 2, 1)
    idx = np.arange(len(p))
    return np.stack([p[idx, u_axis], p[idx, v_axis]], axis=1) * scale


def bevelled(solid, bevel: float, detail: int):
    """*solid* with its edges rounded, for display.

    Edges between near-coplanar facets (under 45 degrees -- the rock's own surface) are
    smoothed right out; sharper ones -- the breaks, the rock's corners -- get a fillet whose
    size is *bevel* (0 = none, 1 = fully round). *detail* is how finely the rounded surface
    is subdivided: every edge is split that many times, so triangles grow by its square.
    """
    if bevel <= 0.0:
        return solid
    rounded = solid.smooth_out(45.0, float(min(1.0, bevel))).refine(max(2, int(detail)))
    # Smooth shading to go with it: flat-shading hundreds of small triangles reads as lumps.
    # Appended after any UV / normal channels the source carries, so those survive.
    return rounded.calculate_normals(rounded.num_prop(), 60.0)


def piece_surface(tree: Tree, piece: Piece, root_centre: np.ndarray, uv_scale: float, solid=None):
    """Mesh arrays for one piece, points about its own centre.

    Returns (points, face counts, face indices, faceVarying UVs, faceVarying normals,
    inside face indices). Faces are flat-shaded on the cuts and on procedural rock, and
    keep the source's smooth normals on a model's outside.
    """
    shape = solid or piece.solid
    mesh = shape.to_mesh()
    props = np.asarray(mesh.vert_properties, dtype=np.float64)
    # A bevelled shape carries its own smooth normals in its last three channels.
    smooth = solid is not None and solid is not piece.solid and props.shape[1] >= 6
    tris = np.asarray(mesh.tri_verts, dtype=np.int64)
    pos = props[:, :3]
    run_ids = np.asarray(mesh.run_original_id)
    run_idx = np.asarray(mesh.run_index)
    tri_run = np.searchsorted(run_idx, np.arange(len(tris)) * 3, side="right") - 1
    inside = run_ids[np.clip(tri_run, 0, len(run_ids) - 1)] != tree.surface_id

    corners = tris.reshape(-1)
    p = pos[tris]
    fn = np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0])
    fn /= np.maximum(np.linalg.norm(fn, axis=1, keepdims=True), 1e-12)
    corner_fn = np.repeat(fn, 3, axis=0)
    # Box projection in the tree's frame: the same world point gets the same UV in every
    # piece, so a texture runs on across a crack.
    uv = _box_uv(pos[corners] - root_centre, corner_fn, uv_scale)
    normals = corner_fn.copy()
    if tree.has_uv and props.shape[1] >= 5:
        outside_corner = np.repeat(~inside, 3)
        uv[outside_corner] = props[corners[outside_corner], 3:5]
        if props.shape[1] >= 8:
            sn = props[corners[outside_corner], 5:8]
            sn /= np.maximum(np.linalg.norm(sn, axis=1, keepdims=True), 1e-12)
            normals[outside_corner] = sn
    if smooth:
        sn = props[corners, -3:]
        normals = sn / np.maximum(np.linalg.norm(sn, axis=1, keepdims=True), 1e-12)
    points = pos - piece.centre
    return (
        points,
        np.full(len(tris), 3, dtype=np.int32),
        corners.astype(np.int32),
        uv.astype(np.float32),
        normals.astype(np.float32),
        np.flatnonzero(inside).astype(np.int32),
    )


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def _source_solid(prim) -> tuple[object, bool, str]:
    """A mesh prim as a manifold in world space, with its UVs and normals as properties.

    Returns (solid, has_uv, problem). Corners are split wherever UV or normal differ -- a
    seam -- and welded back by position, which is what lets a UV-mapped model count as
    closed. A mesh that still is not closed comes back as its convex hull, and *problem*
    says so.
    """
    mesh = UsdGeom.Mesh(prim)
    pts = np.asarray(mesh.GetPointsAttr().Get() or [], dtype=np.float64).reshape(-1, 3)
    counts = np.asarray(mesh.GetFaceVertexCountsAttr().Get() or [], dtype=np.int64)
    idx = np.asarray(mesh.GetFaceVertexIndicesAttr().Get() or [], dtype=np.int64)
    if not len(pts) or not len(counts):
        return None, False, "has no geometry"
    xf = np.array(UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default()), dtype=np.float64)
    world = pts @ xf[:3, :3] + xf[3, :3]

    # Fan-triangulate, remembering each triangle corner's face-corner index.
    starts = np.concatenate([[0], np.cumsum(counts)[:-1]])
    tri_fc = []
    for s, c in zip(starts, counts):
        for k in range(1, c - 1):
            tri_fc.append((s, s + k, s + k + 1))
    tri_fc = np.asarray(tri_fc, dtype=np.int64)
    fc_point = idx

    uv = None
    pv_api = UsdGeom.PrimvarsAPI(prim)
    for name in ("st", "uv", "UVMap", "map1"):
        pv = pv_api.GetPrimvar(name)
        if pv and pv.HasValue():
            vals = np.asarray(pv.ComputeFlattened(), dtype=np.float64)
            interp = pv.GetInterpolation()
            if interp == UsdGeom.Tokens.faceVarying and len(vals) == len(idx):
                uv = vals
            elif interp in (UsdGeom.Tokens.vertex, UsdGeom.Tokens.varying) and len(vals) == len(pts):
                uv = vals[fc_point]
            break
    nrm = None
    nattr = mesh.GetNormalsAttr().Get()
    if nattr is not None and len(nattr):
        n = np.asarray(nattr, dtype=np.float64)
        interp = mesh.GetNormalsInterpolation()
        if interp == UsdGeom.Tokens.faceVarying and len(n) == len(idx):
            nrm = n
        elif interp in (UsdGeom.Tokens.vertex, UsdGeom.Tokens.varying) and len(n) == len(pts):
            nrm = n[fc_point]
        if nrm is not None:
            nrm = nrm @ np.linalg.inv(xf[:3, :3]).T

    cols = [world[fc_point]]
    if uv is not None:
        cols.append(uv)
        if nrm is not None:
            cols.append(nrm)
    corner_props = np.hstack(cols)
    uniq, inverse = np.unique(np.round(corner_props, 6), axis=0, return_inverse=True)
    inverse = inverse.reshape(-1)
    tris = inverse[tri_fc]
    # USD meshes may be authored left-handed or simply inside out; manifold wants outward.
    vol, _ = _centroid(uniq[:, :3], tris)
    if vol < 0.0:
        tris = tris[:, ::-1]
    m = _mf.Mesh(vert_properties=uniq.astype(np.float32), tri_verts=tris.astype(np.uint32))
    m.merge()
    solid = _mf.Manifold(m)
    if solid.status() != _mf.Error.NoError or solid.is_empty():
        return _mf.Manifold.hull_points(world), False, "is not a closed surface -- fractured as its convex hull"
    return solid, uv is not None, ""


# ---------------------------------------------------------------------------
# USD
# ---------------------------------------------------------------------------


def piece_name(tree: Tree, node: int) -> str:
    return f"{tree.name}_n{node}"


def author(stage, out_path: Sdf.Path, trees: list[Tree], *, uv_scale: float, preview_generation: int = 0,
           explode: float = 0.0, inside_material: str = "", outside_material: str = "",
           bevel: float = 0.0, bevel_detail: int = 3) -> int:
    """Write the fractured instancer at *out_path*. Returns the instance count."""
    inst = UsdGeom.PointInstancer.Define(stage, out_path)
    inst.GetPrim().CreateAttribute(NS + "fractured", Sdf.ValueTypeNames.Bool, custom=True).Set(True)
    protos = out_path.AppendChild("Prototypes")
    UsdGeom.Scope.Define(stage, protos)
    targets: list[Sdf.Path] = []
    proto_of: dict[tuple[int, int], int] = {}
    inside_mat = Sdf.Path(inside_material) if inside_material else None
    outside_mat = Sdf.Path(outside_material) if outside_material else None
    # Hundreds of meshes, each with subsets, primvars and a binding: written straight into
    # the layer with the Sdf API, in one change block, rather than one composed-stage edit --
    # and one recomposition -- at a time. Measured: 1,270 bevelled pieces, 23 s -> ~2 s.
    layer = stage.GetEditTarget().GetLayer()
    names = Sdf.ValueTypeNames
    varying, uniform = Sdf.VariabilityVarying, Sdf.VariabilityUniform

    def attr(spec, name, type_name, value, variability=varying, custom=False, interpolation=None):
        a = Sdf.AttributeSpec(spec, name, type_name, variability, custom)
        a.default = value
        if interpolation:
            a.SetInfo("interpolation", interpolation)

    with Sdf.ChangeBlock():
        for tree in trees:
            root_centre = tree.pieces[0].centre
            out_m = outside_mat or tree.material
            in_m = inside_mat or out_m
            colour = Vt.Vec3fArray([Gf.Vec3f(*map(float, tree.colour))]) if tree.colour is not None else None
            for piece in tree.pieces:
                path = protos.AppendChild(piece_name(tree, piece.node))
                points, counts, indices, uv, normals, inside = piece_surface(
                    tree, piece, root_centre, uv_scale, bevelled(piece.solid, bevel, bevel_detail))
                spec = Sdf.CreatePrimInLayer(layer, path)
                spec.specifier = Sdf.SpecifierDef
                spec.typeName = "Mesh"
                attr(spec, "points", names.Point3fArray,
                     Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(points, dtype=np.float32)))
                attr(spec, "faceVertexCounts", names.IntArray, Vt.IntArray.FromNumpy(counts))
                attr(spec, "faceVertexIndices", names.IntArray, Vt.IntArray.FromNumpy(indices))
                attr(spec, "subdivisionScheme", names.Token, UsdGeom.Tokens.none, uniform)
                attr(spec, "normals", names.Normal3fArray, Vt.Vec3fArray.FromNumpy(normals),
                     interpolation=UsdGeom.Tokens.faceVarying)
                lo, hi = points.min(axis=0), points.max(axis=0)
                attr(spec, "extent", names.Float3Array,
                     Vt.Vec3fArray([Gf.Vec3f(*map(float, lo)), Gf.Vec3f(*map(float, hi))]))
                attr(spec, "primvars:st", names.TexCoord2fArray, Vt.Vec2fArray.FromNumpy(uv),
                     interpolation=UsdGeom.Tokens.faceVarying)
                if colour is not None:
                    attr(spec, "primvars:displayColor", names.Color3fArray, colour)
                attr(spec, "subsetFamily:materialBind:familyType", names.Token, UsdGeom.Tokens.partition, uniform)
                outside = np.setdiff1d(np.arange(len(counts), dtype=np.int32), inside)
                for sub_name, faces, target in (("outside", outside, out_m), ("inside", inside, in_m)):
                    sub = Sdf.PrimSpec(spec, sub_name, Sdf.SpecifierDef, "GeomSubset")
                    attr(sub, "elementType", names.Token, UsdGeom.Tokens.face, uniform)
                    attr(sub, "familyName", names.Token, UsdShade.Tokens.materialBind, uniform)
                    attr(sub, "indices", names.IntArray, Vt.IntArray.FromNumpy(np.asarray(faces, dtype=np.int32)))
                    if target:
                        # By path: the material need not exist on the stage being written,
                        # which is a scratch one -- see execute().
                        sub.SetInfo("apiSchemas", Sdf.TokenListOp.Create(prependedItems=["MaterialBindingAPI"]))
                        rel = Sdf.RelationshipSpec(sub, UsdShade.Tokens.materialBinding, custom=False)
                        rel.targetPathList.explicitItems = [Sdf.Path(str(target))]
                for name, type_name, value in (
                    ("tree", names.Int, tree.index),
                    ("node", names.Int, piece.node),
                    ("generation", names.Int, piece.generation),
                    ("children", names.IntArray, Vt.IntArray(piece.children)),
                    ("offset", names.Float3, Gf.Vec3f(*map(float, piece.offset))),
                    ("volume", names.Float, float(piece.volume)),
                ):
                    attr(spec, NS + name, type_name, value, custom=True)
                proto_of[(tree.index, piece.node)] = len(targets)
                targets.append(path)
    inst.CreatePrototypesRel().SetTargets(targets)

    # Instances: every tree's pieces of the preview generation (0 = whole), pushed out
    # along their offset from the root by Explode.
    positions, indices = [], []
    for tree in trees:
        root_centre = tree.pieces[0].centre
        for piece in tree.pieces:
            last = not piece.children
            if piece.generation == preview_generation or (last and piece.generation < preview_generation):
                d = piece.centre - root_centre
                positions.append(root_centre + d * (1.0 + explode))
                indices.append(proto_of[(tree.index, piece.node)])
    inst.CreateProtoIndicesAttr(Vt.IntArray(indices))
    inst.CreatePositionsAttr(Vt.Vec3fArray.FromNumpy(np.asarray(positions, dtype=np.float32).reshape(-1, 3)))
    inst.CreateOrientationsAttr(Vt.QuathArray([Gf.Quath(1, 0, 0, 0)] * len(indices)))
    return len(indices)


# ---------------------------------------------------------------------------
# Chunk
# ---------------------------------------------------------------------------


def _report(node, text):
    if (readout := node.get_param("readout")) is not None and readout.value != text:
        readout.value = text


def build_trees(stage, node) -> tuple[list[Tree], list[str], list]:
    """Fracture whatever the chunk's params name. Returns (trees, problems, source prims)."""
    is_f, is_i, is_s = chunk.is_param_t(float), chunk.is_param_t(int), chunk.is_param_t(str)
    seed = node.get_param("seed", is_i).value
    rng = np.random.default_rng(seed)
    generations = max(0, min(8, node.get_param("generations", is_i).value))
    jaggedness = max(0.0, min(1.0, node.get_param("jaggedness", is_f).value))
    candidates = 1 + int(round(7 * max(0.0, min(1.0, node.get_param("piece_shape", is_f).value))))
    trees, problems, sources = [], [], []
    if node.get_param("source", is_s).value == SOURCE_ROCKS:
        count = max(1, node.get_param("count", is_i).value)
        size = max(1e-3, node.get_param("size", is_f).value)
        var = max(0.0, min(0.9, node.get_param("size_variation", is_f).value))
        spacing = 2.2 * size * (1.0 + var)
        cols = max(1, int(np.ceil(np.sqrt(count))))
        origin = np.asarray(node.get_param("rock_origin").value or (0.0, 0.0, 0.0), dtype=np.float64)
        up = 2 if UsdGeom.GetStageUpAxis(stage).upper() == "Z" else 1
        side = (0, 1) if up == 2 else (0, 2)
        for r in range(count):
            s = size * (1.0 + var * (rng.random() * 2.0 - 1.0))
            pts = random_rock_points(
                rng, s, max(8, node.get_param("detail", is_i).value),
                max(0.0, min(0.9, node.get_param("roughness", is_f).value)),
                max(0.0, min(0.9, node.get_param("flatness", is_f).value)),
            )
            at = origin.copy()
            at[side[0]] += (r % cols) * spacing
            at[side[1]] += (r // cols) * spacing
            solid = _mf.Manifold.hull_points(pts + at).as_original()
            pieces = fracture(r, solid, rng, generations, jaggedness, candidates=candidates)
            trees.append(Tree(r, f"Rock{r:02d}", pieces, solid.original_id(), False,
                              colour=_PALETTE[(r * 5 + seed) % len(_PALETTE)]))
        return trees, problems, sources

    for prim in node.get_param("source_prims").get_prims():
        for p in Usd.PrimRange(prim):
            if not p.IsA(UsdGeom.Mesh):
                continue
            solid, has_uv, problem = _source_solid(p)
            if solid is None:
                problems.append(f"{p.GetName()} {problem or 'could not be read'}")
                continue
            if problem:
                problems.append(f"{p.GetName()} {problem}")
            solid = solid.as_original()
            index = len(trees)
            pieces = fracture(index, solid, rng, generations, jaggedness, candidates=candidates)
            colour = UsdGeom.Mesh(p).GetDisplayColorAttr().Get()
            material = UsdShade.MaterialBindingAPI(p).ComputeBoundMaterial()[0]
            trees.append(Tree(index, Tf.MakeValidIdentifier(p.GetName()), pieces, solid.original_id(), has_uv,
                              colour=tuple(colour[0]) if colour else None,
                              material=material.GetPath() if material else None))
            sources.append(p)
    return trees, problems, sources


#: Authored results, keyed on everything that shapes them. Small: re-cooks of the same few
#: settings are the whole point, and each entry holds a full layer.
_CACHE: "dict[bytes, tuple]" = {}
_CACHE_MAX = 6
_PARAMS = ("source", "source_prims", "output_path", "generations", "jaggedness", "piece_shape", "bevel",
           "bevel_detail", "seed", "count", "size",
           "size_variation", "roughness", "flatness", "detail", "rock_origin", "uv_scale",
           "outside_material", "inside_material", "preview_generation", "explode")


def _cache_key(upstream, node) -> bytes:
    """A digest of every param plus, for meshes, the source geometry as it composes upstream.

    Hashing what is read rather than tracking edits makes it safe: a moved, edited or
    swapped source is a different digest with no invalidation to get wrong.
    """
    h = hashlib.blake2b(digest_size=16)
    for name in _PARAMS:
        param = node.get_param(name)
        h.update(f"{name}={getattr(param, 'value', None)!r};".encode())
    if node.get_param("source").value != SOURCE_ROCKS:
        for prim in node.get_param("source_prims").get_prims():
            for p in Usd.PrimRange(upstream.GetPrimAtPath(prim.GetPath())):
                if not p.IsA(UsdGeom.Mesh):
                    continue
                m = UsdGeom.Mesh(p)
                h.update(str(p.GetPath()).encode())
                for attr in (m.GetPointsAttr(), m.GetFaceVertexCountsAttr(), m.GetFaceVertexIndicesAttr(),
                             m.GetNormalsAttr()):
                    v = attr.Get()
                    if v is not None:
                        h.update(np.asarray(v).tobytes())
                pv = UsdGeom.PrimvarsAPI(p).GetPrimvar("st")
                if pv and pv.HasValue():
                    h.update(np.asarray(pv.Get()).tobytes())
                h.update(np.asarray(UsdGeom.Xformable(p).ComputeLocalToWorldTransform(Usd.TimeCode.Default())).tobytes())
    return h.digest()


def execute(node) -> None:
    """Rebuild the fracture -- only when something it depends on changed.

    A cook reaches this for many reasons that change nothing here: an upstream chunk
    re-cooking, a wire connected downstream, a reload. Each one used to re-cut and re-write
    every piece. Now the params and the source geometry are hashed first: the same hash as
    the last cook does nothing, and one seen before restores its result from the cache.

    New results are written to a scratch layer rather than into the live stage -- writing
    hundreds of meshes into a composed stage costs three times as much, every edit a
    recomposition -- and then moved into this chunk's layer in one transfer.
    """
    stage = node.get_stage()
    layer = node.get_edit_context()
    if stage is None or layer is None:
        return
    _sync_visibility(node)
    if not _HAS_MANIFOLD:
        _report(node, "manifold3d is not installed -- pip install manifold3d")
        return
    is_f, is_i, is_s = chunk.is_param_t(float), chunk.is_param_t(int), chunk.is_param_t(str)
    out_path = Sdf.Path(node.get_param("output_path", is_s).value.strip() or "/Fractured")
    # Read the sources without this chunk's own layer, which hides them.
    scene = node.get_scene()
    upstream = chunk_stage.get_upstream_stage(stage, scene, node) if scene is not None else stage
    key = _cache_key(upstream, node)
    if key == getattr(node, "_fracture_key", None) and layer.GetPrimAtPath(out_path):
        return
    digest = key.hex()
    cached = _CACHE.get(key) or _load_from_disk(node, stage, digest)
    if cached is not None:
        _CACHE[key] = cached
    if cached is None:
        trees, problems, sources = build_trees(upstream, node)
        if not trees:
            if lreg := chunk.get_layer_manager():
                lreg.clear_layer(layer.identifier)
            node._fracture_key = None
            _report(node, "nothing to fracture -- pick a closed mesh, or use Procedural Rocks")
            return
        scratch = Usd.Stage.CreateInMemory()
        if out_path.GetParentPath() != Sdf.Path.absoluteRootPath:
            UsdGeom.Xform.Define(scratch, out_path.GetParentPath())
        count = author(
            scratch, out_path, trees,
            uv_scale=node.get_param("uv_scale", is_f).value,
            preview_generation=max(0, node.get_param("preview_generation", is_i).value),
            explode=max(0.0, node.get_param("explode", is_f).value),
            inside_material=node.get_param("inside_material", is_s).value.strip(),
            outside_material=node.get_param("outside_material", is_s).value.strip(),
            bevel=max(0.0, node.get_param("bevel", is_f).value),
            bevel_detail=max(2, node.get_param("bevel_detail", is_i).value),
        )
        for p in sources:
            # The instancer stands in for the source from here on.
            UsdGeom.Imageable(scratch.OverridePrim(p.GetPath())).MakeInvisible()
        total = sum(len(t.pieces) for t in trees)
        smallest = min(p.volume for t in trees for p in t.pieces)
        largest = max(t.pieces[0].volume for t in trees)
        text = f"{len(trees)} source{'s' if len(trees) != 1 else ''} · {total} pieces · {count} instances\n" \
               f"smallest piece {100.0 * smallest / max(largest, 1e-12):.1f}% of the largest source"
        if problems:
            text += "\n" + "\n".join(problems)
        # A layer of our own: the scratch stage's root layer comes back as a weak handle,
        # which expires with the stage -- a cache hit on it later is a use-after-free.
        owned = Sdf.Layer.CreateAnonymous(".usda")
        owned.TransferContent(scratch.GetRootLayer())
        cached = (owned, text)
        _CACHE[key] = cached
        _save_to_disk(node, stage, owned, text, digest)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
    layer.TransferContent(cached[0])
    node._fracture_key = key
    _report(node, cached[1])


def _cache_file(node, stage) -> str:
    """Where this chunk's result is kept on disk: the Cache File param, or -- when empty --
    ``cache/<scene>_<chunk>.usdc`` beside the saved scene. "" for an unsaved scene."""
    explicit = node.get_param("cache_file").value.strip() if node.get_param("cache_file") else ""
    if explicit:
        return explicit
    root = stage.GetRootLayer().realPath if stage else ""
    if not root:
        return ""
    scene = Path(root)
    return str(scene.parent / "cache" / f"{scene.stem}_{node.name}.usdc")


def _load_from_disk(node, stage, digest: str):
    """(layer, readout) of a result made from exactly this input before, or None."""
    path = _cache_file(node, stage)
    if not path or not Path(path).is_file():
        return None
    try:
        layer = Sdf.Layer.OpenAsAnonymous(path)
    except Exception:  # noqa: BLE001 - an unreadable cache is a miss
        return None
    data = layer.customLayerData if layer else {}
    if data.get("fracture:key") != digest:
        return None
    return layer, data.get("fracture:readout", "") + "\nloaded from cache"


def _save_to_disk(node, stage, layer, readout: str, digest: str) -> None:
    path = _cache_file(node, stage)
    if not path:
        return
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        out = Sdf.Layer.CreateAnonymous(".usdc")
        out.TransferContent(layer)
        data = dict(out.customLayerData)
        data["fracture:key"] = digest
        data["fracture:readout"] = readout
        out.customLayerData = data
        out.Export(path)
    except Exception as e:  # noqa: BLE001 - a failed cache write loses no work
        logger.warning("Fracture: could not write the cache %s: %s", path, e)


def _sync_visibility(node) -> None:
    """Show the params that apply to the chosen Source, hide the rest."""
    rocks = node.get_param("source").value == SOURCE_ROCKS
    for name, show in (("Rocks", rocks), ("source_prims", not rocks)):
        param = node.get_param(name)
        if param is None:
            continue
        if show:
            param.unset_state(chunk.ChunkFlags.State.HIDDEN)
        else:
            param.set_state(chunk.ChunkFlags.State.HIDDEN)


def _source_changed(owner, notice, sender) -> None:
    _sync_visibility(owner.get_chunk())


def main() -> chunk.Chunk:
    chunk_api = chunk.Chunk("Fracture")
    labeled = chunk.ChunkFlags.ParamFlags.LABELED
    _GF = (
        chunk.ChunkFlags.GroupFlags.COLLAPSABLE
        | chunk.ChunkFlags.GroupFlags.LABELED
        | chunk.ChunkFlags.GroupFlags.FRAMED
    )

    source = chunk.Menu(
        "source", "Source", menu_items=[SOURCE_PRIMS, SOURCE_ROCKS], default_value=SOURCE_PRIMS,
        tooltip="Fracture the meshes picked below, or generate rocks.",
    )
    source.add_callback(_source_changed, send_on_create=False)
    chunk_api.add_param(source)
    src = chunk.LineEdit(
        "source_prims", "Meshes", flags=labeled | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="The model to fracture -- a mesh, or any prim above meshes. Each closed mesh "
        "becomes its own breakable body.",
    )
    chunk_api.add_param(src)
    chunk_api.add_param(chunk.LineEdit(
        "output_path", "Output", default_value="/Fractured", flags=labeled,
        tooltip="Where the fractured PointInstancer is written. Give it to the RBD Solver as its "
        "Target Prim, with Breakable on.",
    ))
    chunk_api.add_param(chunk.Spinbox(
        "generations", "Generations", default_value=4, is_int=True,
        tooltip="How many times a piece can break. Each generation about halves the pieces, so 4 "
        "makes up to 16 from one model.\nEvery piece is a body the solver keeps ready, so each "
        "extra generation roughly doubles its work.",
    ))
    chunk_api.add_param(chunk.Slider(
        "jaggedness", "Break Angle", min_value=0.0, max_value=1.0, default_value=0.35,
        tooltip="0 = every break is a clean cut across the piece's long axis; 1 = any angle.",
    ))
    chunk_api.add_param(chunk.Slider(
        "piece_shape", "Chunky Pieces", min_value=0.0, max_value=1.0, default_value=0.7,
        tooltip="0 = take the first cut that works, which leaves wedges and needles. Higher tries "
        "more planes per cut and keeps the one leaving the chunkiest pieces -- gravel rather "
        "than shards. Costs fracture time, not simulation time.",
    ))
    chunk_api.add_param(chunk.Spinbox("seed", "Seed", default_value=0, is_int=True))

    rocks = chunk.Group("Rocks", "Procedural Rocks", flags=_GF)
    rocks.add_param(chunk.Spinbox("count", "Rock Shapes", default_value=8, is_int=True))
    rocks.add_param(chunk.Spinbox("size", "Size", default_value=15.0,
                                  tooltip="Average rock diameter, in scene units."))
    rocks.add_param(chunk.Slider("size_variation", "Size Variation", min_value=0.0, max_value=0.9, default_value=0.3))
    rocks.add_param(chunk.Slider("roughness", "Roughness", min_value=0.0, max_value=0.9, default_value=0.3))
    rocks.add_param(chunk.Slider("flatness", "Flatness", min_value=0.0, max_value=0.9, default_value=0.35))
    rocks.add_param(chunk.Spinbox("detail", "Facets", default_value=28, is_int=True))
    rocks.add_param(chunk.Vector("rock_origin", "Lay Out At", param=chunk.Spinbox(), length=3, suffixes="XYZ",
                                 default_value=(0.0, 0.0, 0.0),
                                 tooltip="Where the rocks are laid out in a grid. When the RBD Solver drops "
                                 "them from an emitter, this is only where they wait."))
    chunk_api.add_param(rocks)
    # Selected Meshes is the default, so the rock settings start hidden.
    rocks.set_state(chunk.ChunkFlags.State.HIDDEN)

    surf = chunk.Group("Surface", "Surface", flags=_GF)
    surf.add_param(chunk.Slider(
        "bevel", "Bevel", min_value=0.0, max_value=1.0, default_value=0.0,
        tooltip="Round the pieces' edges: 0 = sharp, 1 = pebble-round. For looks only -- a piece "
        "still collides as its convex hull.",
    ))
    surf.add_param(chunk.Spinbox(
        "bevel_detail", "Bevel Detail", default_value=3, is_int=True,
        tooltip="How finely a bevelled edge is subdivided. Each step up adds triangles by its "
        "square: 3 is 9x the faces, 4 is 16x.",
    ))
    surf.add_param(chunk.Spinbox("uv_scale", "UV Scale", default_value=0.05,
                                 tooltip="UVs per scene unit for the projected faces: the cuts, and all "
                                 "of a procedural rock. 0.05 = one texture repeat per 20 units."))
    surf.add_param(chunk.LineEdit("outside_material", "Outside Material", flags=labeled,
                                  tooltip="Bound to the original surface. Empty keeps the model's own."))
    surf.add_param(chunk.LineEdit("inside_material", "Inside Material", flags=labeled,
                                  tooltip="Bound to the fracture surfaces. Empty uses the outside's."))
    chunk_api.add_param(surf)
    chunk_api.add_param(chunk.LineEdit(
        "cache_file", "Cache File", flags=labeled,
        tooltip="Where the result is kept on disk. Empty = cache/<scene>_<this chunk>.usdc beside the "
        "saved scene. Tagged with a digest of every setting and the source geometry: the same input "
        "-- after a reload, or a change undone -- loads from here instead of cutting again.",
    ))

    prev = chunk.Group("Preview", "Start State", flags=_GF)
    prev.add_param(chunk.Spinbox("preview_generation", "Start Broken", default_value=0, is_int=True,
                                 tooltip="0 = whole. Higher starts every source already broken into that "
                                 "generation's pieces -- also what the solver starts from."))
    prev.add_param(chunk.Spinbox("explode", "Explode", default_value=0.0,
                                 tooltip="Push the starting pieces apart, to see the cuts."))
    chunk_api.add_param(prev)

    chunk_api.add_param(chunk.Label("readout", "not fractured yet", default_value="not fractured yet",
                                    tooltip="What the last cook produced."))
    return chunk_api


def config() -> chunk.DefaulConfig:
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.DYNAMICS,
        description=(
            "Pre-breaks a model -- or procedural rocks -- into a tree of pieces that exactly\n"
            "add up to it, with UVs and inside/outside groups. Feed the result to the RBD\n"
            "Solver with Breakable on."
        ),
        requirements=["numpy>=2,<3", "manifold3d>=3"],
    )
