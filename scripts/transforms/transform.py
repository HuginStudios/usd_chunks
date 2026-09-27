from __future__ import annotations
from pxr import Gf, Sdf, Usd, UsdGeom
from usd_chunks import chunk, chunk_core, chunk_gizmo, chunk_utils

#: The point this chunk rotates and scales about, in the target prim's own object space --
#: the space this chunk's op takes as input, since an appended op is the innermost one.
_PIVOT_PARAMS = ("px", "py", "pz")
_TRANSLATE_PARAMS = ("tx", "ty", "tz")

#: Where the last pivot the chunk composed with is remembered, so a pivot move can be
#: absorbed into translate rather than swinging the object.
_PIVOT_APPLIED = "_pivot_applied"

#: Set while a pivot move is being applied. The move writes all three pivot params, and each
#: write fires the very callback that started it -- so without this one move ran four
#: compensations and authored three half-moved placements on the way, each of which cooks and
#: is visible. It landed on the right answer only because the correction telescopes, which is
#: not a property to depend on.
_PIVOT_BUSY = "_pivot_busy"


def _rescale_translate_range(node, stage):
    """Keep the translate sliders' range at a constant ~10m-equivalent span regardless of
    the stage's declared scale, instead of a flat -1000..1000 in raw stage units. That flat
    range only felt right by accident for a centimeters-scale stage (metersPerUnit=0.01,
    USD's own default when unauthored -- 10 / 0.01 = 1000); a meters-scale stage
    (metersPerUnit=1) would get the same +-1000 range applied to a scene where objects are
    typically single-digit units across, so a small drag (or typing "5") could move
    something many times its own size."""
    meters_per_unit = UsdGeom.GetStageMetersPerUnit(stage)
    if not meters_per_unit:
        return
    span = 10.0 / meters_per_unit
    # The pivot gets the same range, for want of a better guess. It is measured in the target
    # prim's object space rather than in stage units, and nothing here knows how big that is
    # until a prim is targeted -- but a range sized to the stage beats the flat default.
    for name in _TRANSLATE_PARAMS + _PIVOT_PARAMS:
        param = node.get_param(name)
        if param is None:
            continue
        param.min_value = -span
        param.max_value = span


def _rotation(node):
    return (
        Gf.Rotation(Gf.Vec3d(1, 0, 0), node.get_param("rx").value) *
        Gf.Rotation(Gf.Vec3d(0, 1, 0), node.get_param("ry").value) *
        Gf.Rotation(Gf.Vec3d(0, 0, 1), node.get_param("rz").value)
    )


def _scale(node):
    return Gf.Vec3d(
        node.get_param("sx").value or 1.0,
        node.get_param("sy").value or 1.0,
        node.get_param("sz").value or 1.0,
    )


def pivot(node):
    """The point this chunk turns about, or None while it has none.

    All zero means *unset*, not "the prim's origin". Nothing is then authored onto the chunk
    prim and the host goes on placing the handle where it always did -- on the geometry it can
    see. That matters for the common asset whose transform origin is nowhere near the thing
    you are looking at: a Kitchen_set cup carries a transform on its group and the exact
    inverse on the mesh, so its origin composes to the world origin, and honouring a literal
    zero pivot would put the handle out there.

    Origin is still reachable -- it is what the composition does with no pivot -- so the only
    thing given up is telling "at the origin" apart from "not set", which nothing needs.
    """
    values = []
    for name in _PIVOT_PARAMS:
        param = node.get_param(name)
        if param is None:
            return None
        values.append(float(param.value or 0.0))
    return tuple(values) if any(values) else None


def execute(node):
    stage = node.get_stage()
    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    # Before the primpath guard below -- the slider range should be correct as soon as the
    # chunk exists, not only once a target prim is set.
    _rescale_translate_range(node, stage)

    # Seeded on the first cook, which is also the first thing to happen after a scene loads
    # and restores param values. Without it the first pivot edit of a reloaded scene would
    # compensate against a remembered zero and shunt the object by the whole pivot.
    if not hasattr(node, _PIVOT_APPLIED):
        setattr(node, _PIVOT_APPLIED, pivot(node) or (0.0, 0.0, 0.0))

    primpath_param = node.get_param("primpath_location")
    # get_param returns None while the chunk's params are being torn down (remove re-runs
    # execute via the DISABLED-unset). Bail rather than crash on None.value.
    if not primpath_param or not primpath_param.value:
        return

    xform = Gf.Transform()
    xform.SetTranslation(Gf.Vec3d(
        node.get_param("tx").value,
        node.get_param("ty").value,
        node.get_param("tz").value,
    ))
    xform.SetRotation(_rotation(node))
    xform.SetScale(_scale(node))
    if (point := pivot(node)) is not None:
        # Gf.Transform composes this as T(-p) * S * R * T(p) * T(t), so the pivot is a fixed
        # point of the rotation and scale but translate still moves the object as a whole --
        # which is what a pivot has to mean for the sliders to keep behaving as they read.
        xform.SetPivotPosition(Gf.Vec3d(*point))
    matrix = xform.GetMatrix()

    for target_path in chunk_utils.iter_prim_targets(stage, primpath_param):
        prim = stage.GetPrimAtPath(target_path)
        if not prim.IsValid():
            continue

        over = stage.OverridePrim(target_path)
        xformable = UsdGeom.Xformable(over)

        # This chunk's own op, not a shared xformOp:transform. Two chunks placing one prim
        # would compete for one attribute, and USD settles that by picking the strongest
        # layer and discarding the rest -- a Transform and an Offset Transform on the same
        # prim would annihilate each other rather than compose. Owning a named op each,
        # they compose.
        op_name = chunk_utils.xform_op_name(node)

        # Built from the exec chain, not from what happens to be composed at this instant.
        # Every chunk writes its own copy of xformOpOrder into its own layer and USD keeps
        # only the strongest, so copies built from "what I can see right now" DIFFER and the
        # result depends on which one survives -- a chunk that cooked before its neighbour
        # existed holds a list naming only itself, and if that copy wins the neighbour's op
        # is never read. Copies built from the graph are identical, so it stops mattering
        # which one is kept.
        order = chunk_gizmo.xform_op_order(node, prim, op_name)
        # Author the attribute outright rather than going through GetOrderedXformOps() /
        # AddTransformOp(). Both of those trust xformOpOrder, and xformOpOrder can name an
        # op that no layer defines with a type -- ordinary leftover state after chunks are
        # deleted and re-added, since the order and the attribute live in different layers
        # and go away independently. In that state the two USD versions in use here fail
        # differently and both fail: 25.05 hands back an untyped op and Set() raises "Empty
        # typeName for <...xformOp:transform>", while 25.08 skips the op, leaves `existing`
        # empty, and AddTransformOp() then raises because the name IS already in the order.
        #
        # CreateAttribute is idempotent and gives the attribute its type in THIS chunk's
        # layer, which is what makes the composed op well-formed again regardless of what
        # any other layer left behind.
        transform_attr = over.CreateAttribute(
            op_name, Sdf.ValueTypeNames.Matrix4d, custom=False
        )
        transform_attr.Set(matrix)
        # Authored into this chunk's own layer, so the asset's own ops are untouched and
        # come back the moment this chunk is deleted or muted.
        xformable.CreateXformOpOrderAttr().Set(order)


def gizmo_changed(node, gizmo, value):
    """Turn a dragged absolute placement into this chunk's own op value.

    The handle reports where the object should END UP. This chunk's op composes on top of
    everything upstream of it -- the asset's ops, and any chunk before it in the chain -- so
    writing the dragged matrix in raw would apply that upstream placement twice.

    No seeding is needed: the op composes on top of the prim's transform rather than
    replacing it, so an untouched chunk contributes identity and moves nothing, and a node
    behaves the same whether it was dragged into existence or added by hand.
    """
    if gizmo != "transform":
        return False
    primpath_param = node.get_param("primpath_location")
    if not primpath_param or not primpath_param.value:
        return False
    stage = node.get_stage()
    targets = list(chunk_utils.iter_prim_targets(stage, primpath_param))
    if not targets:
        return False

    prim = stage.GetPrimAtPath(targets[0])
    if not prim.IsValid():
        return False

    upstream = _upstream_of(node, prim)
    # Solved about this chunk's own pivot, so the params that come back recompose to exactly
    # the matrix the handle reported. Skip that and the object lands short of the mouse by
    # however far the pivot swings under the rotation.
    parts = chunk_gizmo.decompose(Gf.Matrix4d(value) * upstream.GetInverse(), pivot=pivot(node))
    for names, key in ((_TRANSLATE_PARAMS, "translate"),
                       (("rx", "ry", "rz"), "rotate"),
                       (("sx", "sy", "sz"), "scale")):
        for name, component in zip(names, parts[key]):
            node.get_param(name).value = float(component)
    return True


def gizmo_offset(node, gizmo):
    """Draw the handle on the pivot, once there is one.

    The handle and the sliders then agree: grab the rotate ring and it turns about the same
    point typing into rx does. With no pivot this returns None and the host keeps placing the
    handle itself, which is the behaviour that was there before pivots existed -- a parent's
    handle sitting at the parent's placement, which is the Houdini-ish rotation centre this
    feature grew out of.
    """
    if gizmo != "transform":
        return None
    return pivot(node)


def gizmo_offset_changed(node, gizmo, value):
    """Drag the handle itself to move the pivot, without moving the prim.

    The host has already converted its world-space drag into this prim's own space, so this
    is the same operation as typing into the pivot sliders -- and goes through the same
    function, so it compensates translate identically. Dragging the pivot across the object
    never shifts the object; it only changes what the next rotation turns about.
    """
    if gizmo != "transform":
        return False
    _set_pivot(node, value)
    return True


def _upstream_of(node, prim):
    """Everything that applies to `prim` before this chunk's own op.

    Taken as the composed local transform with this chunk's contribution divided back out,
    rather than by re-composing the op list by hand: the composed value is the one actually
    in effect, muting and layer strength included.
    """
    xformable = UsdGeom.Xformable(prim)
    local = xformable.GetLocalTransformation(Usd.TimeCode.Default())
    if isinstance(local, tuple):
        local = local[0]

    mine = Gf.Matrix4d(1.0)
    attr = prim.GetAttribute(chunk_utils.xform_op_name(node))
    if attr and attr.IsValid():
        current = attr.Get()
        if current is not None:
            mine = Gf.Matrix4d(current)
    # Ordering, verified rather than assumed: for xformOpOrder [rot, translate] USD
    # composes T * R, so the LAST op multiplies on the left. This chunk's op is appended,
    # hence local = mine * upstream and upstream = mine^-1 * local. Getting this backwards
    # is not a small error -- it conjugates the drag, so a 5-unit move through an asset
    # rotated -90 and scaled 100 came out as -500 on a different axis.
    return mine.GetInverse() * local


def _set_pivot(node, new):
    """Move the pivot without moving the object, by absorbing the difference into translate.

    A pivot says what a rotation turns about; it is not itself a placement. Composing about
    a moved pivot with a rotation already set would swing the object -- which is what
    Houdini's Transform SOP does, and what gets reported as "typing in the pivot field
    teleported my prop". Maya's move-pivot compensates instead, and that is the behaviour
    picked here.

    The correction is exact: the composition's translation row is ``-p*A + p + t`` for the
    linear part A, so holding it constant across a pivot move gives
    ``t' = t + (A*p' - p') - (A*p - p)``.
    """
    if getattr(node, _PIVOT_BUSY, False):
        return

    old = getattr(node, _PIVOT_APPLIED, None) or (0.0, 0.0, 0.0)
    new = tuple(float(component) for component in new)
    setattr(node, _PIVOT_APPLIED, new)
    if new == tuple(old):
        return

    linear = Gf.Matrix4d().SetScale(_scale(node)) * Gf.Matrix4d().SetRotate(_rotation(node))
    old_point, new_point = Gf.Vec3d(*old), Gf.Vec3d(*new)
    delta = (linear.Transform(new_point) - new_point) - (linear.Transform(old_point) - old_point)

    setattr(node, _PIVOT_BUSY, True)
    try:
        for name, component in zip(_PIVOT_PARAMS, new):
            node.get_param(name).value = component
        for name, component in zip(_TRANSLATE_PARAMS, delta):
            param = node.get_param(name)
            param.value = float(param.value or 0.0) + component
    finally:
        setattr(node, _PIVOT_BUSY, False)


def _pivot_changed(owner, notice, sender):
    """Compensate translate after the user edits one of the pivot sliders."""
    node = owner.get_chunk()
    if node is None:
        return
    _set_pivot(node, pivot(node) or (0.0, 0.0, 0.0))


def _center_pivot(owner, notice, sender):
    """Put the pivot on the middle of the target's geometry.

    The untransformed bound, not the world one: an appended op is the innermost, so this
    chunk composes in the prim's own object space and a world-space centroid would be off by
    everything upstream of it. Computed on demand rather than on every cook -- it is a bbox,
    and a cook happens on every tick of every slider drag.
    """
    node = owner.get_chunk()
    stage = node.get_stage() if node else None
    if stage is None:
        return

    primpath_param = node.get_param("primpath_location")
    if not primpath_param or not primpath_param.value:
        return
    targets = list(chunk_utils.iter_prim_targets(stage, primpath_param))
    if not targets:
        return
    # One pivot for however many prims the path pattern matched, so it can only be centred on
    # one of them. The first, matching how the drag write-back already picks its reference.
    prim = stage.GetPrimAtPath(targets[0])
    if not prim.IsValid():
        return

    cache = UsdGeom.BBoxCache(
        Usd.TimeCode.Default(),
        [UsdGeom.Tokens.default_, UsdGeom.Tokens.proxy, UsdGeom.Tokens.render],
    )
    bound = cache.ComputeUntransformedBound(prim)
    if bound.GetRange().IsEmpty():
        return
    _set_pivot(node, bound.ComputeCentroid())


def main():
    # One handle for the whole chunk: in effect "host, run your native transform manipulator
    # and give me the result". It absorbs the three gizmo= groups below into a single
    # manipulator rather than leaving three that would fight over the same drag.
    chunk_api = chunk.Chunk("Transform", gizmo="transform")

    primpath = chunk.LineEdit(
        "primpath_location",
        "Prim Path",
        default_value="/prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
    )
    chunk_api.add_param(primpath)

    # The last column is the whole gizmo declaration. A group already *is* the three params
    # its handle drives, so nothing needs listing -- and three separate handles is what a
    # viewport wants anyway, rather than one handle with modes.
    for grp_name, grp_label, axes, min_v, max_v, default, gizmo in [
        ("translate_grp", "Translate", ("tx", "ty", "tz"), -100.0, 100.0, 0.0, "translate"),
        ("rotate_grp",    "Rotate",    ("rx", "ry", "rz"),  -180.0,  180.0, 0.0, "rotate"),
        ("scale_grp",     "Scale",     ("sx", "sy", "sz"),   0.0,  100.0, 1.0, "scale"),
    ]:
        grp = chunk.Group(
            grp_name,
            grp_label,
            gizmo=gizmo,
            flags=chunk_core.ChunkFlags.GroupFlags.COLLAPSABLE | chunk_core.ChunkFlags.GroupFlags.LABELED,
        )
        if grp_name != "translate_grp":
            grp.set_state(chunk_core.ChunkFlags.State.COLLAPSED)
        for name, label in zip(axes, ("X", "Y", "Z")):
            s = chunk.Slider(
                name, label,
                min_value=min_v, max_value=max_v, default_value=default,
                flags=chunk.ChunkFlags.ParamFlags.LABELED,
            )
            grp.add_param(s)
        chunk_api.add_param(grp)

    # No gizmo= on this group, deliberately: the pivot is not a handle of its own. It says
    # where the transform handle sits and what it turns about, which is the `offset` on that
    # one handle -- reported through the gizmo_offset hook above, since it changes as the
    # user drags rather than being static config.
    pivot_grp = chunk.Group(
        "pivot_grp", "Pivot",
        flags=chunk_core.ChunkFlags.GroupFlags.COLLAPSABLE | chunk_core.ChunkFlags.GroupFlags.LABELED,
    )
    pivot_grp.set_state(chunk_core.ChunkFlags.State.COLLAPSED)
    for name, label in zip(_PIVOT_PARAMS, ("X", "Y", "Z")):
        s = chunk.Slider(
            name, label,
            min_value=-100.0, max_value=100.0, default_value=0.0,
            flags=chunk.ChunkFlags.ParamFlags.LABELED,
            tooltip="The point rotation and scale turn about, in the prim's own space. "
                    "Moving it does not move the prim.",
        )
        # The default, spelled out because it matters here: firing at construction would
        # record a pivot before the chunk has restored its saved params, and compensate
        # translate against it.
        s.add_callback(_pivot_changed, send_on_create=False)
        pivot_grp.add_param(s)

    center_btn = chunk.Button(
        "center_pivot", "Center Pivot",
        tooltip="Put the pivot on the middle of the target's geometry, without moving it.",
    )
    center_btn.add_callback(_center_pivot)
    pivot_grp.add_param(center_btn)
    chunk_api.add_param(pivot_grp)

    return chunk_api


def config():
    return chunk.DefaultConfig(
        # No gizmos block: the handles are declared as gizmo= on the three groups in main().
        category=chunk.StandardCategory.TRANSFORM,
        description="""
        Transform chunk — applies translate, rotate (XYZ Euler, degrees), and
        scale xform ops to one or more existing prims.

        Parameters:
          - Prim Path: comma-separated prim paths or Houdini-style glob patterns
            (*, **, ?) to transform. Each matched prim must already exist
            (created by another chunk). An over is authored per prim in the
            chunk layer so the original prim definitions are untouched.
          - Translate (X/Y/Z): world-space translation in scene units (-1000–1000).
            Expanded by default.
          - Rotate (X/Y/Z): XYZ Euler rotation in degrees (-180–180). Collapsed
            by default.
          - Scale (X/Y/Z): per-axis scale (0.001–100, default 1.0). Collapsed
            by default.
          - Pivot (X/Y/Z): the point rotation and scale turn about, in the
            prim's own object space. Moving the pivot never moves the prim —
            translate absorbs the difference. All zero means no pivot, and the
            viewport goes on placing the handle on the geometry; set one and the
            handle moves there. "Center Pivot" snaps it to the middle of the
            target's bounds.

        Each execution clears the chunk layer and re-authors all three ops,
        keeping authoring idempotent. AddTranslateOp / AddRotateXYZOp /
        AddScaleOp are used so the xformOpOrder is always consistent.
        """,
    )
