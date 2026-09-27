"""Test that whole-vector @Node.vecParam@ expressions resolve, persist, and stay live
across chained Vector Math Ops chunks -- the vector equivalent of test_math_ops_expression.py.
"""
from __future__ import annotations

import importlib

import pytest
from pxr import Sdf, Usd

from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_core import ChunkConfig, SceneSender
from usd_chunks.chunk_stage import initialize as init_chunk_usd


def _chunk_path(name: str) -> str:
    """Where a chunk added through the scene actually lands.

    add_chunk routes a context-less chunk into the scene's default chunkgroup, so its
    parent is that group rather than the /chunks scope.
    """
    from usd_chunks import chunk_registry
    from usd_chunks.chunk_stage import get_chunk_prim_path

    scene = chunk_registry.ChunkRegistry().scene
    ctx = scene.peek_root_context() if scene is not None else None
    root = get_chunk_prim_path(ctx) if ctx is not None else "/chunks"
    return f"{root}/{name}"



class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


def _make_vector_math_ops(mod, name: str):
    c = mod.main()
    c.name = name
    c._config = ChunkConfig(module="vector_math_ops")
    c._module = mod
    return c


@pytest.fixture()
def scene():
    stage = Usd.Stage.CreateInMemory()
    sc = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(sc, "pytest")
    init_chunk_usd(stage, sc)
    yield sc


class TestVectorMathOpsExpression:
    def setup_method(self):
        self.mod = importlib.import_module("chunks.utils.vector_math_ops")

    def test_expression_resolves_and_stays_live(self, scene):
        node1 = _make_vector_math_ops(self.mod, "Vec1")
        node2 = _make_vector_math_ops(self.mod, "Vec2")
        scene.add_chunk(node1)
        scene.add_chunk(node2)

        node1.get_param("a").value = (1.0, 2.0, 3.0)
        node1.get_param("b").value = (4.0, 5.0, 6.0)
        assert node1.get_param("result").value == pytest.approx((5.0, 7.0, 9.0))

        a2 = node2.get_param("a")
        a2.value = "@Vec1.result@"

        assert a2.raw_value == "@Vec1.result@"
        assert a2.value == pytest.approx((5.0, 7.0, 9.0))

        # X/Y/Z child sliders must also show the resolved numbers, not the expression.
        assert a2[0].value == pytest.approx(5.0)
        assert a2[1].value == pytest.approx(7.0)
        assert a2[2].value == pytest.approx(9.0)

        # Live update -- mutation on node1 must propagate through without re-authoring.
        node1.get_param("a").value = (10.0, 20.0, 30.0)
        assert a2.raw_value == "@Vec1.result@", "expression must survive the mutation"
        assert a2.value == pytest.approx((14.0, 25.0, 36.0))

    def test_usd_connection_authored_and_reconstructed_on_reload(self, scene):
        node1 = _make_vector_math_ops(self.mod, "Vec1")
        node2 = _make_vector_math_ops(self.mod, "Vec2")
        scene.add_chunk(node1)
        scene.add_chunk(node2)

        node1.get_param("a").value = (1.0, 2.0, 3.0)
        node1.get_param("b").value = (4.0, 5.0, 6.0)
        node2.get_param("a").value = "@Vec1.result@"

        stage = scene.get_stage()
        attr = stage.GetAttributeAtPath(Sdf.Path(f'{_chunk_path("Vec2")}.inputs:a'))
        assert attr.IsValid()
        assert attr.GetTypeName() == Sdf.ValueTypeNames.Float3
        assert len(attr.GetConnections()) == 1
        assert str(attr.GetConnections()[0]) == f'{_chunk_path("Vec1")}.outputs:result'

        # Simulate a fresh process reopening the same stage: a brand new ChunkVectorBase
        # instance restored purely from the (connections-only, no string value) USD attr
        # must reconstruct the @Node.param@ expression, mirroring _connect_param_attr's
        # scalar reconstruction via _expression_from_connections.
        fresh = self.mod.main()
        fresh_a = fresh.get_param("a")
        assert fresh_a.raw_value != "@Vec1.result@"
        fresh_a._connect_param_attr(attr)
        assert fresh_a.raw_value == "@Vec1.result@"

    def test_connection_cleared_when_operation_hides_the_param(self, scene):
        """C is only used by Multiply Add. Wiring C via expression then switching to an
        op that hides it (Add) must clear both the Python-side expression and the USD
        connection -- otherwise switching back would silently reconnect to a source the
        user never re-picked, with no indication anything is still wired."""
        node1 = _make_vector_math_ops(self.mod, "Vec1")
        node2 = _make_vector_math_ops(self.mod, "Vec2")
        scene.add_chunk(node1)
        scene.add_chunk(node2)

        node1.get_param("a").value = (1.0, 2.0, 3.0)
        node1.get_param("b").value = (4.0, 5.0, 6.0)

        node2.get_param("operation").value = "Multiply Add"
        c2 = node2.get_param("c")
        c2.value = "@Vec1.result@"
        assert c2.raw_value == "@Vec1.result@"

        stage = scene.get_stage()
        attr = stage.GetAttributeAtPath(Sdf.Path(f'{_chunk_path("Vec2")}.inputs:c'))
        assert len(attr.GetConnections()) == 1

        # Add doesn't use C -- switching to it must clear the connection, not just hide it.
        node2.get_param("operation").value = "Add"
        assert c2.raw_value == (0.0, 0.0, 0.0)
        assert c2.has_state(chunk.ChunkFlags.State.HIDDEN)
        assert len(attr.GetConnections()) == 0, "USD connection must be cleared, not just hidden"

        # Switching back must NOT silently reconnect to the old source.
        node2.get_param("operation").value = "Multiply Add"
        assert c2.raw_value == (0.0, 0.0, 0.0)
        assert len(attr.GetConnections()) == 0

    def test_downstream_connection_cleared_when_driver_output_type_changes(self, scene):
        """The DRIVER (source) node's own operation change is what breaks the link this
        time -- not the receiving node's. Vec1.result (a Vector) drives Vec2.a; switching
        Vec1 to a float-output op (Dot Product) makes 'result' inactive (execute() stops
        writing to it), so Vec2's connection must be cleared even though Vec2's own
        operation never changed. This is the exact scenario from the user's screenshot:
        driver switched Add -> Dot Product, downstream connection kept lingering."""
        driver = _make_vector_math_ops(self.mod, "Vec1")
        driven = _make_vector_math_ops(self.mod, "Vec2")
        scene.add_chunk(driver)
        scene.add_chunk(driven)

        driver.get_param("a").value = (17.18, 7.19, 37.17)
        a2 = driven.get_param("a")
        a2.value = "@Vec1.result@"
        assert a2.raw_value == "@Vec1.result@"

        stage = scene.get_stage()
        attr = stage.GetAttributeAtPath(Sdf.Path(f'{_chunk_path("Vec2")}.inputs:a'))
        assert len(attr.GetConnections()) == 1

        # Driver switches to a float-output op -- 'result' (vector) goes inactive.
        driver.get_param("operation").value = "Dot Product"
        assert a2.raw_value == (0.0, 0.0, 0.0), "downstream must be cleared, not left stale"
        assert len(attr.GetConnections()) == 0

        # Switching the driver back must NOT silently reconnect the downstream node.
        driver.get_param("operation").value = "Add"
        assert a2.raw_value == (0.0, 0.0, 0.0)
        assert len(attr.GetConnections()) == 0
