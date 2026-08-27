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

import contextlib
import logging

from pxr import Gf, Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk, chunk_core, chunk_utils

logger = logging.getLogger(__name__)

#: Read by scripts/scatter_points.py. Kept as a literal rather than imported, so a scene with
#: only one of the two chunks installed still loads.
DENSITY_PRIMVAR = "scatterDensity"

_MODES = ["Add", "Subtract", "Smooth"]

#: What a stroke produces.
#:
#: *Density* paints a weight and lets Scatter Points decide where instances actually land --
#: "roughly this many, scattered". *Points* drops one instance per dab, exactly where the
#: cursor was, and Scatter Points places those instead of sampling. Two different tools that
#: happen to share a cursor, which is why this is a mode rather than two chunks.
_OUTPUT_DENSITY = "Density"
_OUTPUT_POINTS = "Points"
_OUTPUTS = [_OUTPUT_DENSITY, _OUTPUT_POINTS]

#: Where placed points are kept: a plain point cloud on the chunk's own prim, so it is
#: inspectable USD like the density field is, and travels the same wire.
POINTS_PRIM = "PaintedPoints"


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


#: Where a stroke's working state lives while the mouse is down.
_STROKE_CACHE = "_stroke_cache"


def _cached_targets(node, prim):
    """`_paint_targets`, held for the duration of a stroke.

    Recomputing it per tick means a local-to-world matrix and a transform of *every vertex on
    the surface*, on every mouse-move -- which on a real asset is most of the frame and is why
    painting felt sticky. The geometry cannot move mid-stroke (the brush writes a primvar, not
    a transform), so once per stroke is enough.
    """
    cache = getattr(node, _STROKE_CACHE, None)
    key = str(prim.GetPath())
    if cache is not None and cache[0] == key:
        return cache[1]
    targets = _paint_targets(prim)
    setattr(node, _STROKE_CACHE, (key, targets, {}))
    return targets


def _stroke_state(node, mesh_prim, count):
    """The weights and underlying colour for one mesh, held for the stroke.

    Both were recomputed per tick, and both are fixed for the duration of a stroke: the
    weights because we are the only writer, the colour because a surface does not repaint
    itself while you drag on it.

    Measured on a 14.6k-vertex floor, a tick cost 173ms -- six frames a second -- and 77% of
    that was rebuilding the base colour: fourteen thousand Gf.Vec3f objects, per tick, off the
    property stack.
    """
    cache = getattr(node, _STROKE_CACHE, None)
    if cache is None:
        return _current_weights(mesh_prim, count), []
    states = cache[2]
    key = str(mesh_prim.GetPath())
    state = states.get(key)
    if state is None:
        attr = UsdGeom.Gprim(mesh_prim).GetDisplayColorAttr()
        base = _base_colour(attr, node.get_edit_context())
        weights = _current_weights(mesh_prim, count)
        # The colours are held too, and mutated in place: only a few hundred vertices move
        # under a brush, and rebuilding all of them each tick was the next cost down once the
        # base colour stopped being recomputed.
        state = [weights, base, _tint_colours(node, weights, base)]
        states[key] = state
    return state


def _drop_stroke_cache(node):
    """Let the geometry be re-read. Called when a stroke ends, and whenever anything that
    could move the surface changes."""
    if hasattr(node, _STROKE_CACHE):
        delattr(node, _STROKE_CACHE)


def _stroke_started(owner, notice, sender):
    _drop_stroke_cache(owner)


def _stroke_finished(owner, notice, sender):
    """Recook the chain once, when the brush comes up.

    Painting writes USD rather than params, so nothing downstream ever noticed -- reported as
    instances not updating until some unrelated slider was nudged. Dirtying per *tick* would
    be the other extreme: a scatter recooking on every mouse-move is the sticky drag that was
    also reported. Once, on release, is what a paint tool should do.
    """
    _drop_stroke_cache(owner)
    try:
        # A param write, deliberately, rather than dirtying the node by hand.
        #
        # Two attempts at the direct route did not recook a connected Scatter Points: marking
        # dirty only records that a recook is due, and solving the node's own outputs did not
        # reach it either. Meanwhile the user's own workaround -- nudging any value on this
        # chunk -- worked every time. So this does exactly that, and inherits whatever the
        # write path does that hand-dirtying misses.
        #
        # `stroke_count` exists only to be written. It is what a stroke *is*, from the graph's
        # point of view: the one thing about this chunk that changed.
        counter = owner.get_param("stroke_count")
        counter.value = int(counter.value or 0) + 1
    except Exception:  # noqa: BLE001 - a failed recook must not break the gesture
        logger.debug("Could not recook after a stroke on %s", owner.name, exc_info=True)


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


_DISPLAY_COLOR = "primvars:displayColor"

#: Used where the surface has no colour of its own to blend from.
_NEUTRAL = Gf.Vec3f(0.5, 0.5, 0.5)


def _write_weights(node, stage, prim, weights, colours=None):
    """Author the field into this chunk's own layer, as an over on the surface."""
    layer = node.get_edit_context()
    context = Usd.EditContext(stage, layer) if layer else contextlib.nullcontext()
    with context:
        over = stage.OverridePrim(prim.GetPath())
        primvar = UsdGeom.PrimvarsAPI(over).CreatePrimvar(
            DENSITY_PRIMVAR, Sdf.ValueTypeNames.FloatArray, UsdGeom.Tokens.vertex
        )
        primvar.Set(Vt.FloatArray([float(w) for w in weights]))
        _write_tint(node, stage, over, weights, colours=colours)


def _write_tint(node, stage, over, weights, colours=None):
    """Colour the surface by weight, so a stroke is visible the moment it lands.

    Reported live: "the paint doesn't give away at all that I have hit it". The field was
    invisible until instances appeared downstream, and invisible entirely before a scatter was
    wired up.

    **Blended from the surface's own colour, not painted over it.** Authoring a flat colour on
    every vertex -- which is what this did first -- greys out the whole model to show a stroke
    in one corner of it, and reads as having lost the asset's look. Unpainted vertices now come
    back exactly as they were, because the blend at weight zero *is* the original.

    Ordinary `displayColor` in this chunk's own layer, rather than anything drawn by the
    viewport: it needs no host support, it shows the gradient rather than a silhouette, and
    unticking Show Paint removes our opinion and leaves whatever was underneath.
    """
    show = node.get_param("show_paint")
    attr = UsdGeom.Gprim(over).GetDisplayColorAttr()
    if not show or not show.value:
        _remove_our_tint(node, over)
        return

    if colours is None:
        colours = _tint_colours(node, weights, _base_colour(attr, node.get_edit_context()))
    attr = UsdGeom.Gprim(over).CreateDisplayColorAttr()
    UsdGeom.Primvar(attr).SetInterpolation(UsdGeom.Tokens.vertex)
    attr.Set(Vt.Vec3fArray(colours))


def _tint_colours(node, weights, base):
    """The whole tint, blended from whatever the surface already had."""
    paint = Gf.Vec3f(*(node.get_param("paint_color").value or (0.15, 0.55, 1.0)))
    colours = []
    for index, weight in enumerate(weights):
        under = base[index] if index < len(base) else (base[-1] if base else _NEUTRAL)
        colours.append(under + (paint - under) * max(0.0, min(1.0, float(weight))))
    return colours


def _remove_our_tint(node, over):
    """Take our colour opinion away and leave everything else standing.

    `attr.Clear()` clears the current edit target -- which is only *our* layer when the chunk
    has one. Without one it is whatever the stage is pointed at, so unticking Show Paint
    deleted the surface's own displayColor instead of ours, and the original never came back.
    Reported as not being able to get the original look back.

    So the spec is removed from this chunk's layer by name, and a chunk with no layer of its
    own does nothing at all: never having authored a tint, it has none to withdraw.
    """
    layer = node.get_edit_context()
    if layer is None:
        # No layer of our own, so the tint went to whatever the stage's edit target is and
        # Clear() takes it from the same place. Symmetric, which is the only property that
        # matters: authoring one way and withdrawing another is what left it stuck on.
        attr = UsdGeom.Gprim(over).GetDisplayColorAttr()
        if attr and attr.IsValid():
            attr.Clear()
        return
    prim_spec = layer.GetPrimAtPath(over.GetPath())
    if not prim_spec:
        return
    spec = prim_spec.properties.get(_DISPLAY_COLOR)
    if spec is not None:
        prim_spec.RemoveProperty(spec)


def _base_colour(attr, our_layer):
    """The surface's own displayColor, ignoring the opinion we authored on top of it.

    Read off the property stack rather than off the attribute, because by the time this runs
    ours is usually the winning opinion -- asking the composed value would blend our own tint
    into itself and drift a little further from the asset on every stroke.
    """
    if not attr or not attr.IsValid():
        return []
    for spec in attr.GetPropertyStack(Usd.TimeCode.Default()):
        if our_layer is not None and spec.layer == our_layer:
            continue
        value = spec.default
        if value:
            return [Gf.Vec3f(*c) for c in value]
    return []


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

    if node.get_param("output").value == _OUTPUT_POINTS:
        return _place_points(node, stage, samples)

    targets = _cached_targets(node, prim)
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
        weights, base, colours = _stroke_state(node, mesh_prim, len(points))
        paint = Gf.Vec3f(*(node.get_param("paint_color").value or (0.15, 0.55, 1.0)))
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
                if colours:
                    # In place: only what moved under the brush is recoloured, rather than
                    # every vertex on the surface being rebuilt on every tick.
                    under = base[index] if index < len(base) else _NEUTRAL
                    colours[index] = under + (paint - under) * weights[index]
                changed = True
        # Written per mesh, and only the ones the stroke reached: a table is a dozen prims
        # and a brush touches one of them, so re-authoring all of them every tick would put
        # a full copy of every vertex array into the layer on each mouse-move.
        if changed:
            _write_weights(node, stage, mesh_prim, weights, colours=colours)
            touched = True

    return touched


def _place_points(node, stage, samples):
    """Drop one point per dab, spaced so a slow drag does not pile them up.

    A stroke arrives as one sample per mouse-move, so without spacing a careful drag leaves
    hundreds of instances inside a centimetre and a fast one leaves four. Spacing makes the
    result depend on where you dragged rather than on how quickly.

    Points accumulate on this chunk's own prim as an ordinary point cloud -- inspectable USD,
    like the density field, travelling the same wire.
    """
    spacing = float(node.get_param("radius").value or 0.0)
    existing = _current_points(node, stage)
    added = False

    for sample in samples:
        point, _pressure = _unpack(sample)
        if point is None:
            continue
        if spacing > 0.0 and any(
            (Gf.Vec3d(p) - point).GetLength() < spacing for p in existing
        ):
            continue
        existing.append(Gf.Vec3f(point[0], point[1], point[2]))
        added = True

    if not added:
        return False
    _write_points(node, stage, existing)
    return True


def _points_path(node):
    own = getattr(node, "_exec_prim_path", None) or getattr(node, "prim_path", None)
    return Sdf.Path(str(own)).AppendChild(POINTS_PRIM) if own else None


def _current_points(node, stage):
    path = _points_path(node)
    if path is None:
        return []
    points = UsdGeom.Points(stage.GetPrimAtPath(path)).GetPointsAttr().Get() if stage.GetPrimAtPath(path) else None
    return [Gf.Vec3f(*p) for p in points] if points else []


def _write_points(node, stage, points):
    path = _points_path(node)
    if path is None:
        return
    layer = node.get_edit_context()
    context = Usd.EditContext(stage, layer) if layer else contextlib.nullcontext()
    with context:
        cloud = UsdGeom.Points.Define(stage, path)
        cloud.CreatePointsAttr().Set(Vt.Vec3fArray(points))
        # A visible radius, so placed points read as marks on the model rather than as
        # single pixels -- the same reason the density mode tints.
        width = max(float(node.get_param("radius").value or 1.0) * 0.25, 1e-4)
        cloud.CreateWidthsAttr().Set(Vt.FloatArray([width] * len(points)))
        UsdGeom.Primvar(cloud.CreateDisplayColorAttr()).SetInterpolation(UsdGeom.Tokens.constant)
        cloud.GetDisplayColorAttr().Set(
            Vt.Vec3fArray([Gf.Vec3f(*(node.get_param("paint_color").value or (0.15, 0.55, 1.0)))])
        )


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
    _drop_stroke_cache(node)
    prim, stage = _surface_prim(node)
    if stage is not None and node.get_param("output").value == _OUTPUT_POINTS:
        # Points are the output in this mode, so Fill/Clear act on them instead. Filling a
        # point cloud has no meaning -- there is no "everywhere" to place one at -- so only
        # clearing does anything.
        if value == 0.0:
            _write_points(node, stage, [])
        return
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

    # Ordered as the work is done: what to paint on, what a stroke produces, how the brush
    # behaves, how the result is shown, then the two whole-surface actions. Grew by accretion
    # instead, with the mode switch buried below the brush settings it governs.
    chunk_api.add_param(chunk.LineEdit(
        "surface_prim", "Surface Mesh",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="The mesh to paint on. Wire this chunk upstream of Scatter Points.",
    ))

    output = chunk.Menu(
        "output", "Output", menu_items=_OUTPUTS, default_value=_OUTPUT_DENSITY,
        tooltip="Density paints a weight and lets Scatter Points decide where instances land. "
                "Points drops one exactly where you click.",
    )
    chunk_api.add_param(output)

    # Published so a Scatter Points can be *wired* to this brush rather than agreeing with it
    # by convention. OUTPUT makes it a real outputs: port on the chunk prim, which is what the
    # node graph draws as a connectable socket -- the same shape Scatter Points already uses to
    # hand its instancer to the RBD Solver.
    chunk_api.add_param(chunk.LineEdit(
        "density_name", "Density Field",
        default_value=DENSITY_PRIMVAR,
        flags=chunk.ChunkFlags.ParamFlags.OUTPUT | chunk.ChunkFlags.ParamFlags.LABELED,
    ))

    # Written on every stroke end and nothing else. OUTPUT keeps it out of the UI -- it is
    # bookkeeping, not a setting -- while still being a real param write, which is what makes
    # the chain recook.
    chunk_api.add_param(chunk.Slider(
        "stroke_count", "Strokes", default_value=0, is_int=True,
        flags=chunk.ChunkFlags.ParamFlags.OUTPUT,
    ))

    _GF = (chunk_core.ChunkFlags.GroupFlags.COLLAPSABLE
           | chunk_core.ChunkFlags.GroupFlags.LABELED
           | chunk_core.ChunkFlags.GroupFlags.FRAMED)

    grp = chunk.Group("brush_grp", "Brush", flags=_GF)
    radius = chunk.Slider(
        "radius", "Radius", min_value=0.0, max_value=100.0, default_value=10.0,
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="Brush size in scene units. The viewport draws its ring from this, and in "
                "Points mode it is also the spacing between placed points.",
    )
    grp.add_param(radius)
    strength = chunk.Slider(
        "strength", "Strength", min_value=0.0, max_value=1.0, default_value=0.5,
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="How much one stroke adds or removes. Density mode only.",
    )
    grp.add_param(strength)
    mode = chunk.Menu(
        "mode", "Mode", menu_items=_MODES, default_value="Add",
        tooltip="Add paints instances in, Subtract paints them out, Smooth evens the field. "
                "Density mode only.",
    )
    grp.add_param(mode)
    chunk_api.add_param(grp)

    display = chunk.Group("display_grp", "Display", flags=_GF)
    colour = chunk.ColorPicker(
        "paint_color", "Paint Color", default_value=(0.15, 0.55, 1.0),
        tooltip="What painted areas are tinted toward. Blue by default because usdview "
                "highlights selected prims in yellow, and you paint on selected prims.",
    )
    colour.add_callback(_show_paint_changed)
    display.add_param(colour)
    show = chunk.Checkbox(
        "show_paint", "Show Paint", default_value=True,
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="Tint the surface by painted weight. Authored in this chunk's layer, so "
                "unticking it puts the original colours back.",
    )
    show.add_callback(_show_paint_changed)
    display.add_param(show)
    chunk_api.add_param(display)

    fill = chunk.Button("fill_paint", "Fill", tooltip="Paint the whole surface.")
    fill.add_callback(_fill_paint)
    chunk_api.add_param(fill)

    clear = chunk.Button("clear_paint", "Clear", tooltip="Wipe the paint back to nothing.")
    clear.add_callback(_clear_paint)
    chunk_api.add_param(clear)

    # Nothing downstream computes from these. They steer a *stroke*, which happens outside any
    # cook -- execute() never reads them, and what a scatter sees is the painted field, not the
    # settings that produced it. Without this, dragging Radius recooked the whole chain on
    # every tick, reported as the brush being unusably sticky.
    for param in (radius, strength, mode, colour, show):
        param.presentation_only = True

    # The drag bracket the host already sends. GIZMO_START drops the geometry cache so a
    # stroke reads the surface as it is now; GIZMO_STOP is the one moment a recook is wanted.
    chunk_api.add_callback(_stroke_started, notice_alias=chunk.ChunkNoticeAlias.GIZMO_START)
    chunk_api.add_callback(_stroke_finished, notice_alias=chunk.ChunkNoticeAlias.GIZMO_STOP)

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

        Wire this chunk's Density Field output into a Scatter Points chunk's
        Density Field input, with both pointed at the same mesh. Painting writes
        primvars:scatterDensity on the surface, and the wire is what tells that
        scatter to read it — an unconnected scatter is unaffected. Weight is a
        probability, so 0.5 gives roughly half as many instances, not a hard edge.

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
