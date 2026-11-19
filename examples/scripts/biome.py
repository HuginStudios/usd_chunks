"""A nested forest biome: each layer grows near -- or away from -- the ones before it.

    USD_CHUNK_DIRS=chunks python examples/scripts/biome.py

  trees        on a Noise Field, so there are clearings
  bushes       under the canopy, never on a trunk (Grow Near the trees)
  ferns        around the bushes (Grow Near the bushes)
  grass        out in the light (Grow Near the trees, inverted)

Grow Near reads the earlier layer's instancer directly: no field to paint, and distances are
exact whatever the ground's resolution. The noise is what needs the ground's vertices. The
prototypes are stand-in shapes; point Prototypes at real assets for a real forest.
"""
from __future__ import annotations

from usd_chunks import Scene

PROTOS = """#usda 1.0
def Scope "protos"
{
    token visibility = "invisible"
    def Cone "tree"
    {
        uniform token axis = "Y"
        double height = 8
        double radius = 2.2
        color3f[] primvars:displayColor = [(0.08, 0.3, 0.1)]
    }
    def Sphere "bush"
    {
        double radius = 0.8
        color3f[] primvars:displayColor = [(0.2, 0.45, 0.12)]
    }
    def Sphere "fern"
    {
        double radius = 0.3
        color3f[] primvars:displayColor = [(0.45, 0.65, 0.2)]
    }
    def Cone "grass"
    {
        uniform token axis = "Y"
        double height = 0.5
        double radius = 0.08
        color3f[] primvars:displayColor = [(0.7, 0.7, 0.35)]
    }
}
"""

def scatter(scene, upstream, name, proto, count, spacing, density="", near=None, **params):
    """One layer. *near* is (layer, radius, inside, outside, clear radius) for Grow Near."""
    if near:
        layer, radius, inside, outside, clear = near
        params.update(near_prims=f"/biome/{layer}", near_radius=radius, near_inside=inside,
                      near_outside=outside, near_clear=clear)
    return scene.create.scatter_points(
        upstream, name=name, surface_prim="/ground", inst_path=f"/biome/{name.lower()}",
        max_count=count, auto_spacing=False, spacing=spacing, size_spacing=True,
        scale_min=0.7, scale_max=1.3, density_primvar=density, orient_mode="Up",
        rand_axis="Y", rand_amount=360.0, **{"prototypes.0.proto_path": f"/protos/{proto}"},
        **params)


with Scene.new() as scene:
    ground = scene.create.mesh(prim_path="/ground", mesh_type="grid", dimx1=80, dimy1=80,
                               width1=80.0, height2=80.0)
    protos = scene.create.inline_usd(ground, code=PROTOS)

    forest = scene.create.noise_field(protos, prim_path="/ground", primvar="forest", size=14.0,
                                      threshold=0.45, softness=0.25, seed=1)
    trees = scatter(scene, forest, "Trees", "tree", 120, 3.5, density="forest")

    # Grow Near: (layer, radius, inside, outside, clear radius)
    bushes = scatter(scene, trees, "Bushes", "bush", 400, 1.4, near=("trees", 4.5, 1.0, 0.08, 1.0))
    ferns = scatter(scene, bushes, "Ferns", "fern", 1500, 0.5, near=("bushes", 2.5, 1.0, 0.1, 0.4))
    grass = scatter(scene, ferns, "Grass", "grass", 8000, 0.15, near=("trees", 5.0, 0.1, 1.0, 0.0))

    scene.set_output(grass)
    scene.save("biome_network.usda")
    scene.export("biome.usda")
