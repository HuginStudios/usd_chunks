"""How chunk nodes look in the Qt node graph: their colours, and drawing their icons.

Which icon a node shows is not decided here: chunk_icons answers that for every frontend,
from the chunk itself. This module only draws it.
"""
from __future__ import annotations

import os
from pathlib import Path

from pxr import Usd
from PySide6 import QtCore, QtGui, QtSvg

from usd_chunks import chunk_icons, chunk_resources

# Node frame.
NODE_BG = QtGui.QColor("#3a3a3a")
NODE_HEADER_BG = QtGui.QColor("#2a2a2a")
NODE_BORDER = QtGui.QColor("#555555")
NODE_SELECTED = QtGui.QColor("#f5c542")
NODE_RADIUS = 10.0
TEXT_PRIMARY = QtGui.QColor("#f2f3f5")
TEXT_SECONDARY = QtGui.QColor("#8b8f98")

# Built-in icon colours are blended this far toward this grey, so they sit quietly on the node.
ICON_MUTE_TO = QtGui.QColor("#a0a0a0")
ICON_MUTE = 0.6

# Port colours, one per kind of value: the muted set the graph has always used.
PORT_EXEC = QtGui.QColor("#999999")  # the flow wires between chunks: grey
PORT_FLOAT = QtGui.QColor("#a06845")  # muted orange
PORT_INT = QtGui.QColor("#5d7ea8")  # muted blue
PORT_BOOL = QtGui.QColor("#9e8e4a")  # muted yellow
PORT_STRING = QtGui.QColor("#6a9070")  # muted green
PORT_COLOR = QtGui.QColor("#c87060")  # muted red
PORT_VECTOR = QtGui.QColor("#9a5da8")  # muted purple

_pixmaps: dict[tuple, QtGui.QPixmap | None] = {}


def icon_color(icon: str) -> QtGui.QColor:
    """The colour a built-in icon is drawn in by default: chunk_icons.icon_color, toned down."""
    own = QtGui.QColor(chunk_icons.icon_color(icon))
    grey = ICON_MUTE_TO
    t = ICON_MUTE
    return QtGui.QColor(
        round(own.red() + (grey.red() - own.red()) * t),
        round(own.green() + (grey.green() - own.green()) * t),
        round(own.blue() + (grey.blue() - own.blue()) * t))


def node_icon(chunk, prim: Usd.Prim | None) -> tuple[str, QtGui.QColor]:
    """The icon a chunk's node shows (chunk_icons.node_icon), and the colour to draw it in.

    Args:
        chunk: The chunk the node shows.
        prim: Its chunk prim.

    Returns:
        The icon ("" for none) and its colour: the authored displayColor, else the icon's own.
    """
    icon, rgb = chunk_icons.node_icon(chunk, prim)
    return icon, QtGui.QColor.fromRgbF(*rgb) if rgb is not None else icon_color(icon)


def icon_pixmap(icon: str, color: QtGui.QColor, size: int, dpr: float = 1.0) -> QtGui.QPixmap | None:
    """An icon drawn at size x size logical pixels.

    Args:
        icon: A built-in icon name, or a path to an .svg or image file.
        color: The colour to draw a built-in (line) icon in. Ignored for other images.
        size: Width and height in logical pixels.
        dpr: The device pixel ratio of the screen it is for.

    Returns:
        The pixmap, or None for no icon, or one that can't be found or read.
    """
    if not icon:
        return None
    key = (icon, color.rgba(), size, round(dpr, 2))
    if key in _pixmaps:
        return _pixmaps[key]
    pixmap = None
    builtin = chunk_resources.get_node_icon(icon)
    if builtin is not None:
        svg = builtin.read_text(encoding="utf-8").replace("currentColor", color.name())
        pixmap = _render_svg(svg.encode("utf-8"), size, dpr)
    elif os.path.isfile(icon):
        if icon.lower().endswith(".svg"):
            pixmap = _render_svg(Path(icon).read_bytes(), size, dpr)
        else:
            image = QtGui.QPixmap(icon)
            if not image.isNull():
                pixmap = image.scaled(
                    round(size * dpr), round(size * dpr),
                    QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                    QtCore.Qt.TransformationMode.SmoothTransformation)
                pixmap.setDevicePixelRatio(dpr)
    _pixmaps[key] = pixmap
    return pixmap


def _render_svg(data: bytes, size: int, dpr: float) -> QtGui.QPixmap | None:
    renderer = QtSvg.QSvgRenderer(QtCore.QByteArray(data))
    if not renderer.isValid():
        return None
    pixmap = QtGui.QPixmap(round(size * dpr), round(size * dpr))
    pixmap.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(dpr)
    return pixmap
