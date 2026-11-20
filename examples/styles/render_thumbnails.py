"""Render the picture of each style in chunks/styles: the style on a sample object.

    USD_CHUNK_DIRS=chunks python examples/styles/render_thumbnails.py [usdrecord command] [style ...]

Puts each style on a sample object the way the Styles menu does (a Reference with the object
as its Location; the ground as Around_ground where the style has one), writes the result
flattened, with a camera, a sky and a sun, as <style>.thumb.usda in a temporary folder, and
renders it to <style>.png beside the style with usdrecord. The command defaults to
`usdrecord` on PATH; give the full command when it needs a wrapper that sets up USD's
environment, or picks a renderer (Aurora makes the best pictures). Run build_styles.py first.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux, Vt

from usd_chunks import Scene, polyhaven, styles

SIZE = 256


def _ground(stage, size=6.0, tile=1.0):
    """A flat grey ground at height 0, its UVs repeating every tile units (Aurora ignores a UV
    transform, so the repeat is in the UVs)."""
    h = size / 2
    mesh = UsdGeom.Mesh.Define(stage, "/ground")
    mesh.CreatePointsAttr([(-h, 0, -h), (h, 0, -h), (h, 0, h), (-h, 0, h)])
    mesh.CreateFaceVertexCountsAttr([4])
    mesh.CreateFaceVertexIndicesAttr([0, 3, 2, 1])
    uv = size / tile
    UsdGeom.PrimvarsAPI(mesh).CreatePrimvar("st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.vertex).Set(
        [(0, 0), (0, uv), (uv, uv), (uv, 0)])
    mesh.CreateDisplayColorAttr([(0.42, 0.41, 0.4)])
    return "/ground"


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
    """A sample object: (the chunk that makes it or None, its path, the ground's path or None,
    the prim the camera frames)."""
    stage = scene.get_stage()
    if kind == "steps":
        _steps(stage)
        return None, "/steps", None, "/steps"
    if kind == "wall":
        return scene.create.cube(prim_path="/wall", dimx=33, dimy=25, dimz=5, width=2.0, height=1.5,
                                 depth=0.3), "/wall", None, "/wall"
    if kind == "ground":
        return None, _ground(stage, size=5.0, tile=2.5), None, "/ground"
    if kind == "house":
        ground = _ground(stage, size=14.0)
        house = scene.create.house(house_path="/House", width=4.0, depth=3.6)
        return scene.create.transform(house, primpath_location="/House", tx=-2.0, tz=-1.8), "/House", ground, \
            "/House"
    ground = _ground(stage)
    crate = scene.create.cube(prim_path="/crate", dimx=31, dimy=31, dimz=31, width=1.2, height=1.2, depth=1.2)
    return crate, "/crate", ground, "/crate"


#: The sample object each style is shown on; anything else goes on a crate on the ground.
SAMPLES = {"worn_paint": "crate_only", "dirty_stone": "steps", "rubble": "steps", "hanging_vines": "wall",
           "abandoned_cottage": "house", "overgrown_ruin": "steps", "ruins_ground": "ground",
           "forest_floor": "ground", "asphalt_lot": "ground"}

#: How far the camera stands from what it frames, in the frame's sizes, and how much bigger
#: than the framed prim the view is (a pile reaches past the crate).
DISTANCE = {"crate": (2.3, 2.2), "crate_only": (3.2, 1.0), "steps": (1.9, 1.0), "wall": (2.4, 1.0),
            "house": (1.7, 1.6), "ground": (1.4, 1.0)}


def _frame(stage, out: Path, target: str, distance: float, grow: float, sky: Path | None) -> None:
    """Write stage flattened to out, with a camera framing target and a sky and a sun."""
    flat = Usd.Stage.Open(stage.Flatten())
    box = UsdGeom.BBoxCache(Usd.TimeCode.Default(), ["default", "render"]).ComputeWorldBound(
        flat.GetPrimAtPath(target)).ComputeAlignedRange()
    size = max(box.GetSize()) * grow
    mid = box.GetMidpoint()
    centre = Gf.Vec3d(mid[0], box.GetMin()[1] + 0.35 * box.GetSize()[1], mid[2])
    cam = UsdGeom.Camera.Define(flat, "/thumb_cam")
    eye = centre + Gf.Vec3d(0.9, 0.75, 1.25).GetNormalized() * size * distance
    UsdGeom.Xformable(cam).AddTransformOp().Set(Gf.Matrix4d().SetLookAt(eye, centre, Gf.Vec3d(0, 1, 0)).GetInverse())
    cam.CreateFocalLengthAttr(35)
    cam.CreateHorizontalApertureAttr(24.0)  # square, like the picture
    cam.CreateVerticalApertureAttr(24.0)
    dome = UsdLux.DomeLight.Define(flat, "/thumb_sky")
    dome.CreateIntensityAttr(1.0)
    if sky is not None:
        dome.CreateTextureFileAttr(str(sky))
    sun = UsdLux.DistantLight.Define(flat, "/thumb_sun")
    sun.CreateIntensityAttr(3.0)
    sun.CreateAngleAttr(1.5)
    sun.CreateNormalizeAttr(True)  # Storm draws MaterialX black under an unnormalized sun
    UsdGeom.Xformable(sun).AddRotateXYZOp().Set(Gf.Vec3f(-50, 35, 0))
    chunks = flat.GetPrimAtPath("/chunks")
    if chunks:
        chunks.SetActive(False)
    flat.GetRootLayer().Export(str(out))


def main(record: list[str], names: set[str]) -> None:
    found = [s for s in styles.find_styles() if not names or s.name in names]
    if not found:
        sys.exit("No styles found: run build_styles.py, with USD_CHUNK_DIRS pointing at chunks/.")
    try:
        sky = polyhaven.download_hdri("kloofendal_48d_partly_cloudy_puresky")
    except Exception:  # noqa: BLE001 - offline: a plain sky
        sky = None
    work = Path(tempfile.mkdtemp(prefix="style_thumbs_"))
    # usdrecord may be another USD build than the one this script imported: on Windows, pip's
    # usd-core sets PXR_USD_WINDOWS_DLL_PATH to its own DLLs, and that USD then loads them.
    env = {k: v for k, v in os.environ.items() if k != "PXR_USD_WINDOWS_DLL_PATH"}
    for style in found:
        kind = SAMPLES.get(style.name, "crate")
        with Scene() as scene:
            maker, path, ground, frame = _object(scene, "crate" if kind == "crate_only" else kind)
            inputs = [maker] if maker is not None else []
            node = scene.add(style.name, *inputs, primpath_location=path)
            if ground and node.get_param("Around_ground") is not None:
                node.get_param("Around_ground").value = ground
            scene_file = work / f"{style.name}.thumb.usda"
            _frame(scene.get_stage(), scene_file, frame, *DISTANCE[kind], sky)
        picture = style.path.with_suffix(".png")
        cmd = [*record, "--camera", "/thumb_cam", "--imageWidth", str(SIZE), str(scene_file), str(picture)]
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        print(f"{style.label}: {picture if result.returncode == 0 else 'usdrecord failed: ' + result.stderr[-300:]}",
              flush=True)


if __name__ == "__main__":
    # posix=False on Windows, or the backslashes in a path are taken for escapes.
    command = shlex.split(sys.argv[1], posix=os.name != "nt") if len(sys.argv) > 1 else ["usdrecord"]
    main(command, set(sys.argv[2:]))
