"""CAD to render: a STEP assembly imported, shaded with MaterialX, lit and rendered.

    USD_CHUNK_DIRS=chunks python examples/scripts/cad_to_render.py

  CAD Import        the STEP file as USD: assembly tree, one mesh shared by the four bolts,
                    face colours kept
  MaterialX Shader  one Standard Surface per finish, bound to the parts that wear it
  Transform         the CAD file is Z-up; turn it to stand on the Y-up ground
  Ground, lights    a floor to sit on, a dome and a key light
  Camera            aimed at the assembly
  Render            writes cad_render.png when the network is committed (here, and on
                    every `usdchunks run cad_network.usda`)

examples/assets/motor.step is a small motor on a base plate, with four bolts that are one
part placed four times (examples/assets/make_motor_step.py wrote it). Point CAD Import's
file_path at your own .step or .iges instead, and bind the materials to its parts. Results
go to examples/scripts/out/.

Needs `cadquery-ocp` (CAD Import asks to install it) and a full USD build: usdview's,
Houdini's or Blender's Python. The usd-core wheel has neither MaterialX's shader
definitions (no Standard Surface) nor a renderer.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from usd_chunks import Scene

HERE = Path(__file__).resolve().parent
# Results go to the folder given on the command line, else examples/scripts/out.
OUT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else HERE / "out"
# Relative to the network file, as USD paths are, so the network keeps working if moved
# together with the assets.
ASSET = Path(os.path.relpath(HERE.parent / "assets" / "motor.step", OUT)).as_posix()


# One Standard Surface per finish, and the prototype meshes that wear it. Metalness is kept
# low because Storm lights metal only from the dome's texture, and this dome has none: give
# the Dome Light an HDR image, or render with a path tracer, for fully metallic parts. CAD Import puts
# each part's mesh at <root>/Prototypes/<Part>/mesh; every placement references it, so
# binding the prototype shades every bolt at once.
FINISHES = {
    "painted_red": ({"base_color": (0.6, 0.04, 0.02), "specular_roughness": 0.3, "coat": 0.6}, ["Body"]),
    "black_plastic": ({"base_color": (0.02, 0.02, 0.02), "specular_roughness": 0.45}, ["Cap"]),
    "brushed_steel": ({"base_color": (0.85, 0.85, 0.86), "metalness": 0.5, "specular_roughness": 0.3},
                      ["Shaft", "Bolt"]),
    "cast_aluminium": ({"base_color": (0.7, 0.7, 0.72), "metalness": 0.3, "specular_roughness": 0.55},
                       ["Plate"]),
}

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)

    with Scene.new(path=str(OUT / "cad_network.usda")) as scene:
        cad = scene.create.cad_import(file_path=ASSET, primpath_location="/World/Motor",
                                      angular_deflection=6.0, name="CAD")
        # STEP is Z-up; the stage is Y-up. Turn the assembly to stand on the ground.
        upright = scene.create.transform(cad, primpath_location="/World/Motor", rx=-90.0)

        last = upright
        for material, (values, parts) in FINISHES.items():
            meshes = ",".join(f"/World/Motor/Prototypes/{p}/mesh" for p in parts)
            last = scene.create.standard_surface(last, prim_path=f"/World/Looks/{material}",
                                                 bind_to=meshes, **values)

        ground = scene.create.ground(last, prim_path="/World/ground", width1=3.0, height2=3.0,
                                     grid_checker=False)
        floor = scene.create.preview_surface(ground, mat_path="/World/Looks/floor",
                                             primpath_location="/World/ground",
                                             diffuse_color=(0.35, 0.36, 0.38), roughness=0.8)
        dome = scene.create.dome_light(floor, primpath_location="/World/sky", intensity=1.5)
        key = scene.create.rect_light(dome, primpath_location="/World/key", intensity=8.0,
                                      width=0.8, height=0.8)
        aim_key = scene.create.transform(key, primpath_location="/World/key", tx=0.6, ty=0.9,
                                         tz=0.7, aim_at_point=True, aim_point=(0.0, 0.05, 0.0))
        cam = scene.create.camera(aim_key, primpath_location="/World/cam", tx=0.42, ty=0.3,
                                  tz=0.55, focalLength=50.0, clippingRange=(0.01, 100.0),
                                  aim_at_point=True, aim_point=(0.0, 0.05, 0.0))
        render = scene.create.render(cam, camera="/World/cam", mode="Command", image_width=1280,
                                     camera_light=False, output_image=str(OUT / "cad_render.png"))
        scene.set_output(render)

        scene.save()                              # the network: opens as a graph
        scene.export(str(OUT / "cad_scene.usda"))  # the result: plain USD
        scene.commit()                            # runs Render -> out/cad_render.png
