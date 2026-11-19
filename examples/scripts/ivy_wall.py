"""Ivy on a stone wall, ready to render: Vine -> Curves to Mesh -> materials, with leaves.

A Scatter Brush paints the front of the wall, and the Vine grows where it painted. Most
renderers (Aurora among them) do not draw curves, so the vine is swept into a mesh and
shaded like any other. The leaves are a small leaf mesh instanced on the Vine's leaf points
by Scatter Points.

    USD_CHUNK_DIRS=chunks python examples/scripts/ivy_wall.py

Writes ivy_wall_network.usda (the network) and ivy_wall.usda (the result).
"""

from __future__ import annotations

import math
import sys

from pxr import Gf

from usd_chunks import Scene, chunk
from usd_chunks.gizmo import runtime as gizmo

def _leaf(points=24, size=0.07):
    """One ivy leaf as .usda: five pointed lobes, a little cupped, its stalk at the origin.

    It lies in its XZ plane with +Y as its face, tip along +Z, so Scatter Points' Align to
    Normal lays it on the wall facing out. A fan from the middle: the centre vertex, then the
    outline.
    """
    centre = (0.0, 0.0, size * 0.5)
    outline = []
    for i in range(points):
        angle = 2.0 * math.pi * i / points
        # Five lobes, the tip one longest: ivy's outline, near enough at leaf size.
        lobe = 0.62 + 0.38 * abs(math.cos(2.5 * angle)) ** 3 + 0.12 * max(math.cos(angle), 0.0)
        radius = size * 0.5 * lobe
        x, z = math.sin(angle) * radius, centre[2] + math.cos(angle) * radius
        outline.append((x, 0.006 * (radius / (size * 0.5)) ** 2, z))
    verts = [centre] + outline
    faces = [i for k in range(points) for i in (0, 1 + k, 1 + (k + 1) % points)]
    st = [(0.5 + x / size, z / size) for x, _y, z in verts]

    def vec(values):
        return ", ".join("(" + ", ".join(f"{c:.5f}" for c in v) + ")" for v in values)

    return f"""#usda 1.0
(
    upAxis = "Y"
)

def Xform "protos"
{{
    def Mesh "leaf"
    {{
        uniform bool doubleSided = 1
        int[] faceVertexCounts = [{", ".join(["3"] * points)}]
        int[] faceVertexIndices = [{", ".join(str(i) for i in faces)}]
        point3f[] points = [{vec(verts)}]
        texCoord2f[] primvars:st = [{vec(st)}] (interpolation = "vertex")
        uniform token subdivisionScheme = "none"
    }}
}}
"""


def build(network="ivy_wall_network.usda", result="ivy_wall.usda", animate=False):
    """Build the network, save it, and export the cooked scene."""
    with Scene.new() as scene:
        with chunk.change_block():
            ground = scene.create.ground(prim_path="/World/ground", width1=8, height2=8)
            wall = scene.create.cube(
                ground, prim_path="/World/wall", width=4.0, height=2.0, depth=0.4,
                dimx=40, dimy=20, dimz=4,
            )
            stone = scene.create.preview_surface(
                wall, mat_path="/World/looks/stone", primpath_location="/World/wall",
                diffuse_color=(0.45, 0.43, 0.40), roughness=0.85,
            )
            brush = scene.create.scatter_brush(
                stone, surface_prim="/World/wall", radius=0.45, strength=1.0, show_paint=False,
            )
            ivy = scene.create.vine(
                brush, surface_prim="/World/wall", vine_path="/World/ivy",
                density_primvar=brush.get_param("density_name"),
                density=0.5, thickness=0.6, hang=15.0, hang_length=15.0, leaf_spacing=2.5,
                leaf_spread=1.2, leaf_bare=15.0, **({"animate": True, "start_frame": 1, "end_frame": 48} if animate else {}),
            )
            tube = scene.create.curves_to_mesh(
                ivy, curves_prim=ivy.get_param("out_curves"), primpath_location="/World/ivy/Stems",
            )
            bark = scene.create.preview_surface(
                tube, mat_path="/World/looks/bark", primpath_location="/World/ivy/Stems",
                diffuse_color=(0.22, 0.16, 0.10), roughness=0.9,
            )
            protos = scene.create.inline_usd(bark, code=_leaf())
            leaf_look = scene.create.preview_surface(
                protos, mat_path="/World/looks/leaf", primpath_location="/protos/leaf",
                diffuse_color=(0.10, 0.32, 0.07), roughness=0.45,
            )
            leaves = scene.create.scatter_points(
                leaf_look, points_prim=ivy.get_param("out_leaves"), inst_path="/World/ivy/Foliage",
                prototypes=[{"proto_path": "/protos/leaf"}], scale_min=0.8, scale_max=1.5,
                rand_axis="Y",
            )
            sun = scene.create.light(
                leaves, primpath_location="/World/lights/sun", light_type="DistantLight",
                intensity=2.5, angle=2.0,
            )
            aim = scene.create.transform(sun, primpath_location="/World/lights/sun", rx=-40.0, ry=35.0)
            sky = scene.create.light(
                aim, primpath_location="/World/lights/sky", light_type="DomeLight", intensity=0.35,
            )
            cam = scene.create.camera(
                sky, primpath_location="/World/cam", tx=3.2, ty=1.6, tz=4.2, focalLength=30.0,
                aim_at_point=True, aim_point=(0.0, 0.9, 0.0),
            )
        scene.set_output(cam)
        # Paint the front of the wall the way a viewport stroke does: rows across it. The
        # lowest row is a brush radius up, so the paint reaches the foot of the wall but not
        # its underside; the top stays bare, so the vines stop short of it and hang from there.
        stroke = [
            (Gf.Vec3d(x / 10.0, y / 10.0, 0.2), 1.0)
            for y in range(5, 15, 3)
            for x in range(-19, 20, 2)
        ]
        gizmo.apply_stroke(brush, "paint", stroke)
        ivy.execute()
        scene.save(network)
        scene.export(result)
    return network, result


if __name__ == "__main__":
    build(*sys.argv[1:3])
