"""The scene's default context is a real chunkgroup under /chunks, not /chunks itself.

/chunks is a scope, like /lights or /render -- nothing lives at it. Under it sits a
default, properly typed chunkgroup (/chunks/ChunkGroup), and chunkgroups nest inside
chunkgroups from there. So context == chunkgroup: the CONTEXT-without-a-module context was
only ever a stand-in from before the chunkgroup type existed.

Before this, the default context was a bare ChunkBase claiming /chunks as its own prim
path, which made the scene's ground prim a tab rather than a chunkgroup, and left two
different kinds of container doing the same job.
"""
from __future__ import annotations

import pytest
from pxr import Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.plugins import chunk_usd
from usd_chunks.plugins.chunk_usd import get_chunk_prim_path, initialize as _chunk_usd_init

CHUNKS_SCOPE = "/chunks"


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


@pytest.fixture
def scene(qapp):
    stage = Usd.Stage.CreateInMemory()

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    s = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(s, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, s)
    return qapp, reg, s


def _contexts(scene):
    from usd_chunks.chunk_core import ChunkBase
    return [
        c for c in scene.chunks.get_my_params()
        if isinstance(c, ChunkBase) and c.has_flag(ChunkFlags.GroupFlags.CONTEXT)
    ]


def _add_plain_chunk(reg, scene, key="transform"):
    mod = reg.get_chunk_module(key)
    cs = mod.create() if mod.loaded else mod.load()
    for c in cs:
        scene.add_chunk(c)
    return cs[0]


def test_the_default_context_is_a_chunkgroup(scene):
    qapp, reg, s = scene
    _add_plain_chunk(reg, s)
    qapp.processEvents()

    ctxs = _contexts(s)
    assert len(ctxs) == 1, f"expected one default context, got {[c.name for c in ctxs]}"
    assert chunk_usd.is_chunkgroup(ctxs[0]), (
        "context == chunkgroup; the default must be the real chunkgroup type, not a bare "
        "CONTEXT chunk standing in for one"
    )


def test_nothing_lives_at_the_chunks_scope(scene):
    qapp, reg, s = scene
    _add_plain_chunk(reg, s)
    qapp.processEvents()

    for c in _contexts(s):
        assert getattr(c, "prim_path", None) != CHUNKS_SCOPE, (
            "/chunks is a scope like /lights or /render -- a context must sit under it, "
            "not claim it"
        )


def test_the_default_chunkgroup_sits_under_the_scope(scene):
    qapp, reg, s = scene
    _add_plain_chunk(reg, s)
    qapp.processEvents()

    ctx = _contexts(s)[0]
    path = get_chunk_prim_path(ctx)
    assert path.startswith(CHUNKS_SCOPE + "/"), f"expected /chunks/<name>, got {path}"
    assert path.count("/") == 2, f"expected exactly one level under the scope, got {path}"


def test_a_chunk_with_no_tab_lands_inside_the_default_chunkgroup(scene):
    qapp, reg, s = scene
    c = _add_plain_chunk(reg, s)
    qapp.processEvents()

    ctx = _contexts(s)[0]
    assert get_chunk_prim_path(c).startswith(get_chunk_prim_path(ctx) + "/")


def test_the_default_chunkgroup_is_not_duplicated(scene):
    qapp, reg, s = scene
    _add_plain_chunk(reg, s, "transform")
    _add_plain_chunk(reg, s, "prim")
    qapp.processEvents()

    assert len(_contexts(s)) == 1, "both chunks must land in the same default chunkgroup"


# Chunkgroups stay visible


def test_a_user_created_chunkgroup_is_still_a_chunk_in_the_graph(scene):
    """Only the OUTERMOST container is not a node; every other chunkgroup is one.

    Live-reported: no chunkgroup could be seen or created. Two causes, both from making
    the default context a chunkgroup. get_chunks() excluded contexts by container depth,
    but add_chunk puts every context straight into scene.chunks -- so a user-created
    chunkgroup sits at the same depth as the root and the test hid all of them.
    """
    qapp, reg, s = scene
    _add_plain_chunk(reg, s)          # forces the default chunkgroup into being
    group = _add_plain_chunk(reg, s, "chunkgroup")
    qapp.processEvents()

    assert group in s.get_chunks(), "a user-created chunkgroup must remain a node"
    assert s.peek_root_context() not in s.get_chunks(), (
        "the outermost container is not a node in any graph"
    )


def test_a_new_chunkgroup_nests_inside_the_root_container(scene):
    """The second cause: it was authored as a SIBLING of the root.

    _resolve_chunk_root falls back to the /chunks scope for a context (get_context()
    returns self for one), so a chunkgroup created with nothing dived into landed at
    /chunks/Group -- next to /chunks/ChunkGroup rather than inside it. Being in no
    container's graph, it was invisible however get_chunks() answered.
    """
    qapp, reg, s = scene
    _add_plain_chunk(reg, s)
    group = _add_plain_chunk(reg, s, "chunkgroup")
    qapp.processEvents()

    root = s.peek_root_context()
    assert get_chunk_prim_path(group).startswith(get_chunk_prim_path(root) + "/")
    assert chunk_usd.is_node_in_context(group, root), (
        "it must be a node in the root's graph, which is what makes it visible"
    )


def test_peeking_at_the_root_never_creates_one(scene):
    """get_chunks() is a read; it must not author a context just by being called."""
    _qapp, _reg, s = scene

    assert s.peek_root_context() is None
    assert s.get_chunks() == ()
    assert s.peek_root_context() is None, "reading the scene created a context"


def test_the_chunks_root_is_a_scope_not_a_chunk(scene):
    """/chunks is "bara ett scope" (Dan) -- like /lights or /render.

    It was authored with the Chunk typename, the same as the chunks hanging under it, so
    the scope looked like a chunk prim to anything reading the stage back.
    """
    qapp, reg, s = scene
    _add_plain_chunk(reg, s)
    qapp.processEvents()

    stage = s.get_stage()
    root = stage.GetPrimAtPath(CHUNKS_SCOPE)
    assert root and root.IsValid()
    assert root.GetTypeName() == "Scope", (
        f"the /chunks scope must not be a Chunk prim, got {root.GetTypeName()!r}"
    )


def test_chunkgroups_and_chunks_under_the_scope_are_still_chunk_prims(scene):
    """Only the scope changes type -- everything inside it is a real chunk prim."""
    qapp, reg, s = scene
    _add_plain_chunk(reg, s)
    group = _add_plain_chunk(reg, s, "chunkgroup")
    qapp.processEvents()

    stage = s.get_stage()
    for c in (s.peek_root_context(), group):
        prim = stage.GetPrimAtPath(get_chunk_prim_path(c))
        assert prim and prim.IsValid(), f"no prim for {c.name}"
        assert prim.GetTypeName() == "Chunk", f"{c.name} should stay a Chunk prim"
