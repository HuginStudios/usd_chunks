r"""A product configurator: the CAD motor with paint and part variants, one render per option.

    USD_CHUNK_DIRS=chunks python examples/scripts/configurator.py

  CAD Import, Transform  the motor from examples/assets/motor.step, stood upright
  Add Variants           two variant sets on /World/Motor: paint (red, blue, yellow) and
                         base (plate, bare)
  Standard Surface       the finishes every option shares (steel, plastic, aluminium),
                         and one MaterialX paint per colour
  Bind Material x3       each paint bound inside its own variant: the Prim Path
                         /World/Motor{paint=blue}/Prototypes/Body/mesh binds only in "blue"
  Property Editor        inside base=bare, hides the base plate and its bolts
  Set Variant x2         the options picked (named Paint and Base)
  ground, lights, camera, Render

The script renders every paint with the plate, and the yellow motor without it, to
out/configurator_<option>.png. The saved network is the configurator: open it in usdview
and pick a variant in the Prim view, or render any option from the command line:

    usdchunks run examples/scripts/out/configurator_network.usda Paint.variant_selection=yellow Base.variant_selection=bare

The exported out/configurator.usda keeps the variant sets, so whoever loads it (a web
viewer, Omniverse, a game engine) can switch options without usd_chunks.

Needs `cadquery-ocp` for CAD Import, and a full USD build (usdview's, Houdini's or
Blender's Python) for MaterialX and the images; the usd-core wheel has neither.
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
PAINTS = {"red": (0.6, 0.04, 0.02), "blue": (0.03, 0.15, 0.55), "yellow": (0.85, 0.6, 0.02)}
MOTOR = "/World/Motor"
# Finishes every option shares, bound to the prototype meshes as in cad_to_render.py.
FIXED = {
    "black_plastic": ({"base_color": (0.02, 0.02, 0.02), "specular_roughness": 0.45}, ["Cap"]),
    "steel": ({"base_color": (0.85, 0.85, 0.86), "metalness": 0.5, "specular_roughness": 0.3},
              ["Shaft", "Bolt"]),
    "aluminium": ({"base_color": (0.7, 0.7, 0.72), "metalness": 0.3, "specular_roughness": 0.55},
                  ["Plate"]),
}

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    with Scene.new(path=str(OUT / "configurator_network.usda")) as scene:
        cad = scene.create.cad_import(file_path=ASSET, primpath_location=MOTOR,
                                      angular_deflection=6.0, name="CAD")
        last = scene.create.transform(cad, primpath_location=MOTOR, rx=-90.0)

        last = scene.create.variant(last, primpath_location=MOTOR, variant_entries=[
            {"vs_name": "paint", "v_names": ", ".join(PAINTS), "v_default": "red"},
            {"vs_name": "base", "v_names": "plate, bare", "v_default": "plate"},
        ])

        for material, (values, parts) in FIXED.items():
            meshes = ",".join(f"{MOTOR}/Prototypes/{p}/mesh" for p in parts)
            last = scene.create.standard_surface(last, prim_path=f"/World/Looks/{material}",
                                                 bind_to=meshes, **values)

        # One paint per colour, bound to the motor body only inside that colour's variant.
        for colour, rgb in PAINTS.items():
            last = scene.create.standard_surface(last, prim_path=f"/World/Looks/paint_{colour}",
                                                 base_color=rgb, specular_roughness=0.3, coat=0.6)
            last = scene.create.bind_material(
                last, material_path=f"/World/Looks/paint_{colour}",
                primpath_location=f"{MOTOR}{{paint={colour}}}/Prototypes/Body/mesh")

        # The bare option: no base plate, no bolts. The parts sit under the STEP file's own
        # assembly node, "Motor", inside the import root.
        bare = f"{MOTOR}{{base=bare}}/Motor"
        bolts = ",".join(f"{bare}/bolt_{i}" for i in range(4))
        last = scene.create.property_editor(last, prim=f"{bare}/plate,{bolts}",
                                            edits='visibility = "invisible"')

        paint = scene.create.set_variant(last, name="Paint", primpath_location=MOTOR,
                                         variant_set="paint", variant_selection="red")
        base = scene.create.set_variant(paint, name="Base", primpath_location=MOTOR,
                                        variant_set="base", variant_selection="plate")

        ground = scene.create.ground(base, prim_path="/World/ground", width1=3.0, height2=3.0,
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
        render = scene.create.render(cam, camera="/World/cam", mode="Command", image_width=960,
                                     camera_light=False)
        scene.set_output(render)

        options = [(colour, "plate") for colour in PAINTS] + [("yellow", "bare")]
        for colour, plate in options:
            name = colour if plate == "plate" else f"{colour}_{plate}"
            scene.set(paint, variant_selection=colour)
            scene.set(base, variant_selection=plate)
            scene.set(render, output_image=str(OUT / f"configurator_{name}.png"))
            scene.commit()  # renders this option

        scene.set(paint, variant_selection="red")
        scene.set(base, variant_selection="plate")
        scene.save()
        scene.export(str(OUT / "configurator.usda"))
