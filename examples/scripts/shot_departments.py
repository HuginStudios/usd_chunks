"""A shot built on a set file, in department layers side by side, exported as shot.usda.

    USD_CHUNK_DIRS=chunks python examples/scripts/shot_departments.py [folder]

Writes, under folder (default ./shot_departments):

    inputs/set.usda           a small set, made here so the example runs on its own
    shot/shot_network.usda    the network: open it as a graph, or `usdchunks run` it
    shot/shot.usda            the shot: stage settings, the departments sublayered over the set
    shot/layout.usda, lighting.usda, camera.usda
                              each department's own edits, nothing else

Copy it and change the departments. Every line here runs as written (a test runs it).
"""
from __future__ import annotations

import os
import sys

from pxr import Sdf

from usd_chunks import Scene

folder = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "shot_departments")
os.makedirs(os.path.join(folder, "inputs"), exist_ok=True)
os.makedirs(os.path.join(folder, "shot"), exist_ok=True)

# A set to build on: /World with a box. A real shot sublayers its set file instead.
set_layer = Sdf.Layer.CreateNew(os.path.join(folder, "inputs", "set.usda"))
world = Sdf.CreatePrimInLayer(set_layer, "/World")
world.specifier, world.typeName = Sdf.SpecifierDef, "Xform"
box = Sdf.CreatePrimInLayer(set_layer, "/World/box")
box.specifier, box.typeName = Sdf.SpecifierDef, "Cube"
set_layer.defaultPrim = "World"
set_layer.Save()

# path= : relative file paths are found from the network's folder, as in a USD file.
with Scene.new(path=os.path.join(folder, "shot", "shot_network.usda")) as scene:
    # The shot's own settings first: they go on the session layer, which is the shot.
    last = scene.create.stage_settings(meters_per_unit=1.0, up_axis="Y", default_prim="World",
                                       start_frame=1, end_frame=48)
    last = scene.create.sublayer(last, imports=["../inputs/set.usda"], name="Set")

    # One department at a time: a Layer, an Edit Target into it, its edits, and an Edit
    # Target back out to the session layer, so the next Layer sits beside it, not inside.
    last = scene.create.layer(last, layer_name="layout")
    last = scene.create.edit_target(last, edit_target="layout")
    last = scene.create.transform(last, primpath_location="/World/box", tx=3.0, ty=0.5, name="MoveBox")
    last = scene.create.edit_target(last, edit_target="Session Layer")

    last = scene.create.layer(last, layer_name="lighting")
    last = scene.create.edit_target(last, edit_target="lighting")
    last = scene.create.distant_light(last, primpath_location="/World/key", intensity=3.0, name="Key")
    last = scene.create.edit_target(last, edit_target="Session Layer")

    # There is no keyframe chunk yet: to animate, write a small one that sets time samples
    # (the usd-chunks-write-chunk skill) and put it in a department like this one.
    last = scene.create.layer(last, layer_name="camera")
    last = scene.create.edit_target(last, edit_target="camera")
    last = scene.create.camera(last, primpath_location="/World/cam", ty=2.0, tz=10.0,
                               aim_at="/World/box", name="ShotCam")
    last = scene.create.edit_target(last, edit_target="Session Layer")

    # One file per layer. The session layer is the shot: Session File Name "shot" writes it
    # as shot.usda, and Bake all writes every file as plain USD, without the network.
    export = scene.create.export_usd(last, output_dir=".", session_file_name="shot",
                                     bake_all=True, name="Export")
    scene.set_output(export)
    # Export USD lists every real layer, the set file and the network's own too: ship only
    # what the departments made.
    rows = export.get_param("export_layers")
    for i in range(rows.size):
        if rows.get_indexed_param("layer_name", i).value in ("set.usda", "shot_network.usda"):
            scene.set(export, **{f"export_layers.{i}.export_enabled": False})

    scene.save()     # to the path given to Scene.new
    scene.commit()   # runs Export USD; `usdchunks run shot/shot_network.usda` does the same

print(f"Wrote {folder}")
