"""Write a *sparse* wave clip for Pixar's HumanFemale, to layer over her walk cycle.

    python make_wave_clip.py <path/to/UsdSkelExamples/HumanFemale> [out.usda]

The walk clip (`HumanFemale.walk.usd`) authors the full 109-element `rotations` array on
every frame. This clip authors none of that. Every sample it writes is an array *edit* --
`edit [write <quat> to [i] ...]` -- touching only the right arm, elbow and hand, so when
the Value Clips chunk stacks it above the walk the other 106 joints keep walking and only
the arm changes. That is the OpenUSD 26.03+ sparse array-edit override, and this is what
it is for: a layered performance that says what it changes and nothing else.

The pose is built from the skeleton's bind pose: the character binds in a T-pose with the
right arm along world -X, so raising it is a rotation about world +Y, and the hand waves
about the same axis. Deltas are conjugated into each joint's parent frame so the arm stays
attached to the torso the walk is moving.
"""
from __future__ import annotations

import math
import os
import sys

from pxr import Gf, Sdf, Usd, UsdSkel, Vt

#: Joints the wave owns. Everything else stays with whatever is underneath.
ARM = "Hips/Torso/Chest/UpChest/RShldr/RArm"
ELBOW = ARM + "/RElbow"
HAND = ELBOW + "/RHand"

#: The clip's own frame range: two walk cycles (the walk is 101-129).
START, END = 101, 158
RAISE = 8       # frames to lift the arm, and to lower it again
WAVES = 4       # full side-to-side swings while the arm is up


def _ease(t: float) -> float:
    return 0.5 - 0.5 * math.cos(math.pi * min(max(t, 0.0), 1.0))


def _envelope(frame: int) -> float:
    """0 before the arm comes up, 1 while it is up, 0 once it is back down."""
    up = _ease((frame - START) / RAISE)
    down = _ease((END - frame) / RAISE)
    return min(up, down)


def _rot_y(degrees: float) -> Gf.Matrix4d:
    return Gf.Matrix4d().SetRotate(Gf.Rotation(Gf.Vec3d(0, 1, 0), degrees))


def _local_with_world_delta(bind: dict, joint: str, delta: Gf.Matrix4d) -> Gf.Quatf:
    """The joint's local rotation after `delta` is applied to it in world space.

    Row-vector maths, as Gf: world = local * parent. Adding a world rotation D after the
    joint's own world rotation R gives R*D, and taking the parent back out leaves
    L * P * D * P^-1 -- the delta conjugated into the parent's frame.
    """
    parent = joint.rsplit("/", 1)[0]
    r_joint = bind[joint].ExtractRotationMatrix()
    r_parent = bind[parent].ExtractRotationMatrix()
    local = Gf.Matrix4d(r_joint * r_parent.GetInverse(), Gf.Vec3d(0))
    conj = Gf.Matrix4d(r_parent, Gf.Vec3d(0)) * delta * Gf.Matrix4d(r_parent.GetInverse(), Gf.Vec3d(0))
    quat = (local * conj).ExtractRotationQuat()
    return Gf.Quatf(quat.GetReal(), Gf.Vec3f(quat.GetImaginary()))


def write_wave(character_dir: str, out_path: str) -> str:
    walk = Usd.Stage.Open(os.path.join(character_dir, "HumanFemale.walk.usd"))
    anim = UsdSkel.Animation(walk.GetPrimAtPath("/HumanFemale_Group/SkelAnim"))
    skel = UsdSkel.Skeleton(walk.GetPrimAtPath("/HumanFemale_Group/Rig/Skel"))

    # The anim's joint order is the index space of its rotations array, and the array the
    # edits index into. The skeleton's order is only used to look bind transforms up by name.
    order = {name: i for i, name in enumerate(anim.GetJointsAttr().Get())}
    bind = dict(zip(skel.GetJointsAttr().Get(), skel.GetBindTransformsAttr().Get()))

    walk_rotations = anim.GetRotationsAttr()
    walk_start, walk_end = walk.GetStartTimeCode(), walk.GetEndTimeCode()

    def walking(frame: int, joint: str) -> Gf.Quatf:
        """What the walk has this joint doing at `frame`, with the walk looping under us.

        The Value Clips chunk loops a clip by jumping from its last sample back to its
        first, so the cycle is one frame shorter than the clip's range.
        """
        cycle = walk_start + (frame - walk_start) % (walk_end - walk_start)
        return walk_rotations.Get(cycle)[order[joint]]

    layer = Sdf.Layer.CreateNew(out_path)
    stage = Usd.Stage.Open(layer)
    out = UsdSkel.Animation.Define(stage, "/HumanFemale_Group/SkelAnim")
    rotations = out.GetRotationsAttr()
    for frame in range(START, END + 1):
        amount = _envelope(frame)
        phase = (frame - START - RAISE) / (END - START - 2 * RAISE)
        swing = math.sin(phase * WAVES * 2 * math.pi) * amount
        pose = {
            ARM: _local_with_world_delta(bind, ARM, _rot_y(60 * amount)),
            ELBOW: _local_with_world_delta(bind, ELBOW, _rot_y(40 * amount)),
            HAND: _local_with_world_delta(bind, HAND, _rot_y(30 * swing)),
        }
        # Blend out of the walk's own arm pose rather than snapping to the rest pose, so
        # the arm leaves the swing and comes back to it without a pop at either end.
        builder = Vt.QuatfArrayEditBuilder()
        for joint, target in pose.items():
            builder.Write(Gf.Slerp(amount, walking(frame, joint), target), order[joint])
        rotations.Set(builder.FinalizeAndReset(), frame)
    stage.SetDefaultPrim(out.GetPrim().GetParent())
    stage.SetStartTimeCode(START)
    stage.SetEndTimeCode(END)
    layer.Save()
    return out_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    character = sys.argv[1]
    target = sys.argv[2] if len(sys.argv) > 2 else os.path.join(character, "HumanFemale.wave.usda")
    print(write_wave(character, target))
