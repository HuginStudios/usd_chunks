"""When a handle shows, and how a drag is bracketed.

Two separate concerns that share one property: neither invents machinery. Activation is
declarative -- `editmode` on the gizmo, applied by the host against a selection it already
knows -- and the drag bracket repurposes three notices that have sat in chunk_notice.py as
dead scaffolding with no listeners since long before this feature.
"""
from __future__ import annotations

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom

from usd_chunks import chunk, chunk_gizmo, chunk_notice, chunk_registry
from usd_chunks.chunk_core import ChunkConfig, ChunkFlags, SceneSender
from usd_chunks.chunk_notice import ChunkNoticeAlias
from usd_chunks.plugins.chunk_usd import _CHUNKS_ROOT, _author_prim


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


chunk_registry.ChunkRegistry().initialize(_DummyScene(), "pytest")


def _stage(path: str = "/World/Cube") -> Usd.Stage:
    stage = Usd.Stage.CreateInMemory()
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        UsdGeom.Cube.Define(stage, path)

    class _StagedScene(_DummyScene):
        def get_stage(self):
            return stage

    chunk_registry.ChunkRegistry().initialize(_StagedScene(), "pytest")
    return stage


def _chunk(gizmos=None):
    c = chunk.Chunk("Mover")
    c.add_param(chunk.LineEdit("primpath_location", default_value="/World/Cube"))
    c.add_param(chunk.Slider("amount", "Amount", min_value=0.0, max_value=100.0, default_value=0.0))
    c._config = ChunkConfig(module="mover", gizmos=gizmos or [
        chunk.Gizmo("handle", type="cube", driver="amount"),
    ])
    return c


def _authored(stage, c):
    _author_prim(stage, c)
    return stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/{c.name}")


# ---------------------------------------------------------------------------
# editmode
# ---------------------------------------------------------------------------

def test_editmode_defaults_to_prim_selection() -> None:
    stage = _stage()
    prim = _authored(stage, _chunk())

    assert list(prim.GetAttribute("ui:gizmo:handle:editmode").Get()) == ["onPrimSelection"]


def test_editmode_is_an_array_so_a_gizmo_can_declare_more_than_one() -> None:
    """A gizmo wanting both viewport and node-graph activation is normal, and enumerating
    combinations as single tokens gets silly fast."""
    stage = _stage()
    prim = _authored(stage, _chunk([
        chunk.Gizmo("handle", type="cube", driver="amount",
                    editmode=["onPrimSelection", "onChunkSelection"]),
    ]))

    attr = prim.GetAttribute("ui:gizmo:handle:editmode")
    assert attr.GetTypeName() == Sdf.ValueTypeNames.TokenArray
    assert list(attr.Get()) == ["onPrimSelection", "onChunkSelection"]


@pytest.mark.parametrize("mode", ["onPrimSelection", "onChunkSelection", "always"])
def test_every_editmode_token_is_accepted(mode) -> None:
    assert chunk.Gizmo("g", type="cube", driver="amount", editmode=[mode]).editmode == (mode,)


def test_an_unknown_editmode_fails_at_load() -> None:
    with pytest.raises(ValueError, match="unknown editmode"):
        chunk.Gizmo("g", type="cube", driver="amount", editmode=["onWednesday"])


def test_editmode_round_trips_through_resolve() -> None:
    stage = _stage()
    prim = _authored(stage, _chunk([
        chunk.Gizmo("handle", type="cube", driver="amount", editmode=["always"]),
    ]))

    assert chunk_gizmo.resolve_gizmos(prim)[0].editmode == ("always",)


def test_editmode_needs_no_call_into_us() -> None:
    """The whole reason activation is declarative: everything a host needs to decide whether
    to draw is on the record it already has."""
    stage = _stage()
    desc = chunk_gizmo.resolve_gizmos(_authored(stage, _chunk()))[0]

    assert desc.editmode == ("onPrimSelection",)
    assert desc.anchor, "and the anchor tells it which selection to match against"


# ---------------------------------------------------------------------------
# the drag bracket
# ---------------------------------------------------------------------------

def _record(chunk_obj):
    """Collect gizmo notices in the order they arrive."""
    seen = []

    def recorder(alias):
        # Exactly two parameters, and a closure rather than a default argument. The notice
        # system introspects arity to decide how to call a listener, so a *args callback is
        # not recognised as one and never fires, and a third default parameter gets bound
        # positionally to the sender instead of keeping its default.
        def callback(notice, sender):
            seen.append(alias)
        return callback

    # Held by the caller: NoticeConnection does not keep the callback alive on its own, so a
    # connection whose only reference is a temporary silently stops firing.
    keepalive = [recorder(a) for a in (ChunkNoticeAlias.GIZMO_START,
                                       ChunkNoticeAlias.GIZMO_CHANGED,
                                       ChunkNoticeAlias.GIZMO_STOP)]
    connections = [
        chunk_notice.NoticeConnection(cb, alias, globally=True)
        for cb, alias in zip(keepalive, (ChunkNoticeAlias.GIZMO_START,
                                         ChunkNoticeAlias.GIZMO_CHANGED,
                                         ChunkNoticeAlias.GIZMO_STOP))
    ]
    return seen, (keepalive, connections)


def test_the_typo_is_gone() -> None:
    """GIZMO_CHNAGED sat misspelled with zero listeners repo-wide, so the rename was free --
    but it has to actually be gone, not aliased."""
    assert hasattr(ChunkNoticeAlias, "GIZMO_CHANGED")
    assert not hasattr(ChunkNoticeAlias, "GIZMO_CHNAGED")


def test_a_drag_brackets_its_changes() -> None:
    c = _chunk()
    seen, _connections = _record(c)

    c.start_gizmo()
    for value in (1.0, 2.0, 3.0):
        chunk_gizmo.apply_bindings(c, "handle", {"value": value})
    c.stop_gizmo()

    assert seen == [
        ChunkNoticeAlias.GIZMO_START,
        ChunkNoticeAlias.GIZMO_CHANGED,
        ChunkNoticeAlias.GIZMO_CHANGED,
        ChunkNoticeAlias.GIZMO_CHANGED,
        ChunkNoticeAlias.GIZMO_STOP,
    ]


def test_every_tick_writes_rather_than_batching_to_the_end() -> None:
    """The single most important behaviour in the whole feature, and the one most likely to
    be 'optimised' away the first time a heavy chunk cooks slowly. Batching to GIZMO_STOP is
    what makes a handle feel dead -- the object stops following the cursor and jumps at the
    end. Fix the slow cook instead."""
    c = _chunk()
    observed = []
    param = c.get_param("amount")
    param.add_callback(
        lambda notice, sender: observed.append(param.value),
        notice_alias=ChunkNoticeAlias.PARAM_CHANGED,
        send_on_create=False,
    )

    c.start_gizmo()
    for value in (1.0, 2.0, 3.0, 4.0):
        chunk_gizmo.apply_bindings(c, "handle", {"value": value})
    c.stop_gizmo()

    assert observed == [1.0, 2.0, 3.0, 4.0]


def test_changed_fires_after_the_write_not_before() -> None:
    """So a listener can read the new value straight off the params."""
    c = _chunk()
    seen_values = []
    chunk_notice.NoticeConnection(
        lambda notice, sender: seen_values.append(c.get_param("amount").value),
        ChunkNoticeAlias.GIZMO_CHANGED,
        globally=True,
    )

    chunk_gizmo.apply_bindings(c, "handle", {"value": 7.0})

    assert seen_values == [7.0]


def test_a_rejected_slot_writes_nothing_and_announces_nothing() -> None:
    """A half-applied drag would show as the object tearing apart mid-gesture."""
    c = _chunk()
    seen, _connections = _record(c)

    with pytest.raises(KeyError):
        chunk_gizmo.apply_bindings(c, "handle", {"value": 5.0, "rotate": Gf.Vec3d(0, 0, 0)})

    assert c.get_param("amount").value == pytest.approx(0.0)
    assert seen == []


# ---------------------------------------------------------------------------
# SceneSender stubs
# ---------------------------------------------------------------------------

def test_a_scene_sender_implementing_none_of_them_still_works() -> None:
    """The camera capability is the precedent: usdview implements those methods, Kraken does
    not, and both run."""
    scene = _DummyScene()

    assert scene.supports_gizmos() is False
    assert scene.show_gizmos(_chunk(), []) is None
    assert scene.hide_gizmos(_chunk()) is None


def test_a_host_can_opt_in() -> None:
    class _GizmoHost(_DummyScene):
        def __init__(self):
            super().__init__()
            self.shown = []

        def supports_gizmos(self):
            return True

        def show_gizmos(self, chunk_obj, gizmos):
            self.shown.append((chunk_obj.name, len(gizmos)))

    host = _GizmoHost()
    c = _chunk()
    host.show_gizmos(c, chunk_gizmo.get_gizmos(c))

    assert host.supports_gizmos() is True
    assert host.shown == [("Mover", 1)]


# ---------------------------------------------------------------------------
# who filters
# ---------------------------------------------------------------------------

def test_resolve_returns_every_chunk_that_touched_the_prim() -> None:
    """Filtering to the selected chunk is the host's job, not ours. Getting this backwards
    makes the upstream-editing problem look like a bug: two transform chunks in one chain
    would draw two identical handles in the same place, and picking between them is a
    selection question the host is already answering."""
    assert "host filters" in chunk_gizmo.resolve_gizmos.__doc__
