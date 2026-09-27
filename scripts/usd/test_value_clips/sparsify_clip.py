"""Turn full-array animation clips into sparse ones: write only the joints that move.

    python sparsify_clip.py <character.usd> <clip.usd> [<clip.usd> ...] [--out DIR]
                            [--base rest_clip.usd] [--hold 0 1000] [--separate-override]

A DCC exports a pose or a gesture as a full SkelAnimation: every joint, every frame, most
of them sitting exactly on the rest pose. Stacked over a walk cycle by the Value Clips
chunk, that clip would replace the walk wholesale. This rewrites each clip as array edits
-- `edit [write q to [i] ...]` -- for only the joints that leave the rest pose anywhere in
the clip, so stacked over a cycle it changes those joints and nothing else. That is the
OpenUSD 26.03 sparse array-edit override; see make_wave_clip.py for one written by hand.

A joint that moves at any frame is written at every frame. Writing it only where it
differs would put an identity edit next to a write, and USD interpolates between those
badly (README, "two things worth knowing").

What counts as "rest" matters. A control rig's evaluated pose with no action assigned is
not its `restTransforms`: IK targets and root controls sit wherever the rigger left them,
so against the rest pose a hand pose looked like a whole-body one. Pass `--base` with a
clip exported the same way but with no action, and the diff isolates exactly what the
pose's own keys changed. Without it the skeleton's rest pose is the base.

A pose exported as a single frame comes out of Blender as a default value with no time
samples. Clips never consult defaults, so a pose is written as samples at the two `--hold`
times; a clip set holds its edge value outside its range, so that covers any timeline.

Also written: a full-array clip of the base pose itself (`<base>.clip.usda`, or
`<character>.rest.usda` without `--base`), with real time samples regardless of how the
DCC export shaped the original. Stack a sparse clip over *that* to preview it alone --
over nothing at all, a sparse clip composes to an empty array (a `write` cannot grow a
zero-length base), and a `--base` file is routinely a default value with no time samples
of its own for clips to read, so neither an unloaded stack nor the raw `--base` file works
as that stand-in.

Also authored, directly into the character file: an animation prim with the skeleton's
joint order (Blender's rest export declares only blend shapes there), bound to the
skeleton. Reference the character alone and it is already a valid clip target -- no
sidecar file, no sublayer to remember. Pass `--separate-override` to write that as a
standalone `<character>.anim.usda` instead, for a character file regenerated often enough
(a re-export from the DCC) that baking this in means reauthoring it by hand every time.
"""
from __future__ import annotations

import argparse
import math
import os
import sys

from pxr import Gf, Sdf, Usd, UsdSkel, Vt

#: The three per-joint arrays a SkelAnimation carries, with how far apart two values may
#: be and still count as the same: rotations in degrees, translations in stage units,
#: scales as a factor. Tolerances only have to swallow float noise -- against an
#: evaluated base everything the pose did not touch comes out bit-identical.
_CHANNELS = (
    ("rotations", Vt.QuatfArrayEditBuilder,
     lambda a, b: math.degrees(2 * math.acos(min(1.0, abs(Gf.Dot(Gf.Quatd(a), Gf.Quatd(b)))))), 0.05),
    ("translations", Vt.Vec3fArrayEditBuilder, lambda a, b: (Gf.Vec3d(a) - Gf.Vec3d(b)).GetLength(), 1e-4),
    ("scales", Vt.Vec3hArrayEditBuilder, lambda a, b: (Gf.Vec3d(a) - Gf.Vec3d(b)).GetLength(), 1e-3),
)


def _animations(stage):
    return [UsdSkel.Animation(p) for p in stage.Traverse() if p.IsA(UsdSkel.Animation)]


def _skeleton_for(character, joints):
    """The character's skeleton whose joint order the clip's arrays index into."""
    for prim in character.Traverse():
        if prim.IsA(UsdSkel.Skeleton) and list(UsdSkel.Skeleton(prim).GetJointsAttr().Get() or []) == joints:
            return UsdSkel.Skeleton(prim)
    return None


def _rest(skeleton):
    """Rest pose as the three channel arrays, in the skeleton's joint order."""
    xforms = skeleton.GetRestTransformsAttr().Get()
    translations, rotations, scales = UsdSkel.DecomposeTransforms(Vt.Matrix4dArray(xforms))
    return {"rotations": rotations, "translations": translations, "scales": scales}


def _base_pose(base_path, joints):
    """The evaluated pose a base clip holds, as channel arrays, or None without one."""
    if not base_path:
        return None
    stage = Usd.Stage.Open(base_path)
    anims = _animations(stage)
    if not anims or list(anims[0].GetJointsAttr().Get() or []) != joints:
        print(f"base {base_path}: no SkelAnimation with the same joint order", file=sys.stderr)
        return None
    pose = {}
    for name, _, _, _ in _CHANNELS:
        attr = anims[0].GetPrim().GetAttribute(name)
        times = attr.GetTimeSamples()
        pose[name] = attr.Get(times[0]) if times else attr.Get()
    return pose


def _samples(attr, hold):
    """(time, value) pairs for an attribute: its samples, or its default held at `hold`."""
    times = attr.GetTimeSamples()
    if times:
        return [(t, attr.Get(t)) for t in times]
    value = attr.Get()
    if value is None:
        return []
    return [(t, value) for t in hold]


def write_rest_clip(skeleton, pose, out_dir, out_name, hold):
    """A full-array clip holding one pose, safe to use as a Value Clips row on its own.

    A sparse clip is a delta -- `edit [write q to [i]]` composes over whatever full array
    is beneath it, and produces an empty array with nothing beneath it at all (a `write`
    cannot grow a zero-length base; verified directly against Vt.QuatfArrayEditBuilder).
    So previewing one sparse pose with no cycle loaded needs a full-array clip under it,
    and the obvious candidate -- a `--base` file exported by the DCC for diffing against --
    is usually the wrong shape for that job: a short, unvarying export often gets written
    by the DCC as one default value with no time samples at all, and clips never consult a
    clip file's default (symmetric to a stage never falling through to a clip for its own
    default) -- so stacking a sparse clip over it *still* composes to empty. This writes
    the same pose back out with real time samples at `hold`, which no DCC export shape can
    get wrong, so any pose or rest state doubles as a working base for one-clip previews.
    """
    joints = skeleton.GetJointsAttr().Get()
    out_path = os.path.join(out_dir, out_name)
    layer = Sdf.Layer.CreateNew(out_path)
    stage = Usd.Stage.Open(layer)
    anim = UsdSkel.Animation.Define(stage, Sdf.Path("/Anim"))
    anim.CreateJointsAttr().Set(joints)
    for name, attr in (("rotations", anim.CreateRotationsAttr()),
                       ("translations", anim.CreateTranslationsAttr()),
                       ("scales", anim.CreateScalesAttr())):
        if pose.get(name) is None:
            continue
        for t in hold:
            attr.Set(pose[name], t)
    stage.SetStartTimeCode(hold[0])
    stage.SetEndTimeCode(hold[1])
    stage.SetDefaultPrim(anim.GetPrim())
    layer.Save()
    print(f"{os.path.basename(out_path)}: a full-array clip of the base pose, "
          f"{len(joints)} joints, safe under a sparse clip with nothing else loaded")
    return out_path


def sparsify(character, clip_path, out_dir, hold, base=None):
    clip = Usd.Stage.Open(clip_path)
    anims = _animations(clip)
    if not anims:
        print(f"skip {clip_path}: no SkelAnimation", file=sys.stderr)
        return None
    anim = anims[0]
    joints = list(anim.GetJointsAttr().Get() or [])
    skeleton = _skeleton_for(character, joints)
    if skeleton is None:
        print(f"skip {clip_path}: no skeleton in the character has its {len(joints)}-joint order", file=sys.stderr)
        return None
    rest = base or _rest(skeleton)

    out_path = os.path.join(out_dir, os.path.splitext(os.path.basename(clip_path))[0] + ".sparse.usda")
    layer = Sdf.Layer.CreateNew(out_path)
    stage = Usd.Stage.Open(layer)
    out = UsdSkel.Animation.Define(stage, anim.GetPath())
    out.CreateJointsAttr().Set(Vt.TokenArray(joints))

    touched_total = set()
    times_total = set()
    for name, builder_type, distance, eps in _CHANNELS:
        samples = _samples(anim.GetPrim().GetAttribute(name), hold)
        if not samples or rest.get(name) is None:
            continue
        against = rest[name]
        touched = sorted({i for _, values in samples for i, v in enumerate(values) if distance(v, against[i]) > eps})
        if not touched:
            continue
        attr = out.GetPrim().GetAttribute(name)
        for t, values in samples:
            builder = builder_type()
            for i in touched:
                builder.Write(values[i], i)
            attr.Set(builder.FinalizeAndReset(), t)
            times_total.add(t)
        touched_total.update(touched)

    if not touched_total:
        print(f"skip {clip_path}: nothing leaves the rest pose", file=sys.stderr)
        layer.Clear()
        return None
    stage.SetStartTimeCode(min(times_total))
    stage.SetEndTimeCode(max(times_total))
    stage.SetDefaultPrim(stage.GetPrimAtPath(anim.GetPath().GetPrefixes()[0]))
    layer.Save()
    print(f"{os.path.basename(out_path)}: {len(touched_total)} of {len(joints)} joints, "
          f"{len(times_total)} sample(s), {os.path.getsize(out_path) // 1024} KB")
    return out_path


def _anim_target(character):
    """The skeleton to bind and the animation prim path clips should land on, or None."""
    skeletons = [UsdSkel.Skeleton(p) for p in character.Traverse() if p.IsA(UsdSkel.Skeleton)]
    if not skeletons:
        return None, None
    skeleton = max(skeletons, key=lambda s: len(s.GetJointsAttr().Get() or []))
    anims = [a for a in _animations(character) if a.GetPath().HasPrefix(skeleton.GetPath())]
    anim_path = anims[0].GetPath() if anims else skeleton.GetPath().AppendChild("Anim")
    return skeleton, anim_path


def bake_character_override(character, character_path):
    """Author the animation prim + binding directly into the character's own root layer.

    The default: reference `character_path` alone and it is already a valid clip target.
    Opens the file itself as the edit target rather than authoring through the already-open
    `character` stage's current edit target, which is whatever composed arc happens to be
    strongest and not necessarily this file.
    """
    skeleton, anim_path = _anim_target(character)
    if skeleton is None:
        return None
    joints = skeleton.GetJointsAttr().Get()
    layer = Sdf.Layer.FindOrOpen(character_path)
    stage = Usd.Stage.Open(layer)
    with Usd.EditContext(stage, layer):
        anim = UsdSkel.Animation.Define(stage, anim_path)
        anim.CreateJointsAttr().Set(joints)
        binding = UsdSkel.BindingAPI.Apply(stage.GetPrimAtPath(skeleton.GetPath()))
        binding.CreateAnimationSourceRel().SetTargets([anim_path])
    layer.Save()
    print(f"{os.path.basename(character_path)}: {anim_path} gets {len(joints)} joints "
          f"and is bound on {skeleton.GetPath()} -- reference this file alone, nothing else needed")
    return character_path


def write_character_override(character, out_dir):
    """A standalone override file, for a character regenerated often enough (a DCC
    re-export) that baking the binding into it directly means reauthoring by hand each
    time. Reference the character, then sublayer this over it.
    """
    skeleton, anim_path = _anim_target(character)
    if skeleton is None:
        return None

    stem = os.path.splitext(os.path.basename(character.GetRootLayer().identifier))[0]
    out_path = os.path.join(out_dir, stem + ".anim.usda")
    layer = Sdf.Layer.CreateNew(out_path)
    stage = Usd.Stage.Open(layer)
    anim = UsdSkel.Animation.Define(stage, anim_path)
    anim.CreateJointsAttr().Set(skeleton.GetJointsAttr().Get())
    binding = UsdSkel.BindingAPI.Apply(stage.OverridePrim(skeleton.GetPath()))
    binding.CreateAnimationSourceRel().SetTargets([anim_path])
    layer.Save()
    print(f"{os.path.basename(out_path)}: {anim_path} gets {len(skeleton.GetJointsAttr().Get())} joints "
          f"and is bound on {skeleton.GetPath()} -- sublayer this under the character")
    return out_path


# Not `main`: this file sits under scripts/, which is USD_CHUNK_DIRS, and the registry
# registers any module there with a top-level main() as a chunk. Created from the menu,
# main() would run argparse against the HOST's sys.argv -- usdview's own arguments --
# raising SystemExit past the registry's `except Exception`, or, given two file
# arguments, actually running the sparsify. generate_clips.py avoids it the same way.
def cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("character")
    parser.add_argument("clips", nargs="+")
    parser.add_argument("--out", default=None, help="output directory (default: beside the clips)")
    parser.add_argument("--base", default=None,
                        help="a clip of the rig with no animation applied; poses are diffed against it "
                             "instead of the skeleton's rest pose")
    parser.add_argument("--hold", type=float, nargs=2, default=(0.0, 1000.0), metavar=("START", "END"))
    parser.add_argument("--separate-override", action="store_true",
                        help="write <character>.anim.usda instead of baking the animation "
                             "prim into the character file directly")
    args = parser.parse_args(argv)

    character = Usd.Stage.Open(args.character)
    out_dir = args.out or os.path.dirname(os.path.abspath(args.clips[0]))
    os.makedirs(out_dir, exist_ok=True)
    if args.separate_override:
        write_character_override(character, out_dir)
    else:
        bake_character_override(character, args.character)

    skeleton, _ = _anim_target(character)
    base = _base_pose(args.base, list(skeleton.GetJointsAttr().Get())) if args.base and skeleton else None
    if skeleton is not None:
        pose = base or _rest(skeleton)
        rest_name = (os.path.splitext(os.path.basename(args.base))[0] + ".clip.usda" if args.base
                    else os.path.splitext(os.path.basename(args.character))[0] + ".rest.usda")
        write_rest_clip(skeleton, pose, out_dir, rest_name, args.hold)

    for clip in args.clips:
        sparsify(character, clip, out_dir, args.hold, base)


if __name__ == "__main__":
    cli()
