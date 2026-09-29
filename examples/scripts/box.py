"""A cube, moved up and coloured: Mesh (Cube) -> Transform -> Set Display Color.

    USD_CHUNK_DIRS=chunks python examples/scripts/box.py
"""
from usd_chunks import Scene

with Scene() as scene:
    box = scene.create.cube(prim_path="/geo/box", width=2.0)
    path = box.get_param("prim_path")

    xform = scene.create.transform(box, primpath_location=path, ty=3.0)
    scene.create.set_display_color(xform, prim_path=path, color_r=1.0, color_g=0.2, color_b=0.1)

    scene.save("box_network.usda")
    scene.export("box.usda")
