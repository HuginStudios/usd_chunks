"""Stage expression variables: declared here, resolved by USD where USD resolves them.

USD substitutes ${NAME} into asset paths and variant selections, from the root layer stack,
and nowhere else. usd_chunks does not extend that: a param carrying ${NAME} keeps the text
literally, and a chunk graph acts on a variable through a Stage Expression Filter, which is
a mechanism of ours rather than a place USD's own rule is quietly widened.

The Stage Expression chunk authors into the session layer, where an opinion composes over
the root layer instead of being dropped (a sublayer's expressionVariables is ignored
outright, and chunk layers are sublayers).
"""

from __future__ import annotations

import importlib

import pytest
from pxr import Sdf, Usd, UsdGeom

from usd_chunks import chunk, chunk_registry, chunk_utils
from usd_chunks.chunk_core import ChunkConfig, SceneSender
from usd_chunks.chunk_stage import ChunkStageSync


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
# chunk_utils: reading and authoring
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


# ---------------------------------------------------------------------------
# where a variable applies -- USD's rule, not ours
# ---------------------------------------------------------------------------

def test_usd_resolves_a_variable_in_an_asset_path(tmp_path) -> None:
    """The place this feature is actually for. Composition does the substituting, so
    switching the variable recomposes the stage with nothing re-running."""
    for name, radius in (("a", 1.0), ("b", 5.0)):
        asset = Usd.Stage.CreateNew(str(tmp_path / f"asset_{name}.usda"))
        sphere = UsdGeom.Sphere.Define(asset, "/Root")
        sphere.GetRadiusAttr().Set(radius)
        asset.SetDefaultPrim(asset.GetPrimAtPath("/Root"))
        asset.GetRootLayer().Save()

    stage = Usd.Stage.CreateNew(str(tmp_path / "main.usda"))
    stage.GetRootLayer().expressionVariables = {"WHICH": "a"}
    prim = stage.DefinePrim("/World/thing")
    prim.GetReferences().AddReference('`"./asset_${WHICH}.usda"`')
    assert UsdGeom.Sphere(stage.GetPrimAtPath("/World/thing")).GetRadiusAttr().Get() == 1.0

    stage.GetRootLayer().expressionVariables = {"WHICH": "b"}

    assert UsdGeom.Sphere(stage.GetPrimAtPath("/World/thing")).GetRadiusAttr().Get() == 5.0


def test_a_param_keeps_the_text_literally(scene) -> None:
    """A prim path is not one of the fields USD substitutes into, so usd_chunks must not
    substitute either -- making ${SHOT} work in places OpenUSD says it does not is how a
    scene stops being readable by anything but us."""
    stage = scene.get_stage()
    chunk_utils.set_stage_expression_variables(stage, {"SHOT": "sh010"})
    c = _mk("dst")
    p = chunk.LineEdit("primpath", default_value="")
    c.add_param(p)
    scene.add_chunk(c)

    p.value = "/World/${SHOT}/geo"

    assert p.value == "/World/${SHOT}/geo"
    assert p.raw_value == "/World/${SHOT}/geo"


def test_an_asset_path_param_round_trips_its_expression(scene) -> None:
    """A FileBrowser hands the text to USD untouched, so the attr holds the expression --
    which is exactly what makes composition able to evaluate it."""
    stage = scene.get_stage()
    chunk_utils.set_stage_expression_variables(stage, {"ASSET": "hero"})
    c = _mk("ref")
    p = chunk.FileBrowser("file_path", default_value="")
    c.add_param(p)
    scene.add_chunk(c)

    p.value = '`"./${ASSET}.usda"`'

    assert p.value == '`"./${ASSET}.usda"`'
    assert stage.GetAttributeAtPath(p._param_attr_path).Get() == '`"./${ASSET}.usda"`'


def test_a_variable_on_an_ordinary_param_is_reported(scene, caplog) -> None:
    """It stays exactly as typed -- but silently reading back as the literal text is worse
    than saying, once, where expression variables do resolve."""
    c = _mk("dst")
    p = chunk.LineEdit("primpath", default_value="")
    c.add_param(p)
    scene.add_chunk(c)

    p.value = "/World/${SHOT}/geo"

    assert any("asset paths" in (r.getMessage() or "") for r in caplog.records), (
        "the warning should name where they do resolve"
    )


def test_a_numeric_param_refuses_a_variable(scene) -> None:
    """Nothing resolves it any more, so a Slider holding "${COUNT}" would just be a string
    where a number belongs. Refusing is the honest answer."""
    c = _mk("dst")
    p = chunk.Slider("count", default_value=0, is_int=True)
    c.add_param(p)
    scene.add_chunk(c)

    with pytest.raises(ValueError):
        p.value = "${COUNT}"


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

def _stage_expression(scene, *rows):
    """Build the chunk with one row per (name, type, value) triple."""
    mod = importlib.import_module("scripts.utils.stage_expression")

    node = mod.main()
    node._config = ChunkConfig(module="stage_expression")
    scene.add_chunk(node)
    group = node.get_param(mod.ROWS_PARAM)
    group.size = len(rows)
    for i, (name, type_name, value) in enumerate(rows):
        group.get_indexed_param(mod.NAME_COLUMN, i).value = name
        group.get_indexed_param(mod.TYPE_COLUMN, i).value = type_name
        group.get_indexed_param(mod.VALUE_COLUMN, i).value = value
    mod.execute(node)
    return node, mod


def test_chunk_declares_its_variables(scene) -> None:
    _stage_expression(scene, ("SHOT", "string", "sh010"), ("ASSET", "string", "hero"))

    assert chunk_utils.stage_expression_variables(scene.get_stage()) == {
        "SHOT": "sh010", "ASSET": "hero",
    }


def test_every_type_usd_expressions_have(scene) -> None:
    """Strings, whole numbers, booleans and lists -- and deliberately no float, which USD
    expression variables do not have."""
    _stage_expression(
        scene,
        ("SHOT", "string", "sh010"),
        ("IDENTIFIER", "int", "3254"),
        ("UseProxyModel", "bool", "false"),
        ("renderPassList", "string[]", "foreground, background, FX"),
    )

    variables = chunk_utils.stage_expression_variables(scene.get_stage())
    assert variables["SHOT"] == "sh010"
    assert variables["IDENTIFIER"] == 3254
    assert variables["UseProxyModel"] is False
    assert list(variables["renderPassList"]) == ["foreground", "background", "FX"]


def test_a_typed_variable_evaluates_in_a_usd_expression(scene) -> None:
    """The point of the types: USD's own evaluator has to accept what we authored."""
    _stage_expression(scene, ("USE_LOW_POLY", "bool", "true"))
    variables = chunk_utils.stage_expression_variables(scene.get_stage())

    result = Sdf.VariableExpression(
        '`if(${USE_LOW_POLY}, "assetsLow/hero.usda", "assets/hero.usda")`'
    ).Evaluate(dict(variables))

    assert result.value == "assetsLow/hero.usda", result.errors


def test_editing_a_row_changes_the_variable(scene) -> None:
    node, mod = _stage_expression(scene, ("SHOT", "string", "sh010"))

    node.get_param(mod.ROWS_PARAM).get_indexed_param(mod.VALUE_COLUMN, 0).value = "sh020"
    mod.execute(node)

    assert chunk_utils.stage_expression_variables(scene.get_stage())["SHOT"] == "sh020"


def test_removing_a_row_removes_the_variable(scene) -> None:
    node, mod = _stage_expression(scene, ("SHOT", "string", "sh010"), ("ASSET", "string", "hero"))

    node.get_param(mod.ROWS_PARAM).size = 1
    mod.execute(node)

    assert chunk_utils.stage_expression_variables(scene.get_stage()) == {"SHOT": "sh010"}


def test_two_chunks_do_not_erase_each_other(scene) -> None:
    """Each drops only the names it recorded owning, so a second chunk's variables survive
    a re-execute of the first."""
    a, mod = _stage_expression(scene, ("SHOT", "string", "sh010"))
    _stage_expression(scene, ("ASSET", "string", "hero"))

    mod.execute(a)

    assert chunk_utils.stage_expression_variables(scene.get_stage()) == {
        "SHOT": "sh010", "ASSET": "hero",
    }


def test_studio_variables_survive(scene) -> None:
    stage = scene.get_stage()
    stage.GetRootLayer().expressionVariables = {"SHOW": "hydra"}

    _stage_expression(scene, ("SHOT", "string", "sh010"))

    assert stage.GetRootLayer().expressionVariables == {"SHOW": "hydra"}, "their file is untouched"
    assert chunk_utils.stage_expression_variables(stage) == {"SHOW": "hydra", "SHOT": "sh010"}


def test_a_malformed_row_is_skipped(scene) -> None:
    _stage_expression(
        scene,
        ("1BAD", "string", "y"),
        ("NOT_A_NUMBER", "int", "twelve"),
        ("GOOD", "string", "z"),
    )

    assert chunk_utils.stage_expression_variables(scene.get_stage()) == {"GOOD": "z"}
