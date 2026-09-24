"""Physics Drive chunk -- make any prim a collider, a spinning part or a conveyor.

Everything it does is plain UsdPhysics / PhysX schema on the target, authored in this chunk's
own layer, so the result reads the same in any tool that understands UsdPhysics and nothing
is special to this repo:

* **Collider** -- ``CollisionAPI`` on every mesh under the target.
* **Spin** -- the target becomes a kinematic rigid body on a ``RevoluteJoint`` to the world
  (written beside it as ``<name>_drive``), with an angular ``DriveAPI`` whose
  ``targetVelocity`` is the speed. The RBD Solver turns such a body at exactly that speed,
  with unlimited torque, and bakes the turn onto the prim.
* **Conveyor** -- PhysX's ``physxSurfaceVelocity:surfaceVelocity``: the surface stays where
  it is and carries what rests on it at that velocity, the way a belt does.

A spin is about one of the target's own axes, through its bounds centre or its origin.
"""

from __future__ import annotations

from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics

from usd_chunks import chunk

MODE_COLLIDER = "Collider"
MODE_SPIN = "Spin"
MODE_CONVEYOR = "Conveyor"
_AXES = {"X": Gf.Vec3d(1, 0, 0), "Y": Gf.Vec3d(0, 1, 0), "Z": Gf.Vec3d(0, 0, 1)}

#: Which params each mode uses; the others are hidden.
_MODE_PARAMS = {
    MODE_COLLIDER: (),
    MODE_SPIN: ("axis", "rpm", "pivot"),
    MODE_CONVEYOR: ("belt_direction", "belt_speed"),
}


def _sync_visibility(node) -> None:
    mode = node.get_param("mode").value
    for m, names in _MODE_PARAMS.items():
        for name in names:
            param = node.get_param(name)
            if param is None:
                continue
            if m == mode:
                param.unset_state(chunk.ChunkFlags.State.HIDDEN)
            else:
                param.set_state(chunk.ChunkFlags.State.HIDDEN)


def _mode_changed(owner, notice, sender) -> None:
    _sync_visibility(owner.get_chunk())


def _report(node, text: str) -> None:
    if (readout := node.get_param("readout")) is not None and readout.value != text:
        readout.value = text


def apply_drive(stage, prim, mode, *, axis="X", rpm=30.0, pivot="Bounds Centre",
                direction=(0.0, 0.0, 1.0), speed=60.0) -> str:
    """Author the drive on *prim* into the current edit target. Returns a one-line summary."""
    meshes = [p for p in Usd.PrimRange(prim) if p.IsA(UsdGeom.Mesh)]
    for m in meshes:
        UsdPhysics.CollisionAPI.Apply(m)
    if mode == MODE_SPIN:
        UsdPhysics.RigidBodyAPI.Apply(prim).CreateKinematicEnabledAttr(True)
        if pivot == "Bounds Centre":
            rng = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_]).ComputeUntransformedBound(
                prim).ComputeAlignedRange()
            centre = Gf.Vec3d(0, 0, 0) if rng.IsEmpty() else (rng.GetMin() + rng.GetMax()) * 0.5
        else:
            centre = Gf.Vec3d(0, 0, 0)
        world = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        world_rot = world.RemoveScaleShear().ExtractRotationQuat()
        joint = UsdPhysics.RevoluteJoint.Define(stage, prim.GetPath().GetParentPath().AppendChild(
            f"{prim.GetName()}_drive"))
        joint.CreateBody1Rel().SetTargets([prim.GetPath()])
        joint.CreateAxisAttr(axis)
        joint.CreateLocalPos1Attr(Gf.Vec3f(centre))
        joint.CreateLocalRot1Attr(Gf.Quatf(1.0))
        # The world side of the joint is the same point and orientation, in world space.
        joint.CreateLocalPos0Attr(Gf.Vec3f(world.Transform(centre)))
        joint.CreateLocalRot0Attr(Gf.Quatf(world_rot))
        drive = UsdPhysics.DriveAPI.Apply(joint.GetPrim(), "angular")
        drive.CreateTypeAttr("force")
        drive.CreateStiffnessAttr(0.0)
        drive.CreateDampingAttr(1e10)
        drive.CreateTargetVelocityAttr(float(rpm) * 6.0)  # rpm -> degrees per second
        return f"{prim.GetName()}: spins {rpm:g} rpm about {axis} ({joint.GetPath().name})"
    if mode == MODE_CONVEYOR:
        d = Gf.Vec3d(*direction)
        d = d.GetNormalized() if d.GetLength() > 1e-9 else Gf.Vec3d(0, 0, 1)
        prim.AddAppliedSchema("PhysxSurfaceVelocityAPI")
        prim.CreateAttribute("physxSurfaceVelocity:surfaceVelocity", Sdf.ValueTypeNames.Vector3f).Set(
            Gf.Vec3f(d * float(speed)))
        prim.CreateAttribute("physxSurfaceVelocity:surfaceVelocityEnabled", Sdf.ValueTypeNames.Bool).Set(True)
        return f"{prim.GetName()}: conveyor, {speed:g} units/s"
    return f"{prim.GetName()}: collider ({len(meshes)} mesh{'es' if len(meshes) != 1 else ''})"


def execute(node) -> None:
    stage = node.get_stage()
    layer = node.get_edit_context()
    if stage is None or layer is None:
        return
    _sync_visibility(node)
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)
    prims = node.get_param("prim_path").get_prims()
    if not prims:
        _report(node, "no target -- pick the part to drive")
        return
    is_f, is_s = chunk.is_param_t(float), chunk.is_param_t(str)
    mode = node.get_param("mode", is_s).value
    lines = []
    with Usd.EditContext(stage, layer):
        for prim in prims:
            lines.append(apply_drive(
                stage, stage.GetPrimAtPath(prim.GetPath()), mode,
                axis=node.get_param("axis", is_s).value,
                rpm=node.get_param("rpm", is_f).value,
                pivot=node.get_param("pivot", is_s).value,
                direction=tuple(node.get_param("belt_direction").value or (0.0, 0.0, 1.0)),
                speed=node.get_param("belt_speed", is_f).value,
            ))
    _report(node, "\n".join(lines))


def main() -> chunk.Chunk:
    chunk_api = chunk.Chunk("Physics Drive")
    chunk_api.add_param(chunk.LineEdit(
        "prim_path", "Target", flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="The part to drive -- a mesh, or a prim above meshes. Several, comma separated.",
    ))
    mode = chunk.Menu("mode", "Mode", menu_items=[MODE_COLLIDER, MODE_SPIN, MODE_CONVEYOR], default_value=MODE_SPIN,
                      tooltip="Collider: just collides. Spin: turns at a fixed speed about its own axis. "
                      "Conveyor: stays put and carries what rests on it.")
    mode.add_callback(_mode_changed, send_on_create=False)
    chunk_api.add_param(mode)
    chunk_api.add_param(chunk.Menu("axis", "Axis", menu_items=["X", "Y", "Z"], default_value="Y",
                                   tooltip="The part's own axis to spin about."))
    chunk_api.add_param(chunk.Spinbox("rpm", "RPM", default_value=30.0,
                                      tooltip="Turns per minute. Negative turns the other way."))
    chunk_api.add_param(chunk.Menu("pivot", "Pivot", menu_items=["Bounds Centre", "Origin"],
                                   default_value="Bounds Centre",
                                   tooltip="Spin through the middle of the part, or through its own origin."))
    chunk_api.add_param(chunk.Vector("belt_direction", "Direction", param=chunk.Spinbox(), length=3, suffixes="XYZ",
                                     default_value=(0.0, 0.0, 1.0),
                                     tooltip="Which way the surface moves, in the part's own space."))
    chunk_api.add_param(chunk.Spinbox("belt_speed", "Speed", default_value=60.0,
                                      tooltip="Surface speed, scene units per second."))
    chunk_api.add_param(chunk.Label("readout", "no target yet", default_value="no target yet",
                                    tooltip="What was authored."))
    for name in ("belt_direction", "belt_speed"):
        chunk_api.get_param(name).set_state(chunk.ChunkFlags.State.HIDDEN)
    return chunk_api


def config() -> chunk.DefaulConfig:
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.DYNAMICS,
        description=(
            "Makes a prim a collider, a spinning part (kinematic body on a revolute joint with a\n"
            "velocity drive) or a conveyor (PhysX surface velocity) -- plain UsdPhysics, read by\n"
            "the RBD Solver."
        ),
    )
