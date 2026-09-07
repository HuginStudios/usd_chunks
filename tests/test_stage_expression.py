"""Stage expression variables: ${NAME} resolves in any param, and studio-authored
variables keep working.

USD substitutes ${NAME} into asset paths and variant selections only, and only from the
root layer stack -- a sublayer's expressionVariables is ignored outright. Chunk layers are
sublayers, so both halves of this feature exist because of that: usd_chunks resolves the
variables itself at param-read time (which is what makes a prim path work at all), and the
Stage Expression chunk authors into the session layer, where an opinion composes and
overrides the root layer instead of being dropped.
"""
from __future__ import annotations

import importlib

import pytest
from pxr import Sdf, Usd

from usd_chunks import chunk, chunk_registry, chunk_utils
from usd_chunks.chunk_core import ChunkConfig, SceneSender
from usd_chunks.plugins.chunk_usd import ChunkStageSync


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


def _mk(name: str) -> chunk.Chunk:
    c = chunk.Chunk(name)
    c._config = ChunkConfig(module="prim")
    return c


@pytest.fixture()
def scene():
    stage = Usd.Stage.CreateInMemory()
    sc = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(sc, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    ChunkStageSync().initialize(stage, sc)
    yield sc


# ---------------------------------------------------------------------------
# chunk_utils: reading, resolving, authoring
# ---------------------------------------------------------------------------

def test_session_variables_are_read_back():
    stage = Usd.Stage.CreateInMemory()
    chunk_utils.set_stage_expression_variables(stage, {"SHOT": "sh010"})
    assert chunk_utils.stage_expression_variables(stage) == {"SHOT": "sh010"}


def test_session_overrides_a_root_layer_variable():
    """The whole reason the chunk authors into the session layer: a studio default in the
    opened file is overridden rather than fought with, and their file stays untouched."""
    stage = Usd.Stage.CreateInMemory()
    stage.GetRootLayer().expressionVariables = {"SHOT": "sh010", "SHOW": "hydra"}
    chunk_utils.set_stage_expression_variables(stage, {"SHOT": "sh020"})

    variables = chunk_utils.stage_expression_variables(stage)
    assert variables["SHOT"] == "sh020", "session opinion wins"
    assert variables["SHOW"] == "hydra", "and the studio's other variables survive"
    assert stage.GetRootLayer().expressionVariables["SHOT"] == "sh010", "their file is untouched"


def test_bare_reference_substitutes_inside_a_path():
    """The form USD itself never substitutes into, and the one a prim path needs."""
    stage = Usd.Stage.CreateInMemory()
    chunk_utils.set_stage_expression_variables(stage, {"SHOT": "sh010"})
    assert chunk_utils.resolve_stage_expression("/World/${SHOT}/geo", stage) == "/World/sh010/geo"


def test_usd_delimited_expression_uses_usds_own_evaluator():
    """Studio syntax evaluates by USD's rules, not by a reimplementation of them."""
    stage = Usd.Stage.CreateInMemory()
    chunk_utils.set_stage_expression_variables(stage, {"ASSET": "hero"})
    assert chunk_utils.resolve_stage_expression('`"./${ASSET}.usda"`', stage) == "./hero.usda"
    assert chunk_utils.resolve_stage_expression("`${ASSET}`", stage) == "hero"


def test_unknown_variable_is_left_visible():
    """Blanking it would author into the wrong place and read as a typo in the field."""
    stage = Usd.Stage.CreateInMemory()
    assert chunk_utils.resolve_stage_expression("/World/${SHOT}/geo", stage) == "/World/${SHOT}/geo"


def test_clearing_removes_the_metadata():
    stage = Usd.Stage.CreateInMemory()
    chunk_utils.set_stage_expression_variables(stage, {"SHOT": "sh010"})
    chunk_utils.set_stage_expression_variables(stage, {})
    assert chunk_utils.stage_expression_variables(stage) == {}


def test_has_stage_expression_recognises_both_forms_and_nothing_else():
    assert chunk_utils.has_stage_expression("/World/${SHOT}/geo")
    assert chunk_utils.has_stage_expression("`${SHOT}`")
    assert not chunk_utils.has_stage_expression("/World/geo")
    assert not chunk_utils.has_stage_expression(None)


# ---------------------------------------------------------------------------
# param resolution
# ---------------------------------------------------------------------------

def test_lineedit_resolves_a_variable(scene) -> None:
    chunk_utils.set_stage_expression_variables(scene.get_stage(), {"SHOT": "sh010"})
    c = _mk("dst")
    p = chunk.LineEdit("primpath", default_value="")
    c.add_param(p)
    scene.add_chunk(c)

    p.value = "/World/${SHOT}/geo"

    assert p.value == "/World/sh010/geo"
    assert p.raw_value == "/World/${SHOT}/geo", "the authored text is what's stored"


def test_value_follows_the_variable(scene) -> None:
    """Nothing is cached, so changing a variable takes effect everywhere at once."""
    stage = scene.get_stage()
    chunk_utils.set_stage_expression_variables(stage, {"SHOT": "sh010"})
    c = _mk("dst")
    p = chunk.LineEdit("primpath", default_value="")
    c.add_param(p)
    scene.add_chunk(c)
    p.value = "/World/${SHOT}/geo"
    assert p.value == "/World/sh010/geo"

    chunk_utils.set_stage_expression_variables(stage, {"SHOT": "sh020"})

    assert p.value == "/World/sh020/geo"


def test_param_wire_resolves_before_the_variable_does(scene) -> None:
    """A wire can deliver a string that itself carries a ${NAME}; the refs must be
    substituted first or the variable arrives unexpanded."""
    chunk_utils.set_stage_expression_variables(scene.get_stage(), {"SHOT": "sh010"})
    src = _mk("src")
    src.add_param(chunk.LineEdit("base", default_value=""))
    scene.add_chunk(src)
    src.get_param("base").value = "/World/${SHOT}"

    dst = _mk("dst")
    p = chunk.LineEdit("primpath", default_value="")
    dst.add_param(p)
    scene.add_chunk(dst)

    p.value = "@src.base@/geo"

    assert p.value == "/World/sh010/geo"


def test_numeric_param_reads_back_as_a_number(scene) -> None:
    """A Slider holding a variable has to come back typed, or the USD attr write and every
    consumer downstream gets a str."""
    chunk_utils.set_stage_expression_variables(scene.get_stage(), {"COUNT": "12"})
    c = _mk("dst")
    p = chunk.Slider("count", default_value=0, is_int=True)
    c.add_param(p)
    scene.add_chunk(c)

    p.value = "${COUNT}"

    assert p.value == 12
    assert isinstance(p.value, int)


def test_numeric_param_keeps_its_text_in_usd(scene) -> None:
    """A Float attr cannot hold "${COUNT}" as its value, so the text goes to customData --
    the same split a numeric param wire already makes. Without it the param reloads holding
    whatever the variable resolved to when it was saved and stops tracking it."""
    stage = scene.get_stage()
    chunk_utils.set_stage_expression_variables(stage, {"COUNT": "12"})
    c = _mk("dst")
    p = chunk.Slider("count", default_value=0, is_int=True)
    c.add_param(p)
    scene.add_chunk(c)

    p.value = "${COUNT}"

    attr = next(
        (a for prim in stage.TraverseAll() for a in prim.GetAttributes()
         if a.GetName() == "inputs:count"),
        None,
    )
    assert attr is not None, "the param should have an inputs: attr on its chunk prim"
    assert attr.GetTypeName() != Sdf.ValueTypeNames.String, "typed by the param's default"
    assert chunk_utils.read_expression_text(attr) == "${COUNT}"


def test_a_plain_value_is_untouched(scene) -> None:
    """No regression for the overwhelmingly common case of a field with no variable in it."""
    c = _mk("dst")
    p = chunk.LineEdit("primpath", default_value="")
    c.add_param(p)
    scene.add_chunk(c)

    p.value = "/World/geo"

    assert p.value == "/World/geo"


# ---------------------------------------------------------------------------
# the Stage Expression chunk
# ---------------------------------------------------------------------------

def _stage_expression(scene, spec: str):
    stage_expression = importlib.import_module("scripts.stage_expression")

    node = stage_expression.main()
    node._config = ChunkConfig(module="stage_expression")
    scene.add_chunk(node)
    node.get_param("variables").value = spec
    stage_expression._sync_variables(node)
    stage_expression.execute(node)
    return node, stage_expression


def test_chunk_declares_its_variables(scene) -> None:
    node, _ = _stage_expression(scene, "SHOT = sh010\nASSET = hero")

    assert chunk_utils.stage_expression_variables(scene.get_stage()) == {
        "SHOT": "sh010", "ASSET": "hero",
    }
    assert node.get_param("SHOT") is not None, "each line becomes a connectable port"


def test_editing_a_lofted_param_changes_the_variable(scene) -> None:
    """The port is the control -- the spec only declares the name and its default."""
    node, mod = _stage_expression(scene, "SHOT = sh010")

    node.get_param("SHOT").value = "sh020"
    mod.execute(node)

    assert chunk_utils.stage_expression_variables(scene.get_stage())["SHOT"] == "sh020"


def test_removing_a_line_removes_the_variable(scene) -> None:
    node, mod = _stage_expression(scene, "SHOT = sh010\nASSET = hero")

    node.get_param("variables").value = "SHOT = sh010"
    mod._sync_variables(node)
    mod.execute(node)

    assert chunk_utils.stage_expression_variables(scene.get_stage()) == {"SHOT": "sh010"}


def test_two_chunks_do_not_erase_each_other(scene) -> None:
    """Each drops only the names it recorded owning, so a second chunk's variables survive
    a re-execute of the first."""
    a, mod = _stage_expression(scene, "SHOT = sh010")
    b, _ = _stage_expression(scene, "ASSET = hero")

    mod.execute(a)

    assert chunk_utils.stage_expression_variables(scene.get_stage()) == {
        "SHOT": "sh010", "ASSET": "hero",
    }


def test_studio_variables_survive_and_are_reported(scene) -> None:
    stage = scene.get_stage()
    stage.GetRootLayer().expressionVariables = {"SHOW": "hydra"}

    node, _ = _stage_expression(scene, "SHOT = sh010")

    assert stage.GetRootLayer().expressionVariables == {"SHOW": "hydra"}, "their file is untouched"
    assert "SHOW = hydra" in node.get_param("composed").value
    assert "SHOT = sh010" in node.get_param("composed").value


def test_reserved_and_malformed_names_are_skipped(scene) -> None:
    node, _ = _stage_expression(scene, "variables = x\n1BAD = y\nGOOD = z")

    assert chunk_utils.stage_expression_variables(scene.get_stage()) == {"GOOD": "z"}
    assert node.get_param("variables").value.startswith("variables"), "the spec itself is intact"


def test_load_lofts_the_variables_back(scene) -> None:
    """The spec restores as a string; the params it declares do not restore at all."""
    stage_expression = importlib.import_module("scripts.stage_expression")

    node = stage_expression.main()
    node._config = ChunkConfig(module="stage_expression")
    scene.add_chunk(node)
    node.get_param("variables").value = "SHOT = sh010"
    for p in list(stage_expression._variables(node)):
        node.remove_param_or_group(p)
    assert node.get_param("SHOT") is None

    stage_expression._chunk_loaded(node, None, None)

    assert node.get_param("SHOT") is not None
