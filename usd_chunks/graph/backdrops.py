"""Backdrops and sticky notes in a chunk graph, stored as USD prims. No UI toolkit here.

A backdrop is a coloured box that frames part of a graph and moves the nodes inside it.
A sticky note is a coloured box of text. Both are UsdUI.Backdrop prims that sit next to
the chunk prims under the graph's root (/chunks, or a chunkgroup's prim):

- the text (a backdrop's title, a note's body) is ui:description,
- position, size and colour use UsdUI.NodeGraphNodeAPI, the same attributes a chunk
  node uses,
- a note also has NOTE_ATTR set to true.

Rects here are (x, y, width, height) in graph coordinates: y grows downwards, the way the
Qt scene draws it. USD stores y the other way up, the same as a node's position.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from pxr import Gf, Sdf, Usd, UsdUI

from usd_chunks import chunk_naming, chunk_registry, chunk_utils

BACKDROP_TYPENAME = chunk_naming.BACKDROP_TYPENAME
NOTE_ATTR = chunk_naming.STICKY_NOTE_ATTR

#: Colours to pick from, as sRGB 0-1 floats. Muted, so they sit quietly on the dark canvas.
PALETTE: dict[str, tuple[float, float, float]] = {
    "Grey": (0.38, 0.38, 0.40),
    "Red": (0.55, 0.25, 0.25),
    "Orange": (0.62, 0.40, 0.20),
    "Yellow": (0.85, 0.75, 0.40),
    "Green": (0.30, 0.48, 0.30),
    "Teal": (0.22, 0.47, 0.47),
    "Blue": (0.27, 0.36, 0.55),
    "Purple": (0.45, 0.32, 0.55),
}
BACKDROP_COLOR = PALETTE["Blue"]
NOTE_COLOR = PALETTE["Yellow"]

#: Space left between a backdrop's edge and the nodes it is fitted around.
FIT_PADDING = 30.0
#: Height of a backdrop's title bar.
HEADER_HEIGHT = 36.0
MIN_SIZE = (120.0, 60.0)
NOTE_SIZE = (220.0, 140.0)
#: How big a node is taken to be where no UI has drawn it (a script): the Qt graph's
#: default node width, and its title bar's height.
NODE_SIZE = (250.0, 90.0)
NODE_HEADER = 48.0

Rect = tuple[float, float, float, float]

#: The attributes a backdrop is drawn from. A change to any of them redraws it.
WATCHED_ATTRS = (
    "ui:nodegraph:node:pos",
    "ui:nodegraph:node:size",
    "ui:nodegraph:node:displayColor",
    "ui:description",
    NOTE_ATTR,
)


@dataclass
class BackdropInfo:
    """What one backdrop or note prim says."""

    path: str
    rect: Rect
    color: tuple[float, float, float]
    text: str
    note: bool


def to_color(value: str | tuple[float, float, float]) -> tuple[float, float, float]:
    """A colour from a PALETTE name ("Blue", any case) or an (r, g, b) of 0-1 floats.

    Args:
        value: The name or the floats.

    Returns:
        (r, g, b), sRGB 0-1.

    Raises:
        ValueError: An unknown name, or not three numbers.
    """
    if isinstance(value, str):
        for name, rgb in PALETTE.items():
            if name.lower() == value.strip().lower():
                return rgb
        raise ValueError(f"unknown colour {value!r}; use one of {', '.join(PALETTE)} or (r, g, b)")
    rgb = tuple(float(c) for c in value)
    if len(rgb) != 3:
        raise ValueError(f"a colour is (r, g, b), not {value!r}")
    return rgb


def is_backdrop(prim: Usd.Prim) -> bool:
    """Whether the prim is a backdrop or a note.

    Args:
        prim: Any prim.

    Returns:
        True for a UsdUI.Backdrop prim.
    """
    return bool(prim) and prim.GetTypeName() == BACKDROP_TYPENAME


def is_note(prim: Usd.Prim) -> bool:
    """Whether the prim is a sticky note.

    Args:
        prim: Any prim.

    Returns:
        True for a Backdrop prim with NOTE_ATTR set.
    """
    if not is_backdrop(prim):
        return False
    attr = prim.GetAttribute(NOTE_ATTR)
    return bool(attr and attr.Get())


def list_backdrops(stage: Usd.Stage, root_path: str | Sdf.Path) -> list[Usd.Prim]:
    """The backdrop and note prims in one graph.

    Args:
        stage: The stage.
        root_path: The graph's root: /chunks, or a chunkgroup's prim.

    Returns:
        The Backdrop children of the root, or an empty list if it does not exist.
    """
    root = stage.GetPrimAtPath(Sdf.Path(str(root_path)))
    if not root or not root.IsValid():
        return []
    return [prim for prim in root.GetChildren() if is_backdrop(prim)]


def read(prim: Usd.Prim) -> BackdropInfo:
    """Read a backdrop or note prim.

    Args:
        prim: A Backdrop prim.

    Returns:
        Its rect, colour, text and kind. Missing values fall back to the defaults.
    """
    api = UsdUI.NodeGraphNodeAPI(prim)
    note = is_note(prim)
    pos = api.GetPosAttr().Get() if api.GetPosAttr() else None
    size = api.GetSizeAttr().Get() if api.GetSizeAttr() else None
    color = api.GetDisplayColorAttr().Get() if api.GetDisplayColorAttr() else None
    text = UsdUI.Backdrop(prim).GetDescriptionAttr().Get() or ""
    x, y = (float(pos[0]), -float(pos[1])) if pos is not None else (0.0, 0.0)
    w, h = (float(size[0]), float(size[1])) if size is not None else (NOTE_SIZE if note else MIN_SIZE)
    if color is None:
        color = NOTE_COLOR if note else BACKDROP_COLOR
    return BackdropInfo(str(prim.GetPath()), (x, y, w, h), tuple(float(c) for c in color), text, note)


def _writable(layer: Sdf.Layer, stage: Usd.Stage) -> bool:
    """Whether a backdrop in this layer may be edited: one the chunk system owns, or the
    one the user made the edit target. Never a chunk's own layer: the next cook clears it."""
    if chunk_registry.is_chunk_internal_layer(layer):
        return False
    return not chunk_utils.is_foreign_layer(layer, stage) or layer == stage.GetEditTarget().GetLayer()


def _home_layer(prim: Usd.Prim) -> Sdf.Layer:
    """The layer to write an edit of this prim into.

    That is the strongest local, writable layer that already has a spec for it, so one
    backdrop never splits across layers. Without one, the host's edit target.
    """
    stage = prim.GetStage()
    local = {layer.identifier for layer in stage.GetLayerStack(includeSessionLayers=True)}
    for spec in prim.GetPrimStack():
        layer = spec.layer
        if spec.path == prim.GetPath() and layer.identifier in local and _writable(layer, stage):
            return layer
    return chunk_utils.host_edit_layer(stage)


def _unique_name(root: Usd.Prim, base: str) -> str:
    taken = set(root.GetAllChildrenNames()) if root else set()
    index = 1
    while f"{base}{index}" in taken:
        index += 1
    return f"{base}{index}"


def create(
    stage: Usd.Stage,
    root_path: str | Sdf.Path,
    rect: Rect,
    note: bool = False,
    text: str = "",
    color: tuple[float, float, float] | None = None,
    layer: Sdf.Layer | None = None,
) -> Usd.Prim | None:
    """Make a backdrop or a sticky note in a graph.

    Args:
        stage: The stage.
        root_path: The graph's root. /chunks is made if it is missing; any other root
            must exist.
        rect: Where it goes, in graph coordinates.
        note: Make a sticky note instead of a backdrop.
        text: The title (backdrop) or body (note).
        color: sRGB 0-1 floats. None picks the default for the kind.
        layer: The layer to write it into. None uses the host's edit target.

    Returns:
        The new prim, or None when the root does not exist.
    """
    root_path = Sdf.Path(str(root_path))
    layer = layer or chunk_utils.host_edit_layer(stage)
    with Usd.EditContext(stage, layer):
        root = stage.GetPrimAtPath(root_path)
        if not root or not root.IsValid():
            if str(root_path) != chunk_naming.CHUNKS_ROOT:
                return None
            root = stage.DefinePrim(root_path, chunk_naming.CHUNKS_ROOT_TYPENAME)
        # Lower case: chunks are named after their module with a capital (Transform1), so a
        # chunk's default name never lands on a backdrop's prim.
        name = _unique_name(root, "note" if note else "backdrop")
        backdrop = UsdUI.Backdrop.Define(stage, root_path.AppendChild(name))
        prim = backdrop.GetPrim()
        api = UsdUI.NodeGraphNodeAPI.Apply(prim)
        x, y, w, h = rect
        api.CreatePosAttr().Set(Gf.Vec2f(x, -y))
        api.CreateSizeAttr().Set(Gf.Vec2f(w, h))
        api.CreateDisplayColorAttr().Set(Gf.Vec3f(*(color or (NOTE_COLOR if note else BACKDROP_COLOR))))
        backdrop.CreateDescriptionAttr().Set(text)
        if note:
            prim.CreateAttribute(NOTE_ATTR, Sdf.ValueTypeNames.Bool, custom=True).Set(True)
    return prim


def set_rect(prim: Usd.Prim, rect: Rect) -> None:
    """Move and resize a backdrop or note.

    Args:
        prim: A Backdrop prim.
        rect: Its new rect, in graph coordinates.
    """
    x, y, w, h = rect
    api = UsdUI.NodeGraphNodeAPI(prim)
    with Usd.EditContext(prim.GetStage(), _home_layer(prim)):
        api.CreatePosAttr().Set(Gf.Vec2f(x, -y))
        api.CreateSizeAttr().Set(Gf.Vec2f(w, h))


def set_color(prim: Usd.Prim, color: tuple[float, float, float]) -> None:
    """Change the colour of a backdrop or note.

    Args:
        prim: A Backdrop prim.
        color: sRGB 0-1 floats.
    """
    with Usd.EditContext(prim.GetStage(), _home_layer(prim)):
        UsdUI.NodeGraphNodeAPI(prim).CreateDisplayColorAttr().Set(Gf.Vec3f(*color))


def set_text(prim: Usd.Prim, text: str) -> None:
    """Change a backdrop's title or a note's text.

    Args:
        prim: A Backdrop prim.
        text: The new text.
    """
    with Usd.EditContext(prim.GetStage(), _home_layer(prim)):
        UsdUI.Backdrop(prim).CreateDescriptionAttr().Set(text)


def delete(prim: Usd.Prim) -> bool:
    """Delete a backdrop or note from every local layer it may be edited in.

    A spec in a foreign layer (a sublayered sequence file) that is not the edit target, or
    one behind a reference, stays.

    Args:
        prim: A Backdrop prim.

    Returns:
        True if the prim is gone from the stage.
    """
    stage = prim.GetStage()
    path = prim.GetPath()
    for layer in stage.GetLayerStack(includeSessionLayers=True):
        if layer.GetPrimAtPath(path) is None or not _writable(layer, stage):
            continue
        edit = Sdf.BatchNamespaceEdit()
        edit.Add(path, Sdf.Path.emptyPath)
        layer.Apply(edit)
    return not stage.GetPrimAtPath(path).IsValid()


def _center(rect: Rect) -> tuple[float, float]:
    x, y, w, h = rect
    return x + w / 2, y + h / 2


def _contains_point(rect: Rect, point: tuple[float, float]) -> bool:
    x, y, w, h = rect
    return x <= point[0] <= x + w and y <= point[1] <= y + h


def _contains_rect(outer: Rect, inner: Rect) -> bool:
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    return ox <= ix and oy <= iy and ix + iw <= ox + ow and iy + ih <= oy + oh


def node_inside(box: Rect, node_header: Rect) -> bool:
    """Whether a node belongs to a backdrop: the middle of its title bar is inside the box.

    The title bar, not the whole node: a node with its body open can be far taller than
    the box around it, and still clearly sits in it.

    Args:
        box: The backdrop's rect.
        node_header: The node's title bar rect.

    Returns:
        True if the node is inside.
    """
    return _contains_point(box, _center(node_header))


def backdrop_inside(box: Rect, other: Rect, other_is_note: bool) -> bool:
    """Whether another backdrop or note sits inside a backdrop.

    A note belongs when its middle is inside. A backdrop has to be fully inside, so two
    boxes that overlap do not drag each other along.

    Args:
        box: The backdrop's rect.
        other: The other one's rect.
        other_is_note: Whether the other one is a note.

    Returns:
        True if it is inside.
    """
    if other_is_note:
        return _contains_point(box, _center(other))
    return _contains_rect(box, other) and other != box


def fit_around(rects: Iterable[Rect], padding: float = FIT_PADDING, header: float = HEADER_HEIGHT) -> Rect | None:
    """A backdrop rect that frames all the given rects, with room for its title bar.

    Args:
        rects: The rects to frame, usually nodes.
        padding: Space between the rects and the box edge.
        header: Extra space at the top for the title bar.

    Returns:
        The rect, or None when there is nothing to frame.
    """
    rects = list(rects)
    if not rects:
        return None
    left = min(r[0] for r in rects) - padding
    top = min(r[1] for r in rects) - padding - header
    right = max(r[0] + r[2] for r in rects) + padding
    bottom = max(r[1] + r[3] for r in rects) + padding
    return left, top, max(right - left, MIN_SIZE[0]), max(bottom - top, MIN_SIZE[1])
