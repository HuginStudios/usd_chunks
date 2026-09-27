"""Regression: reported live -- a node loaded from a saved file could get permanently stuck
at header height, its embedded ChunkWidget never showing, with no exception logged and no
visual response to toggling collapse.

Root cause: _create_proxies_batch creates a new node's embedded proxy widget in batches of
_PROXY_BATCH_SIZE across deferred event-loop ticks. If a second rebuild fires (_build_gen
increments) before an earlier batch's remaining queue gets its turn, that queue is abandoned
outright -- no reschedule (see the `gen != self._build_gen` check). _rebuild_nodes's "existing
node" branch then never revisits it either: same chunk, same path, so it's left exactly as-is
-- forever without a proxy. A node without one defaults to header height (see ChunkNodeItem
__init__) and _set_collapsed's own show/hide logic no-ops when self._proxy is None, so nothing
about toggling collapse ever has a visible effect on it again.

A single file load can legitimately fire more than one rebuild (each chunk fires its own
CHUNK_LOADED during bulk restore), so this isn't just a theoretical race.
"""

from __future__ import annotations

import time

import pytest
from pxr import Usd

from usd_chunks.chunk_stage import get_chunk_prim_path, initialize as _chunk_usd_init
from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.frontends.qt.factory import ChunkWidgetDocker
from usd_chunks.frontends.qt import nodegraph


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _mk(mod, scene):
    """notify=False, matching _add_prim_mods' own bulk-restore calls -- so adding several
    chunks doesn't each trigger its own immediate rebuild (which would never exceed
    _PROXY_BATCH_SIZE on its own and so could never reproduce the race this file is about)."""
    cs = mod.create() if mod.loaded else mod.load()
    for c in cs:
        scene.add_chunk(c, notify=False)
    return cs[0]


@pytest.fixture
def graph(qapp):
    stage = Usd.Stage.CreateInMemory()

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    lm = chunk_registry.ChunkLayerManager()
    lm.initialize(stage)
    _chunk_usd_init(stage, scene)

    docker = ChunkWidgetDocker()
    docker.resize(500, 600)
    docker.show()
    qapp.processEvents()

    return qapp, reg, scene, docker


def test_a_rebuild_mid_batch_does_not_permanently_orphan_a_proxy(graph) -> None:
    qapp, reg, scene, docker = graph
    mod = reg.get_chunk_module("transform")

    # More than _PROXY_BATCH_SIZE so some nodes land in the deferred "rest" queue.
    n = nodegraph.ChunkGraphScene._PROXY_BATCH_SIZE + 2
    chunks = [_mk(mod, scene) for _ in range(n)]

    gv = docker.tab_widget.get_current_graph_view()
    gscene = gv.scene()

    # First rebuild: creates all n nodes, batches proxy creation (_PROXY_BATCH_SIZE
    # synchronously, the rest deferred via a zero-delay timer). No processEvents() yet --
    # that deferred "rest" timer must not get a chance to fire before the race below.
    gscene._rebuild_nodes()

    # A second rebuild, fired before the first batch's deferred "rest" queue gets a turn --
    # still no processEvents() in between, matching a reactive rebuild landing mid-tick.
    gscene._rebuild_nodes()

    # Now let every pending deferred proxy-creation timer actually run.
    end = time.monotonic() + 1.0
    while time.monotonic() < end:
        qapp.processEvents()

    orphaned = [
        c for c in chunks
        if (node := gscene._nodes.get(get_chunk_prim_path(c))) is not None and node._proxy is None
    ]
    assert not orphaned, (
        f"{len(orphaned)} of {n} nodes never got their embedded widget after a rebuild "
        "superseded their queued proxy creation -- permanently stuck at header height"
    )
