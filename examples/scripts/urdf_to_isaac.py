"""URDF to Isaac Sim: the SO-101 arm imported as UsdPhysics, posed, put in a scene and exported.

    USD_CHUNK_DIRS=chunks python examples/scripts/urdf_to_isaac.py

  Stage Settings     Z up and metres: what Isaac Sim and ROS expect
  URDF Import        the SO-101 (TheRobotStudio's open 6-axis arm; fetched the first time,
                     see example_assets.py) as links (RigidBodyAPI, MassAPI), colliders
                     (CollisionAPI), revolute joints with their limits, its STL meshes, and
                     an ArticulationRootAPI on the base
  Pose Articulation  one slider per moving joint, named after the link it moves; it also
                     sets each joint drive's target angle
  Property Editor    a stiffness on those drives, so a simulator holds the pose
  Prim               a PhysicsScene (gravity), and a fixed joint bolting the base to the
                     world (Property Editor sets the joint's body)
  Mesh               a textured floor, and a block to pick up
  Apply Schema       the floor and the block collide; the block is a rigid body with a mass
  lights, camera
  Render             writes urdf_render.png: Aurora when it is installed, else Storm

out/so101_scene.usda is plain USD with UsdPhysics schemas only: open it in Isaac Sim
(File > Open) and press Play. The pose is the robot's starting pose. Try another from the
command line, or point URDF Import at your own robot (set Mesh Root to the folder that
`package://` paths are found in):

    usdchunks run examples/scripts/out/urdf_network.usda Pose.shoulder_link=20 Pose.wrist_link=30 -o arm.usda
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import example_assets as assets

from usd_chunks import Scene

HERE = Path(__file__).resolve().parent
# Results go to the folder given on the command line, else examples/scripts/out.
OUT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else HERE / "out"

# The moving joints, as URDF Import names them: <the link it moves>/joint_<URDF joint name>.
JOINTS = {"shoulder_link": "shoulder_pan", "upper_arm_link": "shoulder_lift", "lower_arm_link": "elbow_flex",
          "wrist_link": "wrist_flex", "gripper_link": "wrist_roll", "moving_jaw_so101_v1_link": "gripper"}

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    # Relative to the network file, as USD paths are, so the network keeps working if moved
    # together with the assets.
    urdf = Path(os.path.relpath(assets.so101(), OUT)).as_posix()
    with Scene.new(path=str(OUT / "urdf_network.usda")) as scene:
        last = scene.create.stage_settings(up_axis="Z", meters_per_unit=1.0, default_prim="World")
        last = scene.create.urdf_import(last, primpath_location="/World/so101", file_path=urdf)
        last = scene.create.pose_articulation(last, name="Pose", prim_path="/World/so101", shoulder_link=-30.0, upper_arm_link=10.0,
                                              lower_arm_link=0.0, wrist_link=65.0, moving_jaw_so101_v1_link=30.0)
        # URDF has no joint stiffness, only damping, so the drives Pose sets a target on would
        # hold nothing in a simulator: give them a spring to hold the pose with.
        joints = ",".join(f"/World/so101/{link}/joint_{joint}" for link, joint in JOINTS.items())
        last = scene.create.property_editor(last, name="Stiffness", prim=joints,
                                            edits="float drive:angular:physics:stiffness = 40")

        # The physics scene: gravity, at its default, 9.81 m/s² down the stage's up axis.
        last = scene.create.prim(last, name="PhysicsScene", primpath_location="/World/physicsScene",
                                 prim_type="PhysicsScene")
        # Bolt the base to the world, as a bench arm is: a fixed joint with body0 left empty.
        # Leave these two out for a mobile robot.
        last = scene.create.prim(last, name="Anchor", primpath_location="/World/anchor", prim_type="PhysicsFixedJoint")
        last = scene.create.property_editor(last, name="AnchorBase", prim="/World/anchor",
                                            edits='physics:body1 = "/World/so101/base_link"')

        # A concrete floor: 4 m, built 8 units wide and scaled by half, so its texture repeats
        # every 0.5 m (see example_assets.texture_look).
        last = scene.create.mesh(last, prim_path="/World/floor", mesh_type="grid", dimx1=2, dimy1=2,
                                 width1=8.0, height2=8.0)
        last = scene.create.transform(last, primpath_location="/World/floor", sx=0.5, sy=0.5)
        last = assets.texture_look(scene, last, "/World/looks/concrete", "concrete_floor_02", "/World/floor")

        # A 3 cm block in reach, a rigid body the gripper can push or pick up.
        last = scene.create.mesh(last, prim_path="/World/block", mesh_type="cube", width=0.03, height=0.03,
                                 depth=0.03)
        last = scene.create.transform(last, primpath_location="/World/block", tx=0.3, ty=0.17, tz=0.015,
                                      rz=25.0)
        last = scene.create.set_display_color(last, prim_path="/World/block", color_r=0.05, color_g=0.25,
                                              color_b=0.7)
        # The floor and the block collide. The block also moves (a rigid body) and weighs 20 g.
        last = scene.create.apply_schema(last, name="Colliders", prim="/World/floor,/World/block",
                                         schemas="PhysicsCollisionAPI")
        for schema in ("PhysicsRigidBodyAPI", "PhysicsMeshCollisionAPI", "PhysicsMassAPI"):
            last = scene.create.apply_schema(last, name=schema.removeprefix("Physics").removesuffix("API"),
                                             prim="/World/block", schemas=schema)
        # A moving mesh collides as its convex hull: PhysX takes no triangle mesh on a moving body.
        last = scene.create.property_editor(last, name="BlockPhysics", prim="/World/block",
                                            edits='physics:approximation = "convexHull"\nphysics:mass = 0.02')

        # Normalize makes Intensity the light that lands, not the brightness of the sun's disc.
        sun = scene.create.distant_light(last, primpath_location="/World/sun", intensity=3.0, normalize=True)
        aim_sun = scene.create.transform(sun, primpath_location="/World/sun", rx=45.0, rz=-130.0)
        sky = scene.create.dome_light(aim_sun, primpath_location="/World/sky", intensity=0.8,
                                      file=assets.hdri().as_posix())
        # A lat-long image has its poles on Y; the stage is Z up, so turn the dome to match.
        pole = scene.create.transform(sky, primpath_location="/World/sky", rx=90.0)
        cam = scene.create.camera(pole, primpath_location="/World/cam", tx=0.78, ty=-0.62, tz=0.45,
                                  focalLength=35.0, clippingRange=(0.01, 100.0),
                                  aim_at_point=True, aim_point=(0.15, 0.06, 0.08))
        render = scene.create.render(cam, camera="/World/cam", mode="Command", image_width=1280,
                                     camera_light=False, output_image=str(OUT / "urdf_render.png"),
                                     **assets.render_params(OUT))
        scene.set_output(render)

        scene.save()
        scene.export(str(OUT / "so101_scene.usda"))
        scene.commit()  # runs Render
