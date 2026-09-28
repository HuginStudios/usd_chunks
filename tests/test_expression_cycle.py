"""Expression cycles terminate instead of exhausting the stack.

Two params referring to each other -- a.p = "@b.p@" while b.p = "@a.p@" -- used to recurse
forever down two separate paths: the notice path (each ref installs a NoticeConnection back
to _on_ref_changed) and the read path (resolving an expression reads the ref's .value, which
resolves its own expression). Both are guarded now.

The DagNode side raises CircularDependencyError from topo_order; the expression side
deliberately does not. These fire from notice dispatch and from ordinary .value reads --
including widget paint -- where a settled value plus a logged warning beats an exception.
"""
from __future__ import annotations

import logging

import pytest
from pxr import Usd

from usd_chunks import chunk, chunk_core, chunk_registry
from usd_chunks.chunk_core import ChunkConfig
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import ChunkStageSync

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


def _pair(scene, factory=lambda: chunk.Slider("p", default_value=0.0)):
    a, b = _mk("a"), _mk("b")
    a.add_param(factory())
    b.add_param(factory())
    scene.add_chunk(a)
    scene.add_chunk(b)
    return a, b


def test_two_param_cycle_terminates(scene) -> None:
    """The reported hang: this used to never return."""
    a, b = _pair(scene)

    a.get_param("p").value = "@b.p@"
    b.get_param("p").value = "@a.p@"

    assert a.get_param("p").value is not None
    assert b.get_param("p").value is not None


def test_cycle_warns(scene, caplog) -> None:
    a, b = _pair(scene)
    a.get_param("p").value = "@b.p@"

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        b.get_param("p").value = "@a.p@"

    assert "Expression cycle" in caplog.text


def test_cycle_reads_fall_back_to_default(scene) -> None:
    a, b = _pair(scene, lambda: chunk.Slider("p", default_value=7.0))
    a.get_param("p").value = "@b.p@"
    b.get_param("p").value = "@a.p@"

    assert a.get_param("p").value == pytest.approx(7.0)


def test_self_reference_terminates(scene) -> None:
    """The one-hop case: a param referring to itself."""
    a = _mk("a")
    a.add_param(chunk.Slider("p", default_value=3.0))
    scene.add_chunk(a)

    a.get_param("p").value = "@a.p@"

    assert a.get_param("p").value == pytest.approx(3.0)


def test_three_param_cycle_terminates(scene) -> None:
    """Longer rings are the same problem -- the guard is per param, not per pair."""
    a, b = _pair(scene)
    c = _mk("c")
    c.add_param(chunk.Slider("p", default_value=0.0))
    scene.add_chunk(c)

    a.get_param("p").value = "@b.p@"
    b.get_param("p").value = "@c.p@"
    c.get_param("p").value = "@a.p@"

    assert a.get_param("p").value is not None


def test_acyclic_chain_still_resolves(scene) -> None:
    """The guard must not block a legitimate chain: the flag is per param, and each hop
    is a different object, so a -> b -> c resolves all the way through."""
    a, b = _pair(scene)
    c = _mk("c")
    c.add_param(chunk.Slider("p", default_value=0.0))
    scene.add_chunk(c)

    a.get_param("p").value = 5.0
    b.get_param("p").value = "@a.p@"
    c.get_param("p").value = "@b.p@"

    assert c.get_param("p").value == pytest.approx(5.0)

    a.get_param("p").value = 8.0

    assert c.get_param("p").value == pytest.approx(8.0), "updates still propagate down the chain"


def test_repeated_reads_are_not_poisoned(scene) -> None:
    """The flags must be cleared in a finally -- a cycle read must not leave the param
    permanently stuck returning its default."""
    a, b = _pair(scene)
    a.get_param("p").value = "@b.p@"
    b.get_param("p").value = "@a.p@"

    a.get_param("p").value  # trip the guard
    b.get_param("p").value = 4.0  # break the cycle

    assert a.get_param("p").value == pytest.approx(4.0), "resolution works again after the cycle"


def test_vector_cycle_terminates(scene) -> None:
    """Whole-vector references cycle the same way and get the same guard."""
    a, b = _mk("a"), _mk("b")
    a.add_param(chunk.Vector("v", param=chunk.Slider(), length=3, suffixes="xyz"))
    b.add_param(chunk.Vector("v", param=chunk.Slider(), length=3, suffixes="xyz"))
    scene.add_chunk(a)
    scene.add_chunk(b)

    a.get_param("v").value = "@b.v@"
    b.get_param("v").value = "@a.v@"

    assert len(a.get_param("v").value) == 3
    assert len(b.get_param("v").value) == 3
