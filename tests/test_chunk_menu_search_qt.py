"""Typing in the chunk menu swaps its category tree for one ranked list, best match first.

The ranking itself is core (see test_menu_fuzzy_search); this checks the Qt menu shows it:
the tree goes away while searching, results appear in rank order with the best one already
highlighted so Enter takes it, and clearing the search brings the tree back.
"""
from __future__ import annotations

import pytest
from pxr import Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins.chunk_qt_widgets import FilterableChunkMenu


class _Scene(SceneSender):
    def __init__(self):
        super().__init__()
        self._stage = Usd.Stage.CreateInMemory()

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []

    def should_install_package(self, package):
        return False


@pytest.fixture
def menu(qapp):
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(_Scene(), "pytest")
    if not reg.get_menu_entries():
        pytest.skip("no chunk modules registered -- USD_CHUNK_DIRS must point at scripts/")
    m = FilterableChunkMenu()
    m.exec_ = lambda _pos: None  # build it without popping it up
    m.select_module(None)
    yield m
    m.deleteLater()


def _results(menu):
    flat = set(menu._flat_actions.values())
    return [a.text().split("\t")[0] for a in menu.actions() if a in flat and a.isVisible()]


def _tree_visible(menu):
    return [a.isVisible() for a in menu._tree_actions]


def test_the_tree_shows_until_you_type(menu):
    assert all(_tree_visible(menu))
    assert _results(menu) == []


def test_typing_lists_the_closest_first(menu):
    menu.filter_widget.setText("trans")
    results = _results(menu)
    assert results[0] == "transform"
    assert not any(_tree_visible(menu)), "the tree hides while a search is showing"


def test_the_best_match_is_already_highlighted(menu):
    menu.filter_widget.setText("trans")
    active = menu.activeAction()
    assert active is not None and active.text().split("\t")[0] == "transform"
    assert active.data().label == "transform", "Enter must create what is highlighted"


def test_results_follow_the_query_as_it_changes(menu):
    menu.filter_widget.setText("trans")
    menu.filter_widget.setText("scat")
    results = _results(menu)
    assert results and all("scat" in r for r in results)
    assert "transform" not in results


def test_the_list_is_capped(menu):
    menu.filter_widget.setText("a")
    assert len(_results(menu)) == FilterableChunkMenu.MAX_RESULTS


def test_no_match_says_so(menu):
    menu.filter_widget.setText("zzzzqq")
    assert _results(menu) == []
    assert menu._no_results.isVisible()


def test_clearing_the_search_brings_the_tree_back(menu):
    menu.filter_widget.setText("trans")
    menu.filter_widget.setText("")
    assert all(_tree_visible(menu))
    assert _results(menu) == []
    assert not menu._no_results.isVisible()
