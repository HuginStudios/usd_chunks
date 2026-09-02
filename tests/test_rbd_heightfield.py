"""_build_heightfield must describe the collider's real top surface.

Live-reported: an apple lands correctly on a kitchen table, then sinks half-way into it
as it rolls. The heightfield collider had a several-unit-deep phantom trench through the
middle of the table and the apple was rolling into it. See the two tests below for the
two independent defects behind that.
"""
from __future__ import annotations

import pytest
from pxr import Usd, UsdGeom

import scripts.rbd_solver as rbd_mod

if not rbd_mod._HAS_MUJOCO:
    pytest.skip("mujoco not installed in this environment", allow_module_level=True)

# After the guard: a machine without mujoco has no reason to have numpy either,
# and importing it above would error at collection rather than skip.
import numpy as np  # noqa: E402


_X1, _Y1, _Z_TOP, _Z_BOTTOM = 150.0, 120.0, 80.0, 73.0


def _table(stage, path="/table"):
    """A tabletop shaped the way the reported asset actually is: its flat top is ONE big
    quad (four corner vertices, nothing across the middle) while its underside is finely
    tessellated, so the underside contributes far more vertices, spread over the whole
    footprint, than the surface a body actually rests on.
    """
    # Inset a little, the way a real tabletop sits inside its own slightly-proud edge
    # trim, so the collider has genuine relief rather than being perfectly flat (a
    # perfectly flat one correctly declines to be a heightfield at all).
    i0x, i1x, i0y, i1y = 5.0, _X1 - 5.0, 5.0, _Y1 - 5.0
    pts = [(i0x, i0y, _Z_TOP), (i1x, i0y, _Z_TOP), (i1x, i1y, _Z_TOP), (i0x, i1y, _Z_TOP)]
    counts, indices = [4], [0, 1, 2, 3]

    n = 10  # the finely tessellated underside
    base = len(pts)
    for i in range(n + 1):
        for j in range(n + 1):
            pts.append((_X1 * i / n, _Y1 * j / n, _Z_BOTTOM))
    for i in range(n):
        for j in range(n):
            a = base + i * (n + 1) + j
            counts.append(4)
            indices.extend((a, a + 1, a + n + 2, a + n + 1))

    m = UsdGeom.Mesh.Define(stage, path)
    m.GetPointsAttr().Set(pts)
    m.GetFaceVertexCountsAttr().Set(counts)
    m.GetFaceVertexIndicesAttr().Set(indices)
    return m


def test_a_big_flat_low_poly_slab_has_no_phantom_trench_through_its_middle():
    """The whole top of a slab must read at the slab's top height.

    The grid used to be built by binning mesh VERTICES only. A tabletop is a big flat
    slab whose vertices are all around its perimeter, so every cell across the middle
    caught no vertex at all and got nearest-cell-filled from whatever was nearest -- the
    slab's own UNDERSIDE. That put a trench several units deep straight through the
    middle of the collider, which is what the live-reported apple was sinking into.
    Faces are rasterised now, so the middle reads as the real top surface.
    """
    stage = Usd.Stage.CreateInMemory()
    _table(stage, "/table")
    z_top = _Z_TOP

    hf = rbd_mod._build_heightfield(stage.GetPrimAtPath("/table"), up_idx=2, resolution=64)
    assert hf is not None

    N = hf["nrow"]
    world_z = hf["pos"][2] + np.asarray(hf["heights"]).reshape(N, N) * hf["size"][2]

    # The middle 60% -- comfortably inside the top face, nowhere near its edges.
    lo, hi = int(N * 0.2), int(N * 0.8)
    middle = world_z[lo:hi, lo:hi]
    assert middle.min() > z_top - 0.5, (
        f"the middle of the table must sit at its top surface ({z_top}), but the collider "
        f"dips to {middle.min():.2f} -- a phantom trench is back"
    )


def test_the_solid_below_the_surface_scales_with_the_collider():
    """A collider must be solid enough to actually stop a body.

    MuJoCo's hfield size[3] is how far solid material extends BELOW the sampled surface.
    It was a hard-coded 1.0 -- not a length that means anything on its own, since it is
    in whatever unit the stage is authored in (1.0 is a centimetre on a cm-scale kitchen
    set). It only held up because a collider's grid minimum usually sits well under the
    surface a body rests on, leaving a thick slab by accident; a bowl has no such margin
    (its lowest sampled point IS the interior floor a body lands on) and an apple dropped
    into one punched straight through to the catch plane below.
    """
    stage = Usd.Stage.CreateInMemory()
    _table(stage, "/table")

    hf = rbd_mod._build_heightfield(stage.GetPrimAtPath("/table"), up_idx=2, resolution=64)
    assert hf is not None
    assert hf["size"][3] > 1.0, (
        "the solid base must scale with the collider rather than staying at the "
        f"unit-agnostic hard-coded 1.0, got {hf['size'][3]}"
    )


def test_a_dense_mesh_still_builds_and_stays_bounded():
    """The rasterisation pass only handles triangles spanning more than one cell, so a
    dense mesh (nearly every triangle smaller than a cell) skips it almost entirely and
    stays fast -- confirm such a mesh still produces a sane grid rather than being
    skipped or blowing up."""
    stage = Usd.Stage.CreateInMemory()
    n = 60
    pts, faces = [], []
    for i in range(n):
        for j in range(n):
            pts.append((i * 2.0, j * 2.0, 50.0 + 0.01 * (i + j)))
    for i in range(n - 1):
        for j in range(n - 1):
            a, b, c, d = i * n + j, i * n + j + 1, (i + 1) * n + j + 1, (i + 1) * n + j
            faces.append((a, b, c, d))
    m = UsdGeom.Mesh.Define(stage, "/dense")
    m.GetPointsAttr().Set(pts)
    m.GetFaceVertexCountsAttr().Set([4] * len(faces))
    m.GetFaceVertexIndicesAttr().Set([i for f in faces for i in f])

    hf = rbd_mod._build_heightfield(stage.GetPrimAtPath("/dense"), up_idx=2, resolution=64)
    assert hf is not None
    N = hf["nrow"]
    world_z = hf["pos"][2] + np.asarray(hf["heights"]).reshape(N, N) * hf["size"][2]
    assert 49.9 <= world_z.min() and world_z.max() <= 51.3
