"""A ground, a key light and a camera, authored into their own "shot" layer.

    USD_CHUNK_DIRS=chunks python examples/scripts/shot_setup.py
"""
from usd_chunks import Scene

with Scene() as scene:
    shot = scene.create.layer(layer_name="shot")

    # Everything created inside runs after the Edit Target, so its edits land in "shot".
    with scene.create.edit_target(shot, edit_target="shot"):
        ground = scene.create.ground(prim_path="/set/ground")

        key = scene.create.rect_light(ground, primpath_location="/set/key_light")
        scene.create.transform(key, primpath_location=key.get_param("primpath_location"),
                               tx=4.0, ty=6.0, tz=4.0, rx=-45.0, ry=45.0)

        scene.create.camera(ground, primpath_location="/set/shot_cam",
                            focalLength=35.0, ty=2.0, tz=12.0, rx=-8.0)

    scene.cook()   # Light builds its prim in execute(), not on a value change
    scene.save("shot_network.usda")
    scene.export("shot.usda")
