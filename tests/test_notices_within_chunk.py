"""Notices stay within a chunk; chunks speak to each other only through wires.

A change is announced up through what owns it -- a param, its group, its chunk -- and the
chunks wired below are marked dirty and cooked, but told nothing. Nothing a chunk does may
depend on a channel the graph does not draw: a global listener for another chunk's notices,
a callback on another chunk's param, or a param edge between two chunks are all refused.
"""
from __future__ import annotations

import pytest

from usd_chunks import Scene, chunk
from chunk_stubs import register_stub_module


def _chain(scene, cooks):
    """Up -> Down by exec wire, each with a param, both counting their cooks."""
    def main_with(name, param):
        def main():
            c = chunk.Chunk(name)
            c.add_param(chunk.Slider(param, default_value=0.0))
            return c
        return main

    up_mod = register_stub_module(main=main_with("Up", "size"),
                                  execute=lambda node: cooks.append(node.name))
    down_mod = register_stub_module(main=main_with("Down", "gain"),
                                    execute=lambda node: cooks.append(node.name))
    (up,) = up_mod.load()
    scene.add_chunk(up)
    (down,) = down_mod.load()
    scene.add_chunk(down)
    down.add_dependency(up)
    cooks.clear()
    return up, down


def _heard(target, alias=chunk.ChunkNoticeAlias.CHUNK_CHANGED):
    """What target's own notices of alias reach a callback on target."""
    heard = []

    def callback(owner, notice, sender):
        heard.append(sender)

    target.add_callback(callback, notice_alias=alias)
    target._test_heard_callback = callback  # a NoticeConnection holds its callback weakly
    return heard


def test_the_chunk_that_changed_hears_it() -> None:
    with Scene() as scene:
        cooks = []
        up, _down = _chain(scene, cooks)
        heard = _heard(up)

        up.get_param("size").value = 2.0

        assert heard, "a param change did not reach its own chunk"


def test_a_chunk_below_is_cooked_but_not_told() -> None:
    with Scene() as scene:
        cooks = []
        up, down = _chain(scene, cooks)
        heard = _heard(down)

        up.get_param("size").value = 2.0

        assert down.name in cooks, "the wire did not cook the chunk below"
        assert heard == [], "the chunk below was told about a change in another chunk"


def test_a_change_of_its_own_still_reaches_the_chunk_below() -> None:
    """Being dirtied across a wire is not being quiet for good: its own change is announced."""
    with Scene() as scene:
        cooks = []
        _up, down = _chain(scene, cooks)
        heard = _heard(down)

        down.get_param("gain").value = 3.0

        assert heard, "a chunk's own param change did not reach it"


def test_a_param_edge_within_a_chunk_still_follows() -> None:
    """set_function's edge is inside one chunk, and keeps working."""
    with Scene() as scene:
        c = chunk.Chunk("Solo")
        a = chunk.Slider("a", default_value=1.0)
        b = chunk.Slider("b", default_value=0.0)
        c.add_param(a)
        c.add_param(b)
        scene.add_chunk(c)
        b.connect_value(a)

        a.value = 7.0

        assert b.value == pytest.approx(7.0)


def test_listening_globally_to_chunk_notices_is_refused() -> None:
    c = chunk.Chunk("Listener")
    with pytest.raises(ValueError, match="listen globally only to the stage and the host"):
        c.add_callback(lambda owner, notice, sender: None,
                       notice_alias=chunk.ChunkNoticeAlias.CHUNK_CHANGED, listen_globally=True)


def test_listening_globally_to_the_stage_is_allowed() -> None:
    c = chunk.Chunk("Listener")
    c.add_callback(lambda owner, notice, sender: None,
                   notice_alias=chunk.ChunkNoticeAlias.STAGE_CONTENTS_CHANGED, listen_globally=True)


def test_a_callback_on_another_chunks_param_is_refused() -> None:
    with Scene() as scene:
        cooks = []
        up, down = _chain(scene, cooks)
        with pytest.raises(ValueError, match="wire it in"):
            down.add_callback(lambda owner, notice, sender: None, sender=up.get_param("size"))


def test_a_callback_on_its_own_param_is_allowed() -> None:
    with Scene() as scene:
        cooks = []
        up, _down = _chain(scene, cooks)
        up.add_callback(lambda owner, notice, sender: None, sender=up.get_param("size"))
