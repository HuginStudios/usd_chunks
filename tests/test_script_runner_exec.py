"""Script Runner ("Python" chunk) runs via the reserved execute(node) hook, not a Run
button, and exposes whatever the script assigns to `result` on its OUTPUT param so it can
be wired into a downstream chunk -- e.g. feeding a computed prim name into Duplicate Prim's
destination path."""
from __future__ import annotations

import importlib

import pytest
from pxr import Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import ChunkConfig
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import initialize as init_chunk_usd


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture()
def scene():
    stage = Usd.Stage.CreateInMemory()
    sc = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(sc, "pytest")
    init_chunk_usd(stage, sc)
    yield sc


def _make(mod, name: str):
    c = mod.main()
    c.name = name
    c._config = ChunkConfig(module=mod.__name__.rsplit(".", 1)[-1])
    c._module = mod
    return c


class TestScriptRunnerExecute:
    def setup_method(self):
        self.mod = importlib.import_module("chunks.core.script_runner")

    def test_no_run_button(self, scene):
        node = _make(self.mod, "Python")
        assert node.get_param("run") is None

    def test_execute_sets_result_from_script(self, scene):
        node = _make(self.mod, "Python")
        scene.add_chunk(node)

        node.get_param("code").value = "result = 1 + 2"
        self.mod.execute(node)

        assert node.get_param("result").value == "3"

    def test_empty_code_clears_result(self, scene):
        node = _make(self.mod, "Python")
        scene.add_chunk(node)

        node.get_param("code").value = "result = 'hello'"
        self.mod.execute(node)
        assert node.get_param("result").value == "hello"

        node.get_param("code").value = ""
        self.mod.execute(node)
        assert node.get_param("result").value == ""

    def test_script_error_does_not_raise(self, scene):
        node = _make(self.mod, "Python")
        scene.add_chunk(node)

        node.get_param("code").value = "1 / 0"
        self.mod.execute(node)  # must not raise

    def test_stage_and_prim_available_in_globals(self, scene):
        node = _make(self.mod, "Python")
        scene.add_chunk(node)

        node.get_param("code").value = "result = stage is not None"
        self.mod.execute(node)

        assert node.get_param("result").value == "True"

    def test_result_wires_into_downstream_chunk(self, scene):
        """The cross-chunk case the feature exists for: a computed name feeding another
        chunk's input via an @Node.param@ expression, auto-re-cooking on upstream change."""
        dup_mod = importlib.import_module("chunks.usd.duplicate_prim")

        script_node = _make(self.mod, "Python")
        dup_node = _make(dup_mod, "Duplicate")
        scene.add_chunk(script_node)
        scene.add_chunk(dup_node)

        script_node.get_param("code").value = "result = 'FlowerPotA_copy1'"
        self.mod.execute(script_node)
        assert script_node.get_param("result").value == "FlowerPotA_copy1"

        dup_node.get_param("dest_path").connect_value(script_node.get_param("result"))
        assert dup_node.get_param("dest_path").value == "FlowerPotA_copy1"

        # Upstream changes -> downstream expression tracks it, matching the
        # cross-chunk connect_value behaviour already relied on by math_ops.
        script_node.get_param("code").value = "result = 'FlowerPotA_copy2'"
        self.mod.execute(script_node)

        assert dup_node.get_param("dest_path").value == "FlowerPotA_copy2"
