"""KITTI Writer chunk -- turns rendered frames into a training dataset: KITTI object labels,
calibration, class and instance masks and depth, plus the same data laid out the way
NVIDIA Replicator's BasicWriter writes it.

The render supplies pixels; the stage supplies everything the pixels cannot say. Each
frame's layered EXR (written by a Render chunk whose Render Settings name four Render
Vars) carries

    beauty    the image                                   -> image_2/
    P         world position per pixel                     -> depth/, distance_*
    semantic  the semantic_color primvar, as rendered      -> semantic_primvar/
    instance  the instance_color primvar, as rendered      -> instance/, semantic/, boxes

and the stage at that frame gives the camera (for intrinsics, and to turn world positions
into depth) and each instance's geometry (for 3D boxes, and for the silhouette an
unoccluded object would have, which is what truncation and occlusion are measured against).

**Masks are decoded, not thresholded.** The renderer anti-aliases every AOV, so a pixel on
the edge between two objects holds a blend of their two colours -- and a blend of two ids
would be a third, wrong, class. Interior pixels match an instance colour exactly; an edge
pixel is explained as a mix of the instance colours around it and given to whichever
contributes more. Every pixel ends up with exactly one real label.

Requires: pxr (OpenUSD), usd_chunks, numpy, OpenEXR, Pillow.
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import re

import numpy as np
import OpenEXR
from PIL import Image, ImageDraw
from pxr import CameraUtil, Gf, Sdf, Usd, UsdGeom, UsdRender

from usd_chunks import chunk, chunk_utils

logger = logging.getLogger(f"usd_chunks.{__name__}")

_FRAME_PLACEHOLDER = re.compile(r"#+")

#: UsdRender's aspectRatioConformPolicy, as the CameraUtil policy Hydra conforms with.
_CONFORM = {
    "expandAperture": CameraUtil.Fit,
    "cropAperture": CameraUtil.Crop,
    "adjustApertureWidth": CameraUtil.MatchVertically,
    "adjustApertureHeight": CameraUtil.MatchHorizontally,
    "adjustPixelAspectRatio": CameraUtil.DontConform,
}

#: A pixel within this of an instance colour, on every channel, is that instance outright.
_EXACT = 0.02

#: Silhouettes are rasterised at this many samples per pixel edge, so that "inside" means
#: what the renderer's pixel coverage means rather than whatever a polygon fill touches.
_SUPERSAMPLE = 4

_BACKGROUND = "BACKGROUND"

#: Replicator's structured array layouts.
_BBOX2D_DTYPE = np.dtype([
    ("semanticId", "<u4"), ("x_min", "<i4"), ("y_min", "<i4"),
    ("x_max", "<i4"), ("y_max", "<i4"), ("occlusionRatio", "<f4"),
])
_BBOX3D_DTYPE = np.dtype([
    ("semanticId", "<u4"), ("x_min", "<f4"), ("y_min", "<f4"), ("z_min", "<f4"),
    ("x_max", "<f4"), ("y_max", "<f4"), ("z_max", "<f4"),
    ("transform", "<f4", (4, 4)), ("occlusionRatio", "<f4"),
])


# --------------------------------------------------------------------------------------
# Inputs


def _split(text):
    return [t.strip() for t in (text or "").split(",") if t.strip()]


def _mapping(text):
    """'cube=Cube, sphere=Ball' -> {'cube': 'Cube', 'sphere': 'Ball'}."""
    pairs = (entry.split("=", 1) for entry in _split(text) if "=" in entry)
    return {k.strip(): v.strip() for k, v in pairs if k.strip() and v.strip()}


def frame_path(pattern, frame):
    """The file a frame was rendered to: a {frame} field or a run of hashes, padded to the
    run's width -- the Render chunk's spelling, so the two agree on every name."""
    number = int(round(frame))
    path = pattern.replace("{frame}", str(number))
    path = _FRAME_PLACEHOLDER.sub(lambda m: str(number).zfill(len(m.group(0))), path)
    return _cwd_relative(path)


def _cwd_relative(path):
    """Relative paths resolve against the working directory, as the Render chunk's do."""
    if not path or os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(os.getcwd(), path))


def read_layers(path):
    """Every layer of a layered EXR, as float32 arrays of shape (H, W, C)."""
    layers = {}
    with OpenEXR.File(path) as exr:
        for name, channel in exr.channels().items():
            pixels = np.asarray(channel.pixels, dtype=np.float32)
            layers[name] = pixels if pixels.ndim == 3 else pixels[..., None]
    return layers


def _layer(layers, name):
    """A named layer. OpenEXR groups a layer's channels under its name ("P"), and names a
    one-channel layer by its channel ("depth.R"); the beauty is the bare RGBA."""
    if name in layers:
        return layers[name]
    for key, pixels in layers.items():
        if key.startswith(name + "."):
            return pixels
    if name.upper() in ("RGBA", "RGB", "BEAUTY", "COLOR"):
        for key in ("RGBA", "RGB"):
            if key in layers:
                return layers[key]
    raise KeyError(f"no layer {name!r} (have {sorted(layers)})")


# --------------------------------------------------------------------------------------
# Camera


class Camera:
    """A stage camera at one frame, as the pinhole the renderer used.

    The aperture is conformed to the image aspect with the same policy Hydra applies, so
    that the intrinsics describe the pixels actually rendered, not the camera's nominal
    film back. KITTI's camera frame is x right, y down, z forward; USD's is x right, y up,
    looking down -z. Everything metric is in metres, via the stage's metersPerUnit.
    """

    def __init__(self, stage, path, time, width, height, policy="expandAperture"):
        prim = stage.GetPrimAtPath(path)
        if not prim or not prim.IsA(UsdGeom.Camera):
            raise ValueError(f"no camera at {path}")
        self.width, self.height = width, height
        self.usd = UsdGeom.Camera(prim)
        gf_camera = self.usd.GetCamera(time)
        CameraUtil.ConformWindow(gf_camera, _CONFORM.get(policy, CameraUtil.Fit), width / height)
        self.gf = gf_camera
        frustum = gf_camera.frustum
        self.near, self.far = frustum.nearFar.min, frustum.nearFar.max
        self.meters = UsdGeom.GetStageMetersPerUnit(stage)

        projection = frustum.ComputeProjectionMatrix()
        self.projection = projection
        # Row-vector clip = p * M, so the x terms sit in column 0 and y in column 1.
        # Pixel centres are integers (OpenCV/KITTI), hence the half pixel.
        self.fx = projection[0][0] * width / 2.0
        self.fy = projection[1][1] * height / 2.0
        self.cx = (1.0 - projection[2][0]) * width / 2.0 - 0.5
        self.cy = (1.0 + projection[2][1]) * height / 2.0 - 0.5

        self.view = frustum.ComputeViewMatrix()  # world -> USD camera, row-vector
        flip = np.diag([1.0, -1.0, -1.0, 1.0])
        # World (scene units) -> KITTI camera (metres), column-vector.
        self.world_to_cam = flip @ np.array(self.view, dtype=np.float64).T
        self.world_to_cam[:3, :] *= self.meters
        self.eye = np.array(frustum.position, dtype=np.float64)

    @property
    def K(self):
        return np.array([[self.fx, 0.0, self.cx], [0.0, self.fy, self.cy], [0.0, 0.0, 1.0]])

    def to_camera(self, world_points):
        """(N, 3) world points -> (N, 3) KITTI camera coordinates in metres."""
        homogeneous = np.c_[world_points, np.ones(len(world_points))]
        return (homogeneous @ self.world_to_cam.T)[:, :3]

    def project(self, cam_points):
        """(N, 3) KITTI camera points -> (N, 2) pixel coordinates (centres at integers)."""
        z = cam_points[:, 2]
        return np.c_[self.fx * cam_points[:, 0] / z + self.cx, self.fy * cam_points[:, 1] / z + self.cy]


# --------------------------------------------------------------------------------------
# Geometry


def _unit_sphere(rings=16, segments=32):
    points = [(0.0, 1.0, 0.0), (0.0, -1.0, 0.0)]
    for i in range(1, rings):
        theta = math.pi * i / rings
        for j in range(segments):
            phi = 2.0 * math.pi * j / segments
            points.append((math.sin(theta) * math.cos(phi), math.cos(theta), math.sin(theta) * math.sin(phi)))
    ring = lambda i, j: 2 + (i - 1) * segments + (j % segments)  # noqa: E731
    triangles = []
    for j in range(segments):
        triangles.append((0, ring(1, j), ring(1, j + 1)))
        triangles.append((1, ring(rings - 1, j + 1), ring(rings - 1, j)))
    for i in range(1, rings - 1):
        for j in range(segments):
            a, b, c, d = ring(i, j), ring(i, j + 1), ring(i + 1, j + 1), ring(i + 1, j)
            triangles += [(a, b, c), (a, c, d)]
    return np.array(points, dtype=np.float64), np.array(triangles, dtype=np.int64)


_SPHERE = _unit_sphere()
_CUBE = (
    np.array([(x, y, z) for x in (-0.5, 0.5) for y in (-0.5, 0.5) for z in (-0.5, 0.5)], dtype=np.float64),
    np.array([(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
              (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)], dtype=np.int64),
)


def gprim_triangles(prim, time):
    """(points, triangles) of a gprim in its local space, or None for a shape without a
    surface worth rasterising. Implicit shapes are tessellated; anything else falls back to
    its extent's box, which over-covers but never under-covers."""
    if prim.IsA(UsdGeom.Mesh):
        mesh = UsdGeom.Mesh(prim)
        points = np.array(mesh.GetPointsAttr().Get(time) or [], dtype=np.float64)
        counts = mesh.GetFaceVertexCountsAttr().Get(time) or []
        indices = mesh.GetFaceVertexIndicesAttr().Get(time) or []
        triangles, start = [], 0
        for count in counts:
            for k in range(1, count - 1):
                triangles.append((indices[start], indices[start + k], indices[start + k + 1]))
            start += count
        return points, np.array(triangles, dtype=np.int64).reshape(-1, 3)
    if prim.IsA(UsdGeom.Sphere):
        radius = UsdGeom.Sphere(prim).GetRadiusAttr().Get(time) or 1.0
        return _SPHERE[0] * radius, _SPHERE[1]
    if prim.IsA(UsdGeom.Cube):
        size = UsdGeom.Cube(prim).GetSizeAttr().Get(time) or 2.0
        return _CUBE[0] * size, _CUBE[1]

    extent = UsdGeom.Boundable(prim).GetExtentAttr().Get(time) if prim.IsA(UsdGeom.Boundable) else None
    if not extent:
        return None
    low, high = np.array(extent[0], dtype=np.float64), np.array(extent[1], dtype=np.float64)
    return low + (_CUBE[0] + 0.5) * (high - low), _CUBE[1]


def _to_world(points, prim, time):
    matrix = np.array(UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(time), dtype=np.float64)
    return (np.c_[points, np.ones(len(points))] @ matrix)[:, :3]


def _rasterise(pixels_2d, triangles, depth_ok, width, height, offset, scale):
    """Fill the triangles whose corners are all in front of the camera into a mode-"1"
    image of the given size. `offset` shifts pixel coordinates first, `scale` multiplies
    them, so the same routine draws a padded canvas and a supersampled one."""
    image = Image.new("1", (width, height), 0)
    draw = ImageDraw.Draw(image)
    # Pixel centres are integers; polygon coordinates address pixel corners.
    corners = (pixels_2d + 0.5 + offset) * scale - 0.5
    for tri in triangles:
        if depth_ok[tri].all():
            draw.polygon([tuple(corners[i]) for i in tri], fill=1)
    return np.asarray(image, dtype=bool)


# --------------------------------------------------------------------------------------
# Masks


def decode_ids(pixels, palette):
    """Label every pixel with the palette entry it shows; palette[0] must be background.

    Interior pixels match an entry within _EXACT. An anti-aliased edge pixel is taken as a
    mix of two of the entries labelled around it (background always among them): the pair
    whose mix explains the pixel best wins, and of that pair, the one weighing more.

    Args:
        pixels: (H, W, 3) float colours.
        palette: (K, 3) float colours; entry 0 is background.

    Returns:
        (H, W) int32 palette indices.
    """
    height, width, _ = pixels.shape
    palette = np.asarray(palette, dtype=np.float32)
    flat = pixels.reshape(-1, 3)

    best = np.zeros(len(flat), dtype=np.int32)
    best_distance = np.full(len(flat), np.inf, dtype=np.float32)
    for index, colour in enumerate(palette):
        distance = np.abs(flat - colour).max(axis=1)
        closer = distance < best_distance
        best[closer], best_distance[closer] = index, distance[closer]

    ids = best.reshape(height, width)
    exact = (best_distance < _EXACT).reshape(height, width)
    resolved = ids.copy()

    for y, x in zip(*np.nonzero(~exact)):
        candidates = set()
        for radius in (1, 2, 3):
            window = (slice(max(0, y - radius), y + radius + 1), slice(max(0, x - radius), x + radius + 1))
            candidates = set(ids[window][exact[window]].tolist())
            if candidates:
                break
        candidates.add(0)
        colour = pixels[y, x]
        options = sorted(candidates)
        winner, winner_error = ids[y, x], np.inf
        for i, a in enumerate(options):
            error = float(np.abs(colour - palette[a]).max())
            if error < winner_error:
                winner, winner_error = a, error
            for b in options[i + 1:]:
                ca, cb = palette[a], palette[b]
                span = ca - cb
                length = float(span @ span)
                if length < 1e-12:
                    continue
                weight = min(1.0, max(0.0, float((colour - cb) @ span) / length))
                error = float(np.abs(colour - (cb + weight * span)).max())
                if error < winner_error:
                    winner, winner_error = (a if weight >= 0.5 else b), error
        resolved[y, x] = winner
    return resolved


def _to_srgb8(linear):
    linear = np.clip(np.nan_to_num(linear), 0.0, 1.0)
    srgb = np.where(linear <= 0.0031308, linear * 12.92, 1.055 * np.power(linear, 1.0 / 2.4) - 0.055)
    return (srgb * 255.0 + 0.5).astype(np.uint8)


def _colour8(colour):
    return tuple(int(round(float(c) * 255.0)) for c in colour)


# --------------------------------------------------------------------------------------
# One frame


class _Instance:
    def __init__(self, instance_id, class_id, class_name, path, colour):
        self.instance_id, self.class_id, self.class_name = instance_id, class_id, class_name
        self.path, self.colour = path, colour
        self.gprims = []


def _instances(stage, root, time):
    """Instances visible at `time`, keyed by instance id, from the primvars Semantic
    Primvars authored. An instance's path is the prim its gprims share the label of --
    the nearest ancestor they all sit under."""
    root_prim = stage.GetPrimAtPath(root)
    found = {}
    if not root_prim or not root_prim.IsValid():
        return found
    for prim in Usd.PrimRange(root_prim):
        if not prim.IsA(UsdGeom.Gprim):
            continue
        primvars = UsdGeom.PrimvarsAPI(prim)
        instance_pv, class_pv = primvars.GetPrimvar("instance_id"), primvars.GetPrimvar("semantic_id")
        if not instance_pv or not class_pv or not instance_pv.HasValue():
            continue
        if UsdGeom.Imageable(prim).ComputeVisibility(time) == UsdGeom.Tokens.invisible:
            continue
        instance_id = int(instance_pv.Get(time))
        entry = found.get(instance_id)
        if entry is None:
            colour = primvars.GetPrimvar("instance_color").Get(time)
            entry = found[instance_id] = _Instance(
                instance_id, int(class_pv.Get(time)), "", prim.GetPath(), np.array(colour, dtype=np.float32))
        else:
            entry.path = _common_prefix(entry.path, prim.GetPath())
        entry.gprims.append(prim)
    return found


def _common_prefix(a, b):
    while not b.HasPrefix(a):
        a = a.GetParentPath()
    return a


def _conform_policy(stage):
    """The aspect conform policy of the stage's render settings, which is what the Render
    chunk renders with when it is not pointed at another."""
    settings = UsdRender.Settings.GetStageRenderSettings(stage)
    if settings:
        policy = settings.GetAspectRatioConformPolicyAttr().Get()
        if policy:
            return str(policy)
    return "expandAperture"


def _yaw_box(cam_points, heading_cam):
    """KITTI's box: the tightest box around the points that may only yaw about the camera's
    y axis, headed along the object's own x axis projected into the camera's x-z plane.

    Returns:
        (h, w, l, location xyz of the bottom-face centre, rotation_y).
    """
    rotation_y = math.atan2(-heading_cam[2], heading_cam[0])
    c, s = math.cos(rotation_y), math.sin(rotation_y)
    # Into the box frame: undo Ry(rotation_y). Ry maps x->(c,0,-s), z->(s,0,c).
    local = np.c_[cam_points[:, 0] * c - cam_points[:, 2] * s,
                  cam_points[:, 1],
                  cam_points[:, 0] * s + cam_points[:, 2] * c]
    low, high = local.min(axis=0), local.max(axis=0)
    length, height, width = high - low
    centre = (low + high) / 2.0
    bottom = np.array([centre[0], high[1], centre[2]])  # y points down: bottom is max y
    location = np.array([bottom[0] * c + bottom[2] * s, bottom[1], -bottom[0] * s + bottom[2] * c])
    return height, width, length, location, rotation_y


class FrameRejected(ValueError):
    """A frame that rendered but must not be trained on."""


def _camera_inside(camera, instances, time):
    """The path of the instance the camera sits inside, or None.

    A camera placed at random near objects will now and then land inside one. The render
    is then the inside of a surface and every label is nonsense, so the frame is rejected
    rather than written. Tested against each gprim's own box in its local space, which
    over-covers (a torus's hole counts as inside) -- the safe direction to be wrong in.
    """
    eye = np.append(camera.eye, 1.0)
    for inst in instances:
        for gprim in inst.gprims:
            geometry = gprim_triangles(gprim, time)
            if geometry is None or not len(geometry[0]):
                continue
            to_world = np.array(UsdGeom.Xformable(gprim).ComputeLocalToWorldTransform(time), dtype=np.float64)
            local = (eye @ np.linalg.inv(to_world))[:3]
            if np.all(local >= geometry[0].min(axis=0)) and np.all(local <= geometry[0].max(axis=0)):
                return inst.path
    return None


def _wrap(angle):
    return (angle + math.pi) % (2.0 * math.pi) - math.pi


def process_frame(stage, frame, layers, settings):
    """Everything one frame contributes to the dataset, as arrays and records.

    Split from the file writing so a test can check the numbers without a disk layout.
    """
    time = Usd.TimeCode(frame)
    beauty = _layer(layers, settings["beauty_layer"])
    height, width = beauty.shape[:2]
    camera = Camera(stage, settings["camera"], time, width, height, _conform_policy(stage))

    instances = _instances(stage, settings["root"], time)
    ordered = [instances[k] for k in sorted(instances)]
    if (inside := _camera_inside(camera, ordered, time)) is not None:
        raise FrameRejected(f"the camera is inside {inside}")
    palette = [np.zeros(3, dtype=np.float32)] + [inst.colour for inst in ordered]

    decoded = decode_ids(_layer(layers, settings["instance_layer"])[..., :3], palette)
    instance_ids = np.zeros_like(decoded)
    class_ids = np.zeros((height, width), dtype=np.uint8)
    for index, inst in enumerate(ordered, start=1):
        where = decoded == index
        instance_ids[where] = inst.instance_id
        class_ids[where] = inst.class_id

    # The semantic pass is rendered independently of the instance pass, so decoding it
    # too and comparing is a check on the whole chain, primvars to pixels.
    classes = settings["classes"]
    class_palette = [np.zeros(3, dtype=np.float32)] + [settings["class_colours"][c] for c in range(1, len(classes) + 1)]
    semantic_pass = _layer(layers, settings["semantic_layer"])[..., :3]
    semantic_decoded = decode_ids(semantic_pass, class_palette).astype(np.uint8)
    agreement = float((semantic_decoded == class_ids).mean())

    positions = _layer(layers, settings["position_layer"])
    coverage = positions[..., 3] if positions.shape[2] > 3 else np.ones((height, width), np.float32)
    hit = coverage > 0.5
    world = positions[..., :3].reshape(-1, 3).astype(np.float64)
    cam = camera.to_camera(world).reshape(height, width, 3)
    distance_to_plane = np.where(hit, cam[..., 2], np.inf).astype(np.float32)
    distance_to_camera = np.where(hit, np.linalg.norm(cam, axis=2), np.inf).astype(np.float32)

    records = []
    pad_x, pad_y = width, height
    for inst in ordered:
        visible = instance_ids == inst.instance_id
        visible_px = int(visible.sum())

        world_points = []
        silhouette_image = np.zeros((height, width), dtype=bool)
        padded = np.zeros((height + 2 * pad_y, width + 2 * pad_x), dtype=bool)
        for gprim in inst.gprims:
            geometry = gprim_triangles(gprim, time)
            if geometry is None or not len(geometry[1]):
                continue
            points_world = _to_world(geometry[0], gprim, time)
            world_points.append(points_world)
            cam_points = camera.to_camera(points_world)
            in_front = cam_points[:, 2] > camera.near * camera.meters
            pix = camera.project(np.where(in_front[:, None], cam_points, 1.0))
            padded |= _rasterise(pix, geometry[1], in_front, width + 2 * pad_x, height + 2 * pad_y,
                                 np.array([pad_x, pad_y]), 1)
            fine = _rasterise(pix, geometry[1], in_front, width * _SUPERSAMPLE, height * _SUPERSAMPLE,
                              np.zeros(2), _SUPERSAMPLE)
            covered = fine.reshape(height, _SUPERSAMPLE, width, _SUPERSAMPLE).mean(axis=(1, 3)) >= 0.5
            silhouette_image |= covered
        if not world_points:
            continue

        unoccluded_px = int(silhouette_image.sum())
        if visible_px == 0 and unoccluded_px == 0:
            continue  # nowhere in view
        visible_fraction = min(1.0, visible_px / unoccluded_px) if unoccluded_px else 1.0
        # Both halves of the ratio from the one padded canvas: measured against the
        # supersampled in-image silhouette instead, the two rasterisations' different edge
        # rules alone read as a few percent of truncation on objects wholly in frame.
        padded_total = int(padded.sum())
        in_frame = int(padded[pad_y:pad_y + height, pad_x:pad_x + width].sum())
        truncation = 1.0 - in_frame / padded_total if padded_total else 1.0

        box2d = None
        if visible_px:
            ys, xs = np.nonzero(visible)
            box2d = (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))

        world_points = np.concatenate(world_points)
        cam_points = camera.to_camera(world_points)
        anchor = inst.gprims[0]
        to_world = np.array(UsdGeom.Xformable(anchor).ComputeLocalToWorldTransform(time), dtype=np.float64)
        heading_cam = (camera.world_to_cam[:3, :3] @ to_world[0, :3])
        h, w, l, location, rotation_y = _yaw_box(cam_points, heading_cam)
        alpha = _wrap(rotation_y - math.atan2(location[0], location[2]))

        local_points, _ = gprim_triangles(anchor, time) or (np.zeros((1, 3)), None)
        records.append({
            "instance_id": inst.instance_id,
            "class_id": inst.class_id,
            "class": classes[inst.class_id - 1] if 0 < inst.class_id <= len(classes) else str(inst.class_id),
            "prim_path": str(inst.path),
            "visible_pixels": visible_px,
            "unoccluded_pixels": unoccluded_px,
            "occlusion_ratio": 1.0 - visible_fraction,
            "truncation": truncation,
            "bbox2d": box2d,
            "dimensions_hwl": (h, w, l),
            "location": tuple(location),
            "rotation_y": _wrap(rotation_y),
            "alpha": alpha,
            "local_extent": (local_points.min(axis=0), local_points.max(axis=0)),
            "local_to_world": to_world,
        })

    return {
        "width": width, "height": height, "camera": camera,
        "rgb": _to_srgb8(beauty[..., :3]),
        "class_ids": class_ids, "instance_ids": instance_ids,
        # Data, not an image: written 1:1 so it keeps the colours Semantic Labels authored.
        "semantic_pass": (np.clip(semantic_pass, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8),
        "distance_to_image_plane": distance_to_plane, "distance_to_camera": distance_to_camera,
        "records": records, "semantic_agreement": agreement,
    }


# --------------------------------------------------------------------------------------
# Writing


def _kitti_line(record, name, settings):
    """One KITTI object label line. Objects too small to learn from become DontCare, which
    KITTI's own evaluation ignores rather than counting as missed."""
    left, top, right, bottom = record["bbox2d"]
    if bottom - top + 1 < settings["bbox_height_threshold"]:
        return f"DontCare -1 -1 -10 {left:.2f} {top:.2f} {right:.2f} {bottom:.2f} -1 -1 -1 -1000 -1000 -1000 -10"

    visible = 1.0 - record["occlusion_ratio"]
    if visible >= settings["fully_visible_threshold"]:
        occluded = 0
    elif visible >= settings["partly_occluded_threshold"]:
        occluded = 1
    else:
        occluded = 2
    h, w, l = record["dimensions_hwl"]
    x, y, z = record["location"]
    return (f"{name} {record['truncation']:.2f} {occluded} {record['alpha']:.2f} "
            f"{left:.2f} {top:.2f} {right:.2f} {bottom:.2f} "
            f"{h:.2f} {w:.2f} {l:.2f} {x:.2f} {y:.2f} {z:.2f} {record['rotation_y']:.2f}")


def _calib_text(camera):
    """KITTI calib: the same P for all four cameras (this is one mono camera),
    R0_rect identity, and Tr_velo_to_cam holding world -> camera so that the world frame
    stands in for the lidar frame KITTI's tools expect."""
    projection = np.c_[camera.K, np.zeros(3)]
    fmt = lambda m: " ".join(f"{v:.12e}" for v in np.asarray(m).ravel())  # noqa: E731
    lines = [f"P{i}: {fmt(projection)}" for i in range(4)]
    lines += [f"R0_rect: {fmt(np.eye(3))}",
              f"Tr_velo_to_cam: {fmt(camera.world_to_cam[:3, :])}",
              f"Tr_imu_to_velo: {fmt(np.c_[np.eye(3), np.zeros(3)])}"]
    return "\n".join(lines) + "\n"


def _camera_params(camera):
    """Replicator's camera_params record."""
    return {
        "cameraAperture": [float(camera.gf.horizontalAperture), float(camera.gf.verticalAperture)],
        "cameraApertureOffset": [float(camera.gf.horizontalApertureOffset), float(camera.gf.verticalApertureOffset)],
        "cameraFocalLength": float(camera.gf.focalLength),
        "cameraModel": "pinhole",
        "cameraNearFar": [float(camera.near), float(camera.far)],
        "cameraProjection": [float(v) for row in camera.projection for v in row],
        "cameraViewTransform": [float(v) for row in camera.view for v in row],
        "metersPerSceneUnit": float(camera.meters),
        "renderProductResolution": [camera.width, camera.height],
        "intrinsics": camera.K.tolist(),
    }


def _save(path, array_or_image):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if isinstance(array_or_image, Image.Image):
        array_or_image.save(path)
    else:
        np.save(path, array_or_image)


def write_frame(out_dir, frame, result, settings):
    """Write one processed frame in the KITTI layout and, optionally, Replicator's."""
    fid = f"{int(round(frame)):06d}"
    classes, rename = settings["classes"], settings["rename"]
    detect = set(settings["detect_classes"]) or set(classes)
    class_colours = settings["class_colours"]
    records = result["records"]

    if settings["write_kitti"]:
        _save(os.path.join(out_dir, "image_2", fid + ".png"), Image.fromarray(result["rgb"]))
        lines = [_kitti_line(r, rename.get(r["class"], r["class"]), settings)
                 for r in records if r["bbox2d"] and r["class"] in detect]
        os.makedirs(os.path.join(out_dir, "label_2"), exist_ok=True)
        with open(os.path.join(out_dir, "label_2", fid + ".txt"), "w", encoding="utf-8") as f:
            f.write("".join(line + "\n" for line in lines))
        os.makedirs(os.path.join(out_dir, "calib"), exist_ok=True)
        with open(os.path.join(out_dir, "calib", fid + ".txt"), "w", encoding="utf-8") as f:
            f.write(_calib_text(result["camera"]))

        _save(os.path.join(out_dir, "semantic", fid + ".png"), Image.fromarray(result["class_ids"]))
        colourised = np.zeros(result["class_ids"].shape + (3,), dtype=np.uint8)
        for class_id in range(1, len(classes) + 1):
            colourised[result["class_ids"] == class_id] = _colour8(class_colours[class_id])
        _save(os.path.join(out_dir, "semantic_rgb", fid + ".png"), Image.fromarray(colourised))
        _save(os.path.join(out_dir, "semantic_primvar", fid + ".png"), Image.fromarray(result["semantic_pass"]))

        # KITTI's instance encoding: class id * 256 + instance, in a 16-bit PNG.
        panoptic = result["class_ids"].astype(np.uint16) * 256 + np.minimum(result["instance_ids"], 255).astype(np.uint16)
        _save(os.path.join(out_dir, "instance", fid + ".png"), Image.fromarray(panoptic))

        # KITTI's depth encoding: metres * 256 in a 16-bit PNG, 0 where there is no depth.
        plane = result["distance_to_image_plane"]
        depth = np.where(np.isfinite(plane) & (plane > 0), np.clip(np.round(plane * 256.0), 1, 65535), 0)
        _save(os.path.join(out_dir, "depth", fid + ".png"), Image.fromarray(depth.astype(np.uint16)))

    if settings["write_replicator"]:
        rep = os.path.join(out_dir, "replicator")
        rid = f"{int(round(frame)):04d}"
        _save(os.path.join(rep, f"rgb_{rid}.png"), Image.fromarray(result["rgb"]))
        _save(os.path.join(rep, f"distance_to_camera_{rid}.npy"), result["distance_to_camera"])
        _save(os.path.join(rep, f"distance_to_image_plane_{rid}.npy"), result["distance_to_image_plane"])

        rgba = np.zeros(result["class_ids"].shape + (4,), dtype=np.uint8)
        labels = {"(0, 0, 0, 0)": {"class": _BACKGROUND}}
        for class_id in range(1, len(classes) + 1):
            colour = _colour8(class_colours[class_id]) + (255,)
            rgba[result["class_ids"] == class_id] = colour
            labels[str(colour)] = {"class": classes[class_id - 1]}
        _save(os.path.join(rep, f"semantic_segmentation_{rid}.png"), Image.fromarray(rgba, "RGBA"))
        _json(os.path.join(rep, f"semantic_segmentation_labels_{rid}.json"), labels)

        _save(os.path.join(rep, f"instance_segmentation_{rid}.png"),
              Image.fromarray(result["instance_ids"].astype(np.uint16)))
        _json(os.path.join(rep, f"instance_segmentation_mapping_{rid}.json"),
              {str(r["instance_id"]): {"primPath": r["prim_path"], "class": r["class"]} for r in records})

        boxed = [r for r in records if r["bbox2d"] and r["class"] in detect]
        tight = np.zeros(len(boxed), dtype=_BBOX2D_DTYPE)
        cube = np.zeros(len(boxed), dtype=_BBOX3D_DTYPE)
        for row, r in enumerate(boxed):
            tight[row] = (r["class_id"], *r["bbox2d"], r["occlusion_ratio"])
            low, high = r["local_extent"]
            cube[row] = (r["class_id"], *low, *high, r["local_to_world"].astype(np.float32), r["occlusion_ratio"])
        box_labels = {str(i): {"class": classes[i - 1]} for i in sorted({r["class_id"] for r in boxed})}
        _save(os.path.join(rep, f"bounding_box_2d_tight_{rid}.npy"), tight)
        _json(os.path.join(rep, f"bounding_box_2d_tight_labels_{rid}.json"), box_labels)
        _json(os.path.join(rep, f"bounding_box_2d_tight_prim_paths_{rid}.json"), [r["prim_path"] for r in boxed])
        _save(os.path.join(rep, f"bounding_box_3d_{rid}.npy"), cube)
        _json(os.path.join(rep, f"bounding_box_3d_labels_{rid}.json"), box_labels)
        _json(os.path.join(rep, f"bounding_box_3d_prim_paths_{rid}.json"), [r["prim_path"] for r in boxed])
        _json(os.path.join(rep, f"camera_params_{rid}.json"), _camera_params(result["camera"]))


def _json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _settings(node):
    classes = _split(node.get_param("classes").value)
    colours = {}
    stage = node.get_stage()
    # The class colours the semantic pass was rendered with are on the stage already;
    # reading them back beats re-deriving Semantic Labels' palette and drifting from it.
    root = stage.GetPrimAtPath(node.get_param("root").value or "/")
    if root and root.IsValid():
        for prim in Usd.PrimRange(root):
            primvars = UsdGeom.PrimvarsAPI(prim)
            class_pv = primvars.GetPrimvar("semantic_id")
            if class_pv and class_pv.HasValue():
                colours.setdefault(int(class_pv.Get()), np.array(primvars.GetPrimvar("semantic_color").Get(), np.float32))
    for class_id in range(1, len(classes) + 1):
        colours.setdefault(class_id, np.ones(3, np.float32) * (class_id / (len(classes) + 1)))

    return {
        "root": Sdf.Path(node.get_param("root").value or "/"),
        "camera": Sdf.Path(node.get_param("camera").value),
        "classes": classes,
        "class_colours": colours,
        "detect_classes": _split(node.get_param("detect_classes").value),
        "rename": _mapping(node.get_param("class_mapping").value),
        "beauty_layer": node.get_param("beauty_layer").value or "RGBA",
        "position_layer": node.get_param("position_layer").value or "P",
        "semantic_layer": node.get_param("semantic_layer").value or "semantic",
        "instance_layer": node.get_param("instance_layer").value or "instance",
        "bbox_height_threshold": int(node.get_param("bbox_height_threshold").value),
        "partly_occluded_threshold": float(node.get_param("partly_occluded_threshold").value),
        "fully_visible_threshold": float(node.get_param("fully_visible_threshold").value),
        "write_kitti": bool(node.get_param("write_kitti").value),
        "write_replicator": bool(node.get_param("write_replicator").value),
    }


def write_dataset(node):
    """Read every rendered frame, write the dataset, and return how many frames it wrote."""
    stage = node.get_stage()
    if not stage:
        return 0
    try:
        frames = chunk_utils.parse_frames(node.get_param("frames").value)
    except ValueError as exc:
        logger.error("KITTI Writer: %s", exc)
        return 0
    if not frames:
        logger.error("KITTI Writer: no frames to write -- set Frames to the range Render rendered.")
        return 0

    pattern = (node.get_param("input_images").value or "").strip()
    out_dir = _cwd_relative((node.get_param("output_dir").value or "").strip())
    if not pattern or not out_dir:
        logger.error("KITTI Writer: set both Input Images and Output Dir.")
        return 0

    settings = _settings(node)
    if not settings["classes"]:
        logger.error("KITTI Writer: Classes is empty; nothing would be labelled.")
        return 0

    written = 0
    for frame in frames:
        path = frame_path(pattern, frame)
        if not os.path.isfile(path):
            logger.warning("KITTI Writer: frame %g was not rendered (%s); skipped.", frame, path)
            continue
        try:
            result = process_frame(stage, frame, read_layers(path), settings)
        except FrameRejected as exc:
            # Recorded, so the index knows the frame exists and was left out on purpose --
            # and so a rerun that rejects a frame an earlier run wrote drops it from the splits.
            logger.warning("KITTI Writer: frame %g skipped: %s.", frame, exc)
            fid = f"{int(round(frame)):06d}"
            _json(os.path.join(out_dir, "qa", fid + ".json"), {"frame": fid, "skipped": str(exc)})
            continue
        except (KeyError, ValueError) as exc:
            logger.error("KITTI Writer: frame %g: %s", frame, exc)
            continue
        write_frame(out_dir, frame, result, settings)

        fid = f"{int(round(frame)):06d}"
        _json(os.path.join(out_dir, "qa", fid + ".json"), {
            "frame": fid,
            "image_size": [result["width"], result["height"]],
            "objects": len(result["records"]),
            "labelled": sum(1 for r in result["records"] if r["bbox2d"]),
            "semantic_pass_agreement": round(result["semantic_agreement"], 5),
        })
        written += 1
        if result["semantic_agreement"] < 0.99:
            logger.warning("KITTI Writer: frame %g: semantic pass agrees with the instance pass on only "
                           "%.1f%% of pixels -- check the primvars and render vars.",
                           frame, 100.0 * result["semantic_agreement"])

    if written:
        write_index(out_dir, settings, float(node.get_param("val_fraction").value),
                    int(node.get_param("split_seed").value))
        logger.info("KITTI Writer: wrote %d frame(s) to %s", written, out_dir)
    return written


def write_index(out_dir, settings, val_fraction, split_seed):
    """ImageSets, classes.txt and dataset.json, from every frame in the dataset on disk.

    Rebuilt from disk rather than from the frames this run wrote: under `usdchunks sweep`
    each wedge index is its own process writing one frame, and the index has to describe
    the whole dataset whichever of them finishes last. The frames are read from qa/, which
    a frame's files are written before -- so a frame is only listed once it is complete.
    """
    qa_dir = os.path.join(out_dir, "qa")
    frames = []
    for name in sorted(os.listdir(qa_dir)) if os.path.isdir(qa_dir) else []:
        if name.endswith(".json"):
            with open(os.path.join(qa_dir, name), encoding="utf-8") as f:
                frames.append(json.load(f))
    if not frames:
        return

    # Split by a hash of the frame id, not by position, so adding frames later -- another
    # sweep, a larger count -- never moves an existing frame from train to val.
    train, val = [], []
    for record in frames:
        if record.get("skipped"):
            continue
        fid = record["frame"]
        (val if random.Random(f"{split_seed}:{fid}").random() < val_fraction else train).append(fid)

    image_sets = os.path.join(out_dir, "ImageSets")
    os.makedirs(image_sets, exist_ok=True)
    for name, ids in (("train", train), ("val", val), ("trainval", sorted(train + val))):
        with open(os.path.join(image_sets, name + ".txt"), "w", encoding="utf-8") as f:
            f.write("".join(i + "\n" for i in ids))

    classes, rename = settings["classes"], settings["rename"]
    detected = settings["detect_classes"] or classes
    with open(os.path.join(out_dir, "classes.txt"), "w", encoding="utf-8") as f:
        f.write("".join(rename.get(c, c) + "\n" for c in detected))
    _json(os.path.join(out_dir, "dataset.json"), {
        "format": "KITTI object (label_2, calib, image_2) + KITTI semantic/instance/depth + Replicator BasicWriter",
        "classes": [{"id": i, "name": c, "kitti_name": rename.get(c, c),
                     "color": _colour8(settings["class_colours"][i]), "detected": c in detected}
                    for i, c in enumerate(classes, start=1)],
        "background_id": 0,
        "image_size": next((r["image_size"] for r in reversed(frames) if "image_size" in r), None),
        "semantic": "semantic/*.png: uint8 class id per pixel, 0 = background",
        "instance": "instance/*.png: uint16, class_id * 256 + instance_id",
        "depth": "depth/*.png: uint16, metres * 256 along the optical axis, 0 = no depth",
        "label_2": "type truncated occluded alpha left top right bottom h w l x y z rotation_y "
                   "(camera frame: x right, y down, z forward, metres)",
        "calib": "P2 = K [I|0]; Tr_velo_to_cam = world -> camera",
        "splits": {"train": len(train), "val": len(val)},
        "frames": frames,
    })


def _write_clicked(owner, notice, sender):
    chunk_api = owner.get_chunk()
    chunk_api.get_param("written").value = write_dataset(chunk_api)


def execute(node):
    """The cook authors nothing -- the dataset is written in commit(), after Render."""
    return


def commit(node):
    if node.get_param("write_on_commit").value:
        node.get_param("written").value = write_dataset(node)


_GROUP_FLAGS = (chunk.ChunkFlags.GroupFlags.COLLAPSABLE | chunk.ChunkFlags.GroupFlags.LABELED
                | chunk.ChunkFlags.GroupFlags.FRAMED)


def main():
    chunk_api = chunk.Chunk("KITTI Writer")
    labeled = chunk.ChunkFlags.ParamFlags.LABELED

    chunk_api.add_params([
        chunk.LineEdit("input_images", "Input Images", default_value="renders/raw/frame.####.exr", flags=labeled,
                       tooltip="The layered EXRs the Render chunk wrote -- wire this to its Output "
                       "Image. #### or {frame} is replaced by the frame number."),
        chunk.LineEdit("frames", "Frames", default_value="1:100", flags=labeled,
                       tooltip="Which frames to write, in the Render chunk's spelling."),
        chunk.LineEdit("output_dir", "Output Dir", default_value="dataset", flags=labeled),
        # Neither is PRIM_SELECTION, which would pre-fill them from the host's selection
        # every time the chunk is built -- a reload included -- over their defaults.
        chunk.LineEdit("camera", "Camera", default_value="/World/camera", flags=labeled),
        chunk.LineEdit("root", "Root", default_value="/World", flags=labeled,
                       tooltip="Instances are read from Semantic Labels' render primvars at and below this prim."),
        chunk.LineEdit("classes", "Classes", default_value="sphere,cube,torus", flags=labeled,
                       tooltip="The class list Semantic Labels numbered (its Render Primvars Classes): wire the two together."),
        chunk.LineEdit("detect_classes", "Detect Classes", default_value="", flags=labeled,
                       tooltip="Classes that get label_2 boxes, e.g. 'cube'. The others are still in "
                       "the images and masks, as the negatives a detector needs. Empty labels all."),
        chunk.LineEdit("class_mapping", "Class Mapping", default_value="", flags=labeled,
                       tooltip="Rename classes in label_2, e.g. 'cube=Cube, sphere=Ball'."),
    ])

    layers = chunk.Group("layers", "Render Layers", flags=_GROUP_FLAGS)
    layers.add_params([
        chunk.LineEdit("beauty_layer", "Beauty", default_value="RGBA", flags=labeled),
        chunk.LineEdit("position_layer", "World Position", default_value="P", flags=labeled),
        chunk.LineEdit("semantic_layer", "Semantic", default_value="semantic", flags=labeled),
        chunk.LineEdit("instance_layer", "Instance", default_value="instance", flags=labeled),
    ])
    chunk_api.add_param(layers)

    kitti = chunk.Group("kitti", "Labels", flags=_GROUP_FLAGS)
    kitti.add_params([
        chunk.Slider("bbox_height_threshold", "Min Box Height", default_value=25, is_int=True,
                     min_value=0, max_value=1000,
                     tooltip="Boxes shorter than this many pixels are written as DontCare."),
        chunk.Slider("partly_occluded_threshold", "Partly Occluded", default_value=0.5, min_value=0.0, max_value=1.0,
                     tooltip="Visible fraction at or above which an object is at worst partly "
                     "occluded (KITTI occluded 1); below it, largely occluded (2)."),
        chunk.Slider("fully_visible_threshold", "Fully Visible", default_value=0.95, min_value=0.0, max_value=1.0,
                     tooltip="Visible fraction at or above which an object is fully visible (KITTI occluded 0)."),
        chunk.Slider("val_fraction", "Validation Fraction", default_value=0.2, min_value=0.0, max_value=1.0),
        chunk.Slider("split_seed", "Split Seed", default_value=0, is_int=True, min_value=0, max_value=100000),
        chunk.Checkbox("write_kitti", "Write KITTI", default_value=True),
        chunk.Checkbox("write_replicator", "Write Replicator", default_value=True),
    ])
    chunk_api.add_param(kitti)

    chunk_api.add_params([
        chunk.Checkbox("write_on_commit", "Write on Commit", default_value=True),
        chunk.Slider("written", "Frames Written", is_int=True,
                     flags=chunk.ChunkFlags.ParamFlags.OUTPUT | labeled),
    ])

    write_btn = chunk.Button("write_now", "Write Dataset")
    write_btn.add_callback(_write_clicked)
    chunk_api.add_param(write_btn)

    return chunk_api


def config():
    return chunk.DefaultConfig(
        category="Datasets",
        requirements=["numpy>=1.24,<3", "OpenEXR>=3.3,<4", "Pillow>=10,<13"],
        description="""
        KITTI Writer — turns rendered frames into a training dataset: KITTI object
        labels, calibration, class and instance masks and depth, plus the same data
        in NVIDIA Replicator's BasicWriter layout.

        Put it after the Render chunk. Render writes one layered EXR per frame
        holding the beauty, world position (P) and the semantic_color and
        instance_color primvar passes; this reads each, together with the stage at
        that frame, in commit() — after Render's own commit — or on Write Dataset.

        Output, per frame NNNNNN:
          - image_2/NNNNNN.png          RGB, sRGB 8-bit
          - label_2/NNNNNN.txt          KITTI object labels: type truncated
                                        occluded alpha, the tight 2D box, and a
                                        3D box (h w l, x y z, rotation_y) in the
                                        camera frame in metres
          - calib/NNNNNN.txt            P0-P3 = K[I|0], R0_rect, Tr_velo_to_cam
                                        (world -> camera)
          - semantic/NNNNNN.png         uint8 class id, 0 = background
          - semantic_rgb/NNNNNN.png     the class mask in class colours
          - semantic_primvar/NNNNNN.png the semantic primvar pass as rendered
          - instance/NNNNNN.png         uint16 class*256 + instance
          - depth/NNNNNN.png            uint16 metres*256, 0 = no depth
          - replicator/                 rgb_, distance_to_camera_,
                                        distance_to_image_plane_, semantic_ and
                                        instance_segmentation_, bounding_box_2d_tight_,
                                        bounding_box_3d_, camera_params_ per frame
        and once: ImageSets/train.txt, val.txt, trainval.txt, classes.txt and
        dataset.json (classes, ids, colours, encodings, per-frame QA).

        Masks are decoded rather than thresholded: an anti-aliased edge pixel is
        given to whichever of the instances around it contributes more, so every
        pixel carries one real label. The semantic pass is decoded too and compared
        with the class mask the instance pass implies; dataset.json records the
        agreement per frame and anything under 99% is warned about.

        Occlusion is measured, not estimated: each object's unoccluded silhouette
        is rasterised from its geometry and compared with the pixels it actually
        won. Truncation is the share of that silhouette outside the image.

        Parameters:
          - Input Images: the EXR path Render wrote, with #### for the frame.
          - Frames: frames to write, in the Render chunk's frame spec.
          - Output Dir: dataset root. Relative paths resolve against the working
            directory, as Render's do.
          - Camera, Root: the rendering camera, and where instances are read.
          - Classes: the class list Semantic Labels numbered, from its Render
            Primvars group. Wire them together.
          - Detect Classes: classes that get label_2 boxes (e.g. "cube"). The
            rest stay in the images and masks as negatives. Empty labels all.
          - Class Mapping: renames in label_2, e.g. "cube=Cube".
          - Render Layers: the EXR layer names, i.e. the Render Vars' names.
          - Min Box Height: shorter boxes become DontCare (Replicator's 25 px).
          - Partly Occluded / Fully Visible: visible-fraction thresholds for
            KITTI's occluded levels 1 and 0 (Replicator's 0.5 and 0.95).
          - Validation Fraction / Split Seed: the train/val split, decided per
            frame by a hash so adding frames never moves existing ones.
          - Write KITTI / Write Replicator: which layouts to write.
          - Write on Commit: write when `usdchunks run` commits.
          - Frames Written (output), Write Dataset (button).
        """,
    )
