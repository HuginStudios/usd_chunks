"""Searching the chunk menu ranks what you typed, closest first.

A plain substring filter answered "trans" with every chunk containing it, in menu order --
so Reset Transform could sit above Transform. Ranking puts the name that starts with what
you typed first, then names with a word that does, then looser matches; a letter dropped
along the way ("tranform") still finds it.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from usd_chunks.chunk_core import SceneSender
from usd_chunks.chunk_registry import ChunkRegistry, MenuEntry, search_menu_entries


@dataclass
class _Module:
    module_name: str


def _entry(*namespace):
    return MenuEntry(module=_Module(namespace[-1]), namespace=tuple(namespace))


ENTRIES = [
    _entry("Examples", "translate_demo"),
    _entry("Core", "view"),
    _entry("Materials", "bind_material"),
    _entry("Materials", "MaterialX", "pbr", "standard_surface"),
    _entry("Modelling", "box"),
    _entry("Transform", "reset_transform"),
    _entry("Transform", "transform"),
    _entry("USD", "Properties", "property_editor"),
    _entry("Utility", "math_ops"),
    _entry("Utility", "vector_math_ops"),
]


def _labels(query, entries=ENTRIES):
    return [e.label for e in search_menu_entries(entries, query)]


def test_the_name_that_starts_with_the_query_comes_first():
    assert _labels("trans")[0] == "transform"


def test_a_shorter_prefix_match_beats_a_longer_one():
    assert _labels("trans")[:2] == ["transform", "translate_demo"]


def test_a_word_inside_the_name_ranks_after_a_prefix():
    labels = _labels("trans")
    assert labels.index("reset_transform") > labels.index("transform")


def test_the_exact_name_wins():
    assert _labels("box")[0] == "box"


def test_a_dropped_letter_still_finds_it():
    assert _labels("tranform")[0] == "transform"


def test_initials_find_a_multi_word_name():
    assert _labels("vmo")[0] == "vector_math_ops"


def test_the_category_counts_but_below_the_name():
    labels = _labels("materialx")
    assert labels == ["standard_surface"], "the MaterialX category should find its shaders"
    labels = _labels("mat")
    assert labels.index("math_ops") < labels.index("standard_surface")


def test_a_chunk_beats_a_variant_that_matches_about_as_well():
    entries = ENTRIES + [
        MenuEntry(module=_Module("reference"), namespace=("USD", "reference")),
        MenuEntry(module=_Module("mtlx_shader"), namespace=("Materials", "MaterialX", "math", "reflect"),
                  presets={"nodedef": "ND_reflect_vector3"}),
    ]
    assert _labels("ref", entries)[:2] == ["reference", "reflect"]


def test_a_variant_still_wins_when_it_is_the_closer_match():
    entries = ENTRIES + [
        MenuEntry(module=_Module("mtlx_shader"), namespace=("Materials", "MaterialX", "pbr", "layer"),
                  presets={"nodedef": "ND_layer_bsdf"}),
        MenuEntry(module=_Module("create_layer"), namespace=("USD", "Layer", "create_layer")),
    ]
    assert _labels("layer", entries)[0] == "layer"


def test_every_word_of_the_query_must_match():
    assert _labels("math vector") == ["vector_math_ops"]


def test_case_and_spaces_do_not_matter():
    assert _labels("  Standard Surface ")[0] == "standard_surface"


def test_nothing_that_does_not_match_is_listed():
    assert _labels("zzz") == []


def test_an_empty_query_keeps_menu_order():
    assert _labels("") == [e.label for e in ENTRIES]


class _NoScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []

    def should_install_package(self, package):
        return False


def test_trans_finds_transform_first_in_the_real_library():
    registry = ChunkRegistry()
    registry.initialize(_NoScene(), "pytest")
    entries = registry.get_menu_entries()
    if not entries:
        pytest.skip("no chunk modules registered -- USD_CHUNK_DIRS must point at chunks/")
    assert search_menu_entries(entries, "trans")[0].label == "transform"
