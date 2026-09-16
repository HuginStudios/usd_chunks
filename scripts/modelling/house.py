"""Swedish House chunk — builds a Falu-red cottage from a box.

A procedural-modelling showcase: one chunk, no input mesh, no wiring required. Width is
the headline control, and dragging it does not just stretch a shape — it re-derives the
whole structure. Window bays re-tile to stay evenly spaced (more of them, not wider
ones), the roof re-spans at a constant pitch and re-tiles itself, and the chimney rides
along with the ridge at a fixed real-world size rather than scaling with the house. That
re-derivation, not the shape change itself, is the point: it is what "parametric" means
in practice.

**Walls are punched, not painted.** Each wall is a slab with real openings cut through
it — `_build_wall_panel` decomposes the wall into the solid strips left over once the
bays are removed, columns first and then the gaps between stacked floors. That is what
lets a window be a window: the frame fills the hole through the wall's full thickness and
the glass sits back inside it, so the opening reads by parallax and self-shadowing rather
than as a coloured rectangle stuck to a flat box. `_layout_bays` is the single source of
truth for where those openings are, so the hole and the frame that fills it can never
drift apart.

**Windows, roof tiles and ridge caps are instanced.** Each is one small prototype mesh
under a `UsdGeom.PointInstancer` — a few hundred faces authored once, placed a thousand
times. Roof tiles are laid course by course across every roof plane by one generic
routine (`_tile_plane`) that works off the plane's own polygon, so a Gable's rectangles,
a Gambrel's four bands, a Mono's single slope and a Hip's corner triangles are all tiled
by the same code rather than four special cases. Tiles are drawn from three
slightly-different colours and given a little rotation and offset jitter, because a roof
laid perfectly straight in one flat colour is the thing that reads as computer-generated.

**Five roof styles, one interface.** Gable, Hip, Gambrel, Saltbox and Mono are different
builders, but each returns the same two things: the list of roof planes (to tile) and the
ridge segment (to cap, and to sit the chimney on). Gable and Saltbox are in fact the
*same* builder driven by a different ridge split; only Hip (no gable ends) and Mono (its
two long walls differ in height) need code of their own.

Everything is flat `constant` display colour, not a shader network — so it draws the same
in usdview and in Blender, and the chunk stays self-contained. The defaults lean
cartoonish on purpose: saturated Falu red, green trim, chunky trim boards, plank grooves,
and a warm terracotta tiled roof.
"""

from __future__ import annotations

import logging
import math
import random

from pxr import Gf, Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk

logger = logging.getLogger(__name__)

ROOF_GABLE = "Gable"
ROOF_SALTBOX = "Saltbox"
ROOF_GAMBREL = "Gambrel"
ROOF_HIP = "Hip"
ROOF_MONO = "Mono"
_ROOF_STYLES = [ROOF_GABLE, ROOF_HIP, ROOF_GAMBREL, ROOF_SALTBOX, ROOF_MONO]

#: The flat-mesh prims this chunk authors, each its own colour, under the house scope.
#: The instanced parts (Windows, RoofTiles, RidgeTiles) are PointInstancers alongside them.
PART_NAMES = ("Walls", "Planks", "Trim", "RoofTrim", "Door", "Roof", "Chimney")

#: Safety cap on instanced tiles, so a slider dragged to its end cannot wedge a session.
_MAX_TILES = 24000

#: How far a window is allowed to sit crooked in its opening, at full Crooked.
_WINDOW_SPIN = 0.09

#: Fixed, so the hand-laid jitter is the same every cook. A roof that reshuffles itself
#: each time a slider moves reads as noise rather than as a roof.
_SEED = 20260913


# ---------------------------------------------------------------------------
# a tiny unshared-vertex mesh builder — flat shading with no authored normals
# ---------------------------------------------------------------------------


def _dedupe(points, eps=1e-7):
    """Drop consecutive (and a closing) duplicate point, so a degenerate polygon —
    the pyramid apex a Hip roof collapses to when width <= depth — still has 3+ points
    or is dropped rather than authored as a broken face."""
    out = []
    for p in points:
        if not out or math.dist(p, out[-1]) > eps:
            out.append(p)
    if len(out) > 2 and math.dist(out[0], out[-1]) <= eps:
        out.pop()
    return out


class _Mesh:
    """Accumulates one flat-shaded polygon soup, in a chunk-local (u, v, w) frame.

    Every face gets its own unshared vertices rather than sharing corners between
    faces — the cheap way to get crisp, faceted shading with no authored normals.

    (u, v, w) rather than raw (x, y, z) so every builder is written once and works on a
    Y-up or Z-up stage alike: `u` is always "along the front wall", `v` always "into the
    house", `w` always "up" — see `_axes`. A prototype mesh reuses the same frame with a
    local meaning: u across the part, v into the wall or down the slope, w out of the
    surface — which is exactly the convention `_orient_quat` rotates from.
    """

    __slots__ = ("right", "forward", "up", "points", "counts", "indices")

    def __init__(self, axes):
        self.right, self.forward, self.up = axes
        self.points: list = []
        self.counts: list = []
        self.indices: list = []

    def _to_world(self, u, v, w):
        p = self.right * u + self.forward * v + self.up * w
        return (float(p[0]), float(p[1]), float(p[2]))

    def ngon(self, uvw_points):
        pts = _dedupe([self._to_world(*p) for p in uvw_points])
        if len(pts) < 3:
            return
        start = len(self.points)
        self.points.extend(Gf.Vec3f(*p) for p in pts)
        self.counts.append(len(pts))
        self.indices.extend(range(start, start + len(pts)))

    def box(self, u0, u1, v0, v1, w0, w1):
        """An axis-aligned box in the (u, v, w) frame, as 6 unshared quads."""
        a, b, c, d = (u0, v0, w0), (u1, v0, w0), (u1, v1, w0), (u0, v1, w0)
        a2, b2, c2, d2 = (u0, v0, w1), (u1, v0, w1), (u1, v1, w1), (u0, v1, w1)
        for face in ((a, b, c, d), (a2, d2, c2, b2), (a, a2, b2, b), (d, c, c2, d2), (a, d, d2, a2), (b, b2, c2, c)):
            self.ngon(face)

    def __bool__(self):
        return bool(self.points)


def _axes(stage):
    """(right, forward, up) unit vectors for this stage's up axis — the local (u, v, w)
    frame every builder works in."""
    if UsdGeom.GetStageUpAxis(stage).upper() == "Y":
        return Gf.Vec3d(1, 0, 0), Gf.Vec3d(0, 0, 1), Gf.Vec3d(0, 1, 0)
    return Gf.Vec3d(1, 0, 0), Gf.Vec3d(0, 1, 0), Gf.Vec3d(0, 0, 1)


def _world(axes, u, v, w):
    right, forward, up = axes
    p = right * u + forward * v + up * w
    return Gf.Vec3f(float(p[0]), float(p[1]), float(p[2]))


def _unit(v):
    length = Gf.GetLength(v)
    return v / length if length > 1e-12 else Gf.Vec3d(v)


# ---------------------------------------------------------------------------
# instancing
# ---------------------------------------------------------------------------


def _orient_quat(axes, v_dir, w_dir):
    """The rotation carrying a prototype's canonical (u, v, w) frame onto (·, v_dir, w_dir).

    Prototypes are modelled in the house's own frame — u across the part, v into the wall
    or down the slope, w out of the surface — so an instance only ever needs the rotation
    that takes those three onto the wall or roof plane it belongs to. Pass where v and w
    must end up; the across direction is *derived* rather than passed, because it is not
    free: it has to keep the target basis at the same handedness as the canonical one, or
    the result is a rotation composed with a mirror and every prototype comes out
    inside-out. The canonical triple is left-handed on a Y-up stage and right-handed on a
    Z-up one, so that handedness is measured here rather than assumed.
    """
    right, forward, up = axes
    w_dir = _unit(w_dir)
    # Re-orthogonalise, so a caller's not-quite-in-plane down-slope cannot skew the basis.
    v_dir = _unit(Gf.Vec3d(v_dir) - w_dir * Gf.Dot(Gf.Vec3d(v_dir), w_dir))
    hand = 1.0 if Gf.Dot(Gf.Cross(right, forward), up) > 0 else -1.0
    u_dir = Gf.Cross(v_dir, w_dir) * hand

    canonical = Gf.Matrix3d(
        right[0], right[1], right[2], forward[0], forward[1], forward[2], up[0], up[1], up[2]
    )
    target = Gf.Matrix3d(
        u_dir[0], u_dir[1], u_dir[2], v_dir[0], v_dir[1], v_dir[2], w_dir[0], w_dir[1], w_dir[2]
    )
    # Row-vector convention throughout Gf: a row of a matrix is where that basis vector
    # lands, so canonical * R = target, hence R = canonical^-1 * target = canonical^T * target.
    quat = (canonical.GetTranspose() * target).ExtractRotation().GetQuat()
    return Gf.Quatf(quat.GetReal(), Gf.Vec3f(*quat.GetImaginary()))


def _to_quath(quat):
    im = quat.GetImaginary()
    return Gf.Quath(quat.GetReal(), im[0], im[1], im[2])


def _spin(v_dir, w_dir, angle):
    """`v_dir` turned about `w_dir` by `angle`, both unit and perpendicular.

    Rodrigues with the parallel term dropped, which is exact here precisely because the
    two are perpendicular — it is how a tile gets its hand-laid twist without composing
    quaternions.
    """
    if abs(angle) < 1e-9:
        return v_dir
    return v_dir * math.cos(angle) + Gf.Cross(w_dir, v_dir) * math.sin(angle)


def _author_instancer(stage, path, prototypes, positions, orientations, indices):
    """A PointInstancer plus its prototype meshes, authored under `<path>/Prototypes`.

    Prototypes live beneath the instancer, which is the usual USD arrangement and the one
    `scatter_points` already uses here: an imaging delegate prunes a prim that is only
    reachable as a prototype, so the source geometry never draws on its own.
    """
    instancer = UsdGeom.PointInstancer.Define(stage, path)
    proto_scope = path.AppendChild("Prototypes")
    stage.DefinePrim(proto_scope, "Xform")

    targets = []
    for name, mesh, color in prototypes:
        proto_path = proto_scope.AppendChild(name)
        _author_mesh(stage, proto_path, mesh, color)
        targets.append(proto_path)
    instancer.GetPrototypesRel().SetTargets(targets)

    instancer.CreatePositionsAttr().Set(Vt.Vec3fArray(positions))
    instancer.CreateOrientationsAttr().Set(Vt.QuathArray([_to_quath(q) for q in orientations]))
    instancer.CreateProtoIndicesAttr().Set(Vt.IntArray([int(i) for i in indices]))
    if positions:
        xs = [p[0] for p in positions]
        ys = [p[1] for p in positions]
        zs = [p[2] for p in positions]
        pad = 1.0
        instancer.CreateExtentAttr().Set(
            Vt.Vec3fArray(
                [
                    Gf.Vec3f(min(xs) - pad, min(ys) - pad, min(zs) - pad),
                    Gf.Vec3f(max(xs) + pad, max(ys) + pad, max(zs) + pad),
                ]
            )
        )
    return instancer


# ---------------------------------------------------------------------------
# bays — where every opening is, for both the hole and the thing that fills it
# ---------------------------------------------------------------------------


def _wall_defs(w, d, style):
    """One descriptor per wall the openings are cut into — the axis it runs along, its
    length, its fixed coordinate on the other axis, and which way is outward.

    Mono has no left/right entry on purpose: those walls slope from `h_front` to
    `h_back`, and punching openings into a wall of varying height is more bookkeeping
    than this needs — a shed-roofed house with windows on its two long faces reads as a
    design choice, not an omission.
    """
    walls = [
        dict(name="front", axis="u", length=w, fixed=0.0, sign=-1, has_door=True),
        dict(name="back", axis="u", length=w, fixed=d, sign=1, has_door=False),
    ]
    if style != ROOF_MONO:
        walls += [
            dict(name="left", axis="v", length=d, fixed=0.0, sign=-1, has_door=False),
            dict(name="right", axis="v", length=d, fixed=w, sign=1, has_door=False),
        ]
    return walls


def _layout_bays(walls, s):
    """Every opening in the house, once.

    Both passes read this: the one that cuts the holes and the one that fills them. Two
    separate derivations of "where the windows go" would be the same expression written
    twice, and the failure when they drift is a frame floating in front of solid wall.
    """
    w = s["width"]
    spacing = s["window_spacing"]
    floors = s["floors"]
    wall_h = s["wall_height"]
    dw, dh = s["door_width"], s["door_height"]
    clearance = max(0.2, s["frame_bar"] * 2.0)
    crooked = s["crooked"]
    rng = random.Random(_SEED + 101)

    bays = []
    for wall in walls:
        usable = max(0.0, wall["length"] - 2 * clearance)
        n = max(1, round(usable / spacing)) if usable > 1e-6 else 0
        centers = []
        if n:
            step = usable / n
            centers = [clearance + step * (k + 0.5) for k in range(n)]

        door_zone = None
        if wall["has_door"]:
            door_zone = (w / 2 - dw, w / 2 + dw)
            bays.append(dict(wall=wall, p=w / 2, z0=0.0, z1=dh, half=dw / 2, door=True))

        for f in range(floors):
            z0 = f * wall_h + s["sill_height"]
            z1 = z0 + s["window_height"]
            if z1 > (f + 1) * wall_h - s["frame_bar"] * 2.0:
                continue  # would break through the floor above — skip rather than clip
            for p in centers:
                if f == 0 and door_zone and door_zone[0] < p < door_zone[1]:
                    continue
                bays.append(
                    dict(
                        wall=wall,
                        p=p,
                        z0=z0,
                        z1=z1,
                        half=s["window_width"] / 2,
                        door=False,
                        spin=rng.uniform(-1.0, 1.0) * crooked * _WINDOW_SPIN,
                        slide=rng.uniform(-1.0, 1.0) * crooked * s["frame_bar"] * 0.4,
                        rise=rng.uniform(-1.0, 1.0) * crooked * s["frame_bar"] * 0.4,
                    )
                )
    return bays


def _hole_slack(s):
    """How much smaller than the architrave each opening is cut.

    A crooked frame in a square hole swings its corners out past the edges; cut the hole
    to the frame's true size and the gap shows on the far side of every one. Undersizing
    it by the worst-case swing means the architrave always laps the boards instead —
    which is what an architrave does anyway. Capped well inside the architrave's width,
    so the frame is never cut narrower than the hole it fills.
    """
    face = s["frame_bar"] * _ARCHITRAVE
    half_diagonal = math.hypot(s["window_width"] / 2 + face, s["window_height"] / 2 + face)
    swing = s["crooked"] * (_WINDOW_SPIN * half_diagonal + s["frame_bar"] * 0.4)
    return min(swing, face * 0.6)


def _wall_holes(wall, bays, bar, slack=0.0):
    """The rectangles to cut out of one wall: (u_lo, u_hi, z_lo, z_hi).

    The hole is the frame's *outer* extent, not the glass's — the frame fills the hole
    exactly, so the two are derived from the same half-width and the same bar.
    """
    holes = []
    for bay in bays:
        if bay["wall"] is not wall:
            continue
        face = bar if bay["door"] else bar * _ARCHITRAVE
        give = 0.0 if bay["door"] else slack
        z_lo = -0.02 if bay["door"] else bay["z0"] - face + give
        holes.append(
            (
                bay["p"] - bay["half"] - face + give,
                bay["p"] + bay["half"] + face - give,
                z_lo,
                bay["z1"] + face - give,
            )
        )
    return holes


def _solid_spans(rows, height):
    """The z ranges left solid once a column's openings are taken out of it.

    Shared by the wall panels and the plank grooves, so a groove line stops at exactly
    the edge the wall does instead of running on across the glass.
    """
    spans = []
    cursor = 0.0
    for z_lo, z_hi in sorted(rows):
        if z_lo > cursor:
            spans.append((cursor, z_lo))
        cursor = max(cursor, z_hi)
    if cursor < height:
        spans.append((cursor, height))
    return spans


def _build_wall_panel(mesh, wall, height, thickness, holes):
    """One wall as a slab with its openings cut out.

    Columns first, then rows within each column. Doing only the columns would be enough
    for a bungalow and wrong for anything taller: a single "solid above this window"
    panel reaching the roofline would fill in the window on the floor above. So openings
    whose u ranges touch are merged into one column and that column is punched against
    its own stacked z gaps.

    Each surviving strip is a whole box rather than a pair of skinned faces, which is
    what gives the opening its reveal for free — the strips bordering a hole already
    carry the faces that face into it.
    """
    length, fixed, sign = wall["length"], wall["fixed"], wall["sign"]
    inner = fixed - sign * thickness
    v_lo, v_hi = min(fixed, inner), max(fixed, inner)
    def panel(u0, u1, z0, z1):
        if u1 - u0 <= 1e-6 or z1 - z0 <= 1e-6:
            return
        if wall["axis"] == "u":
            mesh.box(u0, u1, v_lo, v_hi, z0, z1)
        else:
            mesh.box(v_lo, v_hi, u0, u1, z0, z1)

    columns: list = []
    for u_lo, u_hi, z_lo, z_hi in sorted(holes):
        u_lo, u_hi = max(0.0, u_lo), min(length, u_hi)
        if u_hi - u_lo <= 1e-6:
            continue
        if columns and u_lo <= columns[-1][1] + 1e-6:
            columns[-1][1] = max(columns[-1][1], u_hi)
            columns[-1][2].append((z_lo, z_hi))
        else:
            columns.append([u_lo, u_hi, [(z_lo, z_hi)]])

    cursor = 0.0
    for u_lo, u_hi, rows in columns:
        if u_lo > cursor:
            panel(cursor, u_lo, 0.0, height)
        for z0, z1 in _solid_spans(rows, height):
            panel(u_lo, u_hi, z0, z1)
        cursor = max(cursor, u_hi)
    if cursor < length:
        panel(cursor, length, 0.0, height)


def _mono_end_walls(mesh, mesh_planks, w, d, h_front, h_back, spacing):
    """The two sloped, window-less end walls under a Mono roof — flat trapezoids, since
    nothing is ever punched into them, grooved to the roofline like every other wall."""
    for u, sign in ((0.0, -1), (w, 1)):
        mesh.ngon([(u, 0.0, 0.0), (u, d, 0.0), (u, d, h_back), (u, 0.0, h_front)])
        _gable_grooves(mesh_planks, u, sign, [(0.0, h_front), (d, h_back)], 0.0, spacing, d)


def _add_corner_posts(mesh_trim, w, d, h_front, h_back, trim_width):
    half = trim_width / 2.0
    for cu, cv, ch in ((0.0, 0.0, h_front), (w, 0.0, h_front), (0.0, d, h_back), (w, d, h_back)):
        mesh_trim.box(cu - half, cu + half, cv - half, cv + half, 0.0, ch)


#: How much wider the window architrave is than the plain trim bar.
_ARCHITRAVE = 2.2

#: How far a siding board stands off the wall behind it, as a share of its width, and how
#: much of that width the shadow gap between two boards takes.
_BOARD_DEPTH = 0.16
_BOARD_GAP = 0.17


def _add_siding(mesh, walls, bays, s, heights, bar):
    """Vertical boards standing off the wall, one per board — not scored lines.

    The wall slab behind them is painted a much darker shade of the same red and is only
    ever seen *between* the boards, so each gap reads as the shadow in a board joint. That
    inversion is the whole trick: with flat shading and no shadowing, a groove scratched
    into a flat wall has the same normal as the wall and disappears, whereas a board with
    real depth has two side faces that catch the light differently and a dark ground
    behind it. It is what makes the siding read as timber rather than as a striped
    surface.

    Boards are split around the openings through the same `_solid_spans` the wall itself
    is punched with, so one stops at a window instead of running across the glass. A board
    only partly overlapping an opening is cut anyway and the architrave, which stands
    proud of the boards, covers the join.
    """
    pitch = s["plank_spacing"]
    if pitch <= 0.0:
        return
    depth = pitch * _BOARD_DEPTH
    width = pitch * (1.0 - _BOARD_GAP)

    for wall in walls:
        height = heights[wall["name"]]
        holes = _wall_holes(wall, bays, bar)
        fixed, sign = wall["fixed"], wall["sign"]
        v0, v1 = fixed, fixed + sign * depth
        v_lo, v_hi = min(v0, v1), max(v0, v1)

        n = max(1, round(wall["length"] / pitch))
        step = wall["length"] / n
        for k in range(n):
            p = (k + 0.5) * step
            rows = [(z_lo, z_hi) for u_lo, u_hi, z_lo, z_hi in holes if u_lo - width / 2 <= p <= u_hi + width / 2]
            for z0, z1 in _solid_spans(rows, height):
                if wall["axis"] == "u":
                    mesh.box(p - width / 2, p + width / 2, v_lo, v_hi, z0, z1)
                else:
                    mesh.box(v_lo, v_hi, p - width / 2, p + width / 2, z0, z1)


# ---------------------------------------------------------------------------
# windows — one prototype, instanced into every opening
# ---------------------------------------------------------------------------


def _window_prototypes(axes, s):
    """The window, modelled once: a frame and a pane of glass.

    Canonical frame — u along the wall from the bay centre, v into the wall from its
    outer face, w up from the bottom of the opening. Built at real size, so every
    instance is the same window turned to face its own wall and no instance needs a scale.

    The frame is a *ring* of four bars carried through the wall's whole thickness, not a
    slab over the hole. That is the whole difference between this and a coloured
    rectangle: the bars' inward faces become the reveal, the glass sits back at `pane_v`
    behind them, and the opening then shades and parallaxes like a hole because it is one.
    """
    frame = _Mesh(axes)
    glass = _Mesh(axes)

    hw = s["window_width"] * 0.5
    wh = s["window_height"]
    bar = s["frame_bar"]
    thick = s["wall_thickness"]
    board = s["plank_spacing"] * _BOARD_DEPTH
    # The architrave is a wide flat board laid *over* the siding, not a thin bead around
    # the hole, and it has to stand proud of the boards or they cut across its edge. That
    # depth is most of why the openings read heavy rather than drawn on.
    face = bar * _ARCHITRAVE
    lip = board + bar * 0.45
    v0, v1 = -lip, thick
    # The origin sits at the middle of the opening, not at its sill, so the whole window
    # is symmetric about its own anchor and can be placed by its centre.
    mid = wh * 0.5

    frame.box(-hw - face, hw + face, v0, v1, wh - mid, wh + face - mid)  # head
    frame.box(-hw - face, hw + face, v0, v1, -face - mid, -mid)  # apron
    frame.box(-hw - face, -hw, v0, v1, -mid, wh - mid)  # left jamb
    frame.box(hw, hw + face, v0, v1, -mid, wh - mid)  # right jamb

    # Muntins at the glass line rather than at the wall face, so the reveal in front of
    # them stays visible and the bars read as being *inside* the opening.
    pane_v = thick * 0.62
    mb = bar * 0.42
    frame.box(-mb * 0.5, mb * 0.5, pane_v - mb * 0.6, pane_v + mb * 0.6, -mid, wh - mid)
    frame.box(-hw, hw, pane_v - mb * 0.6, pane_v + mb * 0.6, -mb * 0.5, mb * 0.5)

    # A deep sill that oversails the architrave, the one part that breaks the wall plane
    # hard enough to throw a line right across the boards below it.
    frame.box(-hw - face * 1.15, hw + face * 1.15, -lip - bar * 1.2, 0.0, -face * 1.6 - mid, -face * 0.95 - mid)

    if s["shutters"]:
        _add_shutters(frame, hw, wh, mid, face, lip, bar)

    glass.box(-hw, hw, pane_v - 0.01, pane_v + 0.01, -mid, wh - mid)
    return frame, glass


def _add_shutters(mesh, hw, wh, mid, face, lip, bar):
    """A panelled shutter either side of the opening, folded back against the wall.

    Slats rather than a flat leaf: three raised bars across each, which is enough relief
    to keep a shutter from reading as a plain rectangle of trim stuck to the siding.
    """
    leaf = hw * 0.52
    thickness = bar * 0.45
    for side in (-1.0, 1.0):
        near = side * (hw + face)
        far = near + side * leaf
        u_lo, u_hi = min(near, far), max(near, far)
        mesh.box(u_lo, u_hi, -lip - thickness, -lip, -face - mid, wh + face - mid)
        for k in range(3):
            span = (wh + 2 * face) / 3.0
            base = -face - mid + span * (k + 0.35)
            mesh.box(
                u_lo + thickness, u_hi - thickness,
                -lip - thickness * 1.8, -lip - thickness,
                base, base + span * 0.42,
            )


def _inward(wall):
    """The wall's into-the-house direction, in (u, v, w)."""
    if wall["axis"] == "u":
        return (0.0, -wall["sign"], 0.0)
    return (-wall["sign"], 0.0, 0.0)


def _window_instances(axes, bays):
    """Positions and orientations for every window opening, frame and glass alike.

    Two entries per bay at the same transform, one per prototype — the frame and the
    glass are separate prims only because a prim carries a single flat colour.
    """
    right, forward, up = axes
    positions, orientations, indices = [], [], []
    for bay in bays:
        if bay["door"]:
            continue
        wall = bay["wall"]
        p = bay["p"] + bay.get("slide", 0.0)
        z = (bay["z0"] + bay["z1"]) * 0.5 + bay.get("rise", 0.0)
        if wall["axis"] == "u":
            anchor = (p, wall["fixed"], z)
        else:
            anchor = (wall["fixed"], p, z)
        iu, iv, iw = _inward(wall)
        inward = right * iu + forward * iv + up * iw
        # Tipped in the plane of its own wall, by turning `up` about the wall's normal.
        quat = _orient_quat(axes, inward, _spin(up, _unit(-inward), bay.get("spin", 0.0)))
        world = _world(axes, *anchor)
        for proto in (0, 1):
            positions.append(world)
            orientations.append(quat)
            indices.append(proto)
    return positions, orientations, indices


def _build_door(mesh_trim, mesh_door, wall, s):
    """The door, authored in place rather than instanced — there is only ever one.

    Same anatomy as a window: a ring through the wall's thickness, and the slab set back
    inside it, so the doorway is a recess rather than a panel stuck to the wall.
    """
    p = s["width"] / 2.0
    dw, dh = s["door_width"], s["door_height"]
    bar = s["frame_bar"]
    thick = s["wall_thickness"]
    fixed, sign = wall["fixed"], wall["sign"]
    v0, v1 = fixed - sign * bar * 0.35, fixed + sign * thick
    v_lo, v_hi = min(v0, v1), max(v0, v1)

    def bar_box(u0, u1, z0, z1):
        if wall["axis"] == "u":
            mesh_trim.box(u0, u1, v_lo, v_hi, z0, z1)
        else:
            mesh_trim.box(v_lo, v_hi, u0, u1, z0, z1)

    bar_box(p - dw / 2 - bar, p + dw / 2 + bar, dh, dh + bar)  # head
    bar_box(p - dw / 2 - bar, p - dw / 2, 0.0, dh)  # left jamb
    bar_box(p + dw / 2, p + dw / 2 + bar, 0.0, dh)  # right jamb

    slab_v = fixed + sign * -thick * 0.55
    s0, s1 = slab_v - sign * 0.03, slab_v + sign * 0.03
    slab_lo, slab_hi = min(s0, s1), max(s0, s1)

    def door_box(mesh, u0, u1, z0, z1, lo, hi):
        if wall["axis"] == "u":
            mesh.box(u0, u1, lo, hi, z0, z1)
        else:
            mesh.box(lo, hi, u0, u1, z0, z1)

    door_box(mesh_door, p - dw / 2, p + dw / 2, 0.0, dh, slab_lo, slab_hi)

    # Raised panels: flat colour cannot show a moulding, but the faces these add catch
    # light differently, which reads as one.
    cols, rows, inset = 2, 3, 0.09
    pw = (dw - inset * (cols + 1)) / cols
    ph = (dh - inset * (rows + 1)) / rows
    face = slab_v - sign * 0.035
    p_lo, p_hi = min(slab_hi, face), max(slab_hi, face)
    if pw > 0.0 and ph > 0.0:
        for r in range(rows):
            for c in range(cols):
                cx = p - dw / 2 + inset * (c + 1) + pw * (c + 0.5)
                cz = inset * (r + 1) + ph * (r + 0.5)
                door_box(mesh_door, cx - pw / 2, cx + pw / 2, cz - ph / 2, cz + ph / 2, p_lo, p_hi)

    knob = 0.04
    kx, kz = p + dw * 0.32, dh * 0.45
    k0 = face
    k1 = face - sign * 0.05
    door_box(mesh_trim, kx - knob, kx + knob, kz - knob, kz + knob, min(k0, k1), max(k0, k1))


# ---------------------------------------------------------------------------
# roofs — each builder returns (planes, ridge_a, ridge_b)
# ---------------------------------------------------------------------------


def _roof_profile(style, depth, pitch):
    """The roof's cross-section, front eave to back eave, as (v_fraction, height) pairs.

    Gable and Saltbox are the same shape with a different ridge split: centred is a
    Gable, off-centre a Saltbox, whose long low slope is simply whichever side got the
    bigger share. The steeper side's pitch is then *implied* by that split rather than
    set — real saltboxes work the same way, an added-on roof sharing one ridge height
    with the steeper original.
    """
    if style == ROOF_SALTBOX:
        ridge_frac = 0.32
        front_run, back_run = ridge_frac * depth, (1.0 - ridge_frac) * depth
        rise = max(front_run, back_run) * math.tan(pitch)
        return [(0.0, 0.0), (ridge_frac, rise), (1.0, 0.0)]
    if style == ROOF_GAMBREL:
        rise = depth / 2.0 * math.tan(pitch)
        knee = rise * 0.45
        return [(0.0, 0.0), (0.22, knee), (0.5, rise), (0.78, knee), (1.0, 0.0)]
    rise = depth / 2.0 * math.tan(pitch)
    return [(0.0, 0.0), (0.5, rise), (1.0, 0.0)]


def _eave_board(mesh, u0, u1, v, z, depth, drop):
    """The dark board closing the lower edge of a roof slope.

    Both references read the roof edge as a hard dark line under the tiles rather than as
    the tiles simply stopping, and the tiles alone cannot give it: they end on a bare
    plane and the eave dissolves. This is the board they overhang.
    """
    mesh.box(u0, u1, v - depth * 0.5, v + depth * 0.5, z - drop, z + depth * 0.4)


def _rake_board(mesh, gu, sign, a, b, board_width):
    """A trim board following one sloped segment of a gable end exactly — a flat quad
    offset along its own perpendicular rather than a world-aligned box, so it reads as a
    clean diagonal rather than a staircase."""
    (v_a, z_a), (v_b, z_b) = a, b
    dv, dz = v_b - v_a, z_b - z_a
    length = math.hypot(dv, dz) or 1.0
    nv, nz = -dz / length, dv / length
    hw = board_width / 2.0
    u_out = gu + sign * 0.02
    mesh.ngon(
        [
            (u_out, v_a - nv * hw, z_a - nz * hw),
            (u_out, v_a + nv * hw, z_a + nz * hw),
            (u_out, v_b + nv * hw, z_b + nz * hw),
            (u_out, v_b - nv * hw, z_b - nz * hw),
        ]
    )


def _profile_height(pts, v):
    """The roofline height at one depth, by walking the profile — what a groove on the
    gable end has to stop at."""
    for (v_a, z_a), (v_b, z_b) in zip(pts, pts[1:]):
        if v_a - 1e-9 <= v <= v_b + 1e-9 and abs(v_b - v_a) > 1e-9:
            return z_a + (z_b - z_a) * (v - v_a) / (v_b - v_a)
    return pts[0][1]


def _gable_grooves(mesh_planks, gu, sign, pts, wall_top, pitch, depth):
    """Carry the wall's boards up over the gable end to the roofline.

    Same pitch and the same positions the side wall uses, so a board does not stop dead at
    the wall plate and restart somewhere else above it — the gable is the top of the same
    boards.
    """
    if pitch <= 0.0:
        return
    board = pitch * (1.0 - _BOARD_GAP)
    proud = pitch * _BOARD_DEPTH
    v0, v1 = (gu - proud, gu) if sign < 0 else (gu, gu + proud)
    n = max(1, round(depth / pitch))
    step = depth / n
    for k in range(n):
        p = (k + 0.5) * step
        top = _profile_height(pts, p)
        if top - wall_top > 1e-4:
            mesh_planks.box(v0, v1, p - board / 2, p + board / 2, wall_top, top)


def _build_profile_roof(
    mesh_roof,
    mesh_walls,
    mesh_trim,
    mesh_planks,
    mesh_edge,
    w,
    d,
    wall_top,
    overhang,
    profile,
    trim_width,
    plank_spacing,
):
    """Gable, Saltbox and Gambrel: a deck plane per profile segment, extended past the
    wall at the same slope so an eave dips below the wall top the way a real one does,
    plus a gable-end cap tracing the same profile, its groove lines, and a rake board
    along each sloped edge.
    """
    pts = [(f * d, wall_top + h) for f, h in profile]
    n = len(pts)
    u0, u1 = -overhang, w + overhang
    planes = []

    for i in range(n - 1):
        v_a, z_a = pts[i]
        v_b, z_b = pts[i + 1]
        av, az, bv, bz = v_a, z_a, v_b, z_b
        slope = (z_b - z_a) / (v_b - v_a) if abs(v_b - v_a) > 1e-9 else 0.0
        if i == 0:
            av, az = -overhang, z_a - slope * overhang
        if i == n - 2:
            bv, bz = d + overhang, z_b + slope * overhang
        plane = [(u0, av, az), (u1, av, az), (u1, bv, bz), (u0, bv, bz)]
        mesh_roof.ngon(plane)
        planes.append(plane)
        if i == 0:
            _eave_board(mesh_edge, u0, u1, av, az, trim_width * 1.5, trim_width * 2.2)
        if i == n - 2:
            _eave_board(mesh_edge, u0, u1, bv, bz, trim_width * 1.5, trim_width * 2.2)

    for gu, sign in ((0.0, -1), (w, 1)):
        mesh_walls.ngon([(gu, v, z) for v, z in pts])
        _gable_grooves(mesh_planks, gu, sign, pts, wall_top, plank_spacing, d)
        for i in range(n - 1):
            _rake_board(mesh_edge, gu, sign, pts[i], pts[i + 1], trim_width * 1.9)

    peak_v, peak_z = max(pts, key=lambda p: p[1])
    return planes, (0.0, peak_v, peak_z), (w, peak_v, peak_z)


def _build_hip_roof(mesh_roof, w, d, wall_top, overhang, pitch):
    """Four planes to one ridge — or, when the footprint is deeper than it is wide, to a
    single apex, which `_Mesh.ngon`'s dedupe turns the degenerate trapezoids into clean
    triangles for. No gable ends, which makes Hip the simplest wall case of the five."""
    rise = d / 2.0 * math.tan(pitch)
    ridge_z = wall_top + rise
    ridge_half = max(0.0, (w - d) / 2.0)
    cx = w / 2.0
    ridge_a = (cx - ridge_half, d / 2.0, ridge_z)
    ridge_b = (cx + ridge_half, d / 2.0, ridge_z)

    u0, u1, v0, v1 = -overhang, w + overhang, -overhang, d + overhang
    fl, fr = (u0, v0, wall_top), (u1, v0, wall_top)
    br, bl = (u1, v1, wall_top), (u0, v1, wall_top)

    planes = [
        [fl, fr, ridge_b, ridge_a],
        [br, bl, ridge_a, ridge_b],
        [fl, ridge_a, bl],
        [fr, br, ridge_b],
    ]
    for plane in planes:
        mesh_roof.ngon(plane)
    return planes, ridge_a, ridge_b


def _build_mono_roof(
    mesh_roof,
    mesh_walls,
    mesh_trim,
    mesh_planks,
    mesh_edge,
    w,
    d,
    wall_top,
    overhang,
    pitch,
    trim_width,
    plank_spacing,
):
    """A single flat slope, front wall short and back wall tall — the one style whose
    walls are not uniform height."""
    rise = d * math.tan(pitch)
    h_front, h_back = wall_top, wall_top + rise
    _mono_end_walls(mesh_walls, mesh_planks, w, d, h_front, h_back, plank_spacing)
    _add_corner_posts(mesh_trim, w, d, h_front, h_back, trim_width)

    slope = rise / d if d else 0.0
    u0, u1 = -overhang, w + overhang
    v0, v1 = -overhang, d + overhang
    z0, z1 = h_front - slope * overhang, h_back + slope * overhang
    plane = [(u0, v0, z0), (u1, v0, z0), (u1, v1, z1), (u0, v1, z1)]
    mesh_roof.ngon(plane)
    _eave_board(mesh_edge, u0, u1, v0, z0, trim_width * 1.5, trim_width * 2.2)
    return [plane], (0.0, d, h_back), (w, d, h_back)


# ---------------------------------------------------------------------------
# roof tiles and ridge caps — instanced
# ---------------------------------------------------------------------------


#: How far a tile's body stands off the deck, as a share of its size. The skirt spans
#: exactly this, so it is also the depth of the shadow line at every course.
_TILE_STANDOFF = 0.14

#: A pantile's cross-section, as (across the tile, height) in fractions of its width and
#: camber. Emphatically not a semicircle: an arch spanning the whole width is a half
#: cylinder, and a roof of them reads as a stack of pipes. A real pantile is an S — most
#: of its width is a shallow, nearly flat **pan**, with a single raised **roll** near one
#: edge. Laid side by side that alternates pan, roll, pan, roll, which is the wave an
#: actual tiled roof has. Both ends sit at the same height so neighbours meet flush.
_PANTILE_SECTION = (
    (0.00, 0.22),
    (0.08, 0.07),
    (0.22, 0.01),
    (0.38, 0.00),
    (0.52, 0.06),
    (0.62, 0.30),
    (0.72, 0.66),
    (0.82, 0.91),
    (0.90, 1.00),
    (0.96, 0.78),
    (1.00, 0.22),
)

#: How much higher a tile's nose sits than its head, as a share of its size. This is the
#: whole reason courses can lap: the head of the course below slides *under* the nose of
#: the one above without the two ever crossing, and the step between them is the shadow
#: line a tiled roof is mostly read by.
_TILE_RISE = 0.20

#: Course pitch, as a share of the tile — the rest is the lap.
_TILE_LAP = 0.66


def _tile_prototype(axes, size, camber):
    """One clay pantile: a barrel arched across its width, standing off the deck, with a
    skirt closing its downslope edge.

    Canonical frame — u across the tile, v down the slope, w out of the roof.

    The shape is what lets tiles *abut* instead of lapping. A lapped tile is what a real
    roof does and the wrong thing to model: geometry that overlaps by a third of its width
    and is then jittered does not interlock, it interpenetrates, and a deep arch cutting
    through its neighbour is the mess that results. So the relief comes out of the tile
    rather than out of the overlap — the body floats one standoff above the deck and the
    skirt drops back down to it at the leading edge, which puts a vertical face at every
    course boundary. That face is the shadow line a lap would have given, and the next
    course starts exactly where it ends, touching but never crossing.

    The camber is deep rather than token: repeated down a column — courses are laid in
    line, not bonded like brick — it is what gives a pantile roof its vertical ribbing.
    A shallow one reads as fish scales.
    """
    mesh = _Mesh(axes)
    hw, hl = size * 0.5, size * 0.5
    standoff = size * _TILE_STANDOFF
    rise = size * _TILE_RISE
    section = [(-hw + size * u_frac, camber * w_frac) for u_frac, w_frac in _PANTILE_SECTION]

    for (u_a, c_a), (u_b, c_b) in zip(section, section[1:]):
        # Ramped head to nose. Every quad stays planar — its height is a function of `u`
        # plus a function of `v`, so opposite corners still sum equal — which is what lets
        # the tiler keep treating a tile as something that sits on a flat plane.
        mesh.ngon(
            [
                (u_a, -hl, standoff + c_a),
                (u_b, -hl, standoff + c_b),
                (u_b, hl, standoff + rise + c_b),
                (u_a, hl, standoff + rise + c_a),
            ]
        )
        # The nose drops back exactly as far as it rose, so it lands level with the head of
        # the course it covers: a closed edge, and no geometry pushed through anything.
        mesh.ngon(
            [
                (u_a, hl, standoff + rise + c_a),
                (u_b, hl, standoff + rise + c_b),
                (u_b, hl, standoff + c_b),
                (u_a, hl, standoff + c_a),
            ]
        )
    return mesh


def _ridge_prototype(axes, size, length, drop):
    """One ridge cap, as a closed shell. Canonical frame — u along the ridge, v across it,
    w up. Every roof style's ridge runs parallel to the width axis, so these never need
    turning: the instances are a translation apiece.

    Closed on all six sides, which an arch alone is not. An open half-tube shows straight
    through itself at the end of the run — at a gable that is a hole looking out over the
    overhang, and the cap reads as floating rather than as sitting on anything. The side
    skirts drop `drop` below the cap so it laps the top course of tiles instead of hovering
    clear of them, and the two end faces close the tube.
    """
    mesh = _Mesh(axes)
    r, hl = size * 0.5, length * 0.5
    segs = 8
    profile = [
        (-math.cos(math.pi * i / segs) * r, math.sin(math.pi * i / segs) * r * 0.85) for i in range(segs + 1)
    ]

    for i in range(segs):
        v_a, w_a = profile[i]
        v_b, w_b = profile[i + 1]
        mesh.ngon([(-hl, v_a, w_a), (hl, v_a, w_a), (hl, v_b, w_b), (-hl, v_b, w_b)])

    for v_edge in (profile[0][0], profile[-1][0]):
        mesh.ngon([(-hl, v_edge, 0.0), (hl, v_edge, 0.0), (hl, v_edge, -drop), (-hl, v_edge, -drop)])

    for end in (-hl, hl):
        mesh.ngon(
            [(end, v, w) for v, w in profile] + [(end, profile[-1][0], -drop), (end, profile[0][0], -drop)]
        )
    return mesh


def _polygon_normal(points):
    """Newell's normal — robust on the slightly non-planar quads an overhang can produce,
    where a single cross product of two edges is not."""
    nx = ny = nz = 0.0
    n = len(points)
    for i in range(n):
        x0, y0, z0 = points[i]
        x1, y1, z1 = points[(i + 1) % n]
        nx += (y0 - y1) * (z0 + z1)
        ny += (z0 - z1) * (x0 + x1)
        nz += (x0 - x1) * (y0 + y1)
    return _unit(Gf.Vec3d(nx, ny, nz))


def _inside(polygon2d, x, y):
    """Ray-cast point-in-polygon, so one gridding routine covers a Gable's rectangle, a
    Gambrel's bands and a Hip's triangles without knowing which it has."""
    inside = False
    n = len(polygon2d)
    for i in range(n):
        x0, y0 = polygon2d[i]
        x1, y1 = polygon2d[(i + 1) % n]
        if (y0 > y) != (y1 > y):
            if x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
                inside = not inside
    return inside


def _tile_plane(axes, plane, size, jitter, rng, budget):
    """Lay courses of tiles over one roof plane: (position, orientation) per tile.

    Works off the plane's own polygon rather than off which roof style produced it, so
    every style is tiled by this one routine. The plane's frame is derived from it: the
    normal by Newell, the down-slope as the steepest descent within the plane, and the
    across direction as their cross product — which is the same vector `_orient_quat`
    derives for the instance, so the grid and the tiles it places cannot end up rotated
    90 degrees from each other.

    Courses overlap — the step is a fraction of the tile — since a tile that merely abuts
    its neighbour leaves a seam the moment anything jitters.
    """
    normal = _polygon_normal(plane)
    if Gf.GetLength(normal) < 1e-9:
        return []
    if normal[2] < 0.0:
        normal = -normal  # the outward side of a roof is the up-facing one

    down = Gf.Vec3d(0, 0, -1)
    downslope = Gf.Vec3d(down) - normal * Gf.Dot(down, normal)
    if Gf.GetLength(downslope) < 1e-6:
        downslope = Gf.Vec3d(0, 1, 0)  # a dead-flat plane has no steepest descent
    downslope = _unit(downslope)
    across = _unit(Gf.Cross(downslope, normal))

    pts = [Gf.Vec3d(*p) for p in plane]
    xs = [Gf.Dot(p, across) for p in pts]
    ys = [Gf.Dot(p, downslope) for p in pts]
    polygon2d = list(zip(xs, ys))
    offset = normal * Gf.Dot(pts[0], normal)

    # Across the slope tiles abut: the section is the same height at both of its own
    # edges, so neighbours in a course meet without crossing. Down the slope they *lap* —
    # the tile is ramped, so the head of each course passes under the nose of the one
    # above it and the step between them is what throws the course shadow.
    step_x = max(size, 1e-3)
    step_y = max(size * _TILE_LAP, 1e-3)

    # Snapped to one lattice shared by every plane, rather than started from each
    # polygon's own corner. A sagging roof is cut into bands so each stays flat, and
    # bands whose grids each began at their own edge put their courses at different
    # heights — the seams between them were the whole reason the roof read as scrambled.
    # Quantising the start to a multiple of the step makes every band agree.
    x_start = math.floor(min(xs) / step_x) * step_x + step_x * 0.5
    y_start = math.floor(min(ys) / step_y) * step_y + step_y * 0.5
    cols = max(1, int(math.ceil((max(xs) - x_start) / step_x)) + 1)

    right, forward, up = axes
    normal_world = _unit(right * normal[0] + forward * normal[1] + up * normal[2])
    downslope_world = _unit(right * downslope[0] + forward * downslope[1] + up * downslope[2])
    across_world = _unit(right * across[0] + forward * across[1] + up * across[2])

    out = []
    y = y_start
    while y < max(ys) + step_y * 0.5 and len(out) < budget:
        # Columns line up rather than bonding like brick: a pantile interlocks with the
        # one below it, so its roll has to continue straight down the slope. Staggering
        # them breaks every rib into short offset scallops, which reads as fish scales.
        x = x_start
        for _ in range(cols + 1):
            if len(out) >= budget:
                break
            if _inside(polygon2d, x, y):
                # Laid by hand, not by machine. This is where a storybook roof gets its
                # look: courses that wander up and down, tiles that sit askew, nod forward
                # or back, and stand a little proud of their neighbours. Lopsided on
                # purpose — across the slope a shift breaks the rib a column shares, so
                # that axis gets least; along it a shift only wanders the course line,
                # which is the point. The lap is deep enough to absorb all of it.
                jx = rng.uniform(-1.0, 1.0) * jitter * step_x * 0.05
                jy = rng.uniform(-1.0, 1.0) * jitter * step_y * 0.22
                lift = size * 0.008 + rng.uniform(0.0, 1.0) * jitter * size * 0.12
                local = offset + across * (x + jx) + downslope * (y + jy) + normal * lift
                spin = rng.uniform(-1.0, 1.0) * jitter * 0.13
                nod = rng.uniform(-1.0, 1.0) * jitter * 0.11
                n_w = _spin(normal_world, across_world, nod)
                d_w = _spin(_spin(downslope_world, across_world, nod), n_w, spin)
                out.append((_world(axes, local[0], local[1], local[2]), _orient_quat(axes, d_w, n_w)))
            x += step_x
        y += step_y
    return out


def _ridge_instances(axes, ridge_a, ridge_b, nominal, lift, blocked):
    """Caps marching along the ridge: (instances, the length one cap should be built at).

    The pitch is returned rather than assumed so the prototype can be built a little
    shorter than it — closed caps stepped by exactly their own length meet face to face
    and z-fight over the seam, where a small gap reads as the joint between two ridge
    tiles instead.

    `lift` raises them clear of the top course: the ridge line is the *deck's* peak, and
    the tiles meeting there already stand a standoff and a camber above it, so a cap left
    on the line would be buried. `blocked` are (centre, half-width) spans along the ridge
    a chimney already occupies — a cap there would run straight through it.
    """
    ax, ay, az = ridge_a
    bx, by, bz = ridge_b
    span = math.dist((ax, ay, az), (bx, by, bz))
    if span < 1e-6:
        return [], nominal
    count = max(1, int(round(span / max(nominal, 1e-3))))
    pitch = span / count
    identity = Gf.Quatf(1, 0, 0, 0)

    out = []
    for k in range(count + 1):
        t = k / count
        u = ax + (bx - ax) * t
        v = ay + (by - ay) * t
        if any(abs(u - cu) < chalf and abs(v - cv) < chalf for cu, cv, chalf in blocked):
            continue
        out.append((_world(axes, u, v, az + (bz - az) * t + lift), identity))
    # Longer than the pitch, so consecutive caps lap. Ridge tiles are the one part of this
    # roof that *should* overlap: they are closed shells sitting proud of everything, so a
    # lap is swallowed whole, where the gap that stepping them exactly apart leaves is a
    # hole straight down onto the deck.
    return out, pitch * 1.2


#: Brick proportions, as fractions of the chimney's width. A brick is roughly four times
#: as long as it is tall, and stands a little proud of the mortar it is bedded in.
_BRICK_LENGTH = 0.62
_BRICK_HEIGHT = 0.46  # of the brick's own length
_BRICK_DEPTH = 0.07
_BRICK_JOINT = 0.16  # mortar gap, of the brick's height


def _chimney_bricks(axes, u, v, z0, z1, half, size, rng, crooked):
    """Brick courses wrapping one shaft: (instances, per-course brick lengths).

    Two brick lengths, not one, because of the bond. Courses alternate between fitting
    `n` bricks across a face and `n + 1`, which is what puts each course's joints over the
    middle of the course below instead of stacking them into a continuous vertical seam.
    Doing that by sliding a single brick along would push half of one off the end of the
    face; changing how many go in the same span instead keeps every course flush and needs
    no half bricks — at the cost of a second prototype, which is free.

    The run is inset by the brick's own depth at each end so the four faces do not
    interpenetrate at the corners, and the mortar core shows through there as a joint.
    """
    right, forward, up = axes
    brick_len = size * _BRICK_LENGTH
    brick_h = brick_len * _BRICK_HEIGHT
    depth = size * _BRICK_DEPTH
    pitch = brick_h * (1.0 + _BRICK_JOINT)
    span = 2.0 * (half - depth)
    if span <= 0.0 or pitch <= 0.0 or z1 - z0 <= pitch:
        return [], (0.0, 0.0, 0.0)

    courses = max(1, int((z1 - z0) / pitch))
    counts = (max(1, int(round(span / brick_len))),)
    counts += (counts[0] + 1,)
    joint = brick_h * _BRICK_JOINT
    lengths = tuple(span / n - joint for n in counts)

    faces = (((0.0, -1.0), "u"), ((0.0, 1.0), "u"), ((-1.0, 0.0), "v"), ((1.0, 0.0), "v"))

    # Leaned, and laid by hand. A stack built dead plumb out of identical bricks is the
    # one part of the house that always gives the model away. The lean is carried by
    # tilting the `up` the orientation is built from — `_orient_quat` re-squares each
    # face against it — rather than by composing a second rotation onto every brick.
    lean_u, lean_v = 0.82, 0.57  # in the house's own (u, v), not world
    tilt = math.radians(crooked * 4.0)
    up_tilted = _unit(up * math.cos(tilt) + (right * lean_u + forward * lean_v) * math.sin(tilt))
    quats = {face: _orient_quat(axes, right * -face[0] + forward * -face[1], up_tilted) for face, _ in faces}
    shift = math.tan(tilt)

    out = []
    for c in range(courses):
        zc = z0 + c * pitch
        drift = ((zc - z0) * shift * lean_u, (zc - z0) * shift * lean_v)
        which = c % 2
        n = counts[which]
        step = span / n
        for face, axis in faces:
            ou, ov = face
            for k in range(n):
                a = -span / 2.0 + step * (k + 0.5) + rng.uniform(-1.0, 1.0) * crooked * joint * 0.9
                nudge = rng.uniform(-1.0, 1.0) * crooked * joint * 0.5
                if axis == "u":
                    pos = _world(axes, u + a + drift[0], v + ov * half + drift[1], zc + nudge)
                else:
                    pos = _world(axes, u + ou * half + drift[0], v + a + drift[1], zc + nudge)
                out.append((pos, quats[face], which * 2 + rng.randrange(2)))
    return out, (lengths[0], lengths[1], brick_h, depth)


def _place_chimneys(mesh_chimney, axes, ridge_a, ridge_b, count, size, height, offset, rng, crooked):
    """One or two chimneys along the ridge: (brick instances, brick sizes, blocked spans).

    Brick and nothing else. A stone base, corbel and crown are what a real chimney has and
    were the wrong thing here: against a red roof they became a white mass that read as the
    chimney and left the brick as a stripe in the middle of it.

    So what is authored is only the mortar core the courses are bedded on, one brick depth
    inside the face, and a slab closing the top. Everything visible is brick.
    """
    if count <= 0:
        return [], (0.0, 0.0, 0.0, 0.0), []
    ax, ay, az = ridge_a
    bx, by, bz = ridge_b
    ts = [0.5 + offset * 0.35] if count == 1 else [0.22 + 0.56 * k / (count - 1) + offset * 0.2 for k in range(count)]
    half = size / 2.0
    depth = size * _BRICK_DEPTH

    bricks: list = []
    sizes = (0.0, 0.0, 0.0, 0.0)
    blocked: list = []
    for t in ts:
        t = max(0.06, min(0.94, t))
        u = ax + (bx - ax) * t
        v = ay + (by - ay) * t
        z = az + (bz - az) * t
        top = z + height
        core = half - depth

        # Down into the roof far enough that the tiles it interrupts cannot show a gap
        # under it, whatever the pitch. No capping slab: anything sitting on top reads as
        # a lid rather than as a chimney, so the core simply stops flush with the last
        # course and its own top face closes it, inset behind the brick.
        mesh_chimney.box(u - core, u + core, v - core, v + core, z - size * 1.2, top)

        laid, sizes = _chimney_bricks(axes, u, v, z - size * 0.35, top, half, size, rng, crooked)
        bricks += laid
        blocked.append((u, v, half))
    return bricks, sizes, blocked


def _brick_prototype(axes, length, height, depth):
    """One brick. Canonical frame — u along the face, v into the wall, w up."""
    mesh = _Mesh(axes)
    mesh.box(-length / 2.0, length / 2.0, 0.0, depth, 0.0, height)
    return mesh


# ---------------------------------------------------------------------------
# cook
# ---------------------------------------------------------------------------


def _settings(node):
    is_f, is_i, is_s = chunk.is_param_t(float), chunk.is_param_t(int), chunk.is_param_t(str)

    def f(name):
        p = node.get_param(name, is_f)
        return float(p.value) if p is not None and p.value is not None else 0.0

    def i(name):
        p = node.get_param(name, is_i)
        return int(p.value) if p is not None and p.value is not None else 0

    def s(name):
        p = node.get_param(name, is_s)
        return p.value if p is not None else None

    def col(name):
        p = node.get_param(name)
        return tuple(p.value) if p is not None and p.value else (1.0, 1.0, 1.0)

    wall_height = max(f("wall_height"), 1.2)
    floors = max(i("floors"), 1)
    trim_width = max(f("trim_width"), 0.02)

    return {
        "width": max(f("width"), 1.5),
        "depth": max(f("depth"), 1.5),
        "wall_height": wall_height,
        "floors": floors,
        "wall_top": wall_height * floors,
        "roof_style": s("roof_style") or ROOF_GABLE,
        "pitch": math.radians(max(5.0, min(80.0, f("roof_pitch")))),
        "overhang": max(f("overhang"), 0.0),
        "trim_width": trim_width,
        # One definition, read by the frame, the door and the hole each is cut into.
        "frame_bar": max(0.05, trim_width * 0.9),
        "wall_thickness": max(0.02, f("wall_thickness")),
        "plank_spacing": max(f("plank_spacing"), 0.0),
        "shutters": bool(node.get_param("shutters").value) if node.get_param("shutters") else False,
        "tile_size": max(f("tile_size"), 0.0),
        "crooked": max(0.0, min(1.0, f("crooked"))),
        "window_width": max(f("window_width"), 0.2),
        "window_height": max(f("window_height"), 0.2),
        "window_spacing": max(f("window_spacing"), 0.6),
        "sill_height": max(f("sill_height"), 0.0),
        "door_width": 1.0,
        "door_height": 2.05,
        "chimney_count": max(0, min(2, i("chimney_count"))),
        "chimney_size": max(f("chimney_size"), 0.15),
        "chimney_height": max(f("chimney_height"), 0.2),
        "chimney_offset": max(-1.0, min(1.0, f("chimney_offset"))),
        "colors": {
            "Walls": col("wall_color"),
            "Trim": col("trim_color"),
            "Windows": col("window_color"),
            "Door": col("door_color"),
            "Roof": col("roof_color"),
            "Chimney": col("chimney_color"),
        },
    }


def _shade(color, factor):
    return tuple(max(0.0, min(1.0, c * factor)) for c in color)


def _mix(color, other, t):
    return tuple(max(0.0, min(1.0, a + (b - a) * t)) for a, b in zip(color, other))


def _author_mesh(stage, path, mesh, color):
    prim = UsdGeom.Mesh.Define(stage, path)
    prim.CreateSubdivisionSchemeAttr().Set(UsdGeom.Tokens.none)
    prim.CreateDoubleSidedAttr().Set(True)
    if not mesh:
        prim.CreatePointsAttr().Set(Vt.Vec3fArray())
        prim.CreateFaceVertexCountsAttr().Set(Vt.IntArray())
        prim.CreateFaceVertexIndicesAttr().Set(Vt.IntArray())
        return
    prim.CreatePointsAttr().Set(Vt.Vec3fArray(mesh.points))
    prim.CreateFaceVertexCountsAttr().Set(Vt.IntArray(mesh.counts))
    prim.CreateFaceVertexIndicesAttr().Set(Vt.IntArray(mesh.indices))
    UsdGeom.Primvar(prim.CreateDisplayColorAttr()).SetInterpolation(UsdGeom.Tokens.constant)
    prim.GetDisplayColorAttr().Set(Vt.Vec3fArray([Gf.Vec3f(*color)]))
    xs = [p[0] for p in mesh.points]
    ys = [p[1] for p in mesh.points]
    zs = [p[2] for p in mesh.points]
    prim.CreateExtentAttr().Set(
        Vt.Vec3fArray([Gf.Vec3f(min(xs), min(ys), min(zs)), Gf.Vec3f(max(xs), max(ys), max(zs))])
    )


def _clear_layer(layer):
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)


def _report(node, s, windows, tiles):
    text = (
        f"{s['width']:.1f} x {s['depth']:.1f} m · {s['roof_style']} roof · "
        f"{s['floors']} floor{'s' if s['floors'] != 1 else ''}"
    )
    text += f"\n{windows} windows · {tiles} roof tiles"
    if tiles >= _MAX_TILES:
        text += " (capped — raise Tile Size)"
    if readout := node.get_param("readout"):
        readout.value = text
    logger.debug("%s", text.replace(chr(10), " | "))


def execute(node: chunk.Chunk) -> None:
    """Build the house. Reactive: any param change re-runs this from scratch — there is
    no incremental path, because at this geometry budget there is nothing to save by
    having one."""
    stage = node.get_stage()
    if not stage:
        return
    layer = node.get_edit_context()
    if not layer:
        return

    s = _settings(node)
    root_path = Sdf.Path((node.get_param("house_path").value or "").strip() or "/House")

    _clear_layer(layer)

    axes = _axes(stage)
    meshes = {name: _Mesh(axes) for name in PART_NAMES}

    w, d, top = s["width"], s["depth"], s["wall_top"]
    style = s["roof_style"]
    bar = s["frame_bar"]

    # --- roof first: the walls need to know how tall they are under a Mono slope.
    if style == ROOF_MONO:
        planes, ridge_a, ridge_b = _build_mono_roof(
            meshes["Roof"],
            meshes["Walls"],
            meshes["Trim"],
            meshes["Planks"],
            meshes["RoofTrim"],
            w,
            d,
            top,
            s["overhang"],
            s["pitch"],
            s["trim_width"],
            s["plank_spacing"],
        )
        h_front, h_back = top, top + d * math.tan(s["pitch"])
        heights = {"front": h_front, "back": h_back}
    elif style == ROOF_HIP:
        _add_corner_posts(meshes["Trim"], w, d, top, top, s["trim_width"])
        planes, ridge_a, ridge_b = _build_hip_roof(meshes["Roof"], w, d, top, s["overhang"], s["pitch"])
        heights = {"front": top, "back": top, "left": top, "right": top}
    else:
        _add_corner_posts(meshes["Trim"], w, d, top, top, s["trim_width"])
        planes, ridge_a, ridge_b = _build_profile_roof(
            meshes["Roof"],
            meshes["Walls"],
            meshes["Trim"],
            meshes["Planks"],
            meshes["RoofTrim"],
            w,
            d,
            top,
            s["overhang"],
            _roof_profile(style, d, s["pitch"]),
            s["trim_width"],
            s["plank_spacing"],
        )
        heights = {"front": top, "back": top, "left": top, "right": top}

    # --- walls, punched around the openings
    walls = _wall_defs(w, d, style)
    bays = _layout_bays(walls, s)
    for wall in walls:
        _build_wall_panel(
            meshes["Walls"],
            wall,
            heights[wall["name"]],
            s["wall_thickness"],
            _wall_holes(wall, bays, bar, _hole_slack(s)),
        )
    meshes["Walls"].box(0.0, w, 0.0, d, -0.06, 0.0)  # floor slab, so a hollow house has one
    _add_siding(meshes["Planks"], walls, bays, s, heights, bar)

    front = next((wall for wall in walls if wall["has_door"]), None)
    if front is not None:
        _build_door(meshes["Trim"], meshes["Door"], front, s)

    rng = random.Random(_SEED)
    bricks, brick_sizes, blocked = _place_chimneys(
        meshes["Chimney"],
        axes,
        ridge_a,
        ridge_b,
        s["chimney_count"],
        s["chimney_size"],
        s["chimney_height"],
        s["chimney_offset"],
        rng,
        s["crooked"],
    )

    # --- instanced parts
    window_frame, window_glass = _window_prototypes(axes, s)
    win_pos, win_orient, win_index = _window_instances(axes, bays)

    tiles: list = []
    if s["tile_size"] > 0.0:
        for plane in planes:
            if len(tiles) >= _MAX_TILES:
                break
            tiles += _tile_plane(
                axes, plane, s["tile_size"], s["crooked"], rng, _MAX_TILES - len(tiles)
            )
    if s["tile_size"] > 0.0:
        ridges, cap_length = _ridge_instances(
            axes, ridge_a, ridge_b, s["tile_size"], s["tile_size"] * 0.26, blocked
        )
    else:
        ridges, cap_length = [], s["tile_size"]

    colors = dict(s["colors"])
    # Derived rather than exposed: one fewer swatch, and they stay coherent when the
    # colour they hang off is changed.
    # Inverted on purpose: the boards carry the wall's real colour and the slab behind
    # them, visible only in the joints, is the shadow. See `_add_siding`.
    colors["Planks"] = colors["Walls"]
    colors["Walls"] = _shade(colors["Walls"], 0.42)
    # Kept close together. Spread the shades wide and the scatter reads as blotches
    # rather than as clay, and the blotches are loud enough to bury the rolls the
    # tiles are shaped to show.
    tile_colors = [_shade(colors["Roof"], f) for f in (1.0, 1.05, 0.94)]
    colors["RoofTrim"] = _mix(_shade(colors["Roof"], 0.22), (0.10, 0.10, 0.11), 0.6)
    # The Chimney swatch is the *brick*, since that is what is read; the stonework it is
    # bedded in and capped with is a pale, washed-out version of the same clay.
    brick_color = colors["Chimney"]
    # Only the mortar between the courses and the slab closing the top are not brick, so
    # they stay close to it: pale enough to read as a joint, nowhere near white enough to
    # become the thing you see instead of the chimney.
    colors["Chimney"] = _mix(brick_color, (0.44, 0.40, 0.37), 0.4)

    with Usd.EditContext(stage, layer):
        UsdGeom.Xform.Define(stage, root_path)
        for name in PART_NAMES:
            _author_mesh(stage, root_path.AppendChild(name), meshes[name], colors[name])

        _author_instancer(
            stage,
            root_path.AppendChild("Windows"),
            [("Frame", window_frame, colors["Trim"]), ("Glass", window_glass, colors["Windows"])],
            win_pos,
            win_orient,
            win_index,
        )

        tile_mesh = _tile_prototype(axes, s["tile_size"] or 0.3, (s["tile_size"] or 0.3) * 0.50)
        _author_instancer(
            stage,
            root_path.AppendChild("RoofTiles"),
            [(f"Tile{i}", tile_mesh, tile_colors[i]) for i in range(len(tile_colors))],
            [p for p, _ in tiles],
            [q for _, q in tiles],
            [rng.randrange(len(tile_colors)) for _ in tiles],
        )

        # Only enough bigger than a field tile to straddle the ridge and lap the top
        # course on both sides, and drawn from the same three shades -- a ridge is the
        # same clay as the rest of the roof, so it has no business being one flat colour.
        cap_size = (s["tile_size"] or 0.3) * 1.15
        ridge_mesh = _ridge_prototype(axes, cap_size, cap_length or 0.3, cap_size * 0.55)
        _author_instancer(
            stage,
            root_path.AppendChild("RidgeTiles"),
            [(f"Cap{i}", ridge_mesh, tile_colors[i]) for i in range(len(tile_colors))],
            [p for p, _ in ridges],
            [q for _, q in ridges],
            [rng.randrange(len(tile_colors)) for _ in ridges],
        )

        brick_a, brick_b, brick_h, brick_d = brick_sizes
        _author_instancer(
            stage,
            root_path.AppendChild("ChimneyBricks"),
            [
                (f"Brick{n}{t}", _brick_prototype(axes, length, brick_h, brick_d), _shade(brick_color, f))
                for n, length in (("Long", brick_a), ("Short", brick_b))
                for t, f in (("A", 1.0), ("B", 0.84))
            ],
            [p for p, _, _ in bricks],
            [q for _, q, _ in bricks],
            [i for _, _, i in bricks],
        )

    _report(node, s, len(win_pos) // 2, len(tiles))


# ---------------------------------------------------------------------------
# chunk
# ---------------------------------------------------------------------------

_GF = (
    chunk.ChunkFlags.GroupFlags.COLLAPSABLE
    | chunk.ChunkFlags.GroupFlags.LABELED
    | chunk.ChunkFlags.GroupFlags.FRAMED
)


def _collapsed(group):
    group.set_state(chunk.ChunkFlags.State.COLLAPSED)
    return group


def main():
    chunk_api = chunk.Chunk("Swedish House")

    chunk_api.add_param(
        chunk.LineEdit("house_path", "House Path", default_value="/House", tooltip="Where the house is authored.")
    )
    chunk_api.add_param(
        chunk.Label("readout", "not built yet", default_value="not built yet", tooltip="What the last cook produced.")
    )

    footprint = chunk.Group("footprint_grp", "Footprint", flags=_GF)
    footprint.add_params(
        [
            chunk.Slider(
                "width",
                "Width",
                min_value=2.0,
                max_value=30.0,
                default_value=5.9,
                tooltip="Front-facing length — the headline control. Stretching it re-tiles the\n"
                "window bays, re-spans and re-tiles the roof, and carries the chimney with\n"
                "it, rather than just scaling a fixed shape.",
            ),
            chunk.Slider(
                "depth",
                "Depth",
                min_value=2.0,
                max_value=20.0,
                default_value=4.7,
                tooltip="Gable-end depth. What a Saltbox's asymmetric ridge and a Gambrel's\n"
                "cross-slope are measured across.",
            ),
            chunk.Slider(
                "wall_height", "Wall Height", min_value=1.2, max_value=5.0, default_value=3.67,
                tooltip="Height of one floor.",
            ),
            chunk.Slider(
                "floors",
                "Floors",
                min_value=1,
                max_value=4,
                default_value=1,
                is_int=True,
                tooltip="Adds a full window row per floor, rather than stretching one.",
            ),
        ]
    )
    chunk_api.add_param(footprint)

    roof = chunk.Group("roof_grp", "Roof", flags=_GF)
    roof.add_param(
        chunk.Menu(
            "roof_style",
            "Style",
            menu_items=_ROOF_STYLES,
            default_value=ROOF_GABLE,
            tooltip="Gable: classic two-slope ridge roof.\n"
            "Hip: all four sides slope to the ridge — no gable ends.\n"
            "Gambrel: barn-style double pitch.\n"
            "Saltbox: off-centre ridge, one long low slope.\n"
            "Mono: a single flat slope, no ridge at all.",
        )
    )
    roof.add_param(
        chunk.Slider("roof_pitch", "Pitch", min_value=10.0, max_value=70.0, default_value=57.57, tooltip="Roof slope, in degrees.")
    )
    roof.add_param(
        chunk.Slider(
            "overhang", "Overhang", min_value=0.0, max_value=1.5, default_value=0.43,
            tooltip="How far the eaves reach past the walls.",
        )
    )
    roof.add_param(
        chunk.Slider(
            "tile_size",
            "Tile Size",
            min_value=0.0,
            max_value=1.2,
            default_value=0.38,
            tooltip="Size of one instanced clay tile. Courses overlap, so the tiles laid are\n"
            "rather more than the roof area divided by this. 0 leaves the roof untiled.",
        )
    )
    roof.add_param(
        chunk.Slider(
            "crooked",
            "Crooked",
            min_value=0.0,
            max_value=1.0,
            default_value=0.69,
            tooltip="How far off true the house is allowed: roof courses wander and tiles\n"
            "sit askew, windows tip a little in their openings, and the chimney leans\n"
            "and is laid by hand. 0 builds everything square. The walls themselves\n"
            "stay true — warping them is what breaks the tiling.",
        )
    )
    chunk_api.add_param(roof)

    windows = _collapsed(chunk.Group("window_grp", "Windows", flags=_GF))
    windows.add_params(
        [
            chunk.Slider("window_width", "Width", min_value=0.3, max_value=2.0, default_value=0.63),
            chunk.Slider("window_height", "Height", min_value=0.3, max_value=2.5, default_value=1.15),
            chunk.Slider(
                "window_spacing",
                "Spacing",
                min_value=0.8,
                max_value=5.0,
                default_value=2.4,
                tooltip="Target distance between window centres. The count comes from wall\n"
                "length, not from stretching — a wider wall gets more windows, not wider ones.",
            ),
            chunk.Slider("sill_height", "Sill Height", min_value=0.0, max_value=2.0, default_value=1.0),
        ]
    )
    chunk_api.add_param(windows)

    chimney = _collapsed(chunk.Group("chimney_grp", "Chimney", flags=_GF))
    chimney.add_params(
        [
            chunk.Slider("chimney_count", "Count", min_value=0, max_value=2, default_value=1, is_int=True),
            chunk.Slider(
                "chimney_size", "Size", min_value=0.2, max_value=1.2, default_value=0.72,
                tooltip="Fixed real-world size — a chimney does not scale with the house.",
            ),
            chunk.Slider("chimney_height", "Height", min_value=0.3, max_value=3.0, default_value=1.19),
            chunk.Slider(
                "chimney_offset", "Ridge Offset", min_value=-1.0, max_value=1.0, default_value=0.0,
                tooltip="Slides the chimney (or chimneys) along the ridge.",
            ),
        ]
    )
    chunk_api.add_param(chimney)

    detail = _collapsed(chunk.Group("detail_grp", "Detailing", flags=_GF))
    detail.add_params(
        [
            chunk.Slider(
                "trim_width",
                "Trim Width",
                min_value=0.02,
                max_value=0.3,
                default_value=0.12,
                tooltip="Thickness of the corner posts, rake boards and window frames — and,\n"
                "with them, the openings those frames are cut into.",
            ),
            chunk.Slider(
                "wall_thickness",
                "Wall Thickness",
                min_value=0.02,
                max_value=0.6,
                default_value=0.09,
                tooltip="How deep an opening is. The frame carries through the whole thickness\n"
                "and the glass sits at the back of it, so this is what gives a window its\n"
                "reveal instead of leaving it flat against the wall.",
            ),
            chunk.Slider(
                "plank_spacing",
                "Board Width",
                min_value=0.0,
                max_value=1.0,
                default_value=0.18,
                tooltip="Width of one siding board, joint included. The boards stand off a darker\n"
                "wall behind them, so this is also the rhythm of the shadow lines. 0 leaves\n"
                "the walls flat.",
            ),
            chunk.Checkbox(
                "shutters",
                "Shutters",
                default_value=False,
                tooltip="A panelled shutter either side of every window, folded back against\n"
                "the boards.",
            ),
        ]
    )
    chunk_api.add_param(detail)

    materials = _collapsed(chunk.Group("material_grp", "Materials", flags=_GF))
    materials.add_params(
        [
            chunk.ColorPicker("wall_color", "Walls", default_value=(0.52, 0.12, 0.08)),
            chunk.ColorPicker("trim_color", "Trim", default_value=(0.96, 0.96, 0.94)),
            chunk.ColorPicker("window_color", "Glass", default_value=(0.12, 0.13, 0.14)),
            chunk.ColorPicker("door_color", "Door", default_value=(0.20, 0.13, 0.09)),
            chunk.ColorPicker("roof_color", "Roof", default_value=(0.66, 0.28, 0.15)),
            chunk.ColorPicker("chimney_color", "Chimney", default_value=(0.47, 0.20, 0.14)),
        ]
    )
    chunk_api.add_param(materials)

    return chunk_api


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.MODELLING,
        description="""
        Swedish House — builds a Falu-red cottage from a box.

        Width is the headline control: stretching it re-tiles the window bays to
        stay evenly spaced, re-spans and re-tiles the roof, and carries the
        chimney with it, instead of just scaling a fixed shape. Floors adds a
        full window row per floor rather than stretching one.

        The walls are punched, not painted: each opening is cut clean through a
        wall of real thickness, and an instanced window frame fills it with the
        glass set back inside, so a window reads by parallax rather than as a
        rectangle stuck to a flat box.

        Windows, roof tiles and ridge caps are PointInstancers — one small
        prototype placed many times. Tiles are laid course by course over every
        roof plane by one routine that works off the plane's own polygon, so
        Gable, Hip, Gambrel, Saltbox and Mono are all tiled by the same code,
        and they are drawn from three shades with a little per-tile jitter.

        Everything is flat display colour rather than a shader network, so it
        draws the same in usdview and in Blender.
        """,
    )
