from pxr import Usd, UsdGeom, Sdf
from usd_chunks import chunk, chunk_registry, chunk_notice

import numpy as np
import usd_chunks.chunk_core


def build_hair(stage, target, count=15, seg=2, length=0.2, path="/curves", faces=None):
    """
    Create hair primitives on a target UsdPrim. Choose what faces to apply the hair to
    for testing face indexes.
    """
    counts = target.GetFaceVertexCountsAttr().Get()
    indices = target.GetFaceVertexIndicesAttr().Get()
    pts = target.GetPointsAttr().Get()

    if faces is None:
        faces = range(len(counts))

    # On avarage, howmany curves per face to add
    avg_curves = count / len(faces)

    # The increment in length to add to each segment
    seg_incr = length / seg

    curve_cnt = [seg] * count
    curve_pts = np.zeros((count, seg, 3))
    curve_w = np.full((1, count * seg), 0.03)

    # create some random points in uv space. These will be our roots
    r = np.random.random((count, 2))

    carried = 0
    c_offset = 0
    pt_offset = 0

    for i in faces:
        # find how many curves to add to this face
        li = int(np.rint(avg_curves + carried))

        # the next face should account for the loss of guides on this face
        carried += avg_curves - li
        if li == 0:
            continue

        # how many vertices on this face
        c = counts[i]

        # find all vertices on this face
        p = np.zeros((c, 3))
        for j in range(c):
            p[j] = pts[indices[j + c_offset]]
        c_offset += c

        # compute face normal
        e = p[1:] - p[0]
        n = np.cross(e[0], e[1])
        d = np.dot(n, n)

        if d != 0.0:
            n /= np.sqrt(d)

        # loop over 0..li where li is how many curves to add to the face i. Store roots in curves[j][0]
        # curves = np.zeros((li, seg, 3), dtype=np.float64)
        for j in range(pt_offset, pt_offset + li):
            u = r[j][0]
            v = r[j][1]
            if c == 3:
                # triange parameterization
                u /= 2
                v /= 2
                w = 1 - u - v
                curve_pts[j][0] = p[0] * w + p[1] * u + p[2] * v
            elif c == 4:
                # quad parameterization
                curve_pts[j][0] = p[0] * (1 - u) * (1 - v) + p[1] * (1 - u) * v + p[2] * u * v + p[3] * u * (1 - v)
            else:
                raise ValueError(f"Face vertex count {c} is not supported. Should be either 3 or 4")

            # add the segments after the root
            for s in range(1, seg):
                ns = curve_pts[j][0] + n * ((s + 1) * seg_incr)
                curve_pts[j][s] = ns

        pt_offset += li

    hair = UsdGeom.BasisCurves.Define(stage, Sdf.Path(path))
    hair.CreatePointsAttr(curve_pts)
    hair.CreateCurveVertexCountsAttr(curve_cnt)
    hair.CreateWidthsAttr(curve_w)
    hair.CreateTypeAttr("linear")

    cd = np.random.random((3, 1))
    hair.CreateDisplayColorAttr(cd)
    return hair


def curl_hair(curves: UsdGeom.BasisCurves, freq: float, amp: float):
    curve_cnt = curves.GetCurveVertexCountsAttr().Get()
    curve_pts = curves.GetPointsAttr().Get()

    offset = 0
    for i, c in enumerate(curve_cnt):
        for j in range(1, c):
            curl = i + (j / (c - 1)) * 3.14 * amp

            pt = curve_pts[offset + j]
            pt[0] += (np.cos(curl) - np.sin(curl)) * freq
            pt[1] += (np.sin(curl) + np.cos(curl)) * freq

            curve_pts[offset + j] = pt

        offset += c
    curves.GetPointsAttr().Set(curve_pts)


def menu_changed(owner: chunk.Chunk, notice, sender: chunk.ChunkParamBase):
    param = notice.data

    if sender is not owner.get_param("mesh_type"):
        return

    groups = [x for x in owner.get_my_params() if isinstance(x, chunk.Group)]
    selected = param.value

    ctx = owner.get_edit_context()
    if ctx and chunk_registry.ChunkLayerManager().initialized:
        chunk_registry.ChunkLayerManager().clear_layer(ctx.identifier)

    for g in groups:
        if g.name == selected:
            g.unset_state(chunk.ChunkFlags.State.HIDDEN | chunk.ChunkFlags.State.DISABLED)
        else:
            g.set_state(chunk.ChunkFlags.State.HIDDEN | chunk.ChunkFlags.State.DISABLED)


def hair_changed(owner: chunk.ChunkGroupBase, notice, sender):
    if owner.has_state(chunk.ChunkFlags.State.HIDDEN):
        return

    stage = owner.get_stage()

    if not stage:
        return

    count = owner.get_param("count")
    segs = owner.get_param("segments")
    len = owner.get_param("length")

    tgt = chunk_registry.ChunkRegistry().scene.get_selected_prims()

    if not tgt:
        return

    tgtPrim = tgt[0]
    mesh = UsdGeom.Mesh.Get(stage, tgtPrim.GetPath())
    if not mesh:
        return

    build_hair(owner.get_stage(), mesh, count.value, segs.value, len.value)


def main():
    menu = chunk.Menu("mesh_type", "Mesh Type", menu_items=["none", "curly"], default_value="none")

    curly_group = chunk.Group("curly", "curly")
    curly_group.add_params(
        [
            # chunk.LineEdit("target", "Target Prim", default_value="/"),
            chunk.Spinbox("count", min_value=1, default_value=10, is_int=True),
            chunk.Spinbox("segments", min_value=2, default_value=3, is_int=True),
            chunk.Slider("length", default_value=1),
        ]
    )
    curly_group.set_state(usd_chunks.chunk_core.ChunkFlags.State.HIDDEN | chunk.ChunkFlags.State.DISABLED)
    curly_group.add_callback(hair_changed)

    chunk_api = chunk.Chunk("Add Hair")
    chunk_api.add_params([menu, curly_group])

    chunk_api.add_callback(menu_changed, sender=menu)

    return chunk_api


def config():
    return chunk.DefaulConfig(category=chunk.StandardCategory.MODELLING)
