"""Typing in the chunk menu swaps its category tree for one ranked list, best match first.

The ranking itself is core (see test_menu_fuzzy_search); this checks the Qt menu shows it:
the tree goes away while searching, results appear in rank order with the best one already
highlighted so Enter takes it, a broad search scrolls rather than growing, and clearing the
search brings the tree back.
"""
from __future__ import annotations

import pytest
from pxr import Usd
from PySide6 import QtCore, QtGui

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins.chunk_qt_widgets import _RESULT_DIVIDER, FilterableChunkMenu, _host_menu_highlight


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
    if not menu._results_action.isVisible():
        return []
    tree = menu.results
    return [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]


def _tree_visible(menu):
    return [a.isVisible() for a in menu._tree_actions]


def _key(menu, key):
    event = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, key, QtCore.Qt.KeyboardModifier.NoModifier)
    return menu.eventFilter(menu.filter_widget, event)


def test_the_tree_shows_until_you_type(menu):
    assert all(_tree_visible(menu))
    assert _results(menu) == []


def test_typing_lists_the_closest_first(menu):
    menu.filter_widget.setText("trans")
    assert _results(menu)[0] == "transform"
    assert not any(_tree_visible(menu)), "the tree hides while a search is showing"


def test_the_best_match_is_already_highlighted(menu):
    menu.filter_widget.setText("trans")
    assert menu.results.current_entry().label == "transform"


def test_enter_takes_the_highlighted_result(menu):
    menu.filter_widget.setText("trans")
    assert _key(menu, QtCore.Qt.Key.Key_Return)
    assert menu._picked.label == "transform"


def test_arrow_keys_move_through_the_results(menu):
    menu.filter_widget.setText("trans")
    second = _results(menu)[1]
    _key(menu, QtCore.Qt.Key.Key_Down)
    assert menu.results.current_entry().label == second
    _key(menu, QtCore.Qt.Key.Key_Up)
    _key(menu, QtCore.Qt.Key.Key_Up)
    assert menu.results.current_entry().label == "transform", "stops at the top"


def test_clicking_a_result_takes_it(menu):
    menu.filter_widget.setText("trans")
    item = menu.results.topLevelItem(1)
    menu.results.itemClicked.emit(item, 0)
    assert menu._picked.label == item.text(0)


def test_results_follow_the_query_as_it_changes(menu):
    menu.filter_widget.setText("trans")
    menu.filter_widget.setText("scat")
    results = _results(menu)
    assert results and all("scat" in r for r in results)
    assert "transform" not in results


def test_a_broad_search_scrolls_rather_than_growing(menu):
    menu.filter_widget.setText("materialx")
    results = _results(menu)
    assert FilterableChunkMenu.VISIBLE_RESULTS < len(results) <= FilterableChunkMenu.MAX_RESULTS
    row = menu.results.sizeHintForRow(0)
    assert menu.results.height() <= row * FilterableChunkMenu.VISIBLE_RESULTS + 2
    for _ in results:
        _key(menu, QtCore.Qt.Key.Key_PageDown)
    assert menu.results.current_entry().label == results[-1], "the end of the list is reachable"


def test_a_short_list_is_only_as_tall_as_its_rows(menu):
    menu.filter_widget.setText("scat")
    count = len(_results(menu))
    assert count < FilterableChunkMenu.VISIBLE_RESULTS
    assert menu.results.height() == menu.results.sizeHintForRow(0) * count + 2


def test_the_category_sits_in_its_own_column_after_a_divider(menu):
    menu.filter_widget.setText("transform")
    item = menu.results.topLevelItem(0)
    assert (item.text(0), item.text(1)) == ("transform", f"{_RESULT_DIVIDER}Transform")


def test_the_list_is_as_wide_as_its_columns_and_stays_so(menu):
    menu.filter_widget.setText("standard_surface_to")
    menu.filter_widget.setText("scat")
    first = menu.results.width()
    for _ in range(3):
        menu.filter_widget.setText("sca")
        menu.filter_widget.setText("scat")
    assert menu.results.width() == first, "searching again must not widen it"
    columns = menu.results.columnWidth(0) + menu.results.columnWidth(1)
    assert first == columns + 4


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


def test_the_highlight_matches_the_host_menu(qapp):
    """usdview colours menus in its stylesheet, not its palette."""
    previous = qapp.styleSheet()
    qapp.setStyleSheet(
        "QMenu::item:selected {\n    background: rgb(190, 156, 85);\n    color: rgb(54, 54, 54);\n}"
    )
    try:
        assert _host_menu_highlight() == ("rgb(190, 156, 85)", "rgb(54, 54, 54)")
    finally:
        qapp.setStyleSheet(previous)


def test_without_a_host_rule_the_palette_is_used(qapp):
    previous = qapp.styleSheet()
    qapp.setStyleSheet("")
    try:
        assert _host_menu_highlight() == ("palette(highlight)", "palette(highlighted-text)")
    finally:
        qapp.setStyleSheet(previous)
