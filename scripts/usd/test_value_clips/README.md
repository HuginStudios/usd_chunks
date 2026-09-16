# Value Clips Test Scene

A simple test to see the Value Clips chunk in action. A static cube mesh gets its `points` attribute overridden by per-frame clip files, creating a stretching animation.

## What's here

```
test_value_clips/
├── scene.usda              ← static cube mesh (8 points, unit cube)
├── clips/
│   ├── clip.001.usda       ← frame 1 (cube at rest, top y=0.5)
│   ├── clip.002.usda       ← frame 2 (top y=0.78)
│   ├── ...
│   ├── clip.010.usda       ← frame 10 (top y=3.0, fully stretched)
│   └── manifest.usda       ← lists which attributes are clipped (points)
└── generate_clips.py       ← script that generated the clip files
```

The animation: the bottom 4 vertices stay at `y = -0.5`, the top 4 vertices move from `y = 0.5` to `y = 3.0` over 10 frames. The cube stretches into a tall box.

## How to test with the Value Clips chunk

1. **Open `scene.usda` in usdview**
   ```
   usdview scripts/core_usd/test_value_clips/scene.usda
   ```

2. **Add the Value Clips chunk** from the chunk panel (category: Core USD)

3. **Fill in the parameters:**
   - **Target Prim**: `/CubeGeo`
   - **Clip Template**: `./clips/clip.###.usda`
   - **Clip Prim Path**: `/CubeGeo` (same path — the clip files have the same prim name)
   - **Start**: `1`
   - **End**: `10`
   - **Stride**: `1`
   - **Manifest Path**: (optional) browse to `./clips/manifest.usda`

4. **Scrub the timeline** from frame 1 to 10 — you should see the cube stretch upward.

## How it works under the hood

The Value Clips chunk authors this metadata on `/CubeGeo`:

```
clips = {
    "default" = {
        templateAssetPath = "./clips/clip.###.usda"
        templateStartTime = 1
        templateEndTime = 10
        templateStride = 1
        primPath = "/CubeGeo"
        manifestAssetPath = @./clips/manifest.usda@
        active = [(1, 0), (10, 0)]
    }
}
```

At each frame, USD resolves the template (e.g. frame 5 → `./clips/clip.005.usda`), opens that layer, and uses the `points` value from `/CubeGeo` in that clip file instead of the static value in `scene.usda`.

## Re-generating clips

If you want to tweak the animation, edit `generate_clips.py` and run:
```
python scripts/usd/test_value_clips/generate_clips.py
```
