"""The chunk menu lists its categories Examples first, then alphabetically.

A menu nests submenus in the order it first meets them, so without ordering the categories
came out in the order of each one's alphabetically-first chunk -- effectively random.
"""
from __future__ import annotations

import pytest

from usd_chunks import chunk_registry
from usd_chunks.chunk_scene import SceneSender


class _NoScene(SceneSender):
    """Enough of a scene to let the registry discover modules, and nothing more."""

    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []

    def should_install_package(self, package):
        return False


def _top_level_order(entries):
    """Top-level menu items in the order a menu would first meet them."""
    seen = []
    for entry in entries:
        if entry.namespace[0] not in seen:
            seen.append(entry.namespace[0])
    return seen


@pytest.fixture(scope="module")
def entries():
    registry = chunk_registry.ChunkRegistry()
    registry.initialize(_NoScene(), "pytest")
    found = registry.get_menu_entries(sort_result=True)
    if not found:
        pytest.skip("no chunk modules registered -- USD_CHUNK_DIRS must point at chunks/")
    return found


def test_examples_comes_first(entries):
    assert _top_level_order(entries)[0] == "Examples"


def test_the_other_categories_are_alphabetical(entries):
    categories = [e for e in entries if len(e.module.get_menu_path()) > 1]
    order = _top_level_order(categories)[1:]
    assert order == sorted(order, key=str.lower)


def test_uncategorized_chunks_come_after_every_category(entries):
    kinds = [len(e.module.get_menu_path()) > 1 for e in entries]
    assert kinds == sorted(kinds, reverse=True)


def test_direct_items_come_before_a_categorys_submenus(entries):
    usd = [e.namespace for e in entries if e.namespace[0] == "USD"]
    first_nested = next(i for i, ns in enumerate(usd) if len(ns) > 2)
    assert all(len(ns) > 2 for ns in usd[first_nested:])


def test_chunks_stay_in_name_order_within_a_category(entries):
    core = [e.namespace[-1] for e in entries if e.namespace[:-1] == ("Core",)]
    assert core == sorted(core, key=str.lower)
