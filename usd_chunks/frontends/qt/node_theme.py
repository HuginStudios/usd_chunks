"""How chunk nodes look in the node graph: their colours, and the icon a node or group shows.

An icon is a name from the built-in set (usd_chunks/resources/icons/node/<name>.svg, line
icons drawn in one colour and tinted when drawn) or a path to an image file.

Where a node's icon comes from, first match wins:

1. The chunk prim's UsdUINodeGraphNodeAPI icon (ui:nodegraph:node:icon), when authored.
2. The chunk's own `icon` (ChunkCommon.icon), when a chunk's main() sets one.
3. A built-in default for the chunk's type, then for its menu category.

Its colour is the prim's UsdUINodeGraphNodeAPI displayColor when authored, else the icon's
own colour. A group's icon is its `icon` when set, else one picked from words in its name or
label (Translate, Rotate, Visibility...); a group that matches nothing shows no icon.
"""
from __future__ import annotations

import os
from pathlib import Path

from pxr import Usd, UsdUI
from PySide6 import QtCore, QtGui, QtSvg

from usd_chunks import chunk_resources

# Node frame.
NODE_BG = QtGui.QColor("#2a2b30")
NODE_HEADER_BG = QtGui.QColor("#1b1c20")
NODE_BORDER = QtGui.QColor("#3d3f46")
NODE_SELECTED = QtGui.QColor("#f5c542")
NODE_RADIUS = 10.0
TEXT_PRIMARY = QtGui.QColor("#f2f3f5")
TEXT_SECONDARY = QtGui.QColor("#8b8f98")


# Port colours, one per kind of value: the muted set the graph has always used.
PORT_EXEC = QtGui.QColor("#999999")  # the flow wires between chunks: grey
PORT_FLOAT = QtGui.QColor("#a06845")  # muted orange
PORT_INT = QtGui.QColor("#5d7ea8")  # muted blue
PORT_BOOL = QtGui.QColor("#9e8e4a")  # muted yellow
PORT_STRING = QtGui.QColor("#6a9070")  # muted green
PORT_COLOR = QtGui.QColor("#c87060")  # muted red
PORT_VECTOR = QtGui.QColor("#9a5da8")  # muted purple

# Each built-in icon's own colour, used unless the prim authors a displayColor.
_ICON_COLORS = {
    "cube": "#4ade80", "sun": "#facc15", "move": "#22d3ee", "rotate": "#60a5fa",
    "scale": "#22d3ee", "pivot": "#3b82f6", "eye": "#60a5fa", "link": "#2dd4bf",
    "ruler": "#c084fc", "shuffle": "#c084fc", "bars": "#c084fc", "posts": "#c084fc",
    "rails": "#c084fc", "camera": "#60a5fa", "sphere": "#f472b6", "palette": "#a855f7",
    "layers": "#818cf8", "waves": "#38bdf8", "output": "#38bdf8", "view": "#fb923c",
    "folder": "#fbbf24", "code": "#94a3b8", "sliders": "#94a3b8", "image": "#f472b6",
    "merge": "#94a3b8", "scatter": "#4ade80", "sprout": "#4ade80", "home": "#fb923c",
    "sparkles": "#fb923c", "zap": "#f87171", "bone": "#e5e7eb", "film": "#f472b6",
    "tag": "#fbbf24", "sigma": "#a78bfa", "dice": "#a78bfa", "database": "#818cf8",
    "download": "#38bdf8", "curve": "#4ade80", "grid": "#818cf8", "brush": "#4ade80",
    "box_select": "#818cf8",
}
_DEFAULT_ICON_COLOR = "#94a3b8"

# A chunk type's icon, by the type name the prim records (info:chunkType).
_TYPE_ICONS = {
    "light": "sun", "transform": "move", "reset_transform": "move", "camera": "camera",
    "set_camera": "camera", "fence": "posts", "house": "home", "vine": "sprout",
    "scatter_points": "scatter", "scatter_brush": "brush", "emit": "sparkles",
    "particle_solver": "waves", "surface_flow": "waves", "rbd_solver": "cube",
    "rbd_breakage": "zap", "fracture": "zap", "physics_drive": "waves", "trails": "curve",
    "add_curves": "curve", "curves_to_mesh": "curve", "noise_field": "waves",
    "proximity_field": "waves", "mesh": "cube", "prim": "cube", "duplicate_prim": "cube",
    "render": "film", "render_settings": "film", "render_pass": "film",
    "render_product": "film", "render_var": "film", "uv_texture": "image",
    "uv_checker": "image", "uv_transform": "image", "uv_unwrap": "image",
    "bake_textures": "image", "preview_surface": "sphere", "mtlx_shader": "sphere",
    "bind_material": "sphere", "mdl_to_mtlx": "sphere", "AMDMaterialXLibrary": "sphere",
    "primvar_reader": "sphere", "layer": "layers", "sublayer": "layers",
    "mute_layer": "layers", "edit_target": "layers", "export_usd": "download",
    "export_chunkgroup": "download", "reference": "link", "payload": "link",
    "value_clips": "film", "inline_usd": "code", "script_runner": "code",
    "math_ops": "sigma", "vector_math_ops": "sigma", "random_value": "dice",
    "semantics": "tag", "get_metadata": "tag", "set_metadata": "tag",
    "get_property": "tag", "property_editor": "tag", "apply_schema": "tag",
    "collection": "grid", "variant": "merge", "set_variant": "merge", "switch": "merge",
    "stage_expression_switch": "merge", "stage_expression": "code", "merge": "merge",
    "output": "output", "view": "view", "chunkgroup": "folder", "wedge": "shuffle",
    "poly_reduce": "cube", "remesh_proxy": "cube", "cad_import": "cube",
    "blender_import": "cube", "urdf_import": "bone", "skel_pose": "bone",
    "pose_articulation": "bone", "kitti_writer": "database", "set_display_color": "palette",
    "mute_prim": "eye", "prune_prim": "eye", "stage_settings": "sliders",
    "override_chunk_prim": "tag",
}
# A menu category's icon, for a type with none of its own. Matched on the top level.
_CATEGORY_ICONS = {
    "modelling": "cube", "dynamics": "waves", "transform": "move", "utility": "sliders",
    "rigs": "bone", "usd": "layers", "core": "grid", "materials": "sphere",
    "examples": "code", "user": "sliders",
}
# Words that pick a group's icon, checked in order against its name and label.
_GROUP_WORDS = (
    ("translate", "move"), ("rotat", "rotate"), ("scale", "scale"), ("pivot", "pivot"),
    ("visib", "eye"), ("link", "link"), ("color", "palette"), ("colour", "palette"),
    ("light", "sun"), ("lens", "camera"), ("sensor", "camera"), ("focus", "camera"),
    ("clipping", "camera"), ("material", "sphere"), ("texture", "image"), ("map", "image"),
    ("post", "posts"), ("rail", "rails"), ("picket", "bars"), ("run", "ruler"),
    ("dimension", "ruler"), ("footprint", "ruler"), ("variation", "shuffle"),
    ("random", "dice"), ("roof", "home"), ("window", "home"), ("chimney", "home"),
    ("emission", "sparkles"), ("emit", "sparkles"), ("collision", "zap"),
    ("breakage", "zap"), ("simulation", "waves"), ("physics", "waves"), ("force", "waves"),
    ("domain", "grid"), ("region", "grid"), ("distribution", "scatter"),
    ("grow", "sprout"), ("leaf", "sprout"), ("prototype", "cube"), ("shape", "curve"),
    ("output", "download"), ("export", "download"), ("quality", "sliders"),
    ("advanced", "sliders"), ("settings", "sliders"), ("option", "sliders"), ("inputs", "sliders"),
    ("display", "eye"), ("preview", "eye"), ("viewport", "eye"), ("brush", "brush"),
    ("maps", "image"), ("uv", "image"), ("variable", "code"), ("edit", "tag"),
)

_SVG_DIR = chunk_resources.get_icons_path() / "node"
_pixmaps: dict[tuple, QtGui.QPixmap | None] = {}


def icon_color(icon: str) -> QtGui.QColor:
    """The colour a built-in icon is drawn in by default.

    Args:
        icon: An icon name or image path.

    Returns:
        The icon's colour, or a neutral grey for an unknown one.
    """
    return QtGui.QColor(_ICON_COLORS.get(icon, _DEFAULT_ICON_COLOR))


def icon_pixmap(icon: str, color: QtGui.QColor, size: int, dpr: float = 1.0) -> QtGui.QPixmap | None:
    """An icon drawn at size x size logical pixels.

    Args:
        icon: A built-in icon name, or a path to an .svg or image file.
        color: The colour to draw a built-in (line) icon in. Ignored for other images.
        size: Width and height in logical pixels.
        dpr: The device pixel ratio of the screen it is for.

    Returns:
        The pixmap, or None when the icon can't be found or read.
    """
    key = (icon, color.rgba(), size, round(dpr, 2))
    if key in _pixmaps:
        return _pixmaps[key]
    pixmap = None
    builtin = _SVG_DIR / f"{icon}.svg"
    if builtin.is_file():
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


def node_icon(chunk, prim: Usd.Prim | None, chunk_type: str) -> tuple[str, QtGui.QColor]:
    """The icon and colour a chunk's node shows in its header.

    Args:
        chunk: The chunk the node shows.
        prim: Its chunk prim, read for an authored UsdUI icon and displayColor.
        chunk_type: The chunk's type name (info:chunkType).

    Returns:
        The icon (a built-in name or an image path) and the colour to draw it in.
    """
    icon = ""
    color = None
    if prim is not None and prim.IsValid():
        api = UsdUI.NodeGraphNodeAPI(prim)
        icon_attr = api.GetIconAttr()
        if icon_attr and icon_attr.HasAuthoredValue():
            asset = icon_attr.Get()
            icon = (asset.resolvedPath or asset.path) if asset else ""
        color_attr = api.GetDisplayColorAttr()
        if color_attr and color_attr.HasAuthoredValue():
            c = color_attr.Get()
            color = QtGui.QColor.fromRgbF(*(min(max(float(v), 0.0), 1.0) for v in c))
    if not icon:
        icon = getattr(chunk, "icon", "") or ""
    if not icon:
        icon = _TYPE_ICONS.get(chunk_type, "")
    if not icon:
        icon = _CATEGORY_ICONS.get(_category(chunk), "cube")
    return icon, color if color is not None else icon_color(icon)


def group_icon(group) -> str:
    """The icon a group's header shows, or "" for none.

    Args:
        group: The group.

    Returns:
        A built-in icon name or an image path, or "".
    """
    icon = getattr(group, "icon", "") or ""
    if icon:
        return icon
    try:
        words = f"{group.name} {group.get_label()}".lower()
    except Exception:  # noqa: BLE001 - a header must still draw for an odd group
        return ""
    for word, name in _GROUP_WORDS:
        if word in words:
            return name
    return ""


def _category(chunk) -> str:
    try:
        config = chunk.get_config()
    except Exception:  # noqa: BLE001 - a chunk mid-teardown still paints
        return ""
    category = getattr(config, "category", "") or ""
    # A StandardCategory is a str Enum, whose str() is its member name, not its value.
    category = str(getattr(category, "value", category))
    return category.split("/")[0].strip().lower()
