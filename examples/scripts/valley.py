"""Scatter a biome by height: a valley with a village, a forest, a lake and bare peaks.

    USD_CHUNK_DIRS=chunks python examples/scripts/valley.py

The script makes the terrain (terrain.usdc, beside the network). Height Field chunks mark
where things grow: houses on the flat valley floor, firs below the tree line, grass low
down, rocks on the peaks, sand along the lake. Every layer keeps away from the houses.
Writes valley.png.

Change it without editing the script:

    usdchunks run examples/scripts/out/valley_network.usda TreeLine.max_height=18 Houses.max_count=3
"""
from __future__ import annotations

import sys
from pathlib import Path

import example_assets as assets
import numpy as np
from pxr import Gf, Sdf, Usd, UsdGeom, Vt

from usd_chunks import Scene

HERE = Path(__file__).resolve().parent
# Results go to the folder given on the command line, else examples/scripts/out.
OUT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else HERE / "out"

SIZE = 240.0  # metres across
WATER = 0.8  # the lake's surface, metres up
EARTH = (0.2, 0.19, 0.12)  # the land past the terrain's edge, as the sky shows it

# Prototype name: (Poly Haven model, the variation to use, or None for a model of one part).
PROTOS = {
    "fir_a": ("fir_sapling", "fir_sapling_a"),
    "fir_b": ("fir_sapling", "fir_sapling_b"),
    "shrub": ("shrub_04", "shrub_04_c_LOD0"),
    "grass_tall": ("grass_medium_01", "grass_medium_01_tall_a_LOD0"),
    "grass_wide": ("grass_medium_01", "grass_medium_01_large_a_LOD0"),
    "boulder": ("boulder_01", None),
}

COTTAGES = [
    {"width": 6.5, "roof_style": "Gable", "floors": 1, "wall_color": (0.52, 0.12, 0.08)},
    {"width": 5.0, "roof_style": "Gambrel", "floors": 2, "wall_color": (0.85, 0.75, 0.45), "shutters": True},
    {"width": 7.5, "roof_style": "Hip", "floors": 1, "wall_color": (0.3, 0.38, 0.45)},
]


def terrain(path: Path, cells: int = 96) -> Path:
    """Write a valley as grid meshes, Z up: the one piece of plain USD here.

    /terrain/ground: a valley floor runs along X with a lake in it, between ridges about 60 m
    high, with a peak behind the far shore. The ridges come down at the far end, where the
    valley opens onto a plain. /terrain/far: that plain, coarse, out to the horizon around
    the valley. st repeats every 4 m (Aurora ignores UV Transform).
    """
    n = cells + 1
    x, y = np.meshgrid(np.linspace(-SIZE / 2, SIZE / 2, n), np.linspace(-SIZE / 2, SIZE / 2, n))
    axis = 10.0 * np.sin(x / 45.0)  # the valley meanders
    side = np.clip((np.abs(y - axis) - 18.0) / 85.0, 0.0, 1.0)  # 0 on the floor, 1 on the ridge
    side *= np.clip((SIZE / 2 - x) / 70.0, 0.0, 1.0)  # the ridges come down at the far end
    rng = np.random.default_rng(7)
    bumps = sum(a * np.sin(x / w + rng.uniform(0, 6)) * np.cos(y / w + rng.uniform(0, 6))
                for a, w in ((6.0, 23.0), (3.0, 11.0), (1.2, 5.0)))
    height = 3.0 + 62.0 * side ** 1.6 + bumps * (0.1 + side)
    height += 40.0 * np.exp(-((x - 40.0) ** 2 + (y - 95.0) ** 2) / 900.0)  # a peak behind the far shore
    height -= 5.0 * np.exp(-((x + 60.0) ** 2 / 1200.0 + (y - axis) ** 2 / 200.0))  # the lake's bed

    stage = Usd.Stage.CreateNew(str(path))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    root = UsdGeom.Xform.Define(stage, "/terrain")
    stage.SetDefaultPrim(root.GetPrim())
    mesh = UsdGeom.Mesh.Define(stage, "/terrain/ground")
    points = np.stack([x, y, height], axis=-1).reshape(-1, 3).astype(np.float32)
    mesh.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(points))
    i, j = np.meshgrid(np.arange(cells), np.arange(cells))
    corner = (j * n + i).ravel()
    quads = np.stack([corner, corner + 1, corner + n + 1, corner + n], axis=-1)
    mesh.CreateFaceVertexCountsAttr(Vt.IntArray.FromNumpy(np.full(len(quads), 4, dtype=np.int32)))
    mesh.CreateFaceVertexIndicesAttr(Vt.IntArray.FromNumpy(quads.ravel().astype(np.int32)))
    mesh.CreateExtentAttr([Gf.Vec3f(*map(float, points.min(axis=0))), Gf.Vec3f(*map(float, points.max(axis=0)))])
    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
    st = UsdGeom.PrimvarsAPI(mesh).CreatePrimvar("st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.vertex)
    st.Set(Vt.Vec2fArray.FromNumpy((points[:, :2] / 4.0).astype(np.float32)))
    _plain(stage, "/terrain/far", floor=3.0)
    stage.Save()
    return path


def _plain(stage, path: str, floor: float, reach: float = 3000.0, cell: float = 30.0) -> None:
    """A flat, coarse ground at *floor* around the valley, with a hole where the valley is."""
    n = int(reach / cell)
    x, y = np.meshgrid(np.linspace(-reach / 2, reach / 2, n + 1), np.linspace(-reach / 2, reach / 2, n + 1))
    points = np.stack([x, y, np.full_like(x, floor)], axis=-1).reshape(-1, 3).astype(np.float32)
    i, j = np.meshgrid(np.arange(n), np.arange(n))
    corner = (j * (n + 1) + i).ravel()
    quads = np.stack([corner, corner + 1, corner + n + 2, corner + n + 1], axis=-1)
    centre = points[quads].mean(axis=1)
    keep = (np.abs(centre[:, 0]) > SIZE / 2 - cell) | (np.abs(centre[:, 1]) > SIZE / 2 - cell)
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(points))
    mesh.CreateFaceVertexCountsAttr(Vt.IntArray.FromNumpy(np.full(int(keep.sum()), 4, dtype=np.int32)))
    mesh.CreateFaceVertexIndicesAttr(Vt.IntArray.FromNumpy(quads[keep].ravel().astype(np.int32)))
    mesh.CreateExtentAttr([Gf.Vec3f(*map(float, points.min(axis=0))), Gf.Vec3f(*map(float, points.max(axis=0)))])
    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
    st = UsdGeom.PrimvarsAPI(mesh).CreatePrimvar("st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.vertex)
    st.Set(Vt.Vec2fArray.FromNumpy((points[:, :2] / 4.0).astype(np.float32)))


def scatter(scene, upstream, name, protos, count, spacing, scale, density="", keep_away=0.0, **params):
    """One Scatter Points layer on the terrain. *protos* is {prototype path: (weight, scale)}."""
    for i, (proto, (weight, proto_scale)) in enumerate(protos.items()):
        params[f"prototypes.{i}.proto_path"] = proto
        params[f"prototypes.{i}.proto_weight"] = weight
        params[f"prototypes.{i}.proto_scale"] = proto_scale
    if keep_away:
        # Every layer after the village keeps away from the houses' points.
        params.update(avoid_prims="/World/biome/houses", avoid_distance=keep_away)
    return scene.create.scatter_points(
        upstream, name=name, surface_prim="/World/terrain/ground", inst_path=f"/World/biome/{name.lower()}",
        max_count=count, auto_spacing=False, spacing=spacing, size_spacing=True,
        scale_min=scale[0], scale_max=scale[1], density_primvar=density, orient_mode="Up",
        rand_axis="Z", rand_amount=360.0, **params)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    ground = terrain(OUT / "terrain.usdc")
    with Scene.new(path=str(OUT / "valley_network.usda")) as scene:
        last = scene.create.stage_settings(up_axis="Z", meters_per_unit=1.0, default_prim="World")
        last = scene.create.reference(last, name="Terrain", file_path=ground.name, primpath_location="/World/terrain")

        # Where things may grow, by height. Each Height Field writes a 0..1 primvar on the
        # ground, which a Scatter Points reads as its Density Field. Softness fades it out
        # past both ends, so keep the soft edge above the water line.
        last = scene.create.height_field(last, name="ValleyFloor", prim_path="/World/terrain", primvar="valley",
                                         min_height=3.0, max_height=6.0, softness=0.0)
        last = scene.create.noise_field(last, name="Clearings", prim_path="/World/terrain", primvar="clearings",
                                        size=30.0, threshold=0.4, softness=0.3, seed=5)
        last = scene.create.height_field(last, name="TreeLine", prim_path="/World/terrain", primvar="forest",
                                         base_primvar="clearings", min_height=5.0, max_height=32.0, softness=4.0)
        last = scene.create.height_field(last, name="Lowland", prim_path="/World/terrain", primvar="lowland",
                                         min_height=2.6, max_height=22.0, softness=1.5)
        last = scene.create.height_field(last, name="Peaks", prim_path="/World/terrain", primvar="peaks",
                                         min_height=42.0, max_height=500.0, softness=4.0, subset="rock")
        last = scene.create.height_field(last, name="Shore", prim_path="/World/terrain", primvar="shore",
                                         min_height=-10.0, max_height=WATER + 0.9, softness=0.0, subset="sand")

        # Grass on the ground, sand on the Shore's faces, rock on the Peaks'.
        last = assets.texture_look(scene, last, "/World/looks/grass", "aerial_grass_rock",
                                   "/World/terrain/ground,/World/terrain/far")
        last = assets.texture_look(scene, last, "/World/looks/sand", "coast_sand_01", "/World/terrain/ground/sand")
        last = assets.texture_look(scene, last, "/World/looks/rock", "rock_boulder_dry", "/World/terrain/ground/rock")
        # The lake: a flat sheet at the water line, under the ground everywhere but the lake bed.
        last = scene.create.mesh(last, prim_path="/World/water", mesh_type="grid", dimx1=2, dimy1=2,
                                 width1=SIZE, height2=SIZE)
        last = scene.create.transform(last, primpath_location="/World/water", tz=WATER)
        last = scene.create.preview_surface(last, name="Water", mat_path="/World/looks/water",
                                            primpath_location="/World/water", diffuse_color=(0.02, 0.06, 0.08),
                                            roughness=0.05)

        # The prototypes, hidden: three cottages and the Poly Haven models.
        for i, cottage in enumerate(COTTAGES, start=1):
            last = scene.create.house(last, name=f"Cottage{i}", house_path=f"/World/protos/cottage{i}", **cottage)
        last = assets.add_models(scene, last, "/World/protos", PROTOS, OUT, looks="/World/looks")

        p = "/World/protos/"
        # The village first, on the flat valley floor, so every later layer can keep away from it.
        last = scatter(scene, last, "Houses", {f"{p}cottage{i}": (1.0, 1.0) for i in (1, 2, 3)}, 9, 16.0,
                       (1.0, 1.0), density="valley", max_slope=6.0)
        # A young fir is about 1.2 m; scaled 4x to 7x it is a 5 to 8 m tree.
        last = scatter(scene, last, "Trees", {f"{p}fir_a": (1.0, 1.0), f"{p}fir_b": (1.0, 1.3)}, 260, 3.5,
                       (4.0, 7.0), density="forest", keep_away=7.0, max_slope=35.0)
        last = scatter(scene, last, "Shrubs", {f"{p}shrub": (1.0, 1.6)}, 300, 1.5, (2.5, 4.0), density="forest",
                       keep_away=5.0, max_slope=35.0, near_prims="/World/biome/trees", near_radius=5.0,
                       near_inside=1.0, near_outside=0.0, near_clear=0.8)
        last = scatter(scene, last, "Grass", {f"{p}grass_tall": (1.0, 1.0), f"{p}grass_wide": (1.0, 1.0)}, 20000,
                       0.5, (2.0, 3.5), density="lowland", keep_away=4.0, max_slope=30.0)
        last = scatter(scene, last, "Boulders", {f"{p}boulder": (1.0, 1.0)}, 120, 6.0, (0.3, 1.0), density="peaks")

        # The sun late in the afternoon, along the valley, and the sky: Atmosphere when it is
        # installed (see example_assets.sky), else a Distant Light and an HDRI.
        last = assets.sky(scene, last, "/World", elevation=22.0, azimuth=300.0, ground=EARTH)
        cam = scene.create.camera(last, primpath_location="/World/cam", tx=-128.0, ty=-22.0, tz=24.0,
                                  focalLength=30.0, clippingRange=(0.1, 2000.0),
                                  aim_at_point=True, aim_point=(20.0, 4.0, 2.0))
        render = scene.create.render(cam, camera="/World/cam", mode="Command", image_width=1280,
                                     camera_light=False, output_image=str(OUT / "valley.png"),
                                     **assets.render_params(OUT, samples=256))
        scene.set_output(render)

        scene.save()
        scene.export(str(OUT / "valley.usda"))
        scene.commit()  # runs Render
