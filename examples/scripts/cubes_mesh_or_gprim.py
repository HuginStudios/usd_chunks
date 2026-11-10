"""A cube and a sphere two ways: as polygon meshes, and as USD's own shape prims (gprims).

    USD_CHUNK_DIRS=chunks python examples/scripts/cubes_mesh_or_gprim.py [file]

Pick by the prim type a task asks for:

    scene.create.cube / sphere           Mesh chunk  -> a Mesh prim: polygons with UVs, which
                                                         you can deform, subdivide or texture
    scene.create.usd_cube / usd_sphere   Shape chunk -> a Cube / Sphere prim: USD's own shape,
                                                         a size and nothing else; the renderer
                                                         makes the surface

"A USD Cube", "a Cube prim", "UsdGeom.Cube" mean usd_cube. They also sit differently:
the Mesh cube's bottom is at its origin, the USD Cube is centred on it.
"""
from __future__ import annotations

import sys

from usd_chunks import Scene

out = sys.argv[1] if len(sys.argv) > 1 else "cubes_mesh_or_gprim.usda"

with Scene.new() as scene:
    # Polygon meshes (Mesh chunk). The cube spans y 0..1: it rests on y=0 as it is.
    last = scene.create.cube(prim_path="/World/mesh_box", name="MeshBox")
    last = scene.create.sphere(last, prim_path="/World/mesh_ball", name="MeshBall")

    # USD shape prims (Shape chunk). Centred on the prim: a 1 m Cube spans y -0.5..0.5, so
    # lift it by half its size to rest it on y=0.
    last = scene.create.usd_cube(last, prim_path="/World/usd_box", cube_size=1.0, name="UsdBox")
    last = scene.create.transform(last, primpath_location="/World/usd_box", tx=2.0, ty=0.5)
    last = scene.create.usd_sphere(last, prim_path="/World/usd_ball", sphere_radius=0.5, name="UsdBall")
    last = scene.create.transform(last, primpath_location="/World/usd_ball", tx=4.0, ty=0.5)

    scene.set_output(last)
    scene.export(out)

print(f"Wrote {out}: /World/mesh_box and /World/mesh_ball are Mesh, /World/usd_box is Cube, "
      "/World/usd_ball is Sphere")
