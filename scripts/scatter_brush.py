"""Scatter Brush chunk — paints where instances are allowed to land.

Pairs with Scatter Points. This chunk owns a **field**, not a stroke: a per-vertex weight
(`primvars:scatterDensity`) on the surface, which the scatter chunk samples per candidate
point and keeps with that probability. Paint an area, get instances there; paint it out, they
go away. Wire this upstream of Scatter Points so its cook can see the field.

Its own chunk rather than a mode on Scatter Points, so one painted field can feed several
scatters and Scatter Points stays what its docstring says it is: placement only.

**The paint IS this chunk's output**, held in its own layer rather than rebuilt from params on
every cook -- which is why `execute` deliberately does not clear that layer. A per-vertex array
is not a slider, and reconstructing it from one would mean inventing a param type to hold a
few thousand floats. It saves and loads with the scene like any other chunk layer.

Requires: pxr (OpenUSD), usd_chunks.
"""

from __future__ import annotations

import logging

from pxr import Gf, Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk, chunk_core, chunk_utils

logger = logging.getLogger(__name__)

#: Read by scripts/scatter_points.py. Kept as a literal rather than imported, so a scene with
#: only one of the two chunks installed still loads.
DENSITY_PRIMVAR = "scatterDensity"

_MODES = ["Add", "Subtract", "Smooth"]


def _falloff(distance, radius):
    """Smoothstep from full strength at the centre to nothing at the rim.

    A hard-edged brush is unusable for density work -- the instances it places have a visible
    boundary the eye picks out immediately, which is the thing you are painting to avoid.
    """
    if radius <= 0.0:
        return 0.0
    t = max(0.0, min(1.0, 1.0 - (distance / radius)))
    return t * t * (3.0 - 2.0 * t)


def _surface_prim(node):
    """The mesh being painted, resolved on the chunk's own stage."""
    stage = node.get_stage()
    param = node.get_param("surface_prim") if node else None
    if stage is None or not param or not param.value:
        return None, None
    targets = list(chunk_utils.iter_prim_targets(stage, param))
    if not targets:
        return None, None
    prim = stage.GetPrimAtPath(targets[0])
    return (prim, stage) if prim.IsValid() else (None, None)


def _paint_targets(prim):
    """Every mesh under `prim`, with its points in world space.

    A hierarchy, not a mesh. What a user picks as "the surface" is almost never a single
    Mesh -- `KitchenTable_1` is an Xform whose meshes are children, and that is the ordinary
    shape of any real asset. Reading points off the picked prim itself found none and painted
    nothing, silently, while the hit test walked the descendants perfectly happily and
    reported 2392 triangles. The two disagreeing about what "the surface" means is exactly
    the bug that produced.

    World space, which is the space strokes arrive in. Each mesh is kept separate because the
    weight is authored per mesh: one primvar per prim that has points.
    """
    targets = []
    for p in Usd.PrimRange(prim):
        points = UsdGeom.Mesh(p).GetPointsAttr().Get()
        if not points:
            continue
        to_world = UsdGeom.Xformable(p).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        targets.append((p, [to_world.Transform(Gf.Vec3d(q[0], q[1], q[2])) for q in points]))
    return targets


def _current_weights(prim, count):
    """Whatever is painted now, as a plain list of floats, defaulting to unpainted.

    Zero rather than one: a brush chunk that has been added but never used should scatter
    nothing, so the effect of adding it is visible and undoable. An *absent* primvar still
    means "no opinion" and lets scatter place everywhere -- that difference is the whole
    reason scatter distinguishes an empty field from a missing one.
    """
    primvar = UsdGeom.PrimvarsAPI(prim).GetPrimvar(DENSITY_PRIMVAR)
    if primvar and primvar.HasValue():
        values = primvar.ComputeFlattened()
        if values is not None and len(values) >= count:
            return [float(v) for v in values[:count]]
    return [0.0] * count


#: Unpainted, and fully painted. Ordinary displayColor, so any renderer shows it and no
#: viewport code is needed to draw the field.
_COLD = Gf.Vec3f(0.25, 0.25, 0.30)
_HOT = Gf.Vec3f(0.95, 0.55, 0.10)


def _write_weights(node, stage, prim, weights):
    """Author the field into this chunk's own layer, as an over on the surface."""
    layer = node.get_edit_context()
    context = Usd.EditContext(stage, layer) if layer else _null_context()
    with context:
        over = stage.OverridePrim(prim.GetPath())
        primvar = UsdGeom.PrimvarsAPI(over).CreatePrimvar(
            DENSITY_PRIMVAR, Sdf.ValueTypeNames.FloatArray, UsdGeom.Tokens.vertex
        )
        primvar.Set(Vt.FloatArray([float(w) for w in weights]))
        _write_tint(node, over, weights)


def _write_tint(node, over, weights):
    """Colour the surface by weight, so a stroke is visible the moment it lands.

    Reported live: "the paint doesn't give away at all that I have hit it". The field was
    invisible until instances appeared downstream, which is a slow and indirect way to judge
    a stroke -- and no way at all to judge one before wiring up a scatter.

    Ordinary `displayColor`, authored into this chunk's own layer, rather than anything drawn
    by the viewport. It needs no host support, shows the gradient rather than a silhouette --
    which is the whole difference between a density and a mask -- and unticking Show Paint or
    deleting the chunk takes it away again, because the opinion was only ever ours.
    """
    show = node.get_param("show_paint")
    attr = UsdGeom.Gprim(over).GetDisplayColorAttr()
    if not show or not show.value:
        # Cleared rather than left behind: a tint that outlived the toggle would look like
        # the asset had been recoloured.
        if attr and attr.IsValid():
            attr.Clear()
        return
    attr = UsdGeom.Gprim(over).CreateDisplayColorAttr()
    UsdGeom.Primvar(attr).SetInterpolation(UsdGeom.Tokens.vertex)
    attr.Set(Vt.Vec3fArray([_COLD + (_HOT - _COLD) * max(0.0, min(1.0, float(w)))
                            for w in weights]))


class _null_context:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def gizmo_stroke(node, gizmo, samples):
    """Accumulate one tick of a brush gesture into the painted field.

    `samples` are world-space points with a per-sample weight, which is what the host knows.
    Everything about what a stroke *means* -- falloff, strength, whether it adds or removes --
    is decided here, so two hosts painting the same gesture get the same field.

    Nothing keeps the stroke. It is folded into the weights and dropped.
    """
    if gizmo != "paint":
        return False
    prim, stage = _surface_prim(node)
    if prim is None:
        return False

    targets = _paint_targets(prim)
    if not targets:
        return False

    radius = float(node.get_param("radius").value or 0.0)
    strength = float(node.get_param("strength").value or 0.0)
    mode = node.get_param("mode").value
    if radius <= 0.0 or strength <= 0.0:
        return False

    centres = [_unpack(sample) for sample in samples]
    radius_sq = radius * radius
    touched = False

    for mesh_prim, points in targets:
        weights = _current_weights(mesh_prim, len(points))
        changed = False
        for centre, pressure in centres:
            if centre is None:
                continue
            for index, point in enumerate(points):
                offset = point - centre
                distance_sq = offset[0] ** 2 + offset[1] ** 2 + offset[2] ** 2
                if distance_sq > radius_sq:
                    continue
                amount = strength * pressure * _falloff(distance_sq ** 0.5, radius)
                if amount <= 0.0:
                    continue
                weights[index] = _blend(mode, weights[index], amount)
                changed = True
        # Written per mesh, and only the ones the stroke reached: a table is a dozen prims
        # and a brush touches one of them, so re-authoring all of them every tick would put
        # a full copy of every vertex array into the layer on each mouse-move.
        if changed:
            _write_weights(node, stage, mesh_prim, weights)
            touched = True

    return touched


def _blend(mode, current, amount):
    """One vertex's new weight. Clamped, because the value is used as a probability."""
    if mode == "Subtract":
        return max(0.0, current - amount)
    if mode == "Smooth":
        # Toward the middle rather than toward a target: with no neighbour information here,
        # this evens a field out without needing the mesh's topology.
        return current + (0.5 - current) * amount
    return min(1.0, current + amount)


def _unpack(sample):
    """A sample is a point, or a (point, weight) pair. Both are ordinary for a host to send:
    a plain raycast hit has no pressure, and a tablet does."""
    try:
        point, pressure = sample
        return Gf.Vec3d(point[0], point[1], point[2]), float(pressure)
    except (TypeError, ValueError):
        pass
    try:
        return Gf.Vec3d(sample[0], sample[1], sample[2]), 1.0
    except (TypeError, IndexError):
        logger.warning("Ignoring unreadable brush sample %r", sample)
        return None, 0.0


def _set_everywhere(node, value):
    """Author one weight across every mesh under the surface."""
    prim, stage = _surface_prim(node)
    if prim is None:
        return
    for mesh_prim, points in _paint_targets(prim):
        _write_weights(node, stage, mesh_prim, [value] * len(points))


def _show_paint_changed(owner, notice, sender):
    """Re-author the tint, or take it away, without touching the field itself."""
    node = owner.get_chunk()
    prim, stage = _surface_prim(node)
    if prim is None:
        return
    for mesh_prim, points in _paint_targets(prim):
        _write_weights(node, stage, mesh_prim, _current_weights(mesh_prim, len(points)))


def _clear_paint(owner, notice, sender):
    """Wipe the field back to unpainted."""
    _set_everywhere(owner.get_chunk(), 0.0)


def _fill_paint(owner, notice, sender):
    """Paint the whole surface, as a starting point for subtracting away from."""
    _set_everywhere(owner.get_chunk(), 1.0)


def execute(node):
    """Deliberately almost nothing.

    The painted field is this chunk's output and already lives in its layer, so there is
    nothing to re-author from params -- and clearing the layer the way most chunks do would
    erase the paint on the next slider nudge.
    """
    prim, stage = _surface_prim(node)
    if prim is None:
        return
    # Only to make the attribute exist, so a scatter downstream resolves something rather
    # than nothing the moment the two are wired together. Per mesh, because the surface a
    # user picks is a hierarchy far more often than it is a single Mesh.
    for mesh_prim, points in _paint_targets(prim):
        if not UsdGeom.PrimvarsAPI(mesh_prim).GetPrimvar(DENSITY_PRIMVAR):
            _write_weights(node, stage, mesh_prim, [0.0] * len(points))


def main():
    chunk_api = chunk.Chunk("Scatter Brush")

    chunk_api.add_param(chunk.LineEdit(
        "surface_prim", "Surface Mesh",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="The mesh to paint on. Wire this chunk upstream of Scatter Points.",
    ))

    grp = chunk.Group(
        "brush_grp", "Brush",
        flags=chunk_core.ChunkFlags.GroupFlags.COLLAPSABLE | chunk_core.ChunkFlags.GroupFlags.LABELED,
    )
    grp.add_param(chunk.Slider(
        "radius", "Radius", min_value=0.0, max_value=100.0, default_value=10.0,
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="Brush size in scene units. The viewport draws its ring from this.",
    ))
    grp.add_param(chunk.Slider(
        "strength", "Strength", min_value=0.0, max_value=1.0, default_value=0.5,
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="How much one stroke adds or removes.",
    ))
    grp.add_param(chunk.Menu(
        "mode", "Mode", menu_items=_MODES, default_value="Add",
        tooltip="Add paints instances in, Subtract paints them out, Smooth evens the field.",
    ))
    chunk_api.add_param(grp)

    show = chunk.Checkbox(
        "show_paint", "Show Paint", default_value=True,
        tooltip="Tint the surface by painted weight. Authored in this chunk's layer, so "
                "unticking it puts the original colours back.",
    )
    show.add_callback(_show_paint_changed)
    chunk_api.add_param(show)

    fill = chunk.Button("fill_paint", "Fill", tooltip="Paint the whole surface.")
    fill.add_callback(_fill_paint)
    chunk_api.add_param(fill)

    clear = chunk.Button("clear_paint", "Clear", tooltip="Wipe the paint back to nothing.")
    clear.add_callback(_clear_paint)
    chunk_api.add_param(clear)

    return chunk_api


def config():
    return chunk.DefaulConfig(
        # Declared in full rather than as `gizmo=` sugar: sugar is type-only, and a brush
        # needs its radius named so the viewport can draw a ring the right size. The driver
        # is what the handle *reads*, unusually -- a brush writes a field, not params.
        gizmos=[
            chunk.Gizmo("paint", type="brush", label="Paint Density",
                        anchor="surface_prim",
                        driver=("brush_grp.radius", "brush_grp.strength")),
        ],
        category=chunk.StandardCategory.UTILS,
        description="""
        Scatter Brush — paints a per-vertex weight on a mesh saying where
        Scatter Points may place instances.

        Wire it upstream of a Scatter Points chunk pointed at the same mesh.
        Painting writes primvars:scatterDensity, which scatter samples per
        candidate point and keeps with that probability — so a weight of 0.5
        gives roughly half as many instances, not a hard edge.

        Parameters:
          - Surface Mesh: the mesh to paint on.
          - Radius: brush size in scene units.
          - Strength: how much a single stroke adds or removes.
          - Mode: Add paints instances in, Subtract paints them out, Smooth
            evens the field toward the middle.
          - Fill / Clear: paint everything or nothing, for starting from
            either end.

        The painted field lives in this chunk's own layer and saves with the
        scene. Density resolution is mesh resolution — the weight is per
        vertex, so a coarse mesh gives a blocky edge.
        """,
    )
