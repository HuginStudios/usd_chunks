"""Test that OUTPUT param expressions survive repeated mutations across chained Math Ops chunks."""
from __future__ import annotations

import importlib

import pytest
from pxr import Sdf, Usd

from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_core import ChunkConfig, SceneSender
from usd_chunks.plugins.chunk_usd import initialize as init_chunk_usd


def _chunk_path(name: str) -> str:
    """Where a chunk added through the scene actually lands.

    add_chunk routes a context-less chunk into the scene's default chunkgroup, so its
    parent is that group rather than the /chunks scope.
    """
    from usd_chunks import chunk_registry
    from usd_chunks.plugins.chunk_usd import get_chunk_prim_path

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


def _make_math_ops(mod, name: str):
    c = mod.main()
    c.name = name
    c._config = ChunkConfig(module="math_ops")
    c._module = mod
    return c


def _exec(mod, chunk):
    mod.execute(chunk)


@pytest.fixture()
def scene():
    stage = Usd.Stage.CreateInMemory()
    sc = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(sc, "pytest")
    init_chunk_usd(stage, sc)
    yield sc


class TestMathOpsExpression:
    def setup_method(self):
        self.mod = importlib.import_module("scripts.math_ops")

    def test_expression_survives_first_execute(self, scene):
        node1 = _make_math_ops(self.mod, "Mult1")
        node2 = _make_math_ops(self.mod, "Mult2")
        scene.add_chunk(node1)
        scene.add_chunk(node2)
        node1.get_param("operation").value = "Multiply"
        node2.get_param("operation").value = "Multiply"

        node1.get_param("a").value = 20.0
        node1.get_param("b").value = 1.0
        _exec(self.mod, node1)

        node2.get_param("a").value = "@Mult1.result@"
        node2.get_param("b").value = 1.0

        assert node2.get_param("a").raw_value == "@Mult1.result@", "expression set correctly"

        _exec(self.mod, node1)

        assert node2.get_param("a").raw_value == "@Mult1.result@", \
            "raw_value must still be the expression after first execute"

    def test_expression_survives_second_mutation(self, scene):
        node1 = _make_math_ops(self.mod, "Mult1")
        node2 = _make_math_ops(self.mod, "Mult2")
        scene.add_chunk(node1)
        scene.add_chunk(node2)
        node1.get_param("operation").value = "Multiply"
        node2.get_param("operation").value = "Multiply"

        node1.get_param("a").value = 20.0
        node1.get_param("b").value = 1.0
        node2.get_param("a").value = "@Mult1.result@"
        node2.get_param("b").value = 1.0

        _exec(self.mod, node1)
        _exec(self.mod, node2)

        assert node2.get_param("result").value == pytest.approx(20.0), "node2.result = 20 after first execute"

        # Second mutation — expression must survive
        node1.get_param("a").value = 25.0
        _exec(self.mod, node1)

        assert node2.get_param("a").raw_value == "@Mult1.result@", \
            "raw_value must still be the expression after second mutation"

        _exec(self.mod, node2)

        assert node2.get_param("result").value == pytest.approx(25.0), \
            "node2.result must equal 25 after node1.A changes to 25"

    def test_connection_cleared_when_operation_hides_the_param(self, scene):
        """C is only used by 3-arg ops like Multiply Add. Wiring C via expression then
        switching to a 2-arg op (Add) that hides it must clear both the Python-side
        expression and the USD connection — otherwise switching back would silently
        reconnect to a source the user never re-picked, with no indication anything
        is still wired."""
        node1 = _make_math_ops(self.mod, "Mult1")
        node2 = _make_math_ops(self.mod, "Mult2")
        scene.add_chunk(node1)
        scene.add_chunk(node2)

        node1.get_param("a").value = 20.0
        node1.get_param("b").value = 1.0

        node2.get_param("operation").value = "Multiply Add"
        c2 = node2.get_param("c")
        c2.value = "@Mult1.result@"
        assert c2.raw_value == "@Mult1.result@"

        stage = scene.get_stage()
        attr = stage.GetAttributeAtPath(Sdf.Path(f'{_chunk_path("Mult2")}.inputs:c'))
        assert len(attr.GetConnections()) == 1

        # Add doesn't use C — switching to it must clear the connection, not just hide it.
        node2.get_param("operation").value = "Add"
        assert c2.raw_value == 0.0
        assert c2.has_state(chunk.ChunkFlags.State.HIDDEN)
        assert len(attr.GetConnections()) == 0, "USD connection must be cleared, not just hidden"

        # Switching back must NOT silently reconnect to the old source.
        node2.get_param("operation").value = "Multiply Add"
        assert c2.raw_value == 0.0
        assert len(attr.GetConnections()) == 0

    def test_expression_via_value_changed_chain(self, scene):
        """Simulates the app: mutations go through value_changed → DagNode → auto-execute."""
        node1 = _make_math_ops(self.mod, "Mult1")
        node2 = _make_math_ops(self.mod, "Mult2")
        scene.add_chunk(node1)
        scene.add_chunk(node2)
        node1.get_param("operation").value = "Multiply"
        node2.get_param("operation").value = "Multiply"

        node1.get_param("b").value = 1.0
        node2.get_param("b").value = 1.0
        node2.get_param("a").value = "@Mult1.result@"

        # First mutation — triggers value_changed chain
        node1.get_param("a").value = 20.0

        assert node2.get_param("a").raw_value == "@Mult1.result@", \
            "expression must survive first value_changed"
        assert node2.get_param("result").value == pytest.approx(20.0), \
            "node2.result must be 20 via auto-execute chain"

        # Second mutation
        node1.get_param("a").value = 25.0

        assert node2.get_param("a").raw_value == "@Mult1.result@", \
            "expression must survive second value_changed"
        assert node2.get_param("result").value == pytest.approx(25.0), \
            "node2.result must be 25 via auto-execute chain"
