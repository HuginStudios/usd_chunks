"""RBDSolverNode tests — physics simulation correctness."""

import pytest
from pxr import Usd, UsdGeom, Gf, Vt, Sdf


@pytest.fixture
def rbd_graph(node_graph, qapp):
    """node_graph with RBDSolverNode registered."""
    from node_graph.nodes._simulation import RBDSolverNode

    ng, CubeNodeCls = node_graph
    ng.register_node(RBDSolverNode)
    return ng, RBDSolverNode


@pytest.fixture
def instancer_stage(stage):
    """Stage with a PointInstancer at /TestInstancer with 3 instances."""
    path = "/TestInstancer"
    # Clean up from previous test
    old = stage.GetPrimAtPath(path)
    if old and old.IsValid():
        stage.RemovePrim(path)

    instancer = UsdGeom.PointInstancer.Define(stage, path)
    instancer.GetPositionsAttr().Set(
        Vt.Vec3fArray(
            [
                Gf.Vec3f(0, 0, 0),
                Gf.Vec3f(10, 0, 0),
                Gf.Vec3f(-10, 0, 0),
            ]
        )
    )
    instancer.GetOrientationsAttr().Set(
        Vt.QuathArray(
            [
                Gf.Quath(1, 0, 0, 0),
                Gf.Quath(1, 0, 0, 0),
                Gf.Quath(1, 0, 0, 0),
            ]
        )
    )
    # Prototype
    stage.DefinePrim(f"{path}/Prototypes", "Scope")
    UsdGeom.Cube.Define(stage, f"{path}/Prototypes/proto_0")
    instancer.GetPrototypesRel().SetTargets([Sdf.Path(f"{path}/Prototypes/proto_0")])
    instancer.GetProtoIndicesAttr().Set(Vt.IntArray([0, 0, 0]))
    return stage, path


@pytest.fixture
def single_prim_stage(stage):
    """Stage with a simple xform prim at /TestBody."""
    path = "/TestBody"
    old = stage.GetPrimAtPath(path)
    if old and old.IsValid():
        stage.RemovePrim(path)

    xform = UsdGeom.Xform.Define(stage, path)
    xform.AddTranslateOp().Set(Gf.Vec3d(0, 0, 50))
    return stage, path


# ── Import / registration ─────────────────────────────────────────────────────


def test_rbd_solver_importable():
    from node_graph.nodes._simulation import RBDSolverNode

    assert RBDSolverNode is not None


def test_rbd_solver_category():
    from node_graph.nodes._simulation import RBDSolverNode

    assert RBDSolverNode.CATEGORY == "Simulation"


def test_rbd_solver_has_pybullet_check():
    from node_graph.nodes import _simulation

    # _HAS_PYBULLET should be True since we installed it
    assert _simulation._HAS_PYBULLET is True


# ── Port structure ────────────────────────────────────────────────────────────


def test_rbd_solver_ports(rbd_graph, qapp):
    ng, RBDSolverNode = rbd_graph
    node = ng.create_node(RBDSolverNode.type_)
    input_names = [p.name() for p in node.input_ports()]
    output_names = [p.name() for p in node.output_ports()]
    assert "instancer" in input_names
    assert "collision" in input_names
    assert "prim" in output_names


def test_rbd_solver_collision_is_multi_input(rbd_graph, qapp):
    ng, RBDSolverNode = rbd_graph
    node = ng.create_node(RBDSolverNode.type_)
    port = node.get_input("collision")
    assert port.multi_connection()


# ── Execution: no input ───────────────────────────────────────────────────────


def test_execute_no_input_returns_none(rbd_graph, stage, qapp):
    ng, RBDSolverNode = rbd_graph
    node = ng.create_node(RBDSolverNode.type_)
    result = node.execute(stage)
    assert result["prim"] is None


# ── Execution: instancer ──────────────────────────────────────────────────────


def test_execute_instancer_bakes_positions(rbd_graph, instancer_stage, qapp):
    ng, RBDSolverNode = rbd_graph
    stage, inst_path = instancer_stage

    node = ng.create_node(RBDSolverNode.type_)
    node.set_property("frames", "10")
    node.set_property("fps", "24")
    node.set_property("gravity", "-981")
    node.set_property("drop_height", "50")
    node.set_property("substeps", "4")
    node.set_property("start_frame", "1")

    # Mock the input — bypass port connection check
    instancer_prim = stage.GetPrimAtPath(inst_path)
    _orig_get_input = node.get_input

    class FakePort:
        def connected_ports(self):
            return [True]

    node.get_input = lambda name: (
        FakePort() if name == "instancer" else _orig_get_input(name)
    )
    node._get_input_value = lambda name: instancer_prim if name == "instancer" else None
    node._get_all_collision_inputs = lambda: []
    node._get_all_force_inputs = lambda: []

    result = node.execute(stage)
    assert result["prim"] is not None

    # Check time samples were written
    instancer = UsdGeom.PointInstancer(instancer_prim)
    pos_attr = instancer.GetPositionsAttr()
    assert pos_attr.GetNumTimeSamples() >= 10


def test_instancer_positions_fall_with_gravity(rbd_graph, instancer_stage, qapp):
    ng, RBDSolverNode = rbd_graph
    stage, inst_path = instancer_stage

    node = ng.create_node(RBDSolverNode.type_)
    node.set_property("frames", "20")
    node.set_property("fps", "24")
    node.set_property("gravity", "-981")
    node.set_property("drop_height", "100")
    node.set_property("substeps", "4")
    node.set_property("start_frame", "1")

    instancer_prim = stage.GetPrimAtPath(inst_path)

    class FakePort:
        def connected_ports(self):
            return [True]

    node.get_input = lambda name: (
        FakePort()
        if name == "instancer"
        else type("P", (), {"connected_ports": lambda self: []})()
    )
    node._get_input_value = lambda name: instancer_prim if name == "instancer" else None
    node._get_all_collision_inputs = lambda: []
    node._get_all_force_inputs = lambda: []

    node.execute(stage)

    # At frame 20, objects should have fallen (Z decreased from initial)
    instancer = UsdGeom.PointInstancer(instancer_prim)
    pos_start = instancer.GetPositionsAttr().Get(Usd.TimeCode(1))
    pos_end = instancer.GetPositionsAttr().Get(Usd.TimeCode(20))

    # All Z values should be lower at frame 20 than frame 1
    for i in range(3):
        assert pos_end[i][2] < pos_start[i][2], f"Instance {i} didn't fall"


# ── Execution: single prim ────────────────────────────────────────────────────


def test_execute_single_prim_writes_xform(rbd_graph, single_prim_stage, qapp):
    ng, RBDSolverNode = rbd_graph
    stage, prim_path = single_prim_stage

    node = ng.create_node(RBDSolverNode.type_)
    node.set_property("frames", "10")
    node.set_property("fps", "24")
    node.set_property("gravity", "-981")
    node.set_property("drop_height", "0")
    node.set_property("substeps", "4")
    node.set_property("start_frame", "1")

    prim = stage.GetPrimAtPath(prim_path)

    class FakePort:
        def connected_ports(self):
            return [True]

    node.get_input = lambda name: (
        FakePort()
        if name == "instancer"
        else type("P", (), {"connected_ports": lambda self: []})()
    )
    node._get_input_value = lambda name: prim if name == "instancer" else None
    node._get_all_collision_inputs = lambda: []
    node._get_all_force_inputs = lambda: []

    result = node.execute(stage)
    assert result["prim"] is not None

    # Check xformOp:transform has time samples
    xf_attr = prim.GetAttribute("xformOp:transform")
    assert xf_attr and xf_attr.GetNumTimeSamples() >= 10


def test_single_prim_falls_with_gravity(rbd_graph, single_prim_stage, qapp):
    ng, RBDSolverNode = rbd_graph
    stage, prim_path = single_prim_stage

    node = ng.create_node(RBDSolverNode.type_)
    node.set_property("frames", "20")
    node.set_property("fps", "24")
    node.set_property("gravity", "-981")
    node.set_property("drop_height", "0")
    node.set_property("substeps", "4")
    node.set_property("start_frame", "1")

    prim = stage.GetPrimAtPath(prim_path)

    class FakePort:
        def connected_ports(self):
            return [True]

    node.get_input = lambda name: (
        FakePort()
        if name == "instancer"
        else type("P", (), {"connected_ports": lambda self: []})()
    )
    node._get_input_value = lambda name: prim if name == "instancer" else None
    node._get_all_collision_inputs = lambda: []
    node._get_all_force_inputs = lambda: []

    node.execute(stage)

    # Read transform at start and end
    xf_attr = prim.GetAttribute("xformOp:transform")
    mat_start = xf_attr.Get(Usd.TimeCode(1))
    mat_end = xf_attr.Get(Usd.TimeCode(20))

    # Z translation should decrease (falling)
    z_start = mat_start.ExtractTranslation()[2]
    z_end = mat_end.ExtractTranslation()[2]
    assert z_end < z_start, f"Prim didn't fall: z_start={z_start}, z_end={z_end}"


# ── Drop height ───────────────────────────────────────────────────────────────


def test_drop_height_offsets_initial_position(rbd_graph, instancer_stage, qapp):
    ng, RBDSolverNode = rbd_graph
    stage, inst_path = instancer_stage

    node = ng.create_node(RBDSolverNode.type_)
    node.set_property("frames", "1")
    node.set_property("fps", "24")
    node.set_property("gravity", "0")  # no gravity — just check initial pos
    node.set_property("drop_height", "100")
    node.set_property("substeps", "1")
    node.set_property("start_frame", "1")

    instancer_prim = stage.GetPrimAtPath(inst_path)

    class FakePort:
        def connected_ports(self):
            return [True]

    node.get_input = lambda name: (
        FakePort()
        if name == "instancer"
        else type("P", (), {"connected_ports": lambda self: []})()
    )
    node._get_input_value = lambda name: instancer_prim if name == "instancer" else None
    node._get_all_collision_inputs = lambda: []
    node._get_all_force_inputs = lambda: []

    node.execute(stage)

    instancer = UsdGeom.PointInstancer(instancer_prim)
    pos = instancer.GetPositionsAttr().Get(Usd.TimeCode(1))
    # Original Z was 0, drop_height=100, so first frame Z should be ~100
    assert pos[0][2] > 90, f"Drop height not applied: Z={pos[0][2]}"


# ── Stage time range ──────────────────────────────────────────────────────────


def test_sets_stage_time_range(rbd_graph, instancer_stage, qapp):
    ng, RBDSolverNode = rbd_graph
    stage, inst_path = instancer_stage

    node = ng.create_node(RBDSolverNode.type_)
    node.set_property("frames", "48")
    node.set_property("start_frame", "5")
    node.set_property("substeps", "1")
    node.set_property("drop_height", "10")

    instancer_prim = stage.GetPrimAtPath(inst_path)

    class FakePort:
        def connected_ports(self):
            return [True]

    node.get_input = lambda name: (
        FakePort()
        if name == "instancer"
        else type("P", (), {"connected_ports": lambda self: []})()
    )
    node._get_input_value = lambda name: instancer_prim if name == "instancer" else None
    node._get_all_collision_inputs = lambda: []
    node._get_all_force_inputs = lambda: []

    node.execute(stage)

    assert stage.GetStartTimeCode() == 5
    assert stage.GetEndTimeCode() == 52  # 5 + 48 - 1


# ── PyBullet API correctness ──────────────────────────────────────────────────


def test_pybullet_body_creation_with_offset():
    """Verify the PyBullet API calls we use actually work."""
    import pybullet as p
    import pybullet_data

    client = p.connect(p.DIRECT)
    p.setAdditionalSearchPath(pybullet_data.getDataPath())
    p.setGravity(0, 0, -981)
    p.setTimeStep(1.0 / 24 / 10)

    # Create ground plane
    ground = p.createCollisionShape(p.GEOM_PLANE, planeNormal=[0, 0, 1])
    p.createMultiBody(
        baseMass=0, baseCollisionShapeIndex=ground, basePosition=[0, 0, 0]
    )

    # Create box body with offset position (simulating bbox center offset)
    col_shape = p.createCollisionShape(p.GEOM_BOX, halfExtents=[5, 5, 5])
    init_pos = [10, 20, 100]  # prim origin + col_offset
    init_orn = [0, 0, 0, 1]
    body = p.createMultiBody(
        baseMass=1.0,
        baseCollisionShapeIndex=col_shape,
        basePosition=init_pos,
        baseOrientation=init_orn,
    )
    assert body >= 0

    # Step simulation
    for _ in range(100):
        p.stepSimulation()

    pos, orn = p.getBasePositionAndOrientation(body)
    # Should have fallen (Z decreased)
    assert pos[2] < 100, f"Body didn't fall: Z={pos[2]}"
    # Should have stopped at ground (Z >= 5, the half extent)
    assert pos[2] >= 4, f"Body fell through ground: Z={pos[2]}"

    p.disconnect()


def test_pybullet_sphere_collision():
    """Verify sphere collision shape creation works."""
    import pybullet as p
    import pybullet_data

    client = p.connect(p.DIRECT)
    p.setAdditionalSearchPath(pybullet_data.getDataPath())
    p.setGravity(0, 0, -981)

    sphere = p.createCollisionShape(p.GEOM_SPHERE, radius=30)
    body = p.createMultiBody(
        baseMass=0, baseCollisionShapeIndex=sphere, basePosition=[0, 0, 0]
    )
    assert body >= 0

    p.disconnect()


def test_pybullet_plane_collision():
    """Verify infinite plane creation works."""
    import pybullet as p
    import pybullet_data

    client = p.connect(p.DIRECT)
    p.setAdditionalSearchPath(pybullet_data.getDataPath())

    plane = p.createCollisionShape(p.GEOM_PLANE, planeNormal=[0, 0, 1])
    body = p.createMultiBody(
        baseMass=0, baseCollisionShapeIndex=plane, basePosition=[0, 0, 50]
    )
    assert body >= 0

    # Box should land on the plane at Z=50
    box = p.createCollisionShape(p.GEOM_BOX, halfExtents=[2, 2, 2])
    dyn_body = p.createMultiBody(
        baseMass=1, baseCollisionShapeIndex=box, basePosition=[0, 0, 100]
    )
    p.setGravity(0, 0, -981)
    p.setTimeStep(1.0 / 240)

    for _ in range(500):
        p.stepSimulation()

    pos, _ = p.getBasePositionAndOrientation(dyn_body)
    # Should rest at Z ≈ 52 (plane at 50 + half_extent 2)
    assert 50 < pos[2] < 55, f"Box didn't land on plane: Z={pos[2]}"

    p.disconnect()


def test_pybullet_trimesh_collision():
    """Verify trimesh collision shape creation works."""
    import pybullet as p
    import pybullet_data

    client = p.connect(p.DIRECT)
    p.setAdditionalSearchPath(pybullet_data.getDataPath())

    # Simple quad as trimesh
    vertices = [[50, 50, 0], [-50, 50, 0], [-50, -50, 0], [50, -50, 0]]
    indices = [0, 1, 2, 0, 2, 3]
    mesh = p.createCollisionShape(
        p.GEOM_MESH, vertices=vertices, indices=indices, meshScale=[1, 1, 1]
    )
    body = p.createMultiBody(
        baseMass=0, baseCollisionShapeIndex=mesh, basePosition=[0, 0, 0]
    )
    assert body >= 0

    p.disconnect()


def test_pybullet_velocity_and_forces():
    """Verify resetBaseVelocity works."""
    import pybullet as p
    import pybullet_data

    client = p.connect(p.DIRECT)
    p.setAdditionalSearchPath(pybullet_data.getDataPath())
    p.setGravity(0, 0, 0)  # no gravity
    p.setTimeStep(1.0 / 24)

    box = p.createCollisionShape(p.GEOM_BOX, halfExtents=[1, 1, 1])
    body = p.createMultiBody(
        baseMass=1, baseCollisionShapeIndex=box, basePosition=[0, 0, 0]
    )
    p.resetBaseVelocity(body, linearVelocity=[10, 0, 0], angularVelocity=[0, 0, 1])

    p.stepSimulation()
    pos, _ = p.getBasePositionAndOrientation(body)
    # Should have moved in X
    assert pos[0] > 0, f"Velocity not applied: X={pos[0]}"

    p.disconnect()


# ── Parse helpers ─────────────────────────────────────────────────────────────


def test_parse_vec3():
    from node_graph.nodes._simulation import RBDSolverNode

    assert RBDSolverNode._parse_vec3("1 2 3") == [1.0, 2.0, 3.0]
    assert RBDSolverNode._parse_vec3("0 0 0") == [0.0, 0.0, 0.0]
    assert RBDSolverNode._parse_vec3("-5.5 10 0") == [-5.5, 10.0, 0.0]
