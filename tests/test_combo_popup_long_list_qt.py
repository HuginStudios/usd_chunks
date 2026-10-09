"""A Menu param with hundreds of items opens as one scrolling, searchable list.

A plain QMenu taller than the screen spreads into side-by-side columns wider than the
screen (Apply Schema lists every V-Ray API schema). A long menu gets a search field and a
list capped in height instead; a short one stays a plain menu. Either way it opens as a
window of its own, also from a node in the graph, so typing and the wheel reach it.
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
    """Open a combo box over items, on its own or inside a graph; yields (box, popup)."""
    boxes, keep = [], []

    def _open(items, in_graph=False):
        c = chunk.Chunk("Menus")
        menu = chunk.Menu("pick", "Pick")
        c.add_param(menu)
        attach_stub_module(c, menu_items=lambda node, param_name: list(items))
        box = ChunkComboBoxWidget(menu)
        keep.append(c)  # the widget holds its param weakly
        if in_graph:
            # Like a node: the combo inside a panel inside a proxy, in a zoomed, moved view.
            panel = QtWidgets.QWidget()
            QtWidgets.QVBoxLayout(panel).addWidget(box)
            scene = QtWidgets.QGraphicsScene()
            scene.addWidget(panel).setPos(40, 30)
            view = QtWidgets.QGraphicsView(scene)
            view.setGeometry(150, 120, 500, 400)
            view.scale(1.5, 1.5)
            view.show()
            keep.extend([scene, view])
        else:
            box.show()
        boxes.append(box)
        qapp.processEvents()
        box._show_as_menu()
        qapp.processEvents()
        return box, box._popup

    yield _open
    for box in boxes:
        box._popup.close()
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


@pytest.mark.parametrize("items", [["a", "b", "c"], _LONG], ids=["short", "long"])
def test_in_the_graph_the_menu_is_a_window_of_its_own(open_box, items) -> None:
    _box, popup = open_box(items, in_graph=True)

    assert popup.graphicsProxyWidget() is None, "in the graph scene: no typing, the graph takes the wheel"
    assert popup.isWindow()


def test_in_the_graph_the_menu_opens_under_the_combo(open_box) -> None:
    box, popup = open_box(["a", "b", "c"], in_graph=True)

    panel = box.window()
    proxy = panel.graphicsProxyWidget()
    view = proxy.scene().views()[0]
    bottom_left = proxy.mapToScene(QtCore.QPointF(box.mapTo(panel, QtCore.QPoint(0, box.height()))))
    assert popup.pos() == view.viewport().mapToGlobal(view.mapFromScene(bottom_left))
