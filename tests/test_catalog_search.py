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


@pytest.mark.parametrize("query, wanted", [
    ("tint", "colorcorrect"), ("tint material", "colorcorrect"), ("brighten texture", "colorcorrect"),
    ("colour correction", "colorcorrect"), ("color correction", "colorcorrect"), ("desaturate", "hsvadjust"),
    ("weave", "noise3d"), ("semantic label", "semantics"),
])
def test_materialx_nodes_are_found_by_what_they_do(registry, query, wanted) -> None:
    """A MaterialX node's definition carries no documentation, so its entry used to say only
    'mtlx_shader with nodedef=...': "tint" found nothing about colour, and only its name found it."""
    assert wanted in _names(registry, query)[:3]


def test_a_materialx_entry_says_what_it_does_and_which_node_it_is(registry) -> None:
    entry = chunk_catalog.find(registry, "colorcorrect")
    assert entry.summary.startswith("Colour correction")
    assert "nodedef='ND_colorcorrect_color3'" in entry.summary


def test_british_and_american_spellings_find_each_other() -> None:
    rows = [Entry("paint", "paint", "", "Sets a colour."), Entry("dye", "dye", "", "Sets a color.")]
    assert {e.name for e in search(rows, {}, "color")} == {"paint", "dye"}
    assert {e.name for e in search(rows, {}, "colour")} == {"paint", "dye"}


def test_an_entry_whose_label_is_shared_is_listed_under_its_module(registry) -> None:
    """MaterialX's colour mix and its shading-layer mix are both "mix", so neither is a name
    scene.create knows; they used to be left out, and Mix and Multiply couldn't be found at all."""
    mixes = [e for e in chunk_catalog.entries(registry) if e.presets and e.presets.get("nodedef") == "ND_mix_color3"]
    assert [(e.name, e.module) for e in mixes] == [("mtlx_shader", "mtlx_shader")]
    assert "nodedef='ND_mix_color3'" in mixes[0].summary


@pytest.mark.parametrize("query, nodedef", [
    ("mix two colours", "ND_mix_color3"), ("multiply colour", "ND_multiply_color3"),
    ("position in object space", "ND_position_vector3"),
])
def test_shared_and_input_nodes_are_found_by_task(registry, query, nodedef) -> None:
    found = [e.presets.get("nodedef") for e in chunk_catalog.entries(registry, query)[:3] if e.presets]
    assert nodedef in found


@pytest.mark.parametrize("query, wanted", [
    ("hide part of an asset", "mute_prim"), ("set an attribute", "property_editor"),
    ("change an input on a shader", "property_editor"),
])
def test_task_words_find_the_usd_editing_chunks(registry, query, wanted) -> None:
    assert wanted in _names(registry, query)[:3]
