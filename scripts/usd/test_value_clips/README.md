# Value Clips demos

Two scenes for the Value Clips chunk. The first is a per-frame template cache on a cube;
the second stacks a sparse wave clip over Pixar's HumanFemale walk cycle, which is what
the chunk's clip-set ordering is for.

## 1. Template cache: the stretching cube

A static cube mesh gets its `points` attribute overridden by per-frame clip files, creating a stretching animation.

```
test_value_clips/
├── scene.usda              ← static cube mesh (8 points, unit cube)
├── clips/
│   ├── clip.001.usda       ← frame 1 (cube at rest, top y=0.5)
│   ├── clip.002.usda       ← frame 2 (top y=0.78)
│   ├── ...
│   ├── clip.010.usda       ← frame 10 (top y=3.0, fully stretched)
│   └── manifest.usda       ← lists which attributes are clipped (points)
├── generate_clips.py       ← script that generated the clip files
└── make_wave_clip.py       ← writes the sparse wave clip for demo 2
```

The animation: the bottom 4 vertices stay at `y = -0.5`, the top 4 vertices move from `y = 0.5` to `y = 3.0` over 10 frames. The cube stretches into a tall box.

1. **Open `scene.usda` in usdview**
   ```
   usdview scripts/usd/test_value_clips/scene.usda
   ```

2. **Add the Value Clips chunk** from the chunk panel (category: USD)

3. **Fill in the parameters:**
   - **Target Prim**: `/CubeGeo`
   - one Clip Sets row:
     - **Clip**: `./clips/clip.###.usda`
     - **Clip Prim Path**: `/CubeGeo` (same path — the clip files have the same prim name)
     - **Start / End**: leave End at `0` and the range is read off the files on disk (1–10), or type `1` / `10`
     - **Manifest**: (optional) `./clips/manifest.usda`

4. **Scrub the timeline** from frame 1 to 10 — you should see the cube stretch upward.

The chunk authors this on `/CubeGeo`, in its own layer:

```
clips = {
    dictionary clip = {
        string templateAssetPath = "<abs>/clips/clip.###.usda"   # resolved against the root layer
        double templateStartTime = 1
        double templateEndTime = 10
        double templateStride = 1
        string primPath = "/CubeGeo"
        double2[] active = [(1, 0), (10, 0)]
    }
}
clipSets = ["clip"]
```

At each frame, USD resolves the template (e.g. frame 5 → `./clips/clip.005.usda`), opens that layer, and uses the `points` value from `/CubeGeo` in that clip file instead of the static value in `scene.usda`.

To tweak the animation, edit `generate_clips.py` and run:
```
python scripts/usd/test_value_clips/generate_clips.py
```

## 2. Sparse override: a wave over a walk cycle

OpenUSD 26.03 added sparse array-edit overrides: a stronger opinion on an array attribute
can be `edit [write <value> to [i]]` instead of a whole new array. Value clips take part
in that, so a clip that only says what the right arm does composes over a clip that says
what all 109 joints do — and the character keeps walking underneath the wave.

The character is Pixar's free UsdSkel example, which ships with a walk cycle:
<https://openusd.org/files/UsdSkelExamples.zip> (see `dl_usdskel_examples.html` for the
licence). It is not checked in.

1. **Write the wave clip** next to the walk:
   ```
   python scripts/usd/test_value_clips/make_wave_clip.py <path>/UsdSkelExamples/HumanFemale
   ```
   That reads the walk clip's joint order and the skeleton's bind pose and writes
   `HumanFemale.wave.usda`. Open it: every sample is an `edit [...]` touching three
   elements (`RArm`, `RElbow`, `RHand`) of the `rotations` array, and nothing else. Frames
   101–158: the arm comes up over 8 frames, waves four times, comes back down.

2. **Open usdview** on any stage (an empty one is fine) and build:
   - **Reference** chunk: file `HumanFemale.walk.usd`, insert at `/HumanFemale`. That
     brings in the skeleton, the skinned geometry and the walk's own samples (which clips
     will beat, since they come across a reference).
   - **Value Clips** chunk: Target Prim `/HumanFemale/SkelAnim`, and two rows —
     strongest first:

     | Name | Clip | Clip Prim Path | Start | End | Loop |
     |------|------|----------------|-------|-----|------|
     | wave | `HumanFemale.wave.usda` | `/HumanFemale_Group/SkelAnim` | 101 | 0 | off |
     | walk | `HumanFemale.walk.usd`  | `/HumanFemale_Group/SkelAnim` | 101 | 158 | on |

     The walk clip is 101–129, so `Loop` repeats it out to 158 with jump discontinuities
     in its `times`; the wave plays once (End `0` = its own length).

3. **Play**. She walks the whole way; from 101 the right arm rises, waves, and drops back
   into the arm swing by 158. Swap the two rows and the wave disappears — the row order
   is the `clipSets` list-op, and the walk's full arrays then hide the wave's edits.

What the chunk authors:

```
over "HumanFemale" {
    over "SkelAnim" (
        clips = {
            dictionary walk = {
                asset[] assetPaths = [@.../HumanFemale.walk.usd@]
                string primPath = "/HumanFemale_Group/SkelAnim"
                double2[] active = [(101, 0)]
                double2[] times = [(101, 101), (129, 129), (129, 101), (157, 129), (157, 101), (185, 129)]
            }
            dictionary wave = {
                asset[] assetPaths = [@.../HumanFemale.wave.usda@]
                string primPath = "/HumanFemale_Group/SkelAnim"
                double2[] active = [(101, 0)]
                double2[] times = [(101, 101), (158, 158)]
            }
        }
        clipSets = ["wave", "walk"]
    ) {}
}
```

Two things worth knowing about clips when you write a sparse one yourself:

- A clip set never falls through to the one below it outside its own range; it holds its
  edge value. So an override that should only apply for part of the timeline has to
  cover the whole timeline and be a no-op where it has nothing to say — `make_wave_clip`
  blends into and out of the walk's own arm pose rather than relying on that.
- An identity edit (`edit []`) does release the element on the exact frame it is
  authored, but USD interpolates between an identity edit and a `write` as if the
  identity were a value, so mixing them in one clip gives odd in-between frames. Write
  every frame.
