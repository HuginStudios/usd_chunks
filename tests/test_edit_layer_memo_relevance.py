"""edit_layer_memo_affected: which USD change rounds may move a nearest_upstream_edit_layer
answer, and -- the point of it -- which provably cannot.

The memo used to be dropped on every ObjectsChanged. On a scene load that is hundreds of
rounds of a chunk writing the prims it drives, none of which touch the exec chain or an
Edit Target marker, and each cost every chunk a fresh walk of the chain above it. Each
case here is one route a change arrives by, checked against a live stage rather than
assumed, the same way test_load_chunks_scope pins USD's notice behaviour.
"""
from __future__ import annotations

from pxr import Sdf, Tf, Usd

from usd_chunks import chunk_stage
from usd_chunks.chunk_stage import (
    _CHUNK_PRIM_TYPENAME,
    _CHUNKS_ROOT,
    _EDIT_TARGET_MARKER,
    _EXEC_INPUT,
    edit_layer_memo_affected,
    nearest_upstream_edit_layer,
)


class _Capture:
    """Answers edit_layer_memo_affected for each ObjectsChanged a stage sends, inside the
    handler -- a Usd.Notice is only valid for the duration of its delivery, which is also
    how the real caller (_on_objects_changed) uses it."""

    def __init__(self, stage, known):
        self.known = known
        self.results: list = []
        self.pathless: list = []
        self._key = Tf.Notice.Register(Usd.Notice.ObjectsChanged, self._on, stage)

    def _on(self, notice, _sender):
        self.pathless.append(
            not notice.GetResyncedPaths() and not notice.GetChangedInfoOnlyPaths()
        )
        self.results.append(edit_layer_memo_affected(notice, self.known))

    def any_affected(self) -> bool:
        assert self.results, "the action sent no ObjectsChanged at all"
        return any(self.results)

    def clear(self):
        self.results.clear()
        self.pathless.clear()


def _stage_with_chain():
    """/chunks/A -> /chunks/B, plus a driven prim /geo the chunks write into."""
    stage = Usd.Stage.CreateInMemory()
    stage.DefinePrim(Sdf.Path(_CHUNKS_ROOT), "Scope")
    a = stage.DefinePrim(Sdf.Path(f"{_CHUNKS_ROOT}/A"), _CHUNK_PRIM_TYPENAME)
    b = stage.DefinePrim(Sdf.Path(f"{_CHUNKS_ROOT}/B"), _CHUNK_PRIM_TYPENAME)
    a.CreateAttribute("outputs:chunk", Sdf.ValueTypeNames.Token)
    b.CreateAttribute(_EXEC_INPUT, Sdf.ValueTypeNames.Token).AddConnection(
        Sdf.Path(f"{_CHUNKS_ROOT}/A.outputs:chunk")
    )
    stage.DefinePrim(Sdf.Path("/geo"), "Xform")
    # Prime the memo for this stage, as a real load does before any notice arrives.
    nearest_upstream_edit_layer(stage, b)
    return stage, a, b


_KNOWN = (f"{_CHUNKS_ROOT}/A", f"{_CHUNKS_ROOT}/B")


def test_driven_prim_writes_do_not_invalidate():
    stage, _a, _b = _stage_with_chain()
    cap = _Capture(stage, _KNOWN)

    geo = stage.GetPrimAtPath("/geo")
    geo.CreateAttribute("inputs:tx", Sdf.ValueTypeNames.Float).Set(1.0)
    assert not cap.any_affected(), (
        "an attribute on a driven prim is not on the exec chain and carries no marker"
    )

    cap.clear()
    stage.DefinePrim(Sdf.Path("/geo/child"), "Cube")
    assert not cap.any_affected(), (
        "a prim appearing under a driven prim cannot be reached by an exec walk"
    )

    cap.clear()
    stage.GetPrimAtPath("/geo/child").SetActive(False)
    assert not cap.any_affected()


def test_chunk_prim_param_write_does_not_invalidate():
    stage, _a, b = _stage_with_chain()
    cap = _Capture(stage, _KNOWN)
    b.CreateAttribute("inputs:size", Sdf.ValueTypeNames.Float).Set(2.0)
    assert not cap.any_affected(), (
        "a chunk's ordinary param attribute is neither the exec input nor the marker"
    )


def test_exec_connection_change_invalidates():
    stage, _a, b = _stage_with_chain()
    cap = _Capture(stage, _KNOWN)
    b.GetAttribute(_EXEC_INPUT).ClearConnections()
    assert cap.any_affected()


def test_marker_write_invalidates():
    stage, a, _b = _stage_with_chain()
    cap = _Capture(stage, _KNOWN)
    a.CreateAttribute(_EDIT_TARGET_MARKER, Sdf.ValueTypeNames.String, custom=True).Set("x")
    assert cap.any_affected()


def test_chunk_prim_arrival_and_removal_invalidate():
    stage, _a, _b = _stage_with_chain()
    cap = _Capture(stage, _KNOWN)
    stage.DefinePrim(Sdf.Path(f"{_CHUNKS_ROOT}/C"), _CHUNK_PRIM_TYPENAME)
    assert cap.any_affected(), "a Chunk-typed prim resynced"

    cap.clear()
    stage.RemovePrim(Sdf.Path(f"{_CHUNKS_ROOT}/A"))
    assert cap.any_affected(), (
        "the prim is gone, so its type cannot be read -- must be taken as relevant"
    )


def test_ancestor_of_known_chunk_prim_invalidates():
    """A resync of a non-Chunk prim counts when a known chunk prim lives under it --
    which is how a renamed context root outside /chunks is covered."""
    stage = Usd.Stage.CreateInMemory()
    stage.DefinePrim(Sdf.Path("/World"), "Xform")
    c = stage.DefinePrim(Sdf.Path("/World/Chunky"), _CHUNK_PRIM_TYPENAME)
    nearest_upstream_edit_layer(stage, c)

    known = _Capture(stage, ("/World/Chunky",))
    stage.GetPrimAtPath("/World").SetActive(False)
    assert known.any_affected()

    stage.GetPrimAtPath("/World").SetActive(True)
    elsewhere = _Capture(stage, ("/Elsewhere/Chunky",))
    stage.GetPrimAtPath("/World").SetActive(False)
    assert not elsewhere.any_affected()


def test_layer_stack_changes_invalidate_even_without_paths():
    """A sublayer edit arrives as an info change on "/"; a mute of a layer with no
    composed opinions arrives with no paths at all. Both change the layer stack, which is
    what decides whether a marker's layer counts, so both must be relevant."""
    stage, _a, b = _stage_with_chain()
    cap = _Capture(stage, _KNOWN)
    sub = Sdf.Layer.CreateAnonymous("sub")
    stage.GetRootLayer().subLayerPaths.insert(0, sub.identifier)
    assert cap.any_affected(), "sublayer insert"

    cap.clear()
    stage.MuteLayer(sub.identifier)
    assert all(cap.pathless), "premise: USD reports a mute of an opinion-less layer with no paths"
    assert cap.any_affected(), "mute"

    cap.clear()
    stage.UnmuteLayer(sub.identifier)
    assert cap.any_affected(), "unmute"

    # Each of those rounds is what drops the memo (_on_objects_changed acts on the answer
    # straight away), so nothing waits on a later round to notice the stack moved: rebuilt,
    # an irrelevant round is a no-op again. Only the muted set is compared against the one
    # the memo was built with -- comparing the whole stack cost a layer per chunk on every
    # notice, for the one change ("/") that already announces itself.
    chunk_stage.invalidate_edit_layer_memo()
    nearest_upstream_edit_layer(stage, b)
    cap.clear()
    stage.GetPrimAtPath("/geo").CreateAttribute("inputs:ty", Sdf.ValueTypeNames.Float).Set(1.0)
    assert not cap.any_affected()


def test_muting_an_edit_target_layer_moves_the_answer_end_to_end():
    """The reason the layer-stack rule exists: an Edit Target's layer holds driven
    content only, so muting it resyncs nothing on the exec chain -- yet the answer for
    everything downstream must fall back, or a write lands in a layer that is not
    composed."""
    stage, a, b = _stage_with_chain()
    target = Sdf.Layer.CreateAnonymous("target")
    stage.GetRootLayer().subLayerPaths.insert(0, target.identifier)
    # Driven content in the target layer, so the mute below is reported as resyncs of
    # /geo -- a driven prim, which on its own is exactly what must NOT invalidate.
    with Usd.EditContext(stage, target):
        stage.GetPrimAtPath("/geo").CreateAttribute("driven", Sdf.ValueTypeNames.Float).Set(1.0)
    a.CreateAttribute(_EDIT_TARGET_MARKER, Sdf.ValueTypeNames.String, custom=True).Set(
        target.identifier
    )
    chunk_stage.invalidate_edit_layer_memo()
    assert nearest_upstream_edit_layer(stage, b) is target

    cap = _Capture(stage, _KNOWN)
    stage.MuteLayer(target.identifier)
    assert not all(cap.pathless), "premise: a mute of a layer with opinions reports paths"
    if cap.any_affected():
        chunk_stage.invalidate_edit_layer_memo()
    assert nearest_upstream_edit_layer(stage, b) is stage.GetSessionLayer(), (
        "a muted Edit Target layer must no longer be the answer for downstream chunks"
    )
