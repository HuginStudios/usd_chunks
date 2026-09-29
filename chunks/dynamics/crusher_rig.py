"""Crusher Rig chunk -- an example machine: a parametric double-roll rock crusher.

Rock falls into a hopper, is pinched between two toothed rollers turning in towards each
other, drops through a chute onto a conveyor, and is carried up and tipped into a bucket.

It is an ordinary UsdPhysics scene, so nothing about it is special to this chunk -- a
machine modelled by hand and set up the same way works the same in the RBD Solver:

* every part has ``CollisionAPI``;
* rollers and belt drums are kinematic rigid bodies, each on a ``RevoluteJoint`` whose
  angular ``DriveAPI`` has a ``targetVelocity`` -- the solver spins them at that speed;
* the belt is a moving surface: its points carry ``velocities`` while staying put;
* a guide box, ``Emitter``, with collision switched off -- a Region for the Emit chunk;
* ``rbd:colliderThickness`` gives each part's real wall thickness to the collider builder.

The rig is built Y-up and stood upright with a single rotation on a Z-up stage.
"""

from __future__ import annotations

import math

import numpy as np
from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics, Vt

from usd_chunks import chunk


_STEEL = (0.32, 0.34, 0.37)
_DARK = (0.13, 0.13, 0.14)
_YELLOW = (0.85, 0.62, 0.10)
_BELT = (0.07, 0.07, 0.07)
_BUCKET = (0.60, 0.20, 0.10)


class Part:
    __slots__ = ("name", "verts", "tris", "role", "colour", "thickness", "attrs")

    def __init__(self, name, verts, tris, role="static", colour=_STEEL, thickness=None, **attrs):
        self.name = name
        #: Collider depth behind each face -- the part's real wall thickness, so the prisms
        #: from its two faces fill it without poking out of either side.
        self.thickness = thickness
        self.verts = np.asarray(verts, dtype=np.float64)
        self.tris = np.asarray(tris, dtype=np.int64)
        self.role = role
        self.colour = colour
        self.attrs = attrs


# ---------------------------------------------------------------------------
# Mesh builders (all return verts, tris with outward winding)
# ---------------------------------------------------------------------------

_BOX_FACES = np.array(
    [
        (0, 2, 1), (0, 3, 2),  # bottom  (-y face of a box listed y-min first)
        (4, 5, 6), (4, 6, 7),  # top
        (0, 1, 5), (0, 5, 4),
        (1, 2, 6), (1, 6, 5),
        (2, 3, 7), (2, 7, 6),
        (3, 0, 4), (3, 4, 7),
    ]
)


def hexa(corners) -> tuple[np.ndarray, np.ndarray]:
    """A closed hexahedron from 8 corners: bottom quad 0-3, then the top quad 4-7 above it.

    Winding is fixed up afterwards against the centroid, so the corner order only has to
    walk each quad the same way round.
    """
    v = np.asarray(corners, dtype=np.float64)
    tris = _BOX_FACES.copy()
    c = v.mean(axis=0)
    p = v[tris]
    n = np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0])
    flip = np.einsum("ij,ij->i", n, p.mean(axis=1) - c) < 0.0
    tris[flip] = tris[flip][:, ::-1]
    return v, tris


def box(centre, size) -> tuple[np.ndarray, np.ndarray]:
    cx, cy, cz = centre
    hx, hy, hz = (s * 0.5 for s in size)
    return hexa(
        [
            (cx - hx, cy - hy, cz - hz), (cx + hx, cy - hy, cz - hz),
            (cx + hx, cy - hy, cz + hz), (cx - hx, cy - hy, cz + hz),
            (cx - hx, cy + hy, cz - hz), (cx + hx, cy + hy, cz - hz),
            (cx + hx, cy + hy, cz + hz), (cx - hx, cy + hy, cz + hz),
        ]
    )


def slab(a, b, c, d, thickness) -> tuple[np.ndarray, np.ndarray]:
    """A plate of *thickness* on the quad a-b-c-d, grown towards the quad's back side."""
    q = np.asarray([a, b, c, d], dtype=np.float64)
    n = np.cross(q[1] - q[0], q[3] - q[0])
    n /= np.linalg.norm(n)
    return hexa(list(q - n * thickness) + list(q))


def merge(*meshes) -> tuple[np.ndarray, np.ndarray]:
    verts, tris, base = [], [], 0
    for v, t in meshes:
        verts.append(v)
        tris.append(t + base)
        base += len(v)
    return np.concatenate(verts), np.concatenate(tris)


def toothed_roller(radius: float, length: float, teeth: int, tooth: float, segments_per_tooth: int = 4):
    """A roller along X: a star-shaped profile in YZ extruded, with fan-triangulated caps.

    Each tooth is a trapezoid ridge running the roller's length -- the bite that grips rock
    instead of letting it skate on a smooth drum.
    """
    prof = []
    for k in range(teeth):
        a0 = 2.0 * math.pi * k / teeth
        step = 2.0 * math.pi / teeth
        for frac, r in ((0.0, radius), (0.18, radius + tooth), (0.42, radius + tooth), (0.6, radius)):
            a = a0 + frac * step
            prof.append((r * math.cos(a), r * math.sin(a)))
    prof = np.asarray(prof)
    m = len(prof)
    x0, x1 = -length * 0.5, length * 0.5
    ring0 = np.column_stack([np.full(m, x0), prof])
    ring1 = np.column_stack([np.full(m, x1), prof])
    verts = np.concatenate([ring0, ring1, [(x0, 0.0, 0.0), (x1, 0.0, 0.0)]])
    c0, c1 = 2 * m, 2 * m + 1
    tris = []
    for i in range(m):
        j = (i + 1) % m
        tris += [(i, j, m + j), (i, m + j, m + i)]
        tris += [(c0, j, i), (c1, m + i, m + j)]
    return orient_outward(verts, np.asarray(tris))


def lathe(profile, segments: int = 32) -> tuple[np.ndarray, np.ndarray]:
    """Revolve a closed (radius, height) profile about Y."""
    prof = np.asarray(profile, dtype=np.float64)
    m = len(prof)
    verts = []
    for s in range(segments):
        a = 2.0 * math.pi * s / segments
        ca, sa = math.cos(a), math.sin(a)
        for r, y in prof:
            verts.append((r * ca, y, r * sa))
    verts = np.asarray(verts)
    tris = []
    for s in range(segments):
        s1 = (s + 1) % segments
        for i in range(m):
            j = (i + 1) % m
            a, b = s * m + i, s * m + j
            c, d = s1 * m + j, s1 * m + i
            tris += [(a, c, b), (a, d, c)]
    tris = np.asarray(tris)
    # Drop the degenerate triangles an on-axis profile point makes.
    p = verts[tris]
    area = np.linalg.norm(np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]), axis=1)
    return orient_outward(verts, tris[area > 1e-9])


def orient_outward(verts, tris):
    """Flip a closed, consistently wound mesh inside-out if its signed volume says it is."""
    p = verts[tris]
    if np.einsum("ij,ij->i", p[:, 0], np.cross(p[:, 1], p[:, 2])).sum() < 0.0:
        tris = tris[:, ::-1]
    return verts, tris


def rot_x(verts, angle, pivot=(0.0, 0.0, 0.0)):
    c, s = math.cos(angle), math.sin(angle)
    r = np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
    p = np.asarray(pivot)
    return (np.asarray(verts) - p) @ r.T + p


# ---------------------------------------------------------------------------
# The rig
# ---------------------------------------------------------------------------


def build_parts(
    roller_radius: float = 18.0,
    roller_length: float = 70.0,
    gap: float = 3.0,
    teeth: int = 10,
    tooth_height: float = 4.0,
    rpm: float = 40.0,
    belt_speed: float = 60.0,
    belt_length: float = 220.0,
    belt_incline: float = 12.0,
    bucket_radius: float = 50.0,
) -> list[Part]:
    """Every part of the machine, Y-up, the rollers' nip at the origin."""
    R, L = roller_radius, roller_length
    zc = R + tooth_height * 0.5 + gap * 0.5  # roller centre, either side of the nip
    parts: list[Part] = []

    # Rollers. The one at -Z turns so its top moves towards +Z (into the nip), the other
    # the opposite way: rock is drawn down between them, not flung out.
    rv, rt = toothed_roller(R, L, teeth, tooth_height)
    for name, z, sign in (("RollerA", -zc, 1.0), ("RollerB", zc, -1.0)):
        parts.append(
            Part(name, rv + (0.0, 0.0, z), rt, "rotor", _DARK, thickness=0.5 * R,
                 axis=(1.0, 0.0, 0.0), rpm=sign * rpm, pivot=(0.0, 0.0, z))
        )

    # Cheek plates close the rollers' ends, so rock cannot walk out sideways.
    tall_lo, tall_hi = -R - 12.0, R + 20.0
    for name, x in (("CheekL", -L * 0.5 - 1.5), ("CheekR", L * 0.5 + 1.5)):
        parts.append(Part(name, *box((x, (tall_lo + tall_hi) * 0.5, 0.0), (3.0, tall_hi - tall_lo, 2 * zc + 2 * R + 16.0)),
                          colour=_YELLOW, thickness=3.0))

    # Hopper: four sloping walls from a wide mouth down onto the rollers' top.
    wt = 2.0
    y0, y1 = R * 0.65, R + 75.0
    bx, bz = L * 0.5, zc + R * 0.35  # bottom opening half-extents
    tx, tz = L * 0.5 + 25.0, zc + R + 40.0  # top opening
    corners = {
        "HopperFront": ((-bx, y0, bz), (bx, y0, bz), (tx, y1, tz), (-tx, y1, tz)),
        "HopperBack": ((bx, y0, -bz), (-bx, y0, -bz), (-tx, y1, -tz), (tx, y1, -tz)),
        "HopperLeft": ((-bx, y0, -bz), (-bx, y0, bz), (-tx, y1, tz), (-tx, y1, -tz)),
        "HopperRight": ((bx, y0, bz), (bx, y0, -bz), (tx, y1, -tz), (tx, y1, tz)),
    }
    for name, quad in corners.items():
        # Quads are wound to face into the hopper, so the plate grows outwards.
        parts.append(Part(name, *slab(*quad, wt), colour=_YELLOW, thickness=wt))

    # Chute under the rollers, funnelling onto the start of the belt.
    cy0, cy1 = -R - 8.0, -R - 45.0
    cw0, cw1 = zc + R * 0.9, 22.0
    ch_x0, ch_x1 = L * 0.5 + 1.0, 22.0
    chute = {
        "ChuteFront": ((-ch_x1, cy1, cw1), (ch_x1, cy1, cw1), (ch_x0, cy0, cw0), (-ch_x0, cy0, cw0)),
        "ChuteBack": ((ch_x1, cy1, -cw1), (-ch_x1, cy1, -cw1), (-ch_x0, cy0, -cw0), (ch_x0, cy0, -cw0)),
        "ChuteLeft": ((-ch_x1, cy1, -cw1), (-ch_x1, cy1, cw1), (-ch_x0, cy0, cw0), (-ch_x0, cy0, -cw0)),
        "ChuteRight": ((ch_x1, cy1, cw1), (ch_x1, cy1, -cw1), (ch_x0, cy0, -cw0), (ch_x0, cy0, cw0)),
    }
    for name, quad in chute.items():
        parts.append(Part(name, *slab(*quad, wt), colour=_STEEL, thickness=wt))

    # Conveyor: a belt running along +Z from under the chute, up an incline.
    inc = math.radians(belt_incline)
    belt_w = 56.0
    z_start = -40.0
    # Low enough under the chute's mouth that crushed rock passes under its front edge: the
    # belt climbs from here, so its clearance is smallest where it leaves the chute.
    belt_y = cy1 - 16.0 + z_start * math.tan(inc)
    pivot = (0.0, belt_y, z_start)
    bv, bt = box((0.0, belt_y - 2.0, z_start + belt_length * 0.5), (belt_w, 4.0, belt_length))
    direction = np.array([0.0, math.sin(inc), math.cos(inc)])
    parts.append(Part("Belt", rot_x(bv, -inc, pivot), bt, "conveyor", _BELT, thickness=4.0,
                      beltVelocity=tuple(direction * belt_speed)))
    for name, x in (("RailL", -belt_w * 0.5 - 1.5), ("RailR", belt_w * 0.5 + 1.5)):
        rv_, rt_ = box((x, belt_y + 4.0, z_start + belt_length * 0.5), (3.0, 16.0, belt_length))
        parts.append(Part(name, rot_x(rv_, -inc, pivot), rt_, colour=_YELLOW, thickness=3.0))
    # Tail plate behind the belt start, so the chute cannot spill backwards off it.
    tv, tt = box((0.0, belt_y + 6.0, z_start - 1.5), (belt_w + 6.0, 20.0, 3.0))
    parts.append(Part("BeltTail", rot_x(tv, -inc, pivot), tt, colour=_YELLOW, thickness=3.0))
    # Drums at both ends. They turn with the belt: rock reaching the head rolls over the drum
    # and off, where a static one would catch it balanced on top.
    drum_r = 6.0
    drum_rpm = belt_speed / (2.0 * math.pi * drum_r) * 60.0  # +X spin: top surface moves +Z
    for name, z in (("DrumHead", z_start + belt_length), ("DrumTail", z_start)):
        dv, dt = lathe([(0.0, -belt_w * 0.5), (drum_r, -belt_w * 0.5), (drum_r, belt_w * 0.5), (0.0, belt_w * 0.5)], 20)
        # Lathe axis Y -> X. Swapping two axes mirrors the mesh, so re-orient it after.
        dv, dt = orient_outward(dv[:, [1, 0, 2]], dt)
        centre = rot_x(np.array([[0.0, belt_y - drum_r, z]]), -inc, pivot)[0]
        dv = rot_x(dv, -inc) + centre
        parts.append(Part(name, dv, dt, "rotor", _DARK, thickness=drum_r * 0.6,
                          axis=(1.0, 0.0, 0.0), rpm=drum_rpm, pivot=tuple(centre)))

    # Bucket under the head of the belt.
    head = rot_x(np.array([[0.0, belt_y, z_start + belt_length]]), -inc, pivot)[0]
    rim = belt_y - 45.0
    bottom = rim - 55.0
    br, wall = bucket_radius, 2.5
    bv, bt = lathe(
        [
            (0.0, bottom), (br * 0.85, bottom), (br, rim), (br - wall, rim),
            (br * 0.85 - wall, bottom + wall), (0.0, bottom + wall),
        ],
        40,
    )
    parts.append(Part("Bucket", bv + (0.0, 0.0, head[2] + br * 0.35), bt, colour=_BUCKET, thickness=wall))

    # Floor under everything.
    fz = (head[2] + br * 0.35 - 140.0 + 0.0) * 0.5
    fv, ft = box((0.0, bottom - 2.0, fz + 40.0), (360.0, 4.0, 520.0))
    parts.append(Part("Floor", fv, ft, colour=(0.28, 0.27, 0.25), thickness=4.0))

    # Legs under the crusher body, for looks.
    for i, (x, z) in enumerate(((-L * 0.5 - 1.5, -zc - R - 6), (L * 0.5 + 1.5, -zc - R - 6),
                                (-L * 0.5 - 1.5, zc + R + 6), (L * 0.5 + 1.5, zc + R + 6))):
        lv, lt = box((x, (bottom + tall_lo) * 0.5, z), (5.0, tall_lo - bottom, 5.0))
        parts.append(Part(f"Leg{i}", lv, lt, colour=_DARK, thickness=2.5))

    # Where rock is dropped from: a box over the hopper mouth.
    ev, et = box((0.0, y1 + 40.0, 0.0), (L * 0.8, 50.0, 2 * zc + R))
    parts.append(Part("Emitter", ev, et, "emitter", (0.2, 0.8, 0.3)))
    return parts


#: The walls on the +X side, hidden by Cutaway so the rollers' nip can be watched.
CUTAWAY = ("CheekR", "HopperRight", "ChuteRight")


def _box_uvs(mesh, verts, tris, scale) -> None:
    """faceVarying UVs by box projection along each triangle's dominant axis, *scale* UVs per
    scene unit -- enough for any tiling texture to sit on the machine without stretching."""
    p = verts[tris]
    n = np.abs(np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]))
    axis = np.repeat(np.argmax(n, axis=1), 3)
    corners = verts[tris.reshape(-1)]
    u = np.where(axis == 0, corners[:, 2], corners[:, 0])
    w = np.where(axis == 1, corners[:, 2], corners[:, 1])
    st = np.column_stack([u, w]) * scale
    UsdGeom.PrimvarsAPI(mesh).CreatePrimvar("st", Sdf.ValueTypeNames.TexCoord2fArray,
                                            UsdGeom.Tokens.faceVarying).Set(
        Vt.Vec2fArray.FromNumpy(np.ascontiguousarray(st, dtype=np.float32)))


def author_rig(stage: Usd.Stage, root: Sdf.Path, parts: list[Part], up_is_z: bool, hidden=(),
               uv_scale: float = 0.02) -> None:
    """Write the machine as a UsdPhysics scene.

    Every part is a collider (``CollisionAPI``). Rollers and drums are kinematic rigid
    bodies, each on a ``RevoluteJoint`` to the world whose angular drive asks for the part's
    speed -- the RBD Solver spins them at exactly that, whatever loads them. The belt carries
    USD point velocities. The emitter box is a guide with collision switched off
    (``physics:collisionEnabled = false``); give it to the Emit chunk as its Region.
    drops bodies from.
    """
    top = UsdGeom.Xform.Define(stage, root)
    if up_is_z:
        top.AddRotateXOp().Set(90.0)
    joints = root.AppendChild("Joints")
    for part in parts:
        mesh = UsdGeom.Mesh.Define(stage, root.AppendChild(part.name))
        v = part.verts
        mesh.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(v, dtype=np.float32)))
        mesh.CreateFaceVertexCountsAttr(Vt.IntArray([3] * len(part.tris)))
        mesh.CreateFaceVertexIndicesAttr(
            Vt.IntArray.FromNumpy(np.ascontiguousarray(part.tris.reshape(-1), dtype=np.int32))
        )
        mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
        lo, hi = v.min(axis=0), v.max(axis=0)
        mesh.CreateExtentAttr(Vt.Vec3fArray([Gf.Vec3f(*map(float, lo)), Gf.Vec3f(*map(float, hi))]))
        mesh.CreateDisplayColorAttr(Vt.Vec3fArray([Gf.Vec3f(*part.colour)]))
        _box_uvs(mesh, v, part.tris, uv_scale)
        prim = mesh.GetPrim()
        if part.role == "emitter":
            UsdGeom.Imageable(mesh).CreatePurposeAttr(UsdGeom.Tokens.guide)
            # A drop zone, not a wall: stated the UsdPhysics way, so any solver reads it.
            UsdPhysics.CollisionAPI.Apply(prim).CreateCollisionEnabledAttr(False)
            continue
        UsdPhysics.CollisionAPI.Apply(prim)
        if part.thickness is not None:
            prim.CreateAttribute("rbd:colliderThickness", Sdf.ValueTypeNames.Float, custom=True).Set(
                float(part.thickness))
        if part.name in hidden:
            # Cut away, not removed: it still collides, so rock stays in while the camera
            # sees through.
            UsdGeom.Imageable(mesh).MakeInvisible()
        if part.role == "rotor":
            body = UsdPhysics.RigidBodyAPI.Apply(prim)
            body.CreateKinematicEnabledAttr(True)
            joint = UsdPhysics.RevoluteJoint.Define(stage, joints.AppendChild(part.name))
            joint.CreateBody1Rel().SetTargets([prim.GetPath()])
            joint.CreateLocalPos1Attr(Gf.Vec3f(*map(float, part.attrs["pivot"])))
            joint.CreateLocalRot1Attr(Gf.Quatf(1.0))
            # The joint's own frame on the world side is the same point, in world space.
            joint.CreateLocalPos0Attr(Gf.Vec3f(*map(float, part.attrs["pivot"])))
            joint.CreateLocalRot0Attr(Gf.Quatf(1.0))
            axis = int(np.argmax(np.abs(part.attrs["axis"])))
            joint.CreateAxisAttr("XYZ"[axis])
            sign = 1.0 if part.attrs["axis"][axis] >= 0 else -1.0
            drive = UsdPhysics.DriveAPI.Apply(joint.GetPrim(), "angular")
            drive.CreateTypeAttr("force")
            drive.CreateStiffnessAttr(0.0)
            drive.CreateDampingAttr(1e10)
            # rpm -> degrees per second.
            drive.CreateTargetVelocityAttr(float(part.attrs["rpm"]) * 6.0 * sign)
        elif part.role == "conveyor":
            # A moving surface: the belt's points stay put but carry its velocity.
            mesh.CreateVelocitiesAttr().Set([Gf.Vec3f(*map(float, part.attrs["beltVelocity"]))] * len(v))


def execute(node) -> None:
    stage = node.get_stage()
    layer = node.get_edit_context()
    if stage is None or layer is None:
        return
    is_f, is_i, is_s = chunk.is_param_t(float), chunk.is_param_t(int), chunk.is_param_t(str)
    parts = build_parts(
        roller_radius=max(1.0, node.get_param("roller_radius", is_f).value),
        roller_length=max(1.0, node.get_param("roller_length", is_f).value),
        gap=max(0.0, node.get_param("gap", is_f).value),
        teeth=max(3, node.get_param("teeth", is_i).value),
        tooth_height=max(0.0, node.get_param("tooth_height", is_f).value),
        rpm=node.get_param("rpm", is_f).value,
        belt_speed=node.get_param("belt_speed", is_f).value,
        belt_length=max(20.0, node.get_param("belt_length", is_f).value),
        belt_incline=node.get_param("belt_incline", is_f).value,
        bucket_radius=max(10.0, node.get_param("bucket_radius", is_f).value),
    )
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)
    with Usd.EditContext(stage, layer):
        author_rig(
            stage,
            Sdf.Path(node.get_param("rig_path", is_s).value or "/CrusherRig"),
            parts,
            UsdGeom.GetStageUpAxis(stage).upper() == "Z",
            hidden=CUTAWAY if node.get_param("cutaway", chunk.is_param_t(bool)).value else (),
        )


def main() -> chunk.Chunk:
    chunk_api = chunk.Chunk("Crusher Rig")
    chunk_api.add_param(
        chunk.LineEdit("rig_path", "Rig Prim", default_value="/CrusherRig",
                       flags=chunk.ChunkFlags.ParamFlags.LABELED,
                       tooltip="Where the machine is written. Add it to the RBD Solver's Collider Prims.")
    )
    _GF = chunk.ChunkFlags.GroupFlags.COLLAPSABLE | chunk.ChunkFlags.GroupFlags.LABELED | chunk.ChunkFlags.GroupFlags.FRAMED
    rollers = chunk.Group("Rollers", "Rollers", flags=_GF)
    rollers.add_param(chunk.Spinbox("roller_radius", "Radius", default_value=18.0))
    rollers.add_param(chunk.Spinbox("roller_length", "Length", default_value=70.0))
    rollers.add_param(chunk.Spinbox("gap", "Gap", default_value=3.0,
                                    tooltip="Space between the rollers' teeth: roughly the size rock is crushed down to."))
    rollers.add_param(chunk.Spinbox("teeth", "Teeth", default_value=10, is_int=True))
    rollers.add_param(chunk.Spinbox("tooth_height", "Tooth Height", default_value=4.0))
    rollers.add_param(chunk.Spinbox("rpm", "RPM", default_value=40.0,
                                    tooltip="Roller speed, turns per minute. The rollers always turn in towards each other."))
    chunk_api.add_param(rollers)
    chunk_api.add_param(chunk.Checkbox("cutaway", "Cutaway", default_value=True,
                                       tooltip="Hide the walls on the +X side to see into the crusher. "
                                       "They still collide."))
    belt = chunk.Group("Conveyor", "Conveyor", flags=_GF)
    belt.add_param(chunk.Spinbox("belt_speed", "Belt Speed", default_value=60.0,
                                 tooltip="Scene units per second."))
    belt.add_param(chunk.Spinbox("belt_length", "Belt Length", default_value=220.0))
    belt.add_param(chunk.Spinbox("belt_incline", "Incline", default_value=12.0, tooltip="Degrees."))
    belt.add_param(chunk.Spinbox("bucket_radius", "Bucket Radius", default_value=50.0))
    chunk_api.add_param(belt)
    return chunk_api


def config() -> chunk.DefaultConfig:
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.EXAMPLES,
        description=(
            "A parametric double-roll rock crusher -- hopper, toothed rollers, chute, conveyor\n"
            "and bucket -- set up with UsdPhysics for the RBD Solver."
        ),
        requirements=["numpy>=2,<3"],
    )
