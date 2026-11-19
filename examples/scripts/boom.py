"""Break something: a stone block dropped onto a yard, shattering as it lands.

    USD_CHUNK_DIRS=chunks python examples/scripts/boom.py

  Stage Settings   Z up, metres, frames 1-60
  Mesh (grid)      a concrete yard, textured (Poly Haven; fetched the first time, see
                   example_assets.py)
  Inline USD       props for scale: road barriers and barrels, placed with Transforms
  Mesh (Cube)      the source block, 1 m, 5 m up and tipped onto a corner, in rock
  Fracture         breaks it in advance into a tree of pieces that fit together exactly:
                   rock on the outside, a paler stone on the cuts
  RBD Solver       drops it with MuJoCo; an impact stronger than a piece's Strength breaks
                   it into its children. The fall is baked onto a PointInstancer
  lights, camera
  Render           writes boom.0036.png, just after the impact: Aurora when it is
                   installed, else Storm

Open out/boom.usda in usdview and scrub the timeline. Try another break from the command
line, without touching the script:

    usdchunks run examples/scripts/out/boom_network.usda Fracture.seed=3 RBD.strength=4

Needs manifold3d (Fracture) and mujoco (RBD Solver); each asks to install the first time.
"""
from __future__ import annotations

import sys
from pathlib import Path

import example_assets as assets

from usd_chunks import Scene

HERE = Path(__file__).resolve().parent
# Results go to the folder given on the command line, else examples/scripts/out.
OUT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else HERE / "out"

PROPS = {"barrier_1": ("concrete_road_barrier", None), "barrier_2": ("concrete_road_barrier", None),
         "barrel_1": ("barrel_03", None), "barrel_2": ("barrel_03", None)}
# Where each prop stands: (x, y, turn about Z).
PLACES = {"barrier_1": (-1.2, 4.2, 8.0), "barrier_2": (0.5, 4.4, -4.0),
          "barrel_1": (2.4, 3.1, 0.0), "barrel_2": (3.1, 2.6, 40.0)}

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    with Scene.new(path=str(OUT / "boom_network.usda")) as scene:
        last = scene.create.stage_settings(up_axis="Z", meters_per_unit=1.0, start_frame=1, end_frame=60)
        # A 24 m yard, built 12 units wide and scaled 2x: the texture repeats every 2 m.
        last = scene.create.mesh(last, prim_path="/World/ground", mesh_type="grid", dimx1=2, dimy1=2,
                                 width1=12.0, height2=12.0)
        last = scene.create.transform(last, primpath_location="/World/ground", sx=2.0, sy=2.0)
        last = scene.create.inline_usd(last, name="Concrete",
                                       code=assets.ground_material("/World/looks/concrete", "concrete_floor_02"))
        last = scene.create.bind_material(last, material_path="/World/looks/concrete",
                                          primpath_location="/World/ground")

        last = scene.create.inline_usd(last, name="Props", code=assets.prototypes("/World/props", PROPS, hidden=False))
        for name, (x, y, turn) in PLACES.items():
            last = scene.create.transform(last, primpath_location=f"/World/props/{name}", tx=x, ty=y, rz=turn)

        # The source block: a 1 m cube (its bottom on its origin), up in the air, on a corner.
        last = scene.create.cube(last, prim_path="/World/block", width=1.0)
        last = scene.create.transform(last, primpath_location="/World/block", tz=5.0, rx=35.0, ry=25.0)
        last = scene.create.inline_usd(last, name="Rock", code=assets.ground_material("/World/looks/rock", "rock_boulder_dry"))
        last = scene.create.bind_material(last, material_path="/World/looks/rock", primpath_location="/World/block")
        last = scene.create.preview_surface(last, mat_path="/World/looks/cut", primpath_location="",
                                            diffuse_color=(0.5, 0.47, 0.42), roughness=0.9)

        last = scene.create.fracture(last, name="Fracture", source_prims="/World/block", output_path="/World/pieces",
                                     generations=6, outside_material="/World/looks/rock",
                                     inside_material="/World/looks/cut", seed=1)
        last = scene.create.rbd_solver(last, name="RBD", prim_path="/World/pieces", collision_path="/World/ground",
                                       frame_count=60, strength=0.6, break_kick=4.0)

        # Normalize makes Intensity the light that lands, not the brightness of the sun's disc.
        sun = scene.create.distant_light(last, primpath_location="/World/sun", intensity=3.0, normalize=True)
        aim_sun = scene.create.transform(sun, primpath_location="/World/sun", rx=50.0, rz=-120.0)
        sky = scene.create.dome_light(aim_sun, primpath_location="/World/sky", intensity=0.8,
                                      file=assets.hdri().as_posix())
        # A lat-long image has its poles on Y; the stage is Z up, so turn the dome to match.
        pole = scene.create.transform(sky, primpath_location="/World/sky", rx=90.0)
        cam = scene.create.camera(pole, primpath_location="/World/cam", tx=5.5, ty=-6.5, tz=2.2,
                                  focalLength=30.0, clippingRange=(0.05, 500.0),
                                  aim_at_point=True, aim_point=(0.6, 1.0, 0.6))
        render = scene.create.render(cam, camera="/World/cam", mode="Command", image_width=1280, frames="36",
                                     camera_light=False, output_image=str(OUT / "boom.####.png"),
                                     **assets.render_params(OUT))
        scene.set_output(render)

        scene.save()
        scene.export(str(OUT / "boom.usda"))
        scene.commit()  # runs Render
