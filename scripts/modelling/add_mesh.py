from pxr import Usd, UsdGeom, Sdf, UsdShade
from usd_chunks import chunk, chunk_registry
import itertools
import numpy as np
import os
from enum import IntEnum

from typing import Optional


class Topo(IntEnum):
    TRIS = 3
    QUAD = 4


def build_plane(stage, name="/plane", divx=2, divy=2, width=1, height=1, pos=(0, 0, 0), topo: Topo = Topo.QUAD):
    """
    Build a simple plane for testing hair procedural
    """

    w = max(divx, 2)
    h = max(divy, 2)

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
    width=1,
    height=1,
    depth=1,
    pos=(0, 0, 0),
):
    xseg = max(xseg, 1)
    yseg = max(yseg, 1)
    zseg = max(zseg, 1)

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


def build_tube(stage, name="/tube", rows=5, columns=5, radius=1, height=2, caps=True, pos=(0, 0, 0)):
    """
    Build a tube for testing hair procedural
    """
    rows = max(rows, 2)
    columns = max(columns, 3)
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
    if sender is not owner.get_param("mesh_type"):
        return

    groups = [x for x in owner.get_my_params() if isinstance(x, chunk.Group)]
    selected = sender.value

    for g in groups:
        if g.name == selected:
            g.unset_state(chunk.ChunkFlags.State.HIDDEN)
        else:
            g.set_state(chunk.ChunkFlags.State.HIDDEN)


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


def cube_changed(owner: chunk.Group, notice, sender):
    stage = owner.get_stage()

    if not stage:
        return

    layer = owner.get_edit_context()
    if not layer:
        return None

    if owner.has_state(chunk.ChunkFlags.State.HIDDEN):
        chunk_registry.ChunkLayerManager().clear_layer(layer.identifier)

        return

    xseg = owner.get_param("dimx")
    yseg = owner.get_param("dimy")
    zseg = owner.get_param("dimz")

    if not xseg or not yseg or not zseg:
        return

    width = owner.get_param("width")
    height = owner.get_param("height")
    depth = owner.get_param("depth")

    if not width or not height or not depth:
        return

    with Usd.EditContext(stage, layer):
        build_cube(
            owner.get_stage(), "/cube", xseg.value, yseg.value, zseg.value, width.value, height.value, depth.value
        )


def tube_changed(owner: chunk.Group, notice, sender):
    stage = owner.get_stage()

    if not stage:
        return

    layer = owner.get_edit_context()
    if not layer:
        return None

    if owner.has_state(chunk.ChunkFlags.State.HIDDEN):
        chunk_registry.ChunkLayerManager().clear_layer(layer.identifier)
        return

    rows = owner.get_param("rows")
    cols = owner.get_param("cols")

    radius = owner.get_param("radius")
    height = owner.get_param("height1")
    caps = owner.get_param("caps")

    with Usd.EditContext(stage, layer):
        build_tube(owner.get_stage(), "/tube", rows.value, cols.value, radius.value, height.value, caps.value)


def grid_changed(owner: chunk.Group, notice, sender):
    stage = owner.get_stage()

    if not stage:
        return

    layer = owner.get_edit_context()

    if owner.has_state(chunk.ChunkFlags.State.HIDDEN):
        chunk_registry.ChunkLayerManager().clear_layer(layer.identifier)

        return

    dimx = owner.get_param("dimx1")
    dimy = owner.get_param("dimy1")

    width = owner.get_param("width1")
    height = owner.get_param("height2")
    topo = owner.get_param("topo")

    topo_val = Topo.QUAD

    if topo and topo.value == "tris":
        topo_val = Topo.TRIS

    with Usd.EditContext(stage, layer):
        grid = build_plane(
            owner.get_stage(), "/plane", dimx.value, dimy.value, width.value, height.value, topo=topo_val
        )
        if grid:
            add_texture(grid.GetPrim())


def main():
    path = chunk.LineEdit("prim_path", "Prim Patm", default_value="/mesh")

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
    cube_group.set_state(chunk.ChunkFlags.State.HIDDEN)
    cube_group.add_callback(cube_changed)

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
    tube_group.set_state(chunk.ChunkFlags.State.HIDDEN)
    tube_group.add_callback(tube_changed)

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
    grid_group.set_state(chunk.ChunkFlags.State.HIDDEN)
    grid_group.add_callback(grid_changed)

    chunk_api = chunk.Chunk("Add Mesh", category="Modelling")
    chunk_api.add_params([path, menu, cube_group, tube_group, grid_group])
    chunk_api.add_callback(menu_changed, sender=menu)

    return chunk_api
