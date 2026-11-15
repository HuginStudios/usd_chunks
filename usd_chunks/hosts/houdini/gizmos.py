"""A chunk's gizmos as Houdini's own handles: which handle each one is, and the maths both ways.

Houdini draws and drags the handles; this module only converts. A `box` gizmo is Houdini's
``boundingbox`` handle -- the Box SOP's, whose faces drag one at a time -- and a `translate`
gizmo a ``translate`` handle. viewer_state binds them to a
chunk node; nothing here imports hou, so it is tested without Houdini.

Spaces. A gizmo's values are in its anchor prim's space: a box's six face distances are
along the anchor's own axes, a translate is the anchor's translation in its parent's space
(the Blender and usdview hosts read it the same way). A handle is in Houdini's world, which
for a chunk inside a chunkgroup is the network's space turned by the group's space_out
(see space). Every matrix here is already in Houdini's world: the anchor's local-to-world
times ``to_host``.
"""
from __future__ import annotations

from dataclasses import dataclass

from pxr import Gf, Usd, UsdGeom

from usd_chunks.gizmo import runtime as chunk_gizmo
from usd_chunks.gizmo.types import GIZMO_BOX_ARITY, GIZMO_BOX_TYPE

#: Gizmo type -> the Houdini handle type drawn for it.
HANDLE_TYPES = {GIZMO_BOX_TYPE: "boundingbox", "translate": "translate"}
#: Handle parms read and written per handle type.
BOX_PARMS = ("centerx", "centery", "centerz", "sizex", "sizey", "sizez", "rx", "ry", "rz")
MOVE_PARMS = ("tx", "ty", "tz")


@dataclass
class Target:
    """One gizmo of a chunk node, resolved to the prim its handle hangs off."""

    name: str
    type: str
    label: str
    anchor: str
    driver: tuple
    offset: tuple | None = None


def targets(chunk, stage: Usd.Stage | None = None) -> list[Target]:
    """The chunk's gizmos Houdini has a handle for, each with its anchor prim path.

    Args:
        chunk: The chunk behind the node, its params current.
        stage: A stage for the anchor param to resolve on, or None to read its value as is.

    Returns:
        Box and translate gizmos with an anchor, in declaration order; hidden ones
        (gizmo_hidden) left out.
    """
    out = []
    for gizmo in chunk_gizmo.get_gizmos(chunk):
        if gizmo.type not in HANDLE_TYPES:
            continue
        if gizmo.type == GIZMO_BOX_TYPE and len(gizmo.driver) != GIZMO_BOX_ARITY:
            continue
        if chunk_gizmo.is_gizmo_hidden(chunk, gizmo.name):
            continue
        try:
            anchors = chunk_gizmo._anchor_targets(chunk, gizmo, stage)
        except Exception:  # an anchor that cannot resolve has nowhere to draw
            anchors = []
        if anchors:
            out.append(Target(gizmo.name, gizmo.type, gizmo.label or chunk.name, str(anchors[0]),
                              tuple(gizmo.driver), gizmo.offset))
    return out


def driver_values(chunk, target: Target) -> list[float] | None:
    """The values the target's driver params hold now, or None if one is missing."""
    values = []
    for entry in target.driver:
        try:
            param = chunk_gizmo._param_for_entry(chunk, entry)
        except ValueError:
            return None
        if param.value is None:
            return None
        values.append(float(param.value))
    return values


def anchor_matrices(stage: Usd.Stage | None, path: str, to_host: Gf.Matrix4d,
                    time=Usd.TimeCode.Default()) -> tuple[Gf.Matrix4d, Gf.Matrix4d] | None:
    """(the anchor's local-to-world, its parent's local-to-world), in Houdini's world.

    Returns:
        None when the prim is not on the stage (yet): the handle stays hidden.
    """
    prim = stage.GetPrimAtPath(path) if stage is not None and path else None
    if not prim or not prim.IsValid():
        return None
    cache = UsdGeom.XformCache(time)
    world = cache.GetLocalToWorldTransform(prim)
    parent = prim.GetParent()
    parent_world = (cache.GetLocalToWorldTransform(parent)
                    if parent and not parent.IsPseudoRoot() else Gf.Matrix4d(1.0))
    return world * to_host, parent_world * to_host


def handle_point(stage: Usd.Stage, target: Target, world: Gf.Matrix4d, to_host: Gf.Matrix4d,
                 time=Usd.TimeCode.Default()) -> Gf.Vec3d:
    """Where a translate handle sits: the gizmo's offset in the anchor's space, else the
    middle of what the anchor draws, else its origin -- as the Blender host places it."""
    if target.offset is not None:
        return world.Transform(Gf.Vec3d(*target.offset))
    prim = stage.GetPrimAtPath(target.anchor)
    cache = UsdGeom.BBoxCache(time, [UsdGeom.Tokens.default_, UsdGeom.Tokens.proxy,
                                     UsdGeom.Tokens.render, UsdGeom.Tokens.guide], useExtentsHint=True)
    bound = cache.ComputeWorldBound(prim)
    if bound.GetRange().IsEmpty():
        return world.Transform(Gf.Vec3d(0.0))
    return to_host.Transform(bound.ComputeCentroid())


def _axis_scales(world: Gf.Matrix4d) -> Gf.Vec3d:
    """How long one unit along each of the anchor's axes is in the world."""
    return Gf.Vec3d(*(Gf.Vec3d(world[i][0], world[i][1], world[i][2]).GetLength() for i in range(3)))


def box_to_handle(values, world: Gf.Matrix4d) -> dict:
    """A box gizmo's six face distances as a ``boundingbox`` handle's parms.

    Args:
        values: +X, -X, +Y, -Y, +Z, -Z, from the anchor's origin along its own axes.
        world: The anchor's local-to-world in Houdini's world.
    """
    lo, hi = chunk_gizmo.box_bounds(values)
    centre = world.Transform((lo + hi) * 0.5)
    scale = _axis_scales(world)
    size = hi - lo
    rotate = chunk_gizmo.decompose(world)["rotate"]
    return dict(zip(BOX_PARMS, (*centre, *(size[i] * scale[i] for i in range(3)), *rotate)))


def handle_to_box(parms: dict, world: Gf.Matrix4d) -> list[float]:
    """A ``boundingbox`` handle's centre and size back as the six face distances.

    The handle is turned with the anchor (box_to_handle), so its size is along the anchor's
    axes; its centre is a world point, brought back through the anchor.
    """
    centre = world.GetInverse().Transform(Gf.Vec3d(*(float(parms[n]) for n in BOX_PARMS[:3])))
    scale = _axis_scales(world)
    half = Gf.Vec3d(*(abs(float(parms[n])) / (scale[i] or 1.0) * 0.5 for i, n in enumerate(BOX_PARMS[3:6])))
    lo, hi = centre - half, centre + half
    return [hi[0], -lo[0], hi[1], -lo[1], hi[2], -lo[2]]


def moved(start_values, start_point, point, parent_world: Gf.Matrix4d) -> Gf.Vec3d:
    """A translate gizmo's value after its handle went from start_point to point.

    The world move, brought into the anchor's parent space -- where the chunk's translate
    lives -- and added to what the params held when the drag began.
    """
    delta = Gf.Vec3d(*point) - Gf.Vec3d(*start_point)
    return Gf.Vec3d(*start_values) + parent_world.GetInverse().TransformDir(delta)
