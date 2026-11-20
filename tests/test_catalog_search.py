"""`usdchunks list <words>` (and the MCP's find_chunks) finds chunks by task words, best first.

It matched the query anywhere in a description: "terrain" and "rain" listed every fence
preset, because Fence follows terrain height, while "trees" found nothing, and an agent took
seventeen searches to map the simulation chunks.
"""
from __future__ import annotations

import pytest

from usd_chunks import Scene, chunk_catalog, chunk_registry
from usd_chunks.chunk_catalog import Entry, search


@pytest.fixture(scope="module")
def registry():
    with Scene():
        yield chunk_registry.ChunkRegistry()


def _names(registry, query):
    return [e.name for e in chunk_catalog.entries(registry, query)]


def test_a_word_matches_whole_words_not_inside_them(registry) -> None:
    assert "fence" not in _names(registry, "rain")
    assert _names(registry, "rain")[0] == "surface_flow"


def test_a_menu_entry_is_found_by_its_own_name_not_its_modules_text(registry) -> None:
    assert _names(registry, "terrain") == ["fence"]
    assert {"fence", "picket_fence", "ranch_fence"} <= set(_names(registry, "fence"))


@pytest.mark.parametrize("query, first", [
    ("trees", "scatter_points"), ("grass", "scatter_points"), ("scatter rocks", "scatter_points"),
    ("house", "house"), ("sand", "particle_solver"), ("keyframe", "inline_usd"),
])
def test_task_words_find_the_chunk(registry, query, first) -> None:
    assert _names(registry, query)[0] == first


def test_every_word_must_match_while_some_row_matches_all() -> None:
    rows = [Entry("a", "a", "", "rocks on a slope"), Entry("b", "b", "", "rocks"), Entry("c", "c", "", "slope")]
    assert [e.name for e in search(rows, {}, "rocks slope")] == ["a"]
    assert [e.name for e in search(rows, {}, "rocks cliff")] == ["a", "b"]  # none has both: any of them


def test_the_name_counts_more_than_the_description() -> None:
    rows = [Entry("noise", "noise", "", "a field"), Entry("other", "other", "", "uses noise")]
    assert [e.name for e in search(rows, {}, "noise")] == ["noise", "other"]


def test_cube_still_lists_mesh(registry) -> None:
    assert {"cube", "usd_cube", "mesh", "shape"} <= set(_names(registry, "cube"))
