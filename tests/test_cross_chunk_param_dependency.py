"""Param-level dependencies that cross a chunk boundary.

A DagNode edge between two params is in-memory only — ChunkStageSync._on_dependency_added
authors inputs:chunk for ChunkBase pairs and returns early for anything else — so an edge
between params of two different chunks is never written to USD and is gone after a reload.
Inside one chunk that edge is the right mechanism (it is what set_function reads, and it can
drive attributes no expression can reach); across chunks an expression is, so add_dependency
warns and connect_value redirects.
"""
from __future__ import annotations

import logging

import pytest
from pxr import Usd

from usd_chunks import chunk, chunk_core, chunk_registry
from usd_chunks.chunk_core import ChunkConfig, SceneSender
from usd_chunks.plugins.chunk_usd import _CHUNKS_ROOT, _INPUTS_PREFIX, ChunkStageSync


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


_LOGGER = chunk_core.logger.name


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


def _two_chunks(scene):
    src, dst = _mk("src"), _mk("dst")
    src.add_param(chunk.Slider("p", default_value=0.0))
    dst.add_param(chunk.Slider("q", default_value=0.0))
    scene.add_chunk(src)
    scene.add_chunk(dst)
    src.get_param("p").value = 4.0
    return src, dst


# ---------------------------------------------------------------------------
# add_dependency: warn, but still do it
# ---------------------------------------------------------------------------

def test_cross_chunk_dependency_warns(scene, caplog) -> None:
    src, dst = _two_chunks(scene)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        dst.get_param("q").add_dependency(src.get_param("p"))

    assert "crosses chunks" in caplog.text
    assert "@src.p@" in caplog.text, "the warning must name the expression that would work"


def test_cross_chunk_dependency_still_connects(scene, caplog) -> None:
    """A warning, not a refusal — existing in-memory behaviour is unchanged."""
    src, dst = _two_chunks(scene)
    q = dst.get_param("q")

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        idx = q.add_dependency(src.get_param("p"))

    assert idx >= 0
    assert any(dep is src.get_param("p") for _, dep in q.get_dependencies())


def test_same_chunk_dependency_is_silent(scene, caplog) -> None:
    """The in-chunk case is the supported one — it must not warn."""
    c = _mk("solo")
    c.add_param(chunk.Slider("a", default_value=0.0))
    c.add_param(chunk.Slider("b", default_value=0.0))
    scene.add_chunk(c)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        c.get_param("b").add_dependency(c.get_param("a"))

    assert "crosses chunks" not in caplog.text


def test_unowned_param_is_silent(caplog) -> None:
    """A param not yet added to anything has no chunk to compare — never warn on that."""
    a = chunk.Slider("a", default_value=0.0)
    b = chunk.Slider("b", default_value=0.0)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        b.add_dependency(a)

    assert "crosses chunks" not in caplog.text


# ---------------------------------------------------------------------------
# connect_value: redirect to the mechanism that persists
# ---------------------------------------------------------------------------

def test_connect_value_across_chunks_authors_expression(scene) -> None:
    src, dst = _two_chunks(scene)

    dst.get_param("q").connect_value(src.get_param("p"))

    assert dst.get_param("q").raw_value == "@src.p@"
    assert dst.get_param("q").value == pytest.approx(4.0)


def test_connect_value_across_chunks_reaches_usd(scene) -> None:
    """The whole point of redirecting: the wire exists in USD, so it survives a reload."""
    src, dst = _two_chunks(scene)

    dst.get_param("q").connect_value(src.get_param("p"))

    attr = scene.get_stage().GetPrimAtPath(_chunk_path("dst")).GetAttribute(f"{_INPUTS_PREFIX}q")
    assert attr.IsValid()
    assert [str(c) for c in attr.GetConnections()] == [f'{_chunk_path("src")}.{_INPUTS_PREFIX}p']


def test_connect_value_across_chunks_tracks_source(scene) -> None:
    src, dst = _two_chunks(scene)
    dst.get_param("q").connect_value(src.get_param("p"))

    src.get_param("p").value = 9.0

    assert dst.get_param("q").value == pytest.approx(9.0)


def test_connect_value_within_a_chunk_is_unchanged(scene) -> None:
    """Same chunk keeps the DagNode path — no expression, no USD connection."""
    c = _mk("solo")
    c.add_param(chunk.Slider("a", default_value=0.0))
    c.add_param(chunk.Slider("b", default_value=0.0))
    scene.add_chunk(c)

    c.get_param("b").connect_value(c.get_param("a"))
    c.get_param("a").value = 3.0

    assert c.get_param("b").value == pytest.approx(3.0)
    assert c.get_param("b").raw_value == pytest.approx(3.0), "resolved value, not an expression"


def test_connect_value_keeps_its_type_check(scene) -> None:
    """Redirecting must not skip the check the raw expression path doesn't have."""
    src, dst = _two_chunks(scene)
    dst.add_param(chunk.LineEdit("text"))

    with pytest.raises(ValueError):
        dst.get_param("text").connect_value(src.get_param("p"))


def test_nested_param_is_still_nameable(scene) -> None:
    """A param inside a group is reachable as @src.inner@ — get_param finds it by name,
    which is the same lookup expression resolution uses — so it redirects like any other."""
    src = _mk("src")
    grp = chunk.Group("grp")
    src.add_param(grp)
    grp.add_param(chunk.Slider("inner", default_value=0.0))
    dst = _mk("dst")
    dst.add_param(chunk.Slider("q", default_value=0.0))
    scene.add_chunk(src)
    scene.add_chunk(dst)
    src.get_param("grp.inner").value = 2.0

    dst.get_param("q").connect_value(src.get_param("grp.inner"))

    assert dst.get_param("q").raw_value == "@src.inner@"
    assert dst.get_param("q").value == pytest.approx(2.0)


def test_unnameable_param_falls_back_to_the_dependency(scene, caplog) -> None:
    """When no @Node.param@ can address the source — its bare name resolves to a different
    param, so the reference would silently point at the wrong one — connect_value keeps the
    in-memory edge and lets the warning stand rather than authoring a wrong expression."""
    src, dst = _two_chunks(scene)
    q = dst.get_param("q")
    q._expression_ref_to = lambda *_: None  # the name doesn't round-trip

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        q.connect_value(src.get_param("p"))

    assert "crosses chunks" in caplog.text
    src.get_param("p").value = 5.0
    assert q.value == pytest.approx(5.0), "in-memory edge still works"
    assert not isinstance(q.raw_value, str), "no expression authored"


def test_expression_ref_names_a_reachable_param(scene) -> None:
    """_expression_ref_to only promises a reference the resolver can actually follow."""
    src, dst = _two_chunks(scene)
    q = dst.get_param("q")

    assert q._expression_ref_to(src.get_param("p"), src) == "@src.p@"
    assert q._expression_ref_to(chunk.Slider("stranger", default_value=0.0), src) is None
