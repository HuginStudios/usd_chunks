"""Scatter Brush chunk -- paints where instances are allowed to land.

Pairs with Scatter Points. This chunk owns a **field**, not a stroke: a per-vertex weight
(`primvars:scatterDensity`) on the surface, which the scatter chunk samples per candidate
point and keeps with that probability. Paint an area, get instances there; paint it out, they
go away. Wire this upstream of Scatter Points so its cook can see the field.

Its own chunk rather than a mode on Scatter Points, so one painted field can feed several
scatters and Scatter Points stays what its docstring says it is: placement only.

**The paint is state this chunk holds**, not something rebuilt from params: a per-vertex
array is not a slider, and reconstructing it from one would mean inventing a param type to
hold a few thousand floats. It is kept on the chunk (`_FIELD`, `_POINTS`), a stroke updates it
and authors it as it goes, and `execute` authors all of it again -- the framework clears this
chunk's layer before every cook, as it does every chunk's, so the layer is only ever the
output of the paint, never where the paint is kept.

A saved scene is chunk prims and nothing else, so the field is *also* recorded on this chunk's
own prim, sparsely, and is where `execute` starts from when a reopened scene has no paint in
memory yet. See `_PAINT_MESHES`. The layer itself does not round-trip.

Requires: pxr (OpenUSD), usd_chunks, numpy (a stroke touches every vertex -- see `_stamp`).
"""

from __future__ import annotations

import contextlib
import logging

import numpy as np
from pxr import Gf, Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk, chunk_core, chunk_utils, mesh_math

logger = logging.getLogger(__name__)

#: Read by chunks/modelling/scatter_points.py. Kept as a literal rather than imported, so a scene with
#: only one of the two chunks installed still loads.
DENSITY_PRIMVAR = "scatterDensity"

_MODES = ["Add", "Subtract", "Smooth"]

#: What a stroke produces.
#:
#: *Density* paints a weight and lets Scatter Points decide where instances actually land --
#: "roughly this many, scattered". *Points* drops one instance per dab, exactly where the
#: cursor was, and Scatter Points places those instead of sampling. *Spray* sprinkles points
#: over the surface under the brush, Spacing apart -- a spray brush: a painted
#: region with no mesh-resolution limit, wired into Scatter Points' Painted Points (one
#: instance each) or its Grow Near (the cloud as a region). Tools that happen to share a
#: cursor, which is why this is a mode rather than separate chunks.
_OUTPUT_DENSITY = "Density"
_OUTPUT_POINTS = "Points"
_OUTPUT_SPRAY = "Spray"
_OUTPUTS = [_OUTPUT_DENSITY, _OUTPUT_POINTS, _OUTPUT_SPRAY]
#: The modes whose output is the point cloud rather than the field.
_POINT_OUTPUTS = (_OUTPUT_POINTS, _OUTPUT_SPRAY)

#: Where placed points are kept: a plain point cloud on the chunk's own prim, so it is
#: inspectable USD like the density field is, and travels the same wire.
POINTS_PRIM = "PaintedPoints"


def _falloff(distance, radius):
    """Smoothstep from full strength at the centre to nothing at the rim.

    A hard-edged brush is unusable for density work -- the instances it places have a visible
    boundary the eye picks out immediately, which is the thing you are painting to avoid.

    Takes the whole array of distances under one dab, because on a real asset that is
    hundreds of thousands of vertices and doing it one at a time is the stroke.
    """
    if radius <= 0.0:
        return np.zeros_like(np.asarray(distance, dtype=np.float64))
    t = np.clip(1.0 - np.asarray(distance, dtype=np.float64) / radius, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _surface_prim(node):
    """The mesh being painted, resolved on the chunk's own stage."""
    stage = node.get_stage()
    param = node.get_param("surface_prim") if node else None
    if stage is None or not param or not param.value:
        return None, None
    targets = chunk_utils.target_paths(param)
    if not targets:
        return None, None
    prim = stage.GetPrimAtPath(targets[0])
    return (prim, stage) if prim.IsValid() else (None, None)


#: Where a stroke's working state lives while the mouse is down.
_STROKE_CACHE = "_stroke_cache"

#: Whether anything in the current gesture actually changed the result. A swipe that painted
#: nothing -- Subtract over bare surface, or a drag that never met geometry -- is not an edit,
#: and recooking for it is a stall with nothing to show.
_STROKE_TOUCHED = "_stroke_touched"

#: The points placed in the current stroke. With Stack on, Points spacing is checked against
#: these only, so a new stroke can land on top of what earlier strokes placed.
_STROKE_POINTS = "_stroke_points"

#: The painted field, `{mesh path: weights}`, and the placed points, `(points, normals,
#: widths)`. The paint itself, held on the chunk -- see the module docstring. A stroke's
#: working weights are these same arrays, so a cook in the middle of a stroke authors the
#: stroke as far as it has got.
_FIELD = "_painted_field"
_POINTS = "_painted_points"

#: The surface as world-space triangles, for Spray, held for the duration of a stroke like
#: the vertex targets are.
_SPRAY_CACHE = "_spray_triangles"


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

    The colours are skipped entirely with Show Paint off. Building a per-vertex tint nobody
    is going to look at is free on a floor and is a third of a million vectors on a scanned
    mesh, which is the case that made it worth asking.
    """
    cache = getattr(node, _STROKE_CACHE, None)
    if cache is None:
        return _current_weights(mesh_prim, count), _NO_COLOURS, None
    states = cache[2]
    key = str(mesh_prim.GetPath())
    state = states.get(key)
    if state is None:
        weights = _held_weights(node, mesh_prim, count)
        show = node.get_param("show_paint")
        if show and show.value:
            attr = UsdGeom.Gprim(mesh_prim).GetDisplayColorAttr()
            under = _under_colours(_base_colour(attr, node.get_edit_context()), count)
            # Held and mutated in place: only a few hundred vertices move under a brush, and
            # rebuilding all of them each tick was the next cost down once the base colour
            # stopped being recomputed.
            state = [weights, under, _blend_tint(under, _paint_colour(node), weights)]
        else:
            state = [weights, _NO_COLOURS, None]
        states[key] = state
    return state


def _drop_stroke_cache(node):
    """Let the geometry be re-read. Called when a stroke ends, and whenever anything that
    could move the surface changes.
    """
    if hasattr(node, _STROKE_CACHE):
        delattr(node, _STROKE_CACHE)
    if hasattr(node, _SPRAY_CACHE):
        delattr(node, _SPRAY_CACHE)


def _stroke_started(owner, notice, sender):
    _drop_stroke_cache(owner)
    setattr(owner, _STROKE_TOUCHED, False)
    setattr(owner, _STROKE_POINTS, [])


def _stroke_finished(owner, notice, sender):
    """Recook the chain once, when the brush comes up.

    Painting writes USD rather than params, so nothing downstream would notice on its own.
    Dirtying per *tick* would be the other extreme: a scatter recooking on every mouse-move
    makes the drag sticky. Once, on release, is what a paint tool should do.
    """
    _drop_stroke_cache(owner)
    if not getattr(owner, _STROKE_TOUCHED, True):
        # Nothing moved, so there is nothing for anyone downstream to redo.
        return
    setattr(owner, _STROKE_TOUCHED, False)
    # Once per gesture, alongside the recook, for the same reason: this is the moment the
    # paint has finished changing. See `_record_paint`.
    _record_paint(owner)
    try:
        # A param write, deliberately, rather than dirtying the node by hand.
        #
        # Two attempts at the direct route did not recook a connected Scatter Points: marking
        # dirty only records that a recook is due, and solving the node's own outputs did not
        # reach it either. Meanwhile the user's own workaround -- nudging any value on this
        # chunk -- worked every time. So this does exactly that, and inherits whatever the
        # write path does that hand-dirtying misses.
        #
        # `stroke_count` exists only to be written. It is what an edit *is*, from the graph's
        # point of view: the one thing about this chunk that changed.
        counter = owner.get_param("stroke_count")
        counter.value = int(counter.value or 0) + 1
    except Exception:  # noqa: BLE001 - a failed recook must not break the gesture
        logger.debug("Could not recook after a stroke on %s", owner.name, exc_info=True)


def _paint_meshes(prim):
    """Every mesh under `prim` that has points, with how many it has.

    A hierarchy, not a mesh. What a user picks as "the surface" is almost never a single
    Mesh -- a table is typically an Xform whose meshes are children, and that is the
    ordinary shape of any real asset. Painting and hit-testing must agree about what "the
    surface" means, so both walk the descendants.

    Separate from `_paint_targets` because most callers only need to know how long the weight
    array is. `execute` runs on every cook, and transforming a third of a million vertices to
    world space to count them is a cook's worth of work for a number USD already knows.
    """
    meshes = []
    for p in Usd.PrimRange(prim):
        points = UsdGeom.Mesh(p).GetPointsAttr().Get()
        if points:
            meshes.append((p, len(points)))
    return meshes


def _paint_targets(prim):
    """Every mesh under `prim`, with its points in world space as an (N, 3) array.

    World space, which is the space strokes arrive in. Each mesh is kept separate because the
    weight is authored per mesh: one primvar per prim that has points.

    One matrix multiply per mesh, rather than a `Gf.Matrix4d.Transform` per vertex: on a
    340k-vertex scan the per-vertex form took five and a half seconds, at the start of every
    stroke.
    """
    targets = []
    for p in Usd.PrimRange(prim):
        points = UsdGeom.Mesh(p).GetPointsAttr().Get()
        if not points:
            continue
        matrix = np.asarray(
            UsdGeom.Xformable(p).ComputeLocalToWorldTransform(Usd.TimeCode.Default()),
            dtype=np.float64,
        )
        # Row-vector convention, matching Gf: p * M, translation in the last row.
        world = np.asarray(points, dtype=np.float64) @ matrix[:3, :3] + matrix[3, :3]
        targets.append((p, world))
    return targets


def _current_weights(prim, count):
    """Whatever is painted now, as a float array, defaulting to unpainted.

    Zero rather than one: a brush chunk that has been added but never used should scatter
    nothing, so the effect of adding it is visible and undoable. An *absent* primvar still
    means "no opinion" and lets scatter place everywhere -- that difference is the whole
    reason scatter distinguishes an empty field from a missing one.
    """
    primvar = UsdGeom.PrimvarsAPI(prim).GetPrimvar(DENSITY_PRIMVAR)
    if primvar and primvar.HasValue():
        values = primvar.ComputeFlattened()
        if values is not None and len(values) >= count:
            # Copied, not viewed: this array is mutated in place through the stroke, and the
            # one USD handed back is its own.
            return np.array(values[:count], dtype=np.float64)
    return np.zeros(count, dtype=np.float64)


def _held_weights(node, mesh_prim, count):
    """The weights this chunk holds for one mesh, or what is on the stage when it holds none."""
    held = getattr(node, _FIELD, {}).get(str(mesh_prim.GetPath()))
    if held is not None and len(held) == count:
        return held
    return _current_weights(mesh_prim, count)


_DISPLAY_COLOR = "primvars:displayColor"

#: Used where the surface has no colour of its own to blend from.
_NEUTRAL = Gf.Vec3f(0.5, 0.5, 0.5)

#: "This mesh has no tint this stroke" -- Show Paint is off, or there is no state yet.
_NO_COLOURS = np.zeros((0, 3), dtype=np.float64)


def _paint_colour(node):
    return np.asarray(
        node.get_param("paint_color").value or (0.15, 0.55, 1.0), dtype=np.float64
    )


def _write_weights(node, stage, prim, weights, colours=None):
    """Hold the field for one mesh, and author it into this chunk's own layer, as an over on
    the surface."""
    if not hasattr(node, _FIELD):
        setattr(node, _FIELD, {})
    getattr(node, _FIELD)[str(prim.GetPath())] = weights
    layer = node.get_edit_context()
    context = Usd.EditContext(stage, chunk_utils.edit_target(stage, layer)) if layer else contextlib.nullcontext()
    with context:
        over = stage.OverridePrim(prim.GetPath())
        primvar = UsdGeom.PrimvarsAPI(over).CreatePrimvar(
            DENSITY_PRIMVAR, Sdf.ValueTypeNames.FloatArray, UsdGeom.Tokens.vertex
        )
        # FromNumpy, not a list comprehension into Vt: the array is authored on every tick of
        # a stroke, and rebuilding a third of a million Python floats to do it was most of
        # the tick on a scanned mesh.
        primvar.Set(Vt.FloatArray.FromNumpy(np.ascontiguousarray(weights, dtype=np.float32)))
        _write_tint(node, stage, over, weights, colours=colours)


def _write_tint(node, stage, over, weights, colours=None):
    """Colour the surface by weight, so a stroke is visible the moment it lands.

    Without it, the field is invisible until instances appear downstream, and invisible
    entirely before a scatter is wired up -- painting gives no feedback that it hit.

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
        _show_through_material(node, over, show=False)
        return

    if colours is None:
        colours = _tint_colours(node, weights, _base_colour(attr, node.get_edit_context()))
    attr = UsdGeom.Gprim(over).CreateDisplayColorAttr()
    UsdGeom.Primvar(attr).SetInterpolation(UsdGeom.Tokens.vertex)
    attr.Set(Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(colours, dtype=np.float32)))
    _show_through_material(node, over, show=True)


def _under_colours(base, count):
    """The surface's own colour per vertex, padded out to `count` rows.

    Short arrays repeat their last value and an absent one falls back to neutral grey --
    both carried over from the per-vertex version, where the same two cases were an
    index guard.
    """
    base = np.asarray(base, dtype=np.float64).reshape(-1, 3)
    if not len(base):
        return np.tile(np.asarray(_NEUTRAL, dtype=np.float64), (count, 1))
    if len(base) < count:
        base = np.concatenate((base, np.repeat(base[-1:], count - len(base), axis=0)))
    return np.ascontiguousarray(base[:count])


def _blend_tint(under, paint, weights):
    """Every vertex's tint: its own colour, moved toward the paint by its weight."""
    amount = np.clip(np.asarray(weights, dtype=np.float64), 0.0, 1.0)[:, None]
    return under + (paint - under) * amount


def _tint_colours(node, weights, base):
    """The whole tint, blended from whatever the surface already had."""
    count = len(weights)
    return _blend_tint(_under_colours(base, count), _paint_colour(node), weights)


def _remove_our_tint(node, over):
    """Take our colour opinion away and leave everything else standing.

    `attr.Clear()` clears the current edit target -- which is only *our* layer when the chunk
    has one. Without one it is whatever the stage is pointed at, so unticking Show Paint
    would delete the surface's own displayColor instead of ours, and the original would
    never come back.

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


_MATERIAL_BINDING = "material:binding"


def _show_through_material(node, over, show):
    """Let the tint actually reach the viewport on a textured asset.

    `displayColor` is what a surface falls back to when nothing else has an opinion, and a
    bound material always has one -- so on any asset with a shader on it the paint was
    authored, correct, and completely invisible. Reported on a photogrammetry rock: the
    strokes landed, the weights were right, and the surface never changed.

    So while Show Paint is on, the surface is bound to no material in whichever layer this
    chunk is writing to, and the fallback is what the viewport draws. Nothing is destroyed --
    the empty binding overrides the asset's from above and is removed by name the moment Show
    Paint goes off, the same way the tint is. Painting a mask and looking at the textured
    asset are two different things to be doing, and the tick box is which one you are doing.

    Authored on every mesh we tint rather than only where a binding is found: bindings inherit
    down a hierarchy, so "does this prim have one" is the wrong question to ask of the prim
    alone, and an empty relationship over an unmaterialled mesh costs a line in a layer.

    Never over an existing binding, though. Where we are writing already holds a real one, we
    would be overwriting it and, worse, deleting it on the way back out. That layer is
    somebody's authored opinion, so it is left alone and the paint simply stays behind the
    material -- the honest failure rather than a destructive success. The same asymmetry
    guards the removal: only an *empty* binding is ours to take away.

    A chunk layer only wins over what is weaker than it. An asset opened as the root layer
    outranks every chunk, and there the material stands -- the same limit the tint itself has
    always had.
    """
    layer = node.get_edit_context() or over.GetStage().GetEditTarget().GetLayer()
    spec = _binding_spec(layer, over.GetPath())

    if show:
        if spec is None or not _binding_targets(spec):
            over.CreateRelationship(_MATERIAL_BINDING, False).SetTargets([])
        return

    if spec is not None and not _binding_targets(spec):
        layer.GetPrimAtPath(over.GetPath()).RemoveProperty(spec)


def _binding_spec(layer, path):
    """The material:binding relationship authored on `path` in `layer`, if any."""
    prim_spec = layer.GetPrimAtPath(path) if layer else None
    return prim_spec.properties.get(_MATERIAL_BINDING) if prim_spec else None


def _binding_targets(spec):
    return list(spec.targetPathList.GetAddedOrExplicitItems())


def _base_colour(attr, our_layer):
    """The surface's own displayColor, ignoring the opinion we authored on top of it.

    Read off the property stack rather than off the attribute, because by the time this runs
    ours is usually the winning opinion -- asking the composed value would blend our own tint
    into itself and drift a little further from the asset on every stroke.
    """
    if not attr or not attr.IsValid():
        return _NO_COLOURS
    for spec in attr.GetPropertyStack(Usd.TimeCode.Default()):
        if our_layer is not None and spec.layer == our_layer:
            continue
        value = spec.default
        if value:
            return np.asarray(value, dtype=np.float64).reshape(-1, 3)
    return _NO_COLOURS


def gizmo_stroke(node, gizmo, samples):
    """Accumulate one tick of a brush gesture into the painted field.

    `samples` are world-space points with a per-sample weight, which is what the host knows.
    Everything about what a stroke *means* -- falloff, strength, whether it adds or removes --
    is decided here, so two hosts painting the same gesture get the same field.

    Nothing keeps the stroke. It is folded into the weights and dropped.

    Points and Spray paint on anything when Surface Mesh is empty. Density always needs a
    mesh, because the field is stored on its vertices.
    """
    if gizmo != "paint":
        return False
    prim, _ = _surface_prim(node)
    stage = node.get_stage()
    if stage is None:
        return False

    output = node.get_param("output").value
    if output == _OUTPUT_POINTS:
        return _place_points(node, stage, samples)
    if output == _OUTPUT_SPRAY:
        return _spray_points(node, stage, prim, samples)
    if prim is None:
        return False

    targets = _cached_targets(node, prim)
    if not targets:
        return False

    radius = float(node.get_param("radius").value or 0.0)
    strength = float(node.get_param("strength").value or 0.0)
    mode = node.get_param("mode").value
    if radius <= 0.0 or strength <= 0.0:
        return False

    centres = [(pt, pressure) for pt, pressure, _n in map(_unpack, samples) if pt is not None]
    if not centres:
        return False

    paint = _paint_colour(node)
    touched = False

    for mesh_prim, points in targets:
        weights, under, colours = _stroke_state(node, mesh_prim, len(points))
        moved = _stamp(points, centres, radius, strength, mode, weights)
        if moved is None:
            continue
        if colours is not None:
            # Only what the brush actually reached is recoloured, rather than every vertex on
            # the surface being rebuilt on every tick.
            colours[moved] = _blend_tint(under[moved], paint, weights[moved])
        # Written per mesh, and only the ones the stroke reached: a table is a dozen prims
        # and a brush touches one of them, so re-authoring all of them every tick would put
        # a full copy of every vertex array into the layer on each mouse-move.
        _write_weights(node, stage, mesh_prim, weights, colours=colours)
        touched = True

    if touched:
        setattr(node, _STROKE_TOUCHED, True)
    return touched


def _stamp(points, centres, radius, strength, mode, weights):
    """Fold one tick's dabs into `weights` in place; the vertices that moved, or None.

    The whole surface is tested against each dab in one array pass. The per-vertex Python
    loop this replaces was O(dabs x vertices) *per mouse-move* -- fine on a twelve-triangle
    floor, and a third of a second per tick on a scanned rock, which is the "super heavy to
    even start brushing" that was reported.

    Dabs are still applied one after another, so a vertex caught by two of them in the same
    tick is blended twice, in order.
    """
    radius_sq = radius * radius
    moved = None
    for centre, pressure in centres:
        offset = points - np.asarray([centre[0], centre[1], centre[2]], dtype=np.float64)
        distance_sq = np.einsum("ij,ij->i", offset, offset)
        under = np.flatnonzero(distance_sq <= radius_sq)
        if not len(under):
            continue
        amount = strength * pressure * _falloff(np.sqrt(distance_sq[under]), radius)
        under = under[amount > 0.0]
        if not len(under):
            continue
        amount = amount[amount > 0.0]
        weights[under] = _blend(mode, weights[under], amount)
        moved = under if moved is None else np.concatenate((moved, under))
    return moved


def _place_points(node, stage, samples):
    """Drop one point per dab, spaced so a slow drag does not pile them up.

    A stroke arrives as one sample per mouse-move, so without spacing a careful drag leaves
    hundreds of instances inside a centimetre and a fast one leaves four. Spacing makes the
    result depend on where you dragged rather than on how quickly.

    Points accumulate on this chunk's own prim as an ordinary point cloud -- inspectable USD,
    like the density field, travelling the same wire.

    With Stack on, spacing only counts points from this stroke. A click on an instance that
    stands on an earlier point then adds one on top of it, which is how a tower is built.
    """
    radius = float(node.get_param("radius").value or 0.0)
    existing, normals, widths = _current_points(node, stage)

    if node.get_param("mode").value == "Subtract":
        return _erase_points(node, stage, samples, existing, normals, widths, radius)

    spacing = radius
    stack = bool(node.get_param("stack").value)
    if stack and not hasattr(node, _STROKE_POINTS):
        setattr(node, _STROKE_POINTS, [])
    this_stroke = getattr(node, _STROKE_POINTS, [])
    added = False

    for sample in samples:
        point, _pressure, normal = _unpack(sample)
        if point is None:
            continue
        if spacing > 0.0 and any(
            (Gf.Vec3d(p) - point).GetLength() < spacing for p in (this_stroke if stack else existing)
        ):
            continue
        placed = Gf.Vec3f(point[0], point[1], point[2])
        this_stroke.append(placed)
        existing.append(placed)
        widths.append(_dab_width(node))
        # The surface's facing where the dab landed. Recorded per point, the way Houdini
        # carries N, so a downstream instance can be laid flat on what it was painted on --
        # a scatter has the triangle to hand, a placed point does not.
        normals.append(Gf.Vec3f(*normal) if normal is not None else Gf.Vec3f(0, 0, 1))
        added = True

    if not added:
        return False
    setattr(node, _STROKE_TOUCHED, True)
    _write_points(node, stage, existing, normals, widths)
    return True


def _erase_points(node, stage, samples, existing, normals, widths, radius):
    """Rub placed points out, the way Subtract takes density away.

    The mode was ignored entirely in Points mode, so Subtract quietly went on adding -- and
    the only way to remove anything was Clear, which removes everything.
    """
    if radius <= 0.0 or not existing:
        return False

    keep_points, keep_normals, keep_widths = [], [], []
    for point, normal, width in zip(existing, normals, widths):
        under_brush = False
        for sample in samples:
            centre, _pressure, _n = _unpack(sample)
            if centre is not None and (Gf.Vec3d(point) - centre).GetLength() <= radius:
                under_brush = True
                break
        if not under_brush:
            keep_points.append(point)
            keep_normals.append(normal)
            keep_widths.append(width)

    if len(keep_points) == len(existing):
        return False
    setattr(node, _STROKE_TOUCHED, True)
    _write_points(node, stage, keep_points, keep_normals, keep_widths)
    return True


def _mesh_triangles(prim):
    """One mesh's triangles in world space: (v0, v1, v2, normals), or None if it has none."""
    mesh = UsdGeom.Mesh(prim)
    points = mesh.GetPointsAttr().Get()
    if not points:
        return None
    tris, _corners, _faces = mesh_math.fan_triangulate(
        mesh.GetFaceVertexCountsAttr().Get() or [], mesh.GetFaceVertexIndicesAttr().Get() or [])
    if not len(tris):
        return None
    world = mesh_math.world_points(prim, points)
    v0, v1, v2 = world[tris[:, 0]], world[tris[:, 1]], world[tris[:, 2]]
    n = np.cross(v1 - v0, v2 - v0)
    length = np.linalg.norm(n, axis=1)
    keep = length > 1e-12
    return v0[keep], v1[keep], v2[keep], n[keep] / length[keep, None]


def _join_triangles(parts):
    """Several meshes' triangles as one (v0, v1, v2, normals), or None if there are none."""
    parts = [t for t in parts if t is not None and len(t[0])]
    if not parts:
        return None
    return tuple(np.concatenate([t[i] for t in parts]) for i in range(4))


def _spray_triangles(node, prim):
    """Every triangle under `prim` in world space -- (v0, v1, v2, normals) -- held for the
    stroke. Spray samples the surface itself, so a coarse mesh (one quad for a field) is as
    sprayable as a dense one."""
    cache = getattr(node, _SPRAY_CACHE, None)
    key = str(prim.GetPath())
    if cache is not None and cache[0] == key:
        return cache[1]
    triangles = _join_triangles(_mesh_triangles(p) for p in Usd.PrimRange(prim))
    setattr(node, _SPRAY_CACHE, (key, triangles))
    return triangles


def _scene_meshes(node, stage):
    """Every visible mesh on the stage with its world bounds, held for the stroke.

    For Spray with no Surface Mesh. Triangles are only built for the meshes a dab reaches,
    so a big scene costs one walk per stroke, not one triangulation of everything.
    PointInstancer prototypes are skipped: they are not drawn where they are authored.

    Returns:
        ([(prim, lo, hi)], {mesh path: triangles}). The dict fills in as dabs need it.
    """
    cache = getattr(node, _SPRAY_CACHE, None)
    if cache is not None and cache[0] is None:
        return cache[1]
    bounds = UsdGeom.BBoxCache(
        Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy], True)
    meshes = []
    walk = iter(Usd.PrimRange(stage.GetPseudoRoot()))
    for p in walk:
        if p.IsA(UsdGeom.PointInstancer):
            walk.PruneChildren()
            continue
        if not p.IsA(UsdGeom.Mesh):
            continue
        if UsdGeom.Imageable(p).ComputeVisibility() == UsdGeom.Tokens.invisible:
            continue
        box = bounds.ComputeWorldBound(p).ComputeAlignedRange()
        if box.IsEmpty():
            continue
        meshes.append((p, np.array(box.GetMin(), dtype=np.float64), np.array(box.GetMax(), dtype=np.float64)))
    state = (meshes, {})
    setattr(node, _SPRAY_CACHE, (None, state))
    return state


def _triangles_near(node, stage, centre, radius):
    """The triangles of every mesh within `radius` of `centre`, for Spray with no Surface Mesh."""
    meshes, built = _scene_meshes(node, stage)
    parts = []
    for p, lo, hi in meshes:
        if ((lo - radius) <= centre).all() and ((hi + radius) >= centre).all():
            key = str(p.GetPath())
            if key not in built:
                built[key] = _mesh_triangles(p)
            parts.append(built[key])
    return _join_triangles(parts)


def _spray_spacing(node):
    return max(float(node.get_param("spray_spacing").value or 0.0), 0.0)


def _spray_width(spacing, radius):
    """How big a sprayed point is drawn: half its spacing, so the cloud reads as coverage."""
    return max((spacing if spacing > 0.0 else radius * 0.1) * 0.5, 1e-4)


def _cast_down(triangles, origins, direction):
    """Where rays from `origins` along `direction` first hit the triangles: (points, the
    normal of the triangle hit, which rays hit). Every ray against every triangle -- a dab's
    handful of rays against the triangles near it -- by Moller-Trumbore."""
    v0, v1, v2, n = triangles
    e1, e2 = v1 - v0, v2 - v0
    p = np.cross(direction, e2)
    det = np.einsum("ij,ij->i", e1, p)
    ok = np.abs(det) > 1e-12
    inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
    s = origins[:, None, :] - v0[None, :, :]
    u = np.einsum("rtk,tk->rt", s, p) * inv
    q = np.cross(s, e1[None, :, :])
    v = np.einsum("k,rtk->rt", direction, q) * inv
    t = np.einsum("rtk,tk->rt", q, e2) * inv
    hit = ok & (u >= -1e-9) & (v >= -1e-9) & (u + v <= 1.0 + 1e-9) & (t >= 0.0)
    t = np.where(hit, t, np.inf)
    first = t.argmin(axis=1)
    best = t[np.arange(len(origins)), first]
    any_hit = np.isfinite(best)
    points = origins + np.outer(np.where(any_hit, best, 0.0), direction)
    return points, n[first], any_hit


def _spray_points(node, stage, prim, samples):
    """Sprinkle points over the surface under each dab, Spacing apart.

    A dab tries a share of the disc's capacity -- Strength of it, times pressure -- so a pass
    lays down a scattering, and holding the brush over a spot fills it up to Spacing, never
    past it. Candidates are dropped onto the surface along the dab's normal rather than
    sampled from the triangles near it, which is what lets one big quad take a stroke as
    finely as a dense grid would. Subtract rubs points out, as in Points mode.

    With no surface (`prim` is None) each dab sprays whatever meshes are under it.
    """
    radius = float(node.get_param("radius").value or 0.0)
    existing, normals, widths = _current_points(node, stage)
    if node.get_param("mode").value == "Subtract":
        return _erase_points(node, stage, samples, existing, normals, widths, radius)
    if radius <= 0.0:
        return False
    if prim is not None:
        triangles = _spray_triangles(node, prim)
        if triangles is None:
            return False
        v0, v1, v2, tri_n = triangles
        lo = np.minimum(np.minimum(v0, v1), v2)
        hi = np.maximum(np.maximum(v0, v1), v2)
    spacing = _spray_spacing(node)
    strength = float(node.get_param("strength").value or 0.0)
    width = _spray_width(spacing, radius)
    placed = np.asarray(existing, dtype=np.float64).reshape(-1, 3)
    added = False

    for sample in samples:
        centre, pressure, normal = _unpack(sample)
        if centre is None:
            continue
        c = np.array([centre[0], centre[1], centre[2]], dtype=np.float64)
        if prim is None:
            local = _triangles_near(node, stage, c, radius)
            if local is None:
                continue
        else:
            near = np.flatnonzero(((lo - radius) <= c).all(axis=1) & ((hi + radius) >= c).all(axis=1))
            if not len(near):
                continue
            local = (v0[near], v1[near], v2[near], tri_n[near])
        if normal is not None:
            up = np.array([normal[0], normal[1], normal[2]], dtype=np.float64)
        else:
            # The facing of the surface nearest the dab, when the host did not send one.
            mid = (local[0] + local[1] + local[2]) / 3.0
            up = local[3][np.linalg.norm(mid - c, axis=1).argmin()]
        up = up / max(float(np.linalg.norm(up)), 1e-12)
        side = np.cross(up, [1.0, 0.0, 0.0] if abs(up[0]) < 0.9 else [0.0, 1.0, 0.0])
        side /= np.linalg.norm(side)
        other = np.cross(up, side)

        cell = spacing if spacing > 0.0 else radius * 0.1
        capacity = np.pi * radius * radius / (0.866 * cell * cell)
        goal = int(np.ceil(capacity * max(strength, 0.0) * max(pressure, 0.0)))
        if goal <= 0:
            continue
        # Seeded from where the dab landed, so the same gesture sprays the same points.
        rng = np.random.default_rng(abs(hash((round(c[0], 6), round(c[1], 6), round(c[2], 6), len(placed)))))
        tries = goal * 4
        r = radius * np.sqrt(rng.random(tries))
        a = rng.random(tries) * 2.0 * np.pi
        origins = c + np.outer(r * np.cos(a), side) + np.outer(r * np.sin(a), other) + up * radius
        hits, hit_n, ok = _cast_down(local, origins, -up)
        hits, hit_n = hits[ok], hit_n[ok]
        inside = np.linalg.norm(hits - c, axis=1) <= radius
        hits, hit_n = hits[inside], hit_n[inside]
        # Only points already near the dab can be too close to a new one.
        if len(placed) and spacing > 0.0:
            close = placed[np.linalg.norm(placed - c, axis=1) <= radius + spacing]
        else:
            close = np.zeros((0, 3))
        count = 0
        for h, hn in zip(hits, hit_n):
            if count >= goal:
                break
            if spacing > 0.0 and len(close) and (np.linalg.norm(close - h, axis=1) < spacing).any():
                continue
            close = np.vstack([close, h])
            placed = np.vstack([placed, h])
            existing.append(Gf.Vec3f(*h))
            normals.append(Gf.Vec3f(*hn))
            widths.append(width)
            count += 1
            added = True

    if not added:
        return False
    setattr(node, _STROKE_TOUCHED, True)
    _write_points(node, stage, existing, normals, widths)
    return True


def _spray_fill(node, stage, prim):
    """Spray the whole surface at Spacing -- Fill, for starting full and rubbing out."""
    triangles = _spray_triangles(node, prim)
    spacing = _spray_spacing(node)
    if triangles is None or spacing <= 0.0:
        return
    v0, v1, v2, tri_n = triangles
    areas = 0.5 * np.linalg.norm(np.cross(v1 - v0, v2 - v0), axis=1)
    total = float(areas.sum())
    # Random sequential packing stops at ~70% of hex packing; the cap keeps a typo from hanging.
    goal = int(min(total / (0.866 * spacing * spacing) * 0.7, 500_000))
    if goal <= 0:
        return
    rng = np.random.default_rng(len(areas))
    tries = goal * 3
    tri = np.minimum(np.searchsorted(np.cumsum(areas) / total, rng.random(tries)), len(areas) - 1)
    uv = rng.random((tries, 2))
    flip = uv.sum(axis=1) > 1.0
    uv[flip] = 1.0 - uv[flip]
    pts = v0[tri] * (1.0 - uv[:, :1] - uv[:, 1:]) + v1[tri] * uv[:, :1] + v2[tri] * uv[:, 1:]
    keys = np.floor(pts / spacing).astype(np.int64)
    grid, keep = {}, []
    spacing_sq = spacing * spacing
    for i in range(tries):
        kx, ky, kz = keys[i]
        pt = pts[i]
        clash = False
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for j in grid.get((kx + dx, ky + dy, kz + dz), ()):
                        d = pts[j] - pt
                        if d @ d < spacing_sq:
                            clash = True
                            break
                    if clash:
                        break
                if clash:
                    break
        if clash:
            continue
        grid.setdefault((kx, ky, kz), []).append(i)
        keep.append(i)
        if len(keep) >= goal:
            break
    width = _spray_width(spacing, 0.0)
    _write_points(node, stage, [Gf.Vec3f(*pts[i]) for i in keep],
                  [Gf.Vec3f(*tri_n[tri[i]]) for i in keep], [width] * len(keep))


def _points_path(node):
    own = getattr(node, "_exec_prim_path", None) or getattr(node, "prim_path", None)
    return Sdf.Path(str(own)).AppendChild(POINTS_PRIM) if own else None


def _current_points(node, stage):
    """The points placed so far: position, surface normal and the size each was painted at."""
    held = getattr(node, _POINTS, None)
    if held is not None:
        return [list(part) for part in held]
    path = _points_path(node)
    prim = stage.GetPrimAtPath(path) if path else None
    if not prim or not prim.IsValid():
        # Not cooked since a reopen: add to what was saved rather than starting over.
        recorded = _recorded_points(node, stage)
        return [list(part) for part in recorded] if recorded else ([], [], [])
    cloud = UsdGeom.Points(prim)
    points = cloud.GetPointsAttr().Get()
    normals = cloud.GetNormalsAttr().Get()
    widths = cloud.GetWidthsAttr().Get()
    points = [Gf.Vec3f(*p) for p in points] if points else []
    normals = [Gf.Vec3f(*n) for n in normals] if normals else []
    widths = [float(w) for w in widths] if widths else []
    # Points placed before these were recorded still load: they lie flat, at today's size.
    while len(normals) < len(points):
        normals.append(Gf.Vec3f(0, 0, 1))
    while len(widths) < len(points):
        widths.append(_dab_width(node))
    return points, normals, widths


def _dab_width(node):
    """How big a point placed *now* should be drawn, from the radius at this moment.

    Recorded per point rather than applied to all of them. Deriving every width from the
    current radius would mean changing it resizes every point already placed, and rebuilds
    the whole array to do it. Per point,
    the slider only ever describes the next dab, and painting at several sizes is free.
    """
    return max(float(node.get_param("radius").value or 1.0) * 0.25, 1e-4)


def _write_points(node, stage, points, normals=None, widths=None):
    path = _points_path(node)
    if path is None:
        return
    published = node.get_param("points_path")
    if published is not None and published.value != str(path):
        published.value = str(path)

    if widths is None:
        widths = [_dab_width(node)] * len(points)
    if normals is None:
        normals = [Gf.Vec3f(0, 0, 1)] * len(points)
    setattr(node, _POINTS, (list(points), list(normals), [float(w) for w in widths]))

    layer = node.get_edit_context()
    context = Usd.EditContext(stage, chunk_utils.edit_target(stage, layer)) if layer else contextlib.nullcontext()
    with context:
        cloud = UsdGeom.Points.Define(stage, path)
        cloud.CreatePointsAttr().Set(Vt.Vec3fArray(points))
        cloud.CreateNormalsAttr().Set(Vt.Vec3fArray(normals))
        # The size each point was painted at, kept with the point. Deriving it from the
        # current radius resized everything already placed whenever the slider moved.
        cloud.CreateWidthsAttr().Set(Vt.FloatArray([float(w) for w in widths]))
        UsdGeom.Primvar(cloud.CreateDisplayColorAttr()).SetInterpolation(UsdGeom.Tokens.constant)
        cloud.GetDisplayColorAttr().Set(
            Vt.Vec3fArray([Gf.Vec3f(*(node.get_param("paint_color").value or (0.15, 0.55, 1.0)))])
        )


def _blend(mode, current, amount):
    """New weights for every vertex one dab reached. Clamped: the value is a probability.

    Elementwise over arrays rather than a value at a time -- a dab on a scanned mesh covers
    tens of thousands of vertices, and they all blend the same way.
    """
    if mode == "Subtract":
        return np.maximum(0.0, current - amount)
    if mode == "Smooth":
        # Toward the middle rather than toward a target: with no neighbour information here,
        # this evens a field out without needing the mesh's topology.
        return current + (0.5 - current) * amount
    return np.minimum(1.0, current + amount)


def _unpack(sample):
    """One sample as (point, pressure, normal).

    Three shapes are ordinary for a host to send, and none should be a special case it has to
    know about: a bare point (a plain raycast hit has no pressure), a (point, pressure) pair
    (a tablet does), and a (point, pressure, normal) triple -- the surface's facing at the
    hit, which Points mode records so an instance can be laid flat on what it was painted on.
    """
    if isinstance(sample, (tuple, list)):
        if len(sample) == 3 and not isinstance(sample[0], (int, float)):
            point, pressure, normal = sample
            return Gf.Vec3d(point[0], point[1], point[2]), float(pressure), normal
        if len(sample) == 2 and not isinstance(sample[0], (int, float)):
            point, pressure = sample
            return Gf.Vec3d(point[0], point[1], point[2]), float(pressure), None
    try:
        return Gf.Vec3d(sample[0], sample[1], sample[2]), 1.0, None
    except (TypeError, IndexError):
        logger.warning("Ignoring unreadable brush sample %r", sample)
        return None, 0.0, None


def _bump_strokes(node, reset=False):
    """Tell the chain something changed. See `_stroke_finished` for why it is a param write.

    Every edit counts, including Subtract: rubbing points out changes the result exactly as
    much as painting them in, and the instances have to follow either way. The value is a
    revision number rather than a count of strokes -- naming it the latter made it look wrong
    going up while something was being removed.

    `reset` is for Clear, which does not merely change the paint -- it takes it all away, and a
    number that survived having nothing left to count read as the button not having worked.
    """
    # Fill and Clear change the field as much as a stroke does, so the saved copy follows them
    # too -- including Clear, which has to record that there is now nothing.
    _record_paint(node)
    try:
        counter = node.get_param("stroke_count")
        current = int(counter.value or 0)
        # Still a write when it was already zero, so the chain hears about a Clear on a scene
        # that was loaded rather than painted this session.
        counter.value = 0 if reset else current + 1
        if reset and current == 0:
            counter.value = 0
    except Exception:  # noqa: BLE001
        logger.debug("Could not recook %s", node.name, exc_info=True)


def _set_everywhere(node, value):
    """Author one weight across every mesh under the surface."""
    _drop_stroke_cache(node)
    prim, stage = _surface_prim(node)
    # Points need no surface, so Clear works without one too.
    stage = stage or node.get_stage()
    if stage is not None and node.get_param("output").value in _POINT_OUTPUTS:
        # Points are the output in these modes, so Fill/Clear act on them instead. Filling a
        # cloud of single dabs has no meaning -- there is no "everywhere" to place one at --
        # but Spray has a spacing, so filling sprays the whole surface.
        if value == 0.0:
            _write_points(node, stage, [], [], [])
        elif node.get_param("output").value == _OUTPUT_SPRAY and prim is not None:
            _spray_fill(node, stage, prim)
        # After the change, not before. Bumping first would recook the chain while the points
        # are still there, so Clear would empty the cloud and leave every instance standing.
        _bump_strokes(node, reset=(value == 0.0))
        return
    if prim is None:
        return
    for mesh_prim, count in _paint_meshes(prim):
        _write_weights(node, stage, mesh_prim, np.full(count, value, dtype=np.float64))
    _bump_strokes(node, reset=(value == 0.0))


def _show_paint_changed(owner, notice, sender):
    """Re-author the tint, or take it away, without touching the field itself."""
    node = owner.get_chunk()
    _show_points(node, node.get_stage())
    prim, stage = _surface_prim(node)
    if prim is None:
        return
    for mesh_prim, count in _paint_meshes(prim):
        _write_weights(node, stage, mesh_prim, _current_weights(mesh_prim, count))


def _show_points(node, stage):
    """Hide or show the placed points along with the tint.

    Show Paint means "let me see what I painted", and in Points mode what you painted *is*
    the points -- leaving them lit while the tint went away only turned off half of it.
    Visibility rather than deletion, so unticking loses nothing.
    """
    path = _points_path(node)
    prim = stage.GetPrimAtPath(path) if (stage is not None and path) else None
    if not prim or not prim.IsValid():
        return
    show = node.get_param("show_paint")
    layer = node.get_edit_context()
    context = Usd.EditContext(stage, chunk_utils.edit_target(stage, layer)) if layer else contextlib.nullcontext()
    with context:
        imageable = UsdGeom.Imageable(prim)
        imageable.MakeVisible() if (show and show.value) else imageable.MakeInvisible()


def _clear_paint(owner, notice, sender):
    """Wipe the field back to unpainted."""
    _set_everywhere(owner.get_chunk(), 0.0)


def _fill_paint(owner, notice, sender):
    """Paint the whole surface, as a starting point for subtracting away from."""
    _set_everywhere(owner.get_chunk(), 1.0)


#: Where the paint is kept so it survives a save, on the chunk's own prim.
#:
#: A saved scene is chunk prims and nothing else, on the premise that reopening re-runs every
#: chunk and so regenerates whatever it is responsible for (see chunk_stage.save_scene). Every
#: other chunk holds to that: its output is a function of its params. This one does not and
#: says so in its own docstring -- the paint *is* the output, it lives in a per-chunk layer,
#: and that layer is not what gets written. Two documented designs, each correct alone.
#:
#: So the field is also recorded on the chunk prim, which `export_chunks` copies wholesale
#: (Sdf.CopySpec, subtree and all). That puts this chunk back under the same rule as every
#: other one: everything needed to rebuild its output is on its own prim.
#:
#: Sparse, because it has to go into a .usda as text. Paint is mostly zero -- the whole point
#: of it is that it covers part of a surface -- so only the vertices that carry weight are
#: written. A stroke on a 340k-vertex scan costs the few thousand it actually touched.
_PAINT_MESHES = "chunk:paint:meshes"
_PAINT_COUNTS = "chunk:paint:counts"
_PAINT_SPANS = "chunk:paint:spans"
_PAINT_INDICES = "chunk:paint:indices"
_PAINT_WEIGHTS = "chunk:paint:weights"
#: The placed points (Points and Spray), recorded beside the field for the same reason. They
#: are their own output, a point cloud under the chunk prim in the cook layer, and that was
#: never written either -- a reopened scene came back with the field and without its points.
_PAINT_POINTS = "chunk:paint:points"
_PAINT_NORMALS = "chunk:paint:normals"
_PAINT_WIDTHS = "chunk:paint:widths"


def _chunk_prim_path(node):
    own = getattr(node, "_exec_prim_path", None) or getattr(node, "prim_path", None)
    return Sdf.Path(str(own)) if own else None


def _record_paint(node):
    """Copy the whole painted field onto the chunk prim, so a save carries it.

    Called when an edit finishes rather than per tick: a stroke re-authors weights on every
    mouse-move, and re-flattening every painted mesh each time is the sticky drag `_stamp`
    exists to avoid.
    """
    _record_points(node)
    prim, stage = _surface_prim(node)
    path = _chunk_prim_path(node)
    if prim is None or path is None:
        return

    meshes, counts, spans, indices, weights = [], [], [], [], []
    for mesh_prim, count in _paint_meshes(prim):
        values = _current_weights(mesh_prim, count)
        painted = np.flatnonzero(values > 0.0)
        if not len(painted):
            continue
        meshes.append(str(mesh_prim.GetPath()))
        counts.append(int(count))
        spans.append(int(len(painted)))
        indices.extend(int(i) for i in painted)
        weights.extend(float(v) for v in values[painted])

    layer = chunk_utils.get_bookkeeping_layer(stage, node)
    with Usd.EditContext(stage, chunk_utils.edit_target(stage, layer)):
        chunk_prim = stage.GetPrimAtPath(path)
        if not chunk_prim or not chunk_prim.IsValid():
            return
        for name, kind, value in (
            (_PAINT_MESHES, Sdf.ValueTypeNames.StringArray, Vt.StringArray(meshes)),
            (_PAINT_COUNTS, Sdf.ValueTypeNames.IntArray, Vt.IntArray(counts)),
            (_PAINT_SPANS, Sdf.ValueTypeNames.IntArray, Vt.IntArray(spans)),
            (_PAINT_INDICES, Sdf.ValueTypeNames.IntArray, Vt.IntArray(indices)),
            (_PAINT_WEIGHTS, Sdf.ValueTypeNames.FloatArray, Vt.FloatArray(weights)),
        ):
            chunk_prim.CreateAttribute(name, kind, custom=True).Set(value)


def _record_points(node):
    """Copy the placed points onto the chunk prim, so a save carries them.

    Only once the chunk holds points -- a brush that has only ever painted density records
    nothing -- but an emptied cloud is recorded, so a Clear survives the save too.
    """
    held = getattr(node, _POINTS, None)
    stage = node.get_stage()
    path = _chunk_prim_path(node)
    if held is None or stage is None or path is None:
        return
    points, normals, widths = held
    layer = chunk_utils.get_bookkeeping_layer(stage, node)
    with Usd.EditContext(stage, chunk_utils.edit_target(stage, layer)):
        chunk_prim = stage.GetPrimAtPath(path)
        if not chunk_prim or not chunk_prim.IsValid():
            return
        for name, kind, value in (
            (_PAINT_POINTS, Sdf.ValueTypeNames.Point3fArray, Vt.Vec3fArray(points)),
            (_PAINT_NORMALS, Sdf.ValueTypeNames.Normal3fArray, Vt.Vec3fArray(normals)),
            (_PAINT_WIDTHS, Sdf.ValueTypeNames.FloatArray, Vt.FloatArray(widths)),
        ):
            chunk_prim.CreateAttribute(name, kind, custom=True).Set(value)


def _recorded_points(node, stage):
    """`(points, normals, widths)` from the chunk prim, or None when nothing was recorded."""
    path = _chunk_prim_path(node)
    chunk_prim = stage.GetPrimAtPath(path) if path else None
    if not chunk_prim or not chunk_prim.IsValid():
        return None
    attr = chunk_prim.GetAttribute(_PAINT_POINTS)
    if not attr or not attr.HasValue():
        return None

    def read(name):
        other = chunk_prim.GetAttribute(name)
        return other.Get() if other and other.HasValue() else None

    points = [Gf.Vec3f(*p) for p in attr.Get() or []]
    normals = [Gf.Vec3f(*n) for n in read(_PAINT_NORMALS) or []]
    widths = [float(w) for w in read(_PAINT_WIDTHS) or []]
    # Lengths that disagree are made to agree, the way `_current_points` treats a cloud
    # placed before normals and widths were kept.
    normals = (normals + [Gf.Vec3f(0, 0, 1)] * len(points))[:len(points)]
    widths = (widths + [_dab_width(node)] * len(points))[:len(points)]
    return points, normals, widths


def _recorded_paint(node, stage):
    """`{mesh path: weights}` from the chunk prim, or an empty dict when nothing was recorded."""
    path = _chunk_prim_path(node)
    chunk_prim = stage.GetPrimAtPath(path) if path else None
    if not chunk_prim or not chunk_prim.IsValid():
        return {}

    def read(name, default):
        attr = chunk_prim.GetAttribute(name)
        return attr.Get() if attr and attr.HasValue() else default

    meshes = list(read(_PAINT_MESHES, []) or [])
    counts = list(read(_PAINT_COUNTS, []) or [])
    spans = list(read(_PAINT_SPANS, []) or [])
    indices = np.asarray(read(_PAINT_INDICES, []) or [], dtype=np.int64)
    weights = np.asarray(read(_PAINT_WEIGHTS, []) or [], dtype=np.float64)
    if not meshes or len(meshes) != len(counts) or len(counts) != len(spans):
        return {}

    restored = {}
    at = 0
    for mesh_path, count, span in zip(meshes, counts, spans):
        where = indices[at:at + span]
        what = weights[at:at + span]
        at += span
        if len(where) != span or len(what) != span:
            logger.warning("Painted field on %s is truncated; ignoring it", mesh_path)
            return {}
        values = np.zeros(int(count), dtype=np.float64)
        # A mesh that changed resolution since the paint was recorded keeps whatever still
        # lands on it, rather than throwing the stroke away entirely.
        inside = where < len(values)
        values[where[inside]] = what[inside]
        restored[str(mesh_path)] = values
    return restored


def execute(node):
    """Publish the output paths, and author the paint this chunk holds.

    The framework cleared this chunk's layer before the cook, so everything the paint puts on
    the stage -- the field, its tint, the placed points -- is authored again here from what
    the chunk holds (see the module docstring). After a reopen it holds nothing yet, and
    starts from what was recorded on the chunk prim.
    """
    # Published whether or not anything has been painted, so the port has a value to carry
    # from the moment the chunk exists. It only appeared on the first write before, which
    # meant the Painted Points output could not be connected until after painting -- exactly
    # backwards, since connecting it is how you decide to paint.
    path = _points_path(node)
    published = node.get_param("points_path")
    if path is not None and published is not None and published.value != str(path):
        published.value = str(path)

    stage = node.get_stage()
    held_points = getattr(node, _POINTS, None)
    if held_points is None and stage is not None:
        # A reopened scene: nothing in memory yet, so start from what the chunk prim recorded.
        held_points = _recorded_points(node, stage)
    if held_points is not None and stage is not None:
        _write_points(node, stage, *held_points)
        _show_points(node, stage)

    prim, stage = _surface_prim(node)
    if prim is None:
        return

    held = getattr(node, _FIELD, {})
    # Only read when the chunk holds nothing for a mesh -- a reopened scene. Live, the held
    # field is newer than the recorded one: it is recorded when a stroke ends, and a cook can
    # land in the middle of one.
    saved = None

    # Written whether or not anything was painted, so a scatter downstream resolves something
    # rather than nothing the moment the two are wired together. Per mesh, because the surface
    # a user picks is a hierarchy far more often than it is a single Mesh.
    for mesh_prim, count in _paint_meshes(prim):
        weights = held.get(str(mesh_prim.GetPath()))
        if weights is None or len(weights) != count:
            if saved is None:
                saved = _recorded_paint(node, stage)
            weights = saved.get(str(mesh_prim.GetPath()))
            if weights is None:
                weights = np.zeros(count, dtype=np.float64)
            elif len(weights) != count:
                weights = np.resize(weights, count)
        _write_weights(node, stage, mesh_prim, weights)


def main():
    chunk_api = chunk.Chunk("Scatter Brush")

    # Ordered as the work is done: what to paint on, what a stroke produces, how the brush
    # behaves, how the result is shown, then the two whole-surface actions. Grew by accretion
    # instead, with the mode switch buried below the brush settings it governs.
    chunk_api.add_param(chunk.LineEdit(
        "surface_prim", "Surface Mesh",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="The mesh to paint on. Wire this chunk upstream of Scatter Points. Leave it "
                "empty to paint Points or Spray on anything under the cursor.",
    ))

    output = chunk.Menu(
        "output", "Output", menu_items=_OUTPUTS, default_value=_OUTPUT_DENSITY,
        tooltip="Density paints a weight and lets Scatter Points decide where instances land. "
                "Points drops one exactly where you click. Spray sprinkles points over the "
                "surface, Spacing apart -- wire them into Painted Points for an instance each, "
                "or into Scatter Points' Grow Near to paint a region at any mesh resolution.",
    )
    chunk_api.add_param(output)

    # Published so a Scatter Points can be *wired* to this brush rather than agreeing with it
    # by convention. OUTPUT makes it a real outputs: port on the chunk prim, which is what the
    # node graph draws as a connectable socket -- the same shape Scatter Points already uses to
    # hand its instancer to the RBD Solver.
    # The two outputs, together: one per mode, and only one carries anything at a time.
    # Adjacent because they are the same decision seen twice.
    chunk_api.add_param(chunk.LineEdit(
        "density_name", "Density Field",
        default_value=DENSITY_PRIMVAR,
        flags=chunk.ChunkFlags.ParamFlags.OUTPUT | chunk.ChunkFlags.ParamFlags.LABELED,
    ))
    chunk_api.add_param(chunk.LineEdit(
        "points_path", "Painted Points",
        flags=chunk.ChunkFlags.ParamFlags.OUTPUT | chunk.ChunkFlags.ParamFlags.LABELED,
    ))

    # A revision number, not a tally of painting. It exists solely to be written: painting
    # writes USD rather than params, so without a param change nothing downstream notices.
    # Every edit bumps it -- adding, subtracting and filling alike, because all three change
    # the result and all three have to recook -- and Clear puts it back to nothing.
    #
    # Named "Strokes" at first, which read as a count of painting and so looked wrong going up
    # while you were rubbing points out. OUTPUT keeps it out of the UI either way.
    chunk_api.add_param(chunk.Slider(
        "stroke_count", "Revision", default_value=0, is_int=True,
        flags=chunk.ChunkFlags.ParamFlags.OUTPUT,
    ))

    _GF = (chunk_core.ChunkFlags.GroupFlags.COLLAPSABLE
           | chunk_core.ChunkFlags.GroupFlags.LABELED
           | chunk_core.ChunkFlags.GroupFlags.FRAMED)

    grp = chunk.Group("brush_grp", "Brush", icon="brush", flags=_GF)
    radius = chunk.Slider(
        "radius", "Radius", min_value=0.0, max_value=100.0, default_value=10.0,
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="Brush size in scene units. The viewport draws its ring from this, and in "
                "Points mode it is also the spacing between placed points.",
    )
    strength = chunk.Slider(
        "strength", "Strength", min_value=0.0, max_value=1.0, default_value=0.5,
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="How much one stroke adds or removes. In Spray, how much of the brush one "
                "dab fills -- hold it over a spot to fill it up to Spacing.",
    )
    spray_spacing = chunk.Slider(
        "spray_spacing", "Spacing", min_value=0.0, max_value=20.0, default_value=1.0,
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="Spray: the closest two sprayed points may be. Fill sprays the whole surface "
                "at this spacing.",
    )
    mode = chunk.Menu(
        "mode", "Mode", menu_items=_MODES, default_value="Add",
        tooltip="Add paints instances in, Subtract paints them out, Smooth evens the field. "
                "Points and Spray: Add and Subtract.",
    )
    # First in the group: it decides what Radius and Strength mean.
    grp.add_param(mode)
    grp.add_param(radius)
    grp.add_param(strength)
    grp.add_param(spray_spacing)
    stack = chunk.Checkbox(
        "stack", "Stack", default_value=False,
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="Points: spacing only counts points from the current stroke, so a new stroke "
                "can land on top of earlier instances. Leave Surface Mesh empty to paint on "
                "them. Off: a dab is skipped within Radius of any point already placed.",
    )
    grp.add_param(stack)
    chunk_api.add_param(grp)

    display = chunk.Group("display_grp", "Display", icon="eye", flags=_GF)
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
        tooltip="Tint the surface by painted weight. While it is on, the surface's material "
                "is set aside so the tint is actually visible -- displayColor loses to any "
                "shader otherwise. Both opinions live in this chunk's layer, so unticking "
                "puts the original look back.",
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
    # settings that produced it. Without this, dragging Radius would recook the whole chain
    # on every tick.
    for param in (radius, strength, mode, stack, colour, show):
        param.presentation_only = True

    # The drag bracket the host already sends. GIZMO_START drops the geometry cache so a
    # stroke reads the surface as it is now; GIZMO_STOP is the one moment a recook is wanted.
    chunk_api.add_callback(_stroke_started, notice_alias=chunk.ChunkNoticeAlias.GIZMO_START)
    chunk_api.add_callback(_stroke_finished, notice_alias=chunk.ChunkNoticeAlias.GIZMO_STOP)

    return chunk_api


def config():
    return chunk.DefaultConfig(
        # Declared in full rather than as `gizmo=` sugar: sugar is type-only, and a brush
        # needs its radius named so the viewport can draw a ring the right size. The driver
        # is what the handle *reads*, unusually -- a brush writes a field, not params.
        gizmos=[
            chunk.Gizmo("paint", type="brush", label="Paint Density",
                        anchor="surface_prim",
                        driver=("brush_grp.radius", "brush_grp.strength")),
        ],
        category=chunk.StandardCategory.MODELLING,
        icon="brush",
        # A stroke is array work over every vertex on the surface -- see `_stamp`.
        requirements=["numpy>=2,<3"],
        description="""
        Scatter Brush — paints a per-vertex weight on a mesh saying where
        Scatter Points may place instances.

        Wire this chunk's Density Field output into a Scatter Points chunk's
        Density Field input, with both pointed at the same mesh. Painting writes
        primvars:scatterDensity on the surface, and the wire is what tells that
        scatter to read it — an unconnected scatter is unaffected. Weight is a
        probability, so 0.5 gives roughly half as many instances, not a hard edge.

        Parameters:
          - Surface Mesh: the mesh to paint on. Empty: Points and Spray
            paint on anything under the cursor (Density needs a mesh).
          - Radius: brush size in scene units.
          - Strength: how much a single stroke adds or removes.
          - Mode: Add paints instances in, Subtract paints them out, Smooth
            evens the field toward the middle.
          - Stack (Points): spacing only counts the current stroke, so new
            points can land on top of earlier instances, for towers.
          - Fill / Clear: paint everything or nothing, for starting from
            either end.
          - Output: Density (the field), Points (one per click) or Spray
            (points sprinkled under the brush, Spacing apart -- a painted
            region at any mesh resolution, wired into Painted Points or into
            Scatter Points' Grow Near).

        The painted field lives in this chunk's own layer and saves with the
        scene. Density resolution is mesh resolution — the weight is per
        vertex, so a coarse mesh gives a blocky edge.
        """,
    )
