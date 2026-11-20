"""Procedural buildings: a village street, eight houses from one chunk type, each its own.

    USD_CHUNK_DIRS=chunks python examples/scripts/street.py

  Stage Settings   Z up, metres
  Mesh (grid) x2   a lawn and a street down the middle, each with a Poly Haven texture
                   (fetched the first time; see example_assets.texture_look)
  House x8         four houses on each side, every one a node with its own width, roof
                   style, floors and colours. A House is built from its front-left corner
                   with its door facing -Y, so a Transform turns the far side round
  Fence x2         a picket fence along each row of front gardens
  lights, camera
  Render           writes street.png: Aurora when it is installed, else Storm

Change a house from the command line, without touching the script:

    usdchunks run examples/scripts/out/street_network.usda House3.roof_style=Hip House3.floors=2
"""
from __future__ import annotations

import sys
from pathlib import Path

import example_assets as assets

from usd_chunks import Scene

HERE = Path(__file__).resolve().parent
# Results go to the folder given on the command line, else examples/scripts/out.
OUT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else HERE / "out"

RED, YELLOW, BLUE, WHITE, GREEN = (0.52, 0.12, 0.08), (0.85, 0.72, 0.4), (0.3, 0.4, 0.5), (0.86, 0.85, 0.8), \
    (0.32, 0.42, 0.3)
# One row per side of the street, in order along it.
NORTH = [
    {"width": 7.0, "roof_style": "Gable", "floors": 1, "wall_color": RED},
    {"width": 5.5, "roof_style": "Gambrel", "floors": 2, "wall_color": YELLOW, "shutters": True},
    {"width": 8.0, "roof_style": "Hip", "floors": 1, "wall_color": BLUE},
    {"width": 6.0, "roof_style": "Saltbox", "floors": 2, "wall_color": WHITE},
]
SOUTH = [
    {"width": 6.0, "roof_style": "Hip", "floors": 2, "wall_color": WHITE, "shutters": True},
    {"width": 7.5, "roof_style": "Gable", "floors": 1, "wall_color": GREEN},
    {"width": 5.0, "roof_style": "Mono", "floors": 1, "wall_color": RED},
    {"width": 7.0, "roof_style": "Gable", "floors": 2, "wall_color": YELLOW},
]
STREET = 3.5  # half the street's width, metres
SETBACK = 6.0  # from the middle of the street to a house's front wall
GAP = 4.0  # between neighbouring houses
START = -22.0  # where each row begins along the street

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    with Scene.new(path=str(OUT / "street_network.usda")) as scene:
        last = scene.create.stage_settings(up_axis="Z", meters_per_unit=1.0, default_prim="World")
        # A 120 m lawn and a 7 m street, each built half size and scaled 2x: their textures
        # repeat once per unit of the grid's own st, so this way every 2 m (Aurora ignores
        # UV Transform).
        last = scene.create.mesh(last, prim_path="/World/lawn", mesh_type="grid", dimx1=2, dimy1=2,
                                 width1=60.0, height2=60.0)
        last = scene.create.transform(last, primpath_location="/World/lawn", sx=2.0, sy=2.0)
        last = assets.texture_look(scene, last, "/World/looks/lawn", "grass_path_2", "/World/lawn")
        last = scene.create.mesh(last, prim_path="/World/street", mesh_type="grid", dimx1=2, dimy1=2,
                                 width1=60.0, height2=STREET)
        last = scene.create.transform(last, primpath_location="/World/street", sx=2.0, sy=2.0, tz=0.02)
        last = assets.texture_look(scene, last, "/World/looks/street", "concrete_floor_02", "/World/street")

        # One chunk type, eight houses: each is its own node with its own values.
        number = 0
        for side, row in (("north", NORTH), ("south", SOUTH)):
            x = START
            for house in row:
                number += 1
                path = f"/World/houses/house{number}"
                last = scene.create.house(last, name=f"House{number}", house_path=path, **house)
                if side == "north":
                    last = scene.create.transform(last, primpath_location=path, tx=x, ty=SETBACK)
                else:
                    # Turned round to face the street: its front-left corner is now on the right.
                    last = scene.create.transform(last, primpath_location=path, tx=x + house["width"], ty=-SETBACK,
                                                  rz=180.0)
                x += house["width"] + GAP
        # A picket fence along each row of front gardens, a metre and a half off the street.
        length = 2.0 * -START
        for side, y in (("North", STREET + 1.5), ("South", -STREET - 1.5)):
            path = f"/World/fences/{side.lower()}"
            last = scene.create.fence(last, name=f"Fence{side}", prim_path=path, length=length, height=0.9)
            last = scene.create.transform(last, primpath_location=path, tx=START, ty=y)

        # Afternoon sun across the street. Normalize makes Intensity the light that lands.
        sun = scene.create.distant_light(last, primpath_location="/World/sun", intensity=3.0, angle=1.0,
                                         normalize=True)
        aim_sun = scene.create.transform(sun, primpath_location="/World/sun", rx=50.0, rz=-60.0)
        sky = scene.create.dome_light(aim_sun, primpath_location="/World/sky", intensity=0.8,
                                      file=assets.hdri().as_posix())
        # A lat-long image has its poles on Y; the stage is Z up, so turn the dome to match.
        pole = scene.create.transform(sky, primpath_location="/World/sky", rx=90.0)
        cam = scene.create.camera(pole, primpath_location="/World/cam", tx=-34.0, ty=-9.0, tz=6.0,
                                  focalLength=30.0, clippingRange=(0.1, 1000.0),
                                  aim_at_point=True, aim_point=(0.0, 0.0, 2.5))
        render = scene.create.render(cam, camera="/World/cam", mode="Command", image_width=1280,
                                     camera_light=False, output_image=str(OUT / "street.png"),
                                     **assets.render_params(OUT))
        scene.set_output(render)

        scene.save()
        scene.export(str(OUT / "street.usda"))
        scene.commit()  # runs Render
