"""Node graph view for Chunk prims.

Reads layout from UsdUINodeGraphNodeAPI; writes position back on drag.
Selection syncs with ChunkWidget via STATE_CHANGED notice.
Edges follow inputs:chunk connections between prims.
"""

from __future__ import annotations

import logging
import os
import time
import weakref
from collections.abc import Iterable

import shiboken6
from pxr import Gf, Sdf, Usd, UsdUI
from PySide6 import QtCore, QtGui, QtWidgets

from usd_chunks import chunk as _chunk
from usd_chunks import chunk_core as core
from usd_chunks import chunk_naming
from usd_chunks import chunk_widget_api
from usd_chunks import chunk_registry
from usd_chunks import chunk_scene
from usd_chunks.graph import engine as chunk_graph
from usd_chunks import chunk_utils
from usd_chunks import chunk_stage
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_notice import ChunkNoticeAlias, NoticeConnection
from usd_chunks.frontends.qt import node_theme, window_state
from usd_chunks.frontends.qt.widgets import ChunkContextBreadcrumb, ContainerWidget, ChunkWidget, FilterableChunkMenu, LabeledWidget, apply_chunk_stylesheet, disconnect_when_destroyed, ViewSlotState, pre_delete_hooks, slot_for_key

logger = logging.getLogger(__name__)

# The clipboard model and its snapshot helpers live in the engine now; these aliases keep
# the rest of this file reading as it did, and keep the two frontends on one format.
_ClipboardNode = chunk_graph.ClipboardNode
_ClipboardEdge = chunk_graph.ClipboardEdge
_iter_prim_connections = chunk_graph.iter_prim_connections
_iter_group_children = chunk_graph.iter_group_children
_snapshot_chunk_node = chunk_graph.snapshot_chunk_node
_snapshot_list_sizes = chunk_graph.snapshot_list_sizes
_snapshot_param_values = chunk_graph.snapshot_param_values
_iter_copyable_params = chunk_graph.iter_copyable_params
_iter_chunk_param_paths = chunk_graph.iter_chunk_param_paths
_node_clipboard = chunk_graph.node_clipboard
_edge_clipboard = chunk_graph.edge_clipboard

_PORT_RADIUS = 5
_PORT_SNAP_PIXELS = 28  # on-screen radius within which a dragged wire jumps to a port
_PORT_HOVER_RADIUS = 9  # a port reacts to the mouse this far from its centre, past its dot
# A port's outline: invisible, and wide enough to reach _PORT_HOVER_RADIUS. Qt counts the
# outline in an item's size and hit area, so a port reacts past its dot with no Python in
# the draw path.
_PORT_REACH_PEN = QtGui.QPen(QtGui.QColor(0, 0, 0, 0), (_PORT_HOVER_RADIUS - _PORT_RADIUS) * 2)
_NODE_WIDTH = 250  # a node's width until someone drags its right edge
_NODE_MIN_WIDTH = 140
_NODE_MAX_WIDTH = 800
_RESIZE_GRIP_IN = 3  # how far inside the node's right edge a press starts a resize
_RESIZE_GRIP_OUT = 4  # and how far outside it
# On the edge, a press this close to a port (on screen) starts a wire, not a resize.
_PORT_PRESS_PIXELS = 14
_NODE_HEADER_H = 48
# Room under a node's body for its rounded bottom corners. The body is a square widget, so
# one that reached the bottom edge covered the corners: a group's grey Description box, or
# the frame round a selected chunk, showed square over the rounded outline.
_NODE_BODY_BOTTOM = int(node_theme.NODE_RADIUS)

# A node stops drawing its embedded ChunkWidget -- showing only the box and header that
# ChunkNodeItem paints itself -- when either of these says so. The node keeps its size
# either way, so layout, edges and framing are unaffected.
#
# Below this scale a body's text is hard to read, so it is replaced by the cheap flat form.
# There is deliberately no cap on the number drawn above it.
#
# Culling by viewport position as well was tried and reverted once. A body reports no height
# until it has been through one visible layout cycle, and a node's height is what its ports,
# edges, framing and auto-layout all measure from. Per-node culling hides bodies constantly
# and early -- proxies are built in deferred batches while the view is still moving -- and a
# body hidden before it was ever laid out freezes the node at its bare header height.
# sizeHint(), adjustSize() and an explicit layout activate() all return 0 for such a widget,
# so it cannot be measured ahead of time. It is back now that the "has been laid out" gate
# exists: set_body_visible declines every hide until ChunkGraphScene.heights_settled, and
# _on_heights_settled re-runs LOD, so an offscreen body is only flattened once its height
# is real. See _LOD_VIEWPORT_MARGIN.
_LOD_BODY_MIN_SCALE = 0.1

# Nodes outside the visible area also get the flat form. The visible rect is grown by this
# fraction of the viewport's size on every side, so a node panned toward the edge is
# already live by the time it arrives rather than popping in (a re-shown proxy can flash
# blank for a frame while it re-lays out).
_LOD_VIEWPORT_MARGIN = 0.5
# Throttle for re-running LOD while panning/resizing: often enough to keep up with a drag,
# rarely enough that a pan isn't a per-node walk per mouse move.
_LOD_PAN_THROTTLE_MS = 50

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

# Proxy widgets wait for the host to finish whatever it is busy with -- see
# ChunkGraphScene._create_proxies_batch. How often to look again, and how long to keep
# waiting before building anyway: a host may hold its busy cursor across a long, legitimate
# operation, and a graph that never appears is worse than one that appears slowly.
_PROXY_BUSY_RETRY_MS = 50
_PROXY_BUSY_WAIT_MAX_S = 15.0

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


_COL_NODE_BG = node_theme.NODE_BG
_COL_NODE_HEADER = node_theme.NODE_HEADER_BG
_COL_NODE_BORDER = node_theme.NODE_BORDER
_COL_NODE_SELECTED = node_theme.NODE_SELECTED
_COL_GROUP_HEADER = QtGui.QColor("#1a1a1a")   # near-black -- group/context nodes
_COL_LOCKED_HEADER = QtGui.QColor("#3a3020")  # dark amber -- locked (external-layer) nodes
_COL_LOCKED_BG = QtGui.QColor("#2e2e28")
_COL_CANVAS_BG = QtGui.QColor("#17181b")  # ChunkGraphView's background brush
_DIM_ALPHA = 0.35  # the opacity disabled nodes are drawn at when their body is showing
_DIM_CACHE: dict[int, QtGui.QColor] = {}


_HEADER_FONTS: dict[tuple[int, bool], QtGui.QFont] = {}


def _header_font(size: int, bold: bool = False) -> QtGui.QFont:
    """The application's font at this point size, made once: paint runs per node per frame."""
    key = (size, bold)
    font = _HEADER_FONTS.get(key)
    if font is None:
        font = QtGui.QFont(QtWidgets.QApplication.font())
        font.setPointSize(size)
        font.setBold(bold)
        _HEADER_FONTS[key] = font
    return font


def _icon_dpr(painter: QtGui.QPainter) -> float:
    """The resolution to draw a header icon at, so it stays sharp when zoomed in.

    Rounded up to a whole step, so zooming reuses a handful of cached pixmaps rather than
    rendering one per zoom level.
    """
    device = painter.device()
    screen = device.devicePixelRatioF() if device is not None else 1.0
    zoom = abs(painter.worldTransform().m11()) or 1.0
    return float(min(max(1, int(screen * zoom + 0.99)), 4))


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
_COL_VIEW_HEADER = QtGui.QColor("#c87830")   # pale orange -- the ACTIVE view (driving execution)
_COL_VIEW_INACTIVE = QtGui.QColor("#4a453e")  # dim grey-brown -- an inactive view (not driving)
_COL_VIEW_ACTIVE = QtGui.QColor("#5fd08a")   # green -- active view border accent
_COL_OUTPUT_HEADER = QtGui.QColor("#3a6f8f")  # steel blue -- the Output: what the graph delivers
_COL_STALE = QtGui.QColor("#e0902a")   # amber -- chunk's result no longer reflects its inputs
_COL_FAILED_HEADER = QtGui.QColor("#7a2626")  # dark red -- the chunk's last cook failed or was cancelled
_COL_FAILED_BORDER = QtGui.QColor("#d04848")
_COL_PORT_EXEC = node_theme.PORT_EXEC
_COL_PORT_FLOAT = node_theme.PORT_FLOAT
_COL_PORT_INT = node_theme.PORT_INT
_COL_PORT_BOOL = node_theme.PORT_BOOL
_COL_PORT_STRING = node_theme.PORT_STRING
_COL_PORT_VECTOR = node_theme.PORT_VECTOR
_COL_TEXT_PRIMARY = node_theme.TEXT_PRIMARY
_COL_TEXT_SECONDARY = node_theme.TEXT_SECONDARY
_COL_TEXT_LOCKED = QtGui.QColor("#888877")

_CHUNKS_ROOT = chunk_naming.CHUNKS_ROOT
_CHUNK_PRIM_TYPENAME = chunk_naming.CHUNK_PRIM_TYPENAME
_EXEC_INPUT = chunk_naming.EXEC_INPUT
_EXEC_OUTPUT = chunk_naming.EXEC_OUTPUT
_UNBUILT = object()  # sentinel for ChunkGraphScene._built_context
# Child predicate that keeps disabled chunks: disabling sets the chunk prim inactive
# (prim.SetActive(False)), and the default GetChildren() skips inactive prims -- which would
# drop the disabled node out of the graph. Include inactive-but-defined prims so a disabled
# node stays put (greyed out) instead of vanishing.
_INCLUDE_INACTIVE = Usd.PrimIsDefined & ~Usd.PrimIsAbstract

class _BodyLayoutWatcher(QtCore.QObject):
    """Calls a function whenever a node body's layout is asked to redo itself.

    Qt sends the body a LayoutRequest when anything inside it is added, removed, shown or
    hidden, at any depth. A body that grows also resizes its proxy, but one that should
    shrink does not: a top-level widget never shrinks on its own. Without this, a chunk
    that swaps params inside a group (Light changing type) kept its old height and ports.
    """

    def __init__(self, body: QtWidgets.QWidget, on_change) -> None:
        super().__init__(body)
        self._on_change = on_change
        body.installEventFilter(self)

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:  # noqa: N802
        if event.type() == QtCore.QEvent.Type.LayoutRequest:
            self._on_change()
        return False


# Hidden top-level sink widget: child QWidgets parented here avoid a native OS
# window (and the Windows flash that comes with it) while staying off the viewport.
_proxy_sink: QtWidgets.QWidget | None = None


def _get_proxy_sink() -> QtWidgets.QWidget:
    global _proxy_sink
    # Rebuilt if Qt deleted it (a host tearing down its windows): a dead sink raised on
    # every node body built afterwards.
    if _proxy_sink is None or not shiboken6.isValid(_proxy_sink):
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

#: Inset of the floating context path from the graph viewport's top-left corner.
_BREADCRUMB_MARGIN = 6
#: Where the Auto-connect checkbox remembers its state between sessions.
_AUTO_CONNECT_KEY = "graph/autoConnect"
#: Space between the Auto-connect checkbox and the Output badge beside it.
_AUTO_CONNECT_GAP = 8
#: Every live graph view, so ticking Auto-connect in one ticks it in all.
_graph_views: weakref.WeakSet = weakref.WeakSet()

# The port vocabulary lives in chunk_graph so every frontend names types the same way.
# Aliased rather than re-exported by hand: ~40 references read _PORT_* already, and a
# second definition here is the thing that lets Qt and a non-Qt view disagree.
_PORT_EXEC = chunk_graph.PORT_EXEC
_PORT_FLOAT = chunk_graph.PORT_FLOAT
_PORT_INT = chunk_graph.PORT_INT
_PORT_BOOL = chunk_graph.PORT_BOOL
_PORT_STRING = chunk_graph.PORT_STRING
_PORT_COLOR = chunk_graph.PORT_COLOR
_PORT_VECTOR = chunk_graph.PORT_VECTOR

_COL_PORT_COLOR = node_theme.PORT_COLOR

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

    def __init__(
        self, attr_path: str, port_type: str, parent: ChunkNodeItem, show_label: bool = False, param=None
    ):
        r = _PORT_RADIUS
        super().__init__(-r, -r, r * 2, r * 2, parent)
        self.attr_path = attr_path
        # The param this port stands for; a list-group row's port names the same attribute as
        # its siblings, so this is what tells them apart.
        self.param = param
        self.port_type = port_type
        self.is_input = ".inputs:" in attr_path
        self.setBrush(_PORT_TYPE_COLOR[port_type])
        self.setPen(_PORT_REACH_PEN)
        self.setZValue(1)
        # A valid drop target while a wire is dragged (a white ring), and lit up: hovered,
        # or the port a dragged wire has jumped to.
        self._highlighted = False
        self._hovered = False
        self._snapped = False
        self.setAcceptHoverEvents(True)
        # Its own cursor, so the node edge's resize cursor never shows over a port.
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)

        if show_label:
            label_text = attr_path.rsplit(":", 1)[-1]
            text = QtWidgets.QGraphicsSimpleTextItem(label_text, self)
            font = QtGui.QFont("Arial", 7)
            text.setFont(font)
            text.setBrush(QtGui.QColor("#cccccc"))
            tw = text.boundingRect().width()
            th = text.boundingRect().height()
            # Output ports sit on the right edge -- label goes to the left of the dot
            text.setPos(-r - 3 - tw, -th / 2)

    def set_highlighted(self, on: bool) -> None:
        """Mark the port as a place a dragged wire can be dropped."""
        if on != self._highlighted:
            self._highlighted = on
            self._refresh_look()

    def set_snapped(self, on: bool) -> None:
        """Light the port up as the one a dragged wire has jumped to."""
        if on != self._snapped:
            self._snapped = on
            self._refresh_look()

    def _refresh_look(self) -> None:
        """Show the ring and glow for the current state.

        Both are child items made on first need, so Qt draws every port natively: a port is
        drawn far more often than it changes, and per-port Python in the draw path (paint,
        boundingRect, shape) made the graph about three times slower to redraw.
        """
        lit = self._hovered or self._snapped
        self._overlay("_ring", lit or self._highlighted, self._make_ring)
        self._overlay("_glow", lit, self._make_glow)

    def _overlay(self, name: str, show: bool, make) -> None:
        item = getattr(self, name, None)
        if item is None:
            if not show:
                return
            item = make()
            item.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
            item.setAcceptHoverEvents(False)
            setattr(self, name, item)
        item.setVisible(show)

    def _make_ring(self) -> QtWidgets.QGraphicsEllipseItem:
        r = _PORT_RADIUS
        ring = QtWidgets.QGraphicsEllipseItem(-r, -r, r * 2, r * 2, self)
        ring.setPen(QtGui.QPen(QtGui.QColor("white"), 1.5))
        ring.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        return ring

    def _make_glow(self) -> QtWidgets.QGraphicsEllipseItem:
        # A soft glow in the port's own colour: "start or end a wire here".
        r = _PORT_RADIUS + 4
        glow = QtWidgets.QGraphicsEllipseItem(-r, -r, r * 2, r * 2, self)
        color = QtGui.QColor(self.brush().color())
        color.setAlpha(90)
        glow.setBrush(color)
        glow.setPen(QtGui.QPen(QtCore.Qt.PenStyle.NoPen))
        glow.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemStacksBehindParent)
        return glow

    def hoverEnterEvent(self, event) -> None:  # noqa: N802
        self._hovered = True
        self._refresh_look()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        self._refresh_look()
        super().hoverLeaveEvent(event)



# Port typing and param traversal are pure functions over Sdf types and the param
# classes -- no Qt in any of them -- so they live in the engine and the view borrows them.
_sdf_to_port_type = chunk_graph.sdf_to_port_type
_iter_connectable_params = chunk_graph.iter_connectable_params
_iter_all_vectors = chunk_graph.iter_all_vectors
_param_port_type = chunk_graph.param_port_type


# ---------------------------------------------------------------------------
# Copy / Cut / Paste / Duplicate clipboard
#
# In-process only (a plain Python object, not the OS clipboard/QMimeData) -- same-session
# paste is the only use case this needs to serve. Module-level so it's shared across every
# ChunkGraphView/tab/chunkgroup in the process, matching how a real clipboard behaves.





# The clipboard lives in chunk_graph (see the aliases at the top of this file). Never
# rebind these two names here: that would silently give Qt a clipboard of its own, and a
# copy in one frontend would be invisible to the others despite the shared format.




def _prim_graph_pos(prim: Usd.Prim | None) -> QtCore.QPointF:
    """A prim's authored node-graph position as a QPointF.

    The Y-up-to-Y-down flip is chunk_graph.graph_position's now -- both frontends draw on
    a Y-down canvas, so the convention belongs in the engine rather than in each of them.
    """
    return QtCore.QPointF(*chunk_graph.graph_position(prim))






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
    from.
    """
    is_exec = chunk_stage.is_exec_input_name(dst_attr_suffix)
    dst_attr = stage.GetAttributeAtPath(Sdf.Path(f"{dst_prim_path}.{dst_attr_suffix}"))
    if not (dst_attr and dst_attr.IsValid()):
        prim = stage.GetPrimAtPath(Sdf.Path(dst_prim_path))
        if not prim.IsValid():
            return
        if is_exec:
            sdf_type = Sdf.ValueTypeNames.Token
        else:
            # Type the new attribute from the destination PARAM, not from the fact that a
            # wire is arriving at it. Creating every non-exec attribute as String would give
            # a never-authored numeric param (a Transform's tx, say) an inputs:tx of type
            # String -- and an attribute's type is fixed once created, so every later numeric
            # write would raise for the life of the scene.
            param_path = dst_attr_suffix
            if param_path.startswith(chunk_stage._INPUTS_PREFIX):
                param_path = param_path[len(chunk_stage._INPUTS_PREFIX):]
            param = dst_chunk.get_param(param_path.replace(":", ".")) if dst_chunk else None
            # String remains the fallback for a param we cannot resolve or map: it is what
            # this always did, and it is the one type that never loses information.
            sdf_type = chunk_stage.sdf_type_for_param(param) or Sdf.ValueTypeNames.String
        dst_attr = prim.CreateAttribute(dst_attr_suffix, sdf_type, custom=False)
        if not dst_attr.IsValid():
            return
    if is_exec:
        src_conn_path = Sdf.Path(f"{src_prim_path}.{src_attr_suffix}")
    else:
        # src_attr_suffix is "outputs:param" (ChunkPortItem-suffix form) -- the raw
        # connection target is the source's own inputs:param, per _author_connection.
        src_conn_path = Sdf.Path(f"{src_prim_path}.inputs:{src_attr_suffix[len('outputs:'):]}")
    # A malformed suffix makes Sdf.Path() an empty path, which stringifies to <> and, if
    # handed to SetConnections, raises "Cannot map <> ... via stage's EditTarget" -- drop
    # the wire instead of crashing the whole paste.
    if src_conn_path.isEmpty:
        return
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














def _paste_guard_sets(chunks) -> tuple[list, list]:
    """(guarded, self_building) for chunks about to be pasted.

    guarded: every non-view chunk. Setting a copied value runs a chunk's module, and a
    paste sets them before the chunk is in the scene. That includes chunks that own
    children, even though they must not be *blocked*: unguarded, a chunk that builds its
    own children (AMD MaterialX Library) would build them from outside the scene -- named
    against nothing, with layers the stage never took -- and again once it was added,
    doubling the children and leaving orphan layers that make the next recook raise.

    self_building: the ones among them that make their own children. Nothing else cooks
    them -- recook_for_view never executes a group -- so each gets its one execute once it
    is in place.
    """
    guarded = [c for c in chunks if not chunk_stage.is_view_chunk(c)]
    self_building = [c for c in guarded
                     if chunk_stage.is_group_chunk(c) and not chunk_stage.is_chunkgroup(c)]
    return guarded, self_building


# ---------------------------------------------------------------------------
# Drag-and-drop file import
#
# Dropping a saved-chunkgroup .usda (chunk_qt.save_chunkgroup_dialog's output) reuses the
# copy/paste clipboard machinery above via _snapshot_layer_chunks below, which builds the same
# _ClipboardNode/_ClipboardEdge shape Ctrl+C does -- just read from a dropped file's own
# throwaway Usd.Stage instead of the live scene. Dropping a plain scene file instead spawns a
# Sublayer chunk node with the path pre-filled (see ChunkGraphView._import_as_sublayer).

#: A dropped file with this extension becomes a script_runner node running it.
_SCRIPT_EXT = ".py"


def droppable_paths(event) -> list[str]:
    """The USD and Python file paths a drag carries, if any.

    Module-level because the tab panel needs the same answer before a graph view exists:
    with no context there is no ChunkGraphView to accept the drop at all, so the panel
    takes it, creates the context, and hands the files on.
    """
    if not event.mimeData().hasUrls():
        return []
    exts = (".usd", ".usda", ".usdc", ".usdz", _SCRIPT_EXT)
    return [
        url.toLocalFile() for url in event.mimeData().urls()
        if url.isLocalFile() and os.path.splitext(url.toLocalFile())[1].lower() in exts
    ]


def _detect_dropped_usd_kind(path: str) -> str | None:
    """Classification of a dropped file for ChunkGraphView.dropEvent: 'chunks' if it has any
    Chunk-typed prim ANYWHERE in it, 'scene' if it's a USD file with none, None if it isn't a
    recognized USD file at all. There's no fixed root to shortcut the check against -- a
    chunk's root is its tab/context's own prim_path (chunk_core.add_context names a new tab
    "/" + the tab name, not a fixed "/chunks" convention -- /chunks is only chunk_stage.py's
    fallback for a context with no explicit prim_path), so a saved chunkgroup's chunks can
    live under any prim path depending on what its source tab was called. Sdf.Layer.FindOrOpen
    does a lightweight scene-description parse only (no composition, no schema/plugin loading,
    no geometry processing), and Layer.Traverse below walks specs, not geometry data, so this
    still stays proportionally cheap -- just not O(1) the way a fixed-root lookup would have
    been -- no matter how large an unrelated scene file is.
    """
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

    def _visit(spec_path: Sdf.Path) -> None:
        nonlocal found
        if found or not spec_path.IsPrimPath():
            return
        spec = layer.GetPrimAtPath(spec_path)
        if spec is not None and spec.typeName == _CHUNK_PRIM_TYPENAME:
            found = True

    layer.Traverse(Sdf.Path("/"), _visit)
    return "chunks" if found else "scene"


def _build_clip_node_from_prim(prim: Usd.Prim, rel_pos: tuple[float, float]) -> _ClipboardNode | None:
    """Build a _ClipboardNode straight from a Usd.Prim on a throwaway, unregistered stage (a
    dropped file opened via Usd.Stage.Open) -- the drag-and-drop counterpart of
    _snapshot_chunk_node, which instead reads a *live* chunk already registered in the real
    scene. Instantiates a throwaway chunk never added to any scene, purely to learn its
    module's canonical param order (same instantiate-without-adding pattern
    ChunkGraphView._instantiate_chunk_of_type already uses for paste), then restores its values
    via the same chunk_stage._load_wire_inputs the real stage-load path uses -- so list groups,
    dotted param paths and expression strings all resolve exactly the same way, without
    touching the ChunkRegistry/ChunkLayerManager singletons or the real stage at all.

    Blocked BEFORE _load_wire_inputs restores any values: ChunkBase.get_stage() always
    returns the real, live global stage regardless of whether THIS instance was ever added
    to any scene, so a module with real side-effecting reactive code (e.g.
    chunks/usd/layer.py's execute() authoring a genuine anon Sdf.Layer the instant its
    layer_name value changes) runs for real even on a throwaway, never-added chunk -- live
    confirmed as an orphaned duplicate layer appearing after a drag-and-drop import.
    """
    module_name = prim.GetAttribute(chunk_stage._CHUNK_TYPE_ATTR).Get()
    if not module_name:
        return None
    registry = chunk_registry.ChunkRegistry()
    if not registry.initialized:
        return None
    mod = registry.get_chunk_module(str(module_name))
    if mod is None:
        return None
    chunks = mod.create() if mod.loaded else mod.load()
    primary = next((c for c in chunks if not chunk_stage.is_view_chunk(c)), None)
    if primary is None:
        return None
    primary.set_blocked(True)
    chunk_stage._load_wire_inputs(prim, primary)
    clip_node = _ClipboardNode(str(module_name), _snapshot_param_values(primary), rel_pos,
                               name=prim.GetName())
    # Children only for a chunkgroup -- see chunk_graph.snapshot_chunk_node.
    if not chunk_stage.is_chunkgroup(primary):
        return clip_node

    child_prims = [
        c for c in prim.GetFilteredChildren(_INCLUDE_INACTIVE)
        if c.GetAttribute(chunk_stage._CHUNK_TYPE_ATTR).Get()
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
            child, (pos.x() - min_x, pos.y() - min_y)
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


def _snapshot_layer_chunks(layer: Sdf.Layer) -> tuple[list[_ClipboardNode], list[_ClipboardEdge]]:
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
    would import them twice.
    """
    stage = Usd.Stage.Open(layer)
    if not stage:
        return [], []
    candidates: dict[str, Usd.Prim] = {}
    # _INCLUDE_INACTIVE, not the bare default predicate -- Traverse()'s default excludes
    # inactive prims, and a disabled chunk is authored as prim.SetActive(False) (see this
    # file's other _INCLUDE_INACTIVE uses), so a disabled chunk in the dropped file would
    # otherwise be silently dropped from the import instead of landing greyed-out/disabled
    # like everywhere else in this app.
    for prim in stage.Traverse(_INCLUDE_INACTIVE):
        if prim.GetTypeName() != _CHUNK_PRIM_TYPENAME:
            continue
        if prim.GetAttribute(chunk_stage._CHUNK_TYPE_ATTR).Get():
            candidates[str(prim.GetPath())] = prim

    # A saved file's outermost chunkgroup -- the one sitting directly under the /chunks
    # scope -- is the container the graph lived in, not a node someone dropped in. Left in
    # candidates it becomes the file's ONLY top-level chunk, and everything actually in
    # the file reads as its contents: dropping a two-chunk file imported one node.
    # Dropping it from candidates lets its children be top-level, which is what they were.
    # A group nested any deeper is real content and stays.
    scope = Sdf.Path(chunk_stage._CHUNKS_ROOT)
    for path in [
        path for path, prim in candidates.items()
        if prim.GetParent() and prim.GetParent().GetPath() == scope
        and prim.GetAttribute(chunk_stage._CHUNK_TYPE_ATTR).Get() == "chunkgroup"
    ]:
        del candidates[path]

    def _is_top_level(prim: Usd.Prim) -> bool:
        p = prim.GetParent()
        while p and not p.IsPseudoRoot():
            if str(p.GetPath()) in candidates:
                return False
            p = p.GetParent()
        return True

    # The file's own Output is the end of the graph it was saved from, not content: the
    # graph it is dropped into has an Output already. A nested group keeps its own, since
    # that is part of what the group delivers.
    top_prims = [
        prim for prim in candidates.values()
        if _is_top_level(prim) and prim.GetAttribute(chunk_stage._CHUNK_TYPE_ATTR).Get() != "output"
    ]
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
            prim, (pos.x() - min_x, pos.y() - min_y)
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


def _saved_node_width(prim: Usd.Prim) -> float:
    """The width a node was resized to, from its UsdUI size, or the default width.

    Args:
        prim: The chunk prim the node shows.

    Returns:
        The width, within the allowed range.
    """
    if prim and prim.IsValid():
        attr = UsdUI.NodeGraphNodeAPI(prim).GetSizeAttr()
        if attr and attr.HasAuthoredValue():
            size = attr.Get()
            if size is not None and size[0] > 0:
                return float(min(max(size[0], _NODE_MIN_WIDTH), _NODE_MAX_WIDTH))
    return float(_NODE_WIDTH)


def _drag_wire_path(start: QtCore.QPointF, end: QtCore.QPointF, from_input: bool) -> QtGui.QPainterPath:
    """The curve of a wire being dragged out of a port.

    Args:
        start: The port it is dragged from.
        end: Where its loose end is.
        from_input: Whether that port is an input: the wire then leaves it to the left, as
            a wire into an input arrives, instead of to the right.
    """
    dx = abs(end.x() - start.x()) * 0.5
    if from_input:
        dx = -dx
    path = QtGui.QPainterPath(start)
    path.cubicTo(start.x() + dx, start.y(), end.x() - dx, end.y(), end.x(), end.y())
    return path


class _NodeResizeHandle(QtWidgets.QGraphicsItem):
    """A node's right edge: drag it to change the node's width, double-click to reset it.

    Sits above the body (so the edge of a field does not take the press) and below the
    ports (so a port on the edge still starts a wire).
    """

    def __init__(self, node: ChunkNodeItem) -> None:
        super().__init__(node)
        self._node = node
        self._press_x: float | None = None
        self._start_width = 0.0
        self._lit: ChunkPortItem | None = None  # the port a press here would start a wire from
        self.setZValue(0.5)
        self.setCursor(QtCore.Qt.CursorShape.SizeHorCursor)
        self.setAcceptHoverEvents(True)
        self.setPos(node._width, 0)

    def hoverMoveEvent(self, event) -> None:  # noqa: N802
        # Near a port a press starts a wire (see ChunkGraphView.mousePressEvent): say so,
        # with the cursor and the port's glow.
        view = event.widget().parent() if event.widget() is not None else None
        near = None
        if isinstance(view, QtWidgets.QGraphicsView):
            near = self._node.port_near(event.scenePos(), _PORT_PRESS_PIXELS / (view.transform().m11() or 1.0))
        self._light(near)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor if near is not None
                       else QtCore.Qt.CursorShape.SizeHorCursor)
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event) -> None:  # noqa: N802
        self._light(None)
        super().hoverLeaveEvent(event)

    def _light(self, port: ChunkPortItem | None) -> None:
        if port is self._lit:
            return
        for p, on in ((self._lit, False), (port, True)):
            if p is not None:
                p._hovered = on
                p._refresh_look()
        self._lit = port

    def boundingRect(self) -> QtCore.QRectF:  # noqa: N802
        return QtCore.QRectF(-_RESIZE_GRIP_IN, 0,
                             _RESIZE_GRIP_IN + _RESIZE_GRIP_OUT, self._node._height)

    def paint(self, painter, option, widget=None) -> None:
        pass

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            event.ignore()
            return
        self._press_x = event.scenePos().x()
        self._start_width = self._node._width
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._press_x is None:
            return
        self._node.set_width(self._start_width + event.scenePos().x() - self._press_x)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._press_x is None:
            return
        self._press_x = None
        if self._node._width != self._start_width:
            self._node._write_size_to_usd()
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self._node.reset_width()
        event.accept()


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
        self._lofted = chunk_stage.is_chunk_lofted(chunk)
        # Navigable subnet, not merely "owns children". A chunk-in-chunk such as
        # AMDMaterialXLibrary also carries GroupFlags.CONTEXT, but its children are an
        # implementation detail: it must not offer a dive, nor the group header and the
        # `▶` marker that advertise one. Containment still keys on
        # chunk_stage.is_group_chunk, so its children stay hidden and keep cooking.
        # Subnet, not chunkgroup: a chunk hosting a referenced network draws and dives
        # like a group too.
        self._is_chunkgroup = chunk_stage.is_subnet(chunk)
        self._ports: list[ChunkPortItem] = []
        self._typed_ports: list[ChunkPortItem] = []
        self._selected = chunk.has_state(ChunkFlags.State.SELECTED)
        self._stale = chunk.has_state(ChunkFlags.State.STALE)
        self._failed = chunk.has_state(ChunkFlags.State.FAILED)
        # The collapsed state _set_collapsed last applied; None until something has. Kept so
        # a state change that is not a collapse (selecting, going stale) does not rebuild
        # ports -- see _set_collapsed.
        self._collapsed: bool | None = None
        self._update_stale_tooltip()
        _chunk_type = prim.GetAttribute("info:chunkType").Get()
        self._is_view = _chunk_type == "view"
        self._is_output = _chunk_type == "output"
        self._is_dot = _chunk_type == "dot"
        self._is_merge = _chunk_type == "merge"
        # Dynamic exec inputs (config.dynamic_exec_inputs): the port count isn't fixed, it grows
        # one port per connected branch plus a trailing empty one (Merge, Switch, ...).
        # Distinct from _is_merge, which additionally means "no body at all" -- a Switch
        # keeps its body (the Index param) and still needs its proxy/widget built normally,
        # so the two can't be folded into one flag.
        self._dynamic_exec = chunk_stage.has_dynamic_exec_inputs_prim(prim)
        # Positional exec input slots (config.exec_inputs). >1 gives the node one port per
        # slot on a node that still has a body -- unlike a dynamic-exec node, which grows its
        # slots from what is wired in instead of declaring a fixed count.
        self._exec_slots = chunk_stage.exec_input_slots_for_prim(prim)
        # Every exec input port, in slot order. _exec_in_port stays the primary (slot 0),
        # which is what splice-on-drop and wire-drop targeting mean by "this node's input";
        # this list is what port rebuilds have to restore, and losing it is how a second
        # slot's port silently disappears the first time the body relayouts.
        self._exec_in_ports: list[ChunkPortItem] = []
        self._chunk_type_label = str(_chunk_type or "")  # cached: paint must not read USD
        # The header icon ("" for none) and its colour, cached for the same reason. The
        # chunk decides which icon: see chunk_icons.
        self._icon, self._icon_color = node_theme.node_icon(chunk, prim)
        self._disabled = chunk.has_state(ChunkFlags.State.DISABLED)
        self._lod_simple = False  # see set_body_visible
        # Height settling -- see _note_height_sample and is_height_settled.
        self._last_height_sample = None
        self._drag_active = False
        self._drag_moved = False  # True once a drag actually moves -- plain clicks stay False
        self._drag_start = QtCore.QPointF()
        self._shake_samples: list[tuple[float, float]] = []  # (time, scene x) during a drag
        self._shake_fired = False
        self._last_collapse_toggle_time = 0.0  # see _COLLAPSE_TOGGLE_DEBOUNCE
        # Multi-select drag: other selected (unlocked) nodes moved alongside this one, and
        # whether a plain (no-drag) click on an already-multi-selected node should collapse
        # the selection down to just this node once the press/release completes without moving.
        self._drag_companions: list[ChunkNodeItem] = []
        self._drag_pending_collapse = False
        self._proxy: QtWidgets.QGraphicsProxyWidget | None = None
        self._port_rebuild_timer: QtCore.QTimer | None = None
        # The width the user dragged this node to, saved as its size in USD; the default
        # otherwise. Nodes without a body (dots, merges) keep the default.
        self._width = (float(_NODE_WIDTH) if self._is_dot or self._is_merge
                       else _saved_node_width(prim))
        self._resize_handle: _NodeResizeHandle | None = None
        self._height = _NODE_HEADER_H
        self._full_height = _NODE_HEADER_H
        self._outputs_start_y: float = 0.0  # >0 when typed output ports are present

        self.setFlag(QtWidgets.QGraphicsItem.ItemIsMovable, False)  # handled manually -- header only
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
        # A param's own HIDDEN state toggling (e.g. Layer's Index field, shown only
        # for Insert == "Index") needs the same rebuild PARAM_ADDED gets, or the port stays
        # on the node until something unrelated forces a fresh layout pass. Unlike a
        # group's own collapse animation (toggle_anim.finished, connected in create_proxy),
        # there's no Qt signal to hang this off at all -- a hide is a direct
        # set_state(HIDDEN), no animation involved -- so geometryChanged has nothing to
        # fire from until something else happens to trigger a relayout regardless. Listened
        # for at the chunk-notice level instead, mirroring _param_conn above.
        self._param_state_conns = self._build_param_state_conns()

        self._build_ports(prim)
        self._apply_usd_pos(prim)
        # Reflect disabled state on (re)build -- otherwise a rebuilt disabled node renders at
        # full opacity until the next state change (e.g. selecting it).
        self._apply_dim()

    def set_lofted(self, lofted: bool) -> None:
        """Update the cached loft badge after a menu toggle and repaint just this node --
        no full graph rebuild needed for a cosmetic change.
        """
        self._lofted = lofted
        self.update()

    def set_subnet(self, subnet: bool) -> None:
        """Update whether this node is a graph to dive into (chunk_stage.is_subnet), which
        the header double-click and the group styling key on.
        """
        if subnet != self._is_chunkgroup:
            self._is_chunkgroup = subnet
            self.update()

    def set_locked(self, locked: bool) -> None:
        """Update the cached locked state (edit-target changed, or this node is reused across a
        rebuild) and repaint just this node -- no full node reconstruction needed.
        """
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
            self._exec_in_ports = [self._exec_in_port]
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
            # attr_path, so an unconnected inputs:chunkN never has to be authored -- and then
            # serialized into every saved file forever -- just to give a wire somewhere to
            # land. Ports read top-to-bottom as strongest-to-weakest.
            slots = len(chunk_stage.exec_input_connections(prim)) + 1
            for i in range(slots):
                port = ChunkPortItem(
                    f"{prim.GetPath()}.{chunk_stage.exec_input_name(i)}", _PORT_EXEC, parent=self
                )
                port.setPos(0, _MERGE_PORT_TOP + i * _MERGE_PORT_SPACING)
                self._ports.append(port)
            # Everything that asks a node for "its" exec input (splice-on-drop, wire-drop
            # targeting) should get the primary one, which is slot 0 -- the strongest.
            self._exec_in_port = self._ports[0]
            self._exec_in_ports = list(self._ports)
            self._height = max(
                _NODE_HEADER_H, _MERGE_PORT_TOP * 2 + (slots - 1) * _MERGE_PORT_SPACING
            )
            self._full_height = self._height
            self._exec_out_port = ChunkPortItem(
                f"{prim.GetPath()}.outputs:chunk", _PORT_EXEC, parent=self
            )
            self._exec_out_port.setPos(self._width, _NODE_HEADER_H // 2)
            self._ports.append(self._exec_out_port)
            return

        if self._dynamic_exec:
            # Same growing-port idea as a merge (one per connected branch, plus one empty
            # trailing slot -- see the comment above), but this node keeps its body (a
            # Switch's Index param), so the ports stay confined to the header band like an
            # ordinary node's fixed exec slots below, rather than spanning the full height.
            slots = len(chunk_stage.exec_input_connections(prim)) + 1
            self._exec_in_ports = []
            for i in range(slots):
                port = ChunkPortItem(
                    f"{prim.GetPath()}.{chunk_stage.exec_input_name(i)}", _PORT_EXEC, parent=self
                )
                port.setPos(0, _NODE_HEADER_H * (i + 1) / (slots + 1))
                self._ports.append(port)
                self._exec_in_ports.append(port)
            self._exec_in_port = self._exec_in_ports[0]

            if prim.HasAttribute(chunk_naming.EXEC_OUTPUT):
                self._exec_out_port = ChunkPortItem(
                    f"{prim.GetPath()}.outputs:chunk", _PORT_EXEC, parent=self
                )
                self._exec_out_port.setPos(self._width, _NODE_HEADER_H // 2)
                self._ports.append(self._exec_out_port)
            else:
                self._exec_out_port = None
            return

        # Exec ports only -- lightweight, no widget creation.
        # Proxy (ChunkWidget) is created later via create_proxy() to allow batched deferred loading.
        # Spread across the header band rather than down the left edge: the slots stay
        # clear of the body below, so a node keeps its measured height and nothing has to
        # wait for the body to settle before the ports are placeable.
        self._exec_in_ports = []
        for i in range(self._exec_slots):
            port = ChunkPortItem(
                f"{prim.GetPath()}.{chunk_stage.exec_input_name(i)}", _PORT_EXEC, parent=self
            )
            port.setPos(0, _NODE_HEADER_H * (i + 1) / (self._exec_slots + 1))
            self._ports.append(port)
            self._exec_in_ports.append(port)
        self._exec_in_port = self._exec_in_ports[0]

        if prim.HasAttribute(chunk_naming.EXEC_OUTPUT):
            self._exec_out_port = ChunkPortItem(
                f"{prim.GetPath()}.outputs:chunk", _PORT_EXEC, parent=self
            )
            self._exec_out_port.setPos(self._width, _NODE_HEADER_H // 2)
            self._ports.append(self._exec_out_port)
        else:
            self._exec_out_port = None

    def sync_merge_ports(self) -> bool:
        """Re-place a dynamic-exec node's (Merge, Switch, ...) exec input slots when the number
        of connected branches changed.

        Such a node shows one port per branch plus one empty trailer, so wiring the trailer
        has to grow a fresh one and clearing a slot has to drop one. Ports are built once at
        node construction, and a connection change does not rebuild the node -- only the
        edges -- so without this the trailer would only appear after some unrelated full
        scene rebuild.

        Returns:
            bool: True if the ports were rebuilt (the caller's edge items are now stale)
        """
        if not self._dynamic_exec or not self.prim.IsValid():
            return False
        want = len(chunk_stage.exec_input_connections(self.prim)) + 1
        # Counted from _exec_in_ports, not "every is_input port in self._ports": a node with
        # a body also carries typed param input ports (e.g. Switch's Index, if wired), which
        # share is_input=True and would silently throw this count off.
        have = len(self._exec_in_ports)
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
        # A merge has no params, so there is no body worth embedding -- and skipping the proxy
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
        chunk_widget.setFixedWidth(int(self._width))
        self._chunk_widget = chunk_widget  # pin Python ref; PySide6 may GC despite Qt C++ ownership
        self._proxy = QtWidgets.QGraphicsProxyWidget(self)
        self._proxy.setWidget(chunk_widget)
        self._proxy.setPos(0, _NODE_HEADER_H)
        if not self._locked:
            self._resize_handle = _NodeResizeHandle(self)
        # Proxies are built in deferred batches, so a node can arrive while the view is
        # already zoomed past the LOD threshold. Without this it would come up showing a
        # body every other node has dropped.
        views = self.scene().views() if self.scene() is not None else []
        if views and not getattr(views[0], "_lod_bodies_shown", True):
            self._proxy.setVisible(False)
        if self.chunk.has_state(ChunkFlags.State.COLLAPSED):
            # A chunk that is already collapsed by the time its proxy is finally built (e.g.
            # loaded from a saved scene, where create_proxy() runs in a deferred batch well
            # after state was restored) has no STATE_CHANGED transition to trigger
            # _set_collapsed's own setVisible(False) -- the state never actually changed,
            # it was simply already this way. Without this, the body shows fully expanded
            # (params and all) until something unrelated later toggles COLLAPSED off and
            # back on.
            self._proxy.setVisible(False)
        self._full_height = _NODE_HEADER_H + chunk_widget.sizeHint().height() + _NODE_BODY_BOTTOM
        self._height = self._full_height
        self.prepareGeometryChange()


        self._port_rebuild_timer = QtCore.QTimer(chunk_widget)
        self._port_rebuild_timer.setSingleShot(True)
        # Long enough to outlast a burst of geometry changes. Expanding a group animates
        # the content height, and every frame emits geometryChanged and restarts this
        # timer -- at interval 0 it fired between frames, so a rebuild that walks the whole
        # widget tree twice (two findChildren sweeps) ran once per frame. Ports only need
        # to be right once the geometry settles, so wait for it to stop moving.
        self._port_rebuild_timer.setInterval(_PORT_REBUILD_MS)
        self._port_rebuild_timer.timeout.connect(self._rebuild_typed_ports)
        # geometryChanged fires for collapse/expand after initial setup.
        # Initial port placement is triggered explicitly below so it doesn't
        # depend on geometryChanged firing at the right time relative to the connection.
        self._proxy.geometryChanged.connect(self._port_rebuild_timer.start)
        # Content changes that should shrink the body never resize the proxy, so
        # geometryChanged misses them. The rebuild's adjustSize() does the shrinking.
        _BodyLayoutWatcher(chunk_widget, self._port_rebuild_timer.start)
        # Belt and suspenders for a NESTED group's own collapse/expand: geometryChanged
        # is supposed to cover this too (a group's content resizing should resize the
        # whole chunk_widget in turn), but was observed live to not reliably do so for a
        # collapse specifically (expand was fine) -- animation_finished's SetNoConstraint
        # vs SetMinAndMaxSize switch on content_layout looks like the likely culprit,
        # leaving the node showing stale, overlapping ports until something unrelated
        # (e.g. deselecting, which _set_collapsed's own branch happens to also kick a
        # rebuild for) forced a fresh layout pass. Connecting directly to each group's own
        # animation-finished signal sidesteps depending on that propagation working.
        self._hooked_groups: weakref.WeakSet = weakref.WeakSet()
        self._hook_group_animations(chunk_widget)
        # No context object here, unlike every other deferred call in this file:
        # QGraphicsItem is not a QObject, so QTimer.singleShot has nothing to bind the
        # cancellation to. _rebuild_typed_ports carries the guard instead -- it checks the
        # prim, the proxy, and catches the RuntimeError from an already-destroyed C++
        # proxy -- which is why this one is safe to schedule bare.
        #
        # Immediate, though the body has no height yet and the debounced rebuild after its
        # layout redoes the placement: paste (_materialize_at) and menu-connect wire their
        # edges ~80 ms on and need the typed ports to exist by then.
        QtCore.QTimer.singleShot(0, self._rebuild_typed_ports)

    def _hook_group_animations(self, chunk_widget: QtWidgets.QWidget) -> None:
        """Rebuild ports when any collapsable group in the body finishes folding.

        Re-run on every port rebuild, not only at proxy creation: a folded group builds its
        contents on first expand (ContainerWidget.ensure_children_built), so groups nested
        in it don't exist yet when the proxy is made. Unhooked, collapsing one of those left
        its ports where its params used to be. The parent's own expand rebuilds ports,
        which hooks the new groups before they can be folded.
        """
        for nested in chunk_widget.findChildren(ContainerWidget):
            if nested in self._hooked_groups:
                continue
            if nested.group.has_flag(ChunkFlags.GroupFlags.COLLAPSABLE):
                nested.toggle_anim.finished.connect(self._port_rebuild_timer.start)
                self._hooked_groups.add(nested)

    def _rebuild_typed_ports(self):
        # This is deferred (singleShot), so by the time it runs the chunk may already be
        # gone -- disabled (prim inactive) or deleted (prim expired). Rebuilding ports reads
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
            # scheduling this deferred rebuild and it firing) -- nothing to rebuild.
            return
        scene = self.scene()
        for port in self._typed_ports:
            port.setParentItem(None)
            if scene:
                scene.removeItem(port)
        self._typed_ports.clear()
        # Every exec input, not just self._exec_in_port: a fixed-slot node has a body (so it
        # reaches this rebuild, unlike a merge) and rebuilding from the primary alone would
        # drop its other slots' ports on the first body relayout.
        self._ports = [p for p in (*self._exec_in_ports, self._exec_out_port) if p is not None]
        if self.chunk.has_state(ChunkFlags.State.COLLAPSED):
            # Proxy is hidden -- place ports at header centre using param list alone.
            self._place_param_ports_collapsed()
        else:
            self._hook_group_animations(self._proxy.widget())
            self._place_param_ports(self._proxy.widget())
        if scene:
            # Deferred: this fires once per node whose ports change, and a chunk being
            # built retypes its ports repeatedly. Edges only have to be right once the
            # burst settles.
            scene._request_build_edges()
        # New/removed/recolored port items don't always get their region marked
        # dirty automatically -- without this the old pixels can linger on screen
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
            if self._resize_handle is not None:
                # Its grab area runs the node's full height.
                self._resize_handle.prepareGeometryChange()
            scene = self.scene()
            if isinstance(scene, ChunkGraphScene):
                scene.note_height_change()
        except RuntimeError:
            return

    def _place_prim_output_ports(self, y_start: float, extend_height: bool = False):
        """Create output ports for every outputs:* attribute on the prim (excluding
        outputs:chunk) that isn't already represented by a param-level port -- every
        connectable param gets its own inputs:/outputs: pair from _place_param_ports
        regardless of the OUTPUT flag, so an OUTPUT param's real outputs:attr would
        otherwise show up twice (once inline with its widget, once again down here).
        """
        covered = {f"outputs:{p.name}" for p in _iter_connectable_params(self.chunk)}
        _STEP = _PORT_RADIUS * 2 + 8
        attrs = [
            a for a in self.prim.GetAttributes()
            if a.GetName().startswith("outputs:")
            and a.GetName() != chunk_naming.EXEC_OUTPUT
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
            port.setPos(self._width, y)
            self._ports.append(port)
            self._typed_ports.append(port)
            y += _STEP
        if extend_height:
            needed = y + _PORT_RADIUS + 4
            if needed > self._height:
                self._height = needed
                self.prepareGeometryChange()

    def _connected_param_names(self) -> set[str]:
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
        names = set()
        for attr_name, _, _ in _iter_prim_connections(self.prim):
            name = attr_name[len("inputs:"):]
            # inputs:<group>:<name> for a param in a group: the port goes by the name.
            if ":" in name and (param := self.input_param(attr_name)) is not None:
                name = param.name
            names.add(name)
        scene = self.scene()
        if isinstance(scene, ChunkGraphScene):
            names |= scene.fed_outputs(str(self.prim.GetPath()))
        return names

    def _nearest_collapsed_group(self, param) -> core.ChunkCommon | None:
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
            in_port = ChunkPortItem(f"{self.prim.GetPath()}.inputs:{param.name}", ptype, parent=self, param=param)
            in_port.setPos(0, y)
            self._ports.append(in_port)
            self._typed_ports.append(in_port)
            out_port = ChunkPortItem(f"{self.prim.GetPath()}.outputs:{param.name}", ptype, parent=self, param=param)
            out_port.setPos(self._width, y)
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
        self._full_height = _NODE_HEADER_H + chunk_widget.height() + _NODE_BODY_BOTTOM
        self._height = self._full_height
        self.prepareGeometryChange()

        param_widgets = {
            w.param: w
            for w in chunk_widget.findChildren(QtWidgets.QWidget)
            if isinstance(w, chunk_widget_api.ChunkParamWidgetBase)
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
        # the same pixel, where whichever ended up topmost would silently eat every drag
        # aimed at the other one's port.
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
            collapsed_group = self._nearest_collapsed_group(param)
            # A collapsed group builds its contents only once it is first opened
            # (ContainerWidget.ensure_children_built), so a param inside one may have no
            # widget yet. It does not need one: its port anchors at the group's header.
            if widget is None and collapsed_group is None:
                continue

            ptype = _param_port_type(param)

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
                # a param's own HIDDEN state, like Layer's Index field only showing
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

            in_port = ChunkPortItem(f"{self.prim.GetPath()}.inputs:{param.name}", ptype, parent=self, param=param)
            in_port.setPos(0, y)
            self._ports.append(in_port)
            self._typed_ports.append(in_port)

            out_port = ChunkPortItem(f"{self.prim.GetPath()}.outputs:{param.name}", ptype, parent=self, param=param)
            out_port.setPos(self._width, y)
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

    def get_port(self, attr_path: str) -> ChunkPortItem | None:
        for port in self._ports:
            if port.attr_path == attr_path:
                return port
        return None

    def input_param(self, attr_name: str):
        """The param an inputs: attr on this node's prim holds, or None.

        Args:
            attr_name: The attribute's name, inputs:<name> or, for a param in a group,
                inputs:<group>:<name>.
        """
        try:
            return self.chunk.get_param(attr_name[len("inputs:"):].replace(":", "."))
        except Exception:  # noqa: BLE001 - a chunk mid-teardown still draws
            return None

    def input_port(self, attr_name: str) -> ChunkPortItem | None:
        """The port a connection on the inputs: attr attr_name is drawn into.

        A port is named after its param alone (inputs:tx), but a param in a group keeps its
        value, and an expression typed into it its connection, on inputs:<group>:<name>.

        Args:
            attr_name: The attribute's name.
        """
        if (port := self.get_port(f"{self.prim.GetPath()}.{attr_name}")) is not None:
            return port
        if ":" not in attr_name[len("inputs:"):] or (param := self.input_param(attr_name)) is None:
            return None
        return self.get_port(f"{self.prim.GetPath()}.inputs:{param.name}")

    def port_near(self, scene_pos: QtCore.QPointF, reach: float) -> ChunkPortItem | None:
        """The port closest to scene_pos, if one is within reach.

        Args:
            scene_pos: A point in the scene.
            reach: The furthest a port may be, in scene units.
        """
        best, best_dist = None, reach
        for port in self._ports:
            if port.isVisible() and (dist := QtCore.QLineF(port.scenePos(), scene_pos).length()) <= best_dist:
                best, best_dist = port, dist
        return best

    # ------------------------------------------------------------------
    # QGraphicsItem interface

    def boundingRect(self) -> QtCore.QRectF:
        if self._is_dot:
            pad = _PORT_RADIUS
            return QtCore.QRectF(-pad, -pad, _DOT_R * 2 + pad * 2, _DOT_R * 2 + pad * 2)
        return QtCore.QRectF(
            -_PORT_RADIUS, 0,
            self._width + _PORT_RADIUS * 2,
            self._height,
        )

    def _menu_button_rect(self) -> QtCore.QRectF:
        """Where the header's three-dot menu button is, in node coordinates."""
        return QtCore.QRectF(self._width - 26, 6, 20, min(_NODE_HEADER_H, self._height) - 12)

    def refresh_icon(self) -> None:
        """Read the header icon and colour again, after the prim's UsdUI icon or colour changed."""
        if not self.prim.IsValid():
            return
        self._icon, self._icon_color = node_theme.node_icon(self.chunk, self.prim)
        self.update()

    def _is_active_output(self) -> bool:
        """True if this is an Output and no view is active, so the graph cooks to it."""
        if not self._is_output or not self.prim.IsValid():
            return False
        sc = self.scene()
        return sc is not None and any(
            getattr(v, "_view_state", None) is not None and v._view_state.active_slot == 0 for v in sc.views()
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
            elif self._is_output:
                head = _COL_OUTPUT_HEADER if self._is_active_output() else _COL_VIEW_INACTIVE
            elif self._is_chunkgroup:
                head = _COL_GROUP_HEADER
            elif self._failed:
                head = _COL_FAILED_HEADER
            elif self._locked:
                head = _COL_LOCKED_HEADER
            bg = _COL_LOCKED_BG if self._locked else _COL_NODE_BG
            border = _COL_NODE_SELECTED if self._selected else (
                _COL_FAILED_BORDER if self._failed else _COL_NODE_BORDER)
            if self._disabled:
                bg, head, border = _dimmed(bg), _dimmed(head), _dimmed(border)
            painter.setBrush(bg)
            painter.setPen(QtGui.QPen(border, 1.5))
            painter.drawRect(QtCore.QRectF(0, 0, self._width, self._height))
            painter.setBrush(head)
            painter.setPen(QtCore.Qt.NoPen)
            painter.drawRect(QtCore.QRectF(0, 0, self._width, _NODE_HEADER_H))
            return

        bg_color = _COL_LOCKED_BG if self._locked else _COL_NODE_BG
        body = QtCore.QRectF(0, 0, self._width, self._height)
        radius = node_theme.NODE_RADIUS
        # The active view (the one currently driving execution) gets a green, thicker border
        # so it's obvious which of several view nodes is live.
        is_active_view = self._is_view and self._is_active_view()
        is_active_output = self._is_output and self._is_active_output()
        border_color = _COL_NODE_SELECTED if self._selected else _COL_NODE_BORDER
        border_w = 1.5 if self._selected else 1.0
        if self._failed and not self._selected:
            border_color = _COL_FAILED_BORDER
            border_w = 2.0
        if is_active_view or is_active_output:
            border_color = _COL_VIEW_ACTIVE
            border_w = 2.5

        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setBrush(bg_color)
        painter.setPen(QtCore.Qt.NoPen)
        painter.drawRoundedRect(body, radius, radius)

        # Header background -- square bottom corners so it merges with body
        if self._is_view:
            # Only the active view gets the warm orange header; inactive views go dim/grey
            # so it's obvious at a glance which one is driving execution.
            header_color = _COL_VIEW_HEADER if is_active_view else _COL_VIEW_INACTIVE
        elif self._is_output:
            # Dim like an inactive view while a view drives the graph instead.
            header_color = _COL_OUTPUT_HEADER if is_active_output else _COL_VIEW_INACTIVE
        elif self._is_chunkgroup:
            header_color = _COL_GROUP_HEADER
        elif self._failed:
            header_color = _COL_FAILED_HEADER
        elif self._locked:
            header_color = _COL_LOCKED_HEADER
        else:
            header_color = _COL_NODE_HEADER
        header_h = min(_NODE_HEADER_H, self._height)
        painter.setBrush(header_color)
        header = QtCore.QRectF(0, 0, self._width, header_h)
        painter.drawRoundedRect(header, radius, radius)
        if self._height > header_h + 1:
            # Square off the header's bottom corners where the body continues below it,
            # and draw the line between them.
            painter.drawRect(QtCore.QRectF(0, radius, self._width, header_h - radius))
            painter.setPen(QtGui.QPen(_COL_NODE_BORDER, 1))
            painter.drawLine(QtCore.QPointF(1, header_h - 0.5),
                             QtCore.QPointF(self._width - 1, header_h - 0.5))

        # The outline last, over both fills.
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.setPen(QtGui.QPen(border_color, border_w))
        painter.drawRoundedRect(body.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)

        # Icon, then name and type beside it.
        icon_size = 24
        text_x = 12
        pixmap = node_theme.icon_pixmap(self._icon, self._icon_color, icon_size,
                                        _icon_dpr(painter))
        if pixmap is not None:
            painter.drawPixmap(QtCore.QRectF(12, (header_h - icon_size) / 2, icon_size, icon_size),
                               pixmap, QtCore.QRectF(pixmap.rect()))
            text_x = 12 + icon_size + 10
        text_w = self._width - text_x - 30

        name_color = _COL_TEXT_LOCKED if self._locked else _COL_TEXT_PRIMARY
        painter.setPen(QtGui.QPen(name_color))
        painter.setFont(_header_font(10, bold=True))
        label = self.chunk.name
        if self._is_view:
            slot = self.prim.GetAttribute("view:slot").Get()
            label = f"View [{chunk_graph.view_number(slot)}]" if slot is not None else "View"
            if is_active_view:
                label = "● " + label  # ● marks the active view
        elif self._is_output:
            label = f"Output [{chunk_graph.OUTPUT_NUMBER}]"
            if is_active_output:
                label = "● " + label
        elif self._is_chunkgroup:
            label = f"{label}  ▶"
        elif self._locked:
            label = f"{label}  \U0001f512"
        if self._lofted:
            label = f"{label}  ↑"  # up arrow: lofted, so it shows on the Params page
        painter.drawText(
            QtCore.QRectF(text_x, header_h / 2 - 17, text_w, 18),
            QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
            label,
        )

        chunk_type = self._chunk_type_label
        sub_color = _COL_TEXT_LOCKED if self._locked else _COL_TEXT_SECONDARY
        painter.setPen(QtGui.QPen(sub_color))
        painter.setFont(_header_font(8))
        painter.drawText(
            QtCore.QRectF(text_x, header_h / 2 + 1, text_w, 14),
            QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
            str(chunk_type),
        )

        # The node's menu button: three dots at the right of the header.
        if not self._is_merge:
            menu = self._menu_button_rect()
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(_COL_TEXT_SECONDARY)
            for dy in (-5.0, 0.0, 5.0):
                painter.drawEllipse(QtCore.QPointF(menu.center().x(), menu.center().y() + dy), 1.6, 1.6)

        # Stale badge -- a small fixed-position corner icon rather than a border-color swap,
        # so it never fights with the selected (gold) or active-view (green) border colors.
        if self._stale:
            badge_r = 7.0
            badge_c = QtCore.QPointF(self._width - 38, header_h / 2)
            painter.setBrush(_COL_STALE)
            painter.setPen(QtCore.Qt.NoPen)
            painter.drawEllipse(badge_c, badge_r, badge_r)
            painter.setPen(QtGui.QPen(_COL_NODE_HEADER))
            painter.setFont(_header_font(9, bold=True))
            painter.drawText(
                QtCore.QRectF(badge_c.x() - badge_r, badge_c.y() - badge_r, badge_r * 2, badge_r * 2),
                QtCore.Qt.AlignCenter,
                "↻",
            )

        # Typed outputs section -- separator line + darker strip
        if self._outputs_start_y > _NODE_HEADER_H and self._height > self._outputs_start_y:
            outputs_rect = QtCore.QRectF(
                0, self._outputs_start_y,
                self._width, self._height - self._outputs_start_y,
            )
            # Inside the node's rounded outline, so the strip keeps its bottom corners.
            painter.save()
            outline = QtGui.QPainterPath()
            outline.addRoundedRect(body.adjusted(1, 1, -1, -1), radius, radius)
            painter.setClipPath(outline)
            painter.setBrush(_COL_NODE_HEADER)
            painter.setPen(QtCore.Qt.NoPen)
            painter.drawRect(outputs_rect)
            painter.setPen(QtGui.QPen(_COL_NODE_BORDER, 1))
            painter.drawLine(
                QtCore.QPointF(0, self._outputs_start_y),
                QtCore.QPointF(self._width, self._outputs_start_y),
            )
            painter.restore()

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
            if self._is_output:
                for view in (self.scene().views() if self.scene() else []):
                    if isinstance(view, ChunkGraphView):
                        view._pick_slot(0)
                        break
                event.accept()
                return
            # Debounced: a single real double-click has been observed (live log) delivering
            # this handler twice in a row, toggling COLLAPSED on then straight back off --
            # net no-op, so the node looks permanently stuck. See _COLLAPSE_TOGGLE_DEBOUNCE.
            now = time.monotonic()
            debounced = now - self._last_collapse_toggle_time < _COLLAPSE_TOGGLE_DEBOUNCE
            if self._is_chunkgroup:
                # A collapsed group hides its whole embedded widget -- including the minimize
                # arrow -- so the header double-click is the only way to expand it in the graph.
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
        if (event.button() == QtCore.Qt.LeftButton and not self._is_dot and not self._is_merge
                and self._menu_button_rect().contains(event.pos())):
            # The same menu a right-click opens. Opened after this press has finished, not
            # inside it: a menu run from within a press handler has crashed the graph (#86).
            views = self.scene().views() if self.scene() is not None else []
            if views and hasattr(views[0], "_show_chunk_menu"):
                view, pos = views[0], event.screenPos()
                QtCore.QTimer.singleShot(
                    0, view, lambda: self.prim.IsValid() and view._show_chunk_menu(self, pos))
            event.accept()
            return
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
                    # a plain click with no drag movement -- see mouseReleaseEvent.
                    self._drag_pending_collapse = True
            # A merge has no embedded widget, so its whole body is dead space -- dragging is
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
        prev_x: float | None = None
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
            # Only persist the move / consider a wire-splice if the node actually moved --
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
                # selection -- collapse the selection down to just this node now.
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
        """If this node was dropped on top of an EXEC wire A->B, splice it in as A->self->B.

        Exec only. A data wire means "this param takes its value from that one", and a node
        dropped across one has no business rewriting it. On a busy graph, where exec and
        data wires run through the same space, splicing into whichever wire a node happens
        to overlap would silently re-point param connections at the newcomer.

        Splicing a data wire is not merely riskier, it is a different operation: A -> B
        carries A's value to B, and A -> self -> B only reproduces that if self forwards it
        untouched, which nothing here can know. The exec chain has no such problem -- that
        is exactly what a chunk in the middle of it means.
        """
        scene = self.scene()
        if not isinstance(scene, ChunkGraphScene) or not scene.views():
            return
        view = scene.views()[0]
        # A node already wired on both exec sides is just being moved: it is in the chain
        # somewhere else, and splicing it here would silently re-point wires the user
        # authored rather than fill a gap. Dragging one past a wire is ordinary tidying.
        wired_in = wired_out = False
        for edge in scene._edges:
            if edge.dst_port in self._ports and edge.dst_port.port_type == _PORT_EXEC:
                wired_in = True
            if edge.src_port in self._ports and edge.src_port.port_type == _PORT_EXEC:
                wired_out = True
        if wired_in and wired_out:
            return
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
            if a_out.port_type != _PORT_EXEC or b_in.port_type != _PORT_EXEC:
                continue
            my_in = next(
                (
                    p for p in self._ports
                    if p.is_input and p.port_type == _PORT_EXEC and view._can_connect(a_out, p)
                ),
                None,
            )
            my_out = next(
                (
                    p for p in self._ports
                    if not p.is_input and p.port_type == _PORT_EXEC and view._can_connect(p, b_in)
                ),
                None,
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
        # session layer -- writing without an EditContext relies on the stage's ambient
        # edit target, which may not match wherever this attr's spec actually lives
        # (or, for the very first write, wherever it *should* go). Update an existing
        # spec in place; only resolve a fresh target for a genuinely new one.
        stage = self.prim.GetStage()
        home = chunk_utils.get_attr_home_layer(pos_attr)
        target = home or chunk_utils.get_bookkeeping_layer(stage, getattr(self, "chunk", None))
        with Usd.EditContext(stage, target):
            pos_attr.Set(Gf.Vec2f(p.x(), -p.y()))

    def set_width(self, width: float) -> None:
        """Make the node this wide, within the allowed range, and move its right-hand ports.

        Args:
            width: The new width in scene units.
        """
        width = float(min(max(width, _NODE_MIN_WIDTH), _NODE_MAX_WIDTH))
        old = self._width
        if width == old:
            return
        self.prepareGeometryChange()
        self._width = width
        if self._proxy is not None and getattr(self, "_chunk_widget", None) is not None:
            self._chunk_widget.setFixedWidth(int(width))
        if self._resize_handle is not None:
            self._resize_handle.setPos(width, 0)
        # Right away, so wires follow the edge while it is dragged; the rebuild below puts
        # them on their rows once the body has reflowed.
        for port in self._ports:
            if abs(port.pos().x() - old) < 0.5:
                port.setPos(width, port.pos().y())
        scene = self.scene()
        if isinstance(scene, ChunkGraphScene):
            scene.update_edges_for(self)
        self._kick_port_rebuild()
        self.update()

    def reset_width(self) -> None:
        """Back to the default width, and drop the saved size."""
        self.set_width(_NODE_WIDTH)
        attr = UsdUI.NodeGraphNodeAPI(self.prim).GetSizeAttr()
        home = chunk_utils.get_attr_home_layer(attr) if attr else None
        if home is not None:
            with Usd.EditContext(self.prim.GetStage(), home):
                attr.Clear()

    def _write_size_to_usd(self) -> None:
        """Save the width in UsdUI's size, in the same layer the position goes to."""
        ui_api = UsdUI.NodeGraphNodeAPI(self.prim)
        size_attr = ui_api.GetSizeAttr() or ui_api.CreateSizeAttr()
        stage = self.prim.GetStage()
        home = chunk_utils.get_attr_home_layer(size_attr)
        target = home or chunk_utils.get_bookkeeping_layer(stage, getattr(self, "chunk", None))
        with Usd.EditContext(stage, target):
            size_attr.Set(Gf.Vec2f(self._width, self._height))

    def _apply_usd_size(self) -> None:
        """Take the width from USD, after something other than a drag here changed it."""
        if self._is_dot or self._is_merge or not self.prim.IsValid():
            return
        self.set_width(_saved_node_width(self.prim))

    def _set_collapsed(self, collapsed: bool):
        # Every state change lands here, and re-applying the body's visibility each time is
        # load-bearing: it re-hides a collapsed body that something else showed.
        expanding = collapsed != self._collapsed
        self._collapsed = collapsed
        self.prepareGeometryChange()
        if self._proxy is not None:
            self._proxy.setVisible(not collapsed)
        if collapsed:
            # Rebuild rather than reposition: moving every typed port onto the header
            # centre point would stack them all on the exec ports' pixel, where the topmost
            # item eats all hover/click. A full rebuild both drops unwired ports (nothing
            # to show) and spaces the wired ones out (see _place_param_ports_collapsed),
            # same as an expand does.
            self._rebuild_typed_ports()
        else:
            self._height = self._full_height
            # Only on an actual expand. An already-expanded node got here on every state
            # change, so selecting one node and deselecting another rebuilt both nodes'
            # ports for nothing.
            if expanding and self._port_rebuild_timer is not None:
                # Recompute port positions from widget geometry now that proxy is visible.
                self._port_rebuild_timer.start()
        self.update()

    def _on_state_changed(self, notice, sender):
        # This is a global STATE_CHANGED callback and can arrive after the node's chunk was
        # removed -- the node's C++ item (and its proxy) may already be destroyed, so any Qt
        # call raises. Nothing to update in that case.
        try:
            selected = self.chunk.has_state(ChunkFlags.State.SELECTED)
            if selected != self._selected:
                self._selected = selected
                self.update()
            stale = self.chunk.has_state(ChunkFlags.State.STALE)
            failed = self.chunk.has_state(ChunkFlags.State.FAILED)
            if (stale, failed) != (self._stale, self._failed):
                self._stale, self._failed = stale, failed
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
            # LOD only ever toggles "full body" vs. "simple" and has no concept of collapse,
            # so a collapsed node's widget must stay hidden even when LOD asks to show bodies
            # again -- otherwise zooming back in force-shows the full param widget on a node
            # still drawn at header height (the widget then renders outside/under the node's
            # own box, with the usual stale-layout blank flash of a proxy widget that was
            # hidden for a while). Ports still follow LOD normally either way -- a collapsed
            # node's exec/connected ports are meant to stay visible independent of its body.
            widget_visible = visible and not self.chunk.has_state(ChunkFlags.State.COLLAPSED)
            # Skip only when the flag AND the proxy agree. They can diverge: a body that
            # declined an earlier hide, or one whose proxy was rebuilt under it, leaves the
            # flag saying flat while the widget is still up, and trusting the flag alone
            # then strands it visible forever (41 of 56 buried nodes, measured).
            if self._lod_simple == (not visible) and (
                proxy is None or proxy.isVisible() == widget_visible
            ):
                return
            self._lod_simple = not visible
            if proxy is not None:
                proxy.setVisible(widget_visible)
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
        if self._failed:
            self.setToolTip(
                "Its last cook failed or was cancelled, so the nodes below it did not cook.\n"
                "It cooks again on the next change."
            )
            return
        self.setToolTip(
            "Needs recook — inputs changed since this last ran. Press Run/Recompute to update it."
            if self._stale
            else ""
        )

    def _build_param_state_conns(self) -> list[NoticeConnection]:
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
        src_port: ChunkPortItem,
        dst_port: ChunkPortItem,
        expression: str | None = None,
    ):
        super().__init__()
        self.src_port = src_port
        self.dst_port = dst_port
        self._expression = expression
        self._p1 = QtCore.QPointF()
        self._p2 = QtCore.QPointF()
        self.setPen(QtGui.QPen(src_port.brush().color(), 2.0))
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
        # linearly scan the live item list instead -- fine for the small node counts here.
        self.setItemIndexMethod(QtWidgets.QGraphicsScene.ItemIndexMethod.NoIndex)
        # Graph-wide height settling -- see heights_settled.
        self._heights_settled = False
        self._height_settle_timer = None
        self._build_complete = False
        self._stage: Usd.Stage | None = None
        self._scene_sender: chunk_scene.SceneSender | None = None
        self._nodes: dict[str, ChunkNodeItem] = {}  # prim path → node
        # Source prim path -> names of its outputs that some node in the scene is wired to.
        # Built on demand by fed_outputs and dropped whenever a wire or the node set can
        # have changed; see there.
        self._fed_outputs_index: dict[str, set[str]] | None = None
        self._edges: list[ChunkEdgeItem] = []
        self._show_param_edges: bool = True
        self._built_context: object = _UNBUILT  # sentinel -- tracks what context is currently rendered
        self._dirty: bool = True  # set by invalidate(); cleared by a (re)build
        self._edge_timer: QtCore.QTimer | None = None
        self._proxy_busy_wait_since: float | None = None  # see _create_proxies_batch
        # The stage listeners build() makes. Both are global, so without this a scene Qt has
        # deleted kept reacting to every stage change.
        self._stage_listeners: list[NoticeConnection] = []
        disconnect_when_destroyed(self, self._stage_listeners)

    def fed_outputs(self, prim_path: str) -> set[str]:
        """Names of prim_path's outputs that a node in this scene has a wire from.

        The reverse half of ChunkNodeItem._connected_param_names: a wire lives on the
        downstream prim's inputs:, so finding the ones that start at a node means reading
        every other node. Each node asked that on every port placement, which is every
        node times every placement -- 84k connection scans and over a second to dive into
        an 86-node group. Read once here instead, and dropped by _on_objects_changed when a
        wire can have moved and by _rebuild_nodes when the node set does.
        """
        if self._fed_outputs_index is None:
            index: dict[str, set[str]] = {}
            for node in self._nodes.values():
                for _, src_prim_path, src_attr_suffix in _iter_prim_connections(node.prim):
                    if src_attr_suffix.startswith("outputs:"):
                        index.setdefault(src_prim_path, set()).add(src_attr_suffix[len("outputs:"):])
            self._fed_outputs_index = index
        return self._fed_outputs_index.get(prim_path, set())

    def set_show_param_edges(self, show: bool):
        self._show_param_edges = show
        self._build_edges()

    def invalidate(self):
        # Mark that a rebuild is needed WITHOUT clobbering _built_context. The scene's own
        # notice-driven rebuild reads _built_context; nulling it to the sentinel here caused
        # deletes to rebuild with no context and produce an empty graph until a tab-toggle.
        self._dirty = True

    def build(self, stage: Usd.Stage, scene_sender: chunk_scene.SceneSender, context=None):
        self._built_context = context
        self._dirty = False
        self._stage = stage
        self._scene_sender = scene_sender

        for conn in self._stage_listeners:
            conn.disconnect()
        self._stage_listeners[:] = [
            NoticeConnection(self._on_objects_changed, ChunkNoticeAlias.OBJECT_CHANGED, globally=True),
            NoticeConnection(self._on_chunk_deleted, ChunkNoticeAlias.CHUNK_DELETED, globally=True),
        ]

        self._rebuild_nodes()
        # The context this graph was just built for is what the path row names, and a build
        # is the only thing that changes it.
        for view in self.views():
            if hasattr(view, "refresh_breadcrumb"):
                view.refresh_breadcrumb()

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
                    # window and Qt shows it immediately -- set this before setParent(None)
                    # or every rebuild flashes one native window per node (see create_proxy).
                    w.setAttribute(QtCore.Qt.WA_DontShowOnScreen)
                    w.setParent(None)
                    w.deleteLater()
            except RuntimeError:
                pass
        self.clear()

    def _teardown_node(self, node: ChunkNodeItem) -> None:
        """Same detach-then-destroy sequence as _teardown_items, but for one node -- used by the
        incremental path in _rebuild_nodes so a node that's actually gone doesn't wait for (or
        force) a full-scene wipe.
        """
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
        recreated -- tearing down and rebuilding every node's ChunkWidget proxy (a native Qt
        window under the hood) on every single structural change, even for one unrelated
        new/deleted node, would make the whole graph visibly flash/collapse/expand and could
        show OS-level popup-window flicker, since create_proxy()'s "never let the OS show a
        top-level window" workaround would run once per EXISTING node. Only nodes whose chunk no longer
        exists are torn down, and only nodes that are newly created get a new proxy built.
        """
        if not self._stage or not self._scene_sender:
            return
        stage = self._stage
        scene_sender = self._scene_sender
        context = self._built_context
        if context is _UNBUILT:
            return  # not built yet -- a real build() will render with a proper context

        # Derive the prim root for this context
        if context is not None and getattr(context, "prim_path", None):
            chunks_root_path = chunk_stage._resolve_chunk_root(context)
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
        self._fed_outputs_index = None  # the node set and its prim handles are about to change
        # Bump the build generation so any in-flight deferred proxy batch from a superseded
        # rebuild aborts instead of creating a ghost proxy on a node that got torn down below.
        self._build_gen = getattr(self, "_build_gen", 0) + 1

        chunks_by_path = {
            f"{chunk_stage._resolve_chunk_root(c)}/{c.name}": c
            for c in scene_sender.get_chunks()
            # Exclude the context chunk itself: get_context() returns self for a CONTEXT chunk,
            # so a group would otherwise map to "{its own prim}/{its name}" -- the path of a child
            # inside it -- and shadow a real nested chunk of the same name (backing that node with
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

        # Pre-compute lock state: a prim is locked when it is composed in -- its def comes
        # from a foreign layer or across an arc (chunk_utils.composed_in_from) -- and that
        # layer is not the current edit target. Its OWN def in an owned layer means it is
        # ours to move, however much composes in beneath it: a Reference hosting a network
        # has the network's group under its prim and is still this graph's node.
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

            src_layer = chunk_utils.composed_in_from(prim)
            locked = src_layer is not None and src_layer.identifier != edit_id

            existing = self._nodes.get(path)
            if existing is not None and existing.chunk is chunk:
                # Same chunk, same node -- refresh what a fresh construction would have picked
                # up (the prim handle itself may have been recomposed) and keep everything
                # else, including its proxy widget, exactly as-is.
                try:
                    existing.prim = prim
                    existing.set_locked(locked)
                    # A node can become a subnet after it was built: a Reference pointed
                    # at a network file is one from that moment, and dives on double-click.
                    existing.set_subnet(chunk_stage.is_subnet(chunk))
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
                    existing = None  # its C++ item was already destroyed somehow -- rebuild it
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
        # in progressively -- existing nodes already have theirs and are left untouched.
        if new_nodes:
            self._create_proxies_batch(new_nodes, self._build_gen)

    def _create_proxies_batch(self, remaining: list, gen: int):
        if gen != getattr(self, "_build_gen", gen):
            return  # a newer rebuild superseded this batch -- don't touch its stale nodes
        # Not while the host is busy. usdview holds a busy override cursor from early in its
        # start-up until its first image is drawn, and drains every pending zero-timer -- so
        # this whole batch chain -- inside a processEvents() call it makes on the way. A
        # ChunkWidget built in there costs 10-15x what the same one costs a moment later:
        # measured on a 21-node scene, RenderSettings 16s vs 1.4s, Camera 4.5s vs 0.3s,
        # PropertyEditor 4.4s vs 0.4s, for 50s of start-up against 15s. Not the GIL (no
        # worker thread alive at the time), not window visibility, not the event-loop
        # level; the plain Qt calls (setLayout, addWidget, setStyleSheet) are simply that
        # much slower inside the host's start-up pass. The override cursor is the host's own
        # signal that it is mid-operation, and it means the same thing during File > Open.
        #
        # Bounded (_PROXY_BUSY_WAIT_MAX_S): a host that holds the cursor for a long operation
        # gets its graph anyway, at the slow price, rather than never.
        if QtWidgets.QApplication.overrideCursor() is not None:
            now = time.monotonic()
            if self._proxy_busy_wait_since is None:
                self._proxy_busy_wait_since = now
            if now - self._proxy_busy_wait_since < _PROXY_BUSY_WAIT_MAX_S:
                QtCore.QTimer.singleShot(
                    _PROXY_BUSY_RETRY_MS, self, lambda: self._create_proxies_batch(remaining, gen)
                )
                return
        self._proxy_busy_wait_since = None
        batch, rest = remaining[:self._PROXY_BATCH_SIZE], remaining[self._PROXY_BATCH_SIZE:]
        for node in batch:
            # Never let one bad proxy abort the whole batch (which would leave every remaining
            # node without its widget -- the "only the port dot shows" corruption). Log and skip.
            try:
                node.create_proxy()
            except Exception:
                logger.exception("create_proxy failed for %s", getattr(node.chunk, 'name', '?'))
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

        _build_edges tears down and re-walks every node's attributes, and a single create can
        rebuild ports on one node hundreds of times. A burst of port changes only needs the
        one rebuild that follows it, so collapse them onto a zero-timer.
        """
        if self._edge_timer is None:
            self._edge_timer = QtCore.QTimer()
            self._edge_timer.setSingleShot(True)
            self._edge_timer.setInterval(0)
            self._edge_timer.timeout.connect(self._build_edges)
        # Only when idle. The interval is zero, so restarting a running timer delays
        # nothing -- but it is not free, and this is called for every stage change, which
        # on a large scene can mean hundreds of thousands of restarts in one operation.
        if not self._edge_timer.isActive():
            self._edge_timer.start()

    def _build_edges(self):
        # A pending coalesced rebuild is redundant once we are rebuilding now.
        if self._edge_timer is not None:
            self._edge_timer.stop()

        for edge in self._edges:
            self.removeItem(edge)
        self._edges.clear()
        # A wire dragged into a param in a group is on inputs:<name> and its expression on
        # inputs:<group>:<name>: two connections, one wire.
        drawn: set[tuple] = set()

        # Before resolving ports: a merge grows/loses a slot as branches are wired and
        # cleared, and the port the next loop looks up has to exist. Safe to do here (rather
        # than at the connection site) precisely because edges have just been dropped --
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
                # Every exec slot, not just inputs:chunk -- a merge's branches past the first
                # live on inputs:chunk<N>, and treating those as typed param edges would hide
                # them behind the "Show param connections" toggle and then fail to find a
                # port for them at all.
                is_exec = chunk_stage.is_exec_input_name(attr_name)
                if not is_exec and not self._show_param_edges:
                    continue
                dst_port = node.input_port(attr_name)
                if dst_port is None:
                    continue
                expression: str | None = None
                param = node.input_param(attr_name)
                raw = getattr(param, 'raw_value', None) if param else None
                if isinstance(raw, str) and chunk_utils.has_expression(raw):
                    expression = raw

                # dict.fromkeys: two rows of one list read by one expression author the same
                # connection twice, and _source_ports already finds both rows from either.
                for conn_path in dict.fromkeys(attr.GetConnections()):
                    src_node = self._nodes.get(str(conn_path.GetPrimPath()))
                    if src_node is None:
                        continue
                    # Connection is always owned by the consumer, so the source
                    # always visualizes from its output dot regardless of attr prefix.
                    src_path = str(conn_path).replace(".inputs:", ".outputs:", 1)
                    for src_port in self._source_ports(src_node, src_path, expression):
                        if (src_port, dst_port) in drawn:
                            continue
                        drawn.add((src_port, dst_port))
                        edge = ChunkEdgeItem(src_port, dst_port, expression)
                        self.addItem(edge)
                        self._edges.append(edge)

        if self._show_param_edges:
            self._build_list_row_edges()

        for node in self._nodes.values():
            if node.prim.IsValid():
                node._sync_vector_states()

    def _build_list_row_edges(self) -> None:
        """Draw the wires into list rows, which live in each row's expression, not in USD.

        See chunk_graph.connect for why a row has no connection of its own.
        """
        by_name = {n.prim.GetName(): n for n in self._nodes.values() if n.prim.IsValid()}
        for node in by_name.values():
            for dst_port in node._typed_ports:
                if not (dst_port.is_input and chunk_graph.is_list_row(dst_port.param)):
                    continue
                raw = dst_port.param.raw_value
                if not chunk_utils.is_expression_text(raw):
                    continue
                for src_name, src_attr in dict.fromkeys(chunk_utils.parse_expression_attrs(raw)):
                    src_node = by_name.get(src_name)
                    if src_node is None:
                        continue
                    src_path = f"{src_node.prim.GetPath()}.outputs:{src_attr}"
                    for src_port in self._source_ports(src_node, src_path, raw):
                        edge = ChunkEdgeItem(src_port, dst_port, raw)
                        self.addItem(edge)
                        self._edges.append(edge)

    @staticmethod
    def _source_ports(src_node: ChunkNodeItem, src_path: str, expression: str | None) -> list[ChunkPortItem]:
        """The output ports a wire from src_path is drawn from.

        Every row of a list shares its template's port path, so a wire from a row is told
        apart by the expression alone: @Node.item[1]@ is drawn from row 1's port. Without a
        row index it is the port at src_path, as for any other param.
        """
        node_name = src_node.prim.GetName()
        attr_name = src_path.rsplit(":", 1)[-1]
        rows = set()
        for ref_node, ref in chunk_utils.parse_expression(expression or ""):
            name, index = chunk_utils.split_row_ref(ref)
            if ref_node == node_name and name == attr_name and index is not None:
                rows.add(ref)
        if rows:
            return [port for port in src_node._ports
                    if port.attr_path == src_path and chunk_graph.is_list_row(port.param)
                    and core.list_row_ref(port.param) in rows]
        port = src_node.get_port(src_path)
        return [port] if port is not None else []

    def update_edges_for(self, node: ChunkNodeItem):
        node_ports = set(node._ports)
        for edge in self._edges:
            if edge.src_port in node_ports or edge.dst_port in node_ports:
                edge.update_path()

    # ------------------------------------------------------------------
    # Auto-layout (L key)

    def layout_nodes(self, seed_paths: list[str] | None = None) -> None:
        """Auto-layout -- see usd_chunks/graph/layout.py for the algorithm itself.

        seed_paths: prim paths to grow into their full connected subgraph before laying out
        (the "Layout Selected" case). None lays out every node currently in this context.

        The computation is chunk_graph.layout_graph, shared with the other frontends; what
        stays here is applying it, because only this one has QGraphicsItems to move.
        """
        if not self._nodes:
            return
        prims = {path: node.prim for path, node in self._nodes.items() if node.prim.IsValid()}

        def size_of(path):
            rect = self._nodes[path].boundingRect()
            return rect.width(), rect.height()

        def current_pos(path):
            point = self._nodes[path].pos()
            return point.x(), point.y()

        positions = chunk_graph.layout_graph(
            prims, size_of, current_pos, seeds=seed_paths,
            # Unlike the other frontends, this one knows about locked nodes: a prim from an
            # external or referenced layer must not be moved, though it still acts as an
            # obstacle.
            is_locked=lambda path: self._nodes[path]._locked,
        )

        # Apply after the full layout is computed (not interleaved with computing it) -- each
        # setPos triggers itemChange -> update_edges_for automatically, and _write_pos_to_usd
        # persists the same ui:nodegraph:node:pos attribute an interactive drag already writes.
        for path, (x, y) in positions.items():
            node = self._nodes.get(path)
            if node is None:
                continue
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
        # Default to a full recook -- safe and correct -- UNLESS
        # ChunkGraphView._delete_selected already proved this specific delete can't matter and
        # flagged it via _recook_skip_next_delete. That flag has to be set BEFORE
        # request_delete() runs: request_delete() force-unblocks the chunk as a prerequisite for
        # its own CHUNK_DELETED to fire (a blocked chunk suppresses its own notices), so by the
        # time this notice arrives, is_blocked() always reads False regardless of whether the
        # chunk was ever really cooking -- the delete itself erases that signal, so it can't be
        # judged here after the fact. Deletes triggered from elsewhere (e.g. the Params panel)
        # never set the flag, so they always recook.
        self._schedule_rebuild()
        if sender is not None and not isinstance(sender, core.ChunkBase):
            # A param, not a chunk: a Light rebuilds its schema params on every cook, 38 of
            # them, and each recook here cooked it again -- 84 cooks for one slider step in the
            # kitchen scene. A deleted chunk still recooks below on its own notice, which
            # comes after its params'. The rebuild above stays: ports follow the params.
            return
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
        prim with no node is exactly what a stale graph looks like.
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
        # Before any early return below. A wire is an inputs: property, so an edit to one or
        # any resync can move it; a cook's hundreds of plain attribute writes cannot.
        if self._fed_outputs_index is not None and (
            notice.GetResyncedPaths()
            or any(p.IsPropertyPath() and p.name.startswith("inputs:")
                   for p in notice.GetChangedInfoOnlyPaths())
        ):
            self._fed_outputs_index = None
        # A prim-level resync under our chunks root means a chunk was added/removed/enabled/
        # disabled. Rebuild the whole graph from scratch -- only active chunks survive --
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
            elif path.IsPropertyPath() and path.name == "ui:nodegraph:node:size":
                node = self._nodes.get(str(path.GetPrimPath()))
                if node:
                    node._apply_usd_size()
            elif path.IsPropertyPath() and path.name in (
                    "ui:nodegraph:node:icon", "ui:nodegraph:node:displayColor"):
                node = self._nodes.get(str(path.GetPrimPath()))
                if node:
                    node.refresh_icon()
        # Deferred: a single cook emits hundreds of these notices -- one per attribute it
        # writes -- and rebuilding every edge in the scene on each was 286 rebuilds for one
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

    def __init__(self, view: ChunkGraphView):
        super().__init__(view)
        # Weak, deliberately. Installing this router on the QApplication pins the router,
        # and a strong back-reference here would pin the whole ChunkGraphView through it --
        # leaking the entire scene and widget tree for every view ever constructed, and
        # preventing the destroyed signal that uninstall() hangs off from ever firing.
        self._view = weakref.ref(view)
        self._busy = False
        self._active: QtWidgets.QWidget | None = None  # proxy widget last clicked
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

    def set_active(self, widget: QtWidgets.QWidget | None) -> None:
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
        if etype == QtCore.QEvent.Type.MouseButtonPress:
            # A click anywhere outside this graph view -- most commonly usdview's own 3D
            # viewport -- means the user's intent has moved on from whatever proxy widget
            # was last clicked in the graph. Without this, _active/_view_active stay stuck
            # on e.g. a Script Runner's text field forever (nothing else ever clears them),
            # and the unconditional fallback at the bottom of this method kept hijacking
            # every later keystroke -- including the viewport's own R/F camera shortcuts --
            # and typing them into that field instead, no matter where the cursor now was.
            if QtWidgets.QApplication.activePopupWidget() is None:
                view_global = QtCore.QRect(view.mapToGlobal(QtCore.QPoint(0, 0)), view.size())
                if not view_global.contains(QtGui.QCursor.pos()):
                    self._active = None
                    self._view_active = False
            return False
        if etype not in (QtCore.QEvent.Type.KeyPress, QtCore.QEvent.Type.ShortcutOverride):
            return False
        if QtWidgets.QApplication.activePopupWidget() is not None:
            # A popup (a QMenu) owns the keyboard while it is up, and every shortcut below
            # is a GRAPH shortcut -- none of them mean anything until it closes. This filter
            # is installed on the QApplication, so without this it would keep firing
            # straight through an open menu, and the menu's own filter does not stop it: that
            # one only forwards printable characters to the search box, and Tab is not.
            #
            # Otherwise pressing Tab repeatedly would re-enter _open_tab_menu while its own
            # blocking exec_() is still running, stacking a menu per press, and "f" / "l"
            # typed into the search box would frame / auto-layout the graph behind it.
            return False

        fw = QtWidgets.QApplication.focusWidget()
        if isinstance(fw, self._INPUT_TYPES):
            inside_proxy = self._active is not None and (
                fw is self._active or self._active.isAncestorOf(fw)
            )
            if inside_proxy:
                # QLineEdit inside our proxy has Qt-internal focus -- prevent shortcuts
                # but let KeyPress flow natively so the widget handles it directly.
                if etype == QtCore.QEvent.Type.ShortcutOverride:
                    event.accept()
                return False
            # Input widget outside our proxy has focus -- deactivate and defer.
            self._active = None
            self._view_active = False
            return False

        # Native OS focus doesn't reliably land on a proxy's QLineEdit even when the user is
        # actively typing into it (the same focus-stealing problem the class docstring
        # describes) -- that's exactly why the manual-forwarding fallback at the bottom of
        # this method reads self._active.focusWidget() instead of trusting QApplication's
        # own idea of focus. The _INPUT_TYPES check above only catches the case where native
        # focus DOES happen to agree; check the proxy's own logical focus target too, so a
        # digit/Ctrl+C/etc typed into e.g. Layer's "Layer Name" field isn't stolen by
        # the ungated blocks below before it ever reaches that fallback.
        active_focus = self._active.focusWidget() if self._active is not None else None
        typing_in_proxy = isinstance(active_focus, self._INPUT_TYPES)

        # Ctrl+F-to-search: ahead of bare F, which would otherwise frame. Like Ctrl+C, not
        # gated on a proxy being active, only on not typing in one.
        if (not typing_in_proxy and event.key() == QtCore.Qt.Key.Key_F
                and event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier):
            view_global = QtCore.QRect(view.mapToGlobal(QtCore.QPoint(0, 0)), view.size())
            if view_global.contains(QtGui.QCursor.pos()):
                if etype == QtCore.QEvent.Type.ShortcutOverride:
                    event.accept()
                    return True
                if etype == QtCore.QEvent.Type.KeyPress:
                    view._open_search()
                    event.accept()
                    return True

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

        # 1/2/3-to-switch-view-slot (1 the Output, 2/3 the views): same fallback pattern as Ctrl+C/V/D/X above -- native
        # keyPressEvent (chunk_qt_nodegraph.py ChunkGraphView.keyPressEvent) has the same
        # logic, but depends on ChunkGraphView holding real native Qt focus, which usdview's
        # MainWindow routinely reclaims. F/L/Ctrl+C/V/D/X were already hardened against that
        # via this router; the view-slot keys were never added to it, so they're the one
        # shortcut still riding the fragile native-focus path. Also gated on
        # `not typing_in_proxy` -- these ARE digits, so without that guard this swallowed
        # every "1"/"2" typed into e.g. Layer's "Layer Name" field.
        if not typing_in_proxy and (slot := slot_for_key(event.key())) is not None:
            view_global = QtCore.QRect(view.mapToGlobal(QtCore.QPoint(0, 0)), view.size())
            if view_global.contains(QtGui.QCursor.pos()):
                if etype == QtCore.QEvent.Type.ShortcutOverride:
                    event.accept()
                    return True
                if etype == QtCore.QEvent.Type.KeyPress:
                    view._pick_slot(slot)
                    event.accept()
                    return True

        # Delete-selected: same fallback pattern as Ctrl+C/V/D/X above. Selecting a node
        # is itself a click on its embedded proxy body, which is exactly the case native
        # view focus (ChunkGraphView.keyPressEvent's own Key_Delete handler) can't be
        # trusted to have. Gated on `not typing_in_proxy` so Delete still
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

        # Bare C (collapse selected-or-all): same gating as bare D, and here the fallback is
        # not a reliability nicety but the only path that works at all -- usdview binds "c"
        # itself (it cycles the camera, printing "Camera: Free AutoClip" in the viewport), so
        # the key never reached ChunkGraphView's own Key_C handler and pressing C over the
        # graph moved the camera instead of collapsing anything. Accepting the
        # ShortcutOverride is what takes the key back while the cursor is over the graph;
        # everywhere else usdview keeps it.
        if not typing_in_proxy and not ctrl_down and event.key() == QtCore.Qt.Key.Key_C:
            view_global = QtCore.QRect(view.mapToGlobal(QtCore.QPoint(0, 0)), view.size())
            if view_global.contains(QtGui.QCursor.pos()):
                if etype == QtCore.QEvent.Type.ShortcutOverride:
                    event.accept()
                    return True
                if etype == QtCore.QEvent.Type.KeyPress:
                    view._collapse_selected_or_all()
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

        if self._active is None or not self._view_active:
            # _view_active is False once a click has landed outside the graph view (see the
            # MouseButtonPress branch above) -- an _active proxy left over from an earlier
            # click must not keep swallowing keys once the user's attention, and clicks, have
            # moved to e.g. the viewport.
            return False
        if etype == QtCore.QEvent.Type.ShortcutOverride:
            event.accept()
            return True
        # Route to whichever child last had logical (not native) focus.
        #
        # target.event(), not QApplication.sendEvent: sendEvent runs the forwarded key back
        # through every application-level filter, and usdview's AppEventFilter is one. It
        # sees its MainWindow holding native focus -- not a text field -- and claims its
        # nav keys for itself: Left/Right step the playhead, "f" frames the selection. So
        # arrows never moved the cursor in a node's text field and a lowercase f was never
        # typed, while the same field in the params panel (native focus) worked fine.
        target = self._active.focusWidget() or self._active
        self._busy = True
        try:
            target.event(event)
        finally:
            self._busy = False
        return True


# ---------------------------------------------------------------------------
# Search


class GraphSearchPopup(QtWidgets.QFrame):
    """Ctrl+F: find a node by name or chunk type and frame it.

    Enter frames the best match and closes. Ctrl+Enter frames the next match and stays
    open, so pressing it again steps through them all. The Name and Type boxes narrow what
    is matched; the view remembers them for the next search. A popup, like the Tab menu, so
    it gets the keyboard even while usdview holds focus.
    """

    WIDTH = 360

    def __init__(self, view: ChunkGraphView):
        super().__init__(view, QtCore.Qt.WindowType.Popup)
        self._view = view
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        self.field = QtWidgets.QLineEdit(placeholderText="Find node by name or type")
        self.field.installEventFilter(self)
        self.field.textChanged.connect(self._search)
        self.by_name = QtWidgets.QCheckBox("Name")
        self.by_type = QtWidgets.QCheckBox("Type")
        names, types = view._search_scope
        self.by_name.setChecked(names)
        self.by_type.setChecked(types)
        options = QtWidgets.QHBoxLayout()
        for box in (self.by_name, self.by_type):
            # Clicking one must leave the typing in the search box.
            box.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
            box.toggled.connect(self._scope_changed)
            options.addWidget(box)
        options.addStretch()
        self.status = QtWidgets.QLabel()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.field)
        layout.addLayout(options)
        layout.addWidget(self.status)
        #: Prim paths of the matches, best first, and which one is framed (-1: none yet).
        self.matches: list[str] = []
        self.index = -1

    def _scope_changed(self) -> None:
        self._view._search_scope = (self.by_name.isChecked(), self.by_type.isChecked())
        self._search(self.field.text())

    def _search(self, text: str) -> None:
        self.matches = self._view._search_nodes(text, *self._view._search_scope)
        self.index = -1
        self._show_status()

    def _show_status(self) -> None:
        if not self.field.text().strip():
            text = "Enter: frame the best match.  Ctrl+Enter: step through them."
        elif not any(self._view._search_scope):
            text = "Tick Name or Type to search"
        elif not self.matches:
            text = "No match"
        elif self.index < 0:
            text = f"{len(self.matches)} found"
        else:
            text = f"{self.index + 1} of {len(self.matches)}: {Sdf.Path(self.matches[self.index]).name}"
        self.status.setText(text)

    def step(self) -> None:
        """Frame the next match, back to the first after the last."""
        if not self.matches:
            return
        self.index = (self.index + 1) % len(self.matches)
        self._view._focus_node(self.matches[self.index])
        self._show_status()

    def accept(self) -> None:
        """Frame the best match, or the one stepped to, and close."""
        if self.matches:
            self._view._focus_node(self.matches[max(self.index, 0)])
        self.close()

    def eventFilter(self, obj, event) -> bool:
        if obj is self.field and event.type() == QtCore.QEvent.Type.KeyPress:
            key = event.key()
            if key in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
                if event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier:
                    self.step()
                else:
                    self.accept()
                return True
            if key == QtCore.Qt.Key.Key_Escape:
                self.close()
                return True
        return super().eventFilter(obj, event)

    def popup(self) -> None:
        """Show it along the top of the view, ready to type into."""
        width = min(self.WIDTH, max(self._view.width() - 20, 120))
        self.setFixedWidth(width)
        self.move(self._view.mapToGlobal(QtCore.QPoint((self._view.width() - width) // 2, 8)))
        self._show_status()
        self.show()
        self.field.setFocus()


# ---------------------------------------------------------------------------
# View

# Moved to the engine when connect() took over writing the expression -- this was its
# only caller. Aliased rather than deleted: same reason as _PORT_*, one definition.
_set_param_visibly = chunk_graph.set_param_visibly


def _auto_connect_saved() -> bool:
    """Whether Auto-connect was ticked last session."""
    return window_state.settings().value(_AUTO_CONNECT_KEY, False, type=bool)


def _set_auto_connect(on: bool) -> None:
    """Tick or untick Auto-connect in every graph view, and remember it."""
    window_state.settings().setValue(_AUTO_CONNECT_KEY, on)
    for view in list(_graph_views):
        box = getattr(view, "_auto_connect_box", None)
        if box is not None and box.isChecked() != on:
            box.blockSignals(True)
            box.setChecked(on)
            box.blockSignals(False)


def _badge_font() -> QtGui.QFont:
    """The font of the Output / View badge in the graph's bottom-right corner."""
    return QtGui.QFont("monospace", 9, QtGui.QFont.Bold)


def _port_node_path(port: ChunkPortItem) -> str:
    """The prim path of the node a port belongs to."""
    return str(Sdf.Path(port.attr_path).GetPrimPath())


def _exec_port(node: ChunkNodeItem, is_input: bool) -> ChunkPortItem | None:
    """A node's exec input or output port, or None."""
    return next((p for p in node._ports if p.port_type == _PORT_EXEC and p.is_input == is_input), None)


class ChunkGraphView(QtWidgets.QGraphicsView):
    """QGraphicsView with zoom, pan (middle mouse or Space+drag), and F to frame."""

    _SCENE_EXTENT = 50000
    _GRID_SPACING = 200  # scene units -- fixed world-space, so it scales naturally with zoom
    # ...but only down to a point. The dots are cosmetic (constant screen size), so as the
    # view zooms out they crowd together into a speckle that reads as noise rather than a
    # grid, and their count grows with the square of the zoom-out -- thousands of them at
    # the far end. The spacing doubles instead, holding the on-screen gap between this and
    # twice it at every zoom, which bounds the count. Deliberately below the 200px gap seen
    # at scale 1.0: matching that reads as an empty canvas once zoomed out.
    _GRID_MIN_SCREEN_SPACING = 55  # px
    _GRID_MAX_DOUBLINGS = 12
    _GRID_DOT_COLOR = QtGui.QColor("#2c2f37")  # muted grey-blue, subtle against the canvas
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
        self.setBackgroundBrush(_COL_CANVAS_BG)
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
        # The scene's own rect grows as items move out past it; the canvas follows.
        self.scene().sceneRectChanged.connect(self._grow_to_fit)
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

        # The path to this graph's own context, floating IN the canvas rather than in a
        # strip above it. A row of chrome has to be given a height, and a height that suits
        # one font clips another -- which is exactly what happened: the descenders went
        # missing. Here the widget is sized by what it contains, so there is nothing to clip
        # it, and the path reads as part of the graph it describes.
        self._breadcrumb = ChunkContextBreadcrumb(self.viewport())
        self._breadcrumb.navigate.connect(self.dive_into)
        self._breadcrumb.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self._breadcrumb.move(_BREADCRUMB_MARGIN, _BREADCRUMB_MARGIN)
        self._breadcrumb.raise_()

        # Left of the Output badge in the bottom-right corner. Ticked, each chunk made from
        # the menu wires itself onto the end of the chain (see _chain_tail).
        self._auto_connect_box = QtWidgets.QCheckBox("Auto-connect", self.viewport())
        self._auto_connect_box.setToolTip(
            "Wire each new chunk after the selected one, or after the one the view shows,\n"
            "and move the view to it, to build a chain quickly."
        )
        self._auto_connect_box.setStyleSheet("QCheckBox { color: #c8c8c8; background: transparent; }")
        self._auto_connect_box.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self._auto_connect_box.setChecked(_auto_connect_saved())
        self._auto_connect_box.toggled.connect(_set_auto_connect)
        self._auto_connect_box.adjustSize()
        _graph_views.add(self)

        self._proxy_key_router = _ProxyKeyRouter(self)
        self._proxy_key_router.install()
        # Pair the install with a removal, or every view ever built leaves a filter
        # receiving key events for the rest of the session. Bind the router directly
        # rather than reaching through self: the view is mid-destruction when destroyed
        # fires. The router is a plain QObject child, so it is still alive at that point
        # -- ~QObject emits destroyed() before deleting children.
        self.destroyed.connect(lambda *_, _router=self._proxy_key_router: _router.uninstall())

        self._drag_src_port: ChunkPortItem | None = None
        self._drag_edge: QtWidgets.QGraphicsPathItem | None = None
        self._drag_detach_attr: Usd.Attribute | None = None
        self._drag_detach_source_port: ChunkPortItem | None = None
        self._drag_reused_edge: ChunkEdgeItem | None = None
        # Suppresses build()'s generic _ensure_default_view while a drag-and-drop chunk
        # import is in flight -- see _import_as_chunks/_ensure_view_for_imported.
        self._suppress_default_view = False
        self._quiet_slot_change = False  # see _set_slot_quietly
        # Debounce state for build()'s post-build recook -- see _schedule_restore_active_view.
        self._restore_view_token = 0
        self._restore_view_changed_chunks: set | None = set()
        self._view_state = ViewSlotState(self)
        self._view_state.slot_changed.connect(self._on_view_slot_changed)
        self._view_state.create_view_for.connect(self._on_create_view_for_path)
        self._view_state.set_output_for.connect(self._on_set_output_for_path)
        self._view_state.loft_changed.connect(self._on_loft_changed)

    def _on_toggle_param_edges(self, checked: bool):
        graph_scene = self.scene()
        if isinstance(graph_scene, ChunkGraphScene):
            graph_scene.set_show_param_edges(checked)

    def resizeEvent(self, event: QtGui.QResizeEvent):
        super().resizeEvent(event)
        self._menubar.setFixedWidth(self.width())
        self._anchor_breadcrumb()
        self._schedule_lod()

    def scrollContentsBy(self, dx: int, dy: int):
        """Keep the context path pinned to the corner while the canvas moves under it.

        QAbstractScrollArea scrolls the viewport's CHILD WIDGETS along with its contents, so
        a widget parented to the viewport pans with the graph -- the path drifted off with
        the nodes and read as one of them. Panning is the only thing that moves it, so
        putting it back here costs a move per scroll and nothing else.

        Panning also changes which nodes are on screen, so LOD is re-run (throttled).
        """
        super().scrollContentsBy(dx, dy)
        self._anchor_breadcrumb()
        self._schedule_lod()

    def _schedule_lod(self) -> None:
        """Re-run _apply_lod soon; a throttle, not a debounce, so a long pan keeps updating."""
        timer = getattr(self, "_lod_pan_timer", None)
        if timer is None:
            timer = QtCore.QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(_LOD_PAN_THROTTLE_MS)
            timer.timeout.connect(self._apply_lod)
            self._lod_pan_timer = timer
        if not timer.isActive():
            timer.start()

    def _anchor_breadcrumb(self) -> None:
        """Top-left of the canvas, above whatever is drawn there.

        Also puts the Auto-connect checkbox back beside the Output badge: both are widgets
        on the viewport, which scrolls its children with the canvas.
        """
        breadcrumb = getattr(self, "_breadcrumb", None)
        if breadcrumb is not None:
            breadcrumb.move(_BREADCRUMB_MARGIN, _BREADCRUMB_MARGIN)
            breadcrumb.raise_()
        box = getattr(self, "_auto_connect_box", None)
        if box is not None:
            badge = self._slot_badge()[0]
            box.move(badge.left() - _AUTO_CONNECT_GAP - box.width(),
                     badge.center().y() - box.height() // 2)
            box.raise_()

    def hideEvent(self, event: QtGui.QHideEvent):
        super().hideEvent(event)
        # Fires when a dived-into ChunkGroup's page stops being the current one in its
        # ChunkTabContextStack (QStackedWidget.setCurrentIndex hides the outgoing page,
        # it doesn't destroy it) -- see _ProxyKeyRouter.uninstall's docstring.
        self._proxy_key_router.uninstall()

    def showEvent(self, event: QtGui.QShowEvent):
        super().showEvent(event)
        self._proxy_key_router.install()
        self.refresh_breadcrumb()

    def refresh_breadcrumb(self) -> None:
        """Point the path row at this graph's own context.

        Called when the page becomes visible and at the end of a scene build -- the two
        moments the context can differ from what is drawn. Cheap either way: the row is one
        button per level of nesting.
        """
        breadcrumb = getattr(self, "_breadcrumb", None)
        if breadcrumb is None:
            return
        breadcrumb.set_context(self._graph_context())
        # Sized by its contents, not by a strip: a floating row has nothing to stretch it,
        # so it has to take the width its buttons actually need or it clips them.
        breadcrumb.resize(breadcrumb.sizeHint())
        self._anchor_breadcrumb()

    @staticmethod
    def _grid_points_in_rect(rect: QtCore.QRectF, spacing: int) -> list:
        """Scene-space grid dot positions covering rect, snapped to fixed spacing-unit lines.

        Pure math, no painter -- kept separate from drawBackground so the snapping/coverage
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
        # Only the exposed rect is ever painted here (never the full 100k-unit scene extent --
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

    def _slot_badge(self) -> tuple[QtCore.QRect, str, QtGui.QColor]:
        """The Output / View badge in the bottom-right corner: where it sits, its text, its colour."""
        if self._view_state.active_slot == 0:
            text = "Output"
            colour = _COL_OUTPUT_HEADER
        else:
            text = f"View: {chunk_graph.view_number(self._view_state.active_slot)}"
            colour = _COL_VIEW_HEADER
        fm = QtGui.QFontMetrics(_badge_font())
        pad_x, pad_y = 8, 5
        w = fm.horizontalAdvance(text) + pad_x * 2
        h = fm.height() + pad_y * 2
        vp = self.viewport()
        rect = QtCore.QRect(vp.width() - w - 8, vp.height() - h - 8, w, h)
        return rect, text, QtGui.QColor(colour.red(), colour.green(), colour.blue(), 210)

    def drawForeground(self, painter: QtGui.QPainter, rect) -> None:
        super().drawForeground(painter, rect)
        painter.save()
        painter.resetTransform()
        badge, text, bg = self._slot_badge()
        painter.setFont(_badge_font())
        painter.setBrush(bg)
        painter.setPen(QtCore.Qt.NoPen)
        painter.drawRoundedRect(badge, 4, 4)
        painter.setPen(QtGui.QColor("#ffffff"))
        painter.drawText(badge, QtCore.Qt.AlignCenter, text)
        painter.restore()

    def sizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(200, 200)

    def minimumSizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(0, 0)

    def build(self, stage: Usd.Stage, scene_sender: chunk_scene.SceneSender, context=None, changed_chunks=None):
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
        # Auto-create the Output when the graph is opened on a context that has chunks but
        # neither an Output nor a view yet (e.g. chunks were made in the params panel first).
        # Deferred past this build's rebuild + proxy pass so creation runs on a settled graph.
        # Cheap (just a membership scan) even when re-triggered by an unrelated create, so this
        # one stays unconditional.
        QtCore.QTimer.singleShot(80, self, lambda ctx=context: self._ensure_default_view(ctx))
        # Switching to this context must re-establish the viewport for ITS active view -- the
        # viewport is one global stage, so a view activated in another context (e.g. an inner
        # group view) otherwise stays cooked and the wrong result shows. Deferred past the
        # auto-view step. (On create we deliberately don't re-cook, to avoid churn.)
        self._schedule_restore_active_view(changed_chunks)

    def _schedule_restore_active_view(self, changed_chunks: Iterable[core.ChunkBase] | None) -> None:
        """Debounce build()'s post-build recook across a burst of build() calls landing in
        the same event-loop tick.

        A bulk file load calls build() once per chunk, synchronously, in the same call
        stack (see load_chunks' _add_prim_mods -- each restored chunk fires its own
        CHUNK_LOADED, which ChunkWidgetDocker._loaded_module_callback turns straight into a
        build() call while the Graph tab is active). recook_for_view mutes and reveals its
        whole cook set every time it runs, and cooks whatever is stale at the time, so one
        timer per call would do that pass N times for an N-chunk load, cooking chunks
        mid-load. The calls share one timer instead.

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
        """True when this context (or the graph as a whole, if context is None) has a view
        chunk or an Output: something that decides what cooks, so a new node starts blocked
        until it is wired in, and no default needs adding."""
        return chunk_graph.context_has_view(scene, context) or chunk_graph.context_has_output(scene, context)

    def _nest_target(self, context):
        """The context new chunks should nest into, or None to leave them where they land.

        When the graph is dived into a chunkgroup, add_chunk must parent into it -- it uses
        the scene's current context, which isn't set for a dive, so without this creating a
        node inside a group silently added it to the tab instead. No-op at the tab level
        (plain contexts have no module).

        Pushing and restoring is SceneSender.creating_in's job now; this only answers which
        context, which is the half that differs between frontends.
        """
        cfg = context.get_config() if context is not None else None
        if context is not None and context.has_flag(ChunkFlags.GroupFlags.CONTEXT) and cfg and cfg.module:
            return context
        return None

    # ------------------------------------------------------------------
    # Copy / Paste

    def _instantiate_chunk_of_type(self, module_name: str):
        """Build a fresh chunk instance of the given module, not yet added to the scene.
        Returns (primary_chunk, all_chunks) -- a module's create() can spawn auxiliary
        chunks alongside the main one (mirrors the pattern already used by
        contextMenuEvent/_create_and_connect_from_menu); primary is the first non-view one.
        """
        registry = chunk_registry.ChunkRegistry()
        if not registry.initialized:
            return None, []
        mod = registry.get_chunk_module(module_name)
        if mod is None:
            return None, []
        chunks = mod.create() if mod.loaded else mod.load()
        primary = next((c for c in chunks if not chunk_stage.is_view_chunk(c)), None)
        return primary, list(chunks)

    def _snapshot_selection(self):
        """Build (clip_nodes, clip_edges, top_left) from the current selection -- the shared
        core of both Copy (into the module clipboard) and Duplicate (straight to paste,
        clipboard untouched). View chunks are never included -- they're per-context slot
        machinery, not content worth duplicating. A wire to a node OUTSIDE the selection is
        dropped, not recreated against the original outside node -- only wires between two
        selected nodes survive. Returns ([], [], None) if there's nothing selected to copy.
        """
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene) or not graph_scene._scene_sender or not graph_scene._stage:
            return [], [], None
        selected = [
            node for node in graph_scene._nodes.values()
            if node.chunk.has_state(ChunkFlags.State.SELECTED) and not node._is_view and not node._is_output
        ]
        if not selected:
            return [], [], None
        min_x = min(n.pos().x() for n in selected)
        min_y = min(n.pos().y() for n in selected)
        kept_index: dict[str, int] = {}
        clip_nodes: list[_ClipboardNode] = []
        for node in selected:
            rel_pos = (node.pos().x() - min_x, node.pos().y() - min_y)
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

    def selected_chunks(self, including: core.ChunkBase | None = None) -> list[core.ChunkBase]:
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
        wires between them, into the module-level clipboard.
        """
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
        source layer -- while the rest of a mixed selection is still cut normally.
        """
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene):
            return
        self._copy_selected_to_clipboard()
        if not _node_clipboard:
            return
        deletable = [
            node.chunk for node in graph_scene._nodes.values()
            if node.chunk.has_state(ChunkFlags.State.SELECTED) and not node._locked and not node._is_view
            and not node._is_output
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

    def _paste_clipboard_at(self, scene_pos: QtCore.QPointF) -> list[core.ChunkBase]:
        """Materialize the module-level clipboard as new chunk(s) centered at scene_pos."""
        if not _node_clipboard:
            return []
        return self._materialize_at(_node_clipboard, _edge_clipboard, scene_pos)

    def _materialize_at(
        self, clip_nodes: list[_ClipboardNode], clip_edges: list[_ClipboardEdge], scene_pos: QtCore.QPointF
    ) -> list[core.ChunkBase]:
        """Create new chunk(s) from clip_nodes centered at scene_pos, wire clip_edges between
        them, and select the result -- nested into whatever context/tab this view is
        currently showing, same mechanism right-click "New Chunk" already uses
        (scene.creating_in), which is what makes pasting/duplicating into a chunkgroup work
        for free, including a selection copied from a different context. Shared by both Paste
        (module clipboard) and Duplicate (clipboard-free, straight from the selection).
        Returns the list of newly created primary chunks.
        """
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
        new_chunks: list[core.ChunkBase] = []
        new_path_by_index: dict[int, str] = {}
        with scene.creating_in(self._nest_target(context)):
            for index, item in enumerate(clip_nodes):
                primary, chunks = self._instantiate_chunk_of_type(item.module_name)
                if primary is None:
                    continue
                # Guard against a premature REAL module execute -- NOT via set_blocked(),
                # which also suppresses per-param VALUE_CHANGED notices entirely (chunk_notice.py's
                # value_changed: "if self.is_blocked() ... return"). That collateral damage was
                # live-confirmed: a restored Layer insert_mode="Index" left the Index
                # spinbox widget missing, because insert_mode's own add_callback handler (which
                # unhides it) never got to run while blocked -- toggling the menu away and back
                # was the only way to make it reappear. _executing is a narrower, more accurate
                # tool for THIS specific hazard: chunk_notice.py's reserved-execute dispatch
                # checks it (`not getattr(prop, '_executing', False)`) but value_changed's
                # per-param notice path does not, so holding it through value application AND
                # add_chunk suppresses ONLY the real module.execute() call -- e.g. Layer
                # authoring a genuine anon Sdf.Layer the instant layer_name changes, which
                # ChunkBase.get_stage() lets happen even before this chunk is added to any scene
                # -- while ordinary reactive UI callbacks (visibility toggles, menu refreshes)
                # fire exactly as they would for live, interactive typing.
                blockable = [
                    c for c in chunks
                    if not chunk_stage.is_view_chunk(c) and not chunk_stage.is_group_chunk(c)
                ]
                guarded, self_building = _paste_guard_sets(chunks)
                for c in guarded:
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
                chunk_graph.apply_clipboard_node(primary, item)
                pos = (scene_pos.x() + item.rel_pos[0], scene_pos.y() + item.rel_pos[1])
                # USD is Y-up, Qt is Y-down -- _create_node_pos is read by chunk_stage in USD
                # space (see chunk_stage._create_node_pos usage elsewhere in this file).
                with chunk_stage.creating_at((pos[0], -pos[1])):
                    for c in chunks:
                        scene.add_chunk(c)
                    chunk_graph.apply_disabled(primary, item)
                    for c in guarded:
                        c._executing = False
                    if view_exists:
                        # Stays inert (muted/uncooked) until manually wired into the view --
                        # _executing only guarded the value-restoration
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
                    for c in self_building:
                        c.execute()
                new_chunks.append(primary)
                new_path_by_index[index] = chunk_stage.get_chunk_prim_path(primary)
                if item.children:
                    self._materialize_children(
                        primary, item.children, item.child_edges, getattr(item, "lofts", []),
                    )
        if new_chunks and graph_scene._scene_sender:
            graph_scene._scene_sender.set_selected_chunks(new_chunks)
        if clip_edges:
            # The new nodes' ports (typed ones especially) don't exist yet -- their proxy
            # widgets are built in deferred batches (see ChunkGraphScene._create_proxies_batch).
            # Same deferred-wiring pattern _create_and_connect_from_menu already uses.
            #
            # Hold the generic auto-view off until that wiring lands. Until it does, every
            # pasted chunk still looks like an unwired exec tail, and _ensure_default_view
            # would take tails[0] -- the FIRST node of the chain rather than its end, racing
            # an 80ms timer.
            self._suppress_default_view = True
            QtCore.QTimer.singleShot(
                80, self,
                lambda paths=new_path_by_index, edges=list(clip_edges), created=list(new_chunks):
                    self._wire_pasted_edges(paths, edges, created),
            )
        return new_chunks

    def _materialize_children(
        self, parent_chunk: core.ChunkBase,
        clip_children: list[_ClipboardNode], clip_child_edges: list[_ClipboardEdge],
        clip_lofts: list[tuple[int, str]] | None = None,
    ) -> None:
        """Recreate a ChunkGroup's copied children nested inside the newly created
        parent_chunk (recursing further for a nested sub-group), wiring their internal
        connections directly at the USD level via _author_raw_connection -- no live Qt ports
        exist for a group's children unless someone has actually dived into that group, so
        the ChunkPortItem-based _wire_pasted_edges path used for the top-level paste doesn't
        apply here.
        """
        if not clip_children:
            return
        scene = _chunk.get_scene()
        if not scene:
            return
        stage = scene.get_stage()
        if not stage:
            return
        child_path_by_index: dict[int, str] = {}
        child_chunk_by_index: dict[int, core.ChunkBase] = {}
        with scene.creating_in(self._nest_target(parent_chunk)):
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
                # reactive code (e.g. Layer authoring an anon layer under whatever's
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
                    if not chunk_stage.is_view_chunk(c) and not chunk_stage.is_group_chunk(c)
                ]
                guarded, self_building = _paste_guard_sets(chunks)
                for c in guarded:
                    c._executing = True
                # Apply values before add_chunk -- see the matching comment in
                # _materialize_at for why (a widget can be built synchronously as a side
                # effect of add_chunk's own notice dispatch, and reads its initial value
                # once, at construction).
                chunk_graph.apply_clipboard_node(primary, item)
                with chunk_stage.creating_at((item.rel_pos[0], -item.rel_pos[1])):
                    for c in chunks:
                        scene.add_chunk(c)
                    chunk_graph.apply_disabled(primary, item)
                    for c in guarded:
                        c._executing = False
                    for c in blockable:
                        c.set_blocked(True)
                    for c in self_building:
                        c.execute()
                path = chunk_stage.get_chunk_prim_path(primary)
                child_path_by_index[index] = path
                child_chunk_by_index[index] = primary
                if item.children:
                    self._materialize_children(
                        primary, item.children, item.child_edges, getattr(item, "lofts", []),
                    )
        for edge in clip_child_edges:
            src_path = child_path_by_index.get(edge.src_index)
            dst_path = child_path_by_index.get(edge.dst_index)
            dst_chunk = child_chunk_by_index.get(edge.dst_index)
            if not src_path or not dst_path or dst_chunk is None:
                continue
            _author_raw_connection(stage, dst_path, edge.dst_attr, dst_chunk, src_path, edge.src_attr)

        # Re-loft, rather than re-connect: a loft creates a param on the group that the
        # pasted one does not have yet, so there is nothing for a connection to point at
        # until loft_param_to_context has made it. Last, so the children and their values
        # are fully in place -- lofting reads the inner param's current value onto the
        # interface, and forces its USD attr into existence if it has none.
        for child_index, inner_attr in clip_lofts or []:
            child = child_chunk_by_index.get(child_index)
            if child is None:
                continue
            # inputs:foo -> foo, and inputs:group:param -> group.param, which is the
            # dotted form get_param already understands.
            name = inner_attr.split(":", 1)[-1].replace(":", ".")
            param = child.get_param(name)
            if param is not None:
                chunk_stage.loft_param_to_context(param, parent_chunk)

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
            if node.chunk.has_state(ChunkFlags.State.SELECTED) and not node._is_view and not node._is_output
        ]
        if not selected:
            return

        scene_sender = graph_scene._scene_sender
        centre_x = sum(n.pos().x() for n in selected) / len(selected)
        centre_y = sum(n.pos().y() for n in selected) / len(selected)

        # Read the boundary while it still exists. Cut heals the chain closed as it
        # removes the nodes, so afterwards nothing records what used to attach where.
        inside = {chunk_stage.get_chunk_prim_path(n.chunk) for n in selected}
        feeds_selection, fed_by_selection = chunk_graph.exec_boundary(stage, inside)

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
        with scene_sender.creating_in(context):
            scene_sender.add_chunk(group)

        group_prim = stage.GetPrimAtPath(Sdf.Path(chunk_stage.get_chunk_prim_path(group)))
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
        graph_scene._built_context = group
        try:
            with scene_sender.creating_in(group):
                pasted = self._paste_clipboard_at(QtCore.QPointF(centre_x, centre_y))
        finally:
            graph_scene._built_context = prev_built

        # Wire the pasted chunks to each other here rather than leaving it to the paste's
        # own deferred pass. That pass wires through the graph's PORT items, and the chunks
        # just pasted are inside a group the graph is not currently showing -- so they have
        # no node, no ports, and nothing to wire. Going straight to the exec connection is
        # the same thing the ports would have authored.
        param_edges = []
        for edge in edges:
            if edge.src_index >= len(pasted) or edge.dst_index >= len(pasted):
                continue
            if not (edge.src_attr == _EXEC_OUTPUT and chunk_stage.is_exec_input_name(edge.dst_attr)):
                param_edges.append(edge)
                continue
            # Into the slot it came from, and stacked: a merge inside the selection keeps
            # its branches in order.
            chunk_stage.connect_exec(
                stage, scene_sender,
                chunk_stage.get_chunk_prim_path(pasted[edge.src_index]),
                chunk_stage.get_chunk_prim_path(pasted[edge.dst_index]),
                input_name=edge.dst_attr, append=True,
            )
        # Param wires are read off the stage the same way, so they need no ports either.
        chunk_graph.rewire_pasted(stage, dict(enumerate(pasted)), param_edges)

        # Put the group where the selection was in the chain, each consumer in the slot the
        # selection fed. Writing every consumer's plain inputs:chunk instead replaced the
        # branch a merge already had there with the group.
        chunk_graph.attach_group(
            stage, scene_sender, chunk_stage.get_chunk_prim_path(group),
            feeds_selection, fed_by_selection,
        )

        scene_sender.set_selected_chunks([group])

    def _duplicate_selected(self) -> None:
        """Ctrl+D: copy+paste the current selection in one step, without touching the
        Ctrl+C/Ctrl+V clipboard (so it never clobbers whatever was last explicitly copied).
        Lands at a fixed +40/+40 offset from the originals rather than the mouse cursor.
        """
        clip_nodes, clip_edges, top_left = self._snapshot_selection()
        if not clip_nodes or top_left is None:
            return
        anchor = QtCore.QPointF(top_left.x() + 40, top_left.y() + 40)
        self._materialize_at(clip_nodes, clip_edges, anchor)

    def _wire_pasted_edges(
        self,
        new_path_by_index: dict[int, str],
        edges: list[_ClipboardEdge],
        pasted: list[core.ChunkBase] | None = None,
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
        # first (see _ensure_view_for_imported's docstring).
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
        # unblocks it -- no need to re-cook here.
        if self._context_has_view(registry.scene, context):
            return
        tails = chunk_stage.exec_tails(stage, registry.scene, context)
        if not tails:
            return
        self._create_output_node_for_prim(chunk_stage.get_chunk_prim_path(tails[0]))

    # ------------------------------------------------------------------
    # Zoom

    def _grow_to_fit(self, items_rect: QtCore.QRectF) -> None:
        """Keep every item reachable: the default canvas, grown to cover the items plus a margin.

        Panning moves the scroll bars, and Qt stops them at the view's scene rect. With the
        canvas fixed at +-_SCENE_EXTENT, a node placed past it -- a long scripted chain laid out
        in a row gets there quickly -- could never be scrolled to.

        Args:
            items_rect: The scene's rect, which grows to cover its items.
        """
        e = self._SCENE_EXTENT
        margin = e * 0.2
        grown = QtCore.QRectF(-e, -e, e * 2, e * 2).united(items_rect.adjusted(-margin, -margin, margin, margin))
        if grown != self.sceneRect():
            self.setSceneRect(grown)

    def wheelEvent(self, event: QtGui.QWheelEvent):
        factor = 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
        self.scale(factor, factor)
        self._apply_lod()

    def _apply_lod(self) -> None:
        """Swap node bodies for the flat form below _LOD_BODY_MIN_SCALE, and for nodes
        outside the (margin-grown) visible area.

        Called after anything that changes the view transform, and (throttled, via
        _schedule_lod) after panning and resizing, which change which nodes are on screen.
        Qt already skips painting offscreen nodes on its own (measured: 0 on screen repaints
        in 2.8ms against 66ms for 33), but a live offscreen body still costs layout and
        widget updates, so it is flattened too.
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
        live_rect = self._lod_live_rect() if show_bodies else QtCore.QRectF()
        self._lod_busy = True
        try:
            for node in nodes:
                node.set_body_visible(
                    show_bodies
                    and node not in buried
                    and live_rect.intersects(node.sceneBoundingRect())
                )
        finally:
            self._lod_busy = False

    def _lod_live_rect(self) -> QtCore.QRectF:
        """The scene area whose nodes keep a live body: what's on screen plus a margin.

        A view with no size yet (hidden page, not laid out) culls nothing -- it would
        otherwise flatten every node until its first resize.
        """
        vp_rect = self.viewport().rect()
        if vp_rect.isEmpty():
            e = self._SCENE_EXTENT
            return QtCore.QRectF(-e, -e, e * 2, e * 2)
        visible = self.mapToScene(vp_rect).boundingRect()
        mx = visible.width() * _LOD_VIEWPORT_MARGIN
        my = visible.height() * _LOD_VIEWPORT_MARGIN
        return visible.adjusted(-mx, -my, mx, my)

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
    # Pan -- middle mouse or Space + left drag

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
        if hit is not None and isinstance(hit.parentItem(), ChunkPortItem):
            # A lit port's ring is drawn over its dot, as an item of its own: the press is
            # the port's. Taken as the ring's, it fell through to the resize edge below.
            hit = hit.parentItem()

        # Walk up item parent chain to find a proxy widget, if any.
        proxy = None
        item = hit
        while item is not None:
            if isinstance(item, QtWidgets.QGraphicsProxyWidget):
                proxy = item
                break
            item = item.parentItem()

        # Port left-click → start wire drag (drag off a connected input port to disconnect it).
        if isinstance(hit, _NodeResizeHandle) and event.button() == QtCore.Qt.LeftButton:
            # The edge a node resizes by runs past its ports: a press just off a port's dot
            # is meant for the port.
            near = hit._node.port_near(scene_pos, _PORT_PRESS_PIXELS / (self.transform().m11() or 1.0))
            hit = near or hit
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
        # memory → native crash, which no try/except can catch -- so validity-check first.
        # Do NOT call into the proxy's C++ (mapFromScene/widget/childAt) here: on some Qt/
        # PySide6 builds that dereferences a proxy left in a bad state by a rebuild and crashes
        # usdview. Resolve the key-router target from the live node's pinned ChunkWidget via
        # Python identity instead -- no C++ touch on the proxy.
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
            # movement resolves to an empty box on release, which clears the selection --
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

    def frontend(self, docker=None) -> QtGraphFrontend:
        """This view as a chunk_graph.GraphFrontend.

        Cached, because the adapter is stateless but callers hold on to what they get and
        identity comparisons are cheaper to reason about than a new object per call.

        The keyboard handler below routes through this rather than calling the private
        methods directly, so the contract is a real code path rather than a shape only the
        tests ever exercise. That was a live review finding: an adapter nothing constructs
        rots, and passing a conformance test is not evidence it works.
        """
        existing = getattr(self, "_frontend", None)
        if existing is None or (docker is not None and existing.docker is not docker):
            self._frontend = QtGraphFrontend(self, docker)
        return self._frontend

    def keyPressEvent(self, event: QtGui.QKeyEvent):
        if event.key() == QtCore.Qt.Key.Key_Tab:
            self._open_tab_menu()
            event.accept()
            return
        if event.key() == QtCore.Qt.Key.Key_F:
            if event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier:
                self._open_search()
            else:
                self.frontend().frame(self.frontend().selected_nodes or self.scene()._nodes)
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
            self.frontend().delete_selected()
            event.accept()
            return
        if event.key() == QtCore.Qt.Key.Key_D:
            # Ctrl+D duplicates; bare D (no modifier) toggles disable -- same disambiguation
            # as Ctrl+V/bare-V below.
            if event.modifiers() & QtCore.Qt.ControlModifier:
                self.frontend().duplicate_selection()
            else:
                self.frontend().toggle_disable_selected()
            event.accept()
            return
        if event.key() == QtCore.Qt.Key.Key_C:
            # Ctrl+C copies; bare C collapses -- the same modifier disambiguation D and V
            # already use, so the single-key shortcuts keep their meaning.
            if event.modifiers() & QtCore.Qt.ControlModifier:
                self.frontend().copy_selection()
            else:
                self.frontend().collapse_selected_or_all()
            event.accept()
            return
        if event.key() == QtCore.Qt.Key.Key_X and (event.modifiers() & QtCore.Qt.ControlModifier):
            self.frontend().cut_selection()
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
        if (slot := slot_for_key(event.key())) is not None:
            self._pick_slot(slot)
            event.accept()
            return
        super().keyPressEvent(event)

    def _pick_slot(self, slot: int) -> None:
        """Drive this graph from slot -- 0 its Output, else that view -- if it has one.

        Never switches to what this graph lacks: an empty view slot, or the Output of a graph
        with none, cooks nothing and blocks the whole graph. Scoped to THIS graph's context:
        slots are per-context, so a global scan would accept a slot that only exists in
        another tab or group.
        """
        registry = chunk_registry.ChunkRegistry()
        stage = registry.scene.get_stage() if registry.initialized else None
        if not stage:
            return
        context = self._graph_context()
        if slot == 0:
            if chunk_stage.graph_outputs(stage, registry.scene, context):
                self._view_state.set_slot(0)
        elif chunk_stage.find_view_chunk_for_slot(slot, stage, registry.scene, context):
            self._view_state.set_slot(slot)

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
        chunk_graph.delete_chunks(selected)

    def _collapse_selected_or_all(self) -> None:
        """Collapse the selection, or the whole graph when nothing is selected.

        One direction for the lot: if anything in the target set is still expanded they all
        collapse, and only when every one of them is already collapsed does the key expand
        them again. Toggling each node independently would turn a mixed graph into the
        photographic negative of itself, which is never what "collapse all" means.

        A chunkgroup is left alone. Collapsing one hides its whole embedded widget --
        including the arrow that expands it -- so the only way back is a header
        double-click, and sweeping the graph is no place to put a node in that state.

        A view is left alone too, and that one is not cosmetic: a view has no body to
        collapse, is drawn collapsed already, and is the node that happens to be selected
        after the graph auto-creates one -- so counting it made a bare C decide the whole
        graph was "already collapsed" and expand instead.
        """
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene):
            return

        nodes = [
            n for n in graph_scene._nodes.values()
            if not n._is_chunkgroup and not chunk_stage.is_view_chunk(n.chunk)
        ]
        selected = [n for n in nodes if n.chunk.has_state(ChunkFlags.State.SELECTED)]
        targets = selected or nodes
        if not targets:
            return

        collapse = any(not n.chunk.has_state(ChunkFlags.State.COLLAPSED) for n in targets)
        for node in targets:
            if collapse:
                node.chunk.set_state(ChunkFlags.State.COLLAPSED)
            else:
                node.chunk.unset_state(ChunkFlags.State.COLLAPSED)

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
                drawn = None
                if not conns:
                    # A wire drawn with no connection on the port's own attr: a list row's
                    # (its expression), or one typed into a param in a group, connected on
                    # inputs:<group>:<name> (see ChunkNodeItem.input_port).
                    drawn = next((e for e in graph_scene._edges if e.dst_port is port), None)
                if conns or drawn:
                    self._drag_detach_attr = attr
                    self._drag_detach_source_port = (
                        drawn.src_port if drawn else self._find_sibling_output_port(conns[0], graph_scene)
                    )
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

    def _drop_target(self, scene_pos: QtCore.QPointF) -> ChunkPortItem | None:
        """The port a dragged wire would connect to if dropped at scene_pos, or None.

        The port under the cursor when it is a valid target, else the nearest valid one
        within _PORT_SNAP_PIXELS on screen. The live wire jumps to the same port, so where it
        shows it landing is where it lands.
        """
        # items(), not itemAt(): the dragged wire itself (Z=10) would hide the port under it.
        hit = next(
            (item for item in self.scene().items(scene_pos) if isinstance(item, ChunkPortItem)),
            None,
        )
        if hit is not None and self._drag_target_ok(hit):
            return hit
        return self._nearest_target_port(scene_pos)

    def _set_snap_port(self, port: ChunkPortItem | None) -> None:
        """Light up the port the dragged wire has jumped to, and only that one."""
        previous = getattr(self, "_drag_snap_port", None)
        if previous is port:
            return
        if previous is not None:
            try:
                previous.set_snapped(False)
            except RuntimeError:
                pass
        self._drag_snap_port = port
        if port is not None:
            port.set_snapped(True)

    def _update_wire_drag(self, scene_pos: QtCore.QPointF) -> None:
        # Near a port it can connect to, the wire's loose end jumps onto it and turns solid.
        target = self._drop_target(scene_pos) if self._drag_src_port is not None else None
        end = target.scenePos() if target is not None else scene_pos
        style = QtCore.Qt.PenStyle.SolidLine if target is not None else QtCore.Qt.PenStyle.DashLine
        self._set_snap_port(target)
        if self._drag_reused_edge is not None:
            edge = self._drag_reused_edge
            edge.setPen(QtGui.QPen(edge.pen().color(), 2.0, style))
            p1 = edge.src_port.scenePos()
            p2 = end
            dx = abs(p2.x() - p1.x()) * 0.5
            path = QtGui.QPainterPath(p1)
            path.cubicTo(p1.x() + dx, p1.y(), p2.x() - dx, p2.y(), p2.x(), p2.y())
            edge.prepareGeometryChange()
            edge.setPath(path)
            edge._p1, edge._p2 = p1, p2
        elif self._drag_edge is not None:
            self._drag_edge.setPen(QtGui.QPen(self._drag_edge.pen().color(), 2.0, style))
            self._drag_edge.setPath(
                _drag_wire_path(self._drag_src_port.scenePos(), end, self._drag_src_port.is_input))
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

    def _nearest_target_port(self, scene_pos: QtCore.QPointF) -> ChunkPortItem | None:
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
        # The same port the live wire jumped to (_drop_target): a pixel-perfect drop on the
        # tiny dot is hard to land, so a drop near a valid port lands on it.
        hit = self._drop_target(scene_pos)
        if hit is None:
            # The port under the cursor even when it is not a valid target: dropping a
            # grabbed wire back on its own port must not read as "dropped on nothing".
            hit = next((item for item in self.scene().items(scene_pos)
                        if isinstance(item, ChunkPortItem)), None)
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
        new node's ports don't exist yet).
        """
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
        # Block on creation (view-driven) -- see contextMenuEvent. Here the node is about to be
        # wired to the dragged port, so the deferred connect's recook will unblock + cook it.
        context = self._graph_context()
        view_exists = self._context_has_view(scene, context)
        # Create the node at the wire-drop point instead of the far-away index default.
        with chunk_stage.creating_at((scene_pos.x(), -scene_pos.y())), \
                scene.creating_in(self._nest_target(context)):
            for c in chunks:
                if view_exists and not chunk_stage.is_view_chunk(c) and not chunk_stage.is_group_chunk(c):
                    c.set_blocked(True)
                scene.add_chunk(c)
        new_chunk = next((c for c in chunks if not chunk_stage.is_view_chunk(c)), None)
        if new_chunk is None:
            return
        new_prim = chunk_stage.get_chunk_prim_path(new_chunk)
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
            # item down -- touching it further is a no-op (RuntimeError guarded).
            # Otherwise, snap it back to its real solid path/pen.
            try:
                edge = self._drag_reused_edge
                edge.setPen(QtGui.QPen(edge.src_port.brush().color(), 2.0))
                edge.update_path()
            except RuntimeError:
                pass
            self._drag_reused_edge = None
        self._set_snap_port(None)
        graph_scene = self.scene()
        if isinstance(graph_scene, ChunkGraphScene):
            for node in graph_scene._nodes.values():
                for port in node._ports:
                    port.set_highlighted(False)
        self._drag_src_port = None
        self._drag_detach_attr = None
        self._drag_detach_source_port = None

    def _as_port_ref(self, item: ChunkPortItem) -> chunk_graph.PortRef:
        """A Qt port item as the engine's plain description of it.

        The three fields the graph logic ever reads -- which attribute, what type, which
        direction -- which is why the logic was never really entangled with Qt at all.
        """
        return chunk_graph.PortRef(
            node_path=str(Sdf.Path(item.attr_path).GetPrimPath()),
            attr_path=item.attr_path,
            port_type=item.port_type,
            is_input=item.is_input,
            param=item.param,
        )

    def _can_connect(self, a: ChunkPortItem, b: ChunkPortItem) -> bool:
        return chunk_graph.can_connect(self._as_port_ref(a), self._as_port_ref(b))

    def _drag_target_ok(self, hit: ChunkPortItem) -> bool:
        """Whether `hit` is a valid drop target for the wire currently being dragged."""
        src = self._drag_src_port
        if hit is src or hit.port_type != src.port_type:
            return False
        if hit.is_input != src.is_input:
            return self._can_connect(src, hit)
        # Same role as the grabbed port -- only meaningful when we picked up an
        # existing connection at an input port, moving its destination elsewhere.
        if not src.is_input or self._drag_detach_source_port is None:
            return False
        return (
            Sdf.Path(hit.attr_path).GetPrimPath()
            != Sdf.Path(self._drag_detach_source_port.attr_path).GetPrimPath()
        )

    def _find_sibling_output_port(
        self, conn_path: Sdf.Path, graph_scene: ChunkGraphScene
    ) -> ChunkPortItem | None:
        """Resolve the ChunkPortItem for the source end of a connection. Exec connections
        point straight at outputs:chunk; typed param connections point at the source's
        inputs:param (see _author_connection), so the matching output port lives on the
        sibling outputs:param attribute.
        """
        src_prim_path = str(conn_path.GetPrimPath())
        src_node = graph_scene._nodes.get(src_prim_path)
        if src_node is None:
            return None
        attr_name = conn_path.name
        if attr_name.startswith("inputs:"):
            attr_name = "outputs:" + attr_name[len("inputs:"):]
        return src_node.get_port(f"{src_prim_path}.{attr_name}")

    def _engine_context(self, graph_scene: ChunkGraphScene):
        """The prims and chunks the engine needs, keyed the way it keys them.

        Not _graph_context: this class already has one of those, answering a different
        question with a different signature, and the later definition simply wins.
        """
        prims = {path: node.prim for path, node in graph_scene._nodes.items()
                 if node.prim.IsValid()}
        chunks = {path: node.chunk for path, node in graph_scene._nodes.items()
                  if getattr(node, "chunk", None) is not None}
        return prims, chunks

    def _author_connection(self, a: ChunkPortItem, b: ChunkPortItem) -> None:
        """Wire two ports, then redraw and recook.

        The authoring itself lives in chunk_graph, so this view and the Flet one perform
        the same operation rather than two implementations that agree only until one of
        them is fixed.
        """
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene) or not graph_scene._stage:
            return
        _prims, chunks = self._engine_context(graph_scene)
        change = chunk_graph.connect(graph_scene._stage, self._as_port_ref(a),
                                     self._as_port_ref(b), chunks)
        if change is None:
            return
        if change.expression_error:
            # The wire stands; only the expression was refused. Worth saying so -- the
            # value will not resolve through a connection that looks complete.
            logger.warning("Wired, but the expression was refused: %s", change.expression_error)

        # Everything below is the view's own work, which is why it stayed here. The
        # expression write that used to sit here is chunk_graph.connect's job now, and it
        # took set_param_visibly with it -- see that function.
        if change.exec_changed:
            # A new exec edge can move the node (and everything downstream) under a
            # different Edit Target -- re-home them to honour the target at the new
            # position. This path authors inputs:chunk directly, so DEPENDENCY_ADDED never
            # fires and ChunkStageSync does not reconcile on its own.
            chunk_stage.reconcile_edit_targets(graph_scene._stage, chunk_stage.ChunkStageSync()._scene)
        graph_scene._build_edges()
        # A new connection can only change the active view's cook set if its destination
        # is, or feeds, the view: wiring two nodes elsewhere cannot add anything reachable.
        touched = {chunks[path] for path in change.touched_nodes if path in chunks}
        self._recook_active_view(touched or None)

    def _disconnect_port(self, port: ChunkPortItem, attr: Usd.Attribute, graph_scene: ChunkGraphScene) -> None:
        """Cut the wire into a port, then redraw and recook.

        Only that wire: the node it fed keeps its own downstream wiring (see
        chunk_graph.disconnect's heal). Pulling a node out of the chain with healing is what
        shake-to-disconnect and delete are for.

        `attr` is accepted for call-site compatibility and deliberately unused: the engine
        resolves the attribute itself, so both frontends read the same one.
        """
        prims, chunks = self._engine_context(graph_scene)
        change = chunk_graph.disconnect(graph_scene._stage, self._as_port_ref(port),
                                        prims, chunks, heal=False)
        if not change.edges_dirty:
            return

        # Re-execute the owning chunk so shader-level connections (a UsdUVTexture's
        # inputs:st, say) clear to match. The engine resets the param; only the view knows
        # a cook is wanted here.
        if port.port_type != _PORT_EXEC:
            node = graph_scene._nodes.get(str(Sdf.Path(port.attr_path).GetPrimPath()))
            if node and getattr(node, "chunk", None):
                node.chunk.execute()

        # Detaching an exec wire can change which Edit Target applies to the node and its
        # downstream -- re-home them to the target that survives at the new position.
        chunk_stage.reconcile_edit_targets(graph_scene._stage, chunk_stage.ChunkStageSync()._scene)
        graph_scene._build_edges()
        touched = {chunks[path] for path in change.touched_nodes if path in chunks}
        self._recook_active_view(touched or None)

    def _clear_input_connection(self, port: ChunkPortItem, graph_scene: ChunkGraphScene) -> bool:
        """Clear one input port's connections (no exec chain-heal). For typed ports also reset
        the param and re-execute so shader-level connections clear. Returns True if it changed.
        """
        attr = graph_scene._stage.GetAttributeAtPath(Sdf.Path(port.attr_path))
        if not (attr and attr.IsValid()) or not attr.GetConnections():
            return False
        chunk_stage.ChunkStageSync().clear_attr_connections(attr)
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

    def _shake_disconnect_node(self, node: ChunkNodeItem) -> None:
        """Disconnect all of a node's wires, but heal the exec chain: for A -> C -> B, shaking C
        rewires B's exec input to A (A -> B) before isolating C. Data/typed connections to C are
        just cleared (no generic heal).
        """
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene) or not graph_scene._stage:
            return
        stage = graph_scene._stage
        node_prim = str(node.prim.GetPath())
        changed = False

        # Heal the exec chain: rewire each successor's exec input to this node's
        # predecessor(s). The predecessor is read from slot 0 only -- a successor has one slot
        # to inherit into, so it can be handed exactly one branch, and slot 0 is the primary
        # one (same choice as chunk_stage.heal_exec_chain_on_delete).
        exec_in = stage.GetAttributeAtPath(Sdf.Path(node_prim + ".inputs:chunk"))
        pred_conns = list(exec_in.GetConnections()) if (exec_in and exec_in.IsValid()) else []
        out_chunk_path = node_prim + ".outputs:chunk"
        for other in graph_scene._nodes.values():
            if other is node:
                continue
            # Successors are matched across every slot, though: a merge fed through slot 2
            # would otherwise keep pointing at the node being shaken loose.
            for succ in chunk_stage.iter_exec_inputs(other.prim):
                if not any(str(c) == out_chunk_path for c in succ.GetConnections()):
                    continue
                # Write to succ's own home layer, not the stage's ambient edit target (root,
                # since nothing here calls SetEditTarget) -- an interactively wired
                # connection almost always lives on the session layer, stronger than root,
                # so healing on root left the original opinion (pointing at the node being
                # shaken loose) in charge and the heal silently had no visible effect. Same
                # fix as chunk_stage.heal_exec_chain_on_delete.
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
            chunk_stage.reconcile_edit_targets(stage, chunk_stage.ChunkStageSync()._scene)
            graph_scene._build_edges()
            self._recook_active_view()

    def _show_chunk_menu(self, node: ChunkNodeItem, global_pos: QtCore.QPoint) -> None:
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
        docstring for why (PySide6 won't let tests monkeypatch QMenu.exec itself).
        """
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

    def _create_view_node_for(self, target_node: ChunkNodeItem) -> None:
        self._create_view_node_for_prim(str(target_node.prim.GetPath()))

    def _activate_menu_view(self, view_chunk, context, stage) -> None:
        """A view created from the chunk menu is inert -- it has no view:slot and isn't active,
        so it does nothing. Assign it a free slot (scoped to this graph) on its prim and make it
        the active view, so it behaves like a view from 'Set View' (just unwired until connected).
        """
        vp = stage.GetPrimAtPath(Sdf.Path(chunk_stage.get_chunk_prim_path(view_chunk)))
        if not vp.IsValid():
            return
        if vp.GetAttribute("view:slot").Get() is not None:
            return  # already set up
        used = set()
        for c in chunk_registry.ChunkRegistry().scene.get_chunks():
            if not (chunk_stage.is_view_chunk(c) and c is not view_chunk
                    and chunk_stage.is_node_in_context(c, context)):
                continue
            p = stage.GetPrimAtPath(Sdf.Path(chunk_stage.get_chunk_prim_path(c)))
            s = p.GetAttribute("view:slot").Get() if p.IsValid() else None
            if s is not None:
                used.add(s)
        slot = next((s for s in chunk_graph.VIEW_SLOTS if s not in used), None)
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

    def _create_output_node_for_prim(self, target_prim_path: str) -> None:
        """Give this graph its Output, wired from target_prim_path, then recook to it.

        Made on the graph's own initiative, so the selection is put back afterwards: adding
        a chunk selects it, and a paste or duplicate must keep the nodes it selected.
        """
        registry = chunk_registry.ChunkRegistry()
        if not registry.initialized:
            return
        stage = registry.scene.get_stage()
        scene = _chunk.get_scene()
        if not (stage and scene):
            return
        selected = list(scene.get_selected_chunks())
        if chunk_graph.create_output_for(stage, scene, target_prim_path) is None:
            return
        if selected:
            scene.set_selected_chunks(selected)
        graph_scene = self.scene()
        if isinstance(graph_scene, ChunkGraphScene):
            graph_scene._build_edges()
        # Context object, as elsewhere: the graph can be gone before this fires.
        QtCore.QTimer.singleShot(0, self, lambda: self._recook_active_view(select_view=False))

    def _create_view_node_for_prim(self, target_prim_path: str, keep_selection: bool = False,
                                   slot: int | None = None) -> None:
        """Create a view node wired to target_prim_path, then activate its slot.

        The creation itself is chunk_graph.create_view_for, shared with the Flet and ImGui
        frontends, so every frontend shows the same view nodes for the same file.

        Args:
            target_prim_path: The node to view.
            keep_selection: Put the selection back afterwards. Adding a chunk selects it,
                which is right when a person asked for the view and wrong when the graph
                added one on its own: a paste or duplicate lost the nodes it had selected.
            slot: Put the view in this slot ("Set View 2/3"), re-pointing the view that
                already holds it. None takes the lowest free slot.
        """
        registry = chunk_registry.ChunkRegistry()
        if not registry.initialized:
            return
        stage = registry.scene.get_stage()
        scene = _chunk.get_scene()
        if not (stage and scene):
            return
        selected = list(scene.get_selected_chunks()) if keep_selection else []
        slot = chunk_graph.create_view_for(stage, scene, target_prim_path, slot)
        if slot is None:
            return
        if selected:
            scene.set_selected_chunks(selected)
        graph_scene = self.scene()
        if isinstance(graph_scene, ChunkGraphScene):
            # A re-pointed view authored its exec input directly; draw the moved wire.
            graph_scene._build_edges()
        if slot == self._view_state.active_slot:
            # set_slot would be a no-op, but the slot's view may now look at another node.
            QtCore.QTimer.singleShot(0, self, lambda: self._recook_active_view())
            return
        # Context object, as elsewhere: the view can be gone before this fires.
        switch = self._set_slot_quietly if keep_selection else self._view_state.set_slot
        QtCore.QTimer.singleShot(0, self, lambda s=slot: switch(s))

    def set_view_state(self, vs: ViewSlotState) -> None:
        self._view_state.slot_changed.disconnect(self._on_view_slot_changed)
        self._view_state.create_view_for.disconnect(self._on_create_view_for_path)
        self._view_state.set_output_for.disconnect(self._on_set_output_for_path)
        self._view_state.loft_changed.disconnect(self._on_loft_changed)
        self._view_state = vs
        vs.slot_changed.connect(self._on_view_slot_changed)
        vs.create_view_for.connect(self._on_create_view_for_path)
        vs.set_output_for.connect(self._on_set_output_for_path)
        vs.loft_changed.connect(self._on_loft_changed)

    def _on_loft_changed(self, target_chunk: core.ChunkBase, lofted: bool) -> None:
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene):
            return
        node = graph_scene._nodes.get(chunk_stage.get_chunk_prim_path(target_chunk))
        if node is not None:
            node.set_lofted(lofted)

    def _on_create_view_for_path(self, prim_path: str, slot: int) -> None:
        self._create_view_node_for_prim(prim_path, slot=slot)

    def _on_set_output_for_path(self, prim_path: str) -> None:
        """Set Output: end this graph at prim_path, then cook to its Output."""
        registry = chunk_registry.ChunkRegistry()
        if not registry.initialized:
            return
        stage = registry.scene.get_stage()
        scene = _chunk.get_scene()
        if not (stage and scene):
            return
        if chunk_graph.set_output_for(stage, scene, self._graph_context(), prim_path) is None:
            return
        graph_scene = self.scene()
        if isinstance(graph_scene, ChunkGraphScene):
            # A re-pointed Output authored its exec input directly; draw the moved wire.
            graph_scene._build_edges()
        if self._view_state.active_slot == 0:
            # set_slot would be a no-op, but the Output may now end at another node.
            QtCore.QTimer.singleShot(0, self, lambda: self._recook_active_view())
            return
        # Context object, as elsewhere: the view can be gone before this fires.
        QtCore.QTimer.singleShot(0, self, lambda: self._view_state.set_slot(0))

    def _on_view_slot_changed(self, slot: int) -> None:
        if slot == 0:
            self._forget_saved_view()
        self._recook_active_view(select_view=not self._quiet_slot_change)
        self._anchor_breadcrumb()
        self.viewport().update()

    def _forget_saved_view(self) -> None:
        """Mark this context as cooking to its Output: chunk:activeView set to 0.

        Only a switch to the Output lands here (nothing else sets slot 0), and without it the
        slot saved last would bring its view back on the next rebuild or reload. Written as 0
        where the slot already lives, so it beats that opinion; every reader takes 0 as none.
        """
        registry = chunk_registry.ChunkRegistry()
        stage = registry.scene.get_stage() if registry.initialized else None
        context = self._graph_context()
        if not stage or context is None:
            return
        gp = stage.GetPrimAtPath(Sdf.Path(chunk_stage.get_chunk_prim_path(context)))
        attr = gp.GetAttribute(chunk_graph.ACTIVE_VIEW_ATTR) if gp and gp.IsValid() else None
        if not (attr and attr.IsValid() and attr.Get()):
            return
        prev_w = chunk_stage.chunk_notice._usd_write_active
        chunk_stage.chunk_notice._usd_write_active = True
        try:
            home = chunk_utils.get_attr_home_layer(attr) or chunk_utils.get_bookkeeping_layer(stage, context)
            with Usd.EditContext(stage, home):
                attr.Set(0)
        finally:
            chunk_stage.chunk_notice._usd_write_active = prev_w

    def _set_slot_quietly(self, slot: int) -> None:
        """Switch the active view without selecting it: for a switch the graph made itself."""
        self._quiet_slot_change = True
        try:
            self._view_state.set_slot(slot)
        finally:
            self._quiet_slot_change = False

    def _restore_active_view(self, changed_chunks: Iterable[core.ChunkBase] | None = None) -> None:
        """Adopt the View that drives this context as the active view after a (re)build.

        A freshly built graph starts at slot 0 (no view, so it cooks to its Output) -- but a
        loaded file whose graph had a view active should come up with that view driving
        execution, as it was when saved. Which View that is, is chunk_stage.graph_driver's to
        say. With a slot already active (context switch, not a load) this is just the recook
        the build always did.

        changed_chunks is passed straight through to _recook_active_view (see its docstring)
        on the paths that recook the CURRENT slot. The set_slot path below deliberately does
        not scope: changing slot is a genuine view switch and always needs a full recook.
        """
        if self._view_state.active_slot != 0:
            self._recook_active_view(changed_chunks, select_view=False)
            return
        registry = chunk_registry.ChunkRegistry()
        if not registry.initialized:
            return
        stage = registry.scene.get_stage()
        if not stage:
            return
        driver, _ = chunk_stage.graph_driver(stage, registry.scene, self._graph_context())
        slot = chunk_stage.view_slot(stage, driver) if driver is not None and chunk_stage.is_view_chunk(driver) else 0
        # A restore after a rebuild is the graph's doing, not a view switch: it must not take
        # the selection (a paste lost its pasted nodes to the view this way).
        if slot:
            # set_slot fires slot_changed → _on_view_slot_changed → recook
            self._set_slot_quietly(int(slot))
        else:
            self._recook_active_view(changed_chunks, select_view=False)

    def _recook_active_view(
        self, changed_chunks: Iterable[core.ChunkBase] | None = None, select_view: bool = True
    ) -> None:
        """(Re)apply view-driven execution for this context's active view.

        Called on every structural change (wire, disconnect, delete, create) and on view
        switch so which chunks cook stays live. With no active view (slot 0) the context
        cooks to its Output, or nothing if it has none. Must NOT be called from inside a graph rebuild -- the
        execute() it triggers would author, fire ObjectsChanged, and loop.

        changed_chunks: the specific chunk(s) a caller knows triggered this call (e.g. the
        node just created/deleted/(dis)connected). recook_for_view mutes and reveals its whole
        cook set every time it runs (it only re-executes the stale chunks -- see its own
        docstring), so calling it unconditionally on every structural edit anywhere in the
        graph would recompose the stage and re-dim every blocked chunk for no reason. When changed_chunks is given and none of them are the
        view or already reachable from it, the edit provably can't change what the view cooks
        (cook-set membership is exactly graph reachability from the view), so it's skipped.
        Omit changed_chunks (the default) to always recook -- used for genuinely
        graph-wide triggers like a view switch.

        select_view: select the active view. True for a switch a person made; False when
        the graph recooks on its own, which must leave the selection alone.
        """
        # This is always reachable through a deferred QTimer.singleShot (build()'s own
        # debounced post-build recook -- see _schedule_restore_active_view -- and
        # ChunkGraphScene._on_chunk_deleted's debounce both end up here later, not
        # synchronously) -- by the time it fires this view can have been torn down already
        # (e.g. a test's fixture finished, or the tab/context it belonged to was closed),
        # which crashes as "Internal C++ object already deleted" the moment anything below
        # touches self. A no-op recook on a dead view is correct: there's nothing left to
        # drive.
        if not shiboken6.isValid(self):
            return
        # Only the CURRENTLY SELECTED tab should drive the (global) stage. Gate on the owning
        # tab stack's visibility, NOT self.isVisible(): the graph is page 1 of a QStackedWidget,
        # so in Params mode this widget is hidden even though its tab is current -- self.isVisible()
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
            chunk_stage.find_view_chunk_for_slot(slot, stage, registry.scene, context) if slot else None
        )
        # With no active view the context cooks to its Outputs (recook_for_view's fallback),
        # so reachability is judged from each.
        drivers = [view_chunk] if view_chunk is not None else chunk_stage.graph_outputs(
            stage, registry.scene, context)
        if changed_chunks is not None and not any(
            self._could_affect_view_cook(changed_chunks, stage, registry.scene, driver)
            for driver in drivers
        ):
            return
        # Persist the active view slot on the context prim. For groups a parent cook picks
        # the same inner view from it (find_group_active_view); for tabs it round-trips the
        # last-active view through save/load (_restore_active_view reads it). Only a real
        # slot is written -- 0 just means "not restored yet" and must not clobber the saved
        # value before the deferred restore has read it.
        if context is not None and slot:
            gp = stage.GetPrimAtPath(Sdf.Path(chunk_stage.get_chunk_prim_path(context)))
            if gp and gp.IsValid():
                prev_w = chunk_stage.chunk_notice._usd_write_active
                chunk_stage.chunk_notice._usd_write_active = True
                try:
                    # Bookkeeping like any other chunk-prim attr -- update in place if it
                    # already has a spec, else resolve fresh via the group's current Edit
                    # Target, rather than relying on the stage's ambient one (see
                    # set_chunk_lofted for the same "second mutation doesn't propagate"
                    # class of bug this avoids).
                    active_view_attr = gp.GetAttribute(chunk_graph.ACTIVE_VIEW_ATTR)
                    home = (
                        chunk_utils.get_attr_home_layer(active_view_attr)
                        if active_view_attr and active_view_attr.IsValid() else None
                    )
                    target = home or chunk_utils.get_bookkeeping_layer(stage, context)
                    with Usd.EditContext(stage, target):
                        gp.CreateAttribute(chunk_graph.ACTIVE_VIEW_ATTR, Sdf.ValueTypeNames.Int, custom=True).Set(int(slot))
                finally:
                    chunk_stage.chunk_notice._usd_write_active = prev_w
        if view_chunk is not None and select_view:
            for c in registry.scene.get_chunks():
                if c.has_state(ChunkFlags.State.SELECTED):
                    c.set_selected(False)
            view_chunk.set_selected(True)
        chunk_stage.recook_focus(stage, registry.scene, context, view_chunk)

    @staticmethod
    def _could_affect_view_cook(changed_chunks, stage, scene, view_chunk) -> bool:
        """True if any of changed_chunks could plausibly change what view_chunk's recook
        computes -- i.e. one of them IS the view, or is already reachable from it. Cook-set
        membership is exactly graph reachability from the view (view_cook_list), so
        a chunk that was never part of that reachable set can't affect it: a brand-new
        disconnected node can't be reachable (nothing points to/from it yet), and
        deleting/disconnecting something that was never reachable can't remove anything from
        the set either. No active view means nothing is cooking regardless of graph structure,
        so it can't matter there either.
        """
        if view_chunk is None:
            return False
        cook_set = set(chunk_stage.view_cook_list(view_chunk, stage, scene))
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
        prefixes = tuple(str(chunk_stage.get_chunk_prim_path(c)) + "/" for c in cook_set)
        if not prefixes:
            return False
        return any(str(chunk_stage.get_chunk_prim_path(c)).startswith(prefixes) for c in changed_chunks)

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
                chunk_stage.find_view_chunk_for_slot(slot, stage, registry.scene, context) if slot else None
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

    def center_position(self) -> tuple[float, float]:
        """The USD node position that puts a default-size node in the middle of the view."""
        center = self.mapToScene(self.viewport().rect().center())
        w, h = chunk_graph.DEFAULT_NODE_SIZE
        # Node positions are the top-left corner, and USD is Y-up where the canvas is Y-down.
        return (center.x() - w / 2, -(center.y() - h / 2))

    def _graph_context(self) -> core.ChunkBase | None:
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
    # Search

    def _open_search(self) -> GraphSearchPopup:
        """Ctrl+F: the node search popup, along the top of the view."""
        self._search_popup = GraphSearchPopup(self)
        self._search_popup.popup()
        return self._search_popup

    #: What the search matches, (names, types): the popup's boxes, kept between searches.
    _search_scope = (True, True)

    def _search_nodes(self, query: str, names: bool = True, types: bool = True) -> list[str]:
        """The nodes in this graph matching query, best first (chunk_graph.search_nodes)."""
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene):
            return []
        prims = {path: node.prim for path, node in graph_scene._nodes.items()}
        return chunk_graph.search_nodes(prims, query, names, types)

    def _focus_node(self, path: str) -> None:
        """Select that node alone and frame it."""
        frontend = self.frontend()
        frontend.set_selection([path])
        frontend.frame([path])

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
        self.create_chunk_from_entry(entry, scene_pos)

    def create_chunk_from_entry(self, entry, scene_pos: QtCore.QPointF):
        """Create one MenuEntry's chunk(s) at scene_pos, with no UI of its own.

        Split out of _create_chunk_at for the frontend contract: choosing the entry is the
        frontend's business (a Qt menu here, a dialog in Flet), creating it is not.
        """
        chunks = entry.create()

        scene = _chunk.get_scene()
        if not scene:
            return

        context = self._graph_context()
        view_exists = self._context_has_view(scene, context)
        # Read before the add, which can change the selection.
        tail = self._chain_tail() if self._auto_connect_box.isChecked() else None
        with chunk_stage.creating_at((scene_pos.x(), -scene_pos.y()), loft=False), \
                scene.creating_in(self._nest_target(context)):
            for c in chunks:
                if view_exists and not chunk_stage.is_view_chunk(c) and not chunk_stage.is_group_chunk(c):
                    c.set_blocked(True)
                scene.add_chunk(c)

        menu_view = next((c for c in chunks if chunk_stage.is_view_chunk(c)), None)
        if menu_view is not None:
            self._activate_menu_view(menu_view, context, scene.get_stage())
            return

        new_chunk = next((c for c in chunks if not chunk_stage.is_terminal_chunk(c)), None)
        if self._auto_connect_box.isChecked() and new_chunk is not None:
            new_path = chunk_stage.get_chunk_prim_path(new_chunk)
            # Deferred like _create_and_connect_from_menu: the new node's ports come with
            # the rebuild the add queued.
            QtCore.QTimer.singleShot(
                80, self, lambda: self._chain_after(tail, new_path, context))
            return

        QtCore.QTimer.singleShot(60, self, lambda ctx=context: self._ensure_default_view(ctx))

    def _chain_tail(self) -> str | None:
        """The node a new chunk goes after when Auto-connect is on, as a prim path.

        The one selected node; with none or several, the one the active view or Output
        shows. None in an empty graph, or with nothing shown yet.
        """
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene):
            return None
        chain = {path: node for path, node in graph_scene._nodes.items()
                 if not (node._is_view or node._is_output)}
        picked = [path for path, node in chain.items() if node.chunk.has_state(ChunkFlags.State.SELECTED)]
        if len(picked) == 1:
            return picked[0]
        for edge in graph_scene._edges:
            if edge.dst_port.port_type != _PORT_EXEC:
                continue
            shown_by = graph_scene._nodes.get(_port_node_path(edge.dst_port))
            if shown_by is not None and (shown_by._is_active_view() or shown_by._is_active_output()):
                source = _port_node_path(edge.src_port)
                if source in chain:
                    return source
        return None

    def _chain_after(self, tail_path: str | None, new_path: str, context) -> None:
        """Wire the new node after tail_path, move what showed the tail onto it, and select it.

        Selected, the next new chunk goes after it in turn. Without a tail the new node
        starts the chain, and the graph gives it an Output as for any first chunk.
        """
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene):
            return
        new_node = graph_scene._nodes.get(new_path)
        tail = graph_scene._nodes.get(tail_path) if tail_path else None
        if new_node is not None and tail is not None:
            tail_out = _exec_port(tail, is_input=False)
            new_in = _exec_port(new_node, is_input=True)
            new_out = _exec_port(new_node, is_input=False)
            # The views and Outputs the tail fed, to hand on to the new end.
            shown = [
                edge.dst_port for edge in graph_scene._edges
                if edge.src_port is tail_out
                and (dst := graph_scene._nodes.get(_port_node_path(edge.dst_port))) is not None
                and (dst._is_view or dst._is_output)
            ]
            if tail_out is not None and new_in is not None and self._can_connect(tail_out, new_in):
                self._author_connection(tail_out, new_in)
                for port in shown:
                    if new_out is not None and self._can_connect(new_out, port):
                        self._author_connection(new_out, port)
        if new_node is not None:
            scene = _chunk.get_scene()
            if scene is not None:
                scene.clear_chunk_selection()
            new_node.chunk.set_selected(True)
        self._ensure_default_view(context)

    # ------------------------------------------------------------------
    # Drag-and-drop file import

    def _droppable_paths(self, event) -> list[str]:
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
        if os.path.splitext(path)[1].lower() == _SCRIPT_EXT:
            self._import_as_script(path, scene_pos)
            return
        kind = _detect_dropped_usd_kind(path)
        if kind is None:
            return
        # "Import as Chunks" only makes sense for a file that actually carries chunk prims
        # (a chunkgroup export) -- offering it for a plain scene would be a dead choice.
        # Sublayer and Reference apply either way, so the popup always shows at least those
        # two. A plain scene file does not silently default to Sublayer: that would make
        # Reference unreachable for the single most common drop (a plain USD asset with no
        # chunk prims).
        chosen = self._prompt_import_kind(global_pos, offer_chunks=(kind != "scene"))
        if chosen == "chunks":
            self._import_as_chunks(path, scene_pos)
        elif chosen == "scene":
            self._import_as_sublayer(path, scene_pos)
        elif chosen == "reference":
            self._import_as_reference(path, scene_pos)

    def _prompt_import_kind(self, global_pos: QtCore.QPoint, offer_chunks: bool) -> str | None:
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
        materialize them at the drop point, same as pasting a copied selection.
        """
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

    def _ensure_view_for_imported(self, imported_chunks: list[core.ChunkBase]) -> None:
        """Point a fresh Output at the tail of JUST the imported chunks -- not left to
        build()'s generic _ensure_default_view, which picks tails[0] among EVERY disconnected
        exec tail in the whole context, in whatever order scene.get_chunks() happens to yield.
        A dropped-in chunkgroup usually lands as its own chain alongside whatever was already
        in the tab (e.g. a pre-existing Edit Target chunk with nothing wired after it -- also a
        valid 'tail'), so without scoping to just the newly imported nodes, the generic
        auto-view could easily latch onto that unrelated pre-existing node instead of the
        workflow the user just dropped in. No-ops if the context already has a
        view -- matches _ensure_default_view's own rule of never stealing an existing view.

        Always releases the _suppress_default_view hold _import_as_chunks took, whichever
        path out of here is taken -- otherwise a failure partway through would leave the
        generic auto-view permanently disabled for this view for the rest of the session.
        """
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
                if prim.GetTypeName() != _CHUNK_PRIM_TYPENAME:
                    continue
                exec_in = prim.GetAttribute(_EXEC_INPUT)
                if exec_in.IsValid():
                    for conn in exec_in.GetConnections():
                        consumed.add(str(conn.GetPrimPath()))
            tails = [
                c for c in imported_chunks
                if not chunk_stage.is_view_chunk(c) and chunk_stage.get_chunk_prim_path(c) not in consumed
            ]
            if not tails:
                return

            def _x(c: core.ChunkBase) -> float:
                node = graph_scene._nodes.get(chunk_stage.get_chunk_prim_path(c))
                return node.pos().x() if node is not None else float("-inf")

            # Among several disconnected tails within the imported set itself (a branching
            # workflow), prefer the rightmost -- auto-layout arranges topological depth into
            # columns left-to-right, so "rightmost" is this app's own convention for "most
            # downstream", the closest cheap proxy for "the last node" without a full re-walk
            # of the exec graph's depth.
            target = max(tails, key=_x)
            self._create_output_node_for_prim(chunk_stage.get_chunk_prim_path(target))
        finally:
            self._suppress_default_view = False

    def _import_as_sublayer(self, path: str, scene_pos: QtCore.QPointF) -> None:
        """Drop-a-scene-file: create a Sublayer chunk node at the drop point with its path
        pre-filled. Values are set AFTER add_chunk (unlike the paste path) because Sublayer's
        own callback (chunks/usd/sublayer.py) only does real work -- authoring subLayerPaths onto
        the chunk's edit layer -- once the chunk has a live prim/edit_context, exactly matching
        how a hand-created Sublayer chunk picks up a path typed into its UI after creation.

        Blocked before the path is set (_block_if_unreached), and not cooked until wired.
        Blocking suppresses ChunkNoticePropagator.data_changed/send_self (chunk_notice.py
        checks is_blocked() up the whole owner chain), so Sublayer's own add_callback
        handler does not author subLayerPaths now. That once left the dropped path sitting in
        the field with no effect (an "empty sublayer"), when nothing cooked a blocked chunk
        later. It does now: the chunk stays stale, and once wired the recook runs its
        execute(), which applies the same paths. Writing them now instead brought the whole
        file into the viewport only for the recook to hide it again.

        The recook below still runs when a view exists: it is what mutes the unwired node's
        layer, so nothing it holds renders (live-confirmed without it: a dropped,
        deliberately-unconnected Sublayer's content still rendered in the viewport).
        """
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
        with chunk_stage.creating_at((scene_pos.x(), -scene_pos.y()), loft=False), \
                scene.creating_in(self._nest_target(context)):
            for c in chunks:
                scene.add_chunk(c)
        # Wired to nothing, so blocked before the path is set: its callback does not sublayer
        # the file in only for the recook below to hide it -- the viewport loading the whole
        # file to throw it away. Nothing is lost: execute() applies the same paths, and the
        # chunk stays stale until it is wired in and cooks.
        self._block_if_unreached(primary)
        list_group = primary.get_param("imports", _chunk.is_inst(_chunk.ListGroup))
        if list_group is not None:
            list_group.size = 1
            if row := list_group.get_indexed_param("sublayer_path", 0):
                # Visibly: blocked, a plain set stores the path but the node's field stays
                # empty -- the same reason the Reference drop sets its file_path this way.
                chunk_graph.set_param_visibly(primary, row, path)

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
        would render in the viewport anyway, same bug as the equivalent Sublayer case.
        """
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
        with chunk_stage.creating_at((scene_pos.x(), -scene_pos.y()), loft=False), \
                scene.creating_in(self._nest_target(context)):
            for c in chunks:
                scene.add_chunk(c)
        # Wired to nothing, so blocked: only what the view or Output reaches cooks, and cooked
        # now a Reference composed its whole asset in for the viewport to load, only to be
        # hidden again. set_param_visibly still runs its callbacks -- the location is suggested
        # from the file -- without cooking it; it stays stale and cooks once wired in.
        self._block_if_unreached(primary)
        if file_param := primary.get_param("file_path"):
            chunk_graph.set_param_visibly(primary, file_param, path)

        if view_exists:
            self._recook_active_view()

    def _block_if_unreached(self, c: core.ChunkBase) -> None:
        """Block a dropped chunk its graph's view or Output does not reach -- one wired to nothing.

        Explicitly, before its file is set: a frontend's recook blocks it too, but only after,
        and the set would have cooked it by then. A graph that nothing drives cooks
        everything, so it is left alone there (chunk_stage.reached_by_graph_driver).
        """
        scene = _chunk.get_scene()
        stage = scene.get_stage() if scene else None
        if stage is not None and not chunk_stage.reached_by_graph_driver(c, stage, scene):
            c.set_blocked(True)

    def _import_as_script(self, path: str, scene_pos: QtCore.QPointF) -> None:
        """Drop-a-.py-file: a script_runner node at the drop point, running that file.

        Same shape as _import_as_reference. The script only runs once the node is wired
        into the active view, like any other freshly created chunk, and from then on every
        save of the file re-runs it (script_runner watches the file itself).
        """
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene) or not graph_scene._scene_sender:
            return
        scene = _chunk.get_scene()
        if not scene:
            return
        primary, chunks = self._instantiate_chunk_of_type("script_runner")
        if primary is None:
            return
        context = self._graph_context()
        view_exists = self._context_has_view(scene, context)
        with chunk_stage.creating_at((scene_pos.x(), -scene_pos.y()), loft=False), \
                scene.creating_in(self._nest_target(context)):
            for c in chunks:
                scene.add_chunk(c)
        # As in _import_as_reference: blocked until wired, its callbacks still run.
        self._block_if_unreached(primary)
        if file_param := primary.get_param("script_file"):
            chunk_graph.set_param_visibly(primary, file_param, path)

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
                            # system rather than calling the method directly --
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
        # but only when there's actually something to paste -- otherwise it goes straight
        # into the fuzzy search, with no extra menu step.
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


# The shared right-click "remove" menu action lives in widgets, which cannot import this
# module; it announces deletes through this hook instead.
pre_delete_hooks.append(ChunkGraphView.mark_pending_delete_relevance)


class QtGraphFrontend:
    """chunk_graph.GraphFrontend, for Qt.

    An adapter rather than a base class, for two reasons.

    ABCMeta cannot be mixed into a PySide6 class -- the metaclasses conflict -- and the
    combined-metaclass workaround constructs but silently skips the abstract check, so
    inheritance would give the appearance of a contract without the substance.

    And the Qt graph is not one object. It is a ChunkGraphScene, a ChunkGraphView and the
    ChunkWidgetDocker that owns navigation between context tabs; Flet is a single
    GraphView. A contract phrased as "subclass this" would have had to pick one of those
    shapes and force the other to match. Delegation lets each keep its own.

    Two verbs are genuinely structural rather than cosmetic differences:

    dive_into is a Qt Signal, not a call -- the scene emits, the docker opens a tab for the
    group. So diving here means emitting, and the navigation happens elsewhere.

    dive_out has no counterpart at all: Qt navigates by tab, and "out" is the docker's
    back step through its nav history. Without a docker this adapter cannot do it, and
    says so rather than pretending.
    """

    def __init__(self, view: ChunkGraphView, docker=None):
        #: The ChunkGraphView. Its scene and scene_sender are reached through it, so the
        #: adapter holds one reference rather than three that could drift apart.
        self.view = view
        #: ChunkWidgetDocker, which owns tab navigation. Optional: everything except
        #: dive_out works without it.
        self.docker = docker

    # -- plumbing ----------------------------------------------------------

    def _scene(self):
        scene = self.view.scene()
        return scene if isinstance(scene, ChunkGraphScene) else None

    def _node(self, path: str):
        scene = self._scene()
        return scene._nodes.get(path) if scene is not None else None

    def _chunk(self, path: str):
        node = self._node(path)
        return node.chunk if node is not None else None

    @property
    def context(self) -> str:
        """The prim path of the graph being shown."""
        context = self.view._graph_context()
        return chunk_stage.get_chunk_prim_path(context) if context is not None else ""

    @property
    def selected_nodes(self) -> set:
        scene = self._scene()
        if scene is None:
            return set()
        return {
            path for path, node in scene._nodes.items()
            if node.chunk.has_state(ChunkFlags.State.SELECTED)
        }

    # -- selection ---------------------------------------------------------

    def set_selection(self, paths, add: bool = False) -> None:
        scene = self._scene()
        if scene is None or scene._scene_sender is None:
            return
        chunks = [c for c in (self._chunk(p) for p in paths) if c is not None]
        if add:
            # Dedupe on identity: ChunkBase does not promise __hash__/__eq__ semantics
            # that a set would use correctly here.
            merged = {id(c): c for c in self.selected_chunks()}
            merged.update({id(c): c for c in chunks})
            chunks = list(merged.values())
        scene._scene_sender.set_selected_chunks(chunks)

    def selected_chunks(self) -> list:
        return self.view.selected_chunks()

    # -- editing -----------------------------------------------------------

    def delete_selected(self) -> None:
        self.view._delete_selected()

    def toggle_disable_selected(self) -> None:
        self.view._toggle_disable_selected()

    def collapse_selected_or_all(self) -> None:
        self.view._collapse_selected_or_all()

    def copy_selection(self) -> None:
        self.view._copy_selected_to_clipboard()

    def cut_selection(self) -> None:
        self.view._cut_selected_to_clipboard()

    def paste_at(self, x: float, y: float, nodes=None, edges=None) -> None:
        position = QtCore.QPointF(x, y)
        if nodes is None and edges is None:
            self.view._paste_clipboard_at(position)
            return
        self.view._materialize_at(list(nodes or []), list(edges or []), position)

    def duplicate_selection(self) -> None:
        self.view._duplicate_selected()

    def group_selection(self) -> None:
        self.view._group_selected()

    def create_chunk(self, entry, x: float, y: float) -> None:
        self.view.create_chunk_from_entry(entry, QtCore.QPointF(x, y))

    # -- navigation --------------------------------------------------------

    def can_dive(self, path: str) -> bool:
        chunk = self._chunk(path)
        return chunk is not None and chunk_stage.is_subnet(chunk)

    def dive_into(self, path: str) -> None:
        scene = self._scene()
        chunk = self._chunk(path)
        if scene is None or chunk is None or not self.can_dive(path):
            return
        scene.dive_into.emit(chunk)

    def dive_out(self) -> None:
        """Step back through the docker's navigation history.

        Qt has no "leave this group" operation: it dives by opening a tab, so coming out
        is a back step. With no docker there is nothing to step through.
        """
        if self.docker is None:
            return
        self.docker._nav_step(-1)

    # -- views -------------------------------------------------------------

    def active_slot(self) -> int:
        return int(self.view._view_state.active_slot)

    def set_active_slot(self, slot: int) -> None:
        self.view._view_state.set_slot(int(slot))

    # -- viewport ----------------------------------------------------------

    def frame(self, paths) -> None:
        """Fit those nodes.

        Qt's own _frame_nodes always frames the selection-or-everything, so it cannot
        honour an explicit list; this does the same union-and-fit against the nodes asked
        for, and falls back to _frame_nodes when none of them are on screen.
        """
        items = [n for n in (self._node(p) for p in paths) if n is not None]
        if not items:
            self.view._frame_nodes()
            return
        rect = QtCore.QRectF()
        for item in items:
            rect = rect.united(item.sceneBoundingRect())
        rect.adjust(-60, -60, 60, 60)
        self.view.fitInView(rect, QtCore.Qt.KeepAspectRatio)
        self.view._apply_lod()
