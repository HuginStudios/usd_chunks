"""Boom: a stone block dropped onto the ground, breaking into its pieces as it lands.

    USD_CHUNK_DIRS=chunks python examples/scripts/boom.py

  Mesh (Cube)      the block, lifted 5 m up and tipped onto a corner
  Preview Surface  stone outside, a paler colour on the fresh cuts
  Fracture         pre-breaks the block into a tree of pieces that tile it exactly
  RBD Solver       drops it (MuJoCo); a piece breaks into its children when the impact
                   exceeds its strength, and the whole fall is baked as animation
  lights, camera
  Render           writes boom.0060.png, the last frame

Everything is plain USD in between: open out/boom.usda in usdview and scrub the timeline.
Try another break pattern, or a weaker stone, from the command line:

    usdchunks run examples/scripts/out/boom_network.usda Fracture.seed=3 RBD.strength=4

Needs `manifold3d` (Fracture) and `mujoco` (RBD Solver); both chunks ask to install them.
The image needs a USD build with Hydra (usdview's, Houdini's or Blender's Python).
"""
from __future__ import annotations

import sys
from pathlib import Path

from usd_chunks import Scene

HERE = Path(__file__).resolve().parent
# Results go to the folder given on the command line, else examples/scripts/out.
OUT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else HERE / "out"

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    with Scene.new(path=str(OUT / "boom_network.usda")) as scene:
        settings = scene.create.stage_settings(meters_per_unit=1.0, up_axis="Y", start_frame=1,
                                               end_frame=60)
        ground = scene.create.ground(settings, prim_path="/World/ground", width1=12.0, height2=12.0,
                                     grid_checker=False)
        floor = scene.create.preview_surface(ground, mat_path="/World/looks/floor",
                                             primpath_location="/World/ground",
                                             diffuse_color=(0.3, 0.3, 0.32), roughness=0.9)

        # The source block: a 1 m cube (its bottom on its origin), up in the air, on a corner.
        block = scene.create.cube(floor, prim_path="/World/block", width=1.0)
        lift = scene.create.transform(block, primpath_location="/World/block", ty=5.0, rx=35.0,
                                      rz=25.0)
        stone = scene.create.preview_surface(lift, mat_path="/World/looks/stone",
                                             primpath_location="/World/block",
                                             diffuse_color=(0.45, 0.4, 0.35), roughness=0.8)
        cut = scene.create.preview_surface(stone, mat_path="/World/looks/cut",
                                           primpath_location="", diffuse_color=(0.75, 0.7, 0.62),
                                           roughness=0.9)

        fracture = scene.create.fracture(cut, name="Fracture", source_prims="/World/block",
                                         output_path="/World/pieces", generations=6,
                                         outside_material="/World/looks/stone",
                                         inside_material="/World/looks/cut", seed=1)
        rbd = scene.create.rbd_solver(fracture, name="RBD", prim_path="/World/pieces",
                                      collision_path="/World/ground", frame_count=60,
                                      strength=1.0, break_kick=4.0)

        sun = scene.create.distant_light(rbd, primpath_location="/World/sun", intensity=2.0)
        aim_sun = scene.create.transform(sun, primpath_location="/World/sun", rx=-50.0, ry=30.0)
        sky = scene.create.dome_light(aim_sun, primpath_location="/World/sky", intensity=0.6)
        cam = scene.create.camera(sky, primpath_location="/World/cam", tx=6.0, ty=3.0, tz=7.0,
                                  focalLength=35.0, clippingRange=(0.05, 500.0),
                                  aim_at_point=True, aim_point=(0.5, 0.3, 0.0))
        render = scene.create.render(cam, camera="/World/cam", mode="Command", image_width=1280,
                                     frames="60", camera_light=False,
                                     output_image=str(OUT / "boom.####.png"))
        scene.set_output(render)

        scene.save()
        scene.export(str(OUT / "boom.usda"))
        scene.commit()  # runs Render
