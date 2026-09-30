from __future__ import annotations
from pxr import Usd, UsdGeom, Sdf
from usd_chunks import chunk, chunk_utils
import itertools
import logging
import numpy as np
import os
from enum import IntEnum


logger = logging.getLogger(__name__)


#: The subdivision scheme of every mesh this chunk builds: "none", i.e. plain polygons.
#: USD's default is catmullClark, which makes the mesh a subdivision cage -- a renderer
#: that subdivides (V-Ray does; Storm at its default refine level does not) then draws a
#: two-segment cube as a rounded blob and pulls a coarse sphere inside its own points, so
#: what renders stops matching the viewport and anything measured from the points.
_POLYGONS = UsdGeom.Tokens.none


class Topo(IntEnum):
    TRIS = 3
    QUAD = 4


def build_plane(stage, name="/plane", divx=2, divy=2, width=1.0, height=1.0, pos=(0, 0, 0), topo: Topo = Topo.QUAD):
    """Build a simple plane for testing hair procedural"""
    w = max(int(divx), 2)
    h = max(int(divy), 2)

    # indices per face
    ipf = topo

    nfaces = (w - 1) * (h - 1)
    if ipf == 3:
        nfaces *= 2

    pts = np.zeros((w * h, 3))
    uvs = np.zeros((ipf * nfaces, 2))

    py = pos[1]
    pz = pos[2]
    ids = []

    counts = [ipf] * nfaces

    for y in range(h):
        px = pos[0]

        wr = range(w)
        for x in wr:
            i = x + y * w

            pts[i] = (px, py, pz)

            if x < w - 1 and y < h - 1:
                if ipf == 4:
                    ids += [i, i + 1, i + w + 1, i + w]
                elif ipf == 3:
                    ids += [i, i + 1, i + w + 1]
                    ids += [i, i + w + 1, i + w]

            px += width / (divx - 1)
        py += height / (divy - 1)

    for i in range(ipf * nfaces):
        p = pts[ids[i]]
        uvs[i] = (p[0], p[1])

    # Lay it flat on the stage's ground plane, centered on pos and facing up. Built above in
    # X-Y with its corner at pos, which on a Y-up stage is a wall. Turning -90 degrees about
    # X, (x, y, 0) -> (x, 0, -y), keeps the faces pointing up.
    pts -= (pos[0] + width / 2, pos[1] + height / 2, pos[2])
    if UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.y:
        pts = np.stack([pts[:, 0], pts[:, 2], -pts[:, 1]], axis=1)
    pts += pos

    primpath = Sdf.Path(name)
    mesh = UsdGeom.Mesh.Define(stage, primpath)
    mesh.CreatePointsAttr(pts)
    mesh.CreateFaceVertexIndicesAttr(ids)
    mesh.CreateFaceVertexCountsAttr(counts)
    # Polygons, not a subdivision cage -- see _POLYGONS.
    mesh.CreateSubdivisionSchemeAttr(_POLYGONS)

    # orient = mesh.CreateOrientationAttr()
    # orient.Set("leftHanded")

    primvarsAPI = UsdGeom.PrimvarsAPI.Get(stage, primpath)
    stPrimvar = primvarsAPI.CreatePrimvar("st", Sdf.ValueTypeNames.TexCoord2fArray, "faceVarying")
    stPrimvar.Set(uvs)

    pv = UsdGeom.PrimvarsAPI(mesh.GetPrim())
    rest = pv.CreatePrimvar("rest", Sdf.ValueTypeNames.Vector3fArray, "vertex", len(pts))
    rest.Set(pts)

    return mesh


class Node:
    def __init__(self, xyz, dimxyz):
        self.xyz = xyz
        self.dim = dimxyz

    def next(self, dim):
        ni = list(self.xyz)
        ni[dim] = ni[dim] + 1
        if ni[dim] > self.dim[dim] - 1:
            raise IndexError("Fuck off")

        return Node(tuple(ni), self.dim)

    @property
    def v(self):
        x, y, z = self.xyz

        res = x + (z * self.dim[0] * self.dim[1]) + (y * self.dim[0])

        if res > self.dim[0] * self.dim[1] + self.dim[0]:
            xi = max(self.dim[0] - 2, 0)
            yi = max(self.dim[1] - 2, 0)
            zi = max(self.dim[2] - 2, 0)

            if z == self.dim[2] - 1:
                return res - xi * yi * zi

            skip = xi * yi * (z - 1)
            if y == 0:
                return res - skip
            if y == self.dim[1] - 1 or x == 0:
                return res - (skip + xi * (y - 1))
            if x == self.dim[0] - 1:
                return res - (skip + xi * y)

        return res

    def _flip(self, vts):
        vts[-1], vts[-3] = vts[-3], vts[-1]

    def xy(self):
        res = [self.v, self.next(0).v, self.next(0).next(1).v, self.next(1).v]
        if self.xyz[2] == 0:
            self._flip(res)
        return res

    def zx(self):
        res = [self.v, self.next(2).v, self.next(0).next(2).v, self.next(0).v]
        if self.xyz[1] == 0:
            self._flip(res)
        return res

    def yz(self):
        res = [self.v, self.next(1).v, self.next(2).next(1).v, self.next(2).v]
        if self.xyz[0] == 0:
            self._flip(res)
        return res


def build_cube(
    stage,
    name="/cube",
    xseg=5,
    yseg=5,
    zseg=1,
    width=1.0,
    height=1.0,
    depth=1.0,
    pos=(0, 0, 0),
):
    xseg = max(int(xseg), 1)
    yseg = max(int(yseg), 1)
    zseg = max(int(zseg), 1)

    npts = xseg * yseg * zseg
    if npts < 4:
        return

    npts -= (xseg - 2) * (yseg - 2) * (zseg - 2)

    indices = []
    pts = np.zeros((npts, 3))

    for z, y, x in itertools.product(range(zseg), range(yseg), range(xseg)):
        add_xy = z == zseg - 1 or z == 0
        add_zx = y == yseg - 1 or y == 0
        add_yz = x == xseg - 1 or x == 0

        if not add_xy and not add_yz and not add_zx:
            continue

        p = np.array((x / max(xseg - 1, 1) * width, y / max(yseg - 1, 1) * height, z / max(zseg - 1, 1) * depth))
        n = Node((x, y, z), (xseg, yseg, zseg))
        pts[n.v] = p

        if x < xseg - 1 and y < yseg - 1 and add_xy:
            indices += n.xy()

        if x < xseg - 1 and z < zseg - 1 and add_zx:
            indices += n.zx()

        if z < zseg - 1 and y < yseg - 1 and add_yz:
            indices += n.yz()

    nfaces = len(indices) // 4
    counts = [4] * nfaces

    mesh = UsdGeom.Mesh.Define(stage, Sdf.Path(name))
    mesh.CreatePointsAttr(pts)
    mesh.CreateFaceVertexIndicesAttr(indices)
    mesh.CreateFaceVertexCountsAttr(counts)
    mesh.CreateSubdivisionSchemeAttr(_POLYGONS)

    pv = UsdGeom.PrimvarsAPI(mesh.GetPrim())
    rest = pv.CreatePrimvar("rest", Sdf.ValueTypeNames.Vector3fArray, "vertex", len(pts))
    rest.Set(pts)

    return mesh


def _smooth_mesh(stage, name, pts, normals, indices, counts):
    """A polygon mesh shaded smooth from explicit normals rather than by subdivision.

    subdivisionScheme is "none" so the surface a renderer draws is the one the points
    describe: subdivided, a coarse sphere or torus shrinks inside its own control cage,
    and anything measuring the shape from its points (a bounding box, a silhouette) would
    disagree with the pixels.
    """
    mesh = UsdGeom.Mesh.Define(stage, Sdf.Path(name))
    mesh.CreatePointsAttr(pts)
    mesh.CreateFaceVertexIndicesAttr(indices)
    mesh.CreateFaceVertexCountsAttr(counts)
    mesh.CreateSubdivisionSchemeAttr(_POLYGONS)
    mesh.CreateNormalsAttr(normals)
    mesh.SetNormalsInterpolation(UsdGeom.Tokens.vertex)
    mesh.CreateExtentAttr(UsdGeom.PointBased.ComputeExtent(pts))

    pv = UsdGeom.PrimvarsAPI(mesh.GetPrim())
    rest = pv.CreatePrimvar("rest", Sdf.ValueTypeNames.Vector3fArray, "vertex", len(pts))
    rest.Set(pts)
    return mesh


def build_torus(stage, name="/torus", rings=48, sides=20, radius=1.0, tube_radius=0.4):
    """A closed torus centred on the origin, lying in the XZ plane around +Y.

    `radius` is the distance from the centre to the middle of the tube, `tube_radius` the
    tube's own; the outer diameter is 2 * (radius + tube_radius).
    """
    rings = max(int(rings), 3)
    sides = max(int(sides), 3)

    pts, normals = [], []
    for i in range(rings):
        u = 2.0 * np.pi * i / rings
        for j in range(sides):
            v = 2.0 * np.pi * j / sides
            normal = (np.cos(v) * np.cos(u), np.sin(v), np.cos(v) * np.sin(u))
            normals.append(normal)
            pts.append((radius * np.cos(u) + tube_radius * normal[0], tube_radius * normal[1],
                        radius * np.sin(u) + tube_radius * normal[2]))

    indices, counts = [], []
    for i in range(rings):
        for j in range(sides):
            a = i * sides + j
            b = ((i + 1) % rings) * sides + j
            c = ((i + 1) % rings) * sides + (j + 1) % sides
            d = i * sides + (j + 1) % sides
            indices += [a, d, c, b]  # counter-clockwise seen from outside
            counts.append(4)

    return _smooth_mesh(stage, name, np.array(pts), np.array(normals), indices, counts)


def build_cone(stage, name="/cone", rows=8, columns=32, radius=1.0, height=2.0):
    """A cone standing on its base: the base centred on the origin, the apex on +Y.

    Built like the tube, rings from the base up, with the radius shrinking to nothing at
    the top. The apex is a ring of coincident points rather than one shared point, so each
    column keeps its own smooth normal all the way up. The base is a separate ring with
    its own downward normals, so the cap shades flat instead of borrowing the side's.
    """
    rows = max(int(rows), 2)
    columns = max(int(columns), 3)
    slope = radius / height if height else 0.0

    pts, normals = [], []
    for i in range(rows):
        y = height * i / (rows - 1)
        ring = radius * (1.0 - i / (rows - 1))
        for j in range(columns):
            v = 2.0 * np.pi * j / columns
            pts.append((np.cos(v) * ring, y, np.sin(v) * ring))
            normal = np.array((np.cos(v), slope, np.sin(v)))
            normals.append(normal / np.linalg.norm(normal))

    indices, counts = [], []
    for i in range(rows - 1):
        for j in range(columns):
            a, b = i * columns + j, i * columns + (j + 1) % columns
            indices += [a, a + columns, b + columns, b]  # counter-clockwise seen from outside
            counts.append(4)

    base, centre = len(pts), len(pts) + columns
    for j in range(columns):
        v = 2.0 * np.pi * j / columns
        pts.append((np.cos(v) * radius, 0.0, np.sin(v) * radius))
        normals.append((0.0, -1.0, 0.0))
    pts.append((0.0, 0.0, 0.0))
    normals.append((0.0, -1.0, 0.0))
    for j in range(columns):
        indices += [centre, base + j, base + (j + 1) % columns]
        counts.append(3)

    return _smooth_mesh(stage, name, np.array(pts), np.array(normals), indices, counts)


def build_tube(stage, name="/tube", rows=5, columns=5, radius=1.0, height=2.0, caps=True, pos=(0, 0, 0)):
    """Build a tube for testing hair procedural"""
    rows = max(int(rows), 2)
    columns = max(int(columns), 3)
    rc = rows * columns

    pts = np.zeros((rc, 3))
    for i in range(rows):
        y = (height / (rows - 1)) * i
        for j in range(columns):
            v = (j % columns) / columns * np.pi * 2
            p = (pos[0] + np.sin(v) * radius, pos[1] + y, pos[2] + np.cos(v) * radius)

            pts[i * columns + j] = p

    indices = []
    counts = []

    # CAPS
    if caps:
        pts.resize((rc + 2, 3))
        pts[-2] = pos
        pts[-1] = (pos[0], pos[1] + height, pos[2])
        for i in range(columns):
            indices.append(i + 1 if i + 1 != columns else 0)
            indices.append(i)
            indices.append(rc)
            counts.append(3)

        for i in range(rc - columns, rc):
            indices.append(rc + 1)
            indices.append(i)
            indices.append(i + 1 if i != rc - 1 else rc - columns)
            counts.append(3)

    # SIDES
    for i in range(rows - 1):
        for j in range(columns):
            idx = i * columns + j
            indices.append(idx + 1 if j != columns - 1 else i * columns)
            indices.append(idx + columns + 1 if j != columns - 1 else i * columns + columns)
            indices.append(idx + columns)
            indices.append(idx)
            counts.append(4)

    mesh = UsdGeom.Mesh.Define(stage, Sdf.Path(name))
    mesh.CreatePointsAttr(pts)
    mesh.CreateFaceVertexIndicesAttr(indices)
    mesh.CreateFaceVertexCountsAttr(counts)
    mesh.CreateSubdivisionSchemeAttr(_POLYGONS)

    pv = UsdGeom.PrimvarsAPI(mesh.GetPrim())
    rest = pv.CreatePrimvar("rest", Sdf.ValueTypeNames.Vector3fArray, "vertex", len(pts))
    rest.Set(pts)

    return mesh


def build_sphere(stage, name, dimx, dimy, radius, pos=(0, 0, 0)):
    dimx = max(int(dimx), 3)
    dimy = max(int(dimy), 3)
    rc = dimx * dimy - (dimx - 1) * 2
    indices = []
    counts = []

    pts = np.zeros((rc, 3))
    for i in range(dimx):
        row = (i / dimx) * np.pi * 2
        for j in range(dimy):
            offset = 0 if i == 0 else i + (i - 1)
            idx = int(i * dimy + j - offset)
            nbr = dimy - 1 if i == 0 else dimy - 2

            if j == 0:
                indices.append(0)
                indices.append(idx + 1)
                indices.append(idx + 1 + nbr if i != dimx - 1 else 1)
                counts.append(3)

            elif j == dimy - 2:
                indices.append(idx)
                indices.append(dimy - 1)
                indices.append(idx + nbr if i != dimx - 1 else dimy - 2)
                counts.append(3)

            elif j < dimy - 2:
                indices.append(idx)
                indices.append(idx + 1)
                indices.append(idx + nbr + 1 if i != dimx - 1 else j + 1)
                indices.append(idx + nbr if i != dimx - 1 else j)
                counts.append(4)

            if (j == 0 or j == dimy - 1) and i > 0:
                continue

            theta = (j % dimy) / (dimy - 1) * np.pi
            y = pos[1] + radius * np.sin(theta) * np.sin(row)
            x = pos[0] + radius * np.sin(theta) * np.cos(row)
            z = pos[2] + radius * np.cos(theta)
            pts[idx] = (x, y, z)

    mesh = UsdGeom.Mesh.Define(stage, Sdf.Path(name))
    mesh.CreateFaceVertexIndicesAttr(indices)
    mesh.CreateFaceVertexCountsAttr(counts)
    # Polygons, not a subdivision cage -- see _POLYGONS.
    mesh.CreateSubdivisionSchemeAttr(_POLYGONS)

    mesh.CreatePointsAttr(pts)

    pv = UsdGeom.PrimvarsAPI(mesh.GetPrim())
    rest = pv.CreatePrimvar("rest", Sdf.ValueTypeNames.Vector3fArray, "vertex", len(pts))
    rest.Set(pts)

    return mesh


def menu_changed(owner: chunk.Chunk, notice, sender: chunk.ChunkParamBase):
    selected = sender.value

    for g in owner.get_my_params(chunk.is_group):
        if g.name == selected:
            g.unset_state(chunk.ChunkFlags.State.HIDDEN | chunk.ChunkFlags.State.DISABLED)
        else:
            g.set_state(chunk.ChunkFlags.State.HIDDEN | chunk.ChunkFlags.State.DISABLED)

    if selected == "none":
        clear_layer(owner)

def add_texture(prim: Usd.Prim, texture_file: str | None = None):
    """Bind the UV checker material to prim, with this chunk's checker.png by default."""
    path = texture_file or os.path.join(os.path.dirname(__file__), "checker.png")
    chunk_utils.bind_uv_checker(prim.GetStage(), [prim], path)


def clear_layer(owner: chunk.ChunkCommon):
    ctx = owner.get_edit_context()
    if ctx and (lreg := chunk.get_layer_manager()):
        lreg.clear_layer(ctx.identifier)


def cube_changed(owner: chunk.Group, notice, sender):
    stage = owner.get_stage()

    if not stage:
        return

    if owner.has_state(chunk.ChunkFlags.State.HIDDEN):
        return

    clear_layer(owner)

    xseg = owner.get_param("dimx", chunk.is_param_t(int))
    yseg = owner.get_param("dimy", chunk.is_param_t(int))
    zseg = owner.get_param("dimz", chunk.is_param_t(int))

    width = owner.get_param("width", chunk.is_param_t(float))
    height = owner.get_param("height", chunk.is_param_t(float))
    depth = owner.get_param("depth", chunk.is_param_t(float))

    if not (c := owner.get_chunk()):
        return

    prim_path = c.get_param("prim_path", chunk.is_param_t(str))
    for path in chunk_utils.author_in_variant_contexts(stage, prim_path):
        build_cube(stage, path, xseg.value, yseg.value, zseg.value, width.value, height.value, depth.value)


def tube_changed(owner: chunk.Group, notice, sender):

    if owner.has_state(chunk.ChunkFlags.State.HIDDEN):
        return -1

    stage = owner.get_stage()
    if not stage:
        return

    clear_layer(owner)

    rows = owner.get_param("rows", chunk.is_param_t(int))
    cols = owner.get_param("cols", chunk.is_param_t(int))

    radius = owner.get_param("radius", chunk.is_param_t(float))
    height = owner.get_param("height1", chunk.is_param_t(float))
    caps = owner.get_param("caps", chunk.is_param_t(bool))

    if not (c := owner.get_chunk()):
        return

    prim_path = c.get_param("prim_path", chunk.is_param_t(str))
    for path in chunk_utils.author_in_variant_contexts(stage, prim_path):
        build_tube(stage, path, rows.value, cols.value, radius.value, height.value, caps.value)


def torus_changed(owner: chunk.Group, notice, sender):
    stage = owner.get_stage()
    if not stage or owner.has_state(chunk.ChunkFlags.State.HIDDEN):
        return

    clear_layer(owner)

    if not (c := owner.get_chunk()):
        return

    rings = owner.get_param("torus_rings", chunk.is_param_t(int))
    sides = owner.get_param("torus_sides", chunk.is_param_t(int))
    radius = owner.get_param("torus_radius", chunk.is_param_t(float))
    tube_radius = owner.get_param("torus_tube_radius", chunk.is_param_t(float))

    prim_path = c.get_param("prim_path", chunk.is_param_t(str))
    for path in chunk_utils.author_in_variant_contexts(stage, prim_path):
        build_torus(stage, path, rings.value, sides.value, radius.value, tube_radius.value)


def cone_changed(owner: chunk.Group, notice, sender):
    stage = owner.get_stage()
    if not stage or owner.has_state(chunk.ChunkFlags.State.HIDDEN):
        return

    clear_layer(owner)

    if not (c := owner.get_chunk()):
        return

    rows = owner.get_param("cone_rows", chunk.is_param_t(int))
    cols = owner.get_param("cone_cols", chunk.is_param_t(int))
    radius = owner.get_param("cone_radius", chunk.is_param_t(float))
    height = owner.get_param("cone_height", chunk.is_param_t(float))

    prim_path = c.get_param("prim_path", chunk.is_param_t(str))
    for path in chunk_utils.author_in_variant_contexts(stage, prim_path):
        build_cone(stage, path, rows.value, cols.value, radius.value, height.value)


def grid_changed(owner: chunk.Group, notice, sender):
    stage = owner.get_stage()

    if not stage:
        return

    if owner.has_state(chunk.ChunkFlags.State.HIDDEN):
        return

    clear_layer(owner)

    dimx = owner.get_param("dimx1", chunk.is_param_t(int))
    dimy = owner.get_param("dimy1", chunk.is_param_t(int))
    width = owner.get_param("width1", chunk.is_param_t(float))
    height = owner.get_param("height2", chunk.is_param_t(float))
    topo = owner.get_param("topo", chunk.is_param_t(str))

    topo_val = Topo.QUAD

    if topo.value == "tris":
        topo_val = Topo.TRIS

    if not (c := owner.get_chunk()):
        return

    prim_path = c.get_param("prim_path", chunk.is_param_t(str))
    for path in chunk_utils.author_in_variant_contexts(stage, prim_path):
        grid = build_plane(stage, path, dimx.value, dimy.value, width.value, height.value, topo=topo_val)
        if grid:
            add_texture(grid.GetPrim())


def sphere_changed(owner: chunk.Group, notice, sender):
    stage = owner.get_stage()

    if not stage:
        return

    if owner.has_state(chunk.ChunkFlags.State.HIDDEN):
        return

    clear_layer(owner)

    dimx = owner.get_param("dimx2", chunk.is_param_t(int))
    dimy = owner.get_param("dimy2", chunk.is_param_t(int))
    rad = owner.get_param("radius1", chunk.is_param_t(float))

    raw_path = "/sphere"
    if c := owner.get_chunk():
        raw_path = c.get_param("prim_path", chunk.is_param_t(str)).value

    with chunk_utils.author_in_variant_context(stage, raw_path) as path:
        build_sphere(stage, path, dimx.value, dimy.value, rad.value)


def execute(node: chunk.Chunk) -> None:
    """Reserved top-level reactive hook -- load_chunks calls this once per chunk after
    bulk-restoring its param values, so a reloaded Mesh actually rebuilds its shape
    instead of leaving the target prim empty (the real work lives in cube_changed/
    tube_changed/grid_changed, all add_callback handlers, which load's value-restore
    deliberately does not trigger).

    menu_changed's hidden/disabled bookkeeping is reapplied first: live, picking a mesh
    type only un-hides its group, it doesn't itself trigger a build (that needs a param
    inside the group, or prim_path, to also change) -- so mesh_type's restored value alone
    needs both steps repeated here to end up matching what was actually visible/built
    before saving.
    """
    menu = node.get_param("mesh_type", chunk.is_param_t(str))
    if menu is None:
        return
    menu_changed(node, None, menu)
    build_fn = {
        "cube": cube_changed, "sphere": sphere_changed, "torus": torus_changed, "cone": cone_changed,
        "tube": tube_changed, "grid": grid_changed,
    }.get(menu.value)
    if build_fn is None:
        return
    group = next((g for g in node.get_my_params(chunk.is_group) if g.name == menu.value), None)
    if group is None:
        return
    build_fn(group, None, None)


def main():
    path = chunk.LineEdit("prim_path", "Prim Path", default_value="/mesh")
    menu = chunk.Menu(
        "mesh_type", "Mesh Type", menu_items=["none", "cube", "sphere", "torus", "cone", "grid", "tube"], default_value="none"
    )

    cube_group = chunk.Group("cube", "Cube")
    cube_group.add_params(
        [
            chunk.Spinbox("dimx", min_value=1, default_value=2, is_int=True),
            chunk.Spinbox("dimy", min_value=1, default_value=2, is_int=True),
            chunk.Spinbox("dimz", min_value=1, default_value=2, is_int=True),
            chunk.Slider("width", default_value=1),
            chunk.Slider("height", default_value=1),
            chunk.Slider("depth", default_value=1),
        ]
    )
    cube_group.set_state(chunk.ChunkFlags.State.HIDDEN | chunk.ChunkFlags.State.DISABLED)
    cube_group.add_callback(cube_changed)
    cube_group.add_callback(cube_changed, path, notice_alias=chunk.ChunkNoticeAlias.VALUE_CHANGED)

    sphere_group = chunk.Group("sphere", "Sphere")
    sphere_group.add_params(
        [
            chunk.Spinbox("dimx2", "dimx", min_value=3, default_value=10, is_int=True),
            chunk.Spinbox("dimy2", "dimy", min_value=3, default_value=10, is_int=True),
            chunk.Slider("radius1", "radius", default_value=1),
        ]
    )
    sphere_group.set_state(chunk.ChunkFlags.State.HIDDEN | chunk.ChunkFlags.State.DISABLED)
    sphere_group.add_callback(sphere_changed)
    sphere_group.add_callback(sphere_changed, path, notice_alias=chunk.ChunkNoticeAlias.VALUE_CHANGED)

    # Params named per shape: get_param by bare name returns the first match, and the
    # tube already has a "radius".
    torus_group = chunk.Group("torus", "Torus")
    torus_group.add_params(
        [
            chunk.Slider("torus_rings", "rings", min_value=3, max_value=256, default_value=48, is_int=True),
            chunk.Slider("torus_sides", "sides", min_value=3, max_value=128, default_value=20, is_int=True),
            chunk.Slider("torus_radius", "radius", min_value=0.001, max_value=1000.0, default_value=1.0),
            chunk.Slider("torus_tube_radius", "tube radius", min_value=0.001, max_value=1000.0, default_value=0.4),
        ]
    )
    torus_group.set_state(chunk.ChunkFlags.State.HIDDEN | chunk.ChunkFlags.State.DISABLED)
    torus_group.add_callback(torus_changed)
    torus_group.add_callback(torus_changed, path, notice_alias=chunk.ChunkNoticeAlias.VALUE_CHANGED)

    cone_group = chunk.Group("cone", "Cone")
    cone_group.add_params(
        [
            chunk.Slider("cone_rows", "rows", min_value=2, max_value=128, default_value=8, is_int=True),
            chunk.Slider("cone_cols", "columns", min_value=3, max_value=256, default_value=32, is_int=True),
            chunk.Slider("cone_radius", "radius", min_value=0.001, max_value=1000.0, default_value=1.0),
            chunk.Slider("cone_height", "height", min_value=0.001, max_value=1000.0, default_value=2.0),
        ]
    )
    cone_group.set_state(chunk.ChunkFlags.State.HIDDEN | chunk.ChunkFlags.State.DISABLED)
    cone_group.add_callback(cone_changed)
    cone_group.add_callback(cone_changed, path, notice_alias=chunk.ChunkNoticeAlias.VALUE_CHANGED)

    tube_group = chunk.Group("tube", "Tube")
    tube_group.add_params(
        [
            chunk.Spinbox("rows", min_value=2, default_value=5, is_int=True),
            chunk.Spinbox("cols", min_value=2, default_value=5, is_int=True),
            chunk.Slider("radius", min_value=1, default_value=1),
            chunk.Slider("height1", "height", default_value=2),
            chunk.Checkbox("caps", default_value=False),
        ]
    )
    tube_group.set_state(chunk.ChunkFlags.State.HIDDEN | chunk.ChunkFlags.State.DISABLED)
    tube_group.add_callback(tube_changed)
    tube_group.add_callback(tube_changed, path, notice_alias=chunk.ChunkNoticeAlias.VALUE_CHANGED)

    grid_group = chunk.Group("grid", "Grid")
    grid_group.add_params(
        [
            chunk.Spinbox("dimx1", "dimx", min_value=2, default_value=5, is_int=True),
            chunk.Spinbox("dimy1", "dimy", min_value=2, default_value=5, is_int=True),
            chunk.Slider("width1", "width", default_value=1),
            chunk.Slider("height2", "height", default_value=1),
            chunk.Menu("topo", default_value="quads", menu_items=["tris", "quads"]),
        ]
    )
    grid_group.set_state(chunk.ChunkFlags.State.HIDDEN | chunk.ChunkFlags.State.DISABLED)
    grid_group.add_callback(grid_changed)
    grid_group.add_callback(grid_changed, path, notice_alias=chunk.ChunkNoticeAlias.VALUE_CHANGED)

    chunk_api = chunk.Chunk("Mesh")
    chunk_api.add_params([path, menu, cube_group, sphere_group, torus_group, cone_group, tube_group, grid_group])
    chunk_api.add_callback(menu_changed, sender=menu)

    return chunk_api


def config():
    # numpy is imported at module scope, so without it the registry skips this chunk
    # entirely -- declaring it turns that silence into a named package.
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.MODELLING, requirements=["numpy>=2,<3"],
        # One entry per shape, so "Cube" is found and created directly rather than by
        # creating Mesh and re-picking the type. Ground is a grid preset, sized to stand
        # things on.
        variants=[
            ("Cube", {"mesh_type": "cube", "prim_path": "/cube"}),
            ("Sphere", {"mesh_type": "sphere", "prim_path": "/sphere"}),
            ("Torus", {"mesh_type": "torus", "prim_path": "/torus"}),
            ("Cone", {"mesh_type": "cone", "prim_path": "/cone"}),
            ("Grid", {"mesh_type": "grid", "prim_path": "/grid"}),
            ("Tube", {"mesh_type": "tube", "prim_path": "/tube"}),
            ("Ground", {"mesh_type": "grid", "prim_path": "/ground", "width1": 20.0, "height2": 20.0}),
        ],
    )
