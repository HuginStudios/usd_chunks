"""Emit chunk -- turn a PointInstancer's instances into a stream released over time.

The simulation needs to know *what* comes in, *where*, *when* and *how fast*. This chunk
writes all four onto the instancer itself, as plain data any tool can read, and the RBD
Solver only reads them -- the same split as Houdini, where the source geometry says what is
emitted and the solver just solves:

* ``protoIndices`` -- which shape each instance is: picked at random from the prototypes the
  instancer uses when it reaches this chunk (a Fracture's whole rocks, a Scatter's assets);
* ``positions`` / ``orientations`` -- where it appears, inside the Region prim's bounds, at a
  random orientation;
* ``velocities`` / ``angularVelocities`` -- how it is thrown (USD's own per-instance
  attributes, units/s and degrees/s -- also what a renderer motion-blurs by);
* ``primvars:rbd:emitFrame`` -- the frame it enters the simulation;
* ``invisibleIds`` over time -- a preview: each instance flashes up for a quarter second where
  and when it enters, so the stream can be judged before anything is simulated.

Instances that would appear inside one another are spaced apart in the region; any overlap
left is the solver's to wait out.
"""

from __future__ import annotations

import math

import numpy as np
from pxr import Gf, Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk, chunk_stage, sim_bake, chunk_utils

EMIT_FRAME = "primvars:rbd:emitFrame"
#: How long each instance shows in the preview, from its emit frame.
PREVIEW_SECONDS = 0.25


def _report(node, text: str) -> None:
    if (readout := node.get_param("readout")) is not None and readout.value != text:
        readout.value = text


def _random_quats(rng, n):
    q = rng.normal(size=(n, 4))
    return q / np.linalg.norm(q, axis=1, keepdims=True)


def _bounds(prim, time):
    purposes = [UsdGeom.Tokens.default_, UsdGeom.Tokens.guide, UsdGeom.Tokens.proxy, UsdGeom.Tokens.render]
    rng = UsdGeom.BBoxCache(time, purposes).ComputeWorldBound(prim).ComputeAlignedRange()
    return None if rng.IsEmpty() else (np.array(rng.GetMin()), np.array(rng.GetMax()))


def _proto_radius(stage, target):
    rng = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_]).ComputeLocalBound(
        stage.GetPrimAtPath(target)).ComputeAlignedRange()
    return 1.0 if rng.IsEmpty() else 0.5 * float((rng.GetMax() - rng.GetMin()).GetLength())


def plan(count, rate, start_frame, fps, lo, hi, radii, choices, rng, velocity, velocity_random, spin,
         random_orient=True, clearance_time=0.5):
    """Where, when and how each instance appears. Pure numbers, for the chunk and for tests.

    Positions are drawn in the box *lo*..*hi*; one that would overlap an instance emitted
    within *clearance_time* seconds before it is redrawn (a few tries, then kept -- the
    solver waits out what is left).
    """
    proto = rng.choice(choices, size=count) if count else np.zeros(0, dtype=np.int64)
    frames = start_frame + np.floor(np.arange(count) * fps / max(rate, 1e-6)).astype(np.int64)
    pos = np.zeros((count, 3))
    for i in range(count):
        r = radii[proto[i]]
        recent = [j for j in range(i) if (frames[i] - frames[j]) / fps < clearance_time]
        for _ in range(16):
            p = lo + (hi - lo) * rng.random(3)
            if all(np.linalg.norm(pos[j] - p) > r + radii[proto[j]] for j in recent):
                break
        pos[i] = p
    orient = _random_quats(rng, count) if random_orient else np.tile([1.0, 0.0, 0.0, 0.0], (count, 1))
    vel = np.tile(np.asarray(velocity, dtype=np.float64), (count, 1))
    speed = float(np.linalg.norm(velocity))
    if velocity_random > 0.0:
        vel += rng.normal(size=(count, 3)) * velocity_random * max(speed, 1.0)
    ang = rng.normal(size=(count, 3))
    ang = ang / np.maximum(np.linalg.norm(ang, axis=1, keepdims=True), 1e-9) * spin * rng.random((count, 1))
    return dict(proto=proto, frames=frames, pos=pos, orient=orient, vel=vel, ang=ang)


def execute(node) -> None:
    stage = node.get_stage()
    layer = node.get_edit_context()
    if stage is None or layer is None:
        return
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)
    is_f, is_i, is_s = chunk.is_param_t(float), chunk.is_param_t(int), chunk.is_param_t(str)
    target_path = node.get_param("target", is_s).value.strip()
    region_path = node.get_param("region", is_s).value.strip()
    if not target_path or not region_path:
        _report(node, "set a Target instancer and a Region")
        return
    scene = node.get_scene()
    upstream = chunk_stage.get_upstream_stage(stage, scene, node) if scene is not None else stage
    target = upstream.GetPrimAtPath(target_path)
    inst = UsdGeom.PointInstancer(target) if target else None
    if not inst:
        _report(node, f"{target_path} is not a PointInstancer")
        return
    region = upstream.GetPrimAtPath(region_path)
    start_frame = node.get_param("start_frame", is_i).value
    box = _bounds(region, start_frame) if region else None
    if box is None:
        _report(node, f"{region_path} has no geometry to emit from")
        return
    protos = inst.GetPrototypesRel().GetTargets()
    used = sorted({int(i) for i in (inst.GetProtoIndicesAttr().Get() or [])}) or list(range(len(protos)))
    if not used:
        _report(node, f"{target_path} has no prototypes")
        return
    # Region and velocities are world space; the instancer's own space is what it stores.
    to_world = UsdGeom.Xformable(target).ComputeLocalToWorldTransform(start_frame)
    to_local = np.array(to_world.GetInverse(), dtype=np.float64)
    rot_local = to_world.GetInverse().RemoveScaleShear().ExtractRotationQuat()

    fps = stage.GetTimeCodesPerSecond() or 24.0
    radii = {i: _proto_radius(upstream, protos[i]) for i in used}
    count = max(0, node.get_param("count", is_i).value)
    p = plan(
        count, max(1e-3, node.get_param("rate", is_f).value), start_frame, fps, box[0], box[1], radii,
        np.asarray(used), np.random.default_rng(node.get_param("seed", is_i).value),
        node.get_param("velocity").value or (0.0, 0.0, 0.0),
        max(0.0, node.get_param("velocity_random", is_f).value),
        math.radians(max(0.0, node.get_param("spin", is_f).value)),
        node.get_param("orient", is_s).value == "Random",
    )
    pos_local = p["pos"] @ to_local[:3, :3] + to_local[3, :3]
    quats = []
    for w, x, y, z in p["orient"]:
        q = (rot_local * Gf.Quatd(w, x, y, z)).GetNormalized()
        quats.append(Gf.Quath(q))
    vel_local = p["vel"] @ to_local[:3, :3]
    ang_local = np.degrees(p["ang"]) @ to_local[:3, :3]

    ids = np.arange(count, dtype=np.int64)
    with Usd.EditContext(stage, chunk_utils.edit_target(stage, layer)):
        out = UsdGeom.PointInstancer(stage.OverridePrim(target_path))
        out.CreateProtoIndicesAttr().Set(Vt.IntArray.FromNumpy(p["proto"].astype(np.int32)))
        out.CreatePositionsAttr().Set(Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(pos_local, dtype=np.float32)))
        out.CreateOrientationsAttr().Set(Vt.QuathArray(quats))
        out.CreateVelocitiesAttr().Set(Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(vel_local, dtype=np.float32)))
        out.CreateAngularVelocitiesAttr().Set(
            Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(ang_local, dtype=np.float32)))
        out.CreateIdsAttr().Set(Vt.Int64Array.FromNumpy(ids))
        pv = UsdGeom.PrimvarsAPI(out).CreatePrimvar("rbd:emitFrame", Sdf.ValueTypeNames.IntArray,
                                                     UsdGeom.Tokens.vertex)
        pv.Set(Vt.IntArray.FromNumpy(p["frames"].astype(np.int32)))
        # The stream as a preview: each instance flashes up for a few frames where and when
        # it enters, then goes. Left showing, instances pile up at their spawn points -- a
        # clump that reads as a simulation stuck at the emitter rather than as "not
        # simulated yet".
        flash = max(1, int(round(fps * PREVIEW_SECONDS)))
        inv = out.CreateInvisibleIdsAttr()
        inv.Set(Vt.Int64Array.FromNumpy(ids))
        frames = p["frames"]
        changes = sorted(set(int(v) for v in frames) | set(int(v) + flash for v in frames))
        sim_bake.write_samples(inv, (
            (f, Vt.Int64Array.FromNumpy(ids[~((frames <= f) & (f < frames + flash))])) for f in changes
        ))
        if (sc := out.GetScalesAttr()) and sc.HasAuthoredValue():
            sc.Block()
    last = int(p["frames"].max()) if count else start_frame
    _report(node, f"{count} instances · frames {start_frame}–{last} · {len(used)} shape"
                  f"{'s' if len(used) != 1 else ''} to pick from")


def main() -> chunk.Chunk:
    chunk_api = chunk.Chunk("Emit")
    labeled = chunk.ChunkFlags.ParamFlags.LABELED
    chunk_api.add_param(chunk.LineEdit(
        "target", "Target", flags=labeled | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="The PointInstancer to emit -- e.g. a Fracture's output. Its current instances "
        "decide which shapes can be emitted; they are replaced by the stream.",
    ))
    chunk_api.add_param(chunk.LineEdit(
        "region", "Region", flags=labeled,
        tooltip="Any prim; instances appear inside its bounding box. A guide box works well.",
    ))
    chunk_api.add_param(chunk.Spinbox("count", "Count", default_value=30, is_int=True,
                                      tooltip="How many instances in total."))
    chunk_api.add_param(chunk.Spinbox("rate", "Rate", default_value=4.0, tooltip="Instances per second."))
    chunk_api.add_param(chunk.Spinbox("start_frame", "Start Frame", default_value=1, is_int=True,
                                      tooltip="Frame the first instance appears."))
    chunk_api.add_param(chunk.Vector("velocity", "Velocity", param=chunk.Spinbox(), length=3, suffixes="XYZ",
                                     default_value=(0.0, -100.0, 0.0),
                                     tooltip="Starting velocity, world space, units per second."))
    chunk_api.add_param(chunk.Slider("velocity_random", "Velocity Random", min_value=0.0, max_value=1.0,
                                     default_value=0.1,
                                     tooltip="Random spread on the velocity, as a fraction of its speed."))
    chunk_api.add_param(chunk.Spinbox("spin", "Spin", default_value=60.0,
                                      tooltip="Up to this much random spin, degrees per second."))
    chunk_api.add_param(chunk.Menu("orient", "Orientation", menu_items=["Random", "Keep"], default_value="Random",
                                   tooltip="Random: every instance at a random orientation. Keep: as the "
                                   "prototype is modelled."))
    chunk_api.add_param(chunk.Spinbox("seed", "Seed", default_value=0, is_int=True))
    chunk_api.add_param(chunk.Label("readout", "nothing emitted yet", default_value="nothing emitted yet",
                                    tooltip="What was written."))
    return chunk_api


def config() -> chunk.DefaultConfig:
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.DYNAMICS,
        icon="sparkles",
        description=(
            "Rewrites a PointInstancer's instances as a stream over time: where each appears, when\n"
            "(primvars:rbd:emitFrame), and its velocities -- plain instancer data the RBD Solver reads."
        ),
        requirements=["numpy>=2,<3"],
    )
