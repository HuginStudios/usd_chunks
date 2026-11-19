"""Batch an asset library: one network, run headless once per file, to make a proxy of each.

    USD_CHUNK_DIRS=chunks python examples/scripts/batch_library.py

  1. Writes a small stand-in library to out/library/: a sphere, a torus and a cone, dense
     meshes, one asset per .usda. Point the batch at your own folder instead.
  2. Builds the proxy network once and saves it, out/proxy_network.usda:
       Reference   brings in one asset at /asset (its File Path is what the batch swaps)
       Poly Reduce keeps 10% of the triangles, keeping the silhouette and UV seams
  3. Runs it over the whole library with the usdchunks CLI, as a farm or CI job would:

       usdchunks batch out/proxy_network.usda "Reference.file_path=library/*.usda" -o "proxies/{1}_proxy.usda" --dry-run
       usdchunks batch out/proxy_network.usda "Reference.file_path=library/*.usda" -o "proxies/{1}_proxy.usda"

     Patterns are relative to the network's folder, so this works from any directory.
     `{1}` is what the first `*` matched: library/torus.usda -> proxies/torus_proxy.usda.
  4. Prints each asset's triangle count before and after.

Runs with the usd-core wheel: no renderer needed.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from pxr import Usd, UsdGeom

from usd_chunks import Scene

HERE = Path(__file__).resolve().parent
# Results go to the folder given on the command line, else examples/scripts/out.
OUT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else HERE / "out"
ASSETS = {
    "sphere": {"mesh_type": "sphere", "dimx2": 160, "dimy2": 120, "radius1": 0.5},
    "torus": {"mesh_type": "torus", "torus_rings": 256, "torus_sides": 96, "torus_radius": 0.5,
              "torus_tube_radius": 0.15},
    "cone": {"mesh_type": "cone", "cone_rows": 96, "cone_cols": 256, "cone_radius": 0.4,
             "cone_height": 1.0},
}


def triangles(path: Path) -> int:
    """How many triangles the meshes in a USD file add up to."""
    stage = Usd.Stage.Open(str(path))
    total = 0
    for prim in stage.Traverse():
        if prim.IsA(UsdGeom.Mesh):
            counts = UsdGeom.Mesh(prim).GetFaceVertexCountsAttr().Get() or []
            total += sum(c - 2 for c in counts)
    return total


def usdchunks(*args: str) -> None:
    """Run the usdchunks CLI with this Python, so it finds the same pxr and usd_chunks."""
    subprocess.run([sys.executable, "-m", "usd_chunks.cli", *args], check=True)


if __name__ == "__main__":
    library = OUT / "library"
    library.mkdir(parents=True, exist_ok=True)

    # 1. A stand-in library, one asset per file.
    for name, params in ASSETS.items():
        with Scene.new() as scene:
            # An asset is a root Xform with its meshes under it, the usual layout.
            mesh = scene.create.mesh(prim_path=f"/{name}/geo", **params)
            root = scene.create.stage_settings(mesh, default_prim=name)
            scene.set_output(root)
            scene.export(str(library / f"{name}.usda"))

    # 2. The proxy network, built once against any one asset.
    with Scene.new(path=str(OUT / "proxy_network.usda")) as scene:
        ref = scene.create.reference(name="Reference", file_path="library/sphere.usda",
                                     primpath_location="/asset")
        reduce = scene.create.poly_reduce(ref, name="Reduce", prim_path="/asset", keep_percent=10.0)
        scene.set_output(reduce)
        scene.save()

    # 3. The batch: look at the plan first, then run it.
    network = str(OUT / "proxy_network.usda")
    batch = [network, "Reference.file_path=library/*.usda", "-o", "proxies/{1}_proxy.usda"]
    usdchunks("batch", *batch, "--dry-run")
    usdchunks("batch", *batch)

    # 4. Before and after.
    for name in ASSETS:
        before = triangles(library / f"{name}.usda")
        after = triangles(OUT / "proxies" / f"{name}_proxy.usda")
        print(f"{name:8s} {before:7d} -> {after:6d} triangles")
