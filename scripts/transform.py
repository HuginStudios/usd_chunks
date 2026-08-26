from pxr import Gf, Sdf, Usd, UsdGeom
from usd_chunks import chunk, chunk_core, chunk_gizmo, chunk_utils


def _rescale_translate_range(node, stage):
    """Keep the translate sliders' range at a constant ~10m-equivalent span regardless of
    the stage's declared scale, instead of a flat -1000..1000 in raw stage units. That flat
    range only felt right by accident for a centimeters-scale stage (metersPerUnit=0.01,
    USD's own default when unauthored -- 10 / 0.01 = 1000, exactly the old constant); a
    meters-scale stage (metersPerUnit=1) got the same +-1000 range applied to a scene where
    objects are typically single-digit units across, so a small drag (or typing "5") could
    move something many times its own size -- reported live as translate "flying away" and
    being unusably coarse to fine-tune."""
    meters_per_unit = UsdGeom.GetStageMetersPerUnit(stage)
    if not meters_per_unit:
        return
    span = 10.0 / meters_per_unit
    for name in ("tx", "ty", "tz"):
        param = node.get_param(name)
        if param is None:
            continue
        param.min_value = -span
        param.max_value = span


def execute(node):
    stage = node.get_stage()
    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    # Before the primpath guard below -- the slider range should be correct as soon as the
    # chunk exists, not only once a target prim is set.
    _rescale_translate_range(node, stage)

    primpath_param = node.get_param("primpath_location")
    # get_param returns None while the chunk's params are being torn down (remove re-runs
    # execute via the DISABLED-unset). Bail rather than crash on None.value.
    if not primpath_param or not primpath_param.value:
        return

    rotation = (
        Gf.Rotation(Gf.Vec3d(1, 0, 0), node.get_param("rx").value) *
        Gf.Rotation(Gf.Vec3d(0, 1, 0), node.get_param("ry").value) *
        Gf.Rotation(Gf.Vec3d(0, 0, 1), node.get_param("rz").value)
    )
    xform = Gf.Transform()
    xform.SetTranslation(Gf.Vec3d(
        node.get_param("tx").value,
        node.get_param("ty").value,
        node.get_param("tz").value,
    ))
    xform.SetRotation(rotation)
    xform.SetScale(Gf.Vec3d(
        node.get_param("sx").value or 1.0,
        node.get_param("sy").value or 1.0,
        node.get_param("sz").value or 1.0,
    ))
    matrix = xform.GetMatrix()
    # local = user * pre, and Zero Prim sets pre = upstream^-1 -- so with the sliders at
    # rest the prim lands on identity, and whatever is typed reads from there rather than
    # being composed on top of the asset's own rotation.
    matrix = matrix * _pre_matrix(node)

    for target_path in chunk_utils.iter_prim_targets(stage, primpath_param):
        prim = stage.GetPrimAtPath(target_path)
        if not prim.IsValid():
            continue

        over = stage.OverridePrim(target_path)
        xformable = UsdGeom.Xformable(over)

        # This chunk's own op, not the shared xformOp:transform every chunk used to write.
        # Two chunks placing one prim were competing for one attribute, and USD settles that
        # by picking the strongest layer and discarding the rest -- which is why a Transform
        # and an Offset Transform on the same prim used to annihilate each other rather than
        # compose. Owning a named op each, they compose.
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

    This replaces the old seeding step, and is why that step is gone. Because the op used to
    REPLACE the prim's transform, a newly created chunk had to be pre-loaded with the prim's
    current placement or the object jumped to the origin the moment you grabbed it -- and
    only the auto-created path did that, so the same node behaved differently depending on
    whether it was dragged into existence or added by hand. Composing needs no seeding: an
    untouched chunk contributes identity and moves nothing.
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
    parts = chunk_gizmo.decompose(Gf.Matrix4d(value) * upstream.GetInverse())
    for names, key in ((("tx", "ty", "tz"), "translate"),
                       (("rx", "ry", "rz"), "rotate"),
                       (("sx", "sy", "sz"), "scale")):
        for name, component in zip(names, parts[key]):
            node.get_param(name).value = float(component)
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


def _pre_matrix(node) -> Gf.Matrix4d:
    """The stored pre-transform, or identity when nothing has been zeroed."""
    def _vec(name, fallback):
        param = node.get_param(name)
        return tuple(param.value) if param is not None and param.value else fallback

    xform = Gf.Transform()
    xform.SetTranslation(Gf.Vec3d(*_vec("pre_translate", (0.0, 0.0, 0.0))))
    rx, ry, rz = _vec("pre_rotate", (0.0, 0.0, 0.0))
    xform.SetRotation(
        Gf.Rotation(Gf.Vec3d(1, 0, 0), rx)
        * Gf.Rotation(Gf.Vec3d(0, 1, 0), ry)
        * Gf.Rotation(Gf.Vec3d(0, 0, 1), rz)
    )
    xform.SetScale(Gf.Vec3d(*_vec("pre_scale", (1.0, 1.0, 1.0))))
    return xform.GetMatrix()


def zero_incoming(owner, notice, sender):
    """Set this chunk's params so the target prim ends up at identity.

    A Transform composes on top of what the prim already carries, which is what lets two of
    them stack -- and it means the numbers are relative to the prim's rest pose rather than
    absolute. On an asset exported with a -90 rotation, typing 50 gives -40 and the object
    tilts the opposite way to what you asked for. That is correct and consistently
    surprising, and it is arithmetic nobody should do in their head.

    So: cancel the incoming placement once, then type from a known zero. Deliberately a
    button rather than a world-space mode -- a mode would have to keep re-deriving the
    offset on every upstream change, which is the kind of thing that quietly fights a drag.
    """
    node = owner.get_chunk()
    stage = node.get_stage()
    primpath_param = node.get_param("primpath_location")
    if not stage or not primpath_param or not primpath_param.value:
        return
    targets = list(chunk_utils.iter_prim_targets(stage, primpath_param))
    if not targets:
        return
    prim = stage.GetPrimAtPath(targets[0])
    if not prim.IsValid():
        return

    # Everything between this chunk and world, inverted: the prim's own upstream ops AND
    # every ancestor's transform.
    #
    # The ancestors are the part that matters and the part the first version missed. It
    # cancelled only the LOCAL upstream, and on the reported case there was none -- the
    # bowl's -90 sits on /carved_wooden_plate_4k, not on Circle_002, whose only op is this
    # chunk's. So the button computed identity, stored identity, and did nothing at all
    # while looking like it had worked. Same local-versus-world mistake as the solver's
    # scale bug, in a different place.
    #
    # Ordering: world = local * parent_world and local = user * pre * upstream, so with
    # pre = (upstream * parent_world)^-1 the tail collapses to identity and world == user.
    # Typed numbers then read as world, exactly, for any user value -- not only when the
    # sliders happen to be at rest.
    parent_world = Gf.Matrix4d(1.0)
    parent = prim.GetParent()
    if parent and parent.IsValid() and parent.GetPath() != Sdf.Path("/"):
        parent_world = UsdGeom.XformCache(Usd.TimeCode.Default()).GetLocalToWorldTransform(parent)
    parts = chunk_gizmo.decompose((_upstream_of(node, prim) * parent_world).GetInverse())
    node.get_param("pre_translate").value = tuple(float(v) for v in parts["translate"])
    node.get_param("pre_rotate").value = tuple(float(v) for v in parts["rotate"])
    node.get_param("pre_scale").value = tuple(float(v) for v in parts["scale"])

    # And the visible sliders go back to rest, so what gets typed next reads as written.
    for name in ("tx", "ty", "tz", "rx", "ry", "rz"):
        node.get_param(name).value = 0.0
    for name in ("sx", "sy", "sz"):
        node.get_param(name).value = 1.0


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

    zero = chunk.Button(
        "zero_incoming",
        "Zero Prim",
        tooltip="Cancel the placement this prim already has, so it sits at identity and "
        "these values read from there. A Transform composes on top of whatever the prim "
        "already carries -- its own baked ops, and any Transform before this one -- so "
        "the numbers here are relative to that, not absolute. An asset exported with a "
        "-90 rotation tilts the opposite way to what you type. This zeroes the incoming "
        "placement once, as a starting point; it is not a mode.",
    )
    zero.add_callback(zero_incoming)
    chunk_api.add_param(zero)

    # The pre-transform, hidden. Zero Prim puts the cancelling placement HERE rather than
    # into the visible sliders, which is the whole point: the sliders stay at 0/1 and read
    # as absolute from there. Putting it in the sliders instead -- the first attempt -- was
    # useless, because typing a value over them throws the cancellation away and you are
    # back where you started. Same idea as Houdini's pre-transform, for the same reason.
    for name, default in (("pre_translate", (0.0, 0.0, 0.0)),
                          ("pre_rotate", (0.0, 0.0, 0.0)),
                          ("pre_scale", (1.0, 1.0, 1.0))):
        pre = chunk.Vector(
            name, name, param=chunk.Spinbox(), length=3, suffixes="XYZ",
            default_value=default,
        )
        # HIDDEN is a State, not a ParamFlag -- it hints the UI not to draw the param.
        pre.set_state(chunk_core.ChunkFlags.State.HIDDEN)
        chunk_api.add_param(pre)

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

    return chunk_api


def config():
    return chunk.DefaulConfig(
        # No gizmos block: the handles are declared as gizmo= on the three groups in main().
        category=chunk.StandardCategory.EXAMPLES,
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

        Each execution clears the chunk layer and re-authors all three ops,
        keeping authoring idempotent. AddTranslateOp / AddRotateXYZOp /
        AddScaleOp are used so the xformOpOrder is always consistent.
        """,
    )
