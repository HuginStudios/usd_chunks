"""OffsetTransformNode — issue #09."""

from pxr import Gf, UsdGeom


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_cube(stage, path):
    prim = UsdGeom.Cube.Define(stage, path).GetPrim()
    return prim


def _get_translate(prim):
    attr = prim.GetAttribute("xformOp:translate")
    if not attr.IsValid():
        return Gf.Vec3d(0, 0, 0)
    return attr.Get(0)


def _get_rotate(prim):
    attr = prim.GetAttribute("xformOp:rotateXYZ")
    if not attr.IsValid():
        return Gf.Vec3f(0, 0, 0)
    return attr.Get(0)


def _get_scale(prim):
    attr = prim.GetAttribute("xformOp:scale")
    if not attr.IsValid():
        return Gf.Vec3f(1, 1, 1)
    return attr.Get(0)


def _make_node(ng):
    from node_graph.nodes._transform import OffsetTransformNode

    try:
        ng.register_node(OffsetTransformNode)
    except Exception:
        pass
    return ng.create_node(OffsetTransformNode.type_)


def _inject_prim(node, prim):
    node._get_input_value = lambda name: prim if name == "prim" else None


# ── category / metadata ───────────────────────────────────────────────────────


def test_offset_transform_category():
    from node_graph.nodes._transform import OffsetTransformNode

    assert OffsetTransformNode.CATEGORY == "Transform"


def test_offset_transform_node_name():
    from node_graph.nodes._transform import OffsetTransformNode

    assert OffsetTransformNode.NODE_NAME == "Offset Transform"


def test_offset_transform_exported_from_package():
    from node_graph.nodes import OffsetTransformNode

    assert OffsetTransformNode is not None


def test_offset_transform_in_all_nodes():
    from node_graph.nodes import ALL_NODES, OffsetTransformNode

    assert OffsetTransformNode in ALL_NODES


# ── port structure ────────────────────────────────────────────────────────────


def test_offset_transform_has_prim_input(node_graph, qapp):
    ng, _ = node_graph
    node = _make_node(ng)
    assert node.get_input("prim") is not None


def test_offset_transform_has_prim_output(node_graph, qapp):
    ng, _ = node_graph
    node = _make_node(ng)
    assert node.get_output("prim") is not None


def test_offset_transform_translate_defaults(node_graph, qapp):
    ng, _ = node_graph
    node = _make_node(ng)
    for name in ("tx", "ty", "tz"):
        assert node.get_property(name) == "0", f"expected default '0' for {name}"


def test_offset_transform_rotate_defaults(node_graph, qapp):
    ng, _ = node_graph
    node = _make_node(ng)
    for name in ("rx", "ry", "rz"):
        assert node.get_property(name) == "0", f"expected default '0' for {name}"


def test_offset_transform_scale_defaults(node_graph, qapp):
    ng, _ = node_graph
    node = _make_node(ng)
    for name in ("sx", "sy", "sz"):
        assert node.get_property(name) == "1", f"expected default '1' for {name}"


# ── execute: no prim ─────────────────────────────────────────────────────────


def test_offset_transform_no_prim_returns_none(node_graph, qapp, stage):
    ng, _ = node_graph
    node = _make_node(ng)
    result = node.execute(stage)
    assert result["prim"] is None


# ── execute: offsets apply correctly ─────────────────────────────────────────


def test_offset_translate_adds_to_zero(node_graph, qapp, stage):
    prim = _make_cube(stage, "/World/OffsetCube1")
    ng, _ = node_graph
    node = _make_node(ng)
    _inject_prim(node, prim)
    node.set_property("tx", "10")
    node.set_property("ty", "20")
    node.set_property("tz", "30")

    result = node.execute(stage)

    assert result["prim"] is prim
    t = _get_translate(prim)
    assert abs(t[0] - 10.0) < 1e-5
    assert abs(t[1] - 20.0) < 1e-5
    assert abs(t[2] - 30.0) < 1e-5


def test_offset_translate_adds_to_existing(node_graph, qapp, stage):
    prim = _make_cube(stage, "/World/OffsetCube2")
    xf = UsdGeom.Xformable(prim)
    xf.AddTranslateOp().Set(Gf.Vec3d(5, 10, 15))

    ng, _ = node_graph
    node = _make_node(ng)
    _inject_prim(node, prim)
    node.set_property("tx", "5")
    node.set_property("ty", "-5")
    node.set_property("tz", "0")

    node.execute(stage)

    t = _get_translate(prim)
    assert abs(t[0] - 10.0) < 1e-5
    assert abs(t[1] - 5.0) < 1e-5
    assert abs(t[2] - 15.0) < 1e-5


def test_offset_scale_multiplies(node_graph, qapp, stage):
    prim = _make_cube(stage, "/World/OffsetCube3")
    xf = UsdGeom.Xformable(prim)
    xf.AddTranslateOp().Set(Gf.Vec3d(0, 0, 0))
    xf.AddRotateXYZOp().Set(Gf.Vec3f(0, 0, 0))
    xf.AddScaleOp().Set(Gf.Vec3f(2.0, 2.0, 2.0))

    ng, _ = node_graph
    node = _make_node(ng)
    _inject_prim(node, prim)
    node.set_property("sx", "3")
    node.set_property("sy", "0.5")
    node.set_property("sz", "1")

    node.execute(stage)

    s = _get_scale(prim)
    assert abs(s[0] - 6.0) < 1e-5
    assert abs(s[1] - 1.0) < 1e-5
    assert abs(s[2] - 2.0) < 1e-5


def test_offset_rotate_adds(node_graph, qapp, stage):
    prim = _make_cube(stage, "/World/OffsetCube4")
    xf = UsdGeom.Xformable(prim)
    xf.AddTranslateOp().Set(Gf.Vec3d(0, 0, 0))
    xf.AddRotateXYZOp().Set(Gf.Vec3f(10.0, 0.0, 0.0))

    ng, _ = node_graph
    node = _make_node(ng)
    _inject_prim(node, prim)
    node.set_property("rx", "20")

    node.execute(stage)

    r = _get_rotate(prim)
    assert abs(r[0] - 30.0) < 1e-5


def test_offset_scale_one_unchanged(node_graph, qapp, stage):
    """Scale offset of (1,1,1) should preserve existing scale."""
    prim = _make_cube(stage, "/World/OffsetCube5")
    xf = UsdGeom.Xformable(prim)
    xf.AddTranslateOp().Set(Gf.Vec3d(0, 0, 0))
    xf.AddRotateXYZOp().Set(Gf.Vec3f(0, 0, 0))
    xf.AddScaleOp().Set(Gf.Vec3f(3.0, 3.0, 3.0))

    ng, _ = node_graph
    node = _make_node(ng)
    _inject_prim(node, prim)

    node.execute(stage)

    s = _get_scale(prim)
    assert abs(s[0] - 3.0) < 1e-5


def test_offset_returns_same_prim(node_graph, qapp, stage):
    prim = _make_cube(stage, "/World/OffsetCube6")
    ng, _ = node_graph
    node = _make_node(ng)
    _inject_prim(node, prim)

    result = node.execute(stage)
    assert result["prim"] is prim


# ── chaining ──────────────────────────────────────────────────────────────────


def test_offset_chaining(node_graph, qapp, stage):
    """Two sequential OffsetTransformNode executions accumulate correctly."""
    prim = _make_cube(stage, "/World/OffsetCubeChain")
    xf = UsdGeom.Xformable(prim)
    xf.AddTranslateOp().Set(Gf.Vec3d(0, 0, 0))

    ng, _ = node_graph

    node1 = _make_node(ng)
    _inject_prim(node1, prim)
    node1.set_property("ty", "50")
    node1.execute(stage)

    node2 = _make_node(ng)
    _inject_prim(node2, prim)
    node2.set_property("tx", "10")
    node2.execute(stage)

    t = _get_translate(prim)
    assert abs(t[0] - 10.0) < 1e-5
    assert abs(t[1] - 50.0) < 1e-5
