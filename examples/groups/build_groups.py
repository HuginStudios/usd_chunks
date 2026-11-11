"""Build the ChunkGroup assets in this folder: ready-made networks a scene references.

    USD_CHUNK_DIRS=chunks python examples/groups/build_groups.py

Writes, next to this script:

    scatter_on_terrain.usda   instances spread over a terrain mesh, so many per m², on
                              faces up to a slope, resting on the surface
    spiral_stairs.usda        a box copied N times, each copy risen and turned: a spiral
                              stair, or a straight row with Turn 0

To use one, point a Reference chunk at it and set its params on that node -- see
examples/scripts/use_groups.py. `usdchunks info <file>` prints a group's description and
params. The .usda files are made by this script: change them here and run it again.
"""
from __future__ import annotations

import os
import sys

from usd_chunks import Scene, chunk_stage

HERE = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))


def loft(group, *params) -> None:
    for param in params:
        chunk_stage.loft_param_to_context(param, group)


def scatter_on_terrain(scene) -> None:
    group = scene.create.chunkgroup(name="ScatterOnTerrain", doc=(
        "Instances spread over a terrain mesh, as one PointInstancer. Wire it after what "
        "brings in the terrain and the prototype (a Sublayer, a usd_sphere ...).\n"
        "Params: Scatter_surface_prim, the terrain mesh; Scatter_proto_path, the prim to "
        "instance; Scatter_inst_path, where the PointInstancer is written; Scatter_density, "
        "instances per m² of the faces within Scatter_max_slope degrees of up (count = "
        "density x that area, exactly); Scatter_seed, a different pattern per seed; "
        "Scatter_sit_on_surface, rest each instance's bottom on the surface. Instances may "
        "overlap: spacing is off so the count is exact."))
    with group:
        scatter = scene.create.scatter_points(
            name="Scatter", surface_prim="/World/terrain", inst_path="/World/scatter",
            prototypes=[{"proto_path": "/prototypes/rock"}], scatter_mode="Surface",
            density=1.0, max_slope=90.0, sit_on_surface=True, auto_spacing=False, spacing=0.0,
            max_count=1_000_000,
        )
        scene.set_output(scatter)
    rows = scatter.get_param("prototypes")
    loft(group, scatter.get_param("surface_prim"), rows.get_indexed_param("proto_path", 0),
         scatter.get_param("inst_path"), scatter.get_param("density"), scatter.get_param("seed"),
         scatter.get_param("max_slope"), scatter.get_param("sit_on_surface"))
    chunk_stage.export_network(scene, group, os.path.join(HERE, "scatter_on_terrain.usda"))


def spiral_stairs(scene) -> None:
    group = scene.create.chunkgroup(name="SpiralStairs", doc=(
        "A box copied N times, each copy risen and turned about the Y axis: a spiral stair. "
        "Writes Stairs_dest_path/step_00, step_01 ... as USD Cube prims.\n"
        "Params: Stairs_copies, the number of steps; Place_tx, the radius to a step's "
        "centre; Place_ty, the first step's centre height; Place_sx / Place_sy / Place_sz, "
        "a step's length along the radius, height and width; Stairs_copy_translate, the "
        "rise per step (0, rise, 0); Stairs_copy_rotate, the turn per step (0, -degrees, 0): "
        "negative turns +X towards +Z; 0 makes a straight row; Stairs_dest_path, where the "
        "steps go."))
    with group:
        # One step, then copies of it. The step's own path is wired to each chunk that names
        # it, so it is set in one place.
        step = scene.create.usd_cube(prim_path="/World/stairs/step", name="Step")   # 1 m, centred
        path = step.get_param("prim_path")
        place = scene.create.transform(step, primpath_location=path, tx=2.0, ty=0.1,
                                       sx=1.0, sy=0.2, sz=0.4, name="Place")
        stairs = scene.create.duplicate_prim(place, source_prim=path, dest_path="/World/stairs",
                                             copies=20, copy_translate=(0.0, 0.2, 0.0),
                                             copy_rotate=(0.0, -18.0, 0.0), name="Stairs")
        last = scene.create.prune_prim(stairs, prim_path=path, name="DropSource")
        scene.set_output(last)
    loft(group, stairs.get_param("copies"), place.get_param("tx"), place.get_param("ty"),
         place.get_param("sx"), place.get_param("sy"), place.get_param("sz"),
         stairs.get_param("copy_translate"), stairs.get_param("copy_rotate"),
         stairs.get_param("dest_path"))
    chunk_stage.export_network(scene, group, os.path.join(HERE, "spiral_stairs.usda"))


if __name__ == "__main__":
    # Lazy: nothing cooks. A group's own file holds no terrain to scatter on.
    for build in (scatter_on_terrain, spiral_stairs):
        with Scene(lazy=True) as scene:
            build(scene)
    print(f"Wrote scatter_on_terrain.usda and spiral_stairs.usda in {HERE}")
