"""URDF to USD — a robot described in ROS, authored as UsdPhysics.

URDF is how nearly every robot is actually distributed: the LeRobot SO-101, every ROS arm,
most of what a robotics team already owns. USD is where it has to end up to be composed,
varied and simulated alongside everything else. Nothing open bridges the two -- Isaac's
importer does, inside Omniverse.

This authors the USD the RBD Solver already knows how to read. The mapping is deliberately
the exact inverse of `rbd_solver.read_articulations`, down to the attribute names, which
makes the round trip testable: URDF -> USD -> MuJoCo should agree with URDF -> MJCF taken
directly, and a disagreement is a real bug rather than an opinion.

    <joint type="revolute">            PhysicsRevoluteJoint
      <origin xyz rpy/>          ->      physics:localPos0 / localRot0   (in the parent)
      <axis xyz/>                ->      physics:axis + localRot1        (see _axis_frame)
      <limit lower upper/>       ->      physics:lowerLimit / upperLimit (degrees)
      <dynamics damping/>        ->      drive:angular:physics:damping

Requires: pxr (OpenUSD), usd_chunks.
"""

from __future__ import annotations

import math
import os
import struct
import xml.etree.ElementTree as ET

from pxr import Gf, Sdf, Tf, Usd, UsdGeom, UsdPhysics, UsdShade

from usd_chunks import chunk
from usd_chunks.chunk_flags import ChunkFlags

#: URDF joint type -> the USD schema to author. continuous is revolute without limits.
_JOINT_TYPES = {
    "revolute": "PhysicsRevoluteJoint",
    "continuous": "PhysicsRevoluteJoint",
    "prismatic": "PhysicsPrismaticJoint",
    "fixed": "PhysicsFixedJoint",
}

#: The canonical axis a joint's rotation is expressed against. URDF axes are arbitrary
#: vectors; physics:axis is a token, so the vector lives in the rotation instead.
_CANONICAL_AXIS = Gf.Vec3d(1, 0, 0)


def _floats(text, count, default=0.0):
    parts = (text or "").split()
    values = [float(p) for p in parts[:count]]
    return values + [default] * (count - len(values))


def _origin(element):
    """A URDF <origin xyz rpy> as a USD transform.

    rpy is fixed-axis roll-pitch-yaw in radians, applied X then Y then Z -- which composes
    as Rz * Ry * Rx, not the other way round. Getting that order backwards produces a robot
    whose links are all in plausible-looking wrong places, which is the worst kind of wrong.
    """
    origin = element.find("origin") if element is not None else None
    xyz = _floats(origin.get("xyz") if origin is not None else None, 3)
    rpy = _floats(origin.get("rpy") if origin is not None else None, 3)

    rotation = (
        Gf.Rotation(Gf.Vec3d(0, 0, 1), math.degrees(rpy[2]))
        * Gf.Rotation(Gf.Vec3d(0, 1, 0), math.degrees(rpy[1]))
        * Gf.Rotation(Gf.Vec3d(1, 0, 0), math.degrees(rpy[0]))
    )
    return Gf.Vec3d(*xyz), rotation


def _axis_frame(axis):
    """The rotation taking the canonical axis onto a URDF joint's axis.

    physics:axis is a token -- X, Y or Z -- so an axis like (0, -1, 0), which is entirely
    ordinary in a URDF, cannot be written down directly. USD's answer is to keep the token
    canonical and carry the real direction in the joint's local rotation, which is exactly
    how rbd_solver reads it back: axis_world = rotation(localRot1) * axis_local.
    """
    direction = Gf.Vec3d(*axis)
    if direction.GetLength() < 1e-9:
        # An explicit identity, not Gf.Rotation(): the default constructor leaves the angle
        # uninitialised and it reads back as nan, which propagates into a quaternion and out
        # into every transform downstream without ever raising.
        return Gf.Rotation(_CANONICAL_AXIS, 0.0)
    return Gf.Rotation(_CANONICAL_AXIS, direction.GetNormalized())


def _to_quat(rotation) -> Gf.Quatf:
    q = rotation.GetQuat()
    return Gf.Quatf(float(q.GetReal()), Gf.Vec3f(*[float(c) for c in q.GetImaginary()]))


def _srgb_to_linear(c: float) -> float:
    """A URDF colour is sRGB; a MaterialX base_color is linear.

    Handing the sRGB value over as linear lifts every dark channel and the model reads
    washed out -- the same mistake that turned twenty Porsches pastel, and just as invisible,
    because it renders plausibly rather than failing.
    """
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def parse_materials(root) -> dict:
    """{name: (r, g, b, a)} from the URDF's top-level <material> definitions.

    URDF declares materials once and refers to them by name from each <visual>, which is
    where the robot's actual appearance lives. Ignoring them gives correct geometry in
    uniform grey -- a joint diagram rather than a robot.
    """
    materials = {}
    for element in root.findall("material"):
        name = element.get("name")
        colour = element.find("color")
        if name and colour is not None:
            materials[name] = tuple(_floats(colour.get("rgba"), 4, 1.0))
    return materials


def _author_material(stage, looks_path, name, rgba):
    """A MaterialX standard_surface for one URDF material, as a plastic.

    URDF carries only a colour, so everything else is asserted: no metalness, a moderate
    roughness and a little specular, which is what 3D-printed parts and servo cases are.
    """
    path = looks_path.AppendChild(Tf.MakeValidIdentifier(name))
    if (existing := stage.GetPrimAtPath(path)) and existing.IsValid():
        return UsdShade.Material(existing)

    material = UsdShade.Material.Define(stage, path)
    shader = UsdShade.Shader.Define(stage, path.AppendChild("surface"))
    shader.CreateIdAttr("ND_standard_surface_surfaceshader")
    shader.CreateInput("base_color", Sdf.ValueTypeNames.Color3f).Set(
        Gf.Vec3f(*[_srgb_to_linear(float(c)) for c in rgba[:3]])
    )
    shader.CreateInput("specular_roughness", Sdf.ValueTypeNames.Float).Set(0.38)
    shader.CreateInput("specular", Sdf.ValueTypeNames.Float).Set(0.5)
    shader.CreateInput("metalness", Sdf.ValueTypeNames.Float).Set(0.0)
    material.CreateSurfaceOutput("mtlx").ConnectToSource(shader.ConnectableAPI(), "out")
    return material


def _author_geometry(stage, parent_path, element, index, kind, mesh_root, scale,
                     materials=None, looks_path=None):
    """One <visual> or <collision> as a USD prim. Returns the prim, or None.

    An STL is converted, since USD cannot open one. Anything USD can read is *referenced*
    rather than inlined: a robot's meshes are the bulk of it, and inlining them into the
    chunk's layer would copy the whole asset per import.
    """
    geometry = element.find("geometry")
    if geometry is None:
        return None

    name = f"{kind}_{index}"
    path = parent_path.AppendChild(name)
    position, rotation = _origin(element)

    if (box := geometry.find("box")) is not None:
        size = _floats(box.get("size"), 3, 1.0)
        prim = UsdGeom.Cube.Define(stage, path)
        prim.CreateSizeAttr(1.0)
        extra_scale = Gf.Vec3d(*size)
    elif (sphere := geometry.find("sphere")) is not None:
        prim = UsdGeom.Sphere.Define(stage, path)
        prim.CreateRadiusAttr(float(sphere.get("radius", 1.0)))
        extra_scale = None
    elif (cylinder := geometry.find("cylinder")) is not None:
        prim = UsdGeom.Cylinder.Define(stage, path)
        prim.CreateRadiusAttr(float(cylinder.get("radius", 1.0)))
        prim.CreateHeightAttr(float(cylinder.get("length", 1.0)))
        prim.CreateAxisAttr(UsdGeom.Tokens.z)  # URDF cylinders are Z-aligned
        extra_scale = None
    elif (mesh := geometry.find("mesh")) is not None:
        resolved = _resolve_mesh(mesh.get("filename", ""), mesh_root)
        if resolved and resolved.lower().endswith(".stl"):
            # Converted rather than referenced: USD cannot open an STL, so a reference to
            # one composes to nothing and the robot arrives as physics with no geometry.
            triangulated = read_stl(resolved)
            if triangulated is None:
                print(f"[URDFImport] cannot read {resolved}")
                prim = UsdGeom.Xform.Define(stage, path)
            else:
                points, counts, indices = triangulated
                usd_mesh = UsdGeom.Mesh.Define(stage, path)
                usd_mesh.CreatePointsAttr(points)
                usd_mesh.CreateFaceVertexCountsAttr(counts)
                usd_mesh.CreateFaceVertexIndicesAttr(indices)
                usd_mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
                prim = usd_mesh
        elif resolved:
            prim = UsdGeom.Xform.Define(stage, path)
            prim.GetPrim().GetReferences().AddReference(resolved)
        else:
            print(f"[URDFImport] cannot resolve mesh {mesh.get('filename')!r}")
            prim = UsdGeom.Xform.Define(stage, path)
        mesh_scale = _floats(mesh.get("scale"), 3, 1.0)
        extra_scale = Gf.Vec3d(*mesh_scale) if mesh_scale != [1.0, 1.0, 1.0] else None
    else:
        return None

    _bind_material(stage, prim, element, materials, looks_path)

    xform = UsdGeom.Xformable(prim)
    if scale != 1.0 and extra_scale is None:
        extra_scale = Gf.Vec3d(scale, scale, scale)
    elif scale != 1.0:
        extra_scale = Gf.Vec3d(*[v * scale for v in extra_scale])
    xform.AddTranslateOp().Set(position * scale)
    xform.AddOrientOp().Set(_to_quat(rotation))
    if extra_scale is not None:
        xform.AddScaleOp().Set(Gf.Vec3f(*[float(v) for v in extra_scale]))
    return prim.GetPrim()


def read_stl(path):
    """An STL as (points, faceVertexCounts, faceVertexIndices), or None.

    USD has no STL reader, and URDF ships STL almost universally -- so an importer that only
    *references* mesh files produces a robot with correct physics and no geometry at all.
    Converting is not optional here, it is the difference between a joint diagram and a
    robot.

    Binary and ASCII both, detected by size rather than by the leading "solid": a binary
    STL's 80-byte header is free text and plenty of exporters write "solid" into it, which
    is the classic way to mis-parse one.

    Vertices are left unwelded, exactly as STL stores them. Welding would need a spatial
    hash and a tolerance, and flat-shaded triangles are what an STL means anyway.
    """
    try:
        data = open(path, "rb").read()
    except OSError:
        return None
    if len(data) < 84:
        return None

    count = struct.unpack_from("<I", data, 80)[0]
    if 84 + count * 50 == len(data):
        points = []
        for i in range(count):
            base = 84 + i * 50 + 12  # skip the per-facet normal
            points.extend(
                Gf.Vec3f(*struct.unpack_from("<3f", data, base + v * 12)) for v in range(3)
            )
    else:
        points = []
        for line in data.decode("utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) == 4 and parts[0] == "vertex":
                points.append(Gf.Vec3f(*[float(v) for v in parts[1:4]]))
        if not points or len(points) % 3:
            return None

    triangles = len(points) // 3
    return points, [3] * triangles, list(range(triangles * 3))


def _bind_material(stage, prim, element, materials, looks_path):
    """Bind the <visual>'s material, by name or inline.

    displayColor as well as the binding: it costs one attribute and means the robot reads
    correctly in a viewport with no material support at all, which is most of them.
    """
    if materials is None or looks_path is None:
        return
    reference = element.find("material")
    if reference is None:
        return
    name = reference.get("name")
    inline = reference.find("color")
    rgba = (tuple(_floats(inline.get("rgba"), 4, 1.0)) if inline is not None
            else materials.get(name))
    if rgba is None:
        return

    UsdGeom.Gprim(prim).CreateDisplayColorAttr([Gf.Vec3f(*[float(c) for c in rgba[:3]])])
    if name:
        material = _author_material(stage, looks_path, name, rgba)
        UsdShade.MaterialBindingAPI.Apply(prim.GetPrim()).Bind(material)


def _resolve_mesh(filename, mesh_root):
    """A URDF mesh path as something USD can open.

    `package://pkg/meshes/x.stl` is ROS's own scheme and means nothing outside a ROS
    workspace, so it is resolved against the Mesh Root the user gives -- which is the
    directory the meshes actually sit in, and usually beside the .urdf.
    """
    if not filename:
        return ""
    path = filename
    if path.startswith("package://"):
        path = path[len("package://"):]
        path = path.split("/", 1)[1] if "/" in path else path
    if os.path.isabs(path) and os.path.exists(path):
        return path
    candidate = os.path.join(mesh_root, path) if mesh_root else path
    return candidate if os.path.exists(candidate) else ""


def _author_inertial(prim, link, scale):
    """<inertial> as UsdPhysics.MassAPI. Absent means USD computes it from the geometry."""
    inertial = link.find("inertial")
    if inertial is None:
        return
    mass_api = UsdPhysics.MassAPI.Apply(prim)
    if (mass := inertial.find("mass")) is not None:
        mass_api.CreateMassAttr(float(mass.get("value", 0.0)))
    position, _ = _origin(inertial)
    mass_api.CreateCenterOfMassAttr(Gf.Vec3f(*[float(v) for v in position * scale]))
    if (inertia := inertial.find("inertia")) is not None:
        mass_api.CreateDiagonalInertiaAttr(Gf.Vec3f(
            float(inertia.get("ixx", 0.0)),
            float(inertia.get("iyy", 0.0)),
            float(inertia.get("izz", 0.0)),
        ))


def _author_joint(stage, joint, parent_path, child_path, scale):
    """A URDF <joint> as the USD joint rbd_solver reads back.

    body0 is the parent and body1 the child, and the joint frame is placed with
    localPos0/localRot0 -- URDF's origin is the joint expressed in the *parent*. On the
    child side the joint frame is the child's own origin, so localPos1 is zero and
    localRot1 carries only the axis rotation.
    """
    kind = joint.get("type", "fixed")
    schema = _JOINT_TYPES.get(kind)
    if schema is None:
        print(f"[URDFImport] {joint.get('name')!r}: unsupported joint type {kind!r}, using fixed")
        schema = "PhysicsFixedJoint"

    path = child_path.AppendChild(f"joint_{joint.get('name', 'unnamed')}")
    prim = stage.DefinePrim(path, schema)

    prim.CreateRelationship("physics:body0").SetTargets([parent_path])
    prim.CreateRelationship("physics:body1").SetTargets([child_path])

    position, rotation = _origin(joint)
    axis_element = joint.find("axis")
    axis = _floats(axis_element.get("xyz") if axis_element is not None else None, 3)
    if axis == [0.0, 0.0, 0.0]:
        axis = [1.0, 0.0, 0.0]
    axis_rotation = _axis_frame(axis)

    prim.CreateAttribute("physics:localPos0", Sdf.ValueTypeNames.Point3f).Set(
        Gf.Vec3f(*[float(v) for v in position * scale])
    )
    prim.CreateAttribute("physics:localRot0", Sdf.ValueTypeNames.Quatf).Set(
        _to_quat(rotation * axis_rotation)
    )
    prim.CreateAttribute("physics:localPos1", Sdf.ValueTypeNames.Point3f).Set(Gf.Vec3f(0, 0, 0))
    prim.CreateAttribute("physics:localRot1", Sdf.ValueTypeNames.Quatf).Set(_to_quat(axis_rotation))

    if schema == "PhysicsFixedJoint":
        return prim

    prim.CreateAttribute("physics:axis", Sdf.ValueTypeNames.Token).Set("X")

    limit = joint.find("limit")
    if limit is not None and kind != "continuous":
        lower, upper = limit.get("lower"), limit.get("upper")
        if lower is not None and upper is not None:
            low, high = float(lower), float(upper)
            if kind == "revolute":
                # Radians in URDF, degrees in USD. Getting this wrong compiles a 90 degree
                # limit as 1.6 degrees, which reads as a seized joint rather than a bug.
                low, high = math.degrees(low), math.degrees(high)
            else:
                low, high = low * scale, high * scale
            prim.CreateAttribute("physics:lowerLimit", Sdf.ValueTypeNames.Float).Set(low)
            prim.CreateAttribute("physics:upperLimit", Sdf.ValueTypeNames.Float).Set(high)

    dynamics = joint.find("dynamics")
    if dynamics is not None:
        prefix = "drive:angular:physics:" if kind != "prismatic" else "drive:linear:physics:"
        if (damping := dynamics.get("damping")) is not None:
            prim.CreateAttribute(prefix + "damping", Sdf.ValueTypeNames.Float).Set(float(damping))
        if (friction := dynamics.get("friction")) is not None:
            prim.CreateAttribute(
                "physxJoint:jointFriction", Sdf.ValueTypeNames.Float
            ).Set(float(friction))
    return prim


def parse(source: str) -> dict:
    """The URDF as {name, links, joints, children, roots}, without touching USD.

    Separate from authoring so the tree logic is testable on its own, and so a malformed
    file fails before anything has been written to the stage.
    """
    root = ET.fromstring(source)
    links = {link.get("name"): link for link in root.findall("link")}
    # find(), not get(): <parent> and <child> are elements in URDF, not attributes. Reading
    # them as attributes silently yields no joints at all and every link looks like a root.
    joints = [
        j for j in root.findall("joint")
        if j.find("parent") is not None and j.find("child") is not None
    ]

    children: dict = {name: [] for name in links}
    has_parent = set()
    for joint in joints:
        parent = joint.find("parent").get("link")
        child = joint.find("child").get("link")
        if parent in children:
            children[parent].append((joint, child))
            has_parent.add(child)

    return {
        "name": root.get("name") or "robot",
        "materials": parse_materials(root),
        "links": links,
        "joints": joints,
        "children": children,
        # More than one root is legal and happens: a URDF may describe several unattached
        # mechanisms, and importing only the first would silently drop the rest.
        "roots": [name for name in links if name not in has_parent],
    }


def execute(node):
    file_path = (node.get_param("file_path").value or "").strip()
    root_path = (node.get_param("primpath_location").value or "").strip()
    if not file_path or not root_path:
        return
    if not os.path.exists(file_path):
        print(f"[URDFImport] no such file: {file_path}")
        return

    stage = node.get_stage()
    layer = node.get_edit_context()
    if layer is None:
        return
    layer.Clear()

    mesh_root = (node.get_param("mesh_root").value or "").strip() or os.path.dirname(file_path)
    scale = float(node.get_param("scale").value or 1.0)
    approximation = node.get_param("collider_approximation").value or "convexHull"
    want_visual = bool(node.get_param("import_visual").value)
    want_collision = bool(node.get_param("import_collision").value)

    with Usd.EditContext(stage, layer):
        try:
            robot = parse(open(file_path, encoding="utf-8").read())
        except ET.ParseError as exc:
            print(f"[URDFImport] {file_path} is not readable URDF: {exc}")
            return

        root = UsdGeom.Xform.Define(stage, Sdf.Path(root_path))
        looks_path = root.GetPath().AppendChild("Looks")
        UsdGeom.Scope.Define(stage, looks_path)
        materials = robot["materials"]
        counts = {"links": 0, "joints": 0}

        def author_link(link_name, parent_prim_path, joint, parent_world=None):
            link = robot["links"].get(link_name)
            if link is None:
                return
            # A moving link is authored FLAT; a fixed one is nested under its parent.
            #
            # Flat, because the solver writes each simulated link's *world* placement back
            # as its own share -- so a nested child inherits its parent's simulated
            # transform too and the two compound. Measured: adjacent links drifting from
            # 0.07 to 0.24 apart over 48 frames, an arm visibly coming to pieces while every
            # joint, axis and limit was correct. Sim-ready assets are flat for this reason,
            # and USD joints reference bodies by relationship rather than hierarchy.
            #
            # Nested when the joint is fixed, because such a link is not a degree of freedom
            # and the solver leaves it out of the articulation entirely. Flat, it would be
            # left behind while its parent moved -- the SO-101's gripper frame drifting
            # 0.098 to 0.234 away from the gripper it is welded to. Nesting *is* the weld,
            # and nothing simulated writes to it, so nothing compounds.
            # The discriminator is not "is the joint fixed" -- it is "will the solver
            # simulate this link", and that follows the collision geometry. A fixed link
            # WITH colliders is still a body the solver bakes, so it must be flat or its
            # parent's transform lands on it twice (the Panda's eight _sc self-collision
            # links are exactly this). A link with no colliders at all is never simulated,
            # so flat it would simply be left behind -- the SO-101's gripper frame, a pure
            # coordinate frame welded to the gripper. Nesting is the weld for those.
            fixed = joint is not None and joint.get("type", "fixed") == "fixed"
            simulated = bool(link.findall("collision"))
            nest = fixed and not simulated
            path = (parent_prim_path if nest else root.GetPath()).AppendChild(link_name)
            prim = UsdGeom.Xform.Define(stage, path).GetPrim()
            counts["links"] += 1

            # A URDF link has no pose of its own, only a position relative to its parent
            # through the joint between them -- so the rest pose has to be accumulated down
            # the chain rather than read off the link.
            world = Gf.Matrix4d(1.0) if parent_world is None else Gf.Matrix4d(parent_world)
            if joint is not None:
                position, rotation = _origin(joint)
                local = Gf.Matrix4d(1.0)
                local.SetRotateOnly(rotation)
                local.SetTranslateOnly(position * scale)
                world = local * world
                # A nested (fixed) link is placed relative to its parent; a flat one carries
                # the accumulated pose, since nothing above it supplies one.
                placement = local if nest else world
                xform = UsdGeom.Xformable(prim)
                xform.AddTranslateOp().Set(placement.ExtractTranslation())
                xform.AddOrientOp().Set(
                    _to_quat(Gf.Rotation(placement.ExtractRotationQuat()))
                )

            UsdPhysics.RigidBodyAPI.Apply(prim)
            _author_inertial(prim, link, scale)

            if want_visual:
                for i, visual in enumerate(link.findall("visual")):
                    _author_geometry(stage, path, visual, i, "visual", mesh_root, scale,
                                     materials, looks_path)
            if want_collision:
                for i, collision in enumerate(link.findall("collision")):
                    geom = _author_geometry(stage, path, collision, i, "collision",
                                            mesh_root, scale)
                    if geom is None:
                        continue
                    UsdPhysics.CollisionAPI.Apply(geom)
                    mesh_api = UsdPhysics.MeshCollisionAPI.Apply(geom)
                    mesh_api.CreateApproximationAttr(approximation)
                    UsdGeom.Imageable(geom).CreatePurposeAttr(UsdGeom.Tokens.guide)

            if joint is not None:
                _author_joint(stage, joint, parent_prim_path, path, scale)
                counts["joints"] += 1

            for child_joint, child_name in robot["children"].get(link_name, []):
                author_link(child_name, path, child_joint, world)

        for root_link in robot["roots"]:
            author_link(root_link, root.GetPath(), None, None)
            base = stage.GetPrimAtPath(root.GetPath().AppendChild(root_link))
            if base and base.IsValid():
                UsdPhysics.ArticulationRootAPI.Apply(base)

    print(f"[URDFImport] {robot['name']}: {counts['links']} link(s), {counts['joints']} joint(s), "
          f"{len(robot['materials'])} material(s), {len(robot['roots'])} root(s) -> {root_path}")


def main():
    chunk_api = chunk.Chunk("URDF Import")
    chunk_api.add_param(
        chunk.FileBrowser("file_path", extensions=[".urdf", ".xml"])
    )
    chunk_api.add_params([
        chunk.LineEdit(
            "primpath_location", "Insert to location",
            flags=ChunkFlags.ParamFlags.LABELED | ChunkFlags.ParamFlags.PRIM_SELECTION,
            tooltip="Where the robot is authored. Each root link becomes a child of it.",
        ),
        chunk.LineEdit(
            "mesh_root", "Mesh Root",
            flags=ChunkFlags.ParamFlags.LABELED,
            tooltip="Where the meshes actually live. URDF writes package://pkg/... which "
            "means nothing outside a ROS workspace. Blank uses the .urdf's own directory.",
        ),
        chunk.Slider(
            "scale", "Scale", default_value=1.0, min_value=0.001, max_value=1000.0,
            flags=ChunkFlags.ParamFlags.LABELED,
            tooltip="URDF is always metres. Set this to 100 for a centimetre stage -- "
            "getting it wrong gives a robot that is plausible and the wrong size, which "
            "reads as a modelling error rather than a units one.",
        ),
        chunk.Menu(
            "collider_approximation", "Collider",
            menu_items=["convexHull", "convexDecomposition", "boundingCube",
                        "boundingSphere", "meshSimplification", "none"],
            default_value="convexHull",
            flags=ChunkFlags.ParamFlags.LABELED,
            tooltip="physics:approximation on each collision shape. convexHull is what most "
            "solvers want; none keeps the exact mesh and is slow.",
        ),
        chunk.Checkbox("import_visual", "Visual Geometry", default_value=True,
                       flags=ChunkFlags.ParamFlags.LABELED),
        chunk.Checkbox("import_collision", "Collision Geometry", default_value=True,
                       flags=ChunkFlags.ParamFlags.LABELED),
    ])
    return chunk_api


def config():
    return chunk.DefaulConfig(
        category="Robotics",
        description="""
        Imports a URDF robot as UsdPhysics: links as rigid bodies with mass and inertia,
        collision shapes with an approximation, and revolute, prismatic and fixed joints
        with their limits and damping.

        URDF is how robots are actually distributed -- the LeRobot SO-101, every ROS arm --
        and this is the bridge into a stage where they can be composed, varied and
        simulated alongside everything else.

        The output is exactly what the RBD Solver reads: the same attribute names, the same
        conventions. A robot imported here simulates without another step.

        A moving link is authored flat under the target rather than nested as the URDF
        tree: the solver writes each link's world placement back onto its own prim, so a
        nested child would inherit its parent's simulated transform too and the mechanism
        would come apart. Joints reference bodies by relationship, so the tree survives.

        A link joined by a fixed joint is nested instead. It is not a degree of freedom, the
        solver leaves it out of the articulation, and nesting is what keeps it attached to
        the parent it is welded to.

        STL meshes are converted to UsdGeom.Mesh -- USD cannot open an STL, so referencing
        one composes to nothing. Meshes USD can read are referenced rather than inlined, so
        importing does not copy the asset.
        Collision geometry is authored as guide purpose, so it is present for physics and
        out of the way of a render.

        URDF materials become MaterialX standard_surface plastics -- URDF carries only a
        colour, so roughness and specular are asserted -- plus displayColor, so the robot
        reads correctly in a viewport with no material support.
        """,
    )
