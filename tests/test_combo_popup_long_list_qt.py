"""A Menu param with hundreds of items opens as one scrolling, searchable list.

A plain QMenu taller than the screen spreads into side-by-side columns wider than the
screen (Apply Schema lists every V-Ray API schema). A long menu gets a search field and a
list capped in height instead; a short one stays a plain menu.
"""

from __future__ import annotations

import pytest
from PySide6 import QtCore, QtGui, QtWidgets

from usd_chunks import Scene, chunk
from usd_chunks.frontends.qt.widgets import ChunkComboBoxWidget
from chunk_stubs import attach_stub_module

_LONG = [f"usdVRaySchema{i:03d}API" for i in range(400)]


@pytest.fixture(autouse=True)
def _scene():
    with Scene() as scene:
        yield scene


@pytest.fixture
def open_box(qapp):
    """Open a combo box over items; yields (box, popup)."""
    boxes, chunks = [], []

    def _open(items):
        c = chunk.Chunk("Menus")
        menu = chunk.Menu("pick", "Pick")
        c.add_param(menu)
        chunks.append(c)  # the widget holds its param weakly
        attach_stub_module(c, menu_items=lambda node, param_name: list(items))
        box = ChunkComboBoxWidget(menu)
        box.show()
        boxes.append(box)
        box._show_as_menu()
        qapp.processEvents()
        popup = box.findChildren(QtWidgets.QMenu)[-1]
        return box, popup

    yield _open
    for box in boxes:
        for popup in box.findChildren(QtWidgets.QMenu):
            popup.close()
        box.deleteLater()


def _list(popup) -> QtWidgets.QTreeWidget:
    return popup.findChild(QtWidgets.QTreeWidget)


def _shown(popup) -> list[str]:
    tree = _list(popup)
    return [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]


def _key(widget, key) -> None:
    QtWidgets.QApplication.sendEvent(
        widget, QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, key, QtCore.Qt.KeyboardModifier.NoModifier)
    )


def test_a_long_menu_fits_on_screen_in_one_column(open_box) -> None:
    box, popup = open_box(_LONG)

    screen = box.screen().availableGeometry()
    assert popup.height() <= screen.height()
    assert popup.width() < screen.width() / 2, "spread into columns"
    assert len(_shown(popup)) == len(_LONG), "every item reachable by scrolling"


def test_typing_filters_the_long_menu(open_box) -> None:
    _box, popup = open_box(_LONG + ["usdVRayVRayTexCheckerAPI"])

    popup.findChild(QtWidgets.QLineEdit).setText("checker")

    assert _shown(popup) == ["usdVRayVRayTexCheckerAPI"]


def test_enter_picks_the_highlighted_item(open_box) -> None:
    box, popup = open_box(_LONG)
    search = popup.findChild(QtWidgets.QLineEdit)
    search.setText("schema01")

    _key(search, QtCore.Qt.Key.Key_Down)
    _key(search, QtCore.Qt.Key.Key_Return)

    assert box.currentText() == "usdVRaySchema011API"
    assert box.param.value == "usdVRaySchema011API"


def test_clicking_an_item_picks_it(open_box) -> None:
    box, popup = open_box(_LONG)
    tree = _list(popup)

    tree.itemClicked.emit(tree.topLevelItem(5), 0)

    assert box.currentText() == _LONG[5]


def test_a_short_menu_stays_a_plain_menu(open_box) -> None:
    _box, popup = open_box(["a", "b", "c"])

    assert _list(popup) is None
    assert [a.text() for a in popup.actions()] == ["a", "b", "c"]
