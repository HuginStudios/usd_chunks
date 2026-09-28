"""The rule behind C, as the engine states it for every frontend.

Qt and Blender each carried their own copy of it; Flet and ImGui had none, which is what
the frontend contract caught. These pin the rule itself, with no toolkit in the way.
"""
from __future__ import annotations

import pytest

from usd_chunks import Scene
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.graph import engine as chunk_graph

COLLAPSED = ChunkFlags.State.COLLAPSED


def _collapsed(chunks):
    return [c.has_state(COLLAPSED) for c in chunks]


@pytest.fixture
def scene():
    with Scene(host_name="pytest") as scene:
        yield scene


@pytest.fixture
def chunks(scene):
    return [scene.add("transform") for _ in range(3)]


def test_nothing_selected_collapses_everything(chunks):
    assert chunk_graph.collapse_or_expand(chunks, []) == 3
    assert _collapsed(chunks) == [True, True, True]


def test_a_selection_is_collapsed_alone(chunks):
    chunk_graph.collapse_or_expand(chunks, chunks[:1])
    assert _collapsed(chunks) == [True, False, False]


def test_a_mixed_set_collapses_rather_than_inverting(chunks):
    chunks[0].set_state(COLLAPSED)
    chunk_graph.collapse_or_expand(chunks, [])
    assert _collapsed(chunks) == [True, True, True]


def test_only_a_set_already_all_collapsed_expands(chunks):
    for c in chunks:
        c.set_state(COLLAPSED)
    chunk_graph.collapse_or_expand(chunks, [])
    assert _collapsed(chunks) == [False, False, False]


def test_a_chunkgroup_and_a_view_are_left_alone(scene, chunks):
    """A collapsed chunkgroup hides the control that expands it, and a view has no body --
    and is often what is selected, so counting it would flip the direction."""
    group = scene.add("chunkgroup")
    view = scene.add(chunk_graph.VIEW_MODULE)
    count = chunk_graph.collapse_or_expand([*chunks, group, view], [])
    assert count == 3
    assert not group.has_state(COLLAPSED)
    assert _collapsed(chunks) == [True, True, True]
