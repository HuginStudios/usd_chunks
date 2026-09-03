"""
Node graph view for Chunk prims.

Reads layout from UsdUINodeGraphNodeAPI; writes position back on drag.
Selection syncs with ChunkWidget via STATE_CHANGED notice.
Edges follow inputs:chunk connections between prims.
"""

from __future__ import annotations

import os
import re
import time
import weakref
from typing import Iterable, Optional

import shiboken6
from pxr import Gf, Sdf, Usd, UsdUI
from PySide6 import QtCore, QtGui, QtWidgets

from usd_chunks import chunk as _chunk
from usd_chunks import chunk_core as core
from usd_chunks import chunk_registry
from usd_chunks.plugins import chunk_graph_layout
from usd_chunks import chunk_utils
from usd_chunks.plugins import chunk_usd
from usd_chunks.chunk import Checkbox, ColorPicker, FileBrowser, LineEdit, Menu, Slider, Spinbox, TextEdit
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_notice import ChunkNoticeAlias, NoticeConnection
from usd_chunks.plugins.chunk_qt_widgets import ContainerWidget, ChunkWidget, FilterableChunkMenu, LabeledWidget, apply_chunk_stylesheet, ViewSlotState

_PORT_RADIUS = 5
_PORT_SNAP_PIXELS = 20  # on-screen click-tolerance radius for dropping a dragged wire near a port
_NODE_WIDTH = 220
_NODE_HEADER_H = 40

# A node stops drawing its embedded ChunkWidget -- showing only the box and header that
# ChunkNodeItem paints itself -- when either of these says so. The node keeps its size
# either way, so layout, edges and framing are unaffected.
#
# Zoom alone decides this: below this scale a body's text is unreadable, so it is replaced
# by the cheap flat form, and above it every body stays live no matter how many are on
# screen. There is deliberately no cap on the number drawn.
#
# Culling by viewport position as well was tried and reverted. A body reports no height
# until it has been through one visible layout cycle, and a node's height is what its ports,
# edges, framing and auto-layout all measure from. Per-node culling hides bodies constantly
# and early -- proxies are built in deferred batches while the view is still moving -- and a
# body hidden before it was ever laid out freezes the node at its bare header height.
# sizeHint(), adjustSize() and an explicit layout activate() all return 0 for such a widget,
# so it cannot be measured ahead of time; making this work needs a real "has been laid out
# at least once" gate on the node, not just careful ordering.
_LOD_BODY_MIN_SCALE = 0.25

# Quiet period before ports are re-placed after the node's geometry changes. Placement
# re-measures every param widget, so it must not run per animation frame.
_PORT_REBUILD_MS = 60
# Quiet period with no height change ANYWHERE IN THE GRAPH before heights count as settled.
#
# A body is re-measured several times while it lays out, and an early pass can report a
# fraction of the final height -- one node measured 64px of an eventual 885px -- so
# "non-zero" and "taller than the header" are both useless as readiness tests. Only "it
# stopped changing" works.
#
# And it has to be asked of the whole scene, not one node. Measurements are event-driven, so
# a node that has simply not been re-measured yet is indistinguishable from one that has
# finished; during a load, gaps between a node's own measurements routinely exceed this
# window, and per-node settling duly marked nodes settled at partial heights. While any node
# is still resizing, none of them can be trusted.
_HEIGHT_SETTLE_MS = 250
_DOT_R = 10  # dot node circle radius

# Merge node slot ports: vertical gap between stacked exec inputs, and the y of the first
# one. The first slot sits at the header centre so it lines up with the output port, which
# keeps a one-input merge looking like every other node.
_MERGE_PORT_SPACING = 20
_MERGE_PORT_TOP = _NODE_HEADER_H // 2

# Shake-to-disconnect tuning: a fast back-and-forth drag disconnects the node.
_SHAKE_WINDOW = 0.4  # seconds of drag samples to look at
_SHAKE_MIN_STEP = 5.0  # scene px; smaller moves are jitter, ignored
_SHAKE_MIN_TRAVEL = 140.0  # total scene px travelled in the window
_SHAKE_REVERSALS = 4  # horizontal direction changes needed to fire

# A header double-click can be delivered to a node twice for one real user gesture (seen
# live: two full STATE_CHANGED solves from a single double-click, toggling COLLAPSED on
# then immediately off again -- net no-op, so the node looks permanently stuck). Debounced
# rather than fixed at the Qt event-delivery layer since that redelivery's exact cause
# wasn't reproducible outside a live session; this bounds the damage regardless of cause.
_COLLAPSE_TOGGLE_DEBOUNCE = 0.2  # seconds


_COL_NODE_BG = QtGui.QColor("#3a3a3a")
_COL_NODE_HEADER = QtGui.QColor("#2a2a2a")
_COL_NODE_BORDER = QtGui.QColor("#555555")
_COL_NODE_SELECTED = QtGui.QColor("#cab46a")  # matches ChunkWidget.SELECTED_COLOR
_COL_GROUP_HEADER = QtGui.QColor("#1a1a1a")   # near-black — group/context nodes
_COL_LOCKED_HEADER = QtGui.QColor("#3a3020")  # dark amber — locked (external-layer) nodes
_COL_LOCKED_BG = QtGui.QColor("#2e2e28")
_COL_CANVAS_BG = QtGui.QColor("#252525")  # ChunkGraphView's background brush
_DIM_ALPHA = 0.35  # the opacity disabled nodes are drawn at when their body is showing
_DIM_CACHE: dict[int, QtGui.QColor] = {}


def _dimmed(color: QtGui.QColor) -> QtGui.QColor:
    """`color` pre-blended toward the canvas, matching how it looks at _DIM_ALPHA opacity.

    Painting a blended colour costs nothing; setting item opacity below 1.0 makes Qt render
    the item *and all its children* to an offscreen buffer and composite it, which measured
    at ~0.7ms per dimmed node per frame with 22 of them on screen.
    """
    key = color.rgba()
    hit = _DIM_CACHE.get(key)
    if hit is None:
        keep = 1.0 - _DIM_ALPHA
        hit = QtGui.QColor(
            round(color.red() * _DIM_ALPHA + _COL_CANVAS_BG.red() * keep),
            round(color.green() * _DIM_ALPHA + _COL_CANVAS_BG.green() * keep),
            round(color.blue() * _DIM_ALPHA + _COL_CANVAS_BG.blue() * keep),
        )
        _DIM_CACHE[key] = hit
    return hit
_COL_VIEW_HEADER = QtGui.QColor("#c87830")   # pale orange — the ACTIVE view (driving execution)
_COL_VIEW_INACTIVE = QtGui.QColor("#4a453e")  # dim grey-brown — an inactive view (not driving)
_COL_VIEW_ACTIVE = QtGui.QColor("#5fd08a")   # green — active view border accent
_COL_STALE = QtGui.QColor("#e0902a")   # amber — chunk's result no longer reflects its inputs
_COL_PORT_EXEC = QtGui.QColor("#999999")
_COL_PORT_FLOAT = QtGui.QColor("#a06845")  # muted orange
_COL_PORT_INT = QtGui.QColor("#5d7ea8")    # muted blue
_COL_PORT_BOOL = QtGui.QColor("#9e8e4a")   # muted yellow
_COL_PORT_STRING = QtGui.QColor("#6a9070")  # muted green
_COL_PORT_VECTOR = QtGui.QColor("#9a5da8")  # muted purple
_COL_TEXT_PRIMARY = QtGui.QColor("#ffffff")
_COL_TEXT_SECONDARY = QtGui.QColor("#aaaaaa")
_COL_TEXT_LOCKED = QtGui.QColor("#888877")

_CHUNKS_ROOT = "/chunks"
_CHUNK_PRIM_TYPENAME = "Chunk"
_EXEC_INPUT = "inputs:chunk"
_EXEC_OUTPUT = "outputs:chunk"
_UNBUILT = object()  # sentinel for ChunkGraphScene._built_context
# Child predicate that keeps disabled chunks: disabling sets the chunk prim inactive
# (prim.SetActive(False)), and the default GetChildren() skips inactive prims — which would
# drop the disabled node out of the graph. Include inactive-but-defined prims so a disabled
# node stays put (greyed out) instead of vanishing.
_INCLUDE_INACTIVE = Usd.PrimIsDefined & ~Usd.PrimIsAbstract

# Hidden top-level sink widget: child QWidgets parented here avoid a native OS
# window (and the Windows flash that comes with it) while staying off the viewport.
_proxy_sink: Optional[QtWidgets.QWidget] = None


def _get_proxy_sink() -> QtWidgets.QWidget:
    global _proxy_sink
    if _proxy_sink is None:
        _proxy_sink = QtWidgets.QWidget()
        # Logically visible (so children inherit WA_WState_Visible and isVisible()
        # returns True for port placement) but physically suppressed so nothing renders.
        _proxy_sink.setAttribute(QtCore.Qt.WA_DontShowOnScreen)
        # This persistent top-level sink is never closed. Without WA_QuitOnClose=False it
        # counts as an open window, so usdview's quitOnLastWindowClosed never fires when the
        # main window closes and the process hangs on exit.
        _proxy_sink.setAttribute(QtCore.Qt.WA_QuitOnClose, False)
        _proxy_sink.show()
    return _proxy_sink

_PORT_EXEC = "exec"
_PORT_FLOAT = "float"
_PORT_INT = "int"
_PORT_BOOL = "bool"
_PORT_STRING = "string"
_PORT_COLOR = "color"
_PORT_VECTOR = "vector"

_COL_PORT_COLOR = QtGui.QColor("#c87060")  # muted red

_PORT_TYPE_COLOR = {
    _PORT_EXEC:   _COL_PORT_EXEC,
    _PORT_FLOAT:  _COL_PORT_FLOAT,
    _PORT_INT:    _COL_PORT_INT,
    _PORT_BOOL:   _COL_PORT_BOOL,
    _PORT_STRING: _COL_PORT_STRING,
    _PORT_COLOR:  _COL_PORT_COLOR,
    _PORT_VECTOR: _COL_PORT_VECTOR,
}


class ChunkPortItem(QtWidgets.QGraphicsEllipseItem):
    """A connection dot on a node."""

    def __init__(self, attr_path: str, port_type: str, parent: "ChunkNodeItem", show_label: bool = False):
        r = _PORT_RADIUS
        super().__init__(-r, -r, r * 2, r * 2, parent)
        self.attr_path = attr_path
        self.port_type = port_type
        self.is_input = ".inputs:" in attr_path
        self.setBrush(_PORT_TYPE_COLOR[port_type])
        self.setPen(QtGui.QPen(QtCore.Qt.NoPen))
        self.setZValue(1)

        if show_label:
            label_text = attr_path.rsplit(":", 1)[-1]
            text = QtWidgets.QGraphicsSimpleTextItem(label_text, self)
            font = QtGui.QFont("Arial", 7)
            text.setFont(font)
            text.setBrush(QtGui.QColor("#cccccc"))
            tw = text.boundingRect().width()
            th = text.boundingRect().height()
            # Output ports sit on the right edge — label goes to the left of the dot
            text.setPos(-r - 3 - tw, -th / 2)

    def set_highlighted(self, on: bool) -> None:
        self.setPen(QtGui.QPen(QtGui.QColor("white"), 1.5) if on else QtGui.QPen(QtCore.Qt.NoPen))



def _sdf_to_port_type(sdf_type_name) -> str:
    if sdf_type_name in (Sdf.ValueTypeNames.Color3f, Sdf.ValueTypeNames.Color4f):
        return _PORT_COLOR
    if sdf_type_name in (Sdf.ValueTypeNames.Bool,):
        return _PORT_BOOL
    if sdf_type_name in (Sdf.ValueTypeNames.String, Sdf.ValueTypeNames.Token, Sdf.ValueTypeNames.Asset):
        return _PORT_STRING
    if sdf_type_name in (
        Sdf.ValueTypeNames.Int, Sdf.ValueTypeNames.Int2,
        Sdf.ValueTypeNames.Int3, Sdf.ValueTypeNames.Int4,
    ):
        return _PORT_INT
    if sdf_type_name in (
        Sdf.ValueTypeNames.Float2, Sdf.ValueTypeNames.Float3, Sdf.ValueTypeNames.Float4,
    ):
        return _PORT_VECTOR
    return _PORT_FLOAT


def _iter_connectable_params(obj: core.ChunkCommon):
    """Recursively yield float/int/bool/string/menu/color params and vectors from a chunk or group."""
    for param in obj.get_my_params():
        if isinstance(param, (Slider, Spinbox, Checkbox, LineEdit, TextEdit, FileBrowser, ColorPicker, Menu)):
            yield param
        elif isinstance(param, core.ChunkVectorBase):
            yield param  # vector as a single port — children are not connectable individually
        elif not isinstance(param, core.ChunkParamBase):
            yield from _iter_connectable_params(param)


def _iter_all_vectors(obj: core.ChunkCommon):
    """Recursively yield all ChunkVectorBase instances from a chunk or group."""
    for param in obj.get_my_params():
        if isinstance(param, core.ChunkVectorBase):
            yield param
        elif not isinstance(param, core.ChunkParamBase):
            yield from _iter_all_vectors(param)


def _param_port_type(param) -> str:
    if isinstance(param, ColorPicker):
        return _PORT_COLOR
    if isinstance(param, core.ChunkVectorBase):
        return _PORT_VECTOR
    if isinstance(param, Checkbox):
        return _PORT_BOOL
    if isinstance(param, (LineEdit, TextEdit, FileBrowser, Menu)):
        return _PORT_STRING
    if param.value is not None and isinstance(param.value, int) and not isinstance(param.value, bool):
        return _PORT_INT
    return _PORT_FLOAT


# ---------------------------------------------------------------------------
# Copy / Cut / Paste / Duplicate clipboard
#
# In-process only (a plain Python object, not the OS clipboard/QMimeData) -- same-session
# paste is the only use case this needs to serve. Module-level so it's shared across every
# ChunkGraphView/tab/chunkgroup in the process, matching how a real clipboard behaves.

class _ClipboardNode:
    __slots__ = ("module_name", "values", "rel_pos", "children", "child_edges", "list_sizes")

    def __init__(
        self, module_name: str, values: list, rel_pos: QtCore.QPointF,
        children: "list[_ClipboardNode] | None" = None,
        child_edges: "list[_ClipboardEdge] | None" = None,
        list_sizes: "list[tuple[str, int]] | None" = None,
    ):
        self.module_name = module_name
        self.values = values
        # How many rows each of the chunk's list groups held. Values alone are not enough:
        # a freshly created chunk starts with zero rows, so there is nothing for the
        # positional zip to pair them against and every row is silently dropped.
        self.list_sizes = list_sizes or []
        self.rel_pos = rel_pos  # position relative to the copied selection's top-left corner
        # Only non-empty for a ChunkGroup: its nested sub-chunks and the wiring between them,
        # so copying/duplicating/cutting a group recursively takes its whole contents along
        # instead of pasting an empty group. rel_pos for a child is relative to the *group's
        # own children's* top-left, not the top-level selection's.
        self.children: list[_ClipboardNode] = children or []
        self.child_edges: list[_ClipboardEdge] = child_edges or []


class _ClipboardEdge:
    """A wire between two copied nodes, referenced by index into the node clipboard rather
    than by prim path (paths don't exist yet at copy time for a not-yet-pasted node, and
    change on every paste anyway). src_attr/dst_attr are ChunkPortItem-suffix form
    ("outputs:chunk"/"inputs:tx", ...), not raw Sdf connection-target form -- see
    `_iter_prim_connections`."""

    __slots__ = ("src_index", "src_attr", "dst_index", "dst_attr")

    def __init__(self, src_index: int, src_attr: str, dst_index: int, dst_attr: str):
        self.src_index = src_index
        self.src_attr = src_attr
        self.dst_index = dst_index
        self.dst_attr = dst_attr


_node_clipboard: list[_ClipboardNode] = []
_edge_clipboard: list[_ClipboardEdge] = []


def _iter_prim_connections(prim: Usd.Prim):
    """Yield (dst_attr_suffix, src_prim_path, src_attr_suffix) for every real inputs:*
    connection on this prim, in ChunkPortItem.attr_path suffix form for BOTH ends (e.g. dst
    "inputs:tx", src "outputs:tx") -- independent of the "Show param connections" view toggle
    (unlike ChunkGraphScene._build_edges, which only builds what's currently drawn), since
    copy/paste needs every real connection, not just the visible ones. Takes a bare Usd.Prim
    rather than a ChunkNodeItem so it also works for a ChunkGroup's children, which have no
    live Qt node at all unless someone has dived into that group."""
    if not prim.IsValid():
        return
    for attr in prim.GetAttributes():
        attr_name = attr.GetName()
        if not attr_name.startswith("inputs:"):
            continue
        for conn_path in attr.GetConnections():
            src_prim_path = str(conn_path.GetPrimPath())
            if chunk_usd.is_exec_input_name(attr_name):
                # Exec connections point straight at the source's outputs:chunk already.
                src_attr_suffix = conn_path.name
            else:
                # Typed connections point at the source's OWN inputs:param (see
                # ChunkGraphView._author_connection) -- normalize back to the
                # ChunkPortItem-suffix "outputs:param" form used on both ends elsewhere.
                src_attr_suffix = "outputs:" + conn_path.name[len("inputs:"):]
            yield attr_name, src_prim_path, src_attr_suffix


def _prim_graph_pos(prim: "Usd.Prim | None") -> QtCore.QPointF:
    """A prim's authored node-graph position (USD Y-up -> Qt Y-down), or the origin if
    unauthored/invalid."""
    if not prim or not prim.IsValid():
        return QtCore.QPointF(0, 0)
    ui_api = UsdUI.NodeGraphNodeAPI(prim)
    pos_attr = ui_api.GetPosAttr()
    if pos_attr and pos_attr.HasAuthoredValue():
        p = pos_attr.Get()
        return QtCore.QPointF(p[0], -p[1])
    return QtCore.QPointF(0, 0)


def _iter_group_children(chunk: core.ChunkBase, scene_sender: core.SceneSender, stage: Usd.Stage):
    """Yield the direct children of a ChunkGroup -- both plain leaf chunks (found via
    get_context(), which walks the real Python ownership chain) and nested sub-groups (which
    get_context() can NEVER find this way: it returns self for any CONTEXT-flagged chunk --
    see ChunkGraphScene._rebuild_nodes's own "c is not context" guard for the same quirk --
    and a sub-group is always added as a top-level scene entry, never actually owned by its
    structural parent). Mirrors _rebuild_nodes's own combined lookup (get_context() equality
    plus a get_context_for_prim_path() scan of the group's own USD children) exactly, so a
    ChunkGroup nested inside another ChunkGroup is still found. View chunks are excluded --
    not content worth duplicating, matching the top-level selection's own exclusion."""
    by_path: dict[str, core.ChunkBase] = {
        chunk_usd.get_chunk_prim_path(c): c
        for c in scene_sender.get_chunks()
        if c.get_context() == chunk and c is not chunk and not chunk_usd.is_view_chunk(c)
    }
    root_prim = stage.GetPrimAtPath(Sdf.Path(chunk.prim_path)) if chunk.prim_path else None
    if root_prim is not None and root_prim.IsValid():
        for prim in root_prim.GetFilteredChildren(_INCLUDE_INACTIVE):
            path = str(prim.GetPath())
            if path in by_path:
                continue
            ctx_chunk = scene_sender.get_context_for_prim_path(path)
            if ctx_chunk is not None and not chunk_usd.is_view_chunk(ctx_chunk):
                by_path[path] = ctx_chunk
    return list(by_path.values())


def _snapshot_chunk_node(
    chunk: core.ChunkBase, scene_sender: core.SceneSender, stage: Usd.Stage, rel_pos: QtCore.QPointF
) -> "_ClipboardNode | None":
    """Build a _ClipboardNode for `chunk` at rel_pos, recursing into its own children (and
    their internal wiring) if it's a ChunkGroup -- shared by the top-level selection snapshot
    and by each recursive level of group contents. Returns None for a chunk with no module
    (nothing to recreate it from -- e.g. a bare exec 'dot')."""
    cfg = chunk.get_config()
    module_name = cfg.module if cfg else None
    if not module_name:
        return None
    clip_node = _ClipboardNode(
        module_name, _snapshot_param_values(chunk), rel_pos,
        list_sizes=_snapshot_list_sizes(chunk),
    )
    if not chunk_usd.is_group_chunk(chunk):
        return clip_node

    children = _iter_group_children(chunk, scene_sender, stage)
    child_prims: dict[str, Usd.Prim] = {}
    child_pos: dict[str, QtCore.QPointF] = {}
    for child in children:
        path = chunk_usd.get_chunk_prim_path(child)
        prim = stage.GetPrimAtPath(Sdf.Path(path))
        child_prims[path] = prim
        child_pos[path] = _prim_graph_pos(prim)
    if child_pos:
        min_x = min(p.x() for p in child_pos.values())
        min_y = min(p.y() for p in child_pos.values())
    else:
        min_x = min_y = 0.0

    kept_index: dict[str, int] = {}
    for child in children:
        path = chunk_usd.get_chunk_prim_path(child)
        pos = child_pos[path]
        child_clip = _snapshot_chunk_node(
            child, scene_sender, stage, QtCore.QPointF(pos.x() - min_x, pos.y() - min_y)
        )
        if child_clip is None:
            continue
        kept_index[path] = len(clip_node.children)
        clip_node.children.append(child_clip)

    for child in children:
        path = chunk_usd.get_chunk_prim_path(child)
        dst_index = kept_index.get(path)
        prim = child_prims.get(path)
        if dst_index is None or prim is None:
            continue
        for dst_attr, src_prim_path, src_attr in _iter_prim_connections(prim):
            src_index = kept_index.get(src_prim_path)
            if src_index is None:
                continue  # source outside this group's copied children -- wire dropped
            clip_node.child_edges.append(_ClipboardEdge(src_index, src_attr, dst_index, dst_attr))

    return clip_node


def _author_raw_connection(
    stage: Usd.Stage,
    dst_prim_path: str, dst_attr_suffix: str, dst_chunk: core.ChunkBase,
    src_prim_path: str, src_attr_suffix: str,
) -> None:
    """Author one connection directly at the USD/attribute level, without going through a
    live ChunkPortItem -- used for wiring a just-pasted ChunkGroup's children, which have no
    Qt graph representation at all unless someone has actually dived into that group.
    Mirrors ChunkGraphView._author_connection's semantics (same raw-connection-target vs.
    visible-port-suffix distinction for typed params, same expression-string write, same
    bookkeeping-layer resolution) without needing a graph_scene/ChunkPortItem to read it
    from."""
    is_exec = chunk_usd.is_exec_input_name(dst_attr_suffix)
    dst_attr = stage.GetAttributeAtPath(Sdf.Path(f"{dst_prim_path}.{dst_attr_suffix}"))
    if not (dst_attr and dst_attr.IsValid()):
        prim = stage.GetPrimAtPath(Sdf.Path(dst_prim_path))
        if not prim.IsValid():
            return
        sdf_type = Sdf.ValueTypeNames.Token if is_exec else Sdf.ValueTypeNames.String
        dst_attr = prim.CreateAttribute(dst_attr_suffix, sdf_type, custom=False)
        if not dst_attr.IsValid():
            return
    if is_exec:
        src_conn_path = Sdf.Path(f"{src_prim_path}.{src_attr_suffix}")
    else:
        # src_attr_suffix is "outputs:param" (ChunkPortItem-suffix form) -- the raw
        # connection target is the source's own inputs:param, per _author_connection.
        src_conn_path = Sdf.Path(f"{src_prim_path}.inputs:{src_attr_suffix[len('outputs:'):]}")
    home = chunk_utils.get_attr_home_layer(dst_attr)
    target = home or chunk_utils.get_bookkeeping_layer(stage, dst_chunk)
    with Usd.EditContext(stage, target):
        dst_attr.SetConnections([src_conn_path])

    if not is_exec:
        src_node_name = Sdf.Path(src_prim_path).name
        src_param_name = src_attr_suffix[len("outputs:"):]
        dst_param_name = dst_attr_suffix[len("inputs:"):]
        param = dst_chunk.get_param(dst_param_name)
        if param is not None:
            param.value = f"@{src_node_name}.{src_param_name}@"


def _iter_copyable_params(chunk: core.ChunkCommon):
    """`_iter_connectable_params`, minus anything flagged State.HIDDEN.

    A HIDDEN param is a chunk module's own internal bookkeeping, never meant for a user to
    see or set directly -- e.g. scripts/create_layer.py's _new_layer_id/_layer_name_used/
    _parent_layer_id, which cache identifiers for the anon Sdf.Layer *that specific chunk
    instance* created. Copying those verbatim onto a pasted chunk hands it the ORIGINAL
    instance's cached identifier; since that layer is still alive (Sdf.Layer.Find succeeds
    process-wide, not scoped to this chunk), the pasted chunk's own next execute() silently
    adopts and reuses the original's real layer object instead of creating an independent one
    of its own -- the two nodes end up secretly sharing state. Not scoped to
    scripts/create_layer.py specifically -- any chunk module using a HIDDEN param the same way
    has the identical hazard, so the exclusion lives at this shared, module-agnostic level
    rather than as a create_layer-specific special case.

    Deliberately a separate generator from `_iter_connectable_params` itself (used elsewhere
    for port placement) rather than changing what that one yields -- copy/paste is the only
    caller that needs this narrower set."""
    for param in _iter_connectable_params(chunk):
        if param.has_state(ChunkFlags.State.HIDDEN):
            continue
        yield param


def _snapshot_list_sizes(chunk: core.ChunkCommon) -> "list[tuple[str, int]]":
    """How many rows each list group on `chunk` holds, keyed by param path.

    A list group's rows are created at runtime, so a pasted chunk has none of them and the
    positional value snapshot has nothing to land on -- copying a Sublayer with a path set
    produced one with an empty list and no path, and the same for every other chunk built
    on a list group.
    """
    sizes: "list[tuple[str, int]]" = []
    for path, param in _iter_chunk_param_paths(chunk):
        if isinstance(param, core.ChunkListGroupBase):
            sizes.append((path, param.size))
    return sizes


def _apply_list_sizes(chunk: core.ChunkCommon, sizes: "list[tuple[str, int]]") -> None:
    """Grow each list group back before any value is applied, so the rows exist to take them."""
    for path, size in sizes:
        param = chunk.get_param(path)
        if isinstance(param, core.ChunkListGroupBase) and param.size != size:
            try:
                param.size = size
            except Exception:
                # A list whose size cannot be set (driven by something else, or a module
                # that has changed shape since the copy) should not abort the paste.
                continue


def _iter_chunk_param_paths(chunk: core.ChunkCommon, prefix: str = ""):
    """(dotted path, param) for everything under `chunk`, groups included."""
    for param in chunk.get_my_params():
        path = f"{prefix}{param.name}"
        yield path, param
        if isinstance(param, core.ChunkGroupBase):
            yield from _iter_chunk_param_paths(param, f"{path}.")


def _snapshot_param_values(chunk: core.ChunkCommon) -> list:
    """Positional snapshot of every copyable param's value, in `_iter_copyable_params`
    order. Two instances of the same chunk module build their param tree in the same code
    order, so a plain positional zip (see `_apply_param_values`) is enough to pair them back
    up -- no param-path bookkeeping needed."""
    return [p.value for p in _iter_copyable_params(chunk)]


def _apply_param_values(chunk: core.ChunkCommon, values: list) -> None:
    for param, value in zip(_iter_copyable_params(chunk), values):
        try:
            param.value = value
        except Exception:
            # A param whose value can't be re-applied as-is (e.g. a stale expression string
            # for a wire that wasn't copied along with it) shouldn't abort the rest of the
            # paste -- leave it at its default and move on.
            continue


# ---------------------------------------------------------------------------
# Drag-and-drop file import
#
# Dropping a saved-chunkgroup .usda (chunk_qt.save_chunkgroup_dialog's output) reuses the
# copy/paste clipboard machinery above via _snapshot_layer_chunks below, which builds the same
# _ClipboardNode/_ClipboardEdge shape Ctrl+C does -- just read from a dropped file's own
# throwaway Usd.Stage instead of the live scene. Dropping a plain scene file instead spawns a
# Sublayer chunk node with the path pre-filled (see ChunkGraphView._import_as_sublayer).

def droppable_paths(event) -> "list[str]":
    """The USD file paths a drag carries, if any.

    Module-level because the tab panel needs the same answer before a graph view exists:
    with no context there is no ChunkGraphView to accept the drop at all, so the panel
    takes it, creates the context, and hands the files on.
    """
    if not event.mimeData().hasUrls():
        return []
    exts = (".usd", ".usda", ".usdc", ".usdz")
    return [
        url.toLocalFile() for url in event.mimeData().urls()
        if url.isLocalFile() and os.path.splitext(url.toLocalFile())[1].lower() in exts
    ]


def _detect_dropped_usd_kind(path: str) -> "str | None":
    """Classification of a dropped file for ChunkGraphView.dropEvent: 'chunks' if it has any
    Chunk-typed prim ANYWHERE in it, 'scene' if it's a USD file with none, None if it isn't a
    recognized USD file at all. There's no fixed root to shortcut the check against -- a
    chunk's root is its tab/context's own prim_path (chunk_core.add_context names a new tab
    "/" + the tab name, not a fixed "/chunks" convention -- /chunks is only chunk_usd.py's
    fallback for a context with no explicit prim_path), so a saved chunkgroup's chunks can
    live under any prim path depending on what its source tab was called. Sdf.Layer.FindOrOpen
    does a lightweight scene-description parse only (no composition, no schema/plugin loading,
    no geometry processing), and Layer.Traverse below walks specs, not geometry data, so this
    still stays proportionally cheap -- just not O(1) the way a fixed-root lookup would have
    been -- no matter how large an unrelated scene file is."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".usd", ".usda", ".usdc", ".usdz"):
        return None
    try:
        layer = Sdf.Layer.FindOrOpen(path)
    except Exception:
        return None
    if layer is None:
        return None
    found = False

    def _visit(spec_path: "Sdf.Path") -> None:
        nonlocal found
        if found or not spec_path.IsPrimPath():
            return
        spec = layer.GetPrimAtPath(spec_path)
        if spec is not None and spec.typeName == _CHUNK_PRIM_TYPENAME:
            found = True

    layer.Traverse(Sdf.Path("/"), _visit)
    return "chunks" if found else "scene"


def _build_clip_node_from_prim(prim: "Usd.Prim", rel_pos: QtCore.QPointF) -> "_ClipboardNode | None":
    """Build a _ClipboardNode straight from a Usd.Prim on a throwaway, unregistered stage (a
    dropped file opened via Usd.Stage.Open) -- the drag-and-drop counterpart of
    _snapshot_chunk_node, which instead reads a *live* chunk already registered in the real
    scene. Instantiates a throwaway chunk never added to any scene, purely to learn its
    module's canonical param order (same instantiate-without-adding pattern
    ChunkGraphView._instantiate_chunk_of_type already uses for paste), then restores its values
    via the same chunk_usd._load_wire_inputs the real stage-load path uses -- so list groups,
    dotted param paths and expression strings all resolve exactly the same way, without
    touching the ChunkRegistry/ChunkLayerManager singletons or the real stage at all.

    Blocked BEFORE _load_wire_inputs restores any values: ChunkBase.get_stage() always
    returns the real, live global stage regardless of whether THIS instance was ever added
    to any scene, so a module with real side-effecting reactive code (e.g.
    scripts/create_layer.py's execute() authoring a genuine anon Sdf.Layer the instant its
    layer_name value changes) runs for real even on a throwaway, never-added chunk -- live
    confirmed as an orphaned duplicate layer appearing after a drag-and-drop import."""
    module_name = prim.GetAttribute(chunk_usd._CHUNK_TYPE_ATTR).Get()
    if not module_name:
        return None
    registry = chunk_registry.ChunkRegistry()
    if not registry.initialized:
        return None
    mod = registry.get_chunk_module(str(module_name))
    if mod is None:
        return None
    chunks = mod.create() if mod.loaded else mod.load()
    primary = next((c for c in chunks if not chunk_usd.is_view_chunk(c)), None)
    if primary is None:
        return None
    primary.set_blocked(True)
    chunk_usd._load_wire_inputs(prim, primary)
    clip_node = _ClipboardNode(str(module_name), _snapshot_param_values(primary), rel_pos)
    if not chunk_usd.is_group_chunk(primary):
        return clip_node

    child_prims = [
        c for c in prim.GetFilteredChildren(_INCLUDE_INACTIVE)
        if c.GetAttribute(chunk_usd._CHUNK_TYPE_ATTR).Get()
    ]
    if not child_prims:
        return clip_node
    positions = {str(c.GetPath()): _prim_graph_pos(c) for c in child_prims}
    min_x = min(p.x() for p in positions.values())
    min_y = min(p.y() for p in positions.values())

    kept_index: dict[str, int] = {}
    for child in child_prims:
        path = str(child.GetPath())
        pos = positions[path]
        child_clip = _build_clip_node_from_prim(
            child, QtCore.QPointF(pos.x() - min_x, pos.y() - min_y)
        )
        if child_clip is None:
            continue
        kept_index[path] = len(clip_node.children)
        clip_node.children.append(child_clip)

    for child in child_prims:
        path = str(child.GetPath())
        dst_index = kept_index.get(path)
        if dst_index is None:
            continue
        for dst_attr, src_prim_path, src_attr in _iter_prim_connections(child):
            src_index = kept_index.get(src_prim_path)
            if src_index is None:
                continue  # source outside this group's children on the dropped file -- wire dropped
            clip_node.child_edges.append(_ClipboardEdge(src_index, src_attr, dst_index, dst_attr))

    return clip_node


def _snapshot_layer_chunks(layer: "Sdf.Layer") -> "tuple[list[_ClipboardNode], list[_ClipboardEdge]]":
    """Read every top-level module-backed chunk prim out of a dropped .usd(a/c/z) file into
    clipboard-shaped data, ready for ChunkGraphView._materialize_at -- the drag-and-drop
    counterpart of Ctrl+C copying a live selection. Opens the file as its own throwaway
    Usd.Stage (not attached to ChunkRegistry/ChunkLayerManager) purely to get real Usd.Prim/
    Usd.Attribute objects to read from; nothing here touches the real stage or scene.

    No fixed root to scope the search to -- a chunk's root is its source tab/context's own
    prim_path (chunk_core.add_context names a new tab "/" + the tab name), so this walks the
    WHOLE dropped stage for module-backed chunk prims, then keeps only the ones not nested
    inside another module-backed chunk already found -- those are picked up instead by
    _build_clip_node_from_prim's own ChunkGroup-children recursion, so keeping them here too
    would import them twice."""
    stage = Usd.Stage.Open(layer)
    if not stage:
        return [], []
    candidates: "dict[str, Usd.Prim]" = {}
    # _INCLUDE_INACTIVE, not the bare default predicate -- Traverse()'s default excludes
    # inactive prims, and a disabled chunk is authored as prim.SetActive(False) (see this
    # file's other _INCLUDE_INACTIVE uses), so a disabled chunk in the dropped file would
    # otherwise be silently dropped from the import instead of landing greyed-out/disabled
    # like everywhere else in this app.
    for prim in stage.Traverse(_INCLUDE_INACTIVE):
        if prim.GetTypeName() != _CHUNK_PRIM_TYPENAME:
            continue
        if prim.GetAttribute(chunk_usd._CHUNK_TYPE_ATTR).Get():
            candidates[str(prim.GetPath())] = prim

    # A saved file's outermost chunkgroup -- the one sitting directly under the /chunks
    # scope -- is the container the graph lived in, not a node someone dropped in. Left in
    # candidates it becomes the file's ONLY top-level chunk, and everything actually in
    # the file reads as its contents: dropping a two-chunk file imported one node.
    # Dropping it from candidates lets its children be top-level, which is what they were.
    # A group nested any deeper is real content and stays.
    scope = Sdf.Path(chunk_usd._CHUNKS_ROOT)
    for path in [
        path for path, prim in candidates.items()
        if prim.GetParent() and prim.GetParent().GetPath() == scope
        and prim.GetAttribute(chunk_usd._CHUNK_TYPE_ATTR).Get() == "chunkgroup"
    ]:
        del candidates[path]

    def _is_top_level(prim: "Usd.Prim") -> bool:
        p = prim.GetParent()
        while p and not p.IsPseudoRoot():
            if str(p.GetPath()) in candidates:
                return False
            p = p.GetParent()
        return True

    top_prims = [prim for prim in candidates.values() if _is_top_level(prim)]
    if not top_prims:
        return [], []
    positions = {str(p.GetPath()): _prim_graph_pos(p) for p in top_prims}
    min_x = min(p.x() for p in positions.values())
    min_y = min(p.y() for p in positions.values())

    kept_index: dict[str, int] = {}
    clip_nodes: list[_ClipboardNode] = []
    for prim in top_prims:
        path = str(prim.GetPath())
        pos = positions[path]
        clip_node = _build_clip_node_from_prim(
            prim, QtCore.QPointF(pos.x() - min_x, pos.y() - min_y)
        )
        if clip_node is None:
            continue
        kept_index[path] = len(clip_nodes)
        clip_nodes.append(clip_node)

    clip_edges: list[_ClipboardEdge] = []
    for prim in top_prims:
        dst_index = kept_index.get(str(prim.GetPath()))
        if dst_index is None:
            continue
        for dst_attr, src_prim_path, src_attr in _iter_prim_connections(prim):
            src_index = kept_index.get(src_prim_path)
            if src_index is None:
                continue  # source outside the dropped file's top-level chunks -- wire dropped
            clip_edges.append(_ClipboardEdge(src_index, src_attr, dst_index, dst_attr))

    return clip_nodes, clip_edges


class ChunkNodeItem(QtWidgets.QGraphicsItem):
    """Represents a single Chunk prim as a compact draggable node.

    Shows float/int/bool param dots for wiring; all other editing happens
    in the Params panel via selection sync.
    """

    def __init__(self, chunk: core.ChunkBase, prim: Usd.Prim, locked: bool = False):
        super().__init__()
        self.chunk = chunk
        self.prim = prim
        self._locked = locked
        self._pinned = chunk_usd.is_chunk_pinned(chunk)
        # Navigable subnet, not merely "owns children". A chunk-in-chunk such as
        # AMDMaterialXLibrary also carries GroupFlags.CONTEXT, but its children are an
        # implementation detail: it must not offer a dive, nor the group header and the
        # `▶` marker that advertise one. Containment still keys on
        # chunk_usd.is_group_chunk, so its children stay hidden and keep cooking.
        self._is_chunkgroup = chunk_usd.is_chunkgroup(chunk)
        self._ports: list[ChunkPortItem] = []
        self._typed_ports: list[ChunkPortItem] = []
        self._selected = chunk.has_state(ChunkFlags.State.SELECTED)
        self._stale = chunk.has_state(ChunkFlags.State.STALE)
        self._update_stale_tooltip()
        _chunk_type = prim.GetAttribute("info:chunkType").Get()
        self._is_view = _chunk_type == "view"
        self._is_dot = _chunk_type == "dot"
        self._is_merge = _chunk_type == "merge"
        self._chunk_type_label = str(_chunk_type or "")  # cached: paint must not read USD
        self._disabled = chunk.has_state(ChunkFlags.State.DISABLED)
        self._lod_simple = False  # see set_body_visible
        # Height settling -- see _note_height_sample and is_height_settled.
        self._last_height_sample = None
        self._drag_active = False
        self._drag_moved = False  # True once a drag actually moves — plain clicks stay False
        self._drag_start = QtCore.QPointF()
        self._shake_samples: list[tuple[float, float]] = []  # (time, scene x) during a drag
        self._shake_fired = False
        self._last_collapse_toggle_time = 0.0  # see _COLLAPSE_TOGGLE_DEBOUNCE
        # Multi-select drag: other selected (unlocked) nodes moved alongside this one, and
        # whether a plain (no-drag) click on an already-multi-selected node should collapse
        # the selection down to just this node once the press/release completes without moving.
        self._drag_companions: list["ChunkNodeItem"] = []
        self._drag_pending_collapse = False
        self._proxy: Optional[QtWidgets.QGraphicsProxyWidget] = None
        self._port_rebuild_timer: Optional[QtCore.QTimer] = None
        self._height = _NODE_HEADER_H
        self._full_height = _NODE_HEADER_H
        self._outputs_start_y: float = 0.0  # >0 when typed output ports are present

        self.setFlag(QtWidgets.QGraphicsItem.ItemIsMovable, False)  # handled manually — header only
        self.setFlag(QtWidgets.QGraphicsItem.ItemSendsGeometryChanges, True)
        self.setFlag(QtWidgets.QGraphicsItem.ItemIsFocusable, True)

        self._state_conn = NoticeConnection(
            self._on_state_changed,
            ChunkNoticeAlias.STATE_CHANGED,
            chunk.get_attribute_sender("state"),
        )
        # A lofted param (e.g. "Loft up" on an inner chunk's param, surfaced onto this
        # ChunkGroup) adds a live param to `chunk` after this node already exists and has
        # a built proxy -- nothing else triggers a port rebuild for that: _rebuild_typed_ports
        # otherwise only runs once via create_proxy()'s initial singleShot and on proxy
        # geometryChanged (collapse/expand), and _rebuild_nodes reuses an existing node
        # as-is rather than reconstructing it. Without this, the loft shows up in the Params
        # tab (which does listen for PARAM_ADDED) but never gets a port on the graph node.
        self._param_conn = NoticeConnection(
            self._on_param_added, ChunkNoticeAlias.PARAM_ADDED, chunk
        )
        self._param_removed_conn = NoticeConnection(
            self._on_param_added, ChunkNoticeAlias.PARAM_REMOVED, chunk
        )
        # A param's own HIDDEN state toggling (e.g. Create Layer's Index field, shown only
        # for Insert == "Index") needs the same rebuild PARAM_ADDED gets -- reported live:
        # switching Insert away from "Index" left the Index port sitting there until
        # something unrelated (deselecting the node) forced a fresh layout pass. Unlike a
        # group's own collapse animation (toggle_anim.finished, connected in create_proxy),
        # there's no Qt signal to hang this off at all -- a hide is a direct
        # set_state(HIDDEN), no animation involved -- so geometryChanged has nothing to
        # fire from until something else happens to trigger a relayout regardless. Listened
        # for at the chunk-notice level instead, mirroring _param_conn above.
        self._param_state_conns = self._build_param_state_conns()

        self._build_ports(prim)
        self._apply_usd_pos(prim)
        # Reflect disabled state on (re)build — otherwise a rebuilt disabled node renders at
        # full opacity until the next state change (e.g. selecting it).
        self._apply_dim()

    def set_pinned(self, pinned: bool) -> None:
        """Update the cached pin badge state after a menu toggle and repaint just this node —
        no full graph rebuild needed for a cosmetic change."""
        self._pinned = pinned
        self.update()

    def set_locked(self, locked: bool) -> None:
        """Update the cached locked state (edit-target changed, or this node is reused across a
        rebuild) and repaint just this node — no full node reconstruction needed."""
        if locked != self._locked:
            self._locked = locked
            self.update()

    # ------------------------------------------------------------------
    # Build

    def _build_ports(self, prim: Usd.Prim):
        if self._is_dot:
            self._exec_in_port = ChunkPortItem(
                f"{prim.GetPath()}.inputs:chunk", _PORT_EXEC, parent=self
            )
            self._exec_in_port.setPos(0, _DOT_R)
            self._ports.append(self._exec_in_port)
            self._exec_out_port = ChunkPortItem(
                f"{prim.GetPath()}.outputs:chunk", _PORT_EXEC, parent=self
            )
            self._exec_out_port.setPos(_DOT_R * 2, _DOT_R)
            self._ports.append(self._exec_out_port)
            self._height = _DOT_R * 2
            self._full_height = _DOT_R * 2
            return

        if self._is_merge:
            # One port per connected branch, plus one empty trailing slot to attach the next
            # branch to. The trailer is deliberately UI-only, with no backing attribute:
            # _author_connection creates the attribute on demand from the port's own
            # attr_path, so an unconnected inputs:chunkN never has to be authored — and then
            # serialized into every saved file forever — just to give a wire somewhere to
            # land. Ports read top-to-bottom as strongest-to-weakest.
            slots = len(chunk_usd.exec_input_connections(prim)) + 1
            for i in range(slots):
                port = ChunkPortItem(
                    f"{prim.GetPath()}.{chunk_usd.exec_input_name(i)}", _PORT_EXEC, parent=self
                )
                port.setPos(0, _MERGE_PORT_TOP + i * _MERGE_PORT_SPACING)
                self._ports.append(port)
            # Everything that asks a node for "its" exec input (splice-on-drop, wire-drop
            # targeting) should get the primary one, which is slot 0 — the strongest.
            self._exec_in_port = self._ports[0]
            self._height = max(
                _NODE_HEADER_H, _MERGE_PORT_TOP * 2 + (slots - 1) * _MERGE_PORT_SPACING
            )
            self._full_height = self._height
            self._exec_out_port = ChunkPortItem(
                f"{prim.GetPath()}.outputs:chunk", _PORT_EXEC, parent=self
            )
            self._exec_out_port.setPos(_NODE_WIDTH, _NODE_HEADER_H // 2)
            self._ports.append(self._exec_out_port)
            return

        # Exec ports only — lightweight, no widget creation.
        # Proxy (ChunkWidget) is created later via create_proxy() to allow batched deferred loading.
        self._exec_in_port = ChunkPortItem(
            f"{prim.GetPath()}.inputs:chunk", _PORT_EXEC, parent=self
        )
        self._exec_in_port.setPos(0, _NODE_HEADER_H // 2)
        self._ports.append(self._exec_in_port)

        if prim.HasAttribute("outputs:chunk"):
            self._exec_out_port = ChunkPortItem(
                f"{prim.GetPath()}.outputs:chunk", _PORT_EXEC, parent=self
            )
            self._exec_out_port.setPos(_NODE_WIDTH, _NODE_HEADER_H // 2)
            self._ports.append(self._exec_out_port)
        else:
            self._exec_out_port = None

    def sync_merge_ports(self) -> bool:
        """Re-place a merge's exec input slots when the number of connected branches changed.

        A merge shows one port per branch plus one empty trailer, so wiring the trailer has to
        grow a fresh one and clearing a slot has to drop one. Ports are built once at node
        construction, and a connection change does not rebuild the node — only the edges — so
        without this the trailer would only appear after some unrelated full scene rebuild.

        Returns:
            bool: True if the ports were rebuilt (the caller's edge items are now stale)
        """
        if not self._is_merge or not self.prim.IsValid():
            return False
        want = len(chunk_usd.exec_input_connections(self.prim)) + 1
        have = sum(1 for p in self._ports if p.is_input)
        if want == have:
            return False
        scene = self.scene()
        for port in self._ports:
            port.setParentItem(None)
            if scene:
                scene.removeItem(port)
        self._ports.clear()
        self.prepareGeometryChange()
        self._build_ports(self.prim)
        self.update()
        return True

    def create_proxy(self):
        """Create the embedded ChunkWidget proxy. Called in deferred batches after scene build."""
        # A merge has no params, so there is no body worth embedding — and skipping the proxy
        # also keeps _rebuild_typed_ports a no-op for it (it bails on a None proxy), which
        # matters because that method rebuilds _ports from the two exec port slots alone and
        # would otherwise drop every numbered slot port.
        if self._is_dot or self._is_merge:
            return
        # Runs deferred; the node may have been removed by a rebuild before this fires.
        # Building a proxy/ports on a detached node leaves a ghost (its C++ item lingers in
        # the scene after its Python wrapper is GC'd). Bail if we're no longer in a scene.
        try:
            if self.scene() is None:
                return
        except RuntimeError:
            return
        # Create as a child of the sink so the show() called during init (via
        # update_from_state → set_hidden(False)) doesn't flash a native OS window.
        # for_proxy: the body arrives unstyled, on purpose. Reparenting a styled tree costs
        # ~22ms against ~2.5ms for an unstyled one, because the move re-resolves the style
        # of every descendant -- more than is saved by applying the sheet early. So the
        # sheet waits until the reparent below is done.
        chunk_widget = ChunkWidget(self.chunk, parent=_get_proxy_sink(), for_proxy=True)
        # setWidget() requires a top-level widget.  Reparent to None first, but set
        # WA_DontShowOnScreen beforehand so the OS never calls ShowWindow during the
        # transition from child → top-level.
        chunk_widget.setAttribute(QtCore.Qt.WA_DontShowOnScreen)
        chunk_widget.setParent(None)
        # Now, with the move paid for: this is the one application for this tree, and any
        # later one (set_selected) finds the guard already set.
        chunk_widget._defer_stylesheet = False
        apply_chunk_stylesheet(chunk_widget)
        chunk_widget.setFixedWidth(_NODE_WIDTH)
        self._chunk_widget = chunk_widget  # pin Python ref; PySide6 may GC despite Qt C++ ownership
        self._proxy = QtWidgets.QGraphicsProxyWidget(self)
        self._proxy.setWidget(chunk_widget)
        self._proxy.setPos(0, _NODE_HEADER_H)
        # Proxies are built in deferred batches, so a node can arrive while the view is
        # already zoomed past the LOD threshold. Without this it would come up showing a
        # body every other node has dropped.
        views = self.scene().views() if self.scene() is not None else []
        if views and not getattr(views[0], "_lod_bodies_shown", True):
            self._proxy.setVisible(False)
        self._full_height = _NODE_HEADER_H + chunk_widget.sizeHint().height()
        self._height = self._full_height
        self.prepareGeometryChange()


        self._port_rebuild_timer = QtCore.QTimer(chunk_widget)
        self._port_rebuild_timer.setSingleShot(True)
        # Long enough to outlast a burst of geometry changes. Expanding a group animates
        # the content height, and every frame emits geometryChanged and restarts this
        # timer — at interval 0 it fired between frames, so a rebuild that walks the whole
        # widget tree twice (two findChildren sweeps) ran once per frame. Ports only need
        # to be right once the geometry settles, so wait for it to stop moving.
        self._port_rebuild_timer.setInterval(_PORT_REBUILD_MS)
        self._port_rebuild_timer.timeout.connect(self._rebuild_typed_ports)
        # geometryChanged fires for collapse/expand after initial setup.
        # Initial port placement is triggered explicitly below so it doesn't
        # depend on geometryChanged firing at the right time relative to the connection.
        self._proxy.geometryChanged.connect(self._port_rebuild_timer.start)
        # Belt and suspenders for a NESTED group's own collapse/expand: geometryChanged
        # is supposed to cover this too (a group's content resizing should resize the
        # whole chunk_widget in turn), but was observed live to not reliably do so for a
        # collapse specifically (expand was fine) -- animation_finished's SetNoConstraint
        # vs SetMinAndMaxSize switch on content_layout looks like the likely culprit,
        # leaving the node showing stale, overlapping ports until something unrelated
        # (e.g. deselecting, which _set_collapsed's own branch happens to also kick a
        # rebuild for) forced a fresh layout pass. Connecting directly to each group's own
        # animation-finished signal sidesteps depending on that propagation working.
        for nested in chunk_widget.findChildren(ContainerWidget):
            if nested.group.has_flag(ChunkFlags.GroupFlags.COLLAPSABLE):
                nested.toggle_anim.finished.connect(self._port_rebuild_timer.start)
        # No context object here, unlike every other deferred call in this file:
        # QGraphicsItem is not a QObject, so QTimer.singleShot has nothing to bind the
        # cancellation to. _rebuild_typed_ports carries the guard instead -- it checks the
        # prim, the proxy, and catches the RuntimeError from an already-destroyed C++
        # proxy -- which is why this one is safe to schedule bare.
        QtCore.QTimer.singleShot(0, self._rebuild_typed_ports)

    def _rebuild_typed_ports(self):
        # This is deferred (singleShot), so by the time it runs the chunk may already be
        # gone — disabled (prim inactive) or deleted (prim expired). Rebuilding ports reads
        # prim attributes, so bail rather than crash; the scene rebuild will drop this node.
        if not self.prim.IsValid():
            return
        if self._proxy is None:
            return
        try:
            if self._proxy.widget() is None:
                return
        except RuntimeError:
            # C++ QGraphicsProxyWidget already destroyed (e.g. node deleted between
            # scheduling this deferred rebuild and it firing) — nothing to rebuild.
            return
        scene = self.scene()
        for port in self._typed_ports:
            port.setParentItem(None)
            if scene:
                scene.removeItem(port)
        self._typed_ports.clear()
        self._ports = [p for p in [self._exec_in_port, self._exec_out_port] if p is not None]
        if self.chunk.has_state(ChunkFlags.State.COLLAPSED):
            # Proxy is hidden — place ports at header centre using param list alone.
            self._place_param_ports_collapsed()
        else:
            self._place_param_ports(self._proxy.widget())
        if scene:
            # Deferred: this fires once per node whose ports change, and a chunk being
            # built retypes its ports repeatedly. Edges only have to be right once the
            # burst settles.
            scene._request_build_edges()
        # New/removed/recolored port items don't always get their region marked
        # dirty automatically — without this the old pixels can linger on screen
        # until something unrelated (e.g. deselecting the node) forces a repaint.
        self.update()
        # Both branches above have just written self._height; this is the one place every
        # height update passes through, so it is where settling is tracked.
        self._note_height_sample()

    def is_height_settled(self) -> bool:
        """True once the graph has finished laying out and this height can be trusted.

        A node's height comes from measuring its body widget, and that measurement is only
        meaningful after the body has been laid out -- which takes several event loop turns
        and several *intermediate* measurements, each a legitimate-looking number. Anything
        that shrinks or hides a body has to wait for this: a body hidden mid-layout never
        finishes and freezes the node at whatever partial height it had reached, taking that
        node's ports and edges with it.

        Answered by the scene rather than the node -- see ChunkGraphScene.heights_settled for
        why a node cannot tell on its own.
        """
        scene = self.scene()
        return bool(scene.heights_settled()) if isinstance(scene, ChunkGraphScene) else False

    def _note_height_sample(self) -> None:
        """Report a height change to the scene, which owns the settle countdown."""
        try:
            if self._height == self._last_height_sample:
                return
            self._last_height_sample = self._height
            scene = self.scene()
            if isinstance(scene, ChunkGraphScene):
                scene.note_height_change()
        except RuntimeError:
            return

    def _place_prim_output_ports(self, y_start: float, extend_height: bool = False):
        """Create output ports for every outputs:* attribute on the prim (excluding
        outputs:chunk) that isn't already represented by a param-level port — every
        connectable param gets its own inputs:/outputs: pair from _place_param_ports
        regardless of the OUTPUT flag, so an OUTPUT param's real outputs:attr would
        otherwise show up twice (once inline with its widget, once again down here)."""
        covered = {f"outputs:{p.name}" for p in _iter_connectable_params(self.chunk)}
        _STEP = _PORT_RADIUS * 2 + 8
        attrs = [
            a for a in self.prim.GetAttributes()
            if a.GetName().startswith("outputs:")
            and a.GetName() != "outputs:chunk"
            and a.GetName() not in covered
        ]
        if not attrs:
            return
        self._outputs_start_y = y_start
        y = y_start + _PORT_RADIUS + 4
        for attr in attrs:
            name = attr.GetName()
            ptype = _sdf_to_port_type(attr.GetTypeName())
            port = ChunkPortItem(f"{self.prim.GetPath()}.{name}", ptype, parent=self, show_label=True)
            port.setPos(_NODE_WIDTH, y)
            self._ports.append(port)
            self._typed_ports.append(port)
            y += _STEP
        if extend_height:
            needed = y + _PORT_RADIUS + 4
            if needed > self._height:
                self._height = needed
                self.prepareGeometryChange()

    def _connected_param_names(self) -> "set[str]":
        """Names of connectable params with a real wire touching them, either end.

        Collapsed nodes have no visible per-param widgets to hang ports off, so this is
        what decides which ports still get shown (see _place_param_ports_collapsed) --
        an unwired param has nothing to point at and no reason to take up room.

        A param's inputs:X attr carries the connection when THIS node is the one being
        fed (checked directly); when this node instead feeds something downstream, the
        connection lives on the OTHER prim's inputs:Y attr pointing back at our
        outputs:X, so that side needs a reverse lookup. Scoped to scene._nodes (the
        currently-built context) rather than a whole-stage traverse -- the same universe
        _build_edges draws wires for, and cheap since it only runs on collapse/rebuild,
        not per frame.
        """
        names = {
            attr_name[len("inputs:"):]
            for attr_name, _, _ in _iter_prim_connections(self.prim)
        }
        scene = self.scene()
        if isinstance(scene, ChunkGraphScene):
            my_path = str(self.prim.GetPath())
            for node in scene._nodes.values():
                for _, src_prim_path, src_attr_suffix in _iter_prim_connections(node.prim):
                    if src_prim_path == my_path and src_attr_suffix.startswith("outputs:"):
                        names.add(src_attr_suffix[len("outputs:"):])
        return names

    def _nearest_collapsed_group(self, param) -> "core.ChunkCommon | None":
        """The outermost Group between param and self.chunk that's currently collapsed, or
        None if none of them are.

        Outermost rather than nearest: a port anchored at a collapsed group two levels
        deep would itself be invisible/meaningless if the group ABOVE it is also
        collapsed, so the walk keeps going and the last (closest-to-chunk) collapsed
        ancestor found wins.
        """
        owner = param.get_owner()
        found = None
        while owner is not None and owner is not self.chunk:
            # The chain alternates ChunkCommon groups with their own NameSyncedContainer
            # (which owns the group's children but isn't itself a group and has no state
            # of its own) -- only the former can be collapsed.
            if isinstance(owner, core.ChunkCommon) and owner.has_state(ChunkFlags.State.COLLAPSED):
                found = owner
            owner = owner.get_owner()
        return found

    def _place_param_ports_collapsed(self):
        # Everything else on a collapsed node's edge (the exec in/out ports) already sits
        # at header centre, so a wired param's ports start one step below it rather than
        # on top of it -- overlapping there is the same "wrong port" bug as overlapping
        # each other, just with the exec port instead of a sibling data port.
        _STEP = _PORT_RADIUS * 2 + 8
        self._height = _NODE_HEADER_H
        self.prepareGeometryChange()
        connected = self._connected_param_names()
        y = _NODE_HEADER_H // 2
        for param in _iter_connectable_params(self.chunk):
            if param.name not in connected:
                continue
            y += _STEP
            ptype = _param_port_type(param)
            in_port = ChunkPortItem(f"{self.prim.GetPath()}.inputs:{param.name}", ptype, parent=self)
            in_port.setPos(0, y)
            self._ports.append(in_port)
            self._typed_ports.append(in_port)
            out_port = ChunkPortItem(f"{self.prim.GetPath()}.outputs:{param.name}", ptype, parent=self)
            out_port.setPos(_NODE_WIDTH, y)
            self._ports.append(out_port)
            self._typed_ports.append(out_port)
        if y > _NODE_HEADER_H // 2:
            needed = y + _PORT_RADIUS + 4
            if needed > self._height:
                self._height = needed
                self.prepareGeometryChange()
        self._place_prim_output_ports(self._height, extend_height=True)

    def _place_param_ports(self, chunk_widget: QtWidgets.QWidget):

        chunk_widget.adjustSize()
        self._full_height = _NODE_HEADER_H + chunk_widget.height()
        self._height = self._full_height
        self.prepareGeometryChange()

        param_widgets = {
            w.param: w
            for w in chunk_widget.findChildren(QtWidgets.QWidget)
            if isinstance(w, core.ChunkParamWidgetBase)
        }
        containers_by_group = {
            w.group: w
            for w in chunk_widget.findChildren(ContainerWidget)
        }
        vector_widgets = {
            group: w for group, w in containers_by_group.items()
            if isinstance(group, core.ChunkVectorBase)
        }

        # A param inside a collapsed group has no meaningful widget position to point a
        # port at, so it falls back to that group's own header below -- and unlike the
        # normal case, several params can share one such anchor. An unconnected one gets
        # no port at all there (nothing to point at, and it declutters exactly the way
        # collapsing the whole node already does for its own unwired ports); a connected
        # one still needs one, staggered below the anchor so two sharing it don't land on
        # the same pixel -- previously they did, and whichever ended up topmost silently
        # ate every drag aimed at the other one's port.
        #
        # Checked against the chunk-level state model (has_state(COLLAPSED) walked up
        # param's own owner chain), not widget.isVisible(): a collapsed ContainerWidget
        # shrinks its content via a fixed height of 0, not .hide(), so its descendants
        # stay isVisible() == True in Qt's own terms the whole time -- that can't be used
        # as a stand-in for "the group is visually collapsed".
        _STEP = _PORT_RADIUS * 2 + 8
        connected = self._connected_param_names()
        collapsed_anchor_counts: dict = {}

        for param in _iter_connectable_params(self.chunk):
            if isinstance(param, core.ChunkVectorBase):
                widget = vector_widgets.get(param)
            else:
                widget = param_widgets.get(param)
            if widget is None:
                continue

            ptype = _param_port_type(param)

            collapsed_group = self._nearest_collapsed_group(param)
            if collapsed_group is not None:
                if param.name not in connected:
                    continue
                anchor = containers_by_group.get(collapsed_group)
                if anchor is None:
                    continue
                local = anchor.mapTo(chunk_widget, QtCore.QPoint(0, anchor.height() // 2))
                offset = collapsed_anchor_counts.get(id(anchor), 0)
                collapsed_anchor_counts[id(anchor)] = offset + 1
                local += QtCore.QPoint(0, offset * _STEP)
            elif widget.isVisible():
                if isinstance(param, core.ChunkVectorBase):
                    hdr = widget.minimize_button
                    local = hdr.mapTo(chunk_widget, QtCore.QPoint(0, hdr.height() // 2))
                else:
                    local = widget.mapTo(chunk_widget, QtCore.QPoint(0, widget.height() // 2))
            else:
                # Hidden for a reason that has nothing to do with a collapsed group (e.g.
                # a param's own HIDDEN state, like Create Layer's Index field only showing
                # for Insert == "Index") -- Qt stops laying it out once hidden, so its own
                # position is stale/meaningless rather than just off-screen, and using it
                # anyway is what put a port floating below the node entirely, disconnected
                # from anything drawn. Anchor at the nearest visible ContainerWidget
                # ancestor instead, same as the collapsed-group case above but for a
                # param hidden on its own rather than by its group.
                effective: QtWidgets.QWidget | None = None
                parent = widget.parent()
                while parent is not None and parent is not chunk_widget:
                    if isinstance(parent, ContainerWidget) and parent.isVisible():
                        effective = parent
                        break
                    parent = parent.parent()
                if effective is None:
                    continue
                local = effective.mapTo(chunk_widget, QtCore.QPoint(0, effective.height() // 2))

            y = _NODE_HEADER_H + local.y()

            in_port = ChunkPortItem(f"{self.prim.GetPath()}.inputs:{param.name}", ptype, parent=self)
            in_port.setPos(0, y)
            self._ports.append(in_port)
            self._typed_ports.append(in_port)

            out_port = ChunkPortItem(f"{self.prim.GetPath()}.outputs:{param.name}", ptype, parent=self)
            out_port.setPos(_NODE_WIDTH, y)
            self._ports.append(out_port)
            self._typed_ports.append(out_port)

        self._place_prim_output_ports(self._full_height, extend_height=True)

    def _sync_vector_states(self):
        """Disable vector params whose inputs:* attr is connected; enable the rest."""
        for vec in _iter_all_vectors(self.chunk):
            attr = self.prim.GetAttribute(f"inputs:{vec.name}")
            connected = attr.IsValid() and bool(attr.GetConnections())
            if connected:
                vec.set_state(ChunkFlags.State.DISABLED)
            else:
                vec.unset_state(ChunkFlags.State.DISABLED)

    def _apply_usd_pos(self, prim: Usd.Prim):
        # A position notice can arrive for a node whose cached prim is already invalid
        # (e.g. a stale node removed but not yet cleared by the deferred rebuild). Schemas
        # raise on an invalid prim, so bail.
        if not prim or not prim.IsValid():
            return
        ui_api = UsdUI.NodeGraphNodeAPI(prim)
        pos_attr = ui_api.GetPosAttr()
        if pos_attr and pos_attr.HasAuthoredValue():
            p = pos_attr.Get()
            # USD Y-up → Qt Y-down
            self.setPos(p[0], -p[1])

    # ------------------------------------------------------------------
    # Port lookup

    def get_port(self, attr_path: str) -> Optional["ChunkPortItem"]:
        for port in self._ports:
            if port.attr_path == attr_path:
                return port
        return None

    # ------------------------------------------------------------------
    # QGraphicsItem interface

    def boundingRect(self) -> QtCore.QRectF:
        if self._is_dot:
            pad = _PORT_RADIUS
            return QtCore.QRectF(-pad, -pad, _DOT_R * 2 + pad * 2, _DOT_R * 2 + pad * 2)
        return QtCore.QRectF(
            -_PORT_RADIUS, 0,
            _NODE_WIDTH + _PORT_RADIUS * 2,
            self._height,
        )

    def _is_active_view(self) -> bool:
        """True if this is the view node whose slot is currently active (driving execution)."""
        if not self._is_view or not self.prim.IsValid():
            return False
        slot = self.prim.GetAttribute("view:slot").Get()
        if not slot:
            return False
        sc = self.scene()
        if sc is None:
            return False
        for v in sc.views():
            vs = getattr(v, "_view_state", None)
            if vs is not None and vs.active_slot == slot:
                return True
        return False

    def paint(self, painter: QtGui.QPainter, option, widget=None):
        # The chunk may already be deleted while this node awaits its deferred removal
        # (see ChunkGraphScene._on_chunk_deleted); painting reads live prim attributes,
        # so skip it rather than touch an expired prim.
        if not self.prim.IsValid():
            return
        if self._is_dot:
            color = _COL_NODE_SELECTED if self._selected else _COL_PORT_EXEC
            painter.setBrush(color)
            painter.setPen(QtCore.Qt.NoPen)
            painter.drawEllipse(QtCore.QPointF(_DOT_R, _DOT_R), _DOT_R, _DOT_R)
            if self._selected:
                painter.setBrush(QtCore.Qt.NoBrush)
                painter.setPen(QtGui.QPen(_COL_NODE_SELECTED, 1.5))
                painter.drawEllipse(QtCore.QPointF(_DOT_R, _DOT_R), _DOT_R + 3, _DOT_R + 3)
            return

        if self._lod_simple:
            # Too far out for the title to be legible or the 4px corner radius to survive
            # rounding: two flat rects per node, no text, no font construction, no prim
            # reads. Disabled nodes dim through blended colours here -- see _apply_dim.
            head = _COL_NODE_HEADER
            if self._is_view:
                head = _COL_VIEW_HEADER if self._is_active_view() else _COL_VIEW_INACTIVE
            elif self._is_chunkgroup:
                head = _COL_GROUP_HEADER
            elif self._locked:
                head = _COL_LOCKED_HEADER
            bg = _COL_LOCKED_BG if self._locked else _COL_NODE_BG
            border = _COL_NODE_SELECTED if self._selected else _COL_NODE_BORDER
            if self._disabled:
                bg, head, border = _dimmed(bg), _dimmed(head), _dimmed(border)
            painter.setBrush(bg)
            painter.setPen(QtGui.QPen(border, 1.5))
            painter.drawRect(QtCore.QRectF(0, 0, _NODE_WIDTH, self._height))
            painter.setBrush(head)
            painter.setPen(QtCore.Qt.NoPen)
            painter.drawRect(QtCore.QRectF(0, 0, _NODE_WIDTH, _NODE_HEADER_H))
            return

        bg_color = _COL_LOCKED_BG if self._locked else _COL_NODE_BG
        body = QtCore.QRectF(0, 0, _NODE_WIDTH, self._height)
        # The active view (the one currently driving execution) gets a green, thicker border
        # so it's obvious which of several view nodes is live.
        is_active_view = self._is_view and self._is_active_view()
        border_color = _COL_NODE_SELECTED if self._selected else _COL_NODE_BORDER
        border_w = 1.5
        if is_active_view:
            border_color = _COL_VIEW_ACTIVE
            border_w = 2.5

        painter.setBrush(bg_color)
        painter.setPen(QtGui.QPen(border_color, border_w))
        painter.drawRoundedRect(body, 4, 4)

        # Header background — square bottom corners so it merges with body
        if self._is_view:
            # Only the active view gets the warm orange header; inactive views go dim/grey
            # so it's obvious at a glance which one is driving execution.
            header_color = _COL_VIEW_HEADER if is_active_view else _COL_VIEW_INACTIVE
        elif self._is_chunkgroup:
            header_color = _COL_GROUP_HEADER
        elif self._locked:
            header_color = _COL_LOCKED_HEADER
        else:
            header_color = _COL_NODE_HEADER
        painter.setBrush(header_color)
        painter.setPen(QtCore.Qt.NoPen)
        header = QtCore.QRectF(0, 0, _NODE_WIDTH, _NODE_HEADER_H)
        painter.drawRoundedRect(header, 4, 4)
        # Cover the rounded bottom of the header rect
        painter.drawRect(QtCore.QRectF(0, 10, _NODE_WIDTH, _NODE_HEADER_H - 10))

        name_color = _COL_TEXT_LOCKED if self._locked else _COL_TEXT_PRIMARY
        painter.setPen(QtGui.QPen(name_color))
        painter.setFont(QtGui.QFont("Arial", 9, QtGui.QFont.Bold))
        label = self.chunk.name
        if self._is_view:
            slot = self.prim.GetAttribute("view:slot").Get()
            label = f"View [{slot}]" if slot is not None else "View"
            if is_active_view:
                label = "● " + label  # ● marks the active view
        elif self._is_chunkgroup:
            label = f"{label}  ▶"
        elif self._locked:
            label = f"{label}  \U0001f512"
        if self._pinned:
            label = f"{label}  \U0001f4cc"
        painter.drawText(
            QtCore.QRectF(8, 4, _NODE_WIDTH - 16, 18),
            QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
            label,
        )

        chunk_type = self._chunk_type_label
        sub_color = _COL_TEXT_LOCKED if self._locked else _COL_TEXT_SECONDARY
        painter.setPen(QtGui.QPen(sub_color))
        painter.setFont(QtGui.QFont("Arial", 8))
        painter.drawText(
            QtCore.QRectF(8, 22, _NODE_WIDTH - 16, 14),
            QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
            str(chunk_type),
        )

        # Stale badge — a small fixed-position corner icon rather than a border-color swap,
        # so it never fights with the selected (gold) or active-view (green) border colors.
        if self._stale:
            badge_r = 7.0
            badge_c = QtCore.QPointF(_NODE_WIDTH - 15, 13)
            painter.setBrush(_COL_STALE)
            painter.setPen(QtCore.Qt.NoPen)
            painter.drawEllipse(badge_c, badge_r, badge_r)
            painter.setPen(QtGui.QPen(_COL_NODE_HEADER))
            painter.setFont(QtGui.QFont("Arial", 9, QtGui.QFont.Bold))
            painter.drawText(
                QtCore.QRectF(badge_c.x() - badge_r, badge_c.y() - badge_r, badge_r * 2, badge_r * 2),
                QtCore.Qt.AlignCenter,
                "↻",
            )

        # Typed outputs section — separator line + darker strip
        if self._outputs_start_y > _NODE_HEADER_H and self._height > self._outputs_start_y:
            outputs_rect = QtCore.QRectF(
                0, self._outputs_start_y,
                _NODE_WIDTH, self._height - self._outputs_start_y,
            )
            painter.setBrush(QtGui.QColor("#2a2a2a"))
            painter.setPen(QtCore.Qt.NoPen)
            painter.drawRect(outputs_rect)
            painter.setPen(QtGui.QPen(QtGui.QColor("#555555"), 1))
            painter.drawLine(
                QtCore.QPointF(0, self._outputs_start_y),
                QtCore.QPointF(_NODE_WIDTH, self._outputs_start_y),
            )

    def mouseDoubleClickEvent(self, event):
        # A recomposition (mute/unmute) can expire this node's prim before the graph
        # rebuilds; touching it then raises. Bail rather than crash the event handler.
        if self.prim is None or not self.prim.IsValid():
            event.accept()
            return
        if self._is_dot:
            event.accept()
            return
        if event.pos().y() <= _NODE_HEADER_H:
            if self._is_view:
                slot = self.prim.GetAttribute("view:slot").Get()
                if slot is not None:
                    for view in (self.scene().views() if self.scene() else []):
                        if isinstance(view, ChunkGraphView):
                            view._view_state.set_slot(slot)
                            break
                event.accept()
                return
            # Debounced: a single real double-click has been observed (live log) delivering
            # this handler twice in a row, toggling COLLAPSED on then straight back off --
            # net no-op, so the node looks permanently stuck. See _COLLAPSE_TOGGLE_DEBOUNCE.
            now = time.monotonic()
            debounced = now - self._last_collapse_toggle_time < _COLLAPSE_TOGGLE_DEBOUNCE
            if self._is_chunkgroup:
                # A collapsed group hides its whole embedded widget — including the minimize
                # arrow — so the header double-click is the only way to expand it in the graph.
                # Collapsed -> expand; expanded -> dive in.
                if self.chunk.has_state(ChunkFlags.State.COLLAPSED):
                    if not debounced:
                        self._last_collapse_toggle_time = now
                        self.chunk.unset_state(ChunkFlags.State.COLLAPSED)
                    event.accept()
                    return
                scene = self.scene()
                if scene and hasattr(scene, "dive_into"):
                    scene.dive_into.emit(self.chunk)
                event.accept()
                return
            if not self._locked and not debounced:
                self._last_collapse_toggle_time = now
                if self.chunk.has_state(ChunkFlags.State.COLLAPSED):
                    self.chunk.unset_state(ChunkFlags.State.COLLAPSED)
                else:
                    self.chunk.set_state(ChunkFlags.State.COLLAPSED)
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            scene = self.scene()
            sender = getattr(scene, "_scene_sender", None) if scene else None
            modifiers = event.modifiers()
            toggle = bool(modifiers & (QtCore.Qt.ControlModifier | QtCore.Qt.ShiftModifier))
            already_selected = self.chunk.has_state(ChunkFlags.State.SELECTED)
            self._drag_pending_collapse = False
            if sender:
                if toggle:
                    current = list(sender.get_selected_chunks())
                    if self.chunk in current:
                        current.remove(self.chunk)
                    else:
                        current.append(self.chunk)
                    sender.set_selected_chunks(current)
                elif not already_selected:
                    # Plain click on a node not already part of the selection replaces it.
                    sender.set_selected_chunks([self.chunk])
                else:
                    # Plain click on a node that's already part of a (possibly multi-node)
                    # selection: keep the whole selection for now (so it can be drag-moved as
                    # a group) and only collapse to just this node if the click ends up being
                    # a plain click with no drag movement — see mouseReleaseEvent.
                    self._drag_pending_collapse = True
            # A merge has no embedded widget, so its whole body is dead space — dragging is
            # the only thing it could mean. Restricting it to the header would leave a tall
            # merge grabbable only by its top 40px. (Ports are child items and are hit first
            # by the view's own handler, so they still start a wire drag.)
            drag_zone = self._height if self._is_merge else _NODE_HEADER_H
            if event.pos().y() <= drag_zone and not self._locked:
                self.setFocus()
                self._drag_active = True
                self._drag_moved = False
                self._drag_start = event.pos()
                self._shake_samples = []
                self._shake_fired = False
                self._drag_companions = []
                if isinstance(scene, ChunkGraphScene):
                    self._drag_companions = [
                        node for node in scene._nodes.values()
                        if node is not self and not node._locked
                        and node.chunk.has_state(ChunkFlags.State.SELECTED)
                    ]
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_active:
            delta = event.pos() - self._drag_start
            self.setPos(self.pos() + delta)
            for companion in self._drag_companions:
                try:
                    companion.setPos(companion.pos() + delta)
                except RuntimeError:
                    continue
            if not self._drag_moved and (abs(delta.x()) + abs(delta.y())) > 2:
                self._drag_moved = True
            self._track_shake()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def _track_shake(self) -> None:
        """Detect a fast back-and-forth drag and, once, disconnect all of this node's wires."""
        if self._shake_fired:
            return
        now = time.monotonic()
        self._shake_samples.append((now, self.pos().x()))
        cutoff = now - _SHAKE_WINDOW
        while self._shake_samples and self._shake_samples[0][0] < cutoff:
            self._shake_samples.pop(0)
        reversals = 0
        travel = 0.0
        last_dir = 0
        prev_x: Optional[float] = None
        for _, x in self._shake_samples:
            if prev_x is not None:
                dx = x - prev_x
                if abs(dx) >= _SHAKE_MIN_STEP:
                    travel += abs(dx)
                    d = 1 if dx > 0 else -1
                    if last_dir and d != last_dir:
                        reversals += 1
                    last_dir = d
                    prev_x = x
            else:
                prev_x = x
        if reversals >= _SHAKE_REVERSALS and travel >= _SHAKE_MIN_TRAVEL:
            self._shake_fired = True
            scene = self.scene()
            if isinstance(scene, ChunkGraphScene) and scene.views():
                scene.views()[0]._shake_disconnect_node(self)

    def mouseReleaseEvent(self, event):
        if self._drag_active:
            self._drag_active = False
            # Only persist the move / consider a wire-splice if the node actually moved —
            # a plain click on the header must not splice the node into an overlapping wire.
            if self._drag_moved:
                self._write_pos_to_usd()
                # Moving a node changes what is stacked on what, in both directions: it can
                # bury another node, or free itself from a pile.
                for view in (self.scene().views() if self.scene() is not None else []):
                    if hasattr(view, "_apply_lod"):
                        view._apply_lod()
                for companion in self._drag_companions:
                    try:
                        companion._write_pos_to_usd()
                    except RuntimeError:
                        continue
                self._try_insert_on_edge()
            elif self._drag_pending_collapse:
                # A plain click (no movement) on a node that was already part of a multi-node
                # selection — collapse the selection down to just this node now.
                scene = self.scene()
                sender = getattr(scene, "_scene_sender", None) if scene else None
                if sender:
                    sender.set_selected_chunks([self.chunk])
            self._drag_companions = []
            self._drag_pending_collapse = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _try_insert_on_edge(self) -> None:
        """If this node was dropped on top of a wire A->B, splice it in as A->self->B."""
        scene = self.scene()
        if not isinstance(scene, ChunkGraphScene) or not scene.views():
            return
        view = scene.views()[0]
        my_rect = self.sceneBoundingRect()
        for edge in list(scene._edges):
            # Skip edges already attached to this node.
            if edge.src_port in self._ports or edge.dst_port in self._ports:
                continue
            try:
                on_wire = edge.path().intersects(my_rect)
            except RuntimeError:
                continue
            if not on_wire:
                continue
            a_out, b_in = edge.src_port, edge.dst_port  # wire is A.output -> B.input
            my_in = next(
                (p for p in self._ports if p.is_input and view._can_connect(a_out, p)), None
            )
            my_out = next(
                (p for p in self._ports if not p.is_input and view._can_connect(p, b_in)), None
            )
            if my_in is None or my_out is None:
                continue
            # A -> self, then self -> B (the second replaces B's old input from A).
            view._author_connection(a_out, my_in)
            view._author_connection(my_out, b_in)
            return

    def itemChange(self, change, value):
        if change == QtWidgets.QGraphicsItem.ItemPositionHasChanged:
            scene = self.scene()
            if scene:
                scene.update_edges_for(self)
        return super().itemChange(change, value)

    # ------------------------------------------------------------------
    # Internal

    def _write_pos_to_usd(self):
        p = self.pos()
        ui_api = UsdUI.NodeGraphNodeAPI(self.prim)
        pos_attr = ui_api.GetPosAttr() or ui_api.CreatePosAttr()
        # Chunk-prim bookkeeping (this attr) follows Edit Target now, not always the
        # session layer — writing without an EditContext relies on the stage's ambient
        # edit target, which may not match wherever this attr's spec actually lives
        # (or, for the very first write, wherever it *should* go). Update an existing
        # spec in place; only resolve a fresh target for a genuinely new one.
        stage = self.prim.GetStage()
        home = chunk_utils.get_attr_home_layer(pos_attr)
        target = home or chunk_utils.get_bookkeeping_layer(stage, getattr(self, "chunk", None))
        with Usd.EditContext(stage, target):
            pos_attr.Set(Gf.Vec2f(p.x(), -p.y()))

    def _set_collapsed(self, collapsed: bool):
        self.prepareGeometryChange()
        if self._proxy is not None:
            self._proxy.setVisible(not collapsed)
        if collapsed:
            # Rebuild rather than reposition: the old code moved every existing typed
            # port straight onto the header centre point, which is also where the exec
            # ports already live -- every data port for every param, connected or not,
            # ended up stacked on that one pixel along with them. Whichever port item
            # was topmost then ate all hover/click, so a wire could look like it landed
            # on the wrong port entirely, and the node never grew to fit anything either.
            # A full rebuild both drops unwired ports (nothing to show) and spaces the
            # wired ones out (see _place_param_ports_collapsed), same as an expand does.
            self._rebuild_typed_ports()
        else:
            self._height = self._full_height
            if self._port_rebuild_timer is not None:
                # Recompute port positions from widget geometry now that proxy is visible.
                self._port_rebuild_timer.start()
        self.update()

    def _on_state_changed(self, notice, sender):
        # This is a global STATE_CHANGED callback and can arrive after the node's chunk was
        # removed — the node's C++ item (and its proxy) may already be destroyed, so any Qt
        # call raises. Nothing to update in that case.
        try:
            selected = self.chunk.has_state(ChunkFlags.State.SELECTED)
            if selected != self._selected:
                self._selected = selected
                self.update()
            stale = self.chunk.has_state(ChunkFlags.State.STALE)
            if stale != self._stale:
                self._stale = stale
                self._update_stale_tooltip()
                self.update()
            self._set_collapsed(self.chunk.has_state(ChunkFlags.State.COLLAPSED))
            disabled = self.chunk.has_state(ChunkFlags.State.DISABLED)
            if disabled != self._disabled:
                self._disabled = disabled
                self.update()
            self._apply_dim()
        except RuntimeError:
            return

    def set_body_visible(self, visible: bool) -> None:
        """Switch the node between full detail and a cheap far-zoom representation.

        Detail is dropped when `visible` is False, in descending order of what it cost to
        paint: the embedded ChunkWidget, then the port dots (~33 per node here, and edges
        position off their scenePos rather than their visibility, so wires stay put), then
        the title/subtitle text and rounded corners in paint().

        The node keeps its geometry throughout, so edges, auto-layout and framing all still
        measure the same box.
        """
        proxy = self._proxy
        try:
            # Don't pull the rug out from under someone mid-edit: a focused body means a
            # field is being typed into, which can only be true at a readable zoom anyway.
            if proxy is not None and not visible and proxy.hasFocus():
                return
            if not visible and proxy is not None and not self.is_height_settled():
                # Still laying out. Hiding it now would strand the node at a partial height
                # -- see is_height_settled. _on_height_settled re-runs LOD once it is safe,
                # so nothing is lost by declining here.
                return
            # Skip only when the flag AND the proxy agree. They can diverge: a body that
            # declined an earlier hide, or one whose proxy was rebuilt under it, leaves the
            # flag saying flat while the widget is still up, and trusting the flag alone
            # then strands it visible forever (41 of 56 buried nodes, measured).
            if self._lod_simple == (not visible) and (
                proxy is None or proxy.isVisible() == visible
            ):
                return
            self._lod_simple = not visible
            if proxy is not None:
                proxy.setVisible(visible)
            for port in self._ports:
                port.setVisible(visible)
            self._apply_dim()
            self.update()
        except RuntimeError:
            return

    def _apply_dim(self) -> None:
        """Dim a disabled node: through item opacity while its body shows, through blended
        colours once it doesn't.

        The body is a real widget, so nothing but opacity will fade it. Without one there is
        only paint(), which can dim far more cheaply -- see _dimmed.
        """
        try:
            self.setOpacity(0.35 if (self._disabled and not self._lod_simple) else 1.0)
        except RuntimeError:
            return

    def _update_stale_tooltip(self) -> None:
        self.setToolTip(
            "Needs recook — inputs changed since this last ran. Press Run/Recompute to update it."
            if self._stale
            else ""
        )

    def _build_param_state_conns(self) -> "list[NoticeConnection]":
        """One STATE_CHANGED listener per connectable param, so a param's own HIDDEN
        toggling triggers a port rebuild the same way PARAM_ADDED/PARAM_REMOVED already
        do. Rebuilt (not just added to) on every param add/remove -- see _on_param_added --
        since a param.get_attribute_sender("state") for a since-removed param is dead
        weight, and a newly added one needs its own connection to begin with.
        """
        return [
            NoticeConnection(
                self._on_param_state_changed, ChunkNoticeAlias.STATE_CHANGED, param.get_attribute_sender("state")
            )
            for param in _iter_connectable_params(self.chunk)
        ]

    def _kick_port_rebuild(self):
        if self._port_rebuild_timer is not None:
            self._port_rebuild_timer.start()
        else:
            # Proxy not built yet -- create_proxy()'s own initial _rebuild_typed_ports()
            # call will pick up the change once it runs (_rebuild_typed_ports no-ops
            # safely if the proxy still isn't there by then). Bare singleShot for the same
            # reason as above: a QGraphicsItem cannot be a timer context object, and the
            # callee guards itself.
            QtCore.QTimer.singleShot(0, self._rebuild_typed_ports)

    def _on_param_added(self, notice, sender):
        # Same defensive pattern as _on_state_changed: this is a global per-chunk callback
        # and can arrive after the node/proxy has already been torn down.
        try:
            # The param set changed, so the per-param state listeners need refreshing too
            # -- a removed param's connection is dead weight, and a newly added one needs
            # its own connection to have its later HIDDEN toggles noticed at all.
            self._param_state_conns = self._build_param_state_conns()
            self._kick_port_rebuild()
        except RuntimeError:
            return

    def _on_param_state_changed(self, notice, sender):
        # Just the rebuild -- the param SET didn't change here, only one of their STATE
        # bits, so there's nothing for _build_param_state_conns to do (and rebuilding the
        # very list this connection lives in from inside its own dispatch would be an
        # unnecessary, self-referential churn for every single HIDDEN toggle).
        try:
            self._kick_port_rebuild()
        except RuntimeError:
            return


# ---------------------------------------------------------------------------
# Edge

class ChunkEdgeItem(QtWidgets.QGraphicsPathItem):
    """Cubic bezier connecting two ChunkPortItems."""

    _EXP_RADIUS = 12

    def __init__(
        self,
        src_port: "ChunkPortItem",
        dst_port: "ChunkPortItem",
        expression: str | None = None,
    ):
        super().__init__()
        self.src_port = src_port
        self.dst_port = dst_port
        self._expression = expression
        self._p1 = QtCore.QPointF()
        self._p2 = QtCore.QPointF()
        self.setPen(QtGui.QPen(src_port.brush().color(), 1.5))
        self.setZValue(-1)
        if expression:
            self.setAcceptHoverEvents(True)
            self.setToolTip(expression)
        self.update_path()

    def update_path(self):
        self.prepareGeometryChange()
        self._p1 = self.src_port.scenePos()
        self._p2 = self.dst_port.scenePos()
        p1, p2 = self._p1, self._p2
        dx = abs(p2.x() - p1.x()) * 0.5
        path = QtGui.QPainterPath(p1)
        path.cubicTo(p1.x() + dx, p1.y(), p2.x() - dx, p2.y(), p2.x(), p2.y())
        self.setPath(path)

    def boundingRect(self) -> QtCore.QRectF:
        rect = super().boundingRect()
        if self._expression:
            r = self._EXP_RADIUS
            mid = QtCore.QPointF(
                (self._p1.x() + self._p2.x()) / 2,
                (self._p1.y() + self._p2.y()) / 2,
            )
            rect = rect.united(QtCore.QRectF(mid.x() - r, mid.y() - r, r * 2, r * 2))
        return rect

    def paint(self, painter: QtGui.QPainter, option, widget=None):
        super().paint(painter, option, widget)
        if not self._expression:
            return
        r = self._EXP_RADIUS
        mid = QtCore.QPointF(
            (self._p1.x() + self._p2.x()) / 2,
            (self._p1.y() + self._p2.y()) / 2,
        )
        painter.setBrush(self.pen().color())
        painter.setPen(QtCore.Qt.NoPen)
        painter.drawEllipse(mid, r, r)
        painter.setPen(QtGui.QPen(QtGui.QColor("white")))
        painter.setFont(QtGui.QFont("Arial", 7, QtGui.QFont.Bold))
        painter.drawText(
            QtCore.QRectF(mid.x() - r, mid.y() - r, r * 2, r * 2),
            QtCore.Qt.AlignCenter,
            "Exp",
        )


# ---------------------------------------------------------------------------
# Scene

class ChunkGraphScene(QtWidgets.QGraphicsScene):
    """QGraphicsScene backed by a USD stage of Chunk prims."""

    _PROXY_BATCH_SIZE = 5  # ChunkWidgets created per event-loop tick

    dive_into = QtCore.Signal(object)  # emitted with the CONTEXT ChunkBase when a group node is double-clicked

    def __init__(self, parent=None):
        super().__init__(parent)
        # Disable the BSP spatial index. clear() (used on every rebuild) can leave a
        # destroyed QGraphicsProxyWidget dangling in the BSP index, so a later itemAt()
        # returns freed memory and usdview crashes on the next click. NoIndex makes itemAt
        # linearly scan the live item list instead — fine for the small node counts here.
        self.setItemIndexMethod(QtWidgets.QGraphicsScene.ItemIndexMethod.NoIndex)
        # Graph-wide height settling -- see heights_settled.
        self._heights_settled = False
        self._height_settle_timer = None
        self._build_complete = False
        self._stage: Optional[Usd.Stage] = None
        self._scene_sender: Optional[core.SceneSender] = None
        self._nodes: dict[str, ChunkNodeItem] = {}  # prim path → node
        self._edges: list[ChunkEdgeItem] = []
        self._objects_changed_conn: Optional[NoticeConnection] = None
        self._show_param_edges: bool = True
        self._built_context: object = _UNBUILT  # sentinel — tracks what context is currently rendered
        self._dirty: bool = True  # set by invalidate(); cleared by a (re)build
        self._edge_timer: Optional[QtCore.QTimer] = None

    def set_show_param_edges(self, show: bool):
        self._show_param_edges = show
        self._build_edges()

    def invalidate(self):
        # Mark that a rebuild is needed WITHOUT clobbering _built_context. The scene's own
        # notice-driven rebuild reads _built_context; nulling it to the sentinel here caused
        # deletes to rebuild with no context and produce an empty graph until a tab-toggle.
        self._dirty = True

    def build(self, stage: Usd.Stage, scene_sender: core.SceneSender, context=None):
        self._built_context = context
        self._dirty = False
        self._stage = stage
        self._scene_sender = scene_sender

        self._objects_changed_conn = NoticeConnection(
            self._on_objects_changed, ChunkNoticeAlias.OBJECT_CHANGED, globally=True
        )
        self._chunk_deleted_conn = NoticeConnection(
            self._on_chunk_deleted, ChunkNoticeAlias.CHUNK_DELETED, globally=True
        )

        self._rebuild_nodes()

    def _schedule_rebuild(self):
        """Queue one deferred, coalesced rebuild.

        A structural change (add/remove/enable/disable) fires a burst of notices; debouncing
        collapses them into a single rebuild, and deferring past the current notice means we
        never destroy an embedded widget while its callback is still on the stack.
        """
        if getattr(self, "_rebuild_pending", False):
            return
        self._rebuild_pending = True
        QtCore.QTimer.singleShot(0, self, self._do_rebuild)

    def _do_rebuild(self):
        self._rebuild_pending = False
        self._rebuild_nodes()

    def _teardown_items(self):
        # Detach embedded widgets before their proxies are destroyed. QGraphicsScene.clear()
        # tearing down live QGraphicsProxyWidgets (with complex embedded ChunkWidgets) can
        # corrupt the scene, so a later itemAt()/click reads freed memory and crashes usdview.
        # setWidget(None) hands each widget back and we delete it separately, so clear() only
        # destroys now-empty proxies.
        for node in self._nodes.values():
            proxy = getattr(node, "_proxy", None)
            if proxy is None:
                continue
            try:
                w = proxy.widget()
                proxy.setWidget(None)
                if w is not None:
                    # A visible widget reparented to no parent becomes a real top-level
                    # window and Qt shows it immediately — set this before setParent(None)
                    # or every rebuild flashes one native window per node (see create_proxy).
                    w.setAttribute(QtCore.Qt.WA_DontShowOnScreen)
                    w.setParent(None)
                    w.deleteLater()
            except RuntimeError:
                pass
        self.clear()

    def _teardown_node(self, node: "ChunkNodeItem") -> None:
        """Same detach-then-destroy sequence as _teardown_items, but for one node — used by the
        incremental path in _rebuild_nodes so a node that's actually gone doesn't wait for (or
        force) a full-scene wipe."""
        proxy = getattr(node, "_proxy", None)
        if proxy is not None:
            try:
                w = proxy.widget()
                proxy.setWidget(None)
                if w is not None:
                    w.setParent(None)
                    w.deleteLater()
            except RuntimeError:
                pass
        try:
            self.removeItem(node)
        except RuntimeError:
            pass

    def _rebuild_nodes(self):
        """Reconcile nodes/edges with the current chunk prims.

        Safe to call on any structural change. Deleted chunks drop out (their prim is gone);
        disabled chunks stay (their prim is inactive but still included via _INCLUDE_INACTIVE)
        and render greyed.

        A node whose chunk is still present and unchanged is REUSED, not torn down and
        recreated — tearing down and rebuilding every node's ChunkWidget proxy (a native Qt
        window under the hood) on every single structural change, even for one unrelated
        new/deleted node, made the whole graph visibly flash/collapse/expand and could show a
        genuine OS-level popup-window flicker (reported live): create_proxy()'s "never let the
        OS show a top-level window" workaround was being exercised once per EXISTING node, every
        time, not just for the node that actually changed. Only nodes whose chunk no longer
        exists are torn down, and only nodes that are newly created get a new proxy built.
        """
        if not self._stage or not self._scene_sender:
            return
        stage = self._stage
        scene_sender = self._scene_sender
        context = self._built_context
        if context is _UNBUILT:
            return  # not built yet — a real build() will render with a proper context

        # Derive the prim root for this context
        if context is not None and getattr(context, "prim_path", None):
            chunks_root_path = chunk_usd._resolve_chunk_root(context)
        else:
            chunks_root_path = Sdf.Path(_CHUNKS_ROOT)

        self._chunks_root_path = chunks_root_path

        # Check the root is valid BEFORE tearing anything down. A disable/delete recomposition
        # can leave /chunks transiently invalid; if we wiped first and then bailed, the graph
        # would be emptied and never rebuilt. Keep the current graph and retry once it settles.
        chunks_root = stage.GetPrimAtPath(chunks_root_path)
        if not chunks_root.IsValid():
            # /chunks is invalid. Distinguish "no chunks left" (e.g. the last chunk was
            # removed, so /chunks legitimately went away) from a transient recomposition.
            # If no chunks are expected here, clear the graph; otherwise keep it and retry so
            # we don't wipe a live graph mid-recomposition (which would leave it empty).
            expected = any(c.get_context() == context for c in scene_sender.get_chunks())
            if not expected:
                self._rebuild_retries = 0
                self._teardown_items()
                self._nodes.clear()
                self._edges.clear()
                return
            retries = getattr(self, "_rebuild_retries", 0)
            if retries < 20:
                self._rebuild_retries = retries + 1
                QtCore.QTimer.singleShot(0, self, self._do_rebuild)
            return
        self._rebuild_retries = 0

        self._dirty = False
        # Bump the build generation so any in-flight deferred proxy batch from a superseded
        # rebuild aborts instead of creating a ghost proxy on a node that got torn down below.
        self._build_gen = getattr(self, "_build_gen", 0) + 1

        chunks_by_path = {
            f"{chunk_usd._resolve_chunk_root(c)}/{c.name}": c
            for c in scene_sender.get_chunks()
            # Exclude the context chunk itself: get_context() returns self for a CONTEXT chunk,
            # so a group would otherwise map to "{its own prim}/{its name}" — the path of a child
            # inside it — and shadow a real nested chunk of the same name (backing that node with
            # the wrong chunk, so double-clicking it dives into the parent instead of the child).
            if c.get_context() == context and c is not context
        }

        # Also include CONTEXT group chunks whose prims live directly under this root
        for prim in chunks_root.GetFilteredChildren(_INCLUDE_INACTIVE):
            path = str(prim.GetPath())
            if path not in chunks_by_path:
                ctx_chunk = scene_sender.get_context_for_prim_path(path)
                if ctx_chunk is not None:
                    chunks_by_path[path] = ctx_chunk

        # Pre-compute lock state: a prim is locked when authored in a layer that isn't
        # the session layer, root layer, or current edit target.
        root_id = stage.GetRootLayer().identifier
        edit_id = stage.GetEditTarget().GetLayer().identifier

        desired_paths: set[str] = set()
        new_nodes: list[ChunkNodeItem] = []
        for prim in chunks_root.GetFilteredChildren(_INCLUDE_INACTIVE):
            if prim.GetTypeName() != _CHUNK_PRIM_TYPENAME:
                continue
            path = str(prim.GetPath())
            chunk = chunks_by_path.get(path)
            if chunk is None:
                continue
            desired_paths.add(path)

            src_layer = next(
                (spec.layer for spec in prim.GetPrimStack()
                 if not spec.layer.anonymous and spec.layer.identifier != root_id),
                None,
            )
            locked = src_layer is not None and src_layer.identifier != edit_id

            existing = self._nodes.get(path)
            if existing is not None and existing.chunk is chunk:
                # Same chunk, same node — refresh what a fresh construction would have picked
                # up (the prim handle itself may have been recomposed) and keep everything
                # else, including its proxy widget, exactly as-is.
                try:
                    existing.prim = prim
                    existing.set_locked(locked)
                    # A node can be left without one: _create_proxies_batch defers a node's
                    # create_proxy() call across event-loop ticks, and abandons whatever is
                    # still queued (no reschedule) the moment a newer rebuild supersedes it
                    # (_build_gen mismatch, right below). If that node already exists here on
                    # the newer rebuild, this branch is normally a no-op that never revisits
                    # its proxy -- silently orphaning it forever with no exception logged,
                    # stuck at header height since create_proxy() never ran. One file load can
                    # fire several rebuilds (each wiring/reactive pass gets its own), so this
                    # is not just a theoretical race. Queue it again rather than leave it dark.
                    if existing._proxy is None and not existing._is_dot and not existing._is_merge:
                        new_nodes.append(existing)
                except RuntimeError:
                    existing = None  # its C++ item was already destroyed somehow — rebuild it
            else:
                existing = None

            if existing is None:
                if path in self._nodes:
                    self._teardown_node(self._nodes[path])
                node = ChunkNodeItem(chunk, prim, locked=locked)
                self.addItem(node)
                self._nodes[path] = node
                new_nodes.append(node)

        # Remove nodes for chunks that no longer exist under this root.
        for path in [p for p in self._nodes if p not in desired_paths]:
            self._teardown_node(self._nodes.pop(path))

        # Exec edges are built immediately (exec ports always exist).
        self._build_edges()

        # Proxy widgets (ChunkWidget) are created in small batches so newly-added nodes fill
        # in progressively — existing nodes already have theirs and are left untouched.
        if new_nodes:
            self._create_proxies_batch(new_nodes, self._build_gen)

    def _create_proxies_batch(self, remaining: list, gen: int):
        if gen != getattr(self, "_build_gen", gen):
            return  # a newer rebuild superseded this batch — don't touch its stale nodes
        batch, rest = remaining[:self._PROXY_BATCH_SIZE], remaining[self._PROXY_BATCH_SIZE:]
        for node in batch:
            # Never let one bad proxy abort the whole batch (which would leave every remaining
            # node without its widget — the "only the port dot shows" corruption). Log and skip.
            try:
                node.create_proxy()
            except Exception as e:
                print(f"[PROXY] create_proxy FAILED for {getattr(node.chunk, 'name', '?')}: "
                      f"{type(e).__name__}: {e}")
        if rest:
            QtCore.QTimer.singleShot(0, self, lambda: self._create_proxies_batch(rest, gen))
            return
        # Every proxy exists, so heights can now start counting toward settled; the countdown
        # runs from here and _on_heights_settled applies the body decision once it expires.
        self._build_complete = True
        self.note_height_change()

    def heights_settled(self) -> bool:
        """True once the graph is fully built AND no node has resized for _HEIGHT_SETTLE_MS.

        Both halves are needed. Proxies are constructed in deferred batches, and constructing
        one measures nothing -- so mid-build there are quiet gaps longer than the window, and
        a quiet period alone reported "settled" while a third of the graph had not been laid
        out yet.
        """
        return self._build_complete and self._heights_settled

    def note_height_change(self) -> None:
        """Called by a node whose height just changed; restarts the graph-wide countdown."""
        self._heights_settled = False
        timer = self._height_settle_timer
        if timer is None:
            timer = QtCore.QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(_HEIGHT_SETTLE_MS)
            timer.timeout.connect(self._on_heights_settled)
            self._height_settle_timer = timer
        timer.start()

    def _on_heights_settled(self) -> None:
        self._heights_settled = True
        # Whatever was held off waiting for real heights can now run.
        for view in self.views():
            if hasattr(view, "_apply_lod"):
                view._apply_lod()

    def _request_build_edges(self):
        """Coalesce edge rebuilds onto one deferred pass.

        _build_edges tears down and re-walks every node's attributes, and each node's port
        rebuild used to trigger its own. Creating one 64-param shader ran it 1031 times for
        ~1s of a 6s create. A burst of port changes only needs the one rebuild that follows
        it, so collapse them onto a zero-timer.
        """
        if self._edge_timer is None:
            self._edge_timer = QtCore.QTimer()
            self._edge_timer.setSingleShot(True)
            self._edge_timer.setInterval(0)
            self._edge_timer.timeout.connect(self._build_edges)
        self._edge_timer.start()

    def _build_edges(self):
        # A pending coalesced rebuild is redundant once we are rebuilding now.
        if self._edge_timer is not None:
            self._edge_timer.stop()

        for edge in self._edges:
            self.removeItem(edge)
        self._edges.clear()

        # Before resolving ports: a merge grows/loses a slot as branches are wired and
        # cleared, and the port the next loop looks up has to exist. Safe to do here (rather
        # than at the connection site) precisely because edges have just been dropped —
        # rebuilding a node's ports destroys the port items any surviving edge would hold.
        for node in self._nodes.values():
            node.sync_merge_ports()

        for node in self._nodes.values():
            # A stage change fired by deleting a chunk lands here before that node's
            # removal (which _on_chunk_deleted defers via singleShot), so its prim can
            # already be expired. Skip it rather than touch an invalid prim.
            if not node.prim.IsValid():
                continue
            for attr in node.prim.GetAttributes():
                attr_name = attr.GetName()
                if not attr_name.startswith("inputs:"):
                    continue
                # Every exec slot, not just inputs:chunk — a merge's branches past the first
                # live on inputs:chunk<N>, and treating those as typed param edges would hide
                # them behind the "Show param connections" toggle and then fail to find a
                # port for them at all.
                is_exec = chunk_usd.is_exec_input_name(attr_name)
                if not is_exec and not self._show_param_edges:
                    continue
                dst_attr_path = f"{node.prim.GetPath()}.{attr_name}"
                dst_port = node.get_port(dst_attr_path)
                if dst_port is None:
                    continue
                expression: str | None = None
                try:
                    param = node.chunk.get_param(attr_name[len("inputs:"):])
                    raw = getattr(param, 'raw_value', None) if param else None
                    if isinstance(raw, str) and re.search(r'@\w+\.\w+@', raw):
                        expression = raw
                except Exception:
                    pass

                for conn_path in attr.GetConnections():
                    src_node = self._nodes.get(str(conn_path.GetPrimPath()))
                    if src_node is None:
                        continue
                    # Connection is always owned by the consumer, so the source
                    # always visualizes from its output dot regardless of attr prefix.
                    src_path = str(conn_path).replace(".inputs:", ".outputs:", 1)
                    src_port = src_node.get_port(src_path)
                    if src_port is None:
                        continue
                    edge = ChunkEdgeItem(src_port, dst_port, expression)
                    self.addItem(edge)
                    self._edges.append(edge)

        for node in self._nodes.values():
            if node.prim.IsValid():
                node._sync_vector_states()

    def update_edges_for(self, node: ChunkNodeItem):
        node_ports = set(node._ports)
        for edge in self._edges:
            if edge.src_port in node_ports or edge.dst_port in node_ports:
                edge.update_path()

    # ------------------------------------------------------------------
    # Auto-layout (L key)

    def _build_layout_adjacency(self):
        """Prim-path-keyed up/down adjacency from ALL connections (exec + typed data),
        independent of the "Show param connections" view toggle — layout should account for
        every real connection, not just the ones currently drawn on screen."""
        up_of: dict = {}
        down_of: dict = {}
        for path, node in self._nodes.items():
            if not node.prim.IsValid():
                continue
            for attr in node.prim.GetAttributes():
                if not attr.GetName().startswith("inputs:"):
                    continue
                for conn_path in attr.GetConnections():
                    src_path = str(conn_path.GetPrimPath())
                    if src_path == path or src_path not in self._nodes:
                        continue
                    up_of.setdefault(path, set()).add(src_path)
                    down_of.setdefault(src_path, set()).add(path)
        return up_of, down_of

    def layout_nodes(self, seed_paths: "list[str] | None" = None) -> None:
        """Auto-layout — see plugins/chunk_graph_layout.py for the algorithm itself.

        seed_paths: prim paths to grow into their full connected subgraph before laying out
        (the "Layout Selected" case). None lays out every node currently in this context.
        """
        if not self._nodes:
            return
        up_of, down_of = self._build_layout_adjacency()

        if seed_paths:
            grown = chunk_graph_layout.grow_to_connected_subgraph(list(seed_paths), up_of, down_of)
            node_ids = sorted(grown & set(self._nodes))
        else:
            node_ids = sorted(self._nodes)
        if not node_ids:
            return

        node_id_set = set(node_ids)
        scoped_up = {nid: {u for u in up_of.get(nid, ()) if u in node_id_set} for nid in node_ids}
        scoped_down = {nid: {d for d in down_of.get(nid, ()) if d in node_id_set} for nid in node_ids}

        def size_of(nid):
            r = self._nodes[nid].boundingRect()
            return r.width(), r.height()

        def is_locked(nid):
            return self._nodes[nid]._locked

        def current_pos(nid):
            p = self._nodes[nid].pos()
            return p.x(), p.y()

        positions = chunk_graph_layout.layout_positions(
            node_ids, size_of, is_locked, current_pos, scoped_up, scoped_down
        )

        # Apply after the full layout is computed (not interleaved with computing it) — each
        # setPos triggers itemChange -> update_edges_for automatically, and _write_pos_to_usd
        # persists the same ui:nodegraph:node:pos attribute an interactive drag already writes.
        for nid, (x, y) in positions.items():
            node = self._nodes[nid]
            node.setPos(x, y)
            node._write_pos_to_usd()

    def _on_chunk_deleted(self, notice, sender):
        # A deleted chunk drops its prim from the stage. Rebuild from scratch so its node
        # and any dangling edges go away cleanly (deferred inside _schedule_rebuild). The exec
        # chain is healed (A -> X -> B becomes A -> B) in ChunkStageSync._on_chunk_deleted,
        # before the prim is removed; recook after the rebuild so the view follows the new chain.
        #
        # Deleting a chunk cascades request_delete() through every one of its own params
        # (ChunkGroupBase.request_delete()), each firing its own CHUNK_DELETED -- so a chunk
        # with many params (e.g. the RBD solver) fires this handler dozens of times per delete.
        # _schedule_rebuild() below is already debounced; recook must be too, or each of those
        # notices schedules its own redundant re-execution of the whole upstream chain -- harmless
        # when upstream chunks were side-effect-free on cook, but a real chunk (e.g. a reactive
        # Scatter) now re-runs, once per param, every time anything with many params is deleted.
        #
        # Default to a full recook -- safe/correct, matches pre-existing behaviour -- UNLESS
        # ChunkGraphView._delete_selected already proved this specific delete can't matter and
        # flagged it via _recook_skip_next_delete. That flag has to be set BEFORE
        # request_delete() runs: request_delete() force-unblocks the chunk as a prerequisite for
        # its own CHUNK_DELETED to fire (a blocked chunk suppresses its own notices), so by the
        # time this notice arrives, is_blocked() always reads False regardless of whether the
        # chunk was ever really cooking -- the delete itself erases that signal, so it can't be
        # judged here after the fact. Deletes triggered from elsewhere (e.g. the Params panel)
        # never set the flag, so they keep recooking unconditionally, same as before this fix.
        self._schedule_rebuild()
        for v in self.views():
            if not isinstance(v, ChunkGraphView):
                continue
            if not getattr(v, "_recook_skip_next_delete", False):
                v._recook_force_full = True
            if not getattr(v, "_recook_pending", False):
                v._recook_pending = True
                QtCore.QTimer.singleShot(0, v, v._run_pending_recook)

    def _node_set_is_stale(self, root) -> bool:
        """Whether the nodes on screen still match the chunk prims under `root`.

        Deliberately compares against prims rather than against live chunk objects: a chunk
        with no prim has no node either way, so it is not evidence of staleness, whereas a
        prim with no node is exactly the reported symptom.
        """
        root_prim = self._stage.GetPrimAtPath(root)
        if not root_prim.IsValid():
            return False  # nothing to compare against; _rebuild_nodes owns this case
        on_stage = {
            str(p.GetPath()) for p in root_prim.GetFilteredChildren(_INCLUDE_INACTIVE)
            if p.GetTypeName() == _CHUNK_PRIM_TYPENAME
        }
        # Only prims-without-nodes. A node whose prim is gone is already handled by the
        # fast path above (removing a prim IS a prim-level resync), and treating it as
        # staleness here would schedule a second, redundant rebuild for every delete.
        return bool(on_stage - set(self._nodes))

    def _on_objects_changed(self, notice, sender):
        if not self._stage or not sender:
            return
        if sender.GetRootLayer().identifier != self._stage.GetRootLayer().identifier:
            return
        # A prim-level resync under our chunks root means a chunk was added/removed/enabled/
        # disabled. Rebuild the whole graph from scratch — only active chunks survive —
        # instead of editing nodes in place around expired or inactive prims. Property-level
        # resyncs (e.g. wiring a connection) don't change the node set, so they fall through
        # to the cheap edge rebuild below.
        root = getattr(self, "_chunks_root_path", None)
        if root is not None and any(
            p.IsPrimPath() and p.HasPrefix(root) for p in notice.GetResyncedPaths()
        ):
            self._schedule_rebuild()
            return
        # Self-heal. The check above is the fast path and covers ordinary creates/deletes,
        # but it is not sufficient on its own: the graph can be left holding a node set that
        # no longer matches the stage, with nothing scheduled to correct it.
        #
        # Seen live after undoing a chunk create and redoing it -- the chunk prim was back
        # and active, the chunk object was alive, the two paired up perfectly, and the node
        # simply never returned. Two things combine to produce that. Undoing away every
        # chunk makes the root invalid, which _rebuild_nodes treats as a transient
        # recomposition: it keeps the current graph and retries, then gives up after 20
        # attempts. And the redo that follows reports its restore as *property*-level
        # resyncs (`<chunk>.inputs:...`), which the check above ignores by design. Nothing
        # was left to trigger a rebuild, so the graph stayed frozen.
        #
        # Rather than widen the fast path (property resyncs are the hot path -- every cook
        # emits hundreds), reconcile only when something under the root was resynced AND the
        # node set provably disagrees with the stage. Costs one prim scan on a rare notice.
        if root is not None and any(p.HasPrefix(root) for p in notice.GetResyncedPaths()):
            if self._node_set_is_stale(root):
                self._schedule_rebuild()
                return
        for path in notice.GetChangedInfoOnlyPaths():
            if path.IsPropertyPath() and path.name == "ui:nodegraph:node:pos":
                prim_path = str(path.GetPrimPath())
                node = self._nodes.get(prim_path)
                if node:
                    node._apply_usd_pos(node.prim)
        # Deferred: a single cook emits hundreds of these notices — one per attribute it
        # writes — and rebuilding every edge in the scene on each was 286 rebuilds for one
        # group expand. The edges only have to be right once the notices stop arriving.
        self._request_build_edges()


# ---------------------------------------------------------------------------
# Proxy key router

class _ProxyKeyRouter(QtCore.QObject):
    """Application-level event filter that routes key events to the proxy widget
    that was most recently clicked.

    usdview's MainWindow immediately reclaims native keyboard focus whenever
    any child widget (including our QGraphicsView) gets it, so the normal
    Qt proxy-focus mechanism never works.  Instead we track which proxy widget
    was last clicked (_active) and manually forward key events to the logically-
    focused child within that widget, bypassing native focus entirely.

    ShortcutOverride events are accepted while a proxy is active so usdview's
    camera-key shortcuts (F/W/A/S/D…) don't fire while the user is typing.
    """

    def __init__(self, view: "ChunkGraphView"):
        super().__init__(view)
        # Weak, deliberately. Installing this router on the QApplication pins the router,
        # and a strong back-reference here would pin the whole ChunkGraphView through it —
        # leaking the entire scene and widget tree for every view ever constructed, and
        # preventing the destroyed signal that uninstall() hangs off from ever firing.
        self._view = weakref.ref(view)
        self._busy = False
        self._active: Optional[QtWidgets.QWidget] = None  # proxy widget last clicked
        self._view_active: bool = False  # True while graph view was most recently clicked
        self._installed = False

    def install(self) -> None:
        """Attach to the QApplication, if not already attached.

        Called on construction and again on every showEvent -- a dived-into ChunkGroup's
        view is hidden, never destroyed, when navigating away (see uninstall's docstring),
        so this is what makes the filter's install actually track visibility instead of
        accumulating for the rest of the session. The guard makes both directions
        idempotent: installEventFilter doesn't dedupe on its own, and calling it twice for
        the same object makes Qt run eventFilter twice per event.
        """
        if self._installed:
            return
        if (app := QtWidgets.QApplication.instance()) is not None:
            app.installEventFilter(self)
            self._installed = True

    def uninstall(self) -> None:
        """Detach from the QApplication, if attached.

        An application-level filter outlives the view that owns it: Qt keeps dispatching
        to eventFilter after the view is gone, and once the Python wrapper is torn down
        that raises on the first attribute access. The owning view connects this to its
        destroyed signal so the filter's lifetime matches its own -- and, since a hidden
        view is never destroyed, also to its hideEvent, so a view sitting in a
        navigated-away-from tab stops costing every other view's event dispatch a filter
        call for as long as it stays hidden (which, for most views most of the time, is
        most of a session).
        """
        if not self._installed:
            return
        if (app := QtWidgets.QApplication.instance()) is not None:
            app.removeEventFilter(self)
        self._installed = False

    def set_active(self, widget: Optional[QtWidgets.QWidget]) -> None:
        self._active = widget

    def set_view_active(self, active: bool) -> None:
        self._view_active = active

    # Widget types that acquire native focus themselves and handle their own keys.
    _INPUT_TYPES = (
        QtWidgets.QLineEdit,
        QtWidgets.QTextEdit,
        QtWidgets.QPlainTextEdit,
        QtWidgets.QAbstractSpinBox,
        QtWidgets.QComboBox,
    )

    def eventFilter(self, _obj, event) -> bool:
        # Read defensively: if a filter ever outlives its wrapper's __dict__, degrade to a
        # no-op rather than raising AttributeError inside Qt's dispatch. uninstall() is
        # what prevents that happening; this only keeps the failure mode harmless.
        if getattr(self, "_busy", False):
            return False
        if (view := self._view()) is None:
            # Owning view collected: self-heal a filter nothing detached, then stand down.
            self.uninstall()
            return False
        if not view.isVisible():
            # ChunkGraphView.hideEvent/showEvent keep this filter's own installation in
            # sync with visibility (see install/uninstall's docstrings), so this should
            # already be unreachable in the normal case -- kept as a cheap defense-in-depth
            # guard for the gap between a visibility change and its event actually firing
            # (e.g. isVisible() can flip before the hideEvent that would uninstall this is
            # delivered). All ChunkTabContextStack pages share one QStackedWidget rect, so
            # a hidden view's geometry checks below would still match the cursor if this
            # guard were ever actually hit.
            return False
        etype = event.type()
        if etype not in (QtCore.QEvent.Type.KeyPress, QtCore.QEvent.Type.ShortcutOverride):
            return False
        if QtWidgets.QApplication.activePopupWidget() is not None:
            # A popup (a QMenu) owns the keyboard while it is up, and every shortcut below
            # is a GRAPH shortcut -- none of them mean anything until it closes. This filter
            # is installed on the QApplication, so without this it kept firing straight
            # through an open menu, and the menu's own filter does not stop it: that one
            # only forwards printable characters to the search box, and Tab is not printable.
            #
            # Live-reported as pressing Tab repeatedly stacking up a chunk menu per press --
            # the Tab branch below re-entered _open_tab_menu while its own blocking exec_()
            # was still running. The same gap silently ate "f" and "l" typed into that
            # menu's search box, firing frame-selection / auto-layout on the graph behind it
            # instead of reaching the box.
            return False

        fw = QtWidgets.QApplication.focusWidget()
        if isinstance(fw, self._INPUT_TYPES):
            inside_proxy = self._active is not None and (
                fw is self._active or self._active.isAncestorOf(fw)
            )
            if inside_proxy:
                # QLineEdit inside our proxy has Qt-internal focus — prevent shortcuts
                # but let KeyPress flow natively so the widget handles it directly.
                if etype == QtCore.QEvent.Type.ShortcutOverride:
                    event.accept()
                return False
            # Input widget outside our proxy has focus — deactivate and defer.
            self._active = None
            self._view_active = False
            return False

        # Native OS focus doesn't reliably land on a proxy's QLineEdit even when the user is
        # actively typing into it (the same focus-stealing problem the class docstring
        # describes) -- that's exactly why the manual-forwarding fallback at the bottom of
        # this method reads self._active.focusWidget() instead of trusting QApplication's
        # own idea of focus. The _INPUT_TYPES check above only catches the case where native
        # focus DOES happen to agree; check the proxy's own logical focus target too, so a
        # digit/Ctrl+C/etc typed into e.g. Create Layer's "Layer Name" field isn't stolen by
        # the ungated blocks below before it ever reaches that fallback.
        active_focus = self._active.focusWidget() if self._active is not None else None
        typing_in_proxy = isinstance(active_focus, self._INPUT_TYPES)

        # F-to-frame: intercept only when cursor is over the graph view and no proxy active.
        if self._active is None and event.key() == QtCore.Qt.Key.Key_F:
            view_global = QtCore.QRect(view.mapToGlobal(QtCore.QPoint(0, 0)), view.size())
            if view_global.contains(QtGui.QCursor.pos()):
                if etype == QtCore.QEvent.Type.ShortcutOverride:
                    event.accept()
                    return True
                if etype == QtCore.QEvent.Type.KeyPress:
                    view._frame_nodes()
                    event.accept()
                    return True

        # L-to-layout: same fallback pattern as F, for when a proxy widget has logical focus
        # but the cursor is over the graph view.
        if self._active is None and event.key() == QtCore.Qt.Key.Key_L:
            view_global = QtCore.QRect(view.mapToGlobal(QtCore.QPoint(0, 0)), view.size())
            if view_global.contains(QtGui.QCursor.pos()):
                if etype == QtCore.QEvent.Type.ShortcutOverride:
                    event.accept()
                    return True
                if etype == QtCore.QEvent.Type.KeyPress:
                    view._layout_selected_or_all()
                    event.accept()
                    return True

        # Tab-to-create: open the chunk menu at the cursor (like Houdini's Tab menu).
        if self._active is None and event.key() == QtCore.Qt.Key.Key_Tab:
            view_global = QtCore.QRect(view.mapToGlobal(QtCore.QPoint(0, 0)), view.size())
            if view_global.contains(QtGui.QCursor.pos()):
                if etype == QtCore.QEvent.Type.ShortcutOverride:
                    event.accept()
                    return True
                if etype == QtCore.QEvent.Type.KeyPress:
                    view._open_tab_menu()
                    event.accept()
                    return True

        # Ctrl+C / Ctrl+V / Ctrl+D / Ctrl+X: same fallback pattern as F/L, but -- unlike
        # F/L -- NOT gated on self._active being None. A proxy going "active" just means
        # its widget was the last thing clicked (e.g. a ChunkGroup node's embedded body,
        # which has far more clickable surface than a plain leaf node); none of ITS child
        # widgets have a competing meaning for Ctrl+C/V/D/X EXCEPT a text field mid-edit,
        # which is why this is also gated on `not typing_in_proxy` (see that variable's
        # comment above -- native focus can't be trusted to already rule that out).
        # Requiring self._active is None here meant clicking into any node's body before
        # pressing Ctrl+C routed the key to the "forward to active proxy" fallback below
        # instead -- a silent no-op -- while a bare click on the node chrome (proxy is
        # None) worked fine.
        # Ctrl+G belongs here for the same reason as the rest: it is built out of Cut and
        # Paste, so a graph it cannot reach is a graph where grouping silently does nothing
        # -- which is exactly how it behaved before this, since the native keyPressEvent
        # path needs a focus usdview rarely leaves with the view.
        ctrl_down = bool(event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier)
        _EDIT_KEYS = (
            QtCore.Qt.Key.Key_C, QtCore.Qt.Key.Key_V, QtCore.Qt.Key.Key_D,
            QtCore.Qt.Key.Key_X, QtCore.Qt.Key.Key_G,
        )
        if not typing_in_proxy and ctrl_down and event.key() in _EDIT_KEYS:
            view_global = QtCore.QRect(view.mapToGlobal(QtCore.QPoint(0, 0)), view.size())
            if view_global.contains(QtGui.QCursor.pos()):
                if etype == QtCore.QEvent.Type.ShortcutOverride:
                    event.accept()
                    return True
                if etype == QtCore.QEvent.Type.KeyPress:
                    if event.key() == QtCore.Qt.Key.Key_C:
                        view._copy_selected_to_clipboard()
                    elif event.key() == QtCore.Qt.Key.Key_V:
                        view._paste_at_cursor()
                    elif event.key() == QtCore.Qt.Key.Key_D:
                        view._duplicate_selected()
                    elif event.key() == QtCore.Qt.Key.Key_G:
                        view._group_selected()
                    else:
                        view._cut_selected_to_clipboard()
                    event.accept()
                    return True

        # 1/2/3-to-switch-view-slot: same fallback pattern as Ctrl+C/V/D/X above -- native
        # keyPressEvent (chunk_qt_nodegraph.py ChunkGraphView.keyPressEvent) has the same
        # logic, but depends on ChunkGraphView holding real native Qt focus, which usdview's
        # MainWindow routinely reclaims. F/L/Ctrl+C/V/D/X were already hardened against that
        # via this router; the view-slot keys were never added to it, so they're the one
        # shortcut still riding the fragile native-focus path. Also gated on
        # `not typing_in_proxy` -- these ARE digits, so without that guard this swallowed
        # every "1"/"2"/"3" typed into e.g. Create Layer's "Layer Name" field.
        if not typing_in_proxy and event.key() in (QtCore.Qt.Key.Key_1, QtCore.Qt.Key.Key_2, QtCore.Qt.Key.Key_3):
            view_global = QtCore.QRect(view.mapToGlobal(QtCore.QPoint(0, 0)), view.size())
            if view_global.contains(QtGui.QCursor.pos()):
                if etype == QtCore.QEvent.Type.ShortcutOverride:
                    event.accept()
                    return True
                if etype == QtCore.QEvent.Type.KeyPress:
                    slot = event.key() - QtCore.Qt.Key.Key_0
                    registry = chunk_registry.ChunkRegistry()
                    stage = registry.scene.get_stage() if registry.initialized else None
                    # Never toggle to "no view" (slot 0) if nothing exists for this slot in
                    # this graph's own context -- that would block the whole graph.
                    if stage and chunk_usd.find_view_chunk_for_slot(
                        slot, stage, registry.scene, view._graph_context()
                    ):
                        view._view_state.set_slot(slot)
                    event.accept()
                    return True

        # Delete-selected: same fallback pattern as Ctrl+C/V/D/X above. Selecting a node
        # is itself a click on its embedded proxy body, which is exactly the case native
        # view focus (ChunkGraphView.keyPressEvent's own Key_Delete handler) can't be
        # trusted to have -- live-reported as Delete intermittently doing nothing on a
        # node that was clearly selected. Gated on `not typing_in_proxy` so Delete still
        # edits text inside a focused field instead of deleting the node underneath it.
        if not typing_in_proxy and event.key() == QtCore.Qt.Key.Key_Delete:
            view_global = QtCore.QRect(view.mapToGlobal(QtCore.QPoint(0, 0)), view.size())
            if view_global.contains(QtGui.QCursor.pos()):
                if etype == QtCore.QEvent.Type.ShortcutOverride:
                    event.accept()
                    return True
                if etype == QtCore.QEvent.Type.KeyPress:
                    view._delete_selected()
                    event.accept()
                    return True

        # Bare D (disable toggle): identical fragility and identical gating to Delete above.
        # Additionally gated on `not ctrl_down` because Ctrl+D is DUPLICATE, claimed by the
        # Ctrl+C/V/D/X block further up -- this must only ever take the un-modified key.
        # `not typing_in_proxy` matters more here than anywhere else: D is a printable
        # character, so without it every "d" typed into a node's text field would toggle
        # the node disabled instead of reaching the field.
        if not typing_in_proxy and not ctrl_down and event.key() == QtCore.Qt.Key.Key_D:
            view_global = QtCore.QRect(view.mapToGlobal(QtCore.QPoint(0, 0)), view.size())
            if view_global.contains(QtGui.QCursor.pos()):
                if etype == QtCore.QEvent.Type.ShortcutOverride:
                    event.accept()
                    return True
                if etype == QtCore.QEvent.Type.KeyPress:
                    view._toggle_disable_selected()
                    event.accept()
                    return True

        # Escape: cancel an in-flight wire drag, otherwise drop the proxy focus -- the same
        # two jobs ChunkGraphView.keyPressEvent's own Escape does, with the same native-focus
        # fragility as Delete and D.
        #
        # Deliberately NOT gated on `not typing_in_proxy`, unlike every other key here.
        # That flag tracks the proxy's LOGICAL focus, which in usdview is routinely a text
        # field the user is typing into while native focus sits elsewhere -- and escaping
        # out of that field is exactly what Escape is for, so the fallthrough below
        # (forwarding it to a QLineEdit, which ignores it) is the broken behaviour, not the
        # one to preserve. A field holding genuine NATIVE focus is a different case and has
        # already returned above, left to Qt to deliver normally. An open popup returned
        # above too, so a menu's own Escape still closes the menu rather than reaching here.
        if event.key() == QtCore.Qt.Key.Key_Escape:
            view_global = QtCore.QRect(view.mapToGlobal(QtCore.QPoint(0, 0)), view.size())
            if view_global.contains(QtGui.QCursor.pos()):
                if etype == QtCore.QEvent.Type.ShortcutOverride:
                    event.accept()
                    return True
                if etype == QtCore.QEvent.Type.KeyPress:
                    if view._drag_src_port is not None:
                        view._cancel_wire_drag()
                    else:
                        self.set_active(None)
                        view.setFocus()
                    event.accept()
                    return True

        if self._active is None:
            return False
        if etype == QtCore.QEvent.Type.ShortcutOverride:
            event.accept()
            return True
        # Route to whichever child last had logical (not native) focus.
        target = self._active.focusWidget() or self._active
        self._busy = True
        try:
            QtWidgets.QApplication.sendEvent(target, event)
        finally:
            self._busy = False
        return True


# ---------------------------------------------------------------------------
# View

class ChunkGraphView(QtWidgets.QGraphicsView):
    """QGraphicsView with zoom, pan (middle mouse or Space+drag), and F to frame."""

    _SCENE_EXTENT = 50000
    _GRID_SPACING = 200  # scene units — fixed world-space, so it scales naturally with zoom
    # ...but only down to a point. The dots are cosmetic (constant screen size), so as the
    # view zooms out they crowd together into a speckle that reads as noise rather than a
    # grid, and their count grows with the square of the zoom-out -- thousands of them at
    # the far end. The spacing doubles instead, holding the on-screen gap between this and
    # twice it at every zoom, which bounds the count. Deliberately below the 200px gap seen
    # at scale 1.0: matching that reads as an empty canvas once zoomed out.
    _GRID_MIN_SCREEN_SPACING = 55  # px
    _GRID_MAX_DOUBLINGS = 12
    _GRID_DOT_COLOR = QtGui.QColor("#3d4250")  # muted grey-blue, subtle against #252525
    _GRID_DOT_WIDTH = 2

    dive_into = QtCore.Signal(object)  # re-emitted from scene when a group node is double-clicked

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(ChunkGraphScene())
        # Which side of _LOD_BODY_MIN_SCALE the view is currently on; see _apply_lod.
        self._lod_bodies_shown = True
        self.setRenderHint(QtGui.QPainter.Antialiasing)
        self.setDragMode(QtWidgets.QGraphicsView.NoDrag)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.AnchorViewCenter)
        self.setBackgroundBrush(QtGui.QColor("#252525"))
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        # NoFocus prevents Qt's automatic click-to-focus on the view, which would
        # steal native focus away from QLineEdits embedded in proxy widgets.
        # We grant focus to the view explicitly in mousePressEvent when needed.
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.setAcceptDrops(True)
        e = self._SCENE_EXTENT
        self.setSceneRect(-e, -e, e * 2, e * 2)
        self._pan_active = False
        self._pan_start = QtCore.QPoint()
        self._rubber_band = QtWidgets.QRubberBand(QtWidgets.QRubberBand.Shape.Rectangle, self.viewport())
        self._rubber_band_active = False
        self._rubber_band_origin = QtCore.QPoint()
        self._rubber_band_add = False  # Shift held: add boxed nodes to selection instead of replacing

        self._menubar = QtWidgets.QMenuBar(self)
        view_menu = self._menubar.addMenu("View")
        self._param_edge_action = view_menu.addAction("Show param connections")
        self._param_edge_action.setCheckable(True)
        self._param_edge_action.setChecked(True)
        self._param_edge_action.triggered.connect(self._on_toggle_param_edges)
        self._menubar.setFixedWidth(self.width())
        mh = self._menubar.sizeHint().height()
        self.setViewportMargins(0, mh, 0, 0)

        self._proxy_key_router = _ProxyKeyRouter(self)
        self._proxy_key_router.install()
        # Pair the install with a removal, or every view ever built leaves a filter
        # receiving key events for the rest of the session. Bind the router directly
        # rather than reaching through self: the view is mid-destruction when destroyed
        # fires. The router is a plain QObject child, so it is still alive at that point
        # -- ~QObject emits destroyed() before deleting children.
        self.destroyed.connect(lambda *_, _router=self._proxy_key_router: _router.uninstall())

        self._drag_src_port: Optional[ChunkPortItem] = None
        self._drag_edge: Optional[QtWidgets.QGraphicsPathItem] = None
        self._drag_detach_attr: Optional[Usd.Attribute] = None
        self._drag_detach_source_port: Optional[ChunkPortItem] = None
        self._drag_reused_edge: Optional[ChunkEdgeItem] = None
        # Suppresses build()'s generic _ensure_default_view while a drag-and-drop chunk
        # import is in flight -- see _import_as_chunks/_ensure_view_for_imported.
        self._suppress_default_view = False
        # Debounce state for build()'s post-build recook -- see _schedule_restore_active_view.
        self._restore_view_token = 0
        self._restore_view_changed_chunks: "Optional[set]" = set()
        self._view_state = ViewSlotState(self)
        self._view_state.slot_changed.connect(self._on_view_slot_changed)
        self._view_state.create_view_for.connect(self._on_create_view_for_path)
        self._view_state.pin_changed.connect(self._on_pin_changed)

    def _on_toggle_param_edges(self, checked: bool):
        graph_scene = self.scene()
        if isinstance(graph_scene, ChunkGraphScene):
            graph_scene.set_show_param_edges(checked)

    def resizeEvent(self, event: QtGui.QResizeEvent):
        super().resizeEvent(event)
        self._menubar.setFixedWidth(self.width())

    def hideEvent(self, event: QtGui.QHideEvent):
        super().hideEvent(event)
        # Fires when a dived-into ChunkGroup's page stops being the current one in its
        # ChunkTabContextStack (QStackedWidget.setCurrentIndex hides the outgoing page,
        # it doesn't destroy it) -- see _ProxyKeyRouter.uninstall's docstring.
        self._proxy_key_router.uninstall()

    def showEvent(self, event: QtGui.QShowEvent):
        super().showEvent(event)
        self._proxy_key_router.install()

    @staticmethod
    def _grid_points_in_rect(rect: QtCore.QRectF, spacing: int) -> list:
        """Scene-space grid dot positions covering rect, snapped to fixed spacing-unit lines.

        Pure math, no painter — kept separate from drawBackground so the snapping/coverage
        logic is unit-testable without a real paint device.
        """
        # Snap down to the nearest grid line at or before rect's edges. Python's % on a
        # negative left/top already returns a non-negative remainder (floor-division
        # semantics), so this snaps correctly on the negative half of the scene too.
        left = int(rect.left()) - (int(rect.left()) % spacing)
        top = int(rect.top()) - (int(rect.top()) % spacing)

        points = []
        x = left
        while x < rect.right():
            y = top
            while y < rect.bottom():
                points.append(QtCore.QPointF(x, y))
                y += spacing
            x += spacing
        return points

    def _grid_spacing_for_view(self) -> int:
        """Grid spacing in scene units, doubled until the dots are far enough apart on screen.

        Kept separate from _grid_points_in_rect so that stays pure coverage math with no
        view attached, and remains unit-testable on its own.
        """
        spacing = self._GRID_SPACING
        scale = self.transform().m11()
        if scale <= 0:
            return spacing
        for _ in range(self._GRID_MAX_DOUBLINGS):
            if spacing * scale >= self._GRID_MIN_SCREEN_SPACING:
                break
            spacing *= 2
        return spacing

    def drawBackground(self, painter: QtGui.QPainter, rect: QtCore.QRectF) -> None:
        # super() fills with setBackgroundBrush's flat color; dots paint on top of that.
        super().drawBackground(painter, rect)
        # Only the exposed rect is ever painted here (never the full 100k-unit scene extent —
        # Qt only calls drawBackground for the currently visible region).
        points = self._grid_points_in_rect(rect, self._grid_spacing_for_view())
        if not points:
            return

        # A single batched drawPoints call is far cheaper than one drawEllipse call per dot.
        painter.save()
        pen = QtGui.QPen(self._GRID_DOT_COLOR)
        pen.setWidth(self._GRID_DOT_WIDTH)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        pen.setCosmetic(True)  # constant screen-space dot size regardless of view zoom/scale
        painter.setPen(pen)
        painter.drawPoints(points)
        painter.restore()

    def drawForeground(self, painter: QtGui.QPainter, rect) -> None:
        super().drawForeground(painter, rect)
        painter.save()
        painter.resetTransform()
        vp = self.viewport()
        if self._view_state.active_slot == 0:
            text = "View: –"
            bg = QtGui.QColor(40, 40, 40, 180)
            fg = QtGui.QColor(120, 120, 120)
        else:
            text = f"View: {self._view_state.active_slot}"
            bg = QtGui.QColor(_COL_VIEW_HEADER.red(), _COL_VIEW_HEADER.green(), _COL_VIEW_HEADER.blue(), 210)
            fg = QtGui.QColor("#ffffff")
        font = QtGui.QFont("monospace", 9, QtGui.QFont.Bold)
        painter.setFont(font)
        fm = QtGui.QFontMetrics(font)
        pad_x, pad_y = 8, 5
        w = fm.horizontalAdvance(text) + pad_x * 2
        h = fm.height() + pad_y * 2
        x = vp.width() - w - 8
        y = vp.height() - h - 8
        painter.setBrush(bg)
        painter.setPen(QtCore.Qt.NoPen)
        painter.drawRoundedRect(QtCore.QRect(x, y, w, h), 4, 4)
        painter.setPen(fg)
        painter.drawText(QtCore.QRect(x, y, w, h), QtCore.Qt.AlignCenter, text)
        painter.restore()

    def sizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(200, 200)

    def minimumSizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(0, 0)

    def build(self, stage: Usd.Stage, scene_sender: core.SceneSender, context=None, changed_chunks=None):
        """changed_chunks: forwarded to the post-build recook -- see _recook_active_view's
        docstring. build() isn't only called on a genuine context/view switch (which always
        needs a full recook): ChunkWidgetDocker._loaded_module_callback also re-invokes it
        (via _rebuild_graph) every time ANY chunk is created while the Graph tab is active, since
        it invalidates every graph scene first and _rebuild_graph() then rebuilds whichever one
        is current. Without this, creating one chunk unconditionally re-executed the whole
        active view's cook set 100ms later -- a stray "Scattered N instances" print from a
        totally unrelated create, and a visible re-mute/dim of every other chunk -- reported
        live in usdview. Omit changed_chunks (the default) for a real view/tab switch.
        """
        scene = self.scene()
        scene.build(stage, scene_sender, context)
        scene.dive_into.connect(self.dive_into)
        # Auto-create a default view when the graph is opened on a context that has chunks but
        # no view yet (e.g. chunks were made in the params panel first). Deferred past this
        # build's rebuild + proxy pass so view creation runs on a settled graph, not mid-build.
        # Cheap (just a membership scan) even when re-triggered by an unrelated create, so this
        # one stays unconditional.
        QtCore.QTimer.singleShot(80, self, lambda ctx=context: self._ensure_default_view(ctx))
        # Switching to this context must re-establish the viewport for ITS active view — the
        # viewport is one global stage, so a view activated in another context (e.g. an inner
        # group view) otherwise stays cooked and the wrong result shows. Deferred past the
        # auto-view step. (On create we deliberately don't re-cook, to avoid churn.)
        self._schedule_restore_active_view(changed_chunks)

    def _schedule_restore_active_view(self, changed_chunks: Optional[Iterable[core.ChunkBase]]) -> None:
        """Debounce build()'s post-build recook across a burst of build() calls landing in
        the same event-loop tick.

        A bulk file load calls build() once per chunk, synchronously, in the same call
        stack (see load_chunks' _add_prim_mods -- each restored chunk fires its own
        CHUNK_LOADED, which ChunkWidgetDocker._loaded_module_callback turns straight into a
        build() call while the Graph tab is active). Each call used to arm its own
        independent 100ms timer, and recook_for_view re-executes its whole cook set every
        time it runs, not just what changed (see _recook_active_view's docstring) -- so an
        N-chunk load re-ran the entire upstream chain N times, live-reported as repeated
        "Updated stage with sublayer" / "Scattered N instances" spam on every reload.

        Only this recook is debounced, not build() itself: scene.build() above (the actual
        node/proxy-widget creation) stays fully synchronous, since several callers -- both
        here and in tests -- read scene()._nodes[...] immediately after creating or pasting
        a chunk and would otherwise KeyError on a node that hasn't been built yet.

        changed_chunks accumulates (union) across every build() call in the same burst;
        None (a caller that needs an unconditional recook, e.g. a real view/tab switch)
        dominates and is preserved even if later calls in the same burst pass a scoped set,
        since it's the strict superset of "recook if any of these chunks changed."
        """
        if self._restore_view_changed_chunks is not None:
            if changed_chunks is None:
                self._restore_view_changed_chunks = None
            else:
                self._restore_view_changed_chunks |= set(changed_chunks)
        self._restore_view_token += 1
        token = self._restore_view_token
        QtCore.QTimer.singleShot(100, self, lambda t=token: self._run_scheduled_restore_active_view(t))

    def _run_scheduled_restore_active_view(self, token: int) -> None:
        if token != self._restore_view_token:
            return  # superseded by a later build() call in the same burst
        changed = self._restore_view_changed_chunks
        self._restore_view_changed_chunks = set()
        self._restore_active_view(changed)

    def _context_has_view(self, scene, context) -> bool:
        """True when this context (or the graph as a whole, if context is None) has a view chunk."""
        return any(
            chunk_usd.is_view_chunk(c) and (context is None or c.get_context() == context)
            for c in scene.get_chunks()
        )

    def _push_nest_context(self, scene, context):
        """When the graph is dived into a chunkgroup, make new chunks nest into it. add_chunk
        uses scene._current_context, which isn't set for a dive — so without this, creating a
        node inside a group silently added it to the tab instead. Returns (changed, previous) to
        restore. No-op at the tab level (plain contexts have no module)."""
        cfg = context.get_config() if context is not None else None
        if context is not None and context.has_flag(ChunkFlags.GroupFlags.CONTEXT) and cfg and cfg.module:
            prev = scene._current_context
            scene._current_context = weakref.ref(context)
            return True, prev
        return False, None

    # ------------------------------------------------------------------
    # Copy / Paste

    def _instantiate_chunk_of_type(self, module_name: str):
        """Build a fresh chunk instance of the given module, not yet added to the scene.
        Returns (primary_chunk, all_chunks) -- a module's create() can spawn auxiliary
        chunks alongside the main one (mirrors the pattern already used by
        contextMenuEvent/_create_and_connect_from_menu); primary is the first non-view one."""
        registry = chunk_registry.ChunkRegistry()
        if not registry.initialized:
            return None, []
        mod = registry.get_chunk_module(module_name)
        if mod is None:
            return None, []
        chunks = mod.create() if mod.loaded else mod.load()
        primary = next((c for c in chunks if not chunk_usd.is_view_chunk(c)), None)
        return primary, list(chunks)

    def _snapshot_selection(self):
        """Build (clip_nodes, clip_edges, top_left) from the current selection -- the shared
        core of both Copy (into the module clipboard) and Duplicate (straight to paste,
        clipboard untouched). View chunks are never included -- they're per-context slot
        machinery, not content worth duplicating. A wire to a node OUTSIDE the selection is
        dropped, not recreated against the original outside node -- only wires between two
        selected nodes survive. Returns ([], [], None) if there's nothing selected to copy."""
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene) or not graph_scene._scene_sender or not graph_scene._stage:
            return [], [], None
        selected = [
            node for node in graph_scene._nodes.values()
            if node.chunk.has_state(ChunkFlags.State.SELECTED) and not node._is_view
        ]
        if not selected:
            return [], [], None
        min_x = min(n.pos().x() for n in selected)
        min_y = min(n.pos().y() for n in selected)
        kept_index: dict[str, int] = {}
        clip_nodes: list[_ClipboardNode] = []
        for node in selected:
            rel_pos = QtCore.QPointF(node.pos().x() - min_x, node.pos().y() - min_y)
            clip_node = _snapshot_chunk_node(node.chunk, graph_scene._scene_sender, graph_scene._stage, rel_pos)
            if clip_node is None:
                continue
            kept_index[str(node.prim.GetPath())] = len(clip_nodes)
            clip_nodes.append(clip_node)
        if not clip_nodes:
            return [], [], None

        clip_edges: list[_ClipboardEdge] = []
        for node in selected:
            dst_index = kept_index.get(str(node.prim.GetPath()))
            if dst_index is None:
                continue
            for dst_attr, src_prim_path, src_attr in _iter_prim_connections(node.prim):
                src_index = kept_index.get(src_prim_path)
                if src_index is None:
                    continue  # source outside the copied selection -- wire dropped
                clip_edges.append(_ClipboardEdge(src_index, src_attr, dst_index, dst_attr))

        return clip_nodes, clip_edges, QtCore.QPointF(min_x, min_y)

    def selected_chunks(self, including: "core.ChunkBase | None" = None) -> "list[core.ChunkBase]":
        """The chunks this graph has selected, with `including` first if it is one of them.

        `including` is the chunk a menu was opened on. Right-clicking a node that is NOT in
        the selection acts on that node alone -- the same as every other app: the click is
        a new target, not an addition to an old one. Right-clicking one that IS selected
        acts on the whole selection.
        """
        scene = self.scene()
        if not isinstance(scene, ChunkGraphScene):
            return [including] if including is not None else []
        selected = [
            node.chunk for node in scene._nodes.values()
            if node.chunk.has_state(ChunkFlags.State.SELECTED)
        ]
        if including is not None and including not in selected:
            return [including]
        if including is not None:
            selected = [including] + [c for c in selected if c is not including]
        return selected

    def _copy_selected_to_clipboard(self) -> None:
        """Ctrl+C: snapshot the selected chunks' type + param values + position, and the
        wires between them, into the module-level clipboard."""
        clip_nodes, clip_edges, _top_left = self._snapshot_selection()
        if not clip_nodes:
            return
        _node_clipboard.clear()
        _node_clipboard.extend(clip_nodes)
        _edge_clipboard.clear()
        _edge_clipboard.extend(clip_edges)

    def _cut_selected_to_clipboard(self) -> None:
        """Ctrl+X: copy (as above), then delete the selection via the same path Delete
        already uses -- no deferred/pending-cut state, since there's no undo system in this
        codebase to make that meaningful. A locked node (from an external/referenced layer)
        is still copied, but skipped by the delete step -- it can't be removed from its
        source layer -- while the rest of a mixed selection is still cut normally."""
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene):
            return
        self._copy_selected_to_clipboard()
        if not _node_clipboard:
            return
        deletable = [
            node.chunk for node in graph_scene._nodes.values()
            if node.chunk.has_state(ChunkFlags.State.SELECTED) and not node._locked and not node._is_view
        ]
        if not deletable:
            return
        self.mark_pending_delete_relevance(deletable)
        for c in deletable:
            c.request_delete()

    def _paste_at_cursor(self) -> None:
        global_pos = QtGui.QCursor.pos()
        scene_pos = self.mapToScene(self.mapFromGlobal(global_pos))
        self._paste_clipboard_at(scene_pos)

    def _paste_clipboard_at(self, scene_pos: QtCore.QPointF) -> "list[core.ChunkBase]":
        """Materialize the module-level clipboard as new chunk(s) centered at scene_pos."""
        if not _node_clipboard:
            return []
        return self._materialize_at(_node_clipboard, _edge_clipboard, scene_pos)

    def _materialize_at(
        self, clip_nodes: "list[_ClipboardNode]", clip_edges: "list[_ClipboardEdge]", scene_pos: QtCore.QPointF
    ) -> "list[core.ChunkBase]":
        """Create new chunk(s) from clip_nodes centered at scene_pos, wire clip_edges between
        them, and select the result -- nested into whatever context/tab this view is
        currently showing, same mechanism right-click "New Chunk" already uses
        (_push_nest_context), which is what makes pasting/duplicating into a chunkgroup work
        for free, including a selection copied from a different context. Shared by both Paste
        (module clipboard) and Duplicate (clipboard-free, straight from the selection).
        Returns the list of newly created primary chunks."""
        if not clip_nodes:
            return []
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene) or not graph_scene._scene_sender:
            return []
        scene = _chunk.get_scene()
        if not scene:
            return []
        context = self._graph_context()
        view_exists = self._context_has_view(scene, context)
        nested, prev_ctx = self._push_nest_context(scene, context)
        new_chunks: list[core.ChunkBase] = []
        new_path_by_index: dict[int, str] = {}
        try:
            for index, item in enumerate(clip_nodes):
                primary, chunks = self._instantiate_chunk_of_type(item.module_name)
                if primary is None:
                    continue
                # Guard against a premature REAL module execute -- NOT via set_blocked(),
                # which also suppresses per-param VALUE_CHANGED notices entirely (chunk_notice.py's
                # value_changed: "if self.is_blocked() ... return"). That collateral damage was
                # live-confirmed: a restored Create Layer insert_mode="Index" left the Index
                # spinbox widget missing, because insert_mode's own add_callback handler (which
                # unhides it) never got to run while blocked -- toggling the menu away and back
                # was the only way to make it reappear. _executing is a narrower, more accurate
                # tool for THIS specific hazard: chunk_notice.py's reserved-execute dispatch
                # checks it (`not getattr(prop, '_executing', False)`) but value_changed's
                # per-param notice path does not, so holding it through value application AND
                # add_chunk suppresses ONLY the real module.execute() call -- e.g. Create Layer
                # authoring a genuine anon Sdf.Layer the instant layer_name changes, which
                # ChunkBase.get_stage() lets happen even before this chunk is added to any scene
                # -- while ordinary reactive UI callbacks (visibility toggles, menu refreshes)
                # fire exactly as they would for live, interactive typing.
                blockable = [
                    c for c in chunks
                    if not chunk_usd.is_view_chunk(c) and not chunk_usd.is_group_chunk(c)
                ]
                for c in blockable:
                    c._executing = True
                # Apply the copied values BEFORE add_chunk, not after: add_chunk's own
                # CHUNK_LOADED/PARAM_ADDED notice dispatch can synchronously build this
                # chunk's Params-panel widget as a side effect (chunk_qt.py's
                # create_chunk_widget, when the target context already has an open tab --
                # confirmed live, not deferred the way the *graph* node's own proxy widget
                # is). A widget reads its initial displayed text once, synchronously, at
                # construction (ChunkParamWidgetBase.setup_chunk_widget) -- applying values
                # after add_chunk meant that first read saw the still-default/empty value,
                # and a blocked pasted chunk (see below) never gets the later reactive
                # VALUE_CHANGED notice that would otherwise have corrected it, leaving the
                # field looking permanently blank despite the underlying param value being
                # correct.
                # Rows first: a list group starts empty on a fresh chunk, so its values
                # have nowhere to land until the rows it had are back.
                _apply_list_sizes(primary, getattr(item, 'list_sizes', []))
                _apply_param_values(primary, item.values)
                pos = (scene_pos.x() + item.rel_pos.x(), scene_pos.y() + item.rel_pos.y())
                # USD is Y-up, Qt is Y-down -- _create_node_pos is read by chunk_usd in USD
                # space (see chunk_usd._create_node_pos usage elsewhere in this file).
                chunk_usd._create_node_pos = (pos[0], -pos[1])
                try:
                    for c in chunks:
                        scene.add_chunk(c)
                    for c in blockable:
                        c._executing = False
                    if view_exists:
                        # Stays inert (muted/uncooked) until manually wired into the view,
                        # same as before -- _executing only guarded the value-restoration
                        # window above, it doesn't keep the chunk out of recook_for_view's
                        # later cook-membership decisions the way set_blocked() does.
                        for c in blockable:
                            c.set_blocked(True)
                    else:
                        # No view driving this context -- matches the pre-existing behavior
                        # of an unblocked freshly created chunk: cook it for real now, the
                        # one legitimate execute the _executing guard above deferred past
                        # add_chunk so it runs with correct positioning/parenting in place.
                        for c in blockable:
                            c.execute()
                finally:
                    chunk_usd._create_node_pos = None
                new_chunks.append(primary)
                new_path_by_index[index] = chunk_usd.get_chunk_prim_path(primary)
                if item.children:
                    self._materialize_children(primary, item.children, item.child_edges)
        finally:
            if nested:
                scene._current_context = prev_ctx
        if new_chunks and graph_scene._scene_sender:
            graph_scene._scene_sender.set_selected_chunks(new_chunks)
        if clip_edges:
            # The new nodes' ports (typed ones especially) don't exist yet -- their proxy
            # widgets are built in deferred batches (see ChunkGraphScene._create_proxies_batch).
            # Same deferred-wiring pattern _create_and_connect_from_menu already uses.
            #
            # Hold the generic auto-view off until that wiring lands. Until it does, every
            # pasted chunk still looks like an unwired exec tail, and _ensure_default_view
            # takes tails[0] -- the FIRST node of the chain rather than its end. Reported as
            # the view landing on the first pasted node instead of the last, and "sometimes"
            # because it is a race with an 80ms timer.
            self._suppress_default_view = True
            QtCore.QTimer.singleShot(
                80, self,
                lambda paths=new_path_by_index, edges=list(clip_edges), created=list(new_chunks):
                    self._wire_pasted_edges(paths, edges, created),
            )
        return new_chunks

    def _materialize_children(
        self, parent_chunk: core.ChunkBase,
        clip_children: "list[_ClipboardNode]", clip_child_edges: "list[_ClipboardEdge]",
    ) -> None:
        """Recreate a ChunkGroup's copied children nested inside the newly created
        parent_chunk (recursing further for a nested sub-group), wiring their internal
        connections directly at the USD level via _author_raw_connection -- no live Qt ports
        exist for a group's children unless someone has actually dived into that group, so
        the ChunkPortItem-based _wire_pasted_edges path used for the top-level paste doesn't
        apply here."""
        if not clip_children:
            return
        scene = _chunk.get_scene()
        if not scene:
            return
        stage = scene.get_stage()
        if not stage:
            return
        nested, prev_ctx = self._push_nest_context(scene, parent_chunk)
        child_path_by_index: dict[int, str] = {}
        child_chunk_by_index: dict[int, core.ChunkBase] = {}
        try:
            for index, item in enumerate(clip_children):
                primary, chunks = self._instantiate_chunk_of_type(item.module_name)
                if primary is None:
                    continue
                # Guard the value-restoration + add_chunk window with _executing, NOT
                # set_blocked() -- see the matching, fuller comment in _materialize_at for
                # why: set_blocked() also suppresses per-param VALUE_CHANGED notices (a
                # restored menu value's own add_callback visibility toggle, e.g.), while
                # _executing only suppresses chunk_notice.py's reserved module.execute()
                # dispatch. ChunkBase.get_stage() always returns the real live stage
                # regardless of scene membership, so a module with real side-effecting
                # reactive code (e.g. Create Layer authoring an anon layer under whatever's
                # upstream *right now*) can run for real the INSTANT _apply_param_values
                # sets its value below -- well before add_chunk, so guarding any later than
                # this doesn't prevent it (live-confirmed: an orphaned layer nothing ever
                # cleans up). This group's children have no inner view yet (view chunks are
                # excluded from the copy, see _iter_group_children), so they go straight to
                # blocked (not executed) once past the guarded window -- the deferred
                # _wire_pasted_edges -> _recook_active_view call that already runs once this
                # whole paste/duplicate's top-level wiring lands (_materialize_at) recurses
                # into reachable groups (recook_for_view's is_group_chunk branch) and
                # unblocks+executes their contents in order, same as it would for a
                # hand-built group once it's wired into a view. An unwired duplicate stays
                # safely blocked until the user wires it in manually, matching a hand-built
                # group's behavior.
                blockable = [
                    c for c in chunks
                    if not chunk_usd.is_view_chunk(c) and not chunk_usd.is_group_chunk(c)
                ]
                for c in blockable:
                    c._executing = True
                # Apply values before add_chunk -- see the matching comment in
                # _materialize_at for why (a widget can be built synchronously as a side
                # effect of add_chunk's own notice dispatch, and reads its initial value
                # once, at construction).
                # Rows first: a list group starts empty on a fresh chunk, so its values
                # have nowhere to land until the rows it had are back.
                _apply_list_sizes(primary, getattr(item, 'list_sizes', []))
                _apply_param_values(primary, item.values)
                chunk_usd._create_node_pos = (item.rel_pos.x(), -item.rel_pos.y())
                try:
                    for c in chunks:
                        scene.add_chunk(c)
                    for c in blockable:
                        c._executing = False
                        c.set_blocked(True)
                finally:
                    chunk_usd._create_node_pos = None
                path = chunk_usd.get_chunk_prim_path(primary)
                child_path_by_index[index] = path
                child_chunk_by_index[index] = primary
                if item.children:
                    self._materialize_children(primary, item.children, item.child_edges)
        finally:
            if nested:
                scene._current_context = prev_ctx
        for edge in clip_child_edges:
            src_path = child_path_by_index.get(edge.src_index)
            dst_path = child_path_by_index.get(edge.dst_index)
            dst_chunk = child_chunk_by_index.get(edge.dst_index)
            if not src_path or not dst_path or dst_chunk is None:
                continue
            _author_raw_connection(stage, dst_path, edge.dst_attr, dst_chunk, src_path, edge.src_attr)

    def _group_selected(self) -> None:
        """Ctrl+G: put the selected chunks in a new chunkgroup.

        Exactly the sequence a user does by hand -- cut, make a group, dive in, paste --
        composed out of the operations that already do those things, rather than a bespoke
        move. Two things follow from that, both of them the point:

        The group then takes the selection's place in the chain: whatever fed the
        selection feeds the group, and whatever the selection fed is fed by it. Grouping
        the middle of a -> b -> c leaves a -> group -> c, so the graph still reads as the
        same flow with a box drawn round part of it.

        The boundary is read BEFORE the cut, while both ends are still where the user left
        them -- Cut heals the chain closed as it removes the nodes, and by the time it
        returns there is nothing left to say what used to attach where.

        And the paste lands inside the group by making it the current context for the
        duration, which is what diving in does.
        """
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene) or not graph_scene._scene_sender:
            return
        stage = graph_scene._stage
        if stage is None:
            return
        selected = [
            node for node in graph_scene._nodes.values()
            if node.chunk.has_state(ChunkFlags.State.SELECTED) and not node._is_view
        ]
        if not selected:
            return

        scene_sender = graph_scene._scene_sender
        centre_x = sum(n.pos().x() for n in selected) / len(selected)
        centre_y = sum(n.pos().y() for n in selected) / len(selected)

        # Read the boundary while it still exists. Cut heals the chain closed as it
        # removes the nodes, so afterwards nothing records what used to attach where.
        inside = {chunk_usd.get_chunk_prim_path(n.chunk) for n in selected}
        feeds_selection: "list[str]" = []
        fed_by_selection: "list[str]" = []
        for prim in stage.TraverseAll():
            if prim.GetTypeName() != _CHUNK_PRIM_TYPENAME:
                continue
            path = str(prim.GetPath())
            for attr in chunk_usd.iter_exec_inputs(prim):
                for conn in attr.GetConnections():
                    source = str(conn.GetPrimPath())
                    if path in inside and source not in inside and source not in feeds_selection:
                        feeds_selection.append(source)
                    elif source in inside and path not in inside and path not in fed_by_selection:
                        fed_by_selection.append(path)

        # Cut second, while the nodes are still here to be snapshotted.
        self._cut_selected_to_clipboard()
        if not _node_clipboard:
            return

        registry = chunk_registry.ChunkRegistry()
        mod = registry.get_chunk_module("chunkgroup") if registry.initialized else None
        if mod is None:
            return
        created = mod.create() if mod.loaded else mod.load()
        if not created:
            return
        group = created[0]

        context = self._graph_context()
        prev_ctx = scene_sender._current_context
        scene_sender._current_context = weakref.ref(context) if context is not None else None
        try:
            scene_sender.add_chunk(group)
        finally:
            scene_sender._current_context = prev_ctx

        group_prim = stage.GetPrimAtPath(Sdf.Path(chunk_usd.get_chunk_prim_path(group)))
        if group_prim and group_prim.IsValid():
            UsdUI.NodeGraphNodeAPI.Apply(group_prim).CreatePosAttr().Set(
                Gf.Vec2f(float(centre_x), float(centre_y))
            )

        # Paste into the group, which is what diving into it before Ctrl+V amounts to.
        # The paste takes its target from the graph's built context (_graph_context), not
        # from the scene's current one -- diving in is what normally changes that -- so the
        # group stands in as the built context for the duration.
        edges = list(_edge_clipboard)
        prev_built = graph_scene._built_context
        prev_ctx = scene_sender._current_context
        graph_scene._built_context = group
        scene_sender._current_context = weakref.ref(group)
        try:
            pasted = self._paste_clipboard_at(QtCore.QPointF(centre_x, centre_y))
        finally:
            scene_sender._current_context = prev_ctx
            graph_scene._built_context = prev_built

        # Wire the pasted chunks to each other here rather than leaving it to the paste's
        # own deferred pass. That pass wires through the graph's PORT items, and the chunks
        # just pasted are inside a group the graph is not currently showing -- so they have
        # no node, no ports, and nothing to wire. Going straight to the exec connection is
        # the same thing the ports would have authored.
        for edge in edges:
            if edge.src_index >= len(pasted) or edge.dst_index >= len(pasted):
                continue
            if not (edge.src_attr.endswith(_EXEC_OUTPUT.split(":")[-1])
                    and edge.dst_attr.endswith(_EXEC_INPUT.split(":")[-1])):
                continue  # a typed param wire; those go through ports and are left alone
            chunk_usd.connect_exec(
                stage, scene_sender,
                chunk_usd.get_chunk_prim_path(pasted[edge.src_index]),
                chunk_usd.get_chunk_prim_path(pasted[edge.dst_index]),
            )

        # Put the group where the selection was in the chain. These are fresh connections
        # onto a prim that did not exist a moment ago, not edits to existing ones, which is
        # why connect_exec is enough on its own here.
        group_path = chunk_usd.get_chunk_prim_path(group)
        for source in feeds_selection:
            chunk_usd.connect_exec(stage, scene_sender, source, group_path)
        for consumer in fed_by_selection:
            chunk_usd.connect_exec(stage, scene_sender, group_path, consumer)

        scene_sender.set_selected_chunks([group])

    def _duplicate_selected(self) -> None:
        """Ctrl+D: copy+paste the current selection in one step, without touching the
        Ctrl+C/Ctrl+V clipboard (so it never clobbers whatever was last explicitly copied).
        Lands at a fixed +40/+40 offset from the originals rather than the mouse cursor."""
        clip_nodes, clip_edges, top_left = self._snapshot_selection()
        if not clip_nodes or top_left is None:
            return
        anchor = QtCore.QPointF(top_left.x() + 40, top_left.y() + 40)
        self._materialize_at(clip_nodes, clip_edges, anchor)

    def _wire_pasted_edges(
        self,
        new_path_by_index: "dict[int, str]",
        edges: "list[_ClipboardEdge]",
        pasted: "list[core.ChunkBase] | None" = None,
    ) -> None:
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene):
            # Still release the auto-view hold, or it stays off for this view all session.
            if pasted:
                self._ensure_view_for_imported(pasted)
            return
        changed_chunks: set = set()
        for edge in edges:
            src_path = new_path_by_index.get(edge.src_index)
            dst_path = new_path_by_index.get(edge.dst_index)
            if not src_path or not dst_path:
                continue
            src_node = graph_scene._nodes.get(src_path)
            dst_node = graph_scene._nodes.get(dst_path)
            if src_node is None or dst_node is None:
                continue
            src_port = src_node.get_port(f"{src_path}.{edge.src_attr}")
            dst_port = dst_node.get_port(f"{dst_path}.{edge.dst_attr}")
            if src_port is None or dst_port is None:
                continue
            self._author_connection(src_port, dst_port)
            changed_chunks.add(dst_node.chunk)
        if changed_chunks:
            graph_scene._build_edges()
            self._recook_active_view(changed_chunks)
        if pasted:
            # Now that the chain is wired, "which of these is the tail" has a real answer.
            # Scoped to just the pasted nodes for the same reason the drag-drop import is:
            # an unrelated pre-existing tail elsewhere in the context is just as valid a
            # tail, and the generic pass has no way to prefer the thing the user just made.
            # Also releases the hold taken above, on every path out.
            self._ensure_view_for_imported(pasted)

    def _ensure_default_view(self, context) -> None:
        # A drag-and-drop chunk import is picking its own, scoped-to-just-the-imported-nodes
        # tail via _ensure_view_for_imported -- let it, rather than racing it here and
        # possibly latching onto an unrelated pre-existing tail elsewhere in the same context
        # first (see _ensure_view_for_imported's docstring for the reported symptom).
        if self._suppress_default_view:
            return
        registry = chunk_registry.ChunkRegistry()
        if not registry.initialized:
            return
        stage = registry.scene.get_stage()
        if not stage:
            return
        # Nothing to do if this context already has a view. A newly created node is blocked at
        # creation (see contextMenuEvent), so it doesn't execute until it's wired in and recook
        # unblocks it — no need to re-cook here.
        if self._context_has_view(registry.scene, context):
            return
        tails = chunk_usd.get_exec_tail_chunks(stage, registry.scene, context)
        if not tails:
            return
        self._create_view_node_for_prim(chunk_usd.get_chunk_prim_path(tails[0]))

    # ------------------------------------------------------------------
    # Zoom

    def wheelEvent(self, event: QtGui.QWheelEvent):
        factor = 1.08 if event.angleDelta().y() > 0 else 1 / 1.08
        self.scale(factor, factor)
        self._apply_lod()

    def _apply_lod(self) -> None:
        """Swap node bodies for the flat far-zoom form below _LOD_BODY_MIN_SCALE.

        Called after anything that changes the view transform, and only that -- panning and
        resizing move nodes through the viewport but change nothing about how legible a body
        is, so they cannot flip this. Qt already skips painting offscreen nodes on its own
        (measured: 0 on screen repaints in 2.8ms against 66ms for 33), so there is nothing
        to win by tracking position here either.

        The per-node walk runs only when the decision actually flips, so a wheel spin inside
        one band costs a single float comparison.
        """
        # Toggling item visibility schedules updates, which can re-enter here through Qt's
        # own handling -- keep it strictly one deep.
        if getattr(self, "_lod_busy", False):
            return
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene):
            return
        nodes = list(graph_scene._nodes.values())
        if not nodes:
            return

        show_bodies = self.transform().m11() >= _LOD_BODY_MIN_SCALE
        # No early-out on an unchanged band: a node that declined the last pass (still
        # settling) has to be revisited when it becomes ready, and _on_height_settled calls
        # straight back in here. set_body_visible is a no-op for a node already in the
        # requested state, so the repeat costs one attribute compare per node.
        self._lod_bodies_shown = show_bodies
        buried = self._buried_nodes(nodes) if show_bodies else frozenset()
        self._lod_busy = True
        try:
            for node in nodes:
                node.set_body_visible(show_bodies and node not in buried)
        finally:
            self._lod_busy = False

    @staticmethod
    def _buried_nodes(nodes: list) -> frozenset:
        """Nodes sitting exactly under another node, whose bodies are therefore never seen.

        Chunks created without an authored ui:nodegraph:node:pos all land on the same spot
        (see ChunkNodeItem._apply_usd_pos), so a network built programmatically arrives as one
        pile. Qt does no occlusion culling of its own, so it paints every body in that pile:
        57 nodes stacked measured 141.6ms a frame against 15.2ms with only the top one live.

        Deliberately exact overlaps only, keyed on position. Partial overlap would need real
        occlusion tests, and getting one wrong blanks a node the user can actually see.
        """
        top: dict = {}
        for node in nodes:
            try:
                key = (round(node.pos().x(), 1), round(node.pos().y(), 1))
            except RuntimeError:
                continue
            current = top.get(key)
            if current is None or node.zValue() >= current.zValue():
                top[key] = node
        if len(top) == len(nodes):
            return frozenset()  # nothing stacked, the common case
        keep = set(id(node) for node in top.values())
        return frozenset(node for node in nodes if id(node) not in keep)

    # ------------------------------------------------------------------
    # Pan — middle mouse or Space + left drag

    def _start_pan(self, pos: QtCore.QPoint):
        self._pan_active = True
        self._pan_start = pos
        self.setCursor(QtCore.Qt.ClosedHandCursor)

    def _stop_pan(self):
        self._pan_active = False
        self.setCursor(QtCore.Qt.ArrowCursor)

    def _owning_docker(self):
        """The ChunkWidgetDocker this view lives in, or None.

        Walked rather than stored: a graph view is reparented as tabs are built and torn
        down, and a stale reference would navigate a panel that is no longer on screen.
        """
        w = self.parent()
        while w is not None:
            if type(w).__name__ == "ChunkWidgetDocker":
                return w
            w = w.parent()
        return None

    def mousePressEvent(self, event: QtGui.QMouseEvent):
        # The mouse's side buttons retrace where you have been, the way they do in a
        # browser: dive into a group, press back, and you are out again. Handled here
        # because this is the widget under the cursor -- the panel that owns the history
        # never sees the press -- and delegated up rather than duplicated, so every graph
        # view in the panel shares one history.
        if event.button() in (QtCore.Qt.MouseButton.BackButton, QtCore.Qt.MouseButton.ForwardButton):
            docker = self._owning_docker()
            if docker is not None:
                back = event.button() == QtCore.Qt.MouseButton.BackButton
                if (docker.navigate_back() if back else docker.navigate_forward()):
                    event.accept()
                    return
            # Nowhere to go: swallow it anyway. Letting it fall through hands a side button
            # to whatever is underneath, which in usdview is the viewport.
            event.accept()
            return

        scene_pos = self.mapToScene(event.pos())
        hit = self.scene().itemAt(scene_pos, QtGui.QTransform())

        # Walk up item parent chain to find a proxy widget, if any.
        proxy = None
        item = hit
        while item is not None:
            if isinstance(item, QtWidgets.QGraphicsProxyWidget):
                proxy = item
                break
            item = item.parentItem()

        # Port left-click → start wire drag (drag off a connected input port to disconnect it).
        if isinstance(hit, ChunkPortItem):
            if event.button() == QtCore.Qt.LeftButton:
                self._start_wire_drag(hit, scene_pos)
                return

        # Track which proxy widget was clicked so the key router can forward
        # key events to it without relying on native focus (which usdview steals).
        # Find the specific child widget at the click position rather than the
        # top-level proxy widget, so key events reach e.g. QLineEdit directly.
        # itemAt can return a proxy whose C++ object was already freed by a rebuild (a stale
        # item lingering in the scene). Touching it (mapFromScene/widget) dereferences freed
        # memory → native crash, which no try/except can catch — so validity-check first.
        # Do NOT call into the proxy's C++ (mapFromScene/widget/childAt) here: on some Qt/
        # PySide6 builds that dereferences a proxy left in a bad state by a rebuild and crashes
        # usdview. Resolve the key-router target from the live node's pinned ChunkWidget via
        # Python identity instead — no C++ touch on the proxy.
        target_widget = None
        graph_scene = self.scene()
        if proxy is not None and isinstance(graph_scene, ChunkGraphScene):
            for n in graph_scene._nodes.values():
                if getattr(n, "_proxy", None) is proxy:
                    target_widget = getattr(n, "_chunk_widget", None)
                    break
        self._proxy_key_router.set_active(target_widget)
        self._proxy_key_router.set_view_active(True)
        if proxy is None:
            self.setFocus()

        if event.button() == QtCore.Qt.MiddleButton or (
            event.button() == QtCore.Qt.LeftButton
            and QtWidgets.QApplication.keyboardModifiers() == QtCore.Qt.AltModifier
        ):
            self._start_pan(event.pos())
        elif event.button() == QtCore.Qt.LeftButton and hit is None:
            # Empty canvas: start a rubber-band box-select. A plain click with no drag
            # movement resolves to an empty box on release, which clears the selection —
            # same end result as the old immediate-clear-on-press behavior.
            self._rubber_band_add = bool(event.modifiers() & QtCore.Qt.ShiftModifier)
            self._rubber_band_origin = event.pos()
            self._rubber_band.setGeometry(QtCore.QRect(self._rubber_band_origin, QtCore.QSize()))
            self._rubber_band.show()
            self._rubber_band_active = True
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent):
        if self._drag_src_port is not None:
            self._update_wire_drag(self.mapToScene(event.pos()))
            return
        if self._pan_active:
            delta = event.pos() - self._pan_start
            self._pan_start = event.pos()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
        elif self._rubber_band_active:
            rect = QtCore.QRect(self._rubber_band_origin, event.pos()).normalized()
            self._rubber_band.setGeometry(rect)
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent):
        if self._drag_src_port is not None and event.button() == QtCore.Qt.LeftButton:
            self._finish_wire_drag(self.mapToScene(event.pos()))
            return
        if self._pan_active and event.button() in (QtCore.Qt.MiddleButton, QtCore.Qt.LeftButton):
            self._stop_pan()
        elif self._rubber_band_active and event.button() == QtCore.Qt.LeftButton:
            self._finish_rubber_band()
        else:
            super().mouseReleaseEvent(event)

    def _finish_rubber_band(self) -> None:
        self._rubber_band_active = False
        rect_view = self._rubber_band.geometry()
        self._rubber_band.hide()
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene) or not graph_scene._scene_sender:
            return
        scene_rect = self.mapToScene(rect_view).boundingRect()
        boxed = [
            node.chunk for node in graph_scene._nodes.values()
            if node.sceneBoundingRect().intersects(scene_rect)
        ]
        if self._rubber_band_add:
            current = list(graph_scene._scene_sender.get_selected_chunks())
            merged = current + [c for c in boxed if c not in current]
            graph_scene._scene_sender.set_selected_chunks(merged)
        else:
            graph_scene._scene_sender.set_selected_chunks(boxed)

    # ------------------------------------------------------------------
    # Keyboard

    def keyPressEvent(self, event: QtGui.QKeyEvent):
        if event.key() == QtCore.Qt.Key.Key_Tab:
            self._open_tab_menu()
            event.accept()
            return
        if event.key() == QtCore.Qt.Key.Key_F:
            self._frame_nodes()
            event.accept()
            return
        if event.key() == QtCore.Qt.Key.Key_Escape:
            if self._drag_src_port is not None:
                self._cancel_wire_drag()
            else:
                self._proxy_key_router.set_active(None)
                self.setFocus()
            event.accept()
            return
        if event.key() == QtCore.Qt.Key.Key_Delete:
            self._delete_selected()
            event.accept()
            return
        if event.key() == QtCore.Qt.Key.Key_D:
            # Ctrl+D duplicates; bare D (no modifier) toggles disable -- same disambiguation
            # as Ctrl+V/bare-V below.
            if event.modifiers() & QtCore.Qt.ControlModifier:
                self._duplicate_selected()
            else:
                self._toggle_disable_selected()
            event.accept()
            return
        if event.key() == QtCore.Qt.Key.Key_C and (event.modifiers() & QtCore.Qt.ControlModifier):
            self._copy_selected_to_clipboard()
            event.accept()
            return
        if event.key() == QtCore.Qt.Key.Key_X and (event.modifiers() & QtCore.Qt.ControlModifier):
            self._cut_selected_to_clipboard()
            event.accept()
            return
        if event.key() == QtCore.Qt.Key.Key_V:
            # Ctrl+V pastes; bare V (no modifier) creates a view node -- same key, disambiguated
            # by modifier so the two long-standing single-key shortcuts don't collide.
            if event.modifiers() & QtCore.Qt.ControlModifier:
                self._paste_at_cursor()
            else:
                self._create_view_node()
            event.accept()
            return
        if event.key() == QtCore.Qt.Key.Key_G and (event.modifiers() & QtCore.Qt.ControlModifier):
            self._group_selected()
            event.accept()
            return
        if event.key() == QtCore.Qt.Key.Key_L:
            self._layout_selected_or_all()
            event.accept()
            return
        if event.key() in (QtCore.Qt.Key.Key_1, QtCore.Qt.Key.Key_2, QtCore.Qt.Key.Key_3):
            slot = event.key() - QtCore.Qt.Key.Key_0
            # Switch to that view slot only if a view exists for it; never toggle to "no view"
            # (slot 0), which would block the whole graph. Keeps an active view at all times.
            registry = chunk_registry.ChunkRegistry()
            stage = registry.scene.get_stage() if registry.initialized else None
            # Scope the check to THIS graph's context — slots are per-context now, so a global
            # scan would accept a slot that only exists in another tab/group, then recook
            # (context-scoped) finds no view here and mutes everything.
            if stage and chunk_usd.find_view_chunk_for_slot(
                slot, stage, registry.scene, self._graph_context()
            ):
                self._view_state.set_slot(slot)
            event.accept()
            return
        super().keyPressEvent(event)

    def _delete_selected(self) -> None:
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene):
            return
        selected = [
            node.chunk for node in graph_scene._nodes.values()
            if node.chunk.has_state(ChunkFlags.State.SELECTED)
        ]
        if not selected:
            return
        # Capture relevance BEFORE request_delete() touches anything -- see
        # mark_pending_delete_relevance's docstring for why it can't be judged afterward.
        self.mark_pending_delete_relevance(selected)
        for c in selected:
            c.request_delete()

    def _toggle_disable_selected(self) -> None:
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene):
            return
        for node in list(graph_scene._nodes.values()):
            if node.chunk.has_state(ChunkFlags.State.SELECTED):
                if node.chunk.has_state(ChunkFlags.State.DISABLED):
                    node.chunk.unset_state(ChunkFlags.State.DISABLED)
                else:
                    node.chunk.set_state(ChunkFlags.State.DISABLED)

    # ------------------------------------------------------------------
    # Wire drag

    def _start_wire_drag(self, port: ChunkPortItem, scene_pos: QtCore.QPointF) -> None:
        self._drag_src_port = port
        self._drag_detach_attr = None
        self._drag_detach_source_port = None
        self._drag_reused_edge = None
        # Grabbing an already-wired input port picks up its existing connection:
        # dropping on a new source rewires it, dropping on another compatible input
        # moves it there, dropping off any port disconnects it.
        if port.is_input:
            graph_scene = self.scene()
            if isinstance(graph_scene, ChunkGraphScene) and graph_scene._stage:
                attr = graph_scene._stage.GetAttributeAtPath(Sdf.Path(port.attr_path))
                conns = list(attr.GetConnections()) if attr and attr.IsValid() else []
                if conns:
                    self._drag_detach_attr = attr
                    self._drag_detach_source_port = self._find_sibling_output_port(conns[0], graph_scene)
                    # Drag the real edge itself (its src end stays put, dst end follows
                    # the cursor) instead of a separate rubber-band line on top of it.
                    for edge in graph_scene._edges:
                        if edge.dst_port is port:
                            self._drag_reused_edge = edge
                            edge.setPen(QtGui.QPen(edge.pen().color(), 1.5, QtCore.Qt.DashLine))
                            break
        if self._drag_reused_edge is None:
            self._drag_edge = QtWidgets.QGraphicsPathItem()
            pen = QtGui.QPen(port.brush().color(), 1.5, QtCore.Qt.DashLine)
            self._drag_edge.setPen(pen)
            self._drag_edge.setZValue(10)
            self.scene().addItem(self._drag_edge)
        else:
            self._drag_edge = None
        self._update_wire_drag(scene_pos)

    def _update_wire_drag(self, scene_pos: QtCore.QPointF) -> None:
        if self._drag_reused_edge is not None:
            edge = self._drag_reused_edge
            p1 = edge.src_port.scenePos()
            p2 = scene_pos
            dx = abs(p2.x() - p1.x()) * 0.5
            path = QtGui.QPainterPath(p1)
            path.cubicTo(p1.x() + dx, p1.y(), p2.x() - dx, p2.y(), p2.x(), p2.y())
            edge.prepareGeometryChange()
            edge.setPath(path)
            edge._p1, edge._p2 = p1, p2
        elif self._drag_edge is not None:
            p1 = self._drag_src_port.scenePos()
            p2 = scene_pos
            dx = abs(p2.x() - p1.x()) * 0.5
            path = QtGui.QPainterPath(p1)
            path.cubicTo(p1.x() + dx, p1.y(), p2.x() - dx, p2.y(), p2.x(), p2.y())
            self._drag_edge.setPath(path)
        else:
            return

        # Highlight compatible destination ports.
        graph_scene = self.scene()
        if isinstance(graph_scene, ChunkGraphScene):
            for node in graph_scene._nodes.values():
                for port in node._ports:
                    port.set_highlighted(
                        port is not self._drag_src_port
                        and self._drag_target_ok(port)
                    )

    def _nearest_target_port(self, scene_pos: QtCore.QPointF) -> Optional[ChunkPortItem]:
        """Closest valid drop target to scene_pos within _PORT_SNAP_PIXELS on screen, if any."""
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene):
            return None
        scale = self.transform().m11() or 1.0
        max_dist = _PORT_SNAP_PIXELS / scale
        best, best_dist = None, max_dist
        for node in graph_scene._nodes.values():
            for port in node._ports:
                if not self._drag_target_ok(port):
                    continue
                dist = QtCore.QLineF(port.scenePos(), scene_pos).length()
                if dist <= best_dist:
                    best, best_dist = port, dist
        return best

    def _finish_wire_drag(self, scene_pos: QtCore.QPointF) -> None:
        # Use items() instead of itemAt() so the rubber-band edge (Z=10) doesn't
        # occlude the port underneath it.
        hit = next(
            (item for item in self.scene().items(scene_pos) if isinstance(item, ChunkPortItem)),
            None,
        )
        # A pixel-perfect drop on the tiny port dot is hard to land, especially when zoomed
        # out. If the cursor didn't land squarely on a valid target, snap to the nearest
        # compatible port within a comfortable on-screen radius instead.
        if hit is None or not self._drag_target_ok(hit):
            snapped = self._nearest_target_port(scene_pos)
            if snapped is not None:
                hit = snapped
        graph_scene = self.scene()
        if hit is not None and self._drag_target_ok(hit):
            if hit.is_input == self._drag_src_port.is_input:
                # Dropped on another input of the same type as the one we grabbed:
                # move the connection there instead of rewiring in place.
                self._author_connection(self._drag_detach_source_port, hit)
                if isinstance(graph_scene, ChunkGraphScene):
                    self._disconnect_port(self._drag_src_port, self._drag_detach_attr, graph_scene)
            else:
                self._author_connection(self._drag_src_port, hit)
        elif self._drag_detach_attr is not None and hit is not self._drag_src_port:
            if isinstance(graph_scene, ChunkGraphScene):
                self._disconnect_port(self._drag_src_port, self._drag_detach_attr, graph_scene)
        elif hit is None and self._drag_detach_attr is None and self._drag_src_port is not None:
            # Fresh wire dragged out to empty space → open the chunk menu; whatever the user
            # picks is created and wired to the dragged port.
            src_attr = self._drag_src_port.attr_path
            src_is_input = self._drag_src_port.is_input
            self._cancel_wire_drag()
            self._create_and_connect_from_menu(src_attr, src_is_input, scene_pos)
            return
        self._cancel_wire_drag()

    def _create_and_connect_from_menu(
        self, src_attr_path: str, src_is_input: bool, scene_pos: QtCore.QPointF
    ) -> None:
        """Pop the chunk menu at the wire release point; create the chosen chunk and wire it
        to the dragged port. The connect is deferred until the create's rebuild has run (the
        new node's ports don't exist yet)."""
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene) or not graph_scene._scene_sender:
            return
        global_pos = self.viewport().mapToGlobal(self.mapFromScene(scene_pos))
        entry = FilterableChunkMenu().select_module(global_pos)
        if not entry:
            return
        chunks = entry.create()
        scene = _chunk.get_scene()
        if not scene:
            return
        # Block on creation (view-driven) — see contextMenuEvent. Here the node is about to be
        # wired to the dragged port, so the deferred connect's recook will unblock + cook it.
        context = self._graph_context()
        view_exists = self._context_has_view(scene, context)
        # Create the node at the wire-drop point instead of the far-away index default.
        chunk_usd._create_node_pos = (scene_pos.x(), -scene_pos.y())
        nested, prev_ctx = self._push_nest_context(scene, context)
        try:
            for c in chunks:
                if view_exists and not chunk_usd.is_view_chunk(c) and not chunk_usd.is_group_chunk(c):
                    c.set_blocked(True)
                scene.add_chunk(c)
        finally:
            if nested:
                scene._current_context = prev_ctx
            chunk_usd._create_node_pos = None
        new_chunk = next((c for c in chunks if not chunk_usd.is_view_chunk(c)), None)
        if new_chunk is None:
            return
        new_prim = chunk_usd.get_chunk_prim_path(new_chunk)
        QtCore.QTimer.singleShot(
            80, self,
            lambda: self._connect_ports_by_path(src_attr_path, src_is_input, new_prim),
        )

    def _connect_ports_by_path(self, src_attr_path: str, src_is_input: bool, new_prim_path: str) -> None:
        """Wire the (still-present) source port to a compatible port on the newly created node."""
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene):
            return
        src_port = None
        for node in graph_scene._nodes.values():
            p = node.get_port(src_attr_path)
            if p is not None:
                src_port = p
                break
        new_node = graph_scene._nodes.get(new_prim_path)
        if src_port is None or new_node is None:
            return
        # Target the opposite role on the new node (src output → new input, or new output → src input).
        want_input = not src_is_input
        target = next(
            (p for p in new_node._ports if p.is_input == want_input and self._can_connect(src_port, p)),
            None,
        )
        if target is not None:
            self._author_connection(src_port, target)

    def _cancel_wire_drag(self) -> None:
        if self._drag_edge is not None:
            self.scene().removeItem(self._drag_edge)
            self._drag_edge = None
        if self._drag_reused_edge is not None:
            # If a connection change happened, _build_edges() already tore this
            # item down — touching it further is a no-op (RuntimeError guarded).
            # Otherwise, snap it back to its real solid path/pen.
            try:
                edge = self._drag_reused_edge
                edge.setPen(QtGui.QPen(edge.src_port.brush().color(), 1.5))
                edge.update_path()
            except RuntimeError:
                pass
            self._drag_reused_edge = None
        graph_scene = self.scene()
        if isinstance(graph_scene, ChunkGraphScene):
            for node in graph_scene._nodes.values():
                for port in node._ports:
                    port.set_highlighted(False)
        self._drag_src_port = None
        self._drag_detach_attr = None
        self._drag_detach_source_port = None

    def _can_connect(self, a: ChunkPortItem, b: ChunkPortItem) -> bool:
        if a.port_type != b.port_type:
            return False
        if a.is_input == b.is_input:
            return False
        if Sdf.Path(a.attr_path).GetPrimPath() == Sdf.Path(b.attr_path).GetPrimPath():
            return False
        return True

    def _drag_target_ok(self, hit: ChunkPortItem) -> bool:
        """Whether `hit` is a valid drop target for the wire currently being dragged."""
        src = self._drag_src_port
        if hit is src or hit.port_type != src.port_type:
            return False
        if hit.is_input != src.is_input:
            return self._can_connect(src, hit)
        # Same role as the grabbed port — only meaningful when we picked up an
        # existing connection at an input port, moving its destination elsewhere.
        if not src.is_input or self._drag_detach_source_port is None:
            return False
        return (
            Sdf.Path(hit.attr_path).GetPrimPath()
            != Sdf.Path(self._drag_detach_source_port.attr_path).GetPrimPath()
        )

    def _find_sibling_output_port(
        self, conn_path: Sdf.Path, graph_scene: "ChunkGraphScene"
    ) -> Optional[ChunkPortItem]:
        """Resolve the ChunkPortItem for the source end of a connection. Exec connections
        point straight at outputs:chunk; typed param connections point at the source's
        inputs:param (see _author_connection), so the matching output port lives on the
        sibling outputs:param attribute."""
        src_prim_path = str(conn_path.GetPrimPath())
        src_node = graph_scene._nodes.get(src_prim_path)
        if src_node is None:
            return None
        attr_name = conn_path.name
        if attr_name.startswith("inputs:"):
            attr_name = "outputs:" + attr_name[len("inputs:"):]
        return src_node.get_port(f"{src_prim_path}.{attr_name}")

    def _author_connection(self, a: ChunkPortItem, b: ChunkPortItem) -> None:
        src, dst = (b, a) if a.is_input else (a, b)
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene) or not graph_scene._stage:
            return
        dst_attr = graph_scene._stage.GetAttributeAtPath(Sdf.Path(dst.attr_path))
        if not (dst_attr and dst_attr.IsValid()):
            # Attribute doesn't exist yet (e.g. inputs:chunk on a not-yet-connected node).
            prim = graph_scene._stage.GetPrimAtPath(Sdf.Path(dst.attr_path).GetPrimPath())
            if not prim.IsValid():
                return
            sdf_type = Sdf.ValueTypeNames.Token if dst.port_type == _PORT_EXEC else Sdf.ValueTypeNames.String
            dst_attr = prim.CreateAttribute(dst.attr_path.rsplit(".", 1)[-1], sdf_type, custom=False)
            if not dst_attr.IsValid():
                return
        # Exec connections reference outputs:chunk; typed param connections reference inputs:param.
        if src.port_type == _PORT_EXEC:
            src_conn_path = Sdf.Path(src.attr_path)
        else:
            src_conn_path = Sdf.Path(src.attr_path.replace(".outputs:", ".inputs:", 1))
        # Chunk-prim bookkeeping (this connection) follows Edit Target now — update in
        # place if the attr already had a spec somewhere, else resolve where a brand
        # new one should go, rather than relying on the stage's ambient edit target.
        home = chunk_utils.get_attr_home_layer(dst_attr)
        if home:
            target = home
        else:
            dst_node = graph_scene._nodes.get(str(Sdf.Path(dst.attr_path).GetPrimPath()))
            target = chunk_utils.get_bookkeeping_layer(graph_scene._stage, getattr(dst_node, "chunk", None))
        with Usd.EditContext(graph_scene._stage, target):
            dst_attr.SetConnections([src_conn_path])

        # For typed params, also write an expression so the value resolves at runtime.
        # The user can clear it from the QLineEdit to keep the wiring without expression eval.
        # ChunkVectorBase supports this too (whole-vector references, e.g. @Node.vecParam@ —
        # no per-component wiring).
        dst_node = graph_scene._nodes.get(str(Sdf.Path(dst.attr_path).GetPrimPath()))
        if src.port_type != _PORT_EXEC:
            src_node_name = Sdf.Path(src.attr_path).GetPrimPath().name
            src_param_name = src.attr_path.rsplit(":", 1)[-1]
            dst_param_name = dst.attr_path.rsplit(":", 1)[-1]
            if dst_node:
                param = dst_node.chunk.get_param(dst_param_name)
                if param is not None:
                    param.value = f"@{src_node_name}.{src_param_name}@"

        # Wiring a new exec edge can put the node (and everything downstream) under a
        # different Edit Target — re-home them to honor the target at their new position.
        # (This drag-drop path authors inputs:chunk directly, so DEPENDENCY_ADDED — which
        # ChunkStageSync._on_dependency_added also reconciles on — never fires here.)
        if src.port_type == _PORT_EXEC:
            chunk_usd.reconcile_edit_targets(graph_scene._stage, chunk_usd.ChunkStageSync()._scene)
        graph_scene._build_edges()
        # A new connection can only change the active view's cook set if its destination side
        # is (or feeds) the view -- wiring two nodes off in an unrelated part of the graph can't
        # add anything reachable from the view. Unresolvable dst_node (shouldn't happen once
        # dst_attr validated above) falls back to an unconditional recook to stay safe.
        self._recook_active_view({dst_node.chunk} if dst_node else None)

    def _disconnect_port(self, port: ChunkPortItem, attr: Usd.Attribute, graph_scene: "ChunkGraphScene") -> None:
        changed_nodes = set()
        port_node = graph_scene._nodes.get(str(Sdf.Path(port.attr_path).GetPrimPath()))
        if port_node:
            changed_nodes.add(port_node)
        # For exec ports: rewire predecessor → successor before clearing.
        if port.port_type == _PORT_EXEC:
            prim_path = Sdf.Path(port.attr_path).GetPrimPath()
            out_attr_path = str(prim_path) + ".outputs:chunk"
            # Find any successor whose exec input points to this node's outputs:chunk. Every
            # slot, not just inputs:chunk: a merge fed through slot 2 is a successor too, and
            # missing it would leave that slot wired to a node the user just detached.
            for node in graph_scene._nodes.values():
                for succ_attr in chunk_usd.iter_exec_inputs(node.prim):
                    if not any(str(c) == out_attr_path for c in succ_attr.GetConnections()):
                        continue
                    # Rewire successor to this node's predecessor.
                    pred_conns = attr.GetConnections()
                    if pred_conns:
                        succ_attr.SetConnections(list(pred_conns))
                    else:
                        succ_attr.ClearConnections()
                    changed_nodes.add(node)
                    break
        chunk_usd.ChunkStageSync().clear_attr_connections(attr)
        # For typed ports, re-execute the owning chunk so shader-level connections
        # (e.g. UsdUVTexture.inputs:st) are cleared to match the disconnected chunk state.
        # Resetting param.value to default_value also clears a ChunkVectorBase's
        # expression string (its value setter treats a non-string Sequence as "not
        # an expression"), same as it does for scalar params.
        if port.port_type != _PORT_EXEC:
            prim_path = str(Sdf.Path(port.attr_path).GetPrimPath())
            node = graph_scene._nodes.get(prim_path)
            if node and getattr(node, 'chunk', None):
                if port.is_input:
                    attr_local = port.attr_path.rsplit(".", 1)[-1]
                    if attr_local.startswith("inputs:"):
                        param_name = attr_local[len("inputs:"):].replace(":", ".")
                        param = node.chunk.get_param(param_name)
                        if param is not None:
                            param.value = param.default_value
                node.chunk.execute()
        # Detaching an exec wire can change which Edit Target applies to the node (and its
        # downstream) — re-home them to the target that survives at their new position.
        chunk_usd.reconcile_edit_targets(graph_scene._stage, chunk_usd.ChunkStageSync()._scene)
        graph_scene._build_edges()
        # See _author_connection: a disconnect can only affect the view's cook set if the
        # touched node(s) were themselves reachable from the view.
        self._recook_active_view({n.chunk for n in changed_nodes} if changed_nodes else None)

    def _clear_input_connection(self, port: ChunkPortItem, graph_scene: "ChunkGraphScene") -> bool:
        """Clear one input port's connections (no exec chain-heal). For typed ports also reset
        the param and re-execute so shader-level connections clear. Returns True if it changed."""
        attr = graph_scene._stage.GetAttributeAtPath(Sdf.Path(port.attr_path))
        if not (attr and attr.IsValid()) or not attr.GetConnections():
            return False
        chunk_usd.ChunkStageSync().clear_attr_connections(attr)
        if port.port_type != _PORT_EXEC:
            prim_path = str(Sdf.Path(port.attr_path).GetPrimPath())
            node = graph_scene._nodes.get(prim_path)
            if node and getattr(node, "chunk", None):
                attr_local = port.attr_path.rsplit(".", 1)[-1]
                if attr_local.startswith("inputs:"):
                    param_name = attr_local[len("inputs:"):].replace(":", ".")
                    param = node.chunk.get_param(param_name)
                    if param is not None:
                        param.value = param.default_value
                node.chunk.execute()
        return True

    def _shake_disconnect_node(self, node: "ChunkNodeItem") -> None:
        """Disconnect all of a node's wires, but heal the exec chain: for A -> C -> B, shaking C
        rewires B's exec input to A (A -> B) before isolating C. Data/typed connections to C are
        just cleared (no generic heal)."""
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene) or not graph_scene._stage:
            return
        stage = graph_scene._stage
        node_prim = str(node.prim.GetPath())
        changed = False

        # Heal the exec chain: rewire each successor's exec input to this node's
        # predecessor(s). The predecessor is read from slot 0 only — a successor has one slot
        # to inherit into, so it can be handed exactly one branch, and slot 0 is the primary
        # one (same choice as chunk_usd.heal_exec_chain_on_delete).
        exec_in = stage.GetAttributeAtPath(Sdf.Path(node_prim + ".inputs:chunk"))
        pred_conns = list(exec_in.GetConnections()) if (exec_in and exec_in.IsValid()) else []
        out_chunk_path = node_prim + ".outputs:chunk"
        for other in graph_scene._nodes.values():
            if other is node:
                continue
            # Successors are matched across every slot, though: a merge fed through slot 2
            # would otherwise keep pointing at the node being shaken loose.
            for succ in chunk_usd.iter_exec_inputs(other.prim):
                if not any(str(c) == out_chunk_path for c in succ.GetConnections()):
                    continue
                # Write to succ's own home layer, not the stage's ambient edit target (root,
                # since nothing here calls SetEditTarget) -- an interactively wired
                # connection almost always lives on the session layer, stronger than root,
                # so healing on root left the original opinion (pointing at the node being
                # shaken loose) in charge and the heal silently had no visible effect. Same
                # fix as chunk_usd.heal_exec_chain_on_delete.
                home = chunk_utils.get_attr_home_layer(succ) or chunk_utils.get_bookkeeping_layer(
                    stage, getattr(other, "chunk", None)
                )
                with Usd.EditContext(stage, home):
                    if pred_conns:
                        succ.SetConnections(pred_conns)
                    else:
                        succ.ClearConnections()
                changed = True

        # This node's own input connections.
        for port in node._ports:
            if port.is_input and self._clear_input_connection(port, graph_scene):
                changed = True
        # Any remaining input on another node still referencing this one (data/typed edges).
        for other in graph_scene._nodes.values():
            if other is node:
                continue
            for port in other._ports:
                if not port.is_input:
                    continue
                attr = stage.GetAttributeAtPath(Sdf.Path(port.attr_path))
                if not (attr and attr.IsValid()):
                    continue
                if any(str(c.GetPrimPath()) == node_prim for c in attr.GetConnections()):
                    if self._clear_input_connection(port, graph_scene):
                        changed = True
        if changed:
            chunk_usd.reconcile_edit_targets(stage, chunk_usd.ChunkStageSync()._scene)
            graph_scene._build_edges()
            self._recook_active_view()

    def _show_chunk_menu(self, node: "ChunkNodeItem", global_pos: QtCore.QPoint) -> None:
        # Copy/Cut/Duplicate act on the current selection, not just this one right-clicked
        # node -- right-clicking a node that's already part of a multi-selection must leave
        # that selection alone, but right-clicking an unselected node should select just it
        # first (matching a plain left-click), or those actions would silently act on
        # whatever else happened to be selected instead.
        graph_scene = self.scene()
        sender = getattr(graph_scene, "_scene_sender", None) if graph_scene else None
        if sender and not node.chunk.has_state(ChunkFlags.State.SELECTED):
            sender.set_selected_chunks([node.chunk])
        self._view_state.show_chunk_menu(node.chunk, str(node.prim.GetPath()), global_pos, graph_view=self)

    def _exec_menu(self, menu: QtWidgets.QMenu, pos: QtCore.QPoint):
        """Thin, overridable seam around QMenu.exec() -- see ViewSlotState._exec_menu's
        docstring for why (PySide6 won't let tests monkeypatch QMenu.exec itself)."""
        return menu.exec(pos)

    # ------------------------------------------------------------------
    # View nodes

    def _create_view_node(self) -> None:
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene) or not graph_scene._scene_sender or not graph_scene._stage:
            return
        selected = [n for n in graph_scene._nodes.values() if n.chunk.has_state(ChunkFlags.State.SELECTED)]
        if not selected:
            return
        self._create_view_node_for(selected[0])

    def _create_view_node_for(self, target_node: "ChunkNodeItem") -> None:
        self._create_view_node_for_prim(str(target_node.prim.GetPath()))

    def _activate_menu_view(self, view_chunk, context, stage) -> None:
        """A view created from the chunk menu is inert — it has no view:slot and isn't active,
        so it does nothing. Assign it a free slot (scoped to this graph) on its prim and make it
        the active view, so it behaves like a view from 'Set View' (just unwired until connected)."""
        vp = stage.GetPrimAtPath(Sdf.Path(chunk_usd.get_chunk_prim_path(view_chunk)))
        if not vp.IsValid():
            return
        if vp.GetAttribute("view:slot").Get() is not None:
            return  # already set up
        used = set()
        for c in chunk_registry.ChunkRegistry().scene.get_chunks():
            if not (chunk_usd.is_view_chunk(c) and c is not view_chunk
                    and chunk_usd.is_node_in_context(c, context)):
                continue
            p = stage.GetPrimAtPath(Sdf.Path(chunk_usd.get_chunk_prim_path(c)))
            s = p.GetAttribute("view:slot").Get() if p.IsValid() else None
            if s is not None:
                used.add(s)
        slot = next((s for s in (1, 2, 3) if s not in used), None)
        if slot is None:
            return
        # Same first-time chunk-prim bookkeeping as _create_view_node_for_prim's slot write:
        # author it wherever THIS chunk's Edit Target resolves to, not wherever the stage's
        # ambient edit target happens to point. Left ambient, the marker lands in a layer
        # outside the composed result, view:slot reads back invalid, find_view_chunk_for_slot
        # finds no view, and the whole cook set collapses to "nothing cooks".
        with Usd.EditContext(stage, chunk_utils.get_bookkeeping_layer(stage, view_chunk)):
            sa = vp.CreateAttribute("view:slot", Sdf.ValueTypeNames.Int, custom=True)
            sa.SetVariability(Sdf.VariabilityUniform)
            sa.Set(slot)
        # `self` as the timer's context object, so Qt cancels the callback if this view is
        # destroyed before it fires. Without it the lambda outlives the view: the queued
        # timer is still in the application's event loop, _view_state is a child QObject
        # that went with the view, and set_slot emitting on the deleted C++ object raises
        # "Signal source has been deleted". Closing a graph tab within a frame of a slot
        # change is the way to hit that in the app; a test whose event queue is drained by
        # whatever runs next is how it actually showed up.
        QtCore.QTimer.singleShot(0, self, lambda s=slot: self._view_state.set_slot(s))

    def _create_view_node_for_prim(self, target_prim_path: str) -> None:
        """Create a view node wired to target_prim_path using only USD stage data."""
        registry = chunk_registry.ChunkRegistry()
        if not registry.initialized:
            return
        stage = registry.scene.get_stage()
        if not stage:
            return

        target_prim = stage.GetPrimAtPath(Sdf.Path(target_prim_path))
        if not target_prim.IsValid():
            return
        if target_prim.GetAttribute("info:chunkType").Get() == "view":
            return

        # If a view is already wired to this prim, activate it instead of creating a duplicate.
        for prim in stage.Traverse():
            if prim.GetAttribute("info:chunkType").Get() != "view":
                continue
            exec_in = prim.GetAttribute(_EXEC_INPUT)
            if exec_in.IsValid():
                for conn_path in exec_in.GetConnections():
                    if str(conn_path.GetPrimPath()) == target_prim_path:
                        existing_slot = prim.GetAttribute("view:slot").Get()
                        if existing_slot is not None:
                            self._view_state.set_slot(existing_slot)
                        return

        # Find an unused slot — scoped to the target's container (the view is created as a sibling
        # of the target). Each graph (tab/group) has its own slots 1-3; a GLOBAL scan runs out of
        # slots once views exist in a few contexts, so 'Set View' silently failed, and the View{slot}
        # name incremented globally across contexts.
        target_parent = str(Sdf.Path(target_prim_path).GetParentPath())
        used_slots: set = set()
        for prim in stage.Traverse():
            if prim.GetAttribute("info:chunkType").Get() != "view":
                continue
            if str(prim.GetPath().GetParentPath()) != target_parent:
                continue
            s = prim.GetAttribute("view:slot").Get()
            if s is not None:
                used_slots.add(s)
        slot = next((s for s in [1, 2, 3] if s not in used_slots), None)
        if slot is None:
            return

        mod = registry.get_chunk_module("view")
        if not mod:
            return
        chunks = mod.create() if mod.loaded else mod.load()
        if not chunks:
            return
        view_chunk = chunks[0]
        view_chunk.name = f"View{slot}"
        view_chunk.set_state(ChunkFlags.State.COLLAPSED)

        # Read target pos from USD before add_chunk fires CHUNK_LOADED and rebuilds the graph.
        node_api = UsdUI.NodeGraphNodeAPI(target_prim)
        pos_attr = node_api.GetPosAttr()
        raw_pos = pos_attr.Get() if (pos_attr and pos_attr.IsValid()) else None
        tx, ty = (float(raw_pos[0]), float(raw_pos[1])) if raw_pos is not None else (0.0, 0.0)

        scene = _chunk.get_scene()
        if not scene:
            return
        scene.add_chunk(view_chunk)

        view_prim_path = f"{chunk_usd._resolve_chunk_root(view_chunk)}/{view_chunk.name}"
        stage = registry.scene.get_stage()
        if not stage:
            return
        view_prim = stage.GetPrimAtPath(Sdf.Path(view_prim_path))
        if not view_prim.IsValid():
            return

        # This is all first-time bookkeeping for a freshly-created view_chunk — the
        # chunk-prim's exec wiring, its view:slot marker, and its initial position —
        # so it belongs wherever this chunk's Edit Target currently resolves to, not
        # wherever the stage's ambient edit target happens to be.
        with Usd.EditContext(stage, chunk_utils.get_bookkeeping_layer(stage, view_chunk)):
            exec_in = view_prim.GetAttribute(_EXEC_INPUT)
            if not exec_in.IsValid():
                exec_in = view_prim.CreateAttribute(_EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False)
            exec_in.SetConnections([Sdf.Path(f"{target_prim_path}.{_EXEC_OUTPUT}")])

            slot_attr = view_prim.CreateAttribute("view:slot", Sdf.ValueTypeNames.Int, custom=True)
            slot_attr.SetVariability(Sdf.VariabilityUniform)
            slot_attr.Set(slot)

            UsdUI.NodeGraphNodeAPI.Apply(view_prim).CreatePosAttr().Set(
                Gf.Vec2f(tx + _NODE_WIDTH + 40, ty - 80)
            )

        # Context object, as above — the view can be gone before this fires.
        QtCore.QTimer.singleShot(0, self, lambda: self._view_state.set_slot(slot))

    def set_view_state(self, vs: ViewSlotState) -> None:
        self._view_state.slot_changed.disconnect(self._on_view_slot_changed)
        self._view_state.create_view_for.disconnect(self._on_create_view_for_path)
        self._view_state.pin_changed.disconnect(self._on_pin_changed)
        self._view_state = vs
        vs.slot_changed.connect(self._on_view_slot_changed)
        vs.create_view_for.connect(self._on_create_view_for_path)
        vs.pin_changed.connect(self._on_pin_changed)

    def _on_pin_changed(self, target_chunk: core.ChunkBase, pinned: bool) -> None:
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene):
            return
        node = graph_scene._nodes.get(chunk_usd.get_chunk_prim_path(target_chunk))
        if node is not None:
            node.set_pinned(pinned)

    def _on_create_view_for_path(self, prim_path: str) -> None:
        self._create_view_node_for_prim(prim_path)

    def _on_view_slot_changed(self, slot: int) -> None:
        self._recook_active_view()
        self.viewport().update()

    def _restore_active_view(self, changed_chunks: Optional[Iterable[core.ChunkBase]] = None) -> None:
        """Adopt this context's saved view as the active view after a (re)build.

        A freshly built graph starts at slot 0 ("no view"), which cooks everything — but a
        loaded file whose graph has view chunks should come up with its view driving
        execution, as it was when saved. Both groups and tabs persist their slot on the
        context prim (chunk:activeView, written by _recook_active_view); files saved
        before that marker existed fall back to the lowest existing view:slot. With a
        slot already active (context switch, not a load) this is just the recook the
        build always did.

        changed_chunks is passed straight through to _recook_active_view (see its docstring)
        on the paths that recook the CURRENT slot. The set_slot path below deliberately does
        not scope: changing slot is a genuine view switch and always needs a full recook.
        """
        if self._view_state.active_slot != 0:
            self._recook_active_view(changed_chunks)
            return
        registry = chunk_registry.ChunkRegistry()
        if not registry.initialized:
            return
        stage = registry.scene.get_stage()
        if not stage:
            return
        context = self._graph_context()
        slot = None
        if context is not None:
            gp = stage.GetPrimAtPath(Sdf.Path(chunk_usd.get_chunk_prim_path(context)))
            if gp and gp.IsValid():
                a = gp.GetAttribute("chunk:activeView")
                if a and a.IsValid():
                    slot = a.Get()
        if not slot or not chunk_usd.find_view_chunk_for_slot(slot, stage, registry.scene, context):
            slots = []
            for c in registry.scene.get_chunks():
                if not (chunk_usd.is_view_chunk(c) and chunk_usd.is_node_in_context(c, context)):
                    continue
                p = stage.GetPrimAtPath(Sdf.Path(chunk_usd.get_chunk_prim_path(c)))
                s = p.GetAttribute("view:slot").Get() if p.IsValid() else None
                if s:
                    slots.append(int(s))
            slot = min(slots) if slots else None
        if slot:
            # set_slot fires slot_changed → _on_view_slot_changed → recook
            self._view_state.set_slot(int(slot))
        else:
            self._recook_active_view(changed_chunks)

    def _recook_active_view(self, changed_chunks: Optional[Iterable[core.ChunkBase]] = None) -> None:
        """(Re)apply view-driven execution for this context's active view.

        Called on every structural change (wire, disconnect, delete, create) and on view
        switch so which chunks cook stays live. With no active view (slot 0) it falls back
        to legacy "run everything". Must NOT be called from inside a graph rebuild — the
        execute() it triggers would author, fire ObjectsChanged, and loop.

        changed_chunks: the specific chunk(s) a caller knows triggered this call (e.g. the
        node just created/deleted/(dis)connected). recook_for_view re-*executes* its whole
        cook set every time it runs (not just chunks that actually changed — see its own
        docstring), so calling it unconditionally on every structural edit anywhere in the
        graph was re-running unrelated upstream chunks (a stray "Scattered N instances" print
        from creating/deleting a node nowhere near the active view) and re-dimming every
        blocked chunk for no reason. When changed_chunks is given and none of them are the
        view or already reachable from it, the edit provably can't change what the view cooks
        (cook-set membership is exactly graph reachability from the view), so it's skipped.
        Omit changed_chunks (the default) to always recook, as before — used for genuinely
        graph-wide triggers like a view switch.
        """
        # This is always reachable through a deferred QTimer.singleShot (build()'s own
        # debounced post-build recook -- see _schedule_restore_active_view -- and
        # ChunkGraphScene._on_chunk_deleted's debounce both end up here later, not
        # synchronously) — by the time it fires this view can have been torn down already
        # (e.g. a test's fixture finished, or the tab/context it belonged to was closed),
        # which crashes as "Internal C++ object already deleted" the moment anything below
        # touches self. A no-op recook on a dead view is correct: there's nothing left to
        # drive.
        if not shiboken6.isValid(self):
            return
        # Only the CURRENTLY SELECTED tab should drive the (global) stage. Gate on the owning
        # tab stack's visibility, NOT self.isVisible(): the graph is page 1 of a QStackedWidget,
        # so in Params mode this widget is hidden even though its tab is current — self.isVisible()
        # would then wrongly skip the recook and the viewport wouldn't follow tab switches while
        # on the Params tab. The stack is visible whenever its tab is current, either page showing.
        stack = self.parentWidget()
        if stack is not None and not stack.isVisible():
            return
        registry = chunk_registry.ChunkRegistry()
        if not registry.initialized:
            return
        stage = registry.scene.get_stage()
        if not stage:
            return
        context = self._graph_context()
        slot = self._view_state.active_slot
        view_chunk = (
            chunk_usd.find_view_chunk_for_slot(slot, stage, registry.scene, context) if slot else None
        )
        if changed_chunks is not None and not self._could_affect_view_cook(
            changed_chunks, stage, registry.scene, view_chunk
        ):
            return
        # Persist the active view slot on the context prim. For groups a parent cook picks
        # the same inner view from it (find_group_active_view); for tabs it round-trips the
        # last-active view through save/load (_restore_active_view reads it). Only a real
        # slot is written — 0 just means "not restored yet" and must not clobber the saved
        # value before the deferred restore has read it.
        if context is not None and slot:
            gp = stage.GetPrimAtPath(Sdf.Path(chunk_usd.get_chunk_prim_path(context)))
            if gp and gp.IsValid():
                prev_w = chunk_usd.chunk_notice._usd_write_active
                chunk_usd.chunk_notice._usd_write_active = True
                try:
                    # Bookkeeping like any other chunk-prim attr — update in place if it
                    # already has a spec, else resolve fresh via the group's current Edit
                    # Target, rather than relying on the stage's ambient one (see
                    # set_chunk_pinned for the same "second mutation doesn't propagate"
                    # class of bug this avoids).
                    active_view_attr = gp.GetAttribute("chunk:activeView")
                    home = (
                        chunk_utils.get_attr_home_layer(active_view_attr)
                        if active_view_attr and active_view_attr.IsValid() else None
                    )
                    target = home or chunk_utils.get_bookkeeping_layer(stage, context)
                    with Usd.EditContext(stage, target):
                        gp.CreateAttribute("chunk:activeView", Sdf.ValueTypeNames.Int, custom=True).Set(int(slot))
                finally:
                    chunk_usd.chunk_notice._usd_write_active = prev_w
        if view_chunk is not None:
            for c in registry.scene.get_chunks():
                if c.has_state(ChunkFlags.State.SELECTED):
                    c.set_selected(False)
            view_chunk.set_selected(True)
        chunk_usd.recook_focus(stage, registry.scene, context, view_chunk)

    @staticmethod
    def _could_affect_view_cook(changed_chunks, stage, scene, view_chunk) -> bool:
        """True if any of changed_chunks could plausibly change what view_chunk's recook
        computes -- i.e. one of them IS the view, or is already reachable from it. Cook-set
        membership is exactly graph reachability from the view (get_exec_upstream_chunks), so
        a chunk that was never part of that reachable set can't affect it: a brand-new
        disconnected node can't be reachable (nothing points to/from it yet), and
        deleting/disconnecting something that was never reachable can't remove anything from
        the set either. No active view means nothing is cooking regardless of graph structure,
        so it can't matter there either.
        """
        if view_chunk is None:
            return False
        cook_set = set(chunk_usd.get_exec_upstream_chunks(view_chunk, stage, scene))
        if any(c is view_chunk or c in cook_set for c in changed_chunks):
            return True
        # A chunk created INSIDE a reachable group is not itself reachable -- nothing in the
        # outer graph points at it -- but it is part of that group's cooked content, and its
        # layer still has to be ranked into the group's tier (recook_for_view's descendant
        # promotion). AMDMaterialXLibrary builds its Reference and BindMaterial children from
        # a button callback, so they arrive exactly this way; bailing out here left their
        # layers appended weakest, below the Sublayer feeding the group, so the material they
        # authored lost to the binding the referenced asset already carried and the assigned
        # material never appeared. Prim-path nesting is the same test the ranking pass uses.
        prefixes = tuple(str(chunk_usd.get_chunk_prim_path(c)) + "/" for c in cook_set)
        if not prefixes:
            return False
        return any(str(chunk_usd.get_chunk_prim_path(c)).startswith(prefixes) for c in changed_chunks)

    @staticmethod
    def mark_pending_delete_relevance(chunks) -> None:
        """Capture, for every live ChunkGraphView, whether deleting `chunks` could affect its
        active view's cook set -- BEFORE request_delete() runs on any of them.

        Must be called from wherever a delete is actually triggered, not from inside the
        CHUNK_DELETED handler: request_delete() force-unblocks the chunk as a prerequisite for
        its own notice to fire at all, and a sibling listener (ChunkStageSync) may have already
        healed the exec chain around it by the time the notice arrives -- both erase the very
        state this needs to read. See ChunkGraphView._delete_selected for the original case
        this covers (Delete key in the graph); also called from the shared right-click "remove"
        menu action (chunk_qt_widgets.show_chunk_menu), which deletes a chunk directly and
        bypasses _delete_selected entirely -- any future UI surface that deletes chunks needs
        this same call before it, or it silently falls back to always recooking.
        """
        registry = chunk_registry.ChunkRegistry()
        if not registry.initialized:
            return
        stage = registry.scene.get_stage()
        if not stage:
            return
        app = QtWidgets.QApplication.instance()
        if app is None:
            return
        for w in app.allWidgets():
            if not isinstance(w, ChunkGraphView):
                continue
            context = w._graph_context()
            slot = w._view_state.active_slot
            view_chunk = (
                chunk_usd.find_view_chunk_for_slot(slot, stage, registry.scene, context) if slot else None
            )
            if not w._could_affect_view_cook(chunks, stage, registry.scene, view_chunk):
                w._recook_skip_next_delete = True

    def _run_pending_recook(self) -> None:
        """Run one debounced recook queued by ChunkGraphScene._on_chunk_deleted.

        Deleting a chunk fires CHUNK_DELETED once per param it owns (request_delete()
        cascades through them), each of which queues a recook -- without this guard that's
        one redundant re-execution of the whole upstream chain per param on the deleted
        chunk. Clear the flag before recooking (not after) so a genuinely new delete arriving
        while this one runs queues its own follow-up instead of being swallowed.
        """
        self._recook_pending = False
        force_full = getattr(self, "_recook_force_full", False)
        self._recook_force_full = False
        self._recook_skip_next_delete = False  # consume -- must not leak into a later delete
        # None = always recook (a relevant chunk was deleted); () = definitely irrelevant, skip.
        self._recook_active_view(None if force_full else ())

    def _graph_context(self) -> "Optional[core.ChunkBase]":
        graph_scene = self.scene()
        return graph_scene._built_context if isinstance(graph_scene, ChunkGraphScene) else None

    # ------------------------------------------------------------------
    # Frame

    def _frame_nodes(self):
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene) or not graph_scene._nodes:
            return
        nodes = [
            n for n in graph_scene._nodes.values()
            if n.chunk.has_state(ChunkFlags.State.SELECTED)
        ] or list(graph_scene._nodes.values())

        rect = QtCore.QRectF()
        for node in nodes:
            rect = rect.united(node.sceneBoundingRect())
        rect.adjust(-60, -60, 60, 60)
        self.fitInView(rect, QtCore.Qt.KeepAspectRatio)
        # Framing the whole graph is exactly the case that zooms far enough out to matter.
        self._apply_lod()

    # ------------------------------------------------------------------
    # Auto-layout

    def _layout_selected_or_all(self) -> None:
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene) or not graph_scene._nodes:
            return
        selected_paths = [
            path for path, node in graph_scene._nodes.items()
            if node.chunk.has_state(ChunkFlags.State.SELECTED)
        ]
        graph_scene.layout_nodes(selected_paths or None)

    def _open_tab_menu(self):
        """Open the chunk creation menu at the cursor, like Houdini's Tab menu."""
        cursor_pos = self.mapFromGlobal(QtGui.QCursor.pos())
        scene_pos = self.mapToScene(cursor_pos)
        global_pos = QtGui.QCursor.pos()
        self._create_chunk_at(scene_pos, global_pos)

    def _create_chunk_at(self, scene_pos: QtCore.QPointF, global_pos: QtCore.QPoint):
        """Show the chunk menu and create the selected chunk at scene_pos.

        Shared by both Tab key and right-click on empty graph area.
        """
        entry = FilterableChunkMenu().select_module(global_pos)
        if not entry:
            return

        chunks = entry.create()

        scene = _chunk.get_scene()
        if not scene:
            return

        context = self._graph_context()
        view_exists = self._context_has_view(scene, context)
        chunk_usd._create_node_pos = (scene_pos.x(), -scene_pos.y())
        chunk_usd._pin_new_chunks = False
        nested, prev_ctx = self._push_nest_context(scene, context)
        try:
            for c in chunks:
                if view_exists and not chunk_usd.is_view_chunk(c) and not chunk_usd.is_group_chunk(c):
                    c.set_blocked(True)
                scene.add_chunk(c)
        finally:
            if nested:
                scene._current_context = prev_ctx
            chunk_usd._create_node_pos = None
            chunk_usd._pin_new_chunks = None

        menu_view = next((c for c in chunks if chunk_usd.is_view_chunk(c)), None)
        if menu_view is not None:
            self._activate_menu_view(menu_view, context, scene.get_stage())
            return

        QtCore.QTimer.singleShot(60, self, lambda ctx=context: self._ensure_default_view(ctx))

    # ------------------------------------------------------------------
    # Drag-and-drop file import

    def _droppable_paths(self, event) -> "list[str]":
        return droppable_paths(event)

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent):
        if self._droppable_paths(event):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QtGui.QDragMoveEvent):
        if self._droppable_paths(event):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event: QtGui.QDropEvent):
        paths = self._droppable_paths(event)
        if not paths:
            super().dropEvent(event)
            return
        event.acceptProposedAction()
        scene_pos = self.mapToScene(event.pos())
        global_pos = self.viewport().mapToGlobal(event.pos())
        offset = QtCore.QPointF(0, 0)
        for path in paths:
            self._handle_dropped_file(path, scene_pos + offset, global_pos)
            offset += QtCore.QPointF(40, 40)  # stagger multi-file drops so nodes don't stack exactly

    def _handle_dropped_file(
        self, path: str, scene_pos: QtCore.QPointF, global_pos: QtCore.QPoint
    ) -> None:
        kind = _detect_dropped_usd_kind(path)
        if kind is None:
            return
        # "Import as Chunks" only makes sense for a file that actually carries chunk prims
        # (a chunkgroup export) -- offering it for a plain scene would be a dead choice.
        # Sublayer and Reference apply either way, so the popup always shows at least those
        # two: a plain scene file no longer silently defaults to Sublayer the way it used to
        # before Reference was an option here, since that default would make the new choice
        # unreachable for the single most common drop (a plain USD asset with no chunk prims).
        chosen = self._prompt_import_kind(global_pos, offer_chunks=(kind != "scene"))
        if chosen == "chunks":
            self._import_as_chunks(path, scene_pos)
        elif chosen == "scene":
            self._import_as_sublayer(path, scene_pos)
        elif chosen == "reference":
            self._import_as_reference(path, scene_pos)

    def _prompt_import_kind(self, global_pos: QtCore.QPoint, offer_chunks: bool) -> "str | None":
        menu = QtWidgets.QMenu()
        as_chunks = menu.addAction("Import as Chunks") if offer_chunks else None
        as_sublayer = menu.addAction("Import as Sublayer")
        as_reference = menu.addAction("Import as Reference")
        res = self._exec_menu(menu, global_pos)
        if as_chunks is not None and res is as_chunks:
            return "chunks"
        if res is as_sublayer:
            return "scene"
        if res is as_reference:
            return "reference"
        return None

    def _import_as_chunks(self, path: str, scene_pos: QtCore.QPointF) -> None:
        """Drop-a-chunkgroup-file: read its chunk prims into clipboard-shaped data and
        materialize them at the drop point, same as pasting a copied selection."""
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene) or not graph_scene._scene_sender:
            return
        layer = Sdf.Layer.FindOrOpen(path)
        if layer is None:
            return
        clip_nodes, clip_edges = _snapshot_layer_chunks(layer)
        if not clip_nodes:
            return
        # Hold off build()'s generic auto-view for the duration of this import -- it's on a
        # shorter (80ms) timer than our own wiring-then-view sequence below and would
        # otherwise very likely win the race and latch onto the wrong tail first.
        self._suppress_default_view = True
        try:
            pasted = self._materialize_at(clip_nodes, clip_edges, scene_pos)
        except Exception:
            self._suppress_default_view = False
            raise
        if not pasted:
            self._suppress_default_view = False
            return
        # Wait past _materialize_at's own deferred (80ms) wiring pass so the imported
        # chain's exec connections actually exist on the stage before tails are computed.
        QtCore.QTimer.singleShot(150, self, lambda chunks=pasted: self._ensure_view_for_imported(chunks))

    def _ensure_view_for_imported(self, imported_chunks: "list[core.ChunkBase]") -> None:
        """Point a fresh default view at the tail of JUST the imported chunks -- not left to
        build()'s generic _ensure_default_view, which picks tails[0] among EVERY disconnected
        exec tail in the whole context, in whatever order scene.get_chunks() happens to yield.
        A dropped-in chunkgroup usually lands as its own chain alongside whatever was already
        in the tab (e.g. a pre-existing Edit Target chunk with nothing wired after it -- also a
        valid 'tail'), so without scoping to just the newly imported nodes, the generic
        auto-view could easily latch onto that unrelated pre-existing node instead of the
        workflow the user just dropped in (reported live: view landed on Edit Target instead of
        the dropped file's own terminal RBD Solver node). No-ops if the context already has a
        view -- matches _ensure_default_view's own rule of never stealing an existing view.

        Always releases the _suppress_default_view hold _import_as_chunks took, whichever
        path out of here is taken -- otherwise a failure partway through would leave the
        generic auto-view permanently disabled for this view for the rest of the session."""
        try:
            graph_scene = self.scene()
            if not isinstance(graph_scene, ChunkGraphScene) or not graph_scene._scene_sender:
                return
            scene = _chunk.get_scene()
            if not scene:
                return
            stage = scene.get_stage()
            if not stage:
                return
            context = self._graph_context()
            if self._context_has_view(scene, context):
                return
            consumed: set = set()
            for prim in stage.Traverse():
                exec_in = prim.GetAttribute(_EXEC_INPUT)
                if exec_in.IsValid():
                    for conn in exec_in.GetConnections():
                        consumed.add(str(conn.GetPrimPath()))
            tails = [
                c for c in imported_chunks
                if not chunk_usd.is_view_chunk(c) and chunk_usd.get_chunk_prim_path(c) not in consumed
            ]
            if not tails:
                return

            def _x(c: "core.ChunkBase") -> float:
                node = graph_scene._nodes.get(chunk_usd.get_chunk_prim_path(c))
                return node.pos().x() if node is not None else float("-inf")

            # Among several disconnected tails within the imported set itself (a branching
            # workflow), prefer the rightmost -- auto-layout arranges topological depth into
            # columns left-to-right, so "rightmost" is this app's own convention for "most
            # downstream", the closest cheap proxy for "the last node" without a full re-walk
            # of the exec graph's depth.
            target = max(tails, key=_x)
            self._create_view_node_for_prim(chunk_usd.get_chunk_prim_path(target))
        finally:
            self._suppress_default_view = False

    def _import_as_sublayer(self, path: str, scene_pos: QtCore.QPointF) -> None:
        """Drop-a-scene-file: create a Sublayer chunk node at the drop point with its path
        pre-filled. Values are set AFTER add_chunk (unlike the paste path) because Sublayer's
        own callback (scripts/sublayer.py) only does real work -- authoring subLayerPaths onto
        the chunk's edit layer -- once the chunk has a live prim/edit_context, exactly matching
        how a hand-created Sublayer chunk picks up a path typed into its UI after creation.

        Deliberately left UNBLOCKED while applying values, unlike _materialize_at/
        _build_clip_node_from_prim's chunks: blocking suppresses ChunkNoticePropagator.
        data_changed/send_self entirely (chunk_notice.py checks is_blocked() up the whole
        owner chain), so a blocked chunk's ATTRIBUTE_CHANGED notice never fires at all --
        meaning Sublayer's own add_callback handler (which is what actually authors
        subLayerPaths) would simply never run, and the dropped path would sit in the field
        with no effect (reported live: "empty sublayer"). That blocking is right for a chunk
        with real, parent-resolution-dependent side effects (e.g. Create Layer -- see
        _materialize_at), but Sublayer's callback only ever writes into its OWN
        already-established edit layer, so there's no equivalent hazard here to guard
        against.

        BUT unlike a chunk whose content comes from the reserved execute() hook (which
        genuinely never runs at all while blocked, so a blocked chunk's layer stays empty
        regardless of mute state), Sublayer's content-bringing mechanism is this direct
        subLayerPaths write -- once authored it composes and stays composed; a later
        set_blocked(True) alone doesn't undo it. Hiding it while unwired (matching every
        other kind of freshly created chunk's "stays inert until wired" behavior) needs an
        actual mute, which only happens as a side effect of a real recook_for_view pass --
        so if a view already exists here, explicitly force one after the write (live-
        confirmed without this: a dropped, deliberately-unconnected Sublayer's content
        still rendered in the viewport)."""
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene) or not graph_scene._scene_sender:
            return
        scene = _chunk.get_scene()
        if not scene:
            return
        primary, chunks = self._instantiate_chunk_of_type("sublayer")
        if primary is None:
            return
        context = self._graph_context()
        view_exists = self._context_has_view(scene, context)
        chunk_usd._create_node_pos = (scene_pos.x(), -scene_pos.y())
        chunk_usd._pin_new_chunks = False
        nested, prev_ctx = self._push_nest_context(scene, context)
        try:
            for c in chunks:
                scene.add_chunk(c)
        finally:
            if nested:
                scene._current_context = prev_ctx
            chunk_usd._create_node_pos = None
            chunk_usd._pin_new_chunks = None

        list_group = primary.get_param("imports", _chunk.is_inst(_chunk.ListGroup))
        if list_group is not None:
            list_group.size = 1
            if row := list_group.get_indexed_param("sublayer_path", 0):
                row.value = path

        if view_exists:
            # recook_for_view (via _recook_active_view) sets c.set_blocked(c not in cook_set)
            # AND mutes every non-cook-set chunk's layer itself for every chunk in this
            # context -- no need to block this one manually first. No changed_chunks: this
            # node is deliberately NOT reachable from the view, so the changed_chunks fast
            # path (skip if the edit provably can't affect the view's cook set) would skip
            # exactly the recook that's needed to mute it.
            self._recook_active_view()

    def _import_as_reference(self, path: str, scene_pos: QtCore.QPointF) -> None:
        """Drop-a-file-as-Reference: create a Reference chunk node at the drop point with
        its file_path pre-filled. Unlike Sublayer, Reference's content-authoring work
        (AddReference) lives on the reserved execute(node) hook -- the normal reactive path
        every param-driven chunk (transform, scatter_points, ...) already uses -- rather
        than on an explicit add_callback, so setting file_path alone is enough to trigger
        it; no separate hand-off is needed the way Sublayer's own subLayerPaths write is.
        file_path's own file_changed callback derives primpath_location (the insertion
        point) from the asset's file name at the same time, so nothing else needs filling
        in for the common case.

        Still needs the same view-recook nudge Sublayer's import does: every chunk's edit
        layer is sublayered into the stage regardless of exec-wiring, and it's the active
        view's recook pass (recook_for_view) that mutes a chunk not reachable from it --
        without forcing that pass here, a dropped-in-but-unconnected Reference's content
        would render in the viewport anyway, same bug as the equivalent Sublayer case."""
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene) or not graph_scene._scene_sender:
            return
        scene = _chunk.get_scene()
        if not scene:
            return
        primary, chunks = self._instantiate_chunk_of_type("reference")
        if primary is None:
            return
        context = self._graph_context()
        view_exists = self._context_has_view(scene, context)
        chunk_usd._create_node_pos = (scene_pos.x(), -scene_pos.y())
        chunk_usd._pin_new_chunks = False
        nested, prev_ctx = self._push_nest_context(scene, context)
        try:
            for c in chunks:
                scene.add_chunk(c)
        finally:
            if nested:
                scene._current_context = prev_ctx
            chunk_usd._create_node_pos = None
            chunk_usd._pin_new_chunks = None

        if file_param := primary.get_param("file_path"):
            file_param.value = path

        if view_exists:
            self._recook_active_view()

    def contextMenuEvent(self, event: QtGui.QContextMenuEvent):
        scene_pos = self.mapToScene(event.pos())
        for item in self.scene().items(scene_pos):
            if isinstance(item, QtWidgets.QGraphicsProxyWidget):
                top_widget = item.widget()
                if top_widget:
                    proxy_local = item.mapFromScene(scene_pos).toPoint()
                    child = top_widget.childAt(proxy_local) or top_widget
                    w = child
                    while w is not None:
                        if isinstance(w, LabeledWidget):
                            # Send a fresh QContextMenuEvent through Qt's event
                            # system rather than calling the method directly —
                            # direct calls bypass popup state tracking and cause
                            # menu.exec_() to close immediately.
                            local_pos = w.mapFromGlobal(event.globalPos())
                            ctx = QtGui.QContextMenuEvent(
                                QtGui.QContextMenuEvent.Reason.Mouse,
                                local_pos,
                                event.globalPos(),
                            )
                            QtWidgets.QApplication.sendEvent(w, ctx)
                            event.accept()
                            return
                        if w is top_widget:
                            break
                        w = w.parent()
                event.accept()
                return
            if isinstance(item, ChunkNodeItem):
                self._show_chunk_menu(item, event.globalPos())
                event.accept()
                return

        # Empty canvas: offer Paste alongside the existing "New Chunk" fuzzy-search popup,
        # but only when there's actually something to paste -- otherwise this behaves exactly
        # as before (straight into the fuzzy search, no extra menu step).
        if _node_clipboard:
            quick_menu = QtWidgets.QMenu()
            paste_action = quick_menu.addAction("Paste")
            new_chunk_action = quick_menu.addAction("New Chunk...")
            res = self._exec_menu(quick_menu, event.globalPos())
            if res is None:
                return
            if res == paste_action:
                self._paste_clipboard_at(scene_pos)
                event.accept()
                return
            if res != new_chunk_action:
                return

        self._create_chunk_at(scene_pos, event.globalPos())
        event.accept()


