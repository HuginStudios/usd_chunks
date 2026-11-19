"""The forest biome from biome.py, lit, framed and rendered to an image.

    USD_CHUNK_DIRS=chunks python examples/scripts/biome_render.py

  biome.build   ground, noise clearings and four scatter layers (see biome.py)
  Preview Surface  a soil material on the ground
  Distant Light the sun, low in the sky
  Dome Light    sky fill
  Camera        looking across the forest from above a clearing
  Render        writes biome_render.png when the network is committed (here, and on
                every `usdchunks run biome_render_network.usda`)

Change the look from the command line without touching the script, for example a new
noise seed for the clearings:

    usdchunks run examples/scripts/out/biome_render_network.usda Forest.seed=7

Needs a USD build with Hydra for the image (usdview's, Houdini's or Blender's Python; the
usd-core wheel builds the network and the scene, but can't render).
"""
from __future__ import annotations

import sys
from pathlib import Path

import biome

from usd_chunks import Scene

HERE = Path(__file__).resolve().parent
# Results go to the folder given on the command line, else examples/scripts/out.
OUT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else HERE / "out"

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    with Scene.new(path=str(OUT / "biome_render_network.usda")) as scene:
        grass = biome.build(scene)
        soil = scene.create.preview_surface(grass, mat_path="/looks/soil", primpath_location="/ground",
                                            diffuse_color=(0.23, 0.2, 0.12), roughness=0.9)

        sun = scene.create.distant_light(soil, primpath_location="/lights/sun", intensity=2.5)
        aim_sun = scene.create.transform(sun, primpath_location="/lights/sun", rx=-35.0, ry=40.0)
        sky = scene.create.dome_light(aim_sun, primpath_location="/lights/sky", intensity=0.5)
        cam = scene.create.camera(sky, primpath_location="/cam", tx=34.0, ty=14.0, tz=34.0,
                                  focalLength=28.0, clippingRange=(0.1, 1000.0),
                                  aim_at_point=True, aim_point=(0.0, 0.0, 0.0))
        render = scene.create.render(cam, camera="/cam", mode="Command", image_width=1280,
                                     output_image=str(OUT / "biome_render.png"), camera_light=False)
        scene.set_output(render)

        scene.save()
        scene.export(str(OUT / "biome_render.usda"))
        scene.commit()  # runs Render
