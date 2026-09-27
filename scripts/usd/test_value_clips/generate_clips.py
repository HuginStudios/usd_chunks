"""Generate per-frame clip USD files for the Value Clips test.

Each frame moves the top 4 vertices of the cube upward, creating a
stretching animation over 10 frames. The bottom stays at y=-0.5,
the top goes from y=0.5 (frame 1) to y=3.0 (frame 10).

IMPORTANT: Value clips require the attribute to be authored as a TIME SAMPLE
in each clip file, not a default value. Each clip file has the points
authored at its own frame number.
"""
from __future__ import annotations

import os

CLIP_DIR = os.path.join(os.path.dirname(__file__), "clips")
NUM_FRAMES = 10

# Base cube points — bottom 4, then top 4
BOTTOM_Y = -0.5
START_TOP_Y = 0.5
END_TOP_Y = 3.0

TEMPLATE = '''#usda 1.0
(
    defaultPrim = "CubeGeo"
)

def Mesh "CubeGeo"
{{
    point3f[] points.timeSamples = {{
        {frame}: [
            (-0.5, {bottom}, -0.5),
            ( 0.5, {bottom}, -0.5),
            ( 0.5, {bottom},  0.5),
            (-0.5, {bottom},  0.5),
            (-0.5, {top}, -0.5),
            ( 0.5, {top}, -0.5),
            ( 0.5, {top},  0.5),
            (-0.5, {top},  0.5)
        ]
    }}
}}
'''


def generate():
    os.makedirs(CLIP_DIR, exist_ok=True)

    for frame in range(1, NUM_FRAMES + 1):
        # Interpolate top Y from START to END over the frame range
        t = (frame - 1) / (NUM_FRAMES - 1)
        top_y = START_TOP_Y + t * (END_TOP_Y - START_TOP_Y)

        content = TEMPLATE.format(
            frame=frame,
            bottom=f"{BOTTOM_Y:.4f}",
            top=f"{top_y:.4f}",
        )

        filename = f"clip.{frame:03d}.usda"
        filepath = os.path.join(CLIP_DIR, filename)
        with open(filepath, "w") as f:
            f.write(content)

        print(f"  Written: {filename}  (frame={frame}, top_y = {top_y:.4f})")

    # Also generate a manifest file listing which attributes are clipped
    manifest = '''#usda 1.0
(
    defaultPrim = "CubeGeo"
)

def Mesh "CubeGeo"
{
    point3f[] points
}
'''
    manifest_path = os.path.join(CLIP_DIR, "manifest.usda")
    with open(manifest_path, "w") as f:
        f.write(manifest)
    print("  Written: manifest.usda")

    print(f"\nDone! {NUM_FRAMES} clip files generated in: {CLIP_DIR}")


if __name__ == "__main__":
    generate()
