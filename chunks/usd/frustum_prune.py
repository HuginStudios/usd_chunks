"""Frustum Prune chunk -- prunes what a camera cannot see, so a renderer never loads it.

A shot of one room in a whole house, or of a clearing in a forest, carries everything else
along: a renderer builds every mesh and every instance whether the camera looks at it or
not. This chunk deactivates the prims outside the camera's view, and switches off the
instances outside it (a PointInstancer's inactiveIds), so they never reach Hydra.

What is outside is decided on bounding spheres against the camera's frustum, widened by
Padding on every side. Out of frame is not the same as unseen: a mirror reflects what is
behind the camera, a blocker outside the window shapes the light coming in, trees out of
shot cast shadows into it. Padding keeps everything within that many metres of the view --
behind the camera too -- and Keep names prims never to prune whatever their position.
Lights, cameras and render settings are never pruned.

A prim fully outside is pruned as a whole, the way Prune Prim does (deactivated, flagged
usd_chunks:pruned); one fully inside is kept as a whole; one across the edge is split into
its children. All edits go to the chunk's own layer.

Instances cost little; prototypes cost a lot. A renderer builds a prototype's meshes once
per PointInstancer, however few instances point at it -- measured in Storm, a set of nine
scanned bushes is ~2 GB a copy whether one instance uses it or ten thousand. So switching
instances off saves little on its own: what it frees is the prototypes no instance left on
uses, and those are pruned too.
"""

from __future__ import annotations

import math

import numpy as np
from pxr import Gf, Usd, UsdGeom, UsdLux, UsdRender

from usd_chunks import chunk

# The metadata convention for pruned prims, shared with Prune Prim.
_PRUNE_KEY = "usd_chunks:pruned"

_PURPOSES = [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy]


class _Frustum:
    """A camera's view as five inward planes in world space: four sides through the eye, and
    the far clip. A sphere is outside when it lies beyond one of them by more than its radius
    plus the padding."""

    def __init__(self, camera: Gf.Camera, padding: float):
        frustum = camera.frustum
        to_world = frustum.ComputeViewInverse()
        eye = np.asarray(to_world.ExtractTranslation(), dtype=np.float64)
        rot = np.asarray(to_world.ExtractRotationMatrix(), dtype=np.float64)  # rows: camera axes
        # The window at unit distance in front of the eye (camera looks down -z).
        (left, bottom), (right, top) = frustum.window.min, frustum.window.max
        local = []
        for a, b in ((left, bottom), (left, top), (right, top), (right, bottom)):
            local.append(np.array([a, b, -1.0]))
        normals = []
        for i in range(4):
            n = np.cross(local[i], local[(i + 1) % 4])
            normals.append(n / np.linalg.norm(n))
        # Each side's normal points into the view: flip any that the window's centre is behind
        # (the centre, not the axis -- a shifted window need not straddle the axis).
        middle = np.array([(left + right) * 0.5, (bottom + top) * 0.5, -1.0])
        normals = [n if n @ middle > 0 else -n for n in normals]
        world_n = [n @ rot for n in normals]
        self._normals = np.array(world_n)
        self._offsets = -self._normals @ eye
        forward = np.array([0.0, 0.0, -1.0]) @ rot
        far = frustum.nearFar.max
        if math.isfinite(far) and far > 0:
            self._normals = np.vstack([self._normals, -forward])
            self._offsets = np.append(self._offsets, forward @ eye + far)
        self._padding = padding

    def outside(self, centres: np.ndarray, radii: np.ndarray) -> np.ndarray:
        """Per sphere: wholly beyond some plane, padding included."""
        d = centres @ self._normals.T + self._offsets
        return (d < -(radii + self._padding)[:, None]).any(axis=1)

    def inside(self, centre, radius: float) -> bool:
        """Wholly within every plane, the sphere and its padding both."""
        d = np.asarray(centre, dtype=np.float64) @ self._normals.T + self._offsets
        return bool((d >= radius).all())


def _sphere(bound: Gf.BBox3d):
    r = bound.ComputeAlignedRange()
    if r.IsEmpty():
        return None
    lo, hi = np.asarray(r.GetMin(), dtype=np.float64), np.asarray(r.GetMax(), dtype=np.float64)
    return (lo + hi) * 0.5, float(np.linalg.norm(hi - lo) * 0.5)


def _never_pruned(prim: Usd.Prim) -> bool:
    return (prim.HasAPI(UsdLux.LightAPI) or prim.IsA(UsdGeom.Camera) or prim.IsA(UsdRender.SettingsBase)
            or prim.IsA(UsdRender.Product) or prim.IsA(UsdRender.Var) or prim.GetTypeName() == "Chunk")


def _cull_instances(instancer: UsdGeom.PointInstancer, view: _Frustum, cache: UsdGeom.BBoxCache,
                    time: Usd.TimeCode) -> tuple[int, int]:
    """Switch off the instances wholly outside the view. Returns (switched off, total).

    Each instance is a sphere at its position: its prototype's bounding sphere, pushed out by
    how far that sphere's centre sits from the prototype's origin, times the instance's largest
    scale -- generous, never short, and no per-instance bound to compute.

    Returns (switched off, total, the prototype indices an instance still on uses). An
    instance already off before this cook -- inactive or invisible -- uses nothing."""
    positions = instancer.GetPositionsAttr().Get(time)
    if positions is None or not len(positions):
        return 0, 0, set()
    pos = np.asarray(positions, dtype=np.float64)
    indices = np.asarray(instancer.GetProtoIndicesAttr().Get(time) or [], dtype=np.int64)
    targets = instancer.GetPrototypesRel().GetTargets()
    if len(indices) != len(pos) or not targets:
        # Not an instancer this can read: every prototype counts as used.
        return 0, len(pos), set(range(len(targets)))
    was_on = np.asarray(instancer.ComputeMaskAtTime(time) or [True] * len(pos), dtype=bool)
    reach = []
    for target in targets:
        proto = instancer.GetPrim().GetStage().GetPrimAtPath(target)
        s = _sphere(cache.ComputeUntransformedBound(proto)) if proto else None
        reach.append(float(np.linalg.norm(s[0]) + s[1]) if s else 0.0)
    radii = np.asarray(reach)[np.clip(indices, 0, len(reach) - 1)]
    if scales := instancer.GetScalesAttr().Get(time):
        radii = radii * np.abs(np.asarray(scales, dtype=np.float64)).max(axis=1)
    m = np.asarray(UsdGeom.Xformable(instancer).ComputeLocalToWorldTransform(time), dtype=np.float64)
    world = pos @ m[:3, :3] + m[3, :3]
    radii = radii * float(np.abs(np.linalg.det(m[:3, :3])) ** (1.0 / 3.0))
    outside = view.outside(world, radii)
    out = np.flatnonzero(outside)
    if len(out):
        ids = instancer.GetIdsAttr().Get(time)
        chosen = np.asarray(ids, dtype=np.int64)[out] if ids else out
        instancer.DeactivateIds(chosen.tolist())
    used = set(np.unique(indices[was_on & ~outside]).tolist())
    return len(out), len(pos), used


def _used_prototypes(instancer: UsdGeom.PointInstancer, time: Usd.TimeCode) -> tuple[list, set]:
    """(prototype targets, the indices an instance that is on uses) -- for an instancer the
    cull left alone."""
    targets = instancer.GetPrototypesRel().GetTargets()
    indices = np.asarray(instancer.GetProtoIndicesAttr().Get(time) or [], dtype=np.int64)
    mask = instancer.ComputeMaskAtTime(time)
    on = indices[np.asarray(mask, dtype=bool)] if mask and len(mask) == len(indices) else indices
    return targets, set(np.unique(on).tolist())


def _prune(prim: Usd.Prim) -> None:
    prim.SetActive(False)
    custom_data = dict(prim.GetMetadata("customData") or {})
    custom_data[_PRUNE_KEY] = True
    prim.SetMetadata("customData", custom_data)


def execute(node):
    stage = node.get_stage()
    readout = node.get_param("readout")
    camera_prims = node.get_param("camera").get_prims()
    if not stage or not camera_prims or not camera_prims[0].IsA(UsdGeom.Camera):
        if readout:
            readout.value = "no camera -- pick one to prune to its view"
        return
    time = Usd.TimeCode.Default()
    view = _Frustum(UsdGeom.Camera(camera_prims[0]).GetCamera(time), float(node.get_param("padding").value))
    keep = {p.GetPath() for p in node.get_param("keep").get_prims()}
    roots = node.get_param("prim_path").get_prims() or list(stage.GetPseudoRoot().GetChildren())
    do_instances = bool(node.get_param("prune_instances").value)
    cache = UsdGeom.BBoxCache(time, _PURPOSES, useExtentsHint=True)

    pruned, culled, instances = [], 0, 0
    # Prototype paths an instance left on still uses, and those an examined instancer has --
    # a prototype in the second but not the first is built for nothing (see below).
    needed, candidates, examined = set(), set(), set()
    stack = list(roots)
    while stack:
        prim = stack.pop()
        if not prim.IsActive() or _never_pruned(prim):
            continue
        path = prim.GetPath()
        if any(path.HasPrefix(k) or k.HasPrefix(path) for k in keep):
            # Kept itself, or holds something kept: only its other children can go.
            if path not in keep:
                stack.extend(prim.GetChildren())
            continue
        sphere = _sphere(cache.ComputeWorldBound(prim))
        if sphere is None:
            continue
        centre, radius = sphere
        if view.outside(centre[None, :], np.array([radius]))[0]:
            if not any(_never_pruned(p) for p in Usd.PrimRange(prim)):
                pruned.append(prim)
                continue
        if prim.IsA(UsdGeom.PointInstancer):
            if do_instances and not view.inside(centre, radius):
                instancer = UsdGeom.PointInstancer(prim)
                off, total, used = _cull_instances(instancer, view, cache, time)
                culled += off
                instances += total
                targets = instancer.GetPrototypesRel().GetTargets()
                candidates.update(targets)
                needed.update(targets[i] for i in used if 0 <= i < len(targets))
                examined.add(path)
            continue
        if not view.inside(centre, radius):
            stack.extend(prim.GetChildren())

    # A prototype no instance left on uses: a renderer builds a prototype's meshes once per
    # instancer however few instances point at it (Storm, measured: a bush set ~2 GB a copy,
    # one instance or ten thousand), so culling instances alone hardly saves memory -- pruning
    # the prototypes it leaves unused does. An instancer the cull left alone (wholly in view,
    # or in a group that is) still counts: under Prims its unused prototypes go too, anywhere
    # else -- or kept -- every prototype it targets stays, as one another instancer uses does.
    root_paths = [r.GetPath() for r in roots]
    for other in stage.Traverse():
        path = other.GetPath()
        if not other.IsA(UsdGeom.PointInstancer) or path in examined:
            continue
        targets, used = _used_prototypes(UsdGeom.PointInstancer(other), time)
        mine = (do_instances and any(path.HasPrefix(r) for r in root_paths)
                and not any(path.HasPrefix(k) for k in keep))
        if mine:
            candidates.update(targets)
            needed.update(targets[i] for i in used if 0 <= i < len(targets))
        else:
            needed.update(targets)
    unused = []
    for target in sorted(candidates - needed):
        proto = stage.GetPrimAtPath(target)
        if proto and proto.IsActive() and not any(target.HasPrefix(p.GetPath()) for p in pruned):
            unused.append(proto)

    for prim in pruned + unused:
        _prune(prim)

    if readout:
        text = f"{len(pruned)} prims pruned"
        if instances:
            text += f" · {culled} of {instances} instances off"
        if unused:
            text += f" · {len(unused)} prototypes unused"
        readout.value = text


def _on_change(owner, notice, sender):
    """Callback for the chunk panel: author again from scratch, the same way a cook does."""
    _chunk = owner.get_chunk()
    if not owner.get_stage() or not _chunk:
        return
    layer = owner.get_edit_context()
    if not layer:
        return
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)
    execute(_chunk)


def main():
    chunk_api = chunk.Chunk("Frustum Prune")
    _PF = chunk.ChunkFlags.ParamFlags

    camera = chunk.LineEdit(
        "camera", "Camera", flags=_PF.LABELED | _PF.PRIM_SELECTION,
        tooltip="The camera whose view decides what stays.",
    )
    prims = chunk.LineEdit(
        "prim_path", "Prims", flags=_PF.LABELED | _PF.PRIM_SELECTION,
        tooltip="Where to look for prims to prune (globs, comma separated). Empty: the whole stage.",
    )
    padding = chunk.Slider(
        "padding", "Padding", default_value=2.0, min_value=0.0, max_value=100.0,
        tooltip=(
            "Keep everything within this many metres of the view, behind the camera too: what a\n"
            "mirror reflects, what shades the window, what casts a shadow into the frame."
        ),
    )
    keep = chunk.LineEdit(
        "keep", "Keep", flags=_PF.LABELED | _PF.PRIM_SELECTION,
        tooltip="Prims never pruned, wherever they are (globs, comma separated).",
    )
    instances = chunk.Checkbox(
        "prune_instances", "Prune Instances", default_value=True,
        tooltip=(
            "Switch off a PointInstancer's instances outside the view, not just whole prims, and\n"
            "prune the prototypes no instance left on uses -- those are what cost memory."
        ),
    )
    for param in (camera, prims, padding, keep, instances):
        param.add_callback(_on_change)
        chunk_api.add_param(param)
    chunk_api.add_param(chunk.Label("readout", default_value="nothing pruned yet",
                                    tooltip="What the last cook pruned."))
    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD,
        icon="eye",
        requirements=["numpy>=2,<3"],
        description="""
        Frustum Prune — prunes what a camera cannot see, so a renderer never loads it.

        Parameters:
          - Camera: the camera whose view decides what stays.
          - Prims: where to look (globs, comma separated); empty is the whole stage.
          - Padding: keep everything within this many metres of the view, behind the
            camera too -- reflections, shadows and blockers out of frame still count.
          - Keep: prims never pruned.
          - Prune Instances: switch off a PointInstancer's instances outside the view,
            and prune the prototypes no instance left on uses (a renderer builds each
            prototype once per instancer, however few instances use it).

        A prim wholly outside is deactivated and flagged usd_chunks:pruned, as Prune
        Prim does; a PointInstancer's outside instances go into its inactiveIds. Lights,
        cameras and render settings are never pruned. All edits go to the chunk's own
        layer.
        """,
    )
