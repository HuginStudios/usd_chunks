"""When a handle shows, and how a drag is bracketed.

Two separate concerns that share one property: neither invents machinery. Activation is
declarative -- `editmode` on the gizmo, applied by the host against a selection it already
knows -- and the drag bracket repurposes three notices that have sat in chunk_notice.py as
dead scaffolding with no listeners since long before this feature.
"""
from __future__ import annotations

import pytest
from pxr import Sdf, Usd, UsdGeom

from usd_chunks import chunk, chunk_notice, chunk_registry
from usd_chunks.gizmo import runtime as chunk_gizmo
from usd_chunks.chunk_core import ChunkConfig
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_notice import ChunkNoticeAlias
from usd_chunks.chunk_stage import _CHUNKS_ROOT
from chunk_prim_authoring import _author_prim


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
        chunk_gizmo.apply_bindings(c, "handle", value)
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
        chunk_gizmo.apply_bindings(c, "handle", value)
    c.stop_gizmo()

    assert observed == [1.0, 2.0, 3.0, 4.0]


def test_changed_fires_after_the_write_not_before() -> None:
    """So a listener can read the new value straight off the params."""
    c = _chunk()
    seen_values = []
    # Held: a connection nobody references is collected whenever the garbage collector
    # next runs, which made this pass alone and fail somewhere in the full suite.
    connection = chunk_notice.NoticeConnection(
        lambda notice, sender: seen_values.append(c.get_param("amount").value),
        ChunkNoticeAlias.GIZMO_CHANGED,
        globally=True,
    )

    chunk_gizmo.apply_bindings(c, "handle", 7.0)

    assert seen_values == [7.0]
    del connection


def test_a_rejected_value_writes_nothing_and_announces_nothing() -> None:
    """A half-applied drag would show as the object tearing apart mid-gesture, so a value the
    handle cannot use must land nowhere at all -- not partially."""
    c = chunk.Chunk("Mover")
    c.add_param(chunk.LineEdit("primpath_location", default_value="/World/Cube"))
    grp = chunk.Group("move_grp", "Move", gizmo="translate")
    for axis in ("x", "y", "z"):
        grp.add_param(chunk.Slider(axis, axis.upper(), default_value=0.0))
    c.add_param(grp)
    c._config = ChunkConfig(module="mover")

    seen, _connections = _record(c)

    with pytest.raises(ValueError, match="drives 3 params"):
        chunk_gizmo.apply_bindings(c, "move_grp", (1.0, 2.0))  # one short

    assert c.get_param("x").value == pytest.approx(0.0)
    assert seen == []


# ---------------------------------------------------------------------------
# the drag throttle: a cook at most every DRAG_INTERVAL, the newest value
# ---------------------------------------------------------------------------

class _TimerScene(_DummyScene):
    """A host with an event loop: call_later keeps fn for the test to fire."""

    def __init__(self):
        super().__init__()
        self.timers = []

    def call_later(self, fn, delay: float = 0.0):
        entry = [fn, delay, False]
        self.timers.append(entry)
        return lambda: entry.__setitem__(2, True)

    def fire(self) -> None:
        due, self.timers = self.timers, []
        for fn, _delay, cancelled in due:
            if not cancelled:
                fn()


@pytest.fixture
def timer_scene(monkeypatch):
    scene = _TimerScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    monkeypatch.setattr(chunk_gizmo, "DRAG_INTERVAL", 3600.0)  # every write after the first is "too soon"
    yield scene
    chunk_registry.ChunkRegistry().initialize(_DummyScene(), "pytest")


def _amounts(c) -> list:
    observed = []
    param = c.get_param("amount")
    param.add_callback(lambda notice, sender: observed.append(param.value),
                       notice_alias=ChunkNoticeAlias.PARAM_CHANGED, send_on_create=False)
    return observed


def test_a_drag_cooks_the_newest_value_once_the_interval_is_up(timer_scene) -> None:
    """Mouse moves that come faster than a cook each cooking in turn is what makes a slow
    chunk trail further and further behind the cursor. The first write goes straight
    through; the ones after it collapse into one, the newest, on a timer."""
    c = _chunk()
    observed = _amounts(c)

    c.start_gizmo()
    for value in (1.0, 2.0, 3.0, 4.0):
        chunk_gizmo.apply_bindings(c, "handle", value)
    assert observed == [1.0]
    assert len(timer_scene.timers) == 1  # one timer, however many writes it carries

    timer_scene.fire()
    assert observed == [1.0, 4.0]
    c.stop_gizmo()
    assert observed == [1.0, 4.0]


def test_letting_go_cooks_where_the_handle_was_let_go_before_stop(timer_scene) -> None:
    """Never batched to the end -- but never left waiting past it either: the held-back
    write lands before GIZMO_STOP, so an undo block closing on STOP holds it."""
    c = _chunk()
    observed = _amounts(c)
    seen, _connections = _record(c)

    c.start_gizmo()
    for value in (1.0, 2.0, 3.0):
        chunk_gizmo.apply_bindings(c, "handle", value)
    c.stop_gizmo()

    assert observed == [1.0, 3.0]
    assert seen == [ChunkNoticeAlias.GIZMO_START, ChunkNoticeAlias.GIZMO_CHANGED,
                    ChunkNoticeAlias.GIZMO_CHANGED, ChunkNoticeAlias.GIZMO_STOP]
    timer_scene.fire()  # the timer, cancelled: nothing more
    assert observed == [1.0, 3.0]


def test_writes_spaced_past_the_interval_all_cook(timer_scene, monkeypatch) -> None:
    monkeypatch.setattr(chunk_gizmo, "DRAG_INTERVAL", 0.0)
    c = _chunk()
    observed = _amounts(c)

    c.start_gizmo()
    for value in (1.0, 2.0, 3.0):
        chunk_gizmo.apply_bindings(c, "handle", value)
    c.stop_gizmo()

    assert observed == [1.0, 2.0, 3.0]
    assert timer_scene.timers == []


def test_a_due_tick_cooks_itself_though_a_timer_is_waiting(timer_scene, monkeypatch) -> None:
    """On Windows a Qt timer only fires once the queue is free of mouse moves, so while the
    mouse keeps moving it may never fire. A tick that comes once the interval is up cooks
    there and then, and the waiting timer is cancelled."""
    c = _chunk()
    observed = _amounts(c)
    clock = [100.0]
    monkeypatch.setattr(chunk_gizmo.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(chunk_gizmo, "DRAG_INTERVAL", 0.05)

    c.start_gizmo()
    chunk_gizmo.apply_bindings(c, "handle", 1.0)  # first: now
    chunk_gizmo.apply_bindings(c, "handle", 2.0)  # too soon: a timer, which never fires
    clock[0] += 0.06
    chunk_gizmo.apply_bindings(c, "handle", 3.0)  # due: now, the timer starved or not
    assert observed == [1.0, 3.0]

    timer_scene.fire()  # cancelled
    assert observed == [1.0, 3.0]
    c.stop_gizmo()
    assert observed == [1.0, 3.0]


def test_after_a_slow_cook_the_drag_waits_for_the_release(timer_scene, monkeypatch) -> None:
    """A cook longer than SLOW_COOK blocks the viewport that long, so the handle would
    freeze on every one. Past it, the drag stops cooking while the handle moves, and cooks
    where it is let go (or rests -- the next test)."""
    c = _chunk()
    clock = [100.0]
    monkeypatch.setattr(chunk_gizmo.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(chunk_gizmo, "DRAG_INTERVAL", 0.0)
    observed = []
    param = c.get_param("amount")

    def cook(notice, sender):
        observed.append(param.value)
        clock[0] += 0.2  # a slow cook

    param.add_callback(cook, notice_alias=ChunkNoticeAlias.PARAM_CHANGED, send_on_create=False)

    c.start_gizmo()
    for value in (1.0, 2.0, 3.0, 4.0):
        chunk_gizmo.apply_bindings(c, "handle", value)
    assert observed == [1.0]  # the first, which showed it was slow
    live = [t for t in timer_scene.timers if not t[2]]
    assert [t[1] for t in live] == [chunk_gizmo.DRAG_REST], "one rest wait, the last tick's"
    c.stop_gizmo()
    assert observed == [1.0, 4.0]

    c.start_gizmo()  # the next drag knows: nothing until the release
    chunk_gizmo.apply_bindings(c, "handle", 5.0)
    chunk_gizmo.apply_bindings(c, "handle", 6.0)
    assert observed == [1.0, 4.0]
    c.stop_gizmo()
    assert observed == [1.0, 4.0, 6.0]


def test_a_slow_drag_cooks_where_the_handle_rests(timer_scene, monkeypatch) -> None:
    """Held still mid-drag, the handle cooks once DRAG_REST is up: no events come from a
    still mouse, so the timer fires. A tick before it starts the wait again."""
    c = _chunk()
    clock = [100.0]
    monkeypatch.setattr(chunk_gizmo.time, "monotonic", lambda: clock[0])
    observed = []
    param = c.get_param("amount")

    def cook(notice, sender):
        observed.append(param.value)
        clock[0] += 0.2

    param.add_callback(cook, notice_alias=ChunkNoticeAlias.PARAM_CHANGED, send_on_create=False)

    c.start_gizmo()
    chunk_gizmo.apply_bindings(c, "handle", 1.0)  # slow
    chunk_gizmo.apply_bindings(c, "handle", 2.0)
    chunk_gizmo.apply_bindings(c, "handle", 3.0)  # cancels 2.0's wait
    timer_scene.fire()  # the mouse rests
    assert observed == [1.0, 3.0]

    timer_scene.fire()  # nothing new waiting
    c.stop_gizmo()  # nothing new to cook on release either
    assert observed == [1.0, 3.0]


def test_a_chunk_that_got_fast_drags_live_again(timer_scene, monkeypatch) -> None:
    """The release cook is timed too, so a chunk made cheaper drags live from the next drag."""
    c = _chunk()
    clock = [100.0]
    monkeypatch.setattr(chunk_gizmo.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(chunk_gizmo, "DRAG_INTERVAL", 0.0)
    cost = [0.2]
    observed = []
    param = c.get_param("amount")

    def cook(notice, sender):
        observed.append(param.value)
        clock[0] += cost[0]

    param.add_callback(cook, notice_alias=ChunkNoticeAlias.PARAM_CHANGED, send_on_create=False)

    c.start_gizmo()
    chunk_gizmo.apply_bindings(c, "handle", 1.0)  # slow
    chunk_gizmo.apply_bindings(c, "handle", 2.0)
    cost[0] = 0.0  # say a cache kicked in
    c.stop_gizmo()  # the release cook is fast
    c.start_gizmo()
    for value in (3.0, 4.0):
        chunk_gizmo.apply_bindings(c, "handle", value)
    c.stop_gizmo()

    assert observed == [1.0, 2.0, 3.0, 4.0]


def test_outside_a_drag_every_write_cooks_now(timer_scene) -> None:
    """A script or a one-off set has no START to throttle against."""
    c = _chunk()
    observed = _amounts(c)

    for value in (1.0, 2.0):
        chunk_gizmo.apply_bindings(c, "handle", value)

    assert observed == [1.0, 2.0]
    assert timer_scene.timers == []


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
    # Whitespace collapsed: the phrase is free to wrap onto a new line in the docstring.
    assert "host filters" in " ".join(chunk_gizmo.resolve_gizmos.__doc__.split())
