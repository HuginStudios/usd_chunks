"""Arithmetic in expressions: "@a.x@ * @b.y@" on a numeric param computes, and the
operators survive a save/reload.

Two halves used to be missing. The value read back was the substituted *string*
("20.0 * 3.0"), and a non-String attr stored only its connections -- so reload rebuilt the
expression by joining the connection targets and dropped everything between them, turning
"@a.x@ * @b.y@" into "@a.x@@b.y@".
"""
from __future__ import annotations

import pytest
from pxr import Sdf, Usd

from usd_chunks import chunk, chunk_registry, chunk_utils
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import (
    _INPUTS_PREFIX,
    _load_wire_inputs,
    _patch_expressions,
    ChunkStageSync,
    get_chunk_prim_path,
)

from chunk_stubs import attach_stub_module


def _chunk_path(name: str) -> str:
    """Where a chunk added through the scene actually lands.

    add_chunk routes a context-less chunk into the scene's default chunkgroup, so its
    parent is that group rather than the /chunks scope.
    """

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


def _mk(name: str) -> chunk.Chunk:
    c = chunk.Chunk(name)
    attach_stub_module(c)
    return c


@pytest.fixture()
def scene():
    stage = Usd.Stage.CreateInMemory()
    sc = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(sc, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    ChunkStageSync().initialize(stage, sc)
    yield sc


def _two_sources(scene, a_val=20.0, b_val=3.0):
    a, b = _mk("chunkA"), _mk("chunkB")
    a.add_param(chunk.Slider("a", default_value=type(a_val)()))
    b.add_param(chunk.Slider("a", default_value=type(b_val)()))
    scene.add_chunk(a)
    scene.add_chunk(b)
    a.get_param("a").value = a_val
    b.get_param("a").value = b_val
    return a, b


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------

def test_numeric_param_computes_product(scene) -> None:
    """The whole point: a Slider wired to two chunks multiplies them."""
    _two_sources(scene)
    dst = _mk("dst")
    p = chunk.Slider("multi", default_value=0.0)
    dst.add_param(p)
    scene.add_chunk(dst)

    p.value = "@chunkA.a@ * @chunkB.a@"

    assert p.value == pytest.approx(60.0)
    assert isinstance(p.value, float)
    assert p.raw_value == "@chunkA.a@ * @chunkB.a@", "the authored text is what's stored"


def test_result_follows_its_inputs(scene) -> None:
    """Changing a referenced param recomputes the sum, like any other expression ref."""
    a, _ = _two_sources(scene)
    dst = _mk("dst")
    p = chunk.Slider("multi", default_value=0.0)
    dst.add_param(p)
    scene.add_chunk(dst)
    p.value = "@chunkA.a@ * @chunkB.a@"
    assert p.value == pytest.approx(60.0)

    a.get_param("a").value = 5.0

    assert p.value == pytest.approx(15.0)


def test_int_param_computes_int(scene) -> None:
    _two_sources(scene, 7, 2)
    dst = _mk("dst")
    p = chunk.Slider("multi", default_value=0, is_int=True)
    dst.add_param(p)
    scene.add_chunk(dst)

    p.value = "@chunkA.a@ + @chunkB.a@"

    assert p.value == 9
    assert isinstance(p.value, int)


def test_single_ref_still_returns_the_typed_value(scene) -> None:
    """The single-token path predates this and must keep returning the ref's own value."""
    _two_sources(scene)
    dst = _mk("dst")
    p = chunk.Slider("one", default_value=0.0)
    dst.add_param(p)
    scene.add_chunk(dst)

    p.value = "@chunkA.a@"

    assert p.value == pytest.approx(20.0)


def test_string_param_still_interpolates(scene) -> None:
    """Path building is the reason multi-ref interpolation exists -- don't compute it."""
    a = _mk("chunkA")
    a.add_param(chunk.LineEdit("primpath_location"))
    scene.add_chunk(a)
    a.get_param("primpath_location").value = "/world/geo"

    dst = _mk("dst")
    p = chunk.LineEdit("path")
    dst.add_param(p)
    scene.add_chunk(dst)

    p.value = "@chunkA.primpath_location@/cube"

    assert p.value == "/world/geo/cube"


def test_unevaluable_text_on_numeric_param_falls_back(scene) -> None:
    """Not arithmetic once substituted -- keep the string rather than raising."""
    a = _mk("chunkA")
    a.add_param(chunk.LineEdit("name"))
    scene.add_chunk(a)
    a.get_param("name").value = "cube"

    dst = _mk("dst")
    p = chunk.Slider("multi", default_value=0.0)
    dst.add_param(p)
    scene.add_chunk(dst)

    p.value = "@chunkA.name@ @chunkA.name@"

    assert p.value == "cube cube"


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------

def test_operators_survive_reload(scene) -> None:
    """The regression this fixes: reload used to rebuild "@chunkA.a@@chunkB.a@"."""
    _two_sources(scene)
    dst = _mk("dst")
    p = chunk.Slider("multi", default_value=0.0)
    dst.add_param(p)
    scene.add_chunk(dst)
    p.value = "@chunkA.a@ * @chunkB.a@"

    stage = scene.get_stage()
    attr = stage.GetPrimAtPath(_chunk_path("dst")).GetAttribute(f"{_INPUTS_PREFIX}multi")
    assert attr.GetTypeName() == Sdf.ValueTypeNames.Float
    assert len(attr.GetConnections()) == 2, "both refs still author real connections"

    reloaded = _mk("dst")
    reloaded_param = chunk.Slider("multi", default_value=0.0)
    reloaded.add_param(reloaded_param)
    _load_wire_inputs(stage.GetPrimAtPath(_chunk_path("dst")), reloaded)

    assert reloaded_param.raw_value == "@chunkA.a@ * @chunkB.a@"


def test_rename_patches_stored_text(scene) -> None:
    """A renamed chunk must be followed into customData, not just String attr values."""
    _two_sources(scene)
    dst = _mk("dst")
    p = chunk.Slider("multi", default_value=0.0)
    dst.add_param(p)
    scene.add_chunk(dst)
    p.value = "@chunkA.a@ * @chunkB.a@"

    stage = scene.get_stage()
    _patch_expressions(stage, "@chunkA.", "@renamedA.")

    attr = stage.GetPrimAtPath(_chunk_path("dst")).GetAttribute(f"{_INPUTS_PREFIX}multi")
    assert chunk_utils.read_expression_text(attr) == "@renamedA.a@ * @chunkB.a@"


def test_legacy_connection_only_attr_still_loads(scene) -> None:
    """Files authored before the text was stored have connections and nothing else --
    they must still come back as a (single-ref) expression, not as a bare number."""
    _two_sources(scene)
    dst = _mk("dst")
    p = chunk.Slider("one", default_value=0.0)
    dst.add_param(p)
    scene.add_chunk(dst)
    p.value = "@chunkA.a@"

    stage = scene.get_stage()
    attr = stage.GetPrimAtPath(_chunk_path("dst")).GetAttribute(f"{_INPUTS_PREFIX}one")
    attr.ClearCustomDataByKey(chunk_utils.EXPR_TEXT_KEY)  # pretend it was authored before

    reloaded = _mk("dst")
    reloaded_param = chunk.Slider("one", default_value=0.0)
    reloaded.add_param(reloaded_param)
    _load_wire_inputs(stage.GetPrimAtPath(_chunk_path("dst")), reloaded)

    assert reloaded_param.raw_value == "@chunkA.a@"


# ---------------------------------------------------------------------------
# the evaluator itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("20.0 * 3.0", 60.0),
    ("1 + 2 * 3", 7),
    ("(1 + 2) * 3", 9),
    ("-4 / 2", -2.0),
    ("2 ** 8", 256),
    ("7 % 4", 3),
])
def test_eval_arithmetic_computes(text, expected) -> None:
    assert chunk_utils.eval_arithmetic(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", [
    "__import__('os').system('echo pwned')",
    "open('/etc/passwd').read()",
    "print(1)",
    "[1, 2, 3]",
    "x + 1",
    "'a' + 'b'",
    "/world/geo/cube",
    "cube_01",
    "True and False",
    "2 ** 10000000",
    "1 / 0",
])
def test_eval_arithmetic_refuses(text) -> None:
    assert chunk_utils.eval_arithmetic(text) is None
