"""URDF to Isaac Sim: a robot imported as UsdPhysics, posed, put in a scene and exported.

    USD_CHUNK_DIRS=chunks python examples/scripts/urdf_to_isaac.py

  Stage Settings     Z up and metres: what Isaac Sim and ROS expect
  URDF Import        examples/assets/desk_arm.urdf as links (RigidBodyAPI, MassAPI),
                     colliders (CollisionAPI), revolute and fixed joints with their limits,
                     and an ArticulationRootAPI on the base
  Pose Articulation  one slider per moving joint, named after the link it moves; it also
                     sets each joint drive's target angle
  Property Editor    a stiffness on those drives, so a simulator holds the pose
  Inline USD         a PhysicsScene (gravity), a fixed joint bolting the base to the world,
                     and a floor, a collider
  lights, camera, Render

out/desk_arm_scene.usda is plain USD with UsdPhysics schemas only: open it in Isaac Sim
(File > Open) and press Play. The pose is the robot's starting pose. Try another from the
command line, or point URDF Import at your own robot (set Mesh Root to the folder that
`package://` paths are found in):

    usdchunks run examples/scripts/out/urdf_network.usda Pose.upper_arm=-60 Pose.forearm=100 -o arm.usda

Needs a USD build with Hydra for the image. The rest runs with the usd-core wheel.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from usd_chunks import Scene

HERE = Path(__file__).resolve().parent
# Results go to the folder given on the command line, else examples/scripts/out.
OUT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else HERE / "out"
# Relative to the network file, as USD paths are, so the network keeps working if moved
# together with the assets.
ASSET = Path(os.path.relpath(HERE.parent / "assets" / "desk_arm.urdf", OUT)).as_posix()

WORLD = """#usda 1.0
over "World"
{
    def PhysicsScene "physicsScene"
    {
        vector3f physics:gravityDirection = (0, 0, -1)
        float physics:gravityMagnitude = 9.81
    }

    # Bolt the base to the world (body0 left empty), as a desk arm is. Leave it out for a
    # mobile robot.
    def PhysicsFixedJoint "anchor"
    {
        rel physics:body1 = </World/desk_arm/base>
    }

    def Cube "floor" (
        prepend apiSchemas = ["PhysicsCollisionAPI"]
    )
    {
        double size = 1
        color3f[] primvars:displayColor = [(0.42, 0.43, 0.45)]
        double3 xformOp:translate = (0, 0, -0.005)
        float3 xformOp:scale = (2, 2, 0.01)
        uniform token[] xformOpOrder = ["xformOp:translate", "xformOp:scale"]
    }
}
"""

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    with Scene.new(path=str(OUT / "urdf_network.usda")) as scene:
        settings = scene.create.stage_settings(up_axis="Z", meters_per_unit=1.0, default_prim="World")
        robot = scene.create.urdf_import(settings, primpath_location="/World/desk_arm",
                                         file_path=ASSET)
        pose = scene.create.pose_articulation(robot, name="Pose", prim_path="/World/desk_arm",
                                              turntable=35.0, upper_arm=-30.0, forearm=75.0)
        # URDF has no joint stiffness, only damping, so the drives Pose sets a target on would
        # hold nothing in a simulator: give them a spring to hold the pose with.
        joints = ",".join(f"/World/desk_arm/{link}/joint_{joint}" for link, joint in
                          (("turntable", "base_yaw"), ("upper_arm", "shoulder"), ("forearm", "elbow")))
        stiff = scene.create.property_editor(pose, prim=joints,
                                             edits="float drive:angular:physics:stiffness = 400")
        world = scene.create.inline_usd(stiff, code=WORLD)

        sun = scene.create.distant_light(world, primpath_location="/World/sun", intensity=2.5)
        aim_sun = scene.create.transform(sun, primpath_location="/World/sun", rx=40.0, rz=30.0)
        sky = scene.create.dome_light(aim_sun, primpath_location="/World/sky", intensity=0.7)
        cam = scene.create.camera(sky, primpath_location="/World/cam", tx=1.15, ty=-0.95, tz=0.65,
                                  focalLength=35.0, clippingRange=(0.01, 100.0),
                                  aim_at_point=True, aim_point=(0.05, 0.0, 0.25))
        render = scene.create.render(cam, camera="/World/cam", mode="Command", image_width=1280,
                                     camera_light=False, output_image=str(OUT / "urdf_render.png"))
        scene.set_output(render)

        scene.save()
        scene.export(str(OUT / "desk_arm_scene.usda"))
        scene.commit()  # runs Render
