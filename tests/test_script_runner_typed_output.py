"""Typed outputs on the Python (script_runner) chunk.

A USD attribute's value type is fixed at creation, so the chunk exposes one output param
per type (string/int/float/bool). The "output_type" menu picks which one `result` is
coerced onto; the others are cleared. This is what lets a computed numeric value flow into
a numeric downstream input, which a String output cannot do.
"""
from __future__ import annotations

import importlib

import pytest
from pxr import Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import ChunkConfig
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_flags import ChunkFlags
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


class TestTypedOutputs:
    def setup_method(self):
        self.mod = importlib.import_module("chunks.core.script_runner")

    def test_all_typed_outputs_exist(self, scene):
        node = _make(self.mod, "Python")
        for name in ("result", "result_int", "result_float", "result_bool"):
            assert node.get_param(name) is not None, name
        assert node.get_param("output_type") is not None

    def test_default_is_string(self, scene):
        node = _make(self.mod, "Python")
        scene.add_chunk(node)
        node.get_param("code").value = "result = 3"
        self.mod.execute(node)
        assert node.get_param("result").value == "3"

    def test_int_output(self, scene):
        node = _make(self.mod, "Python")
        scene.add_chunk(node)
        node.get_param("output_type").value = "int"
        node.get_param("code").value = "result = 2 + 5"
        self.mod.execute(node)

        assert node.get_param("result_int").value == 7
        assert isinstance(node.get_param("result_int").value, int)

    def test_float_output(self, scene):
        node = _make(self.mod, "Python")
        scene.add_chunk(node)
        node.get_param("output_type").value = "float"
        node.get_param("code").value = "result = 1.5 * 2"
        self.mod.execute(node)

        assert node.get_param("result_float").value == pytest.approx(3.0)

    def test_bool_output(self, scene):
        node = _make(self.mod, "Python")
        scene.add_chunk(node)
        node.get_param("output_type").value = "bool"
        node.get_param("code").value = "result = 5 > 2"
        self.mod.execute(node)

        assert node.get_param("result_bool").value is True

    def test_string_to_int_coercion(self, scene):
        node = _make(self.mod, "Python")
        scene.add_chunk(node)
        node.get_param("output_type").value = "int"
        node.get_param("code").value = "result = '42'"
        self.mod.execute(node)

        assert node.get_param("result_int").value == 42

    def test_only_selected_output_carries_value(self, scene):
        """Switching type clears the previously-populated output."""
        node = _make(self.mod, "Python")
        scene.add_chunk(node)

        node.get_param("output_type").value = "int"
        node.get_param("code").value = "result = 9"
        self.mod.execute(node)
        assert node.get_param("result_int").value == 9
        assert node.get_param("result").value == ""      # string output cleared
        assert node.get_param("result_float").value == 0

        node.get_param("output_type").value = "string"
        self.mod.execute(node)
        assert node.get_param("result").value == "9"
        assert node.get_param("result_int").value == 0   # int output now cleared

    def test_bad_coercion_does_not_raise(self, scene):
        node = _make(self.mod, "Python")
        scene.add_chunk(node)
        node.get_param("output_type").value = "int"
        node.get_param("code").value = "result = 'not a number'"
        self.mod.execute(node)  # must not raise

        assert node.get_param("result_int").value == 0


class TestOnlySelectedOutputShows:
    """Four typed outputs is what the ports need; it is not what the panel needs.

    Only one of them ever carries a value, so the other three are noise on the node and
    three more ports to mis-wire.
    """

    def setup_method(self):
        self.mod = importlib.import_module("chunks.core.script_runner")

    @pytest.mark.parametrize(
        "chosen, kept",
        [
            ("string", "result"),
            ("int", "result_int"),
            ("float", "result_float"),
            ("bool", "result_bool"),
        ],
    )
    def test_the_others_are_hidden(self, scene, chosen, kept):
        node = _make(self.mod, "Python")
        node.get_param("output_type").value = chosen
        self.mod._show_only_selected_output(node)

        def hidden(name):
            return node.get_param(name).has_state(ChunkFlags.State.HIDDEN)

        assert not hidden(kept), f"{chosen} selected but {kept} is hidden"
        for other in ("result", "result_int", "result_float", "result_bool"):
            if other != kept:
                assert hidden(other), f"{chosen} selected but {other} is still showing"

    def test_switching_back_shows_the_first_one_again(self, scene):
        """Hiding has to be reversible, or the panel empties out as you try the menu."""
        node = _make(self.mod, "Python")

        node.get_param("output_type").value = "int"
        self.mod._show_only_selected_output(node)
        assert node.get_param("result").has_state(ChunkFlags.State.HIDDEN)

        node.get_param("output_type").value = "string"
        self.mod._show_only_selected_output(node)
        assert not node.get_param("result").has_state(ChunkFlags.State.HIDDEN)


class TestBlankOutputsKeepTheirType:
    """A float output blanks to 0.0, not 0.

    The node graph reads a port's colour off the param's CURRENT value
    (chunk_qt_nodegraph._param_port_type), so an int in a float param paints the float port
    with the int colour -- visible as int and float ports looking identical until a real
    float is assigned, which is what made it look fine as soon as a script set `result`.
    """

    def setup_method(self):
        self.mod = importlib.import_module("chunks.core.script_runner")

    def test_clearing_leaves_a_float_holding_a_float(self, scene):
        node = _make(self.mod, "Python")
        self.mod._clear_outputs(node, keep="result_int")

        value = node.get_param("result_float").value
        assert isinstance(value, float), (
            f"result_float was blanked to {value!r} ({type(value).__name__}); its port will "
            "be drawn in the int colour"
        )

    def test_each_output_blanks_to_its_own_type(self, scene):
        node = _make(self.mod, "Python")
        self.mod._clear_outputs(node)

        assert isinstance(node.get_param("result").value, str)
        assert isinstance(node.get_param("result_bool").value, bool)
        assert isinstance(node.get_param("result_float").value, float)
        int_value = node.get_param("result_int").value
        assert isinstance(int_value, int) and not isinstance(int_value, bool)
