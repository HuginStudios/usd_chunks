"""Scatter a forest from real assets: young firs in noise clearings, with deadwood, undergrowth and grass.

    USD_CHUNK_DIRS=chunks python examples/scripts/forest.py

  Stage Settings   Z up, metres: what the Poly Haven assets are made in
  Mesh (grid)      an 80 m ground
  Inline USD       the prototypes, Poly Haven firs, logs, rocks, shrubs and grass (fetched
                   the first time; see example_assets.py), and a forest-floor material
  Noise Field      where the forest is thick and where the clearings are
  Scatter Points   four layers, each growing near, or away from, the ones before it:
                   trees on the noise, deadwood near the trees, undergrowth under them,
                   grass out in the clearings
  lights, camera
  Render           writes forest.png: Aurora when it is installed, else Storm

Try another forest from the command line, without touching the script:

    usdchunks run examples/scripts/out/forest_network.usda Clearings.seed=7 Trees.max_count=200

biome.py is the same idea with stand-in shapes and no downloads.
"""
from __future__ import annotations

import sys
from pathlib import Path

import example_assets as assets

from usd_chunks import Scene

HERE = Path(__file__).resolve().parent
# Results go to the folder given on the command line, else examples/scripts/out.
OUT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else HERE / "out"

# Prototype name: (Poly Haven asset, the variation to keep, or None for the whole asset).
PROTOS = {
    "fir_a": ("fir_sapling", "fir_sapling_a"),
    "fir_b": ("fir_sapling", "fir_sapling_b"),
    "log": ("dead_tree_trunk_02", None),
    "stump": ("tree_stump_01", None),
    "rock": ("boulder_01", None),
    "shrub": ("shrub_04", "shrub_04_c_LOD0"),
    "nettle": ("nettle_plant", "nettle_plant_tall_a_LOD0"),
    "grass_tall": ("grass_medium_01", "grass_medium_01_tall_a_LOD0"),
    "grass_wide": ("grass_medium_01", "grass_medium_01_large_a_LOD0"),
    "grass_mid": ("grass_medium_01", "grass_medium_01_mid_a_LOD0"),
}


def scatter(scene, upstream, name, protos, count, spacing, scale, density="", near=None, **params):
    """One layer. *protos* is {prototype: (weight, scale)}; *near* is (layer, radius, inside,
    outside, clear radius) for Grow Near."""
    for i, (proto, (weight, proto_scale)) in enumerate(protos.items()):
        params[f"prototypes.{i}.proto_path"] = f"/protos/{proto}"
        params[f"prototypes.{i}.proto_weight"] = weight
        params[f"prototypes.{i}.proto_scale"] = proto_scale
    if near:
        layer, radius, inside, outside, clear = near
        params.update(near_prims=f"/forest/{layer}", near_radius=radius, near_inside=inside,
                      near_outside=outside, near_clear=clear)
    return scene.create.scatter_points(
        upstream, name=name, surface_prim="/ground", inst_path=f"/forest/{name.lower()}",
        max_count=count, auto_spacing=False, spacing=spacing, size_spacing=True,
        scale_min=scale[0], scale_max=scale[1], density_primvar=density, orient_mode="Up",
        rand_axis="Z", rand_amount=360.0, **params)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    with Scene.new(path=str(OUT / "forest_network.usda")) as scene:
        last = scene.create.stage_settings(up_axis="Z", meters_per_unit=1.0)
        # An 80 m ground, built 32 m wide and scaled 2.5x: its texture repeats once per unit
        # of the grid's own st, so this way it repeats every 2.5 m.
        last = scene.create.mesh(last, prim_path="/ground", mesh_type="grid", dimx1=100, dimy1=100,
                                 width1=32.0, height2=32.0)
        last = scene.create.transform(last, primpath_location="/ground", sx=2.5, sy=2.5)
        last = scene.create.inline_usd(last, name="Prototypes", code=assets.prototypes("/protos", PROTOS))
        last = scene.create.inline_usd(last, name="ForestFloor",
                                       code=assets.ground_material("/looks/forest_floor", "brown_mud_leaves_01"))
        last = scene.create.bind_material(last, material_path="/looks/forest_floor", primpath_location="/ground")

        last = scene.create.noise_field(last, name="Clearings", prim_path="/ground", primvar="forest",
                                        size=16.0, threshold=0.45, softness=0.25, seed=3)
        # A young fir is about 1.2 m; scaled 3.5x to 6x it is a 4 to 7 m tree.
        last = scatter(scene, last, "Trees", {"fir_a": (1.0, 1.0), "fir_b": (1.0, 1.3)}, 220, 2.6, (3.5, 6.0),
                       density="forest")
        # Grow Near: (layer, radius, inside, outside, clear radius)
        last = scatter(scene, last, "Deadwood", {"log": (1.0, 1.0), "stump": (1.0, 0.8), "rock": (1.5, 1.0)},
                       30, 4.0, (0.6, 1.2), near=("trees", 6.0, 1.0, 0.1, 1.5))
        last = scatter(scene, last, "Undergrowth", {"shrub": (2.0, 1.6), "nettle": (1.0, 1.0)}, 500, 0.8,
                       (3.0, 5.0), near=("trees", 4.0, 1.0, 0.0, 0.8))
        last = scatter(scene, last, "Grass", {"grass_tall": (1.0, 1.0), "grass_wide": (1.0, 1.0),
                                               "grass_mid": (1.0, 1.0)},
                       6000, 0.4, (2.0, 3.5), near=("trees", 2.5, 0.3, 1.0, 0.6))

        # The sun from the side, low, for long shadows across the clearings. Normalize makes
        # Intensity the light that lands, not the brightness of the sun's tiny disc.
        sun = scene.create.distant_light(last, primpath_location="/lights/sun", intensity=3.0, angle=1.0,
                                         normalize=True)
        aim_sun = scene.create.transform(sun, primpath_location="/lights/sun", rx=60.0, rz=-135.0)
        # The sky: an HDRI. An absolute path, because Aurora reads a dome's file as written
        # and doesn't resolve a relative one.
        sky = scene.create.dome_light(aim_sun, primpath_location="/lights/sky", intensity=0.7,
                                      file=assets.hdri().as_posix())
        # A lat-long image has its poles on Y; the stage is Z up, so turn the dome to match.
        pole = scene.create.transform(sky, primpath_location="/lights/sky", rx=90.0)
        cam = scene.create.camera(pole, primpath_location="/cam", tx=-24.0, ty=-27.0, tz=5.0,
                                  focalLength=24.0, clippingRange=(0.05, 1000.0),
                                  aim_at_point=True, aim_point=(4.0, 2.0, 1.5))
        render = scene.create.render(cam, camera="/cam", mode="Command", image_width=1280,
                                     camera_light=False, output_image=str(OUT / "forest.png"),
                                     **assets.render_params(OUT, samples=256))
        scene.set_output(render)

        scene.save()
        scene.export(str(OUT / "forest.usda"))
        scene.commit()  # runs Render
