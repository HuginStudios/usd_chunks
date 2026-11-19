"""Procedural buildings and a fluid sim: a row of cottages, and a wave of water at their feet.

    USD_CHUNK_DIRS=chunks python examples/scripts/houses_and_water.py

  House x3         three cottages from one chunk, each with its own roof, size and floors
  Mesh (Cube)      the block of water the sim starts from, hidden once the sim has it
  Particle Solver  MLS-MPM water (Taichi), thrown onto the ground in front of the houses
                   and baked as points over 48 frames
  lights, camera
  Render           writes houses_and_water.0040.png

Change a house from the command line, for example a hip roof and two floors on the first:

    usdchunks run examples/scripts/out/houses_network.usda Cottage1.roof_style=Hip Cottage1.floors=2

Needs `taichi` (Particle Solver asks to install it; it runs on the GPU when there is one,
else the CPU), and a USD build with Hydra for the image.
"""
from __future__ import annotations

import sys
from pathlib import Path

from usd_chunks import Scene

HERE = Path(__file__).resolve().parent
# Results go to the folder given on the command line, else examples/scripts/out.
OUT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else HERE / "out"

COTTAGES = [
    {"width": 6.5, "roof_style": "Gable", "floors": 1, "wall_color": (0.52, 0.12, 0.08)},
    {"width": 5.0, "roof_style": "Gambrel", "floors": 2, "wall_color": (0.85, 0.75, 0.45),
     "shutters": True},
    {"width": 7.5, "roof_style": "Hip", "floors": 1, "wall_color": (0.3, 0.38, 0.45)},
]

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    with Scene.new(path=str(OUT / "houses_network.usda")) as scene:
        last = scene.create.stage_settings(meters_per_unit=1.0, up_axis="Y", start_frame=1,
                                           end_frame=48)
        last = scene.create.ground(last, prim_path="/World/ground", width1=40.0, height2=30.0,
                                   grid_checker=False)
        last = scene.create.preview_surface(last, mat_path="/World/looks/grass",
                                            primpath_location="/World/ground",
                                            diffuse_color=(0.2, 0.32, 0.12), roughness=0.9)

        # One chunk type, three houses: each is its own node with its own values.
        x = -9.0
        for i, cottage in enumerate(COTTAGES, start=1):
            last = scene.create.house(last, name=f"Cottage{i}", house_path=f"/World/houses/cottage{i}",
                                      **cottage)
            last = scene.create.transform(last, primpath_location=f"/World/houses/cottage{i}",
                                          tx=x, tz=-4.0)
            x += cottage["width"] + 2.5

        # The water starts as a block in front of the houses, thrown towards the right. The
        # sim's box is the block grown by Margin, reaching down to the ground it lands on.
        water = scene.create.cube(last, prim_path="/World/fx/water_source", width=3.0, height=2.0,
                                  depth=2.0)
        water = scene.create.transform(water, primpath_location="/World/fx/water_source",
                                       tx=-4.0, ty=1.5, tz=3.0)
        water = scene.create.property_editor(water, prim="/World/fx/water_source",
                                             edits='visibility = "invisible"')
        blue = scene.create.preview_surface(water, mat_path="/World/looks/water",
                                            primpath_location="", diffuse_color=(0.1, 0.35, 0.7),
                                            roughness=0.15)
        sim = scene.create.particle_solver(blue, name="Water", source_prim="/World/fx/water_source",
                                           material="Water", collision_path="/World/ground",
                                           margin=3.0, initial_velocity=(4.0, 0.0, 0.0),
                                           grid_res=96, frame_count=48, sim_path="/World/fx/sim",
                                           material_prim="/World/looks/water", point_scale=1.5)

        sun = scene.create.distant_light(sim, primpath_location="/World/sun", intensity=1.5)
        aim_sun = scene.create.transform(sun, primpath_location="/World/sun", rx=-40.0, ry=-35.0)
        sky = scene.create.dome_light(aim_sun, primpath_location="/World/sky", intensity=0.5)
        cam = scene.create.camera(sky, primpath_location="/World/cam", tx=8.0, ty=9.0, tz=34.0,
                                  focalLength=28.0, clippingRange=(0.1, 1000.0),
                                  aim_at_point=True, aim_point=(2.0, 2.5, 0.0))
        render = scene.create.render(cam, camera="/World/cam", mode="Command", image_width=1280,
                                     frames="40", camera_light=False,
                                     output_image=str(OUT / "houses_and_water.####.png"))
        scene.set_output(render)

        scene.save()
        scene.export(str(OUT / "houses_and_water.usda"))
        scene.commit()  # runs Render
