"""CollisionSetNode tests — behavior 4.1."""

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def collision_graph(node_graph, qapp):
    """node_graph with CollisionSetNode registered."""
    from node_graph.nodes._simulation import CollisionSetNode

    ng, CubeNodeCls = node_graph
    ng.register_node(CollisionSetNode)
    return ng, CollisionSetNode


# ── Category / metadata ───────────────────────────────────────────────────────


def test_collision_set_in_simulation_category():
    from node_graph.nodes._simulation import CollisionSetNode

    assert CollisionSetNode.CATEGORY == "Simulation"


def test_collision_set_node_name():
    from node_graph.nodes._simulation import CollisionSetNode

    assert CollisionSetNode.NODE_NAME == "Collision Set"


def test_collision_set_exported_from_package():
    from node_graph.nodes import CollisionSetNode

    assert CollisionSetNode is not None


# ── Port structure ────────────────────────────────────────────────────────────


def test_collision_set_has_prims_output(collision_graph, qapp):
    ng, CollisionSetNode = collision_graph
    node = ng.create_node(CollisionSetNode.type_)
    output_names = [p.name() for p in node.output_ports()]
    assert "prims" in output_names


# ── append_prim_path ──────────────────────────────────────────────────────────


def test_append_prim_path_to_empty(collision_graph, qapp):
    ng, CollisionSetNode = collision_graph
    node = ng.create_node(CollisionSetNode.type_)
    node.append_prim_path("/World/Floor")
    assert node.get_property("prim_paths") == "/World/Floor"


def test_append_prim_path_extends_existing(collision_graph, qapp):
    ng, CollisionSetNode = collision_graph
    node = ng.create_node(CollisionSetNode.type_)
    node.append_prim_path("/World/Floor")
    node.append_prim_path("/World/WallA")
    result = node.get_property("prim_paths")
    assert "/World/Floor" in result
    assert "/World/WallA" in result


def test_append_prim_path_newline_separated(collision_graph, qapp):
    ng, CollisionSetNode = collision_graph
    node = ng.create_node(CollisionSetNode.type_)
    node.append_prim_path("/A")
    node.append_prim_path("/B")
    lines = [
        l.strip() for l in node.get_property("prim_paths").splitlines() if l.strip()
    ]
    assert lines == ["/A", "/B"]


# ── execute() ────────────────────────────────────────────────────────────────


def test_execute_returns_valid_prim(collision_graph, stage, qapp):
    from pxr import UsdGeom

    ng, CollisionSetNode = collision_graph
    UsdGeom.Xform.Define(stage, "/CollisionFloor")
    node = ng.create_node(CollisionSetNode.type_)
    node.set_property("prim_paths", "/CollisionFloor")
    result = node.execute(stage)
    assert len(result["prims"]) == 1
    assert result["prims"][0].GetPath().pathString == "/CollisionFloor"


def test_execute_skips_invalid_path(collision_graph, stage, qapp, capsys):
    ng, CollisionSetNode = collision_graph
    node = ng.create_node(CollisionSetNode.type_)
    node.set_property("prim_paths", "/NoSuchPrimXYZ")
    result = node.execute(stage)
    assert result["prims"] == []
    out = capsys.readouterr().out
    assert "/NoSuchPrimXYZ" in out


def test_execute_filters_invalid_from_mixed(collision_graph, stage, qapp):
    from pxr import UsdGeom

    ng, CollisionSetNode = collision_graph
    UsdGeom.Xform.Define(stage, "/CollisionWall")
    node = ng.create_node(CollisionSetNode.type_)
    node.set_property("prim_paths", "/CollisionWall\n/BadPathXYZ")
    result = node.execute(stage)
    assert len(result["prims"]) == 1
    assert result["prims"][0].GetPath().pathString == "/CollisionWall"


def test_execute_skips_blank_lines(collision_graph, stage, qapp):
    from pxr import UsdGeom

    ng, CollisionSetNode = collision_graph
    UsdGeom.Xform.Define(stage, "/CollisionGround")
    node = ng.create_node(CollisionSetNode.type_)
    node.set_property("prim_paths", "\n/CollisionGround\n\n")
    result = node.execute(stage)
    assert len(result["prims"]) == 1


def test_execute_empty_text_returns_empty_list(collision_graph, stage, qapp):
    ng, CollisionSetNode = collision_graph
    node = ng.create_node(CollisionSetNode.type_)
    node.set_property("prim_paths", "")
    result = node.execute(stage)
    assert result["prims"] == []
