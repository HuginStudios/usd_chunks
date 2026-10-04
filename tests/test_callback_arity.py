"""Which arguments a callback receives, and when it is skipped entirely.

Dispatch used to pick the argument count from whether the connection's owner weakref was
still alive and whether the callback was a bound method -- neither of which the callback
author can see. A three-argument callback was therefore invoked with two once its owner
was collected, raising `missing 1 required positional argument: 'sender'` mid-teardown.
Arity now comes from the callback's own declared parameters.
"""

from __future__ import annotations

import gc

from pxr import Sdf, Usd

from usd_chunks import chunk, chunk_notice, chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_notice import ChunkNoticeAlias


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


chunk_registry.ChunkRegistry().initialize(_DummyScene(), "pytest")


def test_three_param_function_receives_owner() -> None:
    """The convenience form for module-level functions: owner is the object add_callback
    was called on."""
    got: list = []

    def cb(owner, notice, sender):
        got.append(owner)

    button = chunk.Button("click")
    button.add_callback(cb, send_on_create=False)
    button.recook()

    assert got == [button]


def test_two_param_function_receives_notice_and_sender() -> None:
    """The Tf.Notice form, declared by a plain function that has an owner.

    Previously the owner was injected regardless, so this callback was handed three
    arguments and raised TypeError -- swallowed by handle_callback's except, leaving the
    callback silently never running.
    """
    got: list = []

    def cb(notice, sender):
        got.append(sender)

    button = chunk.Button("click")
    button.add_callback(cb, send_on_create=False)
    button.recook()

    assert got == [button]


def test_bound_method_receives_notice_and_sender() -> None:
    """A method on a Chunk subclass already matches Tf.Notice, with self as the chunk."""

    class _MyChunk(chunk.Chunk):
        def __init__(self):
            super().__init__(name="ArityChunk")
            self.seen: list[str] = []
            button = chunk.Button("click")
            button.add_callback(self.on_click, send_on_create=False)
            self.add_param(button)

        def on_click(self, notice, sender):
            self.seen.append(sender.name)

    my_chunk = _MyChunk()
    my_chunk.get_param("click").recook()

    assert my_chunk.seen == ["click"]


def test_collected_owner_is_not_dispatched() -> None:
    """A connection registered with an owner must go quiet once that owner is gone.

    Dispatching anyway is what produced the teardown TypeError: the owner weakref had
    died, so the argument count silently changed under a callback that never opted in.
    """
    got: list = []

    def cb(owner, notice, sender):
        got.append(owner)

    owner = chunk.Button("owner")
    connection = chunk_notice.NoticeConnection(cb, ChunkNoticeAlias.VALUE_CHANGED, owner=owner)

    del owner
    gc.collect()

    notice_type = chunk_notice.get_notice(ChunkNoticeAlias.VALUE_CHANGED)
    connection.handle_callback(notice_type(None), None)

    assert got == []


def test_ownerless_connection_still_dispatches() -> None:
    """The skip above keys off a *collected* owner, not an absent one -- a connection that
    never had an owner (e.g. a global listener) must still fire."""
    got: list = []

    def cb(notice, sender):
        got.append(sender)

    connection = chunk_notice.NoticeConnection(cb, ChunkNoticeAlias.VALUE_CHANGED)
    notice_type = chunk_notice.get_notice(ChunkNoticeAlias.VALUE_CHANGED)
    connection.handle_callback(notice_type(None), "sender-sentinel")

    assert got == ["sender-sentinel"]


def test_owner_layer_not_yet_in_stack_dispatches_without_crashing() -> None:
    """Regression (issue #83): a listen_globally=True callback (edit_target.py's
    _refresh_edit_target is the real one that hit this) can fire while its owner's
    get_edit_context() layer is momentarily out of the stage's local layer stack for a
    reason other than being muted -- recook reordering a graph layer's subLayerPaths,
    say. Usd.EditContext(stage, ctx) raises Tf.ErrorException ("not in the local
    LayerStack") in that gap. Unlike the muted case (a dormant chunk that must not
    author, so handle_callback skips it outright), there's no later retry for this one,
    so it must still run -- just without the EditContext wrapper around it."""
    stage = Usd.Stage.CreateInMemory()
    # Never inserted into stage's layer stack and never muted -- the "not there yet,
    # not muted either" case handle_callback has to tell apart from being muted.
    orphan_layer = Sdf.Layer.CreateAnonymous("orphaned")

    class _StageOwner:
        def get_stage(self):
            return stage

        def get_edit_context(self):
            return orphan_layer

    got: list = []

    def cb(notice, sender):
        got.append(sender)

    owner = _StageOwner()
    connection = chunk_notice.NoticeConnection(cb, ChunkNoticeAlias.VALUE_CHANGED, owner=owner)
    notice_type = chunk_notice.get_notice(ChunkNoticeAlias.VALUE_CHANGED)

    connection.handle_callback(notice_type(None), "sender-sentinel")  # must not raise

    assert got == ["sender-sentinel"]
