"""A menu of what is on the stage is filled as it opens, by its module's menu_items hook.

So a chunk need not listen to the stage to keep a list current that nobody opens -- the
reason Skel Pose, Set Variant and Set Camera listen to nothing now (chunk_notice: chunks
speak to each other only through wires).
"""
from __future__ import annotations

from PySide6 import QtWidgets

import pytest

from usd_chunks import Scene, chunk
from usd_chunks.frontends.qt.widgets import ChunkComboBoxWidget
from chunk_stubs import attach_stub_module


@pytest.fixture(autouse=True)
def _scene():
    with Scene() as scene:
        yield scene


def _chunk_with_menu(**hooks):
    c = chunk.Chunk("Menus")
    menu = chunk.Menu("pick", "Pick")
    c.add_param(menu)
    attach_stub_module(c, **hooks)
    return c, menu


def test_opening_asks_the_module() -> None:
    asked = []

    def menu_items(node, param_name):
        asked.append((node, param_name))
        return ["/a", "/b"]

    c, menu = _chunk_with_menu(menu_items=menu_items)

    assert list(menu.menu_items) == [], "filled before anyone opened it"
    menu.refresh_items()

    assert asked == [(c, "pick")]
    assert list(menu.menu_items) == ["/a", "/b"]


def test_none_leaves_the_items_alone() -> None:
    _c, menu = _chunk_with_menu(menu_items=lambda node, param_name: None)
    menu.menu_items = ["kept"]

    menu.refresh_items()

    assert list(menu.menu_items) == ["kept"]


def test_a_failing_hook_leaves_the_items_alone() -> None:
    def menu_items(node, param_name):
        raise RuntimeError("boom")

    _c, menu = _chunk_with_menu(menu_items=menu_items)
    menu.menu_items = ["kept"]

    menu.refresh_items()

    assert list(menu.menu_items) == ["kept"]


def test_the_current_value_stays_listed() -> None:
    _c, menu = _chunk_with_menu(menu_items=lambda node, param_name: ["/a"])
    menu.value = "/gone"

    menu.refresh_items()

    assert list(menu.menu_items) == ["/a", "/gone"]


def test_without_the_hook_nothing_changes() -> None:
    _c, menu = _chunk_with_menu()
    menu.menu_items = ["fixed"]

    menu.refresh_items()

    assert list(menu.menu_items) == ["fixed"]


def test_the_qt_combo_box_fills_as_it_opens(qapp) -> None:
    _c, menu = _chunk_with_menu(menu_items=lambda node, param_name: ["/a", "/b"])
    box = ChunkComboBoxWidget(menu)
    assert box.count() == 0

    box._show_as_menu()

    assert [box.itemText(i) for i in range(box.count())] == ["/a", "/b"]
    for popup in box.findChildren(QtWidgets.QMenu):
        popup.close()
    box.deleteLater()
