"""Finish Frame chunk -- what a frame needs after the render: sky, haze, sun and grade.

Some renderers leave parts of a shot undone. hdVRay draws no aerial perspective with a plain
dome and distant light, and leaves the dome out of the camera background (alpha 0). This
chunk does those steps on the rendered image, from the stage's own camera, dome and sun, so
a frame can be finished inside chunks:

* **Sky**: pixels the render left transparent get the dome image (or another backplate)
  seen through the shot camera, at the dome's own intensity.
* **Haze**: distance haze, from a depth pass ray-cast through the same camera (embree via
  trimesh), brighter toward the sun.
* **Sun**: light shafts smeared from the sun's spot on screen, bloom on the brightest
  parts, and the sun's disc painted where the sky shows.
* **Grade**: exposure, a colour balance, contrast, saturation and a lift in the shadows.

Runs in commit(), after the Render chunk upstream of it has written its image (commit runs
upstream first), and from Finish Now. It writes a new image and authors nothing.
"""

from __future__ import annotations

import glob
import logging
import math
import os
import re

import numpy as np
from pxr import Usd, UsdGeom, UsdLux, UsdRender

from usd_chunks import chunk, chunk_utils

try:
    from PIL import Image, ImageFilter
except ImportError:  # pragma: no cover - installed with the chunk's requirements
    Image = ImageFilter = None
try:
    import OpenEXR
except ImportError:  # pragma: no cover - only needed for an .exr sky
    OpenEXR = None
try:
    import trimesh
    from trimesh.ray import ray_pyembree
except ImportError:  # pragma: no cover - only needed for haze
    trimesh = ray_pyembree = None

logger = logging.getLogger(f"usd_chunks.{__name__}")

_PF = chunk.ChunkFlags.ParamFlags
_GF = chunk.ChunkFlags.GroupFlags.COLLAPSABLE | chunk.ChunkFlags.GroupFlags.LABELED | chunk.ChunkFlags.GroupFlags.FRAMED

_FRAME = re.compile(r"#+")


# ---------------------------------------------------------------------------
# colour
# ---------------------------------------------------------------------------


def srgb_to_linear(x):
    """sRGB (0-1) to linear light."""
    x = np.asarray(x, dtype=np.float64)
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(x):
    """Linear light to sRGB (0-1, not clipped above 1)."""
    x = np.clip(np.asarray(x, dtype=np.float64), 0.0, None)
    return np.where(x <= 0.0031308, 12.92 * x, 1.055 * np.power(x, 1.0 / 2.4) - 0.055)


# ---------------------------------------------------------------------------
# camera
# ---------------------------------------------------------------------------


def camera_rays(camera: UsdGeom.Camera, width: int, height: int):
    """The world-space ray through every pixel centre.

    Args:
        camera: The camera.
        width: Image width in pixels.
        height: Image height in pixels.

    Returns:
        (origin (3,), directions (height, width, 3) unit vectors, camera-to-world 4x4 row-vector
        matrix, the Gf.Camera).
    """
    gf = camera.GetCamera(Usd.TimeCode.Default())
    m = np.asarray(camera.ComputeLocalToWorldTransform(Usd.TimeCode.Default()))
    # The film is fitted to the image's aspect the way a renderer does: width kept.
    h_ap = gf.horizontalAperture
    v_ap = h_ap * height / width
    xs = ((np.arange(width) + 0.5) / width - 0.5) * h_ap
    ys = (0.5 - (np.arange(height) + 0.5) / height) * v_ap
    px, py = np.meshgrid(xs, ys)
    local = np.stack([px, py, -np.full_like(px, gf.focalLength)], axis=-1)
    dirs = local @ m[:3, :3]
    dirs /= np.linalg.norm(dirs, axis=-1, keepdims=True)
    return m[3, :3].copy(), dirs, m, gf


def screen_point(direction, m, gf, width, height):
    """The pixel a world direction from the camera lands on, or None behind the camera."""
    cam = np.asarray(direction, dtype=np.float64) @ np.linalg.inv(m[:3, :3])
    if cam[2] >= 0:
        return None
    h_ap = gf.horizontalAperture
    v_ap = h_ap * height / width
    x = (cam[0] / -cam[2] * gf.focalLength / h_ap + 0.5) * width
    y = (0.5 - cam[1] / -cam[2] * gf.focalLength / v_ap) * height
    return x, y


# ---------------------------------------------------------------------------
# sky
# ---------------------------------------------------------------------------


def read_image(path: str):
    """A rendered image as (linear RGB (H, W, 3) float64, alpha (H, W)).

    An .exr is read as it is, linear and unclipped; anything else as 8-bit sRGB.
    """
    if path.lower().endswith(".exr"):
        if OpenEXR is None:
            raise RuntimeError("reading an .exr needs OpenEXR — pip install OpenEXR")
        with OpenEXR.File(path) as f:
            channels = f.channels()
            if "RGBA" in channels or "RGB" in channels:
                pixels = np.asarray((channels["RGBA"] if "RGBA" in channels else channels["RGB"]).pixels,
                                    dtype=np.float64)
                rgb = pixels[..., :3]
                alpha = pixels[..., 3] if pixels.shape[-1] > 3 else np.ones(rgb.shape[:2])
            else:
                rgb = np.stack([np.asarray(channels[c].pixels, dtype=np.float64) for c in "RGB"], axis=-1)
                alpha = np.asarray(channels["A"].pixels, dtype=np.float64) if "A" in channels else np.ones(rgb.shape[:2])
        return rgb, alpha
    rgba = np.asarray(Image.open(path).convert("RGBA"), dtype=np.float64) / 255.0
    return srgb_to_linear(rgba[..., :3]), rgba[..., 3]


def write_exr(path: str, linear_rgb, alpha) -> None:
    """A linear image as a float RGBA .exr."""
    if OpenEXR is None:
        raise RuntimeError("writing an .exr needs OpenEXR — pip install OpenEXR")
    pixels = np.ascontiguousarray(np.dstack([linear_rgb, alpha]).astype(np.float32))
    header = {"compression": OpenEXR.ZIP_COMPRESSION, "type": OpenEXR.scanlineimage}
    with OpenEXR.File(header, {"RGBA": pixels}) as f:
        f.write(path)


def read_hdri(path: str) -> np.ndarray:
    """An HDR image as (H, W, 3) float64 linear radiance: .exr through OpenEXR, else Pillow."""
    if path.lower().endswith(".exr"):
        if OpenEXR is None:
            raise RuntimeError("reading an .exr sky needs OpenEXR — pip install OpenEXR")
        with OpenEXR.File(path) as f:
            channels = f.channels()
            if "RGBA" in channels or "RGB" in channels:
                pixels = (channels["RGBA"] if "RGBA" in channels else channels["RGB"]).pixels
                return np.asarray(pixels[..., :3], dtype=np.float64)
            return np.stack([np.asarray(channels[c].pixels, dtype=np.float64) for c in "RGB"], axis=-1)
    return srgb_to_linear(np.asarray(Image.open(path).convert("RGB"), dtype=np.float64) / 255.0)


def sky_radiance(dirs, hdri, dome_matrix, turn_deg=0.0):
    """The latlong image's radiance along each direction.

    The image's centre is the dome's +Z and its top the dome's +Y. That is where hdVRay puts
    it, found from a test render of a pole's shadow; *turn_deg* turns the image about the
    dome's +Y for a renderer that places it elsewhere.

    Args:
        dirs: (..., 3) world directions.
        hdri: (H, W, 3) latlong radiance.
        dome_matrix: The dome's 4x4 row-vector local-to-world matrix.
        turn_deg: Extra turn of the image, degrees.

    Returns:
        (..., 3) radiance.
    """
    d = np.asarray(dirs, dtype=np.float64) @ np.linalg.inv(np.asarray(dome_matrix)[:3, :3])
    d /= np.linalg.norm(d, axis=-1, keepdims=True)
    u = 0.5 + np.arctan2(-d[..., 0], d[..., 2]) / (2.0 * np.pi) + turn_deg / 360.0
    v = np.arccos(np.clip(d[..., 1], -1.0, 1.0)) / np.pi
    h, w = hdri.shape[:2]
    return hdri[np.clip((v * h).astype(int), 0, h - 1), (np.floor(u * w).astype(int)) % w]


def fill_sky(linear_rgb, alpha, sky):
    """Put *sky* behind the render where its alpha is below 1."""
    a = np.asarray(alpha, dtype=np.float64)[..., None]
    return linear_rgb * a + sky * (1.0 - a)


# ---------------------------------------------------------------------------
# depth
# ---------------------------------------------------------------------------


#: Sides of a Sphere, Cylinder, Cone or Capsule in the depth pass: coarse is enough for haze.
_SIDES = 16


def _fan(counts, indices):
    """(N, 3) triangle corner indices from face counts and face-vertex indices, by fans."""
    counts = np.asarray(counts)
    indices = np.asarray(indices)
    starts = np.r_[0, np.cumsum(counts)[:-1]]
    tris = [np.stack([indices[starts[counts > k + 1]], indices[starts[counts > k + 1] + k],
                      indices[starts[counts > k + 1] + k + 1]], axis=1)
            for k in range(1, int(counts.max(initial=0)) - 1)]
    return np.concatenate(tris) if tris else np.zeros((0, 3), dtype=np.int64)


def _ring(radius, z, axis):
    """_SIDES points on a circle of *radius* at height z along *axis* ("X", "Y" or "Z")."""
    a = np.linspace(0.0, 2.0 * np.pi, _SIDES, endpoint=False)
    u, v = radius * np.cos(a), radius * np.sin(a)
    w = np.full(_SIDES, float(z))
    return {"X": np.stack([w, u, v], 1), "Y": np.stack([v, w, u], 1), "Z": np.stack([u, v, w], 1)}[axis]


def _local_triangles(prim):
    """A gprim's triangles (N, 3, 3) in its own space: a Mesh's faces, or a coarse Cube,
    Sphere, Cylinder, Cone or Capsule. None for anything else (curves, points, volumes)."""
    if prim.IsA(UsdGeom.Mesh):
        mesh = UsdGeom.Mesh(prim)
        points = mesh.GetPointsAttr().Get()
        counts = mesh.GetFaceVertexCountsAttr().Get()
        if not points or not counts:
            return None
        return np.asarray(points, dtype=np.float64)[_fan(counts, mesh.GetFaceVertexIndicesAttr().Get())]
    if prim.IsA(UsdGeom.Cube):
        h = (UsdGeom.Cube(prim).GetSizeAttr().Get() or 2.0) / 2.0
        c = np.array([[x, y, z] for x in (-h, h) for y in (-h, h) for z in (-h, h)])
        faces = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
        return c[_fan([4] * 6, [i for f in faces for i in f])]
    if prim.IsA(UsdGeom.Sphere):
        r = UsdGeom.Sphere(prim).GetRadiusAttr().Get() or 1.0
        rings = [_ring(r * np.sin(t), r * np.cos(t), "Z") for t in np.linspace(0.0, np.pi, _SIDES // 2 + 1)]
        return _bands(rings)
    for schema in (UsdGeom.Cylinder, UsdGeom.Cone, UsdGeom.Capsule):
        if prim.IsA(schema):
            g = schema(prim)
            r = g.GetRadiusAttr().Get() or 1.0
            h = (g.GetHeightAttr().Get() or 2.0) / 2.0
            axis = g.GetAxisAttr().Get() or "Z"
            if schema is UsdGeom.Capsule:
                h += r  # its round ends as flat caps: close enough for haze
            top = _ring(0.0 if schema is UsdGeom.Cone else r, h, axis)
            bottom = _ring(r, -h, axis)
            return np.concatenate([_bands([top, bottom]), _cap(top), _cap(bottom)])
    return None


def _bands(rings):
    """Triangles joining each ring of points to the next."""
    tris = []
    for a, b in zip(rings[:-1], rings[1:]):
        a2, b2 = np.roll(a, -1, 0), np.roll(b, -1, 0)
        tris += [np.stack([a, b, b2], 1), np.stack([a, b2, a2], 1)]
    return np.concatenate(tris)


def _cap(ring):
    """A fan closing one ring."""
    centre = np.broadcast_to(ring.mean(0), ring.shape)
    return np.stack([centre, ring, np.roll(ring, -1, 0)], 1)


def _hidden(prim) -> bool:
    imageable = UsdGeom.Imageable(prim)
    return bool(imageable) and (imageable.ComputeVisibility() == UsdGeom.Tokens.invisible
                                or imageable.ComputePurpose() in (UsdGeom.Tokens.guide, UsdGeom.Tokens.proxy))


def _apply(local, xf):
    """(N, 3, 3) local triangles through a 4x4 row-vector matrix, as float32."""
    flat = local.reshape(-1, 3)
    return (flat @ xf[:3, :3] + xf[3, :3]).reshape(-1, 3, 3).astype(np.float32)


def _collect(stage, root, cache, budget, relative_to=None, instancers=True, depth=0):
    """Triangles of every visible render gprim under root, PointInstancers expanded (nested
    ones too), in world space -- or in the space of *relative_to*, for a prototype.

    Args:
        stage: The stage.
        root: The prim to start from.
        cache: A UsdGeom.XformCache.
        budget: [triangles left]; collection stops when it runs out.
        relative_to: The prim whose space to collect in, or None for world space.
        instancers: Expand PointInstancers.
        depth: How deep inside instancers this is (nesting stops at 4).

    Returns:
        A list of (N, 3, 3) float32 arrays.
    """
    to_space = np.linalg.inv(np.asarray(cache.GetLocalToWorldTransform(relative_to))) if relative_to else None
    parts = []
    it = iter(Usd.PrimRange(root, Usd.TraverseInstanceProxies()))
    for prim in it:
        if budget[0] <= 0:
            break
        if _hidden(prim):
            it.PruneChildren()
            continue
        xf = np.asarray(cache.GetLocalToWorldTransform(prim))
        if to_space is not None:
            xf = xf @ to_space
        if prim.IsA(UsdGeom.PointInstancer):
            it.PruneChildren()
            if instancers and depth < 4:
                parts += _instanced(stage, prim, cache, budget, xf, depth)
            continue
        local = _local_triangles(prim)
        if local is not None and len(local):
            parts.append(_apply(local, xf))
            budget[0] -= len(local)
    return parts


def _instanced(stage, prim, cache, budget, parent, depth):
    """The copies of an instancer's prototypes, each prototype's copies in one array call."""
    inst = UsdGeom.PointInstancer(prim)
    mats = inst.ComputeInstanceTransformsAtTime(Usd.TimeCode.Default(), Usd.TimeCode.Default())
    ids = inst.GetProtoIndicesAttr().Get()
    if mats is None or ids is None or not len(ids):
        return []
    mats = np.asarray(mats, dtype=np.float64) @ parent
    ids = np.asarray(ids)
    parts = []
    for i, path in enumerate(inst.GetPrototypesRel().GetTargets()):
        chosen = np.flatnonzero(ids == i)
        proto = stage.GetPrimAtPath(path)
        if not len(chosen) or not proto or budget[0] <= 0:
            continue
        local = _collect(stage, proto, cache, [budget[0]], proto, True, depth + 1)
        if not local:
            continue
        tris = np.concatenate(local).reshape(-1, 3).astype(np.float64)
        chosen = chosen[: max(1, budget[0] // max(1, len(tris) // 3))]
        world = np.einsum("pj,kji->kpi", np.c_[tris, np.ones(len(tris))], mats[chosen])[..., :3]
        parts.append(world.reshape(-1, 3, 3).astype(np.float32))
        budget[0] -= len(chosen) * (len(tris) // 3)
    return parts


def depth_triangles(stage, roots, instancers=True, limit=40_000_000):
    """Every triangle the depth pass can hit under *roots*.

    Meshes, and Cube, Sphere, Cylinder, Cone and Capsule as coarse shapes; PointInstancers
    expanded, nested ones too. Curves, points and cut-out (alpha) cards are not in it.

    Args:
        stage: The stage.
        roots: Prim paths.
        instancers: Expand PointInstancers. Their copies are often the bulk of a scene's
            triangles; small ones (grass, ivy) sit on surfaces close enough for haze anyway.
        limit: Stop collecting at about this many triangles.

    Returns:
        ((N, 3, 3) float32 world triangles, whether the limit cut it short).
    """
    cache = UsdGeom.XformCache()
    budget = [int(limit)]
    parts = []
    for root in roots:
        prim = stage.GetPrimAtPath(root)
        if prim and prim.IsValid():
            parts += _collect(stage, prim, cache, budget, None, instancers)
    tris = np.concatenate(parts) if parts else np.zeros((0, 3, 3), dtype=np.float32)
    return tris, budget[0] <= 0


def ray_depth(triangles, origin, dirs):
    """Distance along each ray to the first triangle it hits (inf for a miss), with embree."""
    if trimesh is None:
        raise RuntimeError("haze needs trimesh and embreex — pip install trimesh embreex")
    shape = dirs.shape[:-1]
    depth = np.full(int(np.prod(shape)), np.inf)
    if not len(triangles):
        return depth.reshape(shape)
    mesh = trimesh.Trimesh(vertices=triangles.reshape(-1, 3),
                           faces=np.arange(len(triangles) * 3).reshape(-1, 3), process=False)
    caster = ray_pyembree.RayMeshIntersector(mesh)
    d = dirs.reshape(-1, 3)
    o = np.repeat(np.asarray(origin, dtype=np.float64)[None], len(d), axis=0)
    hits, rays, _ = caster.intersects_location(o, d, multiple_hits=False)
    depth[rays] = np.linalg.norm(hits - origin, axis=1)
    return depth.reshape(shape)


# ---------------------------------------------------------------------------
# haze, light, grade
# ---------------------------------------------------------------------------


def add_haze(linear_rgb, depth, dirs, up, to_sun, distance, color, level, sun_glow):
    """Distance haze: Beer-Lambert by depth, and for the sky a haze that thins with height.

    Args:
        linear_rgb: (H, W, 3) linear image.
        depth: (H, W) metres, inf where nothing was hit.
        dirs: (H, W, 3) camera ray directions.
        up: (3,) the stage's up axis.
        to_sun: (3,) unit direction toward the sun, or None.
        distance: Metres at which haze reaches 63%.
        color: (3,) linear haze colour.
        level: Haze brightness.
        sun_glow: How much brighter the haze gets toward the sun.

    Returns:
        The hazed image.
    """
    finite = np.isfinite(depth)
    f = np.where(finite, 1.0 - np.exp(-np.where(finite, depth, 0.0) / max(distance, 1e-6)), 0.0)
    elevation = np.arcsin(np.clip(dirs @ np.asarray(up, dtype=np.float64), -1.0, 1.0))
    f = np.where(finite, f, 0.6 * np.exp(-np.clip(elevation, 0.0, None) / math.radians(7.0)))
    glow = np.ones(depth.shape)
    if to_sun is not None:
        cosang = np.clip(dirs @ to_sun, 0.0, 1.0)
        glow = glow + sun_glow * cosang ** 24 + sun_glow * 3.0 * cosang ** 400
    haze = np.asarray(color, dtype=np.float64) * level * glow[..., None]
    return linear_rgb * (1.0 - f[..., None]) + haze * f[..., None]


def add_rays(linear_rgb, dirs, to_sun, sun_xy, strength, color, steps=48):
    """Light shafts: bright pixels near the sun, smeared outward from the sun's spot on screen."""
    if sun_xy is None or strength <= 0 or to_sun is None:
        return linear_rgb
    h, w = linear_rgb.shape[:2]
    sx, sy = sun_xy
    cosang = np.clip(dirs @ to_sun, 0.0, 1.0)
    src = np.clip(linear_rgb - 0.6, 0.0, None) * (cosang[..., None] ** 30)
    acc = np.zeros_like(src)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    for k in range(steps):
        t = k / steps * 0.85
        px = np.clip((xx + (sx - xx) * t).astype(int), 0, w - 1)
        py = np.clip((yy + (sy - yy) * t).astype(int), 0, h - 1)
        acc += src[py, px] * (1.0 - t)
    return linear_rgb + strength * acc / steps * np.asarray(color, dtype=np.float64)


def add_bloom(linear_rgb, strength):
    """The brightest parts bleed into their surroundings, at three widths."""
    if strength <= 0:
        return linear_rgb
    w = linear_rgb.shape[1]
    bright = np.clip(linear_rgb - 0.9, 0.0, None)
    img = Image.fromarray((np.clip(bright / 4.0, 0.0, 1.0) * 255).astype(np.uint8))
    blur = np.zeros_like(linear_rgb)
    for radius, weight in ((w * 0.01, 0.5), (w * 0.04, 0.35), (w * 0.12, 0.25)):
        blur += np.asarray(img.filter(ImageFilter.GaussianBlur(radius)), dtype=np.float64) / 255.0 * 4.0 * weight
    return linear_rgb + strength * blur


def add_sun_disc(linear_rgb, sun_xy, sky_mask, strength, color=(1.0, 0.72, 0.38)):
    """The sun's disc, a wide glow and a faint starburst at its spot on screen.

    The disc and starburst only show where *sky_mask* says the sky is visible -- a building in
    front hides the sun -- the glow everywhere around it.
    """
    if sun_xy is None or strength <= 0:
        return linear_rgb
    h, w = linear_rgb.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    dx, dy = xx - sun_xy[0], yy - sun_xy[1]
    r = np.hypot(dx, dy) / w
    angle = np.arctan2(dy, dx)
    disc = np.clip(1.0 - r / 0.0045, 0.0, 1.0) ** 0.5 * 6.0
    glow = np.exp(-r / 0.02) * 1.2 + np.exp(-r / 0.08) * 0.25
    star = np.exp(-(np.sin(3.0 * (angle - math.radians(12.0))) ** 2) / 0.0015) * np.exp(-r / 0.05) * 0.25
    mask = Image.fromarray((np.asarray(sky_mask, dtype=np.float64) * 255).astype(np.uint8))
    sky = np.asarray(mask.filter(ImageFilter.GaussianBlur(w * 0.004)), dtype=np.float64) / 255.0
    sun = ((disc + star) * sky + glow) * strength
    return linear_rgb + sun[..., None] * np.asarray(color, dtype=np.float64)


def grade(linear_rgb, exposure=1.0, balance=(1.0, 1.0, 1.0), contrast=1.0, saturation=1.0, lift=0.0):
    """A plain grade, returning display (sRGB) values in 0-1.

    Args:
        linear_rgb: (H, W, 3) linear image.
        exposure: Multiplier on the light.
        balance: Per-channel gain in linear light (a warm grade lowers blue).
        contrast: Around mid-grey, in display space.
        saturation: 1 leaves colour as it is, 0 is grey.
        lift: A warm lift in the shadows, in display space.

    Returns:
        (H, W, 3) sRGB values in 0-1.
    """
    lin = linear_rgb * np.asarray(balance, dtype=np.float64) * exposure
    out = linear_to_srgb(lin / (1.0 + lin * 0.18))  # a soft shoulder, so highlights roll off
    out = (out - 0.45) * contrast + 0.45
    out = out + lift * (1.0 - np.clip(out, 0.0, 1.0)) ** 2 * np.array([1.0, 0.8, 0.55])
    grey = out @ np.array([0.2126, 0.7152, 0.0722])
    out = grey[..., None] + (out - grey[..., None]) * saturation
    return np.clip(out, 0.0, 1.0)


# ---------------------------------------------------------------------------
# the chunk
# ---------------------------------------------------------------------------


def _first(stage, schema):
    return next((p for p in stage.Traverse() if p.IsA(schema)), None)


def _prim_or_first(stage, path, schema):
    prim = stage.GetPrimAtPath(path) if path else None
    if prim and prim.IsValid() and prim.IsA(schema):
        return prim
    return _first(stage, schema)


def _dome_image(light: UsdLux.DomeLight) -> str:
    asset = light.GetTextureFileAttr().Get()
    return (asset.resolvedPath or asset.path) if asset else ""


def _dome(stage, path):
    """The Dome Light named, else the first whose image exists.

    Not simply the first: an imported asset can bring a dome of its own (a car exported from
    a DCC with its studio world), and the first one found was one of those, its image missing.
    """
    prim = stage.GetPrimAtPath(path) if path else None
    if prim and prim.IsValid() and prim.IsA(UsdLux.DomeLight):
        return prim
    domes = [p for p in stage.Traverse() if p.IsA(UsdLux.DomeLight)]
    return next((p for p in domes if os.path.exists(_dome_image(UsdLux.DomeLight(p)))), domes[0] if domes else None)


def _camera(node, stage):
    """The chunk's camera, else the stage's render settings camera, else the first camera."""
    path = (node.get_param("camera").value or "").strip()
    prim = stage.GetPrimAtPath(path) if path else None
    if prim and prim.IsA(UsdGeom.Camera):
        return UsdGeom.Camera(prim)
    settings_path = stage.GetMetadata("renderSettingsPrimPath") if stage.HasAuthoredMetadata("renderSettingsPrimPath") else ""
    settings = stage.GetPrimAtPath(settings_path) if settings_path else None
    if settings and settings.IsA(UsdRender.Settings):
        targets = UsdRender.Settings(settings).GetCameraRel().GetTargets()
        if targets and (cam := stage.GetPrimAtPath(targets[0])) and cam.IsA(UsdGeom.Camera):
            return UsdGeom.Camera(cam)
    prim = _first(stage, UsdGeom.Camera)
    return UsdGeom.Camera(prim) if prim else None


def _upstream_image(node):
    """The Output Image of the chunk wired into this one, if it has one (a Render chunk)."""
    for _key, dep in node.get_dependencies():
        param = dep.get_param("output_image") if hasattr(dep, "get_param") else None
        if param is not None and param.value:
            return param.value
    return ""


def _sources(node):
    """The rendered images to finish: the Source Image, or the Render upstream's, frames expanded."""
    text = (node.get_param("source_image").value or "").strip() or _upstream_image(node)
    if not text:
        return []
    text = chunk_utils.resolve_output_path(text, node.get_stage(), logger)
    if _FRAME.search(text):
        return sorted(glob.glob(_FRAME.sub(lambda m: "[0-9]" * len(m.group()), text)))
    return [text] if os.path.exists(text) else []


def _output_for(node, source):
    text = (node.get_param("output_image").value or "").strip()
    if not text:
        stem, _ext = os.path.splitext(source)
        return stem + "_finished.png"
    if _FRAME.search(text):
        frame = re.findall(r"(\d+)(?=\.[^.]+$)", os.path.basename(source))
        return _FRAME.sub(lambda m: frame[-1].zfill(len(m.group())) if frame else m.group(), text)
    return text


def _paths(text):
    return [p.strip() for p in re.split(r"[,\s]+", text or "") if p.strip()]


class EmptyRender(RuntimeError):
    """The source image has nothing in it: every pixel is transparent."""


def finish(node, source: str, output: str) -> str:
    """Finish one image. Returns a one-line summary of what was done."""
    stage = node.get_stage()
    v = lambda name: node.get_param(name).value  # noqa: E731
    lin, alpha = read_image(source)
    if not alpha.any():
        raise EmptyRender("every pixel is transparent, so the render probably failed (no licence?)")
    h, w = lin.shape[:2]
    done = []
    camera = _camera(node, stage)
    if camera is None:
        raise RuntimeError("no camera to finish from")
    origin, dirs, m, gf = camera_rays(camera, w, h)
    up = np.array([0.0, 1.0, 0.0]) if UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.y else np.array([0.0, 0.0, 1.0])

    sun_prim = _prim_or_first(stage, (v("sun") or "").strip(), UsdLux.DistantLight)
    to_sun = sun_xy = None
    if sun_prim is not None:
        sm = np.asarray(UsdGeom.Xformable(sun_prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default()))
        to_sun = np.array([0.0, 0.0, 1.0]) @ sm[:3, :3]  # a distant light shines down its -Z
        to_sun /= np.linalg.norm(to_sun)
        sun_xy = screen_point(to_sun, m, gf, w, h)

    if v("sky_enabled"):
        dome = _dome(stage, (v("dome") or "").strip())
        backplate = (v("backplate") or "").strip()
        if dome is not None or backplate:
            light = UsdLux.DomeLight(dome) if dome is not None else None
            image = backplate
            if not image and light is not None:
                image = _dome_image(light)
            if image and os.path.exists(image):
                intensity = 1.0
                dome_matrix = np.eye(4)
                if light is not None:
                    intensity = light.GetIntensityAttr().Get() * 2.0 ** (light.GetExposureAttr().Get() or 0.0)
                    dome_matrix = np.asarray(UsdGeom.Xformable(dome).ComputeLocalToWorldTransform(Usd.TimeCode.Default()))
                sky = sky_radiance(dirs, read_hdri(image), dome_matrix, v("sky_turn")) * intensity * v("sky_gain")
                lin = fill_sky(lin, alpha, sky)
                done.append(f"sky {100 * (alpha < 0.5).mean():.0f}%")
            else:
                done.append("no sky image found")

    depth = None
    if v("haze_enabled"):
        roots = _paths(v("depth_from")) or [str(stage.GetDefaultPrim().GetPath()) if stage.GetDefaultPrim() else "/"]
        tris, capped = depth_triangles(stage, roots, v("depth_instancers"))
        depth = ray_depth(tris, origin, dirs)
        if v("ground_below"):
            # Rays that miss but point down meet a ground at Ground Height.
            down = ~np.isfinite(depth) & (dirs @ up < -1e-4)
            depth[down] = (float(origin @ up) - v("ground_height")) / -(dirs @ up)[down]
        # Depth is in stage units; the haze distance is in metres.
        depth = depth * UsdGeom.GetStageMetersPerUnit(stage)
        lin = add_haze(lin, depth, dirs, up, to_sun, v("haze_distance"), srgb_to_linear(v("haze_color")),
                       v("haze_level"), v("sun_glow"))
        hit = np.isfinite(depth)
        done.append((f"haze ({len(tris):,} triangles{', capped' if capped else ''}, median {np.median(depth[hit]):.0f} m)")
                    if hit.any() else "haze")

    if v("rays") > 0:
        lin = add_rays(lin, dirs, to_sun, sun_xy, v("rays"), srgb_to_linear(v("haze_color")))
    lin = add_bloom(lin, v("bloom"))
    if v("sun_disc") > 0 and sun_xy is not None:
        sky_mask = ~np.isfinite(depth) if depth is not None else alpha < 0.5
        lin = add_sun_disc(lin, sun_xy, sky_mask, v("sun_disc"))
    output = chunk_utils.cwd_relative(output)
    os.makedirs(os.path.dirname(os.path.abspath(output)) or ".", exist_ok=True)
    if output.lower().endswith(".exr"):
        # Linear, for a comp: exposure and balance are light; contrast, saturation and the lift
        # are display steps, left to whatever views it.
        write_exr(output, lin * np.asarray(v("balance"), dtype=np.float64) * v("exposure"), alpha)
    else:
        out = grade(lin, v("exposure"), v("balance"), v("contrast"), v("saturation"), v("lift"))
        Image.fromarray((out * 255 + 0.5).astype(np.uint8)).save(output)
    return ", ".join(done) or "graded"


def run(node) -> None:
    """Finish every source image, saying how it went in the readout."""
    readout = node.get_param("readout")
    if Image is None:
        readout.value = "needs Pillow — pip install pillow"
        return
    sources = _sources(node)
    if not sources:
        readout.value = "no rendered image to finish yet"
        return
    last = ""
    for source in sources:
        output = _output_for(node, source)
        try:
            last = f"{os.path.basename(output)}: {finish(node, source, output)}"
        except EmptyRender as e:
            logger.error("Finish Frame: %s is empty: %s", source, e)
            last = f"{os.path.basename(source)} is empty: {e}"
        except Exception as e:  # noqa: BLE001 - one bad frame says so and the rest still run
            logger.exception("Finish Frame: %s failed", source)
            last = f"{os.path.basename(source)} failed: {e}"
    readout.value = last if len(sources) == 1 else f"{len(sources)} frames · {last}"
    node.get_param("output").value = _output_for(node, sources[-1])


def _finish_clicked(owner, notice, sender):
    node = owner.get_chunk() if hasattr(owner, "get_chunk") else owner
    run(node)


def execute(node):
    """The cook authors nothing: finishing happens in commit(), after the render."""
    return


def commit(node):
    if node.get_param("finish_on_commit").value:
        run(node)


def main():
    chunk_api = chunk.Chunk("Finish Frame")
    chunk_api.add_params([
        chunk.Label("readout", "not finished yet", default_value="not finished yet",
                    tooltip="What the last run did."),
        chunk.LineEdit("source_image", "Source Image", flags=_PF.LABELED,
                       tooltip="The rendered image. Empty: the Output Image of the Render chunk\n"
                       "wired into this one. #### stands for the frame number. An .exr is read\n"
                       "linear and unclipped; other formats as 8-bit sRGB."),
        chunk.LineEdit("output_image", "Output Image", flags=_PF.LABELED,
                       tooltip="Where the finished image goes. Empty: beside the source, named\n"
                       "<name>_finished.png. An .exr is written linear and unclipped, with\n"
                       "exposure and balance applied and the display steps (contrast,\n"
                       "saturation, lift) left out."),
        chunk.LineEdit("camera", "Camera", flags=_PF.LABELED | _PF.PRIM_SELECTION,
                       tooltip="The camera the image was rendered from. Empty: the stage's Render\n"
                       "Settings camera, else the first camera."),
    ])

    sky = chunk.Group("Sky", icon="sun", flags=_GF)
    sky.add_params([
        chunk.Checkbox("sky_enabled", "Fill Sky", default_value=True,
                       tooltip="Put the dome image behind the render where it is transparent. Some\n"
                       "renderers (hdVRay) leave the dome out of the camera background."),
        chunk.LineEdit("dome", "Dome Light", flags=_PF.LABELED | _PF.PRIM_SELECTION,
                       tooltip="Empty: the first Dome Light on the stage."),
        chunk.FileBrowser("backplate", extensions=[".exr", ".hdr", ".png", ".jpg"],
                          tooltip="Show this image instead of the dome's. Empty: the dome's own."),
        chunk.Slider("sky_turn", "Turn", min_value=-180.0, max_value=180.0, default_value=0.0,
                     tooltip="Turn the sky image about the dome's up axis, degrees. The image is laid\n"
                     "out as hdVRay lays it (centre toward the dome's +Z, checked with a test\n"
                     "render); other renderers were not checked and may need a turn."),
        chunk.Slider("sky_gain", "Brightness", min_value=0.0, max_value=4.0, default_value=1.0,
                     tooltip="How bright the camera sees the sky, against the dome's intensity."),
    ])
    haze = chunk.Group("Haze", icon="waves", flags=_GF)
    haze.add_params([
        chunk.Checkbox("haze_enabled", "Haze", default_value=True,
                       tooltip="Distance haze from a depth pass ray-cast through the camera."),
        chunk.LineEdit("depth_from", "Depth From", flags=_PF.LABELED | _PF.PRIM_SELECTION,
                       tooltip="Prims the depth pass hits, separated by spaces or commas. Empty: the\n"
                       "stage's default prim, else everything. The big geometry is enough: small\n"
                       "things sit on surfaces close enough for haze. Meshes and the simple shapes\n"
                       "are hit; curves, points and cut-out (alpha) cards are not."),
        chunk.Checkbox("depth_instancers", "Include Instancers", default_value=True,
                       tooltip="Expand Point Instancers for the depth pass. Off: faster, and the\n"
                       "copies are left out."),
        chunk.Checkbox("ground_below", "Ground Below", default_value=True,
                       tooltip="A ray that hits nothing but points down meets a flat ground at\n"
                       "Ground Height. Off: such rays count as sky."),
        chunk.Spinbox("ground_height", "Ground Height", default_value=0.0,
                      tooltip="Height of that ground along the stage's up axis, in stage units."),
        chunk.Slider("haze_distance", "Distance", min_value=1.0, max_value=5000.0, default_value=420.0,
                     tooltip="Metres at which the haze covers about two thirds of what is behind it."),
        chunk.ColorPicker("haze_color", "Colour", default_value=(1.0, 0.78, 0.55),
                          tooltip="The haze colour (also tints the light shafts)."),
        chunk.Slider("haze_level", "Brightness", min_value=0.0, max_value=4.0, default_value=0.6),
        chunk.Slider("sun_glow", "Sun Glow", min_value=0.0, max_value=8.0, default_value=1.2,
                     tooltip="How much brighter the haze gets looking toward the sun."),
    ])
    sun = chunk.Group("Sun", icon="sun", flags=_GF)
    sun.add_params([
        chunk.LineEdit("sun", "Sun", flags=_PF.LABELED | _PF.PRIM_SELECTION,
                       tooltip="The Distant Light that is the sun. Empty: the first one on the stage."),
        chunk.Slider("rays", "Light Shafts", min_value=0.0, max_value=4.0, default_value=1.2),
        chunk.Slider("bloom", "Bloom", min_value=0.0, max_value=2.0, default_value=0.2),
        chunk.Slider("sun_disc", "Sun Disc", min_value=0.0, max_value=4.0, default_value=1.0,
                     tooltip="Paint the sun's disc and glow, where the sky shows. A distant light\n"
                     "itself is not visible to the camera."),
    ])
    look = chunk.Group("Grade", icon="sliders", flags=_GF)
    look.add_params([
        chunk.Slider("exposure", "Exposure", min_value=0.0, max_value=8.0, default_value=1.0),
        chunk.ColorPicker("balance", "Balance", default_value=(1.0, 1.0, 1.0),
                          tooltip="A gain per channel on the light. Lower blue for a warmer grade."),
        chunk.Slider("contrast", "Contrast", min_value=0.0, max_value=3.0, default_value=1.0),
        chunk.Slider("saturation", "Saturation", min_value=0.0, max_value=3.0, default_value=1.0),
        chunk.Slider("lift", "Shadow Lift", min_value=0.0, max_value=0.3, default_value=0.0),
    ])
    chunk_api.add_params([sky, haze, sun, look])
    chunk_api.add_param(chunk.Checkbox("finish_on_commit", "Finish on Commit", default_value=True,
                                       tooltip="Finish after the render, whenever the network commits."))
    chunk_api.add_param(chunk.LineEdit("output", "Output", flags=_PF.OUTPUT | _PF.LABELED))
    button = chunk.Button("finish_now", "Finish Now")
    button.add_callback(_finish_clicked)
    chunk_api.add_param(button)
    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD_RENDER,
        icon="image",
        requirements=["numpy>=2,<3", "pillow>=10", "OpenEXR>=3", "trimesh>=4", "embreex>=2"],
    )
