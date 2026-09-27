from pxr import Ar, Usd, UsdGeom, Sdf, UsdShade
from usd_chunks import chunk, chunk_utils
import itertools
import logging
import numpy as np
import os
from enum import IntEnum

from typing import Optional

logger = logging.getLogger(__name__)


def texture_file_is_usable(path: str) -> bool:
    """Whether a UsdUVTexture authored with *path* will actually have an image to load.

    A texture whose file cannot be resolved is not merely a missing image: the shader
    network still carries the whole UV chain (primvar reader -> UsdTransform2d -> the
    texture's `st`), and a render delegate that drops the unloadable texture node while
    still translating what fed it is left holding a connection to a node it never built.
    hdCycles fails exactly that way -- "Failed to load 1 image files" followed by
    "Could not find node '/Mapping' to connect" out of BuildShaderGraph (its Cycles
    `mapping` node being the translation of our UsdTransform2d) -- and takes usdview down
    with it on a delegate switch. So the chain is only worth authoring when the image is
    really there.

    A path carrying a `<UDIM>` (or any other `<token>`) is left alone: those never resolve
    as a plain file and are meant not to, so treating them as missing would break a
    perfectly valid texture set.
    """
    if not path:
        return False
    if "<" in path and ">" in path:
        return True  # UDIM / token pattern -- not resolvable as a single file by design
    return bool(Ar.GetResolver().Resolve(path))


class Topo(IntEnum):
    TRIS = 3
    QUAD = 4


def build_plane(stage, name="/plane", divx=2, divy=2, width=1.0, height=1.0, pos=(0, 0, 0), topo: Topo = Topo.QUAD):
    """
    Build a simple plane for testing hair procedural
    """

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
        if False:
            uoffset = (i // ipf) % (w - 1)
            voffset = (i // (ipf * (w - 1))) % (h - 1)

            pt = i % ipf
            p = pts[ids[pt]]

            u = p[0] - round(p[0] - 0.5) + uoffset
            v = p[1] - round(p[1] - 0.5) + voffset
        else:
            p = pts[ids[i]]
            u = p[0]
            v = p[1]

        uvs[i] = (u, v)

    primpath = Sdf.Path(name)
    mesh = UsdGeom.Mesh.Define(stage, primpath)
    mesh.CreatePointsAttr(pts)
    mesh.CreateFaceVertexIndicesAttr(ids)
    mesh.CreateFaceVertexCountsAttr(counts)

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

    pv = UsdGeom.PrimvarsAPI(mesh.GetPrim())
    rest = pv.CreatePrimvar("rest", Sdf.ValueTypeNames.Vector3fArray, "vertex", len(pts))
    rest.Set(pts)

    return mesh


def build_tube(stage, name="/tube", rows=5, columns=5, radius=1.0, height=2.0, caps=True, pos=(0, 0, 0)):
    """
    Build a tube for testing hair procedural
    """
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


def add_texture(prim: Usd.Prim, texture_file: Optional[str] = None):
    x_scale = 1
    y_scale = 1

    path_to_uv = texture_file
    if not path_to_uv:
        path_to_uv = os.path.sep.join([os.path.dirname(__file__), "checker.png"])

    # Create an group to hold the UV checker matirial
    stage = prim.GetStage()

    base_material_grp = "/materials"
    material_path = f"{base_material_grp}/UVCheckerMaterial"
    surface_path = f"{material_path}/previewSurface"
    texture_path = f"{material_path}/texture"
    transform2d_path = f"{material_path}/transform2d"
    reader_path = f"{material_path}/reader"

    # Create the material, shaders and its id
    stage.DefinePrim(base_material_grp, "Scope")
    material = UsdShade.Material.Define(stage, material_path)

    preview = UsdShade.Shader.Define(stage, surface_path)
    stage.GetPrimAtPath(surface_path).GetAttribute("info:id").Set("UsdPreviewSurface")

    if not texture_file_is_usable(path_to_uv):
        # No image to sample: bind a plain surface rather than a texture network whose
        # image cannot load -- see texture_file_is_usable for what that costs downstream.
        logger.warning("texture file does not resolve, binding a plain surface: %s", path_to_uv)
        preview.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set((0.5, 0.5, 0.5))
        preview_output_surface = preview.CreateOutput("surface", Sdf.ValueTypeNames.Token)
        material.GetOutput("surface").ConnectToSource(preview_output_surface)
        api = UsdShade.MaterialBindingAPI.Apply(prim)
        api.Bind(material, UsdShade.Tokens.strongerThanDescendants)
        return

    texture = UsdShade.Shader.Define(stage, texture_path)
    stage.GetPrimAtPath(texture_path).GetAttribute("info:id").Set("UsdUVTexture")

    texture.CreateInput("wrapS", Sdf.ValueTypeNames.Token).Set("repeat")
    texture.CreateInput("wrapT", Sdf.ValueTypeNames.Token).Set("repeat")

    transform2d = UsdShade.Shader.Define(stage, transform2d_path)
    stage.GetPrimAtPath(transform2d_path).GetAttribute("info:id").Set("UsdTransform2d")
    reader = UsdShade.Shader.Define(stage, reader_path)
    stage.GetPrimAtPath(reader_path).GetAttribute("info:id").Set("UsdPrimvarReader_float2")

    # Create the inputs and outputs for the shaders
    preview_output_surface = preview.CreateOutput("surface", Sdf.ValueTypeNames.Token)
    material.GetOutput("surface").ConnectToSource(preview_output_surface)

    preview_in_diff = preview.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f)
    texture_out_rgb = texture.CreateOutput("rgb", Sdf.ValueTypeNames.Color3f)
    preview_in_diff.ConnectToSource(texture_out_rgb)

    texture.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(path_to_uv)
    transform2d_out_result = transform2d.CreateOutput("result", Sdf.ValueTypeNames.Float2)
    texture.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(transform2d_out_result)

    transform2d.CreateInput("scale", Sdf.ValueTypeNames.Float2).Set((x_scale, y_scale))
    transform2d_in_uv = transform2d.CreateInput("in", Sdf.ValueTypeNames.Float2)
    reader_out = reader.CreateOutput("result", Sdf.ValueTypeNames.Float2)
    transform2d_in_uv.ConnectToSource(reader_out)
    reader.CreateInput("varname", Sdf.ValueTypeNames.String).Set("st")

    api = UsdShade.MaterialBindingAPI.Apply(prim)
    api.Bind(material, UsdShade.Tokens.strongerThanDescendants)


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

    raw_path = "/cube"
    if c := owner.get_chunk():
        raw_path = c.get_param("prim_path", chunk.is_param_t(str)).value

    with chunk_utils.author_in_variant_context(stage, raw_path) as path:
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

    raw_path = "/tube"
    if c := owner.get_chunk():
        raw_path = c.get_param("prim_path", chunk.is_param_t(str)).value

    with chunk_utils.author_in_variant_context(stage, raw_path) as path:
        build_tube(stage, path, rows.value, cols.value, radius.value, height.value, caps.value)


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

    raw_path = "/plane"
    if c := owner.get_chunk():
        raw_path = c.get_param("prim_path", chunk.is_param_t(str)).value

    with chunk_utils.author_in_variant_context(stage, raw_path) as path:
        grid = build_plane(stage, path, dimx.value, dimy.value, width.value, height.value, topo=topo_val)
        if grid:
            add_texture(grid.GetPrim())


def execute(node: chunk.Chunk) -> None:
    """Reserved top-level reactive hook -- load_chunks calls this once per chunk after
    bulk-restoring its param values, so a reloaded Add Mesh actually rebuilds its shape
    instead of leaving the target prim empty (the real work lives in cube_changed/
    tube_changed/grid_changed, all add_callback handlers, which load's value-restore
    deliberately does not trigger).

    menu_changed's hidden/disabled bookkeeping is reapplied first: live, picking a mesh
    type only un-hides its group, it doesn't itself trigger a build (that needs a param
    inside the group, or prim_path, to also change) -- so mesh_type's restored value alone
    needs both steps repeated here to end up matching what was actually visible/built
    before saving."""
    menu = node.get_param("mesh_type", chunk.is_param_t(str))
    if menu is None:
        return
    menu_changed(node, None, menu)
    build_fn = {"cube": cube_changed, "tube": tube_changed, "grid": grid_changed}.get(menu.value)
    if build_fn is None:
        return
    group = next((g for g in node.get_my_params(chunk.is_group) if g.name == menu.value), None)
    if group is None:
        return
    build_fn(group, None, None)


def main():
    path = chunk.LineEdit("prim_path", "Prim Path", default_value="/mesh")
    menu = chunk.Menu(
        "mesh_type", "Mesh Type", menu_items=["none", "cube", "sphere", "grid", "tube"], default_value="none"
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

    chunk_api = chunk.Chunk("Add Mesh")
    chunk_api.add_params([path, menu, cube_group, tube_group, grid_group])
    chunk_api.add_callback(menu_changed, sender=menu)

    return chunk_api


def config():
    # numpy is imported at module scope, so without it the registry skips this chunk
    # entirely -- declaring it turns that silence into a named package.
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.MODELLING, requirements=["numpy>=2,<3"]
    )
