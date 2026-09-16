"""A chunk-in-chunk must not be navigable; a chunkgroup must be.

A chunkgroup is defined in USD and is a subnet you dive into. A chunk is defined in python,
and one that owns children -- AMDMaterialXLibrary creating `reference` and `bind_material` --
is reusing them as an implementation detail. You are not meant to dive in and edit it.

Both carry GroupFlags.CONTEXT, and the node item keyed its dive (plus the group header and
the `▶` marker advertising it) on that flag alone, so the AMD node offered a dive into its
own internals. Navigation now keys on being a chunkgroup; containment still keys on
chunk_usd.is_group_chunk, so a chunk-in-chunk keeps hiding its children and running their
code.
"""
from __future__ import annotations

import pytest
from pxr import Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.plugins.chunk_qt import ChunkWidgetDocker
from usd_chunks.plugins import chunk_usd
from usd_chunks.plugins.chunk_qt_nodegraph import ChunkGraphView
from usd_chunks.plugins.chunk_usd import get_chunk_prim_path, initialize as _chunk_usd_init


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


class _FakeDoubleClickEvent:
    """mouseDoubleClickEvent only ever calls .pos() and .accept()."""

    def __init__(self, y: float):
        self._y = y
        self.accepted = False

    def pos(self):
        class _P:
            def __init__(self, y):
                self._y = y

            def y(self):
                return self._y

        return _P(self._y)

    def accept(self):
        self.accepted = True


def _mk(mod, scene, context=False):
    """Create a chunk and add it to the scene.

    `context` sets GroupFlags.CONTEXT *before* the chunk reaches the scene, so the node
    item is constructed with it already on -- ChunkNodeItem reads the flag once, in
    __init__, and setting it afterwards would leave the node deciding from stale state and
    quietly stop the test exercising the production path at all.
    """
    cs = mod.create() if mod.loaded else mod.load()
    if context:
        for c in cs:
            c.set_flag(ChunkFlags.GroupFlags.CONTEXT)
    for c in cs:
        scene.add_chunk(c)
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
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)

    docker = ChunkWidgetDocker()
    docker.resize(500, 600)
    docker.show()
    qapp.processEvents()

    return qapp, reg, scene, docker


def _node_for(chunk, docker, qapp):
    """The node item for `chunk`, wherever it lives.

    Not via get_current_graph_view(): creating a CONTEXT chunk opens a tab for it and
    switches to that tab, so the "current" view becomes the new chunk's own empty inner
    graph, while its node sits in the graph it was created in.
    """
    qapp.processEvents()
    path = get_chunk_prim_path(chunk)
    for view in docker.findChildren(ChunkGraphView):
        node = view.scene()._nodes.get(path)
        if node is not None:
            return node
    raise AssertionError(f"no node for {path} in any graph view")


# The predicate


def test_the_chunkgroup_type_is_a_chunkgroup(graph):
    _qapp, reg, scene, _docker = graph
    group = _mk(reg.get_chunk_module("chunkgroup"), scene)

    assert chunk_usd.is_chunkgroup(group)
    assert chunk_usd.is_group_chunk(group), "a chunkgroup owns children too"


def test_a_chunk_in_chunk_owns_children_but_is_not_a_chunkgroup(graph):
    """The distinction CONTEXT alone cannot make.

    Built by forcing CONTEXT onto an ordinary chunk rather than by instantiating
    AMDMaterialXLibrary, whose construction fetches a remote material catalog.
    """
    _qapp, reg, scene, _docker = graph
    inner = _mk(reg.get_chunk_module("transform"), scene, context=True)

    assert inner.has_flag(ChunkFlags.GroupFlags.CONTEXT)
    assert chunk_usd.is_group_chunk(inner), (
        "containment must still hold -- children stay hidden and keep cooking"
    )
    assert not chunk_usd.is_chunkgroup(inner), "but it is not a subnet to navigate into"


def test_the_amd_chunk_module_is_not_the_chunkgroup_type(graph):
    """Checked through the registry's config, which the lazy loader populates without
    executing the module body -- so this costs no network and no Qt."""
    _qapp, reg, _scene, _docker = graph
    amd = reg.get_chunk_module("AMDMaterialXLibrary")
    if amd is None:
        pytest.skip("AMDMaterialXLibrary not registered in this environment")

    assert amd.config.module != "chunkgroup"


# The node


def test_the_node_takes_its_dive_gate_from_is_chunkgroup(graph, monkeypatch):
    """ChunkNodeItem must derive the gate from the predicate, not from CONTEXT.

    Driven by patching the predicate rather than by building a real chunk-in-chunk node:
    a top-level CONTEXT chunk is rendered as a tab, not a node, so there is no node to
    inspect. Patching also makes the assertion sharp -- an ordinary Transform carries no
    CONTEXT flag, so the old `has_flag(CONTEXT)` gate reports False no matter what the
    predicate says, and this fails.
    """
    qapp, reg, scene, docker = graph
    monkeypatch.setattr(chunk_usd, "is_chunkgroup", lambda c: True)

    chunk = _mk(reg.get_chunk_module("transform"), scene)
    node = _node_for(chunk, docker, qapp)

    assert node._is_chunkgroup is True, (
        "the node reads chunk_usd.is_chunkgroup; it must not be keyed on the CONTEXT flag"
    )


def test_an_ordinary_chunk_is_not_a_navigable_group(graph):
    qapp, reg, scene, docker = graph
    chunk = _mk(reg.get_chunk_module("transform"), scene)
    node = _node_for(chunk, docker, qapp)

    assert node._is_chunkgroup is False
