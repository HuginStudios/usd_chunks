"""Use the ready-made ChunkGroups in examples/groups: rocks on a terrain, and a spiral stair.

    USD_CHUNK_DIRS=chunks python examples/scripts/use_groups.py [folder]

A group is a whole network in one file. Point a Reference chunk at it and set its params
on that node -- `usdchunks info examples/groups/<file>.usda` lists them. Writes, under
folder (default ./use_groups): terrain.usda (made here), groups_network.usda, groups.usda.
"""
from __future__ import annotations

import os
import sys

from pxr import Sdf, Usd, UsdGeom

from usd_chunks import Scene

GROUPS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "groups"))
folder = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "use_groups")
os.makedirs(folder, exist_ok=True)

# A 10 x 10 m terrain to scatter on. A real one comes in from a file the same way.
terrain = Usd.Stage.CreateNew(os.path.join(folder, "terrain.usda"))
UsdGeom.SetStageMetersPerUnit(terrain, 1.0)
UsdGeom.SetStageUpAxis(terrain, UsdGeom.Tokens.y)
mesh = UsdGeom.Mesh.Define(terrain, "/World/terrain")
mesh.CreatePointsAttr([(-5, 0, -5), (5, 0, -5), (5, 0, 5), (-5, 0, 5)])
mesh.CreateFaceVertexCountsAttr([4])
mesh.CreateFaceVertexIndicesAttr([0, 3, 2, 1])
terrain.SetDefaultPrim(terrain.GetPrimAtPath("/World"))
terrain.GetRootLayer().Save()
Sdf.Layer.FindOrOpen(os.path.join(folder, "terrain.usda")).Reload()

with Scene.new(path=os.path.join(folder, "groups_network.usda")) as scene:
    # Metres: Scatter's density is per square metre of the stage's units.
    last = scene.create.stage_settings(meters_per_unit=1.0, up_axis="Y", default_prim="World")
    last = scene.create.sublayer(last, imports=["terrain.usda"])
    last = scene.create.usd_sphere(last, prim_path="/World/prototypes/rock", sphere_radius=0.2)

    # 2 rocks per m² of the faces within 20° of up: 200 here. Seed 7, resting on the surface.
    last = scene.create.reference(
        last, file_path=os.path.join(GROUPS, "scatter_on_terrain.usda"), name="Rocks",
        Scatter_surface_prim="/World/terrain", Scatter_proto_path="/World/prototypes/rock",
        Scatter_inst_path="/World/rocks", Scatter_density=2.0, Scatter_max_slope=20.0,
        Scatter_seed=7,
    )

    # 12 steps of 1.0 x 0.2 x 0.4 m, 2 m from the axis, each 0.2 m up and 30° on.
    last = scene.create.reference(
        last, file_path=os.path.join(GROUPS, "spiral_stairs.usda"), name="Stairs",
        Stairs_dest_path="/World/stairs", Stairs_copies=12,
        Stairs_copy_translate=(0.0, 0.2, 0.0), Stairs_copy_rotate=(0.0, -30.0, 0.0),
    )
    scene.set_output(last)
    scene.save()                                   # the network
    scene.export(os.path.join(folder, "groups.usda"))   # the result, plain USD

print(f"Wrote {folder}")
# Seeds and step counts from the command line, on the saved network:
#   usdchunks run groups_network.usda Rocks.Scatter_seed=3 Stairs.Stairs_copies=20 -o out.usda
