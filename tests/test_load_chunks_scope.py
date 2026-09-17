"""A chunk prim appearing on the stage must always become a chunk.

load_chunks used to answer "did anything appear?" by traversing the whole stage on every
single stage change, which is both quadratic in the graph and linear in the size of a
scene that has nothing to do with chunks. It now looks only inside the subtrees USD says
were resynced.

That is only safe while a prim cannot appear outside a resynced subtree, so these tests
pin the arrival routes a chunk prim actually has, and the notice behaviour the narrowing
rests on. They are deliberately about *whether the chunk shows up at all* — the failure
mode of getting this wrong is a node silently missing from the graph, not a crash.
"""

from __future__ import annotations

from pxr import Sdf, Tf, Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.chunk_stage import (
    _CHUNK_PRIM_TYPENAME,
    _CHUNK_TYPE_ATTR,
    _CHUNKS_ROOT,
    ChunkStageSync,
)


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


def _live_scene():
    """A stage wired to a ChunkStageSync, as if a file were open in the host."""
    stage = Usd.Stage.CreateInMemory()
    scene = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    ChunkStageSync().initialize(stage, scene)
    return stage, scene


def _author_chunk_prim(stage, path: str, chunk_type: str = "prim"):
    """Author a module-backed Chunk prim the way an external editor would."""
    prim = stage.DefinePrim(Sdf.Path(path), _CHUNK_PRIM_TYPENAME)
    attr = prim.CreateAttribute(_CHUNK_TYPE_ATTR, Sdf.ValueTypeNames.Token, custom=False)
    attr.SetVariability(Sdf.VariabilityUniform)
    attr.Set(chunk_type)
    return prim


def _names(scene) -> set:
    return {c.name for c in scene.get_chunks()}


def test_prim_authored_directly_on_the_stage_becomes_a_chunk():
    """The plain case: something authors a Chunk prim, a chunk appears."""
    stage, scene = _live_scene()

    _author_chunk_prim(stage, f"{_CHUNKS_ROOT}/Direct")

    assert "Direct" in _names(scene)


def test_chunk_arriving_in_a_sublayer_is_picked_up():
    """A chunk can arrive without anyone authoring it on this stage at all.

    Inserting a sublayer that already holds a Chunk prim resyncs the *parent* of the new
    prim, not the prim itself — which is why the narrowing has to scope by subtree rather
    than by the changed paths themselves.
    """
    stage, scene = _live_scene()
    _author_chunk_prim(stage, f"{_CHUNKS_ROOT}/Existing")

    incoming = Sdf.Layer.CreateAnonymous("incoming")
    donor = Usd.Stage.Open(incoming)
    _author_chunk_prim(donor, f"{_CHUNKS_ROOT}/FromSublayer")

    stage.GetRootLayer().subLayerPaths.insert(0, incoming.identifier)

    assert "FromSublayer" in _names(scene)


def test_chunk_nested_under_an_unrelated_prim_is_picked_up():
    """Chunk prims are not required to sit under the chunk root.

    A reference or a hand-authored file can put them under any prim, and a scoped walk
    that assumed the chunk root would never look there.
    """
    stage, scene = _live_scene()
    stage.DefinePrim(Sdf.Path("/World"), "Xform")

    _author_chunk_prim(stage, "/World/Nested")

    assert "Nested" in _names(scene)


def test_objects_changed_precedes_stage_contents_changed():
    """The ordering the scoping depends on.

    The resynced paths arrive on ObjectsChanged, and the scan runs from
    StageContentsChanged. If USD ever sent them the other way round, the scan would run
    against the paths of the *previous* change and could narrow away a real arrival.
    """
    stage = Usd.Stage.CreateInMemory()
    seen: list = []
    keys = [
        Tf.Notice.Register(Usd.Notice.ObjectsChanged, lambda n, s: seen.append("objects"), stage),
        Tf.Notice.Register(Usd.Notice.StageContentsChanged, lambda n, s: seen.append("contents"), stage),
    ]

    try:
        stage.DefinePrim(Sdf.Path("/World/Cube"), "Cube")
    finally:
        del keys

    assert seen, "no notices fired at all"
    assert seen[0] == "objects", f"expected ObjectsChanged first, got {seen}"


def test_resynced_paths_cover_every_arrival_route():
    """Every route a chunk prim can arrive by reports a resynced path containing it.

    This is the assumption the scoping is built on, checked directly against USD rather
    than inferred: author, sublayer insert, and mute/unmute of a layer holding chunks.
    """
    stage = Usd.Stage.CreateInMemory()
    stage.DefinePrim(Sdf.Path(_CHUNKS_ROOT), _CHUNK_PRIM_TYPENAME)
    resyncs: list = []
    key = Tf.Notice.Register(
        Usd.Notice.ObjectsChanged,
        lambda n, s: resyncs.extend(str(p) for p in n.GetResyncedPaths()),
        stage,
    )

    try:
        incoming = Sdf.Layer.CreateAnonymous("incoming")
        donor = Usd.Stage.Open(incoming)
        _author_chunk_prim(donor, f"{_CHUNKS_ROOT}/Arriving")

        for label, action in (
            ("author", lambda: _author_chunk_prim(stage, f"{_CHUNKS_ROOT}/Authored")),
            ("sublayer", lambda: stage.GetRootLayer().subLayerPaths.insert(0, incoming.identifier)),
            ("mute", lambda: incoming.SetMuted(True)),
            ("unmute", lambda: incoming.SetMuted(False)),
        ):
            resyncs.clear()
            action()
            assert resyncs, f"{label} reported no resynced path to scope a scan by"
            # Every resync must be an ancestor of, or equal to, something under the chunk
            # root — i.e. a subtree walk from it would reach the new prim.
            assert any(
                _CHUNKS_ROOT.startswith(path) or path.startswith(_CHUNKS_ROOT) or path == "/"
                for path in resyncs
            ), f"{label} resynced {resyncs}, which does not contain the arriving prim"
    finally:
        del key
