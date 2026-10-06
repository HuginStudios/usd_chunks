"""chunk.ImageMenu: a Menu with pictures, drawn as a grid of tiles in Qt.

Still a Menu everywhere else -- same value, same menu_items hook -- so a frontend that
knows nothing about pictures shows a dropdown.
"""
from __future__ import annotations

from PySide6 import QtGui

import pytest

from usd_chunks import Scene, chunk
from usd_chunks.frontends.qt.widgets import ChunkImageMenuWidget, ChunkQTWidgetFactory


@pytest.fixture(autouse=True)
def _scene():
    with Scene() as scene:
        yield scene


def _menu():
    c = chunk.Chunk("Pictures")
    menu = chunk.ImageMenu("pick", "Pick", dynamic_items=True)
    c.add_param(menu)
    return c, menu


def test_an_image_menu_is_a_menu() -> None:
    _c, menu = _menu()
    assert isinstance(menu, chunk.Menu)
    assert menu.get_type() == chunk.ChunkFlags.ParamType.STRING_MENU


def test_qt_draws_it_as_a_grid(qapp) -> None:  # noqa: ARG001
    _c, menu = _menu()
    assert isinstance(ChunkQTWidgetFactory().create_widget(menu), ChunkImageMenuWidget)


def test_a_tile_click_picks_the_item(qapp) -> None:  # noqa: ARG001
    _c, menu = _menu()
    menu.set_items(["Gold", "Oak"])
    grid = ChunkImageMenuWidget(menu)

    grid._tiles["Oak"].clicked.emit("Oak")

    assert menu.value == "Oak"


def test_the_pick_is_pinned_first_and_search_filters(qapp) -> None:  # noqa: ARG001
    _c, menu = _menu()
    menu.set_items(["Gold", "Oak", "Golden Oak"])
    menu.value = "Oak"
    grid = ChunkImageMenuWidget(menu)
    assert list(grid._tiles) == ["Oak", "Gold", "Golden Oak"]

    grid._search.setText("gold")
    grid._rebuild()

    assert list(grid._tiles) == ["Gold", "Golden Oak"]


def test_pictures_arrive_after_the_tiles(qapp, tmp_path) -> None:  # noqa: ARG001
    """Thumbnails download after the catalog: a new item_images dict redraws the tiles."""
    _c, menu = _menu()
    menu.set_items(["Gold"])
    grid = ChunkImageMenuWidget(menu)
    image = tmp_path / "gold.png"
    pixmap = QtGui.QPixmap(4, 4)
    pixmap.fill(QtGui.QColor("gold"))
    pixmap.save(str(image))

    menu.item_images = {"Gold": str(image)}

    assert grid._tiles["Gold"]._path == str(image)

