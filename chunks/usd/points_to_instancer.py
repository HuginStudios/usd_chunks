"""Points to Instancer chunk -- an animated Points prim as instanced meshes, for renderers
that draw no points.

Aurora draws no UsdGeom.Points, and V-Ray draws each one as a single pixel however wide. Both
draw a PointInstancer of a mesh, so this writes one: one instance per point, every frame the
points have, positioned and sized from the points' positions and widths.

Aurora also skips an implicit Sphere prototype and ignores per-instance colour and opacity.
So the built-in prototype is a low-poly mesh sphere with one material (Colour, Opacity), and
Shrink with Opacity scales each instance by its point's displayOpacity, so a puff that fades
out still disappears. The per-point colour and opacity are copied onto the instancer as
primvars too, for the renderers that read them.
"""

from __future__ import annotations

import math

import numpy as np
from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade, Vt

from usd_chunks import chunk, sim_bake

_PROTO_RADIUS = 0.5  # so an instance's scale is its point's width


def _sphere_mesh(stage: Usd.Stage, path: Sdf.Path, rings: int = 6, segments: int = 10) -> UsdGeom.Mesh:
    """A low-poly UV sphere mesh of radius _PROTO_RADIUS at path."""
    r = _PROTO_RADIUS
    points = [Gf.Vec3f(0, 0, r)]
    for i in range(1, rings):
        theta = math.pi * i / rings
        for j in range(segments):
            phi = 2 * math.pi * j / segments
            points.append(Gf.Vec3f(r * math.sin(theta) * math.cos(phi), r * math.sin(theta) * math.sin(phi),
                                   r * math.cos(theta)))
    points.append(Gf.Vec3f(0, 0, -r))
    bottom = len(points) - 1
    counts, indices = [], []
    for j in range(segments):
        counts.append(3)
        indices += [0, 1 + j, 1 + (j + 1) % segments]
    for i in range(rings - 2):
        a, b = 1 + i * segments, 1 + (i + 1) * segments
        for j in range(segments):
            counts.append(4)
            indices += [a + j, b + j, b + (j + 1) % segments, a + (j + 1) % segments]
    a = 1 + (rings - 2) * segments
    for j in range(segments):
        counts.append(3)
        indices += [a + j, bottom, a + (j + 1) % segments]
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.CreatePointsAttr(Vt.Vec3fArray(points))
    mesh.CreateFaceVertexCountsAttr(Vt.IntArray(counts))
    mesh.CreateFaceVertexIndicesAttr(Vt.IntArray(indices))
    mesh.CreateNormalsAttr(Vt.Vec3fArray([p / r for p in points]))
    mesh.SetNormalsInterpolation(UsdGeom.Tokens.vertex)
    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
    mesh.CreateExtentAttr(Vt.Vec3fArray([Gf.Vec3f(-r), Gf.Vec3f(r)]))
    return mesh


def _material(stage: Usd.Stage, path: Sdf.Path, colour, opacity: float) -> UsdShade.Material:
    """A matte UsdPreviewSurface material with one colour and opacity."""
    material = UsdShade.Material.Define(stage, path)
    shader = UsdShade.Shader.Define(stage, path.AppendChild("surface"))
    shader.CreateIdAttr("UsdPreviewSurface")
    shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*colour))
    shader.CreateInput("opacity", Sdf.ValueTypeNames.Float).Set(float(opacity))
    shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(1.0)
    material.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
    return material


def _per_point(primvar: UsdGeom.Primvar | None, time, count: int, width: int):
    """A primvar's value at time as a (count, width) array, a constant one repeated; or None."""
    if not primvar or not primvar.HasValue():
        return None
    value = primvar.ComputeFlattened(time)
    if value is None or len(value) == 0:
        return None
    arr = np.asarray(value, dtype=np.float32).reshape(-1, width)
    if len(arr) == count:
        return arr
    return np.repeat(arr[:1], count, axis=0) if len(arr) == 1 else None


def _read_frames(points: UsdGeom.Points, every: int):
    """Each frame of points as (time, positions, widths, ids, velocities, colours, opacities),
    keeping every Nth point (by id, so the same points stay from frame to frame)."""
    attr = points.GetPointsAttr()
    times = attr.GetTimeSamples() or [Usd.TimeCode.Default()]
    primvars = UsdGeom.PrimvarsAPI(points)
    colour_pv, opacity_pv = primvars.GetPrimvar("displayColor"), primvars.GetPrimvar("displayOpacity")
    frames = []
    for t in times:
        pos = np.asarray(attr.Get(t) or [], dtype=np.float32).reshape(-1, 3)
        n = len(pos)
        widths = np.asarray(points.GetWidthsAttr().Get(t) or [], dtype=np.float32)
        widths = widths if len(widths) == n else np.full(n, widths[0] if len(widths) else 1.0, np.float32)
        ids = np.asarray(points.GetIdsAttr().Get(t) or [], dtype=np.int64)
        ids = ids if len(ids) == n else None
        vel = np.asarray(points.GetVelocitiesAttr().Get(t) or [], dtype=np.float32).reshape(-1, 3)
        vel = vel if len(vel) == n else None
        colours = _per_point(colour_pv, t, n, 3)
        opacities = _per_point(opacity_pv, t, n, 1)
        if every > 1 and n:
            keep = (ids % every == 0) if ids is not None else (np.arange(n) % every == 0)
            pos, widths = pos[keep], widths[keep]
            ids = ids[keep] if ids is not None else None
            vel = vel[keep] if vel is not None else None
            colours = colours[keep] if colours is not None else None
            opacities = opacities[keep] if opacities is not None else None
        frames.append((t, pos, widths, ids, vel, colours, opacities))
    return frames


def execute(node) -> None:
    stage = node.get_stage()
    readout = node.get_param("readout")
    source = stage.GetPrimAtPath(node.get_param("prim_path").value.strip() or "/") if stage else None
    location = node.get_param("primpath_location").value.strip()
    if not source or not source.IsA(UsdGeom.Points) or not Sdf.Path.IsValidPathString(location):
        readout.value = "pick a Points prim" if not (source and source.IsA(UsdGeom.Points)) else "set a Location"
        return
    out_path = Sdf.Path(location)

    every = max(1, int(node.get_param("every_nth").value))
    size = float(node.get_param("size_scale").value)
    shrink = bool(node.get_param("shrink_with_opacity").value)
    frames = _read_frames(UsdGeom.Points(source), every)
    peak = max((float(o.max()) for *_, o in frames if o is not None and len(o)), default=0.0)

    inst = UsdGeom.PointInstancer.Define(stage, out_path)
    # Line the instances up with the points: the source's transform, under Location's parent.
    parent = stage.GetPrimAtPath(out_path.GetParentPath())
    src_world = UsdGeom.Xformable(source).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
    parent_world = (UsdGeom.Xformable(parent).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
                    if parent and parent.IsA(UsdGeom.Xformable) else Gf.Matrix4d(1))
    to_parent = src_world * parent_world.GetInverse()
    if not np.allclose(np.asarray(to_parent), np.eye(4), atol=1e-9):
        inst.AddTransformOp().Set(to_parent)

    proto_src = node.get_param("proto_prim").value.strip()
    protos = out_path.AppendChild("Prototypes")
    if proto_src and stage.GetPrimAtPath(proto_src):
        proto = stage.DefinePrim(protos.AppendChild("proto"))
        proto.GetReferences().AddInternalReference(Sdf.Path(proto_src))
        radius = 0.5  # a prototype of about unit size: scale = width
    else:
        proto = _sphere_mesh(stage, protos.AppendChild("sphere")).GetPrim()
        mat = _material(stage, protos.AppendChild("material"), node.get_param("colour").value,
                        node.get_param("opacity").value)
        UsdShade.MaterialBindingAPI.Apply(proto).Bind(mat)
        radius = _PROTO_RADIUS
    inst.CreatePrototypesRel().SetTargets([proto.GetPath()])

    columns = {name: [] for name in ("protoIndices", "positions", "scales", "ids", "velocities",
                                     "extent", "colour", "opacity")}
    for t, pos, widths, ids, vel, colours, opacities in frames:
        scale = widths * size
        if shrink and opacities is not None and peak > 0:
            scale = scale * (opacities[:, 0] / peak)
        columns["protoIndices"].append((t, Vt.IntArray(len(pos))))
        columns["positions"].append((t, Vt.Vec3fArray.FromNumpy(pos)))
        columns["scales"].append((t, Vt.Vec3fArray.FromNumpy(
            np.ascontiguousarray(np.repeat(scale[:, None], 3, axis=1), dtype=np.float32))))
        if ids is not None:
            columns["ids"].append((t, Vt.Int64Array.FromNumpy(ids)))
        if vel is not None:
            columns["velocities"].append((t, Vt.Vec3fArray.FromNumpy(vel)))
        pad = float(scale.max()) * radius if len(scale) else 0.0
        bounds = (np.stack((pos.min(axis=0) - pad, pos.max(axis=0) + pad)) if len(pos)
                  else np.zeros((2, 3)))
        columns["extent"].append((t, Vt.Vec3fArray.FromNumpy(bounds.astype(np.float32))))
        if colours is not None:
            columns["colour"].append((t, Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(colours))))
        if opacities is not None:
            columns["opacity"].append((t, Vt.FloatArray.FromNumpy(np.ascontiguousarray(opacities[:, 0]))))

    primvars = UsdGeom.PrimvarsAPI(inst)
    make = {
        "protoIndices": inst.CreateProtoIndicesAttr, "positions": inst.CreatePositionsAttr,
        "scales": inst.CreateScalesAttr, "ids": inst.CreateIdsAttr,
        "velocities": inst.CreateVelocitiesAttr, "extent": inst.CreateExtentAttr,
        "colour": lambda: primvars.CreatePrimvar("displayColor", Sdf.ValueTypeNames.Color3fArray,
                                                 UsdGeom.Tokens.vertex).GetAttr(),
        "opacity": lambda: primvars.CreatePrimvar("displayOpacity", Sdf.ValueTypeNames.FloatArray,
                                                  UsdGeom.Tokens.vertex).GetAttr(),
    }
    animated = len(frames) > 1 or not Usd.TimeCode(frames[0][0]).IsDefault()
    for name, samples in columns.items():
        if not samples:
            continue
        attr = make[name]()
        if animated:
            sim_bake.write_samples(attr, samples)
        else:
            attr.Set(samples[0][1])

    count = max((len(f[1]) for f in frames), default=0)
    readout.value = f"{count:,} instances at most · {len(frames)} frame{'s' if len(frames) != 1 else ''}"


def main():
    chunk_api = chunk.Chunk("Points to Instancer")
    _PF = chunk.ChunkFlags.ParamFlags
    chunk_api.add_params([
        chunk.LineEdit("prim_path", "Points", flags=_PF.LABELED | _PF.PRIM_SELECTION,
                       tooltip="The Points prim to turn into instances. Every frame it has is copied."),
        chunk.LineEdit("primpath_location", "Location", default_value="/points_instanced", flags=_PF.LABELED,
                       tooltip="Where the PointInstancer is written."),
        chunk.LineEdit("proto_prim", "Prototype", flags=_PF.LABELED | _PF.PRIM_SELECTION,
                       tooltip="Optional. A prim of about unit size to instance instead of the built-in sphere.\n"
                               "Use a mesh: Aurora skips implicit shapes (Sphere, Cube)."),
        chunk.Slider("size_scale", "Size Scale", default_value=1.0, min_value=0.0, max_value=5.0,
                     tooltip="Multiplies every instance's size (its point's width)."),
        chunk.Checkbox("shrink_with_opacity", "Shrink with Opacity", default_value=True,
                       tooltip="Scale each instance by its point's displayOpacity, so a point that fades\n"
                               "out also shrinks away. For renderers that ignore per-instance opacity."),
        chunk.Slider("every_nth", "Every Nth Point", default_value=1, min_value=1, max_value=50, is_int=True,
                     tooltip="Keep one point in N, chosen by id so the same points stay every frame."),
        chunk.ColorPicker("colour", "Colour", default_value=(0.8, 0.8, 0.8),
                          tooltip="The built-in sphere's material colour."),
        chunk.Slider("opacity", "Opacity", default_value=1.0, min_value=0.0, max_value=1.0,
                     tooltip="The built-in sphere's material opacity."),
    ])
    chunk_api.add_param(chunk.Label("readout", default_value="not cooked yet",
                                    tooltip="How many instances and frames the last cook wrote."))
    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD,
        icon="scatter",
        requirements=["numpy>=2,<3"],
        description="""
        Turns an animated Points prim into instanced meshes, for Aurora and V-Ray (dust, sparks).

        Writes a PointInstancer at Location with one instance per point, on every frame the
        points have: positions, sizes from widths, ids and velocities, plus displayColor and
        displayOpacity as per-instance primvars. Aurora draws no Points, and V-Ray draws each
        as one pixel; both draw this.

        Parameters:
          - Points: the Points prim to copy.
          - Location: where the PointInstancer goes.
          - Prototype: optional mesh to instance instead of the built-in low-poly sphere.
          - Size Scale: multiplies every instance's size.
          - Shrink with Opacity: scale instances by displayOpacity, so fading points vanish
            in renderers that ignore per-instance opacity (Aurora).
          - Every Nth Point: keep one point in N, by id.
          - Colour, Opacity: the built-in sphere's material.
        """,
    )
