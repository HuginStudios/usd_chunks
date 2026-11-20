"""The node graph's Styles menu: a picture of each style, put on the prim picked in the viewport.

A style is a saved network that works on whatever object it is put on (usd_chunks/styles.py).
Every style is already an entry in the Tab menu, under Styles; this menu shows the same
entries as pictures, which is how a look is chosen. Clicking one hands its MenuEntry to the
graph view, which creates it, wires it onto the end of the chain, and shows it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from usd_chunks import chunk_registry, styles

#: The edge of a style's picture, in pixels.
TILE = 96

#: Tiles per row.
COLUMNS = 3


def style_entries() -> list:
    """The Tab menu's style entries, each with its style: [(MenuEntry, Style or None)]."""
    by_file = {str(Path(s.path).resolve()): s for s in styles.find_styles()}
    out = []
    for entry in chunk_registry.ChunkRegistry().get_menu_entries():
        if entry.namespace[:1] != (styles.MENU,):
            continue
        file_path = (entry.presets or {}).get("file_path") or ""
        out.append((entry, by_file.get(str(Path(file_path).resolve())) if file_path else None))
    return out


def picked_prim() -> str | None:
    """The first prim selected in the viewport, as the style entries will read it."""
    return styles.selected_root(None)


class StyleGrid(QtWidgets.QWidget):
    """The styles as a grid of picture buttons, greyed out until a prim is picked."""

    def __init__(self, entries: list, target: str | None, apply: Callable, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 8)
        layout.setSpacing(6)
        hint = QtWidgets.QLabel(
            f"Put a style on <b>{target}</b>" if target else "Select an object in the viewport, then pick a style.")
        hint.setStyleSheet("color: #c8c8c8;")
        layout.addWidget(hint)
        grid = QtWidgets.QGridLayout()
        grid.setSpacing(6)
        layout.addLayout(grid)
        self.buttons: list[QtWidgets.QToolButton] = []
        for i, (entry, style) in enumerate(entries):
            button = QtWidgets.QToolButton(self)
            button.setText(entry.label)
            button.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            button.setIconSize(QtCore.QSize(TILE, TILE))
            button.setAutoRaise(True)
            if style is not None and style.thumbnail is not None:
                button.setIcon(QtGui.QIcon(str(style.thumbnail)))
            button.setToolTip(style.summary if style is not None and style.summary else entry.label)
            button.setEnabled(target is not None)
            button.clicked.connect(lambda _=False, e=entry: apply(e))
            grid.addWidget(button, i // COLUMNS, i % COLUMNS)
            self.buttons.append(button)


def fill_styles_menu(menu: QtWidgets.QMenu, apply: Callable) -> None:
    """Rebuild menu with the styles found now, for the prim picked now.

    Args:
        menu: The Styles menu, filled each time it opens.
        apply: Called with the chosen MenuEntry, after the menu has closed.
    """
    menu.clear()
    entries = style_entries()
    if not entries:
        none = menu.addAction(f"No styles found: put .usda networks in a {styles.STYLE_FOLDER} folder "
                              "beside your chunks")
        none.setEnabled(False)
        return

    def chosen(entry) -> None:
        menu.close()
        # After the menu has gone: creating the chunk rebuilds the graph under it.
        QtCore.QTimer.singleShot(0, menu, lambda: apply(entry))

    action = QtWidgets.QWidgetAction(menu)
    action.setDefaultWidget(StyleGrid(entries, picked_prim(), chosen, menu))
    menu.addAction(action)
