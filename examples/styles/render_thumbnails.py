"""Render the picture of each style in chunks/styles: the style on a sample object.

    USD_CHUNK_DIRS=chunks python examples/styles/render_thumbnails.py [usdrecord command]

Puts each style on a sample object the way the Styles menu does (a Reference with the object
as its Location), writes the result flattened, with a camera and a light, as
<style>.thumb.usda in a temporary folder, and renders it to <style>.png beside the style with
usdrecord. The command defaults to `usdrecord` on PATH; give the full command when it needs a
wrapper that sets up USD's environment. Run build_styles.py first.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from pxr import Gf, Usd, UsdGeom, UsdLux, Vt

from usd_chunks import Scene, styles

SIZE = 256


def _steps(stage, path="/steps", size=2.0, n=81, rise=0.25, count=3):
    """Stone steps as one dense grid: each step a band of height, the risers one cell wide."""
    xs = np.linspace(-size / 2, size / 2, n)
    gx, gz = np.meshgrid(xs, xs, indexing="ij")
    height = np.floor(np.clip((size / 2 - gz) / (size / (count + 1)), 0, count)) * rise
    pts = np.stack([gx.ravel(), height.ravel(), gz.ravel()], axis=1)
    idx = [v for i in range(n - 1) for j in range(n - 1)
           for v in (i * n + j, i * n + j + 1, (i + 1) * n + j + 1, (i + 1) * n + j)]
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(pts.astype(np.float32)))
    mesh.CreateFaceVertexCountsAttr([4] * ((n - 1) ** 2))
    mesh.CreateFaceVertexIndicesAttr(idx)
    mesh.CreateSubdivisionSchemeAttr("none")


def _object(scene, kind):
    """A sample object to show a style on: (the chunk that makes it or None, its path)."""
    if kind == "steps":
        _steps(scene.get_stage())
        return None, "/steps"
    if kind == "wall":
        return scene.create.cube(prim_path="/wall", dimx=33, dimy=25, dimz=5, width=2.0, height=1.5,
                                 depth=0.3), "/wall"
    return scene.create.cube(prim_path="/crate", dimx=31, dimy=31, dimz=31, width=1.5, height=1.5,
                             depth=1.5), "/crate"


#: The sample object each style is shown on; anything else goes on the crate.
SAMPLES = {"dirty_stone": "steps", "rubble": "steps", "hanging_vines": "wall"}

#: How far the camera stands from each sample object, in the object's sizes.
DISTANCE = {"crate": 3.2, "steps": 1.9, "wall": 2.4}


def _frame(stage, out: Path, target: str, distance: float) -> None:
    """Write stage flattened to out, with a camera framing target from distance (in its sizes), and a sun."""
    flat = Usd.Stage.Open(stage.Flatten())
    box = UsdGeom.BBoxCache(Usd.TimeCode.Default(), ["default", "render"]).ComputeWorldBound(
        flat.GetPrimAtPath(target)).ComputeAlignedRange()
    centre, size = box.GetMidpoint(), max(box.GetSize())
    cam = UsdGeom.Camera.Define(flat, "/thumb_cam")
    eye = centre + Gf.Vec3d(0.9, 0.75, 1.25).GetNormalized() * size * distance
    UsdGeom.Xformable(cam).AddTransformOp().Set(Gf.Matrix4d().SetLookAt(eye, centre, Gf.Vec3d(0, 1, 0)).GetInverse())
    cam.CreateFocalLengthAttr(35)
    cam.CreateHorizontalApertureAttr(24.0)  # square, like the picture
    cam.CreateVerticalApertureAttr(24.0)
    UsdLux.DomeLight.Define(flat, "/thumb_sky").CreateIntensityAttr(0.7)
    sun = UsdLux.DistantLight.Define(flat, "/thumb_sun")
    sun.CreateIntensityAttr(2.5)
    sun.CreateNormalizeAttr(True)  # Storm draws MaterialX black under an unnormalized sun
    UsdGeom.Xformable(sun).AddRotateXYZOp().Set(Gf.Vec3f(-55, 30, 0))
    chunks = flat.GetPrimAtPath("/chunks")
    if chunks:
        chunks.SetActive(False)
    flat.GetRootLayer().Export(str(out))


def main(record: list[str]) -> None:
    found = styles.find_styles()
    if not found:
        sys.exit("No styles found: run build_styles.py, with USD_CHUNK_DIRS pointing at chunks/.")
    work = Path(tempfile.mkdtemp(prefix="style_thumbs_"))
    # usdrecord may be another USD build than the one this script imported: on Windows, pip's
    # usd-core sets PXR_USD_WINDOWS_DLL_PATH to its own DLLs, and that USD then loads them.
    env = {k: v for k, v in os.environ.items() if k != "PXR_USD_WINDOWS_DLL_PATH"}
    for style in found:
        with Scene() as scene:
            kind = SAMPLES.get(style.name, "crate")
            maker, path = _object(scene, kind)
            inputs = [maker] if maker is not None else []
            scene.add(style.name, *inputs, primpath_location=path)
            scene_file = work / f"{style.name}.thumb.usda"
            _frame(scene.get_stage(), scene_file, path, DISTANCE[kind])
        picture = style.path.with_suffix(".png")
        cmd = [*record, "--camera", "/thumb_cam", "--imageWidth", str(SIZE), "--disableCameraLight",
               str(scene_file), str(picture)]
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        print(f"{style.label}: {picture if result.returncode == 0 else 'usdrecord failed: ' + result.stderr[-300:]}")


if __name__ == "__main__":
    # posix=False on Windows, or the backslashes in a path are taken for escapes.
    main(shlex.split(sys.argv[1], posix=os.name != "nt") if len(sys.argv) > 1 else ["usdrecord"])
