"""Rock Library chunk -- procedural convex rocks, each pre-split into a breakage tree.

The Crusher Solver breaks a rock by swapping it for the two pieces it was cut into, so
every piece a simulation can ever produce has to exist before the simulation starts.
This chunk makes them:

* **Roots** are random convex rocks: points scattered over a squashed, jittered sphere,
  wrapped in their convex hull. Convex because the solver collides every piece as its
  hull anyway -- a concave rock would collide as something it does not look like.
* **Pieces** come from cutting a piece in two with a plane, recursively, to *Generations*
  levels. A plane through a convex solid gives two convex solids that tile it exactly, so
  the two children always add up to their parent's volume: material is neither made nor
  lost when a rock breaks. The cut is biased across the piece's longest axis, which is how
  rock actually tends to fail and keeps the pieces from turning into ever-thinner slivers.

The tree is stored in binary-heap order -- node ``n``'s children are ``2n+1`` and
``2n+2`` -- so the solver never has to read a relationship to find what a piece breaks
into. Each piece is a Mesh whose points are centred on its own centre of mass (the frame
a rigid body wants) and carries:

* ``crusher:rock`` / ``crusher:node`` / ``crusher:generation`` -- where it sits in the tree;
* ``crusher:offset`` -- its centre minus its parent's, in the rock's frame, which is
  where a child appears when its parent breaks;
* ``crusher:volume`` -- in cubic scene units.

The preview shows one generation of every rock, optionally pulled apart, so the library
can be judged before anything is simulated.
"""

from __future__ import annotations

import logging

import numpy as np
from pxr import Gf, Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk

try:
    from scipy.spatial import ConvexHull

    _HAS_SCIPY = True
except ImportError:  # pragma: no cover - exercised only on a machine without scipy
    ConvexHull = None
    _HAS_SCIPY = False

logger = logging.getLogger(__name__)

#: Attribute namespace shared with the Crusher Solver and the Crusher Rig.
NS = "crusher:"

# Rock colours, picked per root: a spread of greys and warm browns, so a pile of broken
# rock reads as many rocks rather than one colour.
_PALETTE = np.array(
    [
        (0.30, 0.29, 0.27),
        (0.36, 0.33, 0.30),
        (0.25, 0.24, 0.23),
        (0.40, 0.35, 0.29),
        (0.33, 0.29, 0.25),
        (0.22, 0.21, 0.20),
        (0.42, 0.39, 0.35),
        (0.31, 0.26, 0.22),
    ]
)


# ---------------------------------------------------------------------------
# Convex geometry
# ---------------------------------------------------------------------------


def hull_volume_centroid(points: np.ndarray) -> tuple[float, np.ndarray]:
    """Volume and centre of mass of the convex hull of *points*.

    The hull is split into tetrahedra fanned from an interior point; each contributes its
    volume at its own centroid.
    """
    hull = ConvexHull(points)
    pts = hull.points
    inner = pts[hull.vertices].mean(axis=0)
    a = pts[hull.simplices[:, 0]] - inner
    b = pts[hull.simplices[:, 1]] - inner
    c = pts[hull.simplices[:, 2]] - inner
    vols = np.abs(np.einsum("ij,ij->i", a, np.cross(b, c))) / 6.0
    total = float(vols.sum())
    if total <= 0.0:
        return 0.0, inner
    cents = inner + (a + b + c) / 4.0
    return total, (cents * vols[:, None]).sum(axis=0) / total


def hull_mesh(points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(vertices, triangles) of the convex hull of *points*, wound outward.

    Qhull does not orient its facets consistently; each one's outward plane normal is in
    ``equations``, so a triangle facing the other way is flipped to match.
    """
    hull = ConvexHull(points)
    verts_idx = hull.vertices
    remap = -np.ones(len(hull.points), dtype=np.int64)
    remap[verts_idx] = np.arange(len(verts_idx))
    verts = hull.points[verts_idx]
    tris = remap[hull.simplices]
    p = verts[tris]
    n = np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0])
    flip = np.einsum("ij,ij->i", n, hull.equations[:, :3]) < 0.0
    tris[flip] = tris[flip][:, ::-1]
    return verts, tris


def split_convex(points: np.ndarray, normal: np.ndarray, origin: np.ndarray):
    """Cut the convex hull of *points* with a plane. Returns the two halves' point sets.

    Each half is its own side's hull vertices plus every point where a hull edge crosses
    the plane; the two share the cut polygon, and their hulls tile the original exactly.
    Returns None when the plane misses the solid, or only shaves off something flat.
    """
    hull = ConvexHull(points)
    verts = hull.points
    d = (verts - origin) @ normal
    tri = hull.simplices
    edges = np.concatenate([tri[:, [0, 1]], tri[:, [1, 2]], tri[:, [2, 0]]])
    edges = np.unique(np.sort(edges, axis=1), axis=0)
    di, dj = d[edges[:, 0]], d[edges[:, 1]]
    crossing = di * dj < 0.0
    if not crossing.any():
        return None
    e = edges[crossing]
    t = (d[e[:, 0]] / (d[e[:, 0]] - d[e[:, 1]]))[:, None]
    cut = verts[e[:, 0]] + (verts[e[:, 1]] - verts[e[:, 0]]) * t
    used = np.zeros(len(verts), dtype=bool)
    used[hull.vertices] = True
    below = np.concatenate([verts[used & (d < 0.0)], cut])
    above = np.concatenate([verts[used & (d > 0.0)], cut])
    if len(below) < 4 or len(above) < 4:
        return None
    return below, above


def principal_axes(points: np.ndarray) -> np.ndarray:
    """Rows are the point set's principal directions, longest first."""
    centred = points - points.mean(axis=0)
    _, _, vt = np.linalg.svd(centred, full_matrices=False)
    return vt


def random_rotation(rng: np.random.Generator) -> np.ndarray:
    """A uniformly random 3x3 rotation matrix."""
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


def random_rock(rng: np.random.Generator, size: float, detail: int = 28, roughness: float = 0.3,
                flatness: float = 0.35) -> np.ndarray:
    """Hull vertices of one random convex rock about *size* across, centred on its mass."""
    dirs = rng.normal(size=(max(8, detail), 3))
    dirs /= np.linalg.norm(dirs, axis=1, keepdims=True)
    radii = 1.0 + roughness * (rng.random(len(dirs)) * 2.0 - 1.0)
    # Rock is rarely round: stretch one axis, squash another.
    axes = np.array([1.0, 1.0 - flatness * rng.random(), 1.0 - flatness * (0.5 + 0.5 * rng.random())])
    pts = (dirs * radii[:, None] * axes) @ random_rotation(rng).T
    hull = ConvexHull(pts)
    pts = pts[hull.vertices]
    # Scale to the requested size by the equivalent-sphere diameter, so rocks of one size
    # weigh about the same whatever their proportions.
    vol, centre = hull_volume_centroid(pts)
    diameter = (6.0 * vol / np.pi) ** (1.0 / 3.0)
    return (pts - centre) * (size / diameter)


class Piece:
    """One node of a rock's breakage tree."""

    __slots__ = ("rock", "node", "generation", "points", "offset", "volume")

    def __init__(self, rock, node, generation, points, offset, volume):
        self.rock = rock
        self.node = node
        self.generation = generation
        #: Hull vertices relative to this piece's own centre of mass.
        self.points = points
        #: This piece's centre minus its parent's (zero for a root), in the rock's frame.
        self.offset = offset
        self.volume = volume


def nodes_per_rock(generations: int) -> int:
    """How many pieces one rock's tree holds: 1 root + 2 + 4 + ... down *generations* cuts."""
    return (1 << (generations + 1)) - 1


def _cut(points: np.ndarray, rng: np.random.Generator, jaggedness: float, attempts: int = 12):
    """Split a piece (points about its own centre) in two. Returns [(pts, centre, vol)] * 2."""
    axes = principal_axes(points)
    extent = float(np.ptp(points @ axes[0]))
    for attempt in range(attempts):
        # Across the long axis, tilted at random: a clean slice through the middle when
        # jaggedness is 0, anything up to a random plane at 1.
        wobble = rng.normal(size=3)
        normal = axes[0] + jaggedness * 1.5 * wobble / max(np.linalg.norm(wobble), 1e-9)
        normal /= np.linalg.norm(normal)
        # Off-centre by up to an eighth of the length: unequal halves read as real breakage.
        origin = normal * extent * 0.12 * (rng.random() * 2.0 - 1.0) * (1.0 - attempt / attempts)
        halves = split_convex(points, normal, origin)
        if halves is None:
            continue
        out = []
        for half in halves:
            try:
                vol, centre = hull_volume_centroid(half)
                hull = ConvexHull(half - centre)
            except Exception:  # noqa: BLE001 - qhull rejects flat slivers; try another plane
                out = None
                break
            out.append(((half - centre)[hull.vertices], centre, vol))
        if out and min(o[2] for o in out) > 0.08 * (out[0][2] + out[1][2]):
            return out
    return None


def build_library(count: int = 8, size: float = 15.0, size_variation: float = 0.3,
                  generations: int = 4, roughness: float = 0.3, flatness: float = 0.35,
                  jaggedness: float = 0.35, detail: int = 28, seed: int = 0) -> list[list[Piece]]:
    """Every rock's full breakage tree. ``library[rock][node]``, heap-ordered.

    A cut that fails (the plane only grazes a sliver) is retried with another plane; one
    that keeps failing leaves that branch un-split, recorded as a piece whose children are
    None -- the solver treats it as unbreakable.
    """
    if not _HAS_SCIPY:
        raise RuntimeError("scipy is required to build a rock library -- pip install scipy")
    rng = np.random.default_rng(seed)
    total = nodes_per_rock(generations)
    library: list[list[Piece]] = []
    for r in range(count):
        s = size * (1.0 + size_variation * (rng.random() * 2.0 - 1.0))
        root_pts = random_rock(rng, s, detail, roughness, flatness)
        vol, _ = hull_volume_centroid(root_pts)
        tree: list[Piece | None] = [None] * total
        tree[0] = Piece(r, 0, 0, root_pts, np.zeros(3), vol)
        for n in range(total):
            piece = tree[n]
            if piece is None or 2 * n + 2 >= total:
                continue
            halves = _cut(piece.points, rng, jaggedness)
            if halves is None:
                continue
            for k, (pts, centre, v) in enumerate(halves):
                c = 2 * n + 1 + k
                tree[c] = Piece(r, c, piece.generation + 1, pts, centre, v)
        library.append(tree)
    return library


# ---------------------------------------------------------------------------
# USD
# ---------------------------------------------------------------------------


def piece_name(rock: int, node: int) -> str:
    return f"n{node}"


def rock_name(rock: int) -> str:
    return f"Rock_{rock:02d}"


def author_piece_mesh(stage: Usd.Stage, path: Sdf.Path, points: np.ndarray,
                      colour=None) -> UsdGeom.Mesh:
    """A faceted convex Mesh from hull *points* -- flat-shaded, as broken rock is."""
    verts, tris = hull_mesh(points)
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(verts, dtype=np.float32)))
    mesh.CreateFaceVertexCountsAttr(Vt.IntArray([3] * len(tris)))
    mesh.CreateFaceVertexIndicesAttr(Vt.IntArray.FromNumpy(np.ascontiguousarray(tris.reshape(-1), dtype=np.int32)))
    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
    # One normal per face, repeated on its corners: without it a renderer smooths the
    # hull into a pebble and every fracture face loses its edge.
    p = verts[tris]
    fn = np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0])
    fn /= np.maximum(np.linalg.norm(fn, axis=1, keepdims=True), 1e-12)
    mesh.CreateNormalsAttr(Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(np.repeat(fn, 3, axis=0), dtype=np.float32)))
    mesh.SetNormalsInterpolation(UsdGeom.Tokens.faceVarying)
    lo, hi = verts.min(axis=0), verts.max(axis=0)
    mesh.CreateExtentAttr(Vt.Vec3fArray([Gf.Vec3f(*map(float, lo)), Gf.Vec3f(*map(float, hi))]))
    if colour is not None:
        mesh.CreateDisplayColorAttr(Vt.Vec3fArray([Gf.Vec3f(*map(float, colour))]))
    return mesh


def rock_colour(rock: int, seed: int = 0) -> np.ndarray:
    return _PALETTE[(rock * 5 + seed) % len(_PALETTE)]


def author_library(stage: Usd.Stage, root: Sdf.Path, library: list[list[Piece]], *,
                   preview_generation: int = 0, explode: float = 0.0, spacing: float = 0.0,
                   visible: bool = True, seed: int = 0) -> None:
    """Write *library* under *root*: one Xform per rock, one Mesh per piece."""
    top = UsdGeom.Xform.Define(stage, root)
    top.GetPrim().CreateAttribute(NS + "library", Sdf.ValueTypeNames.Bool, custom=True).Set(True)
    img = UsdGeom.Imageable(top)
    if visible:
        img.CreateVisibilityAttr(UsdGeom.Tokens.inherited)
    else:
        img.MakeInvisible()
    if spacing <= 0.0:
        spacing = 2.0 * max((float(np.ptp(t[0].points, axis=0).max()) for t in library), default=1.0)
    cols = max(1, int(np.ceil(np.sqrt(len(library)))))
    for tree in library:
        root_piece = tree[0]
        r = root_piece.rock
        rock_path = root.AppendChild(rock_name(r))
        xf = UsdGeom.Xform.Define(stage, rock_path)
        xf.AddTranslateOp().Set(Gf.Vec3d((r % cols) * spacing, 0.0, (r // cols) * spacing))
        colour = rock_colour(r, seed)
        # Where each piece sits relative to the rock's root centre: the sum of offsets down
        # its branch.
        placed = {0: np.zeros(3)}
        for piece in tree:
            if piece is None:
                continue
            n = piece.node
            if n:
                parent = (n - 1) // 2
                placed[n] = placed[parent] + piece.offset * (1.0 + explode)
            mesh = author_piece_mesh(stage, rock_path.AppendChild(piece_name(r, n)), piece.points, colour)
            prim = mesh.GetPrim()
            prim.CreateAttribute(NS + "rock", Sdf.ValueTypeNames.Int, custom=True).Set(r)
            prim.CreateAttribute(NS + "node", Sdf.ValueTypeNames.Int, custom=True).Set(n)
            prim.CreateAttribute(NS + "generation", Sdf.ValueTypeNames.Int, custom=True).Set(piece.generation)
            prim.CreateAttribute(NS + "offset", Sdf.ValueTypeNames.Float3, custom=True).Set(
                Gf.Vec3f(*map(float, piece.offset))
            )
            prim.CreateAttribute(NS + "volume", Sdf.ValueTypeNames.Float, custom=True).Set(float(piece.volume))
            mesh.AddTranslateOp().Set(Gf.Vec3d(*map(float, placed[n])))
            if piece.generation != preview_generation:
                mesh.MakeInvisible()


def read_library(stage: Usd.Stage, root: Sdf.Path | str) -> dict[int, dict[int, dict]]:
    """The library under *root* as ``{rock: {node: piece}}``, read back from USD.

    Each piece is a dict of points (local, about its centre), face counts and indices,
    normals, offset, volume, generation, colour and its prim path. Missing nodes simply are not there: a piece
    without both children does not break.
    """
    out: dict[int, dict[int, dict]] = {}
    prim = stage.GetPrimAtPath(str(root))
    if not prim or not prim.IsValid():
        return out
    for p in Usd.PrimRange(prim):
        mesh = UsdGeom.Mesh(p)
        rock_attr = p.GetAttribute(NS + "rock")
        if not mesh or not rock_attr or not rock_attr.HasValue():
            continue
        pts = np.asarray(mesh.GetPointsAttr().Get(), dtype=np.float64)
        idx = np.asarray(mesh.GetFaceVertexIndicesAttr().Get(), dtype=np.int64)
        counts = np.asarray(mesh.GetFaceVertexCountsAttr().Get(), dtype=np.int64)
        colour = mesh.GetDisplayColorAttr().Get()
        normals = mesh.GetNormalsAttr().Get()
        out.setdefault(int(rock_attr.Get()), {})[int(p.GetAttribute(NS + "node").Get())] = dict(
            points=pts,
            indices=idx,
            counts=counts,
            offset=np.asarray(p.GetAttribute(NS + "offset").Get() or (0, 0, 0), dtype=np.float64),
            volume=float(p.GetAttribute(NS + "volume").Get() or 0.0),
            generation=int(p.GetAttribute(NS + "generation").Get() or 0),
            normals=np.asarray(normals, dtype=np.float64) if normals else None,
            normals_interp=mesh.GetNormalsInterpolation(),
            colour=tuple(colour[0]) if colour else (0.45, 0.42, 0.4),
            path=p.GetPath(),
        )
    return out


# ---------------------------------------------------------------------------
# Chunk
# ---------------------------------------------------------------------------


def execute(node) -> None:
    """Rebuild the library. Deterministic from the params, so it is safe to re-run on load."""
    stage = node.get_stage()
    layer = node.get_edit_context()
    if stage is None or layer is None:
        return
    readout = node.get_param("readout")
    if not _HAS_SCIPY:
        if readout is not None:
            readout.value = "scipy is not installed -- pip install scipy"
        return
    is_f, is_i, is_s = chunk.is_param_t(float), chunk.is_param_t(int), chunk.is_param_t(str)
    path = Sdf.Path(node.get_param("library_path", is_s).value or "/RockLibrary")
    generations = max(0, min(7, node.get_param("generations", is_i).value))
    seed = node.get_param("seed", is_i).value
    library = build_library(
        count=max(1, node.get_param("count", is_i).value),
        size=max(1e-3, node.get_param("size", is_f).value),
        size_variation=max(0.0, min(0.9, node.get_param("size_variation", is_f).value)),
        generations=generations,
        roughness=max(0.0, min(0.9, node.get_param("roughness", is_f).value)),
        flatness=max(0.0, min(0.9, node.get_param("flatness", is_f).value)),
        jaggedness=max(0.0, min(1.0, node.get_param("jaggedness", is_f).value)),
        detail=max(8, node.get_param("detail", is_i).value),
        seed=seed,
    )
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)
    with Usd.EditContext(stage, layer):
        author_library(
            stage,
            path,
            library,
            preview_generation=min(generations, max(0, node.get_param("preview_generation", is_i).value)),
            explode=max(0.0, node.get_param("explode", is_f).value),
            visible=bool(node.get_param("show_preview", chunk.is_param_t(bool)).value),
            seed=seed,
        )
    if readout is not None:
        pieces = sum(1 for tree in library for p in tree if p is not None)
        smallest = min(p.volume for tree in library for p in tree if p is not None)
        largest = max(tree[0].volume for tree in library)
        d = lambda v: (6.0 * v / np.pi) ** (1.0 / 3.0)  # noqa: E731 - equivalent diameter
        readout.value = (
            f"{len(library)} rocks · {pieces} pieces · {generations} generations\n"
            f"largest {d(largest):.1f} · smallest piece {d(smallest):.1f} (equiv. diameter)"
        )


def main() -> chunk.Chunk:
    chunk_api = chunk.Chunk("Rock Library")
    labeled = chunk.ChunkFlags.ParamFlags.LABELED

    chunk_api.add_param(
        chunk.LineEdit(
            "library_path",
            "Library Prim",
            default_value="/RockLibrary",
            flags=labeled,
            tooltip="Where the rocks are written. Point the Crusher Solver's Rock Library at it.",
        )
    )
    chunk_api.add_param(chunk.Spinbox("count", "Rock Shapes", default_value=8, is_int=True,
                                      tooltip="How many different rocks. The solver picks one at random per rock it drops."))
    chunk_api.add_param(chunk.Spinbox("size", "Size", default_value=15.0,
                                      tooltip="Average rock diameter, in scene units (15 = 15 cm on a centimetre stage)."))
    chunk_api.add_param(chunk.Slider("size_variation", "Size Variation", min_value=0.0, max_value=0.9,
                                     default_value=0.3, tooltip="How much the rocks differ in size, ± this fraction."))
    chunk_api.add_param(
        chunk.Spinbox(
            "generations",
            "Generations",
            default_value=4,
            is_int=True,
            tooltip="How many times a rock can break. Each generation halves the volume, so 4 "
            "breaks a rock into up to 16 pieces about 40% of its diameter.\n"
            "Every piece is a body the solver has to keep ready, so each extra generation "
            "doubles its size.",
        )
    )

    shape = chunk.Group("Shape", "Shape", flags=chunk.ChunkFlags.GroupFlags.COLLAPSABLE
                        | chunk.ChunkFlags.GroupFlags.LABELED | chunk.ChunkFlags.GroupFlags.FRAMED)
    shape.add_param(chunk.Slider("roughness", "Roughness", min_value=0.0, max_value=0.9, default_value=0.3,
                                 tooltip="How lumpy each rock's outline is."))
    shape.add_param(chunk.Slider("flatness", "Flatness", min_value=0.0, max_value=0.9, default_value=0.35,
                                 tooltip="0 = roughly round, higher = slabby."))
    shape.add_param(chunk.Slider("jaggedness", "Break Angle", min_value=0.0, max_value=1.0, default_value=0.35,
                                 tooltip="0 = every break is a clean cut across the piece's long axis; "
                                 "1 = breaks at any angle."))
    shape.add_param(chunk.Spinbox("detail", "Facets", default_value=28, is_int=True,
                                  tooltip="Points the rock's hull is wrapped around. More = rounder, finer facets."))
    shape.add_param(chunk.Spinbox("seed", "Seed", default_value=0, is_int=True))
    chunk_api.add_param(shape)

    preview = chunk.Group("Preview", "Preview", flags=chunk.ChunkFlags.GroupFlags.COLLAPSABLE
                          | chunk.ChunkFlags.GroupFlags.LABELED | chunk.ChunkFlags.GroupFlags.FRAMED)
    preview.add_param(chunk.Checkbox("show_preview", "Show Library", default_value=True,
                                     tooltip="Show the library in the viewport. Turn it off once it feeds a "
                                     "simulation -- the solver copies the rocks, it does not need these visible."))
    preview.add_param(chunk.Spinbox("preview_generation", "Show Generation", default_value=0, is_int=True,
                                    tooltip="Which generation of pieces to show: 0 = whole rocks."))
    preview.add_param(chunk.Spinbox("explode", "Explode", default_value=0.0,
                                    tooltip="Pull the pieces apart, to see the cuts."))
    chunk_api.add_param(preview)

    chunk_api.add_param(
        chunk.Label("readout", "not built yet", default_value="not built yet", tooltip="What the last cook produced.")
    )
    return chunk_api


def config() -> chunk.DefaulConfig:
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.DYNAMICS,
        description=(
            "Procedural convex rocks, each pre-cut into a tree of pieces that exactly add up\n"
            "to it. Feeds the Crusher Solver, which breaks a rock by swapping it for its pieces."
        ),
        requirements=["numpy>=2,<3", "scipy>=1.10"],
    )
