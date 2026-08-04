"""
Node graph view for Chunk prims.

Reads layout from UsdUINodeGraphNodeAPI; writes position back on drag.
Selection syncs with ChunkWidget via STATE_CHANGED notice.
Edges follow inputs:chunk connections between prims.
"""

from __future__ import annotations

import re
import time
import weakref
from typing import Iterable, Optional

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
from usd_chunks.plugins.chunk_qt_widgets import ContainerWidget, ChunkWidget, FilterableChunkMenu, LabeledWidget, CHUNK_STYLESHEET, ViewSlotState

_PORT_RADIUS = 5
_NODE_WIDTH = 220
_NODE_HEADER_H = 40
_DOT_R = 10  # dot node circle radius

# Shake-to-disconnect tuning: a fast back-and-forth drag disconnects the node.
_SHAKE_WINDOW = 0.4  # seconds of drag samples to look at
_SHAKE_MIN_STEP = 5.0  # scene px; smaller moves are jitter, ignored
_SHAKE_MIN_TRAVEL = 140.0  # total scene px travelled in the window
_SHAKE_REVERSALS = 4  # horizontal direction changes needed to fire


_COL_NODE_BG = QtGui.QColor("#3a3a3a")
_COL_NODE_HEADER = QtGui.QColor("#2a2a2a")
_COL_NODE_BORDER = QtGui.QColor("#555555")
_COL_NODE_SELECTED = QtGui.QColor("#cab46a")  # matches ChunkWidget.SELECTED_COLOR
_COL_GROUP_HEADER = QtGui.QColor("#1a1a1a")   # near-black — group/context nodes
_COL_LOCKED_HEADER = QtGui.QColor("#3a3020")  # dark amber — locked (external-layer) nodes
_COL_LOCKED_BG = QtGui.QColor("#2e2e28")
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
    __slots__ = ("module_name", "values", "rel_pos", "children", "child_edges")

    def __init__(
        self, module_name: str, values: list, rel_pos: QtCore.QPointF,
        children: "list[_ClipboardNode] | None" = None,
        child_edges: "list[_ClipboardEdge] | None" = None,
    ):
        self.module_name = module_name
        self.values = values
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
            if attr_name == _EXEC_INPUT:
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
    clip_node = _ClipboardNode(module_name, _snapshot_param_values(chunk), rel_pos)
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
    is_exec = dst_attr_suffix == _EXEC_INPUT
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
        self._is_group = chunk.has_flag(ChunkFlags.GroupFlags.CONTEXT)
        self._ports: list[ChunkPortItem] = []
        self._typed_ports: list[ChunkPortItem] = []
        self._selected = chunk.has_state(ChunkFlags.State.SELECTED)
        self._stale = chunk.has_state(ChunkFlags.State.STALE)
        self._update_stale_tooltip()
        _chunk_type = prim.GetAttribute("info:chunkType").Get()
        self._is_view = _chunk_type == "view"
        self._is_dot = _chunk_type == "dot"
        self._drag_active = False
        self._drag_moved = False  # True once a drag actually moves — plain clicks stay False
        self._drag_start = QtCore.QPointF()
        self._shake_samples: list[tuple[float, float]] = []  # (time, scene x) during a drag
        self._shake_fired = False
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

        self._build_ports(prim)
        self._apply_usd_pos(prim)
        # Reflect disabled state on (re)build — otherwise a rebuilt disabled node renders at
        # full opacity until the next state change (e.g. selecting it).
        if chunk.has_state(ChunkFlags.State.DISABLED):
            self.setOpacity(0.35)

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

    def create_proxy(self):
        """Create the embedded ChunkWidget proxy. Called in deferred batches after scene build."""
        if self._is_dot:
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
        chunk_widget = ChunkWidget(self.chunk, parent=_get_proxy_sink())
        # setWidget() requires a top-level widget.  Reparent to None first, but set
        # WA_DontShowOnScreen beforehand so the OS never calls ShowWindow during the
        # transition from child → top-level.
        chunk_widget.setAttribute(QtCore.Qt.WA_DontShowOnScreen)
        chunk_widget.setParent(None)
        chunk_widget.setStyleSheet(CHUNK_STYLESHEET)
        chunk_widget.setFixedWidth(_NODE_WIDTH)
        self._chunk_widget = chunk_widget  # pin Python ref; PySide6 may GC despite Qt C++ ownership
        self._proxy = QtWidgets.QGraphicsProxyWidget(self)
        self._proxy.setWidget(chunk_widget)
        self._proxy.setPos(0, _NODE_HEADER_H)

        self._full_height = _NODE_HEADER_H + chunk_widget.sizeHint().height()
        self._height = self._full_height
        self.prepareGeometryChange()

        self._port_rebuild_timer = QtCore.QTimer(chunk_widget)
        self._port_rebuild_timer.setSingleShot(True)
        self._port_rebuild_timer.setInterval(0)
        self._port_rebuild_timer.timeout.connect(self._rebuild_typed_ports)
        # geometryChanged fires for collapse/expand after initial setup.
        # Initial port placement is triggered explicitly below so it doesn't
        # depend on geometryChanged firing at the right time relative to the connection.
        self._proxy.geometryChanged.connect(self._port_rebuild_timer.start)
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
            scene._build_edges()
        # New/removed/recolored port items don't always get their region marked
        # dirty automatically — without this the old pixels can linger on screen
        # until something unrelated (e.g. deselecting the node) forces a repaint.
        self.update()

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

    def _place_param_ports_collapsed(self):
        for param in _iter_connectable_params(self.chunk):
            ptype = _param_port_type(param)
            in_port = ChunkPortItem(f"{self.prim.GetPath()}.inputs:{param.name}", ptype, parent=self)
            in_port.setPos(0, _NODE_HEADER_H // 2)
            self._ports.append(in_port)
            self._typed_ports.append(in_port)
            out_port = ChunkPortItem(f"{self.prim.GetPath()}.outputs:{param.name}", ptype, parent=self)
            out_port.setPos(_NODE_WIDTH, _NODE_HEADER_H // 2)
            self._ports.append(out_port)
            self._typed_ports.append(out_port)
        self._place_prim_output_ports(_NODE_HEADER_H // 2, extend_height=False)

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
        vector_widgets = {
            w.group: w
            for w in chunk_widget.findChildren(ContainerWidget)
            if isinstance(getattr(w, 'group', None), core.ChunkVectorBase)
        }

        for param in _iter_connectable_params(self.chunk):
            if isinstance(param, core.ChunkVectorBase):
                widget = vector_widgets.get(param)
            else:
                widget = param_widgets.get(param)
            if widget is None:
                continue

            ptype = _param_port_type(param)

            if widget.isVisible():
                if isinstance(param, core.ChunkVectorBase):
                    hdr = widget.minimize_button
                    local = hdr.mapTo(chunk_widget, QtCore.QPoint(0, hdr.height() // 2))
                else:
                    local = widget.mapTo(chunk_widget, QtCore.QPoint(0, widget.height() // 2))
            else:
                # Param lives inside a collapsed group — walk up to find the nearest
                # visible ContainerWidget and place the port there instead.
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
        elif self._is_group:
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
        elif self._is_group:
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

        chunk_type = self.prim.GetAttribute("info:chunkType").Get() or ""
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
            if self._is_group:
                # A collapsed group hides its whole embedded widget — including the minimize
                # arrow — so the header double-click is the only way to expand it in the graph.
                # Collapsed -> expand; expanded -> dive in.
                if self.chunk.has_state(ChunkFlags.State.COLLAPSED):
                    self.chunk.unset_state(ChunkFlags.State.COLLAPSED)
                    event.accept()
                    return
                scene = self.scene()
                if scene and hasattr(scene, "dive_into"):
                    scene.dive_into.emit(self.chunk)
                event.accept()
                return
            if not self._locked:
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
            if event.pos().y() <= _NODE_HEADER_H and not self._locked:
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
        self._height = _NODE_HEADER_H if collapsed else self._full_height
        if self._proxy is not None:
            self._proxy.setVisible(not collapsed)
        if collapsed:
            # Move typed ports to header centre so edges still connect to the node.
            for port in self._typed_ports:
                x = 0 if ".inputs:" in port.attr_path else _NODE_WIDTH
                port.setPos(x, _NODE_HEADER_H // 2)
        elif self._port_rebuild_timer is not None:
            # Recompute port positions from widget geometry now that proxy is visible.
            self._port_rebuild_timer.start()
        self.update()
        scene = self.scene()
        if scene:
            scene.update_edges_for(self)

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
            self.setOpacity(0.35 if disabled else 1.0)
        except RuntimeError:
            return

    def _update_stale_tooltip(self) -> None:
        self.setToolTip(
            "Needs recook — inputs changed since this last ran. Press Run/Recompute to update it."
            if self._stale
            else ""
        )


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
        self._stage: Optional[Usd.Stage] = None
        self._scene_sender: Optional[core.SceneSender] = None
        self._nodes: dict[str, ChunkNodeItem] = {}  # prim path → node
        self._edges: list[ChunkEdgeItem] = []
        self._objects_changed_conn: Optional[NoticeConnection] = None
        self._show_param_edges: bool = True
        self._built_context: object = _UNBUILT  # sentinel — tracks what context is currently rendered
        self._dirty: bool = True  # set by invalidate(); cleared by a (re)build

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
        QtCore.QTimer.singleShot(0, self._do_rebuild)

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
                QtCore.QTimer.singleShot(0, self._do_rebuild)
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
            QtCore.QTimer.singleShot(0, lambda: self._create_proxies_batch(rest, gen))

    def _build_edges(self):
        for edge in self._edges:
            self.removeItem(edge)
        self._edges.clear()

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
                is_exec = attr_name == "inputs:chunk"
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
                QtCore.QTimer.singleShot(0, v._run_pending_recook)

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
        for path in notice.GetChangedInfoOnlyPaths():
            if path.IsPropertyPath() and path.name == "ui:nodegraph:node:pos":
                prim_path = str(path.GetPrimPath())
                node = self._nodes.get(prim_path)
                if node:
                    node._apply_usd_pos(node.prim)
        self._build_edges()


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

    def uninstall(self) -> None:
        """Detach from the QApplication.

        An application-level filter outlives the view that owns it: Qt keeps dispatching
        to eventFilter after the view is gone, and once the Python wrapper is torn down
        that raises on the first attribute access. The owning view connects this to its
        destroyed signal so the filter's lifetime matches its own.
        """
        if (app := QtWidgets.QApplication.instance()) is not None:
            app.removeEventFilter(self)

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
        etype = event.type()
        if etype not in (QtCore.QEvent.Type.KeyPress, QtCore.QEvent.Type.ShortcutOverride):
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

        # Ctrl+C / Ctrl+V / Ctrl+D / Ctrl+X: same fallback pattern as F/L.
        ctrl_down = bool(event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier)
        _CVDX_KEYS = (QtCore.Qt.Key.Key_C, QtCore.Qt.Key.Key_V, QtCore.Qt.Key.Key_D, QtCore.Qt.Key.Key_X)
        if self._active is None and ctrl_down and event.key() in _CVDX_KEYS:
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
                    else:
                        view._cut_selected_to_clipboard()
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
    _GRID_DOT_COLOR = QtGui.QColor("#3d4250")  # muted grey-blue, subtle against #252525
    _GRID_DOT_WIDTH = 2

    dive_into = QtCore.Signal(object)  # re-emitted from scene when a group node is double-clicked

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(ChunkGraphScene())
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
        QtWidgets.QApplication.instance().installEventFilter(self._proxy_key_router)
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

    def drawBackground(self, painter: QtGui.QPainter, rect: QtCore.QRectF) -> None:
        # super() fills with setBackgroundBrush's flat color; dots paint on top of that.
        super().drawBackground(painter, rect)
        # Only the exposed rect is ever painted here (never the full 100k-unit scene extent —
        # Qt only calls drawBackground for the currently visible region).
        points = self._grid_points_in_rect(rect, self._GRID_SPACING)
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
        QtCore.QTimer.singleShot(80, lambda ctx=context: self._ensure_default_view(ctx))
        # Switching to this context must re-establish the viewport for ITS active view — the
        # viewport is one global stage, so a view activated in another context (e.g. an inner
        # group view) otherwise stays cooked and the wrong result shows. Deferred past the
        # auto-view step. (On create we deliberately don't re-cook, to avoid churn.)
        QtCore.QTimer.singleShot(100, lambda cc=changed_chunks: self._restore_active_view(cc))

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
                _apply_param_values(primary, item.values)
                pos = (scene_pos.x() + item.rel_pos.x(), scene_pos.y() + item.rel_pos.y())
                # USD is Y-up, Qt is Y-down -- _create_node_pos is read by chunk_usd in USD
                # space (see chunk_usd._create_node_pos usage elsewhere in this file).
                chunk_usd._create_node_pos = (pos[0], -pos[1])
                try:
                    for c in chunks:
                        if view_exists and not chunk_usd.is_view_chunk(c) and not chunk_usd.is_group_chunk(c):
                            c.set_blocked(True)
                        scene.add_chunk(c)
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
            QtCore.QTimer.singleShot(
                80, lambda paths=new_path_by_index, edges=list(clip_edges): self._wire_pasted_edges(paths, edges)
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
                # Apply values before add_chunk -- see the matching comment in
                # _materialize_at for why (a widget can be built synchronously as a side
                # effect of add_chunk's own notice dispatch, and reads its initial value
                # once, at construction).
                _apply_param_values(primary, item.values)
                # Children of a group aren't shown in any currently-open graph view (nobody
                # has necessarily dived into the new group), so there's no view-driven
                # blocking to apply here -- diving in later triggers the usual
                # _ensure_default_view the same way it would for a hand-built group.
                chunk_usd._create_node_pos = (item.rel_pos.x(), -item.rel_pos.y())
                try:
                    for c in chunks:
                        scene.add_chunk(c)
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

    def _duplicate_selected(self) -> None:
        """Ctrl+D: copy+paste the current selection in one step, without touching the
        Ctrl+C/Ctrl+V clipboard (so it never clobbers whatever was last explicitly copied).
        Lands at a fixed +40/+40 offset from the originals rather than the mouse cursor."""
        clip_nodes, clip_edges, top_left = self._snapshot_selection()
        if not clip_nodes or top_left is None:
            return
        anchor = QtCore.QPointF(top_left.x() + 40, top_left.y() + 40)
        self._materialize_at(clip_nodes, clip_edges, anchor)

    def _wire_pasted_edges(self, new_path_by_index: "dict[int, str]", edges: "list[_ClipboardEdge]") -> None:
        graph_scene = self.scene()
        if not isinstance(graph_scene, ChunkGraphScene):
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

    def _ensure_default_view(self, context) -> None:
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

    # ------------------------------------------------------------------
    # Pan — middle mouse or Space + left drag

    def _start_pan(self, pos: QtCore.QPoint):
        self._pan_active = True
        self._pan_start = pos
        self.setCursor(QtCore.Qt.ClosedHandCursor)

    def _stop_pan(self):
        self._pan_active = False
        self.setCursor(QtCore.Qt.ArrowCursor)

    def mousePressEvent(self, event: QtGui.QMouseEvent):
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

    def _finish_wire_drag(self, scene_pos: QtCore.QPointF) -> None:
        # Use items() instead of itemAt() so the rubber-band edge (Z=10) doesn't
        # occlude the port underneath it.
        hit = next(
            (item for item in self.scene().items(scene_pos) if isinstance(item, ChunkPortItem)),
            None,
        )
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
        mod = FilterableChunkMenu().select_module(global_pos)
        if not mod:
            return
        chunks = mod.create() if mod.loaded else mod.load()
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
            80, lambda: self._connect_ports_by_path(src_attr_path, src_is_input, new_prim)
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
            # Find any successor whose inputs:chunk points to this node's outputs:chunk.
            for node in graph_scene._nodes.values():
                succ_attr = graph_scene._stage.GetAttributeAtPath(
                    Sdf.Path(str(node.prim.GetPath()) + ".inputs:chunk")
                )
                if succ_attr and succ_attr.IsValid():
                    if any(str(c) == out_attr_path for c in succ_attr.GetConnections()):
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

        # Heal the exec chain: rewire each successor's inputs:chunk to this node's predecessor(s).
        exec_in = stage.GetAttributeAtPath(Sdf.Path(node_prim + ".inputs:chunk"))
        pred_conns = list(exec_in.GetConnections()) if (exec_in and exec_in.IsValid()) else []
        out_chunk_path = node_prim + ".outputs:chunk"
        for other in graph_scene._nodes.values():
            if other is node:
                continue
            succ = stage.GetAttributeAtPath(Sdf.Path(str(other.prim.GetPath()) + ".inputs:chunk"))
            if not (succ and succ.IsValid()):
                continue
            if any(str(c) == out_chunk_path for c in succ.GetConnections()):
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
        QtCore.QTimer.singleShot(0, lambda s=slot: self._view_state.set_slot(s))

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

        QtCore.QTimer.singleShot(0, lambda: self._view_state.set_slot(slot))

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
        return any(c is view_chunk or c in cook_set for c in changed_chunks)

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

        mod = FilterableChunkMenu().select_module(event.globalPos())
        if not mod:
            return

        chunks = mod.create() if mod.loaded else mod.load()

        scene = _chunk.get_scene()
        if not scene:
            return

        context = self._graph_context()
        # View-driven: if this context already has a view, a new node starts blocked so it does
        # NOT execute on creation (its params resolve during add_chunk and would otherwise cascade
        # into the module, and execute-on-creation would run too). It cooks only once wired into
        # the active view's tree, where recook unblocks it. Must be set before add_chunk.
        view_exists = self._context_has_view(scene, context)
        # Create the node(s) at the right-click point instead of the far-away index default.
        chunk_usd._create_node_pos = (scene_pos.x(), -scene_pos.y())
        # Explicitly flag "not pinned" (rather than leaving the flag at its idle None) — the
        # Params-page widget's own PARAM_ADDED listener can race ahead of ChunkStageSync's and
        # ask is_chunk_pinned() before _define_prim authors the attribute; an explicit False
        # here is what lets it resolve correctly instead of falling back to the legacy default.
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

        # A view created straight from the chunk menu needs a slot + activation to work (the
        # "Set View" path does this). Set it up so it's a usable, active view (unwired until the
        # user connects it), and skip the default-view step below.
        menu_view = next((c for c in chunks if chunk_usd.is_view_chunk(c)), None)
        if menu_view is not None:
            self._activate_menu_view(menu_view, context, scene.get_stage())
            event.accept()
            return

        # If this context has no view (e.g. it was just cleared, or nodes were made in the
        # params panel), make a default one wired to the exec tail — so view-driven execution
        # resumes without needing to re-open the graph. Deferred past the create's own rebuild
        # so it runs on a settled graph (re-cook mid-create corrupts nodes). Wiring to the tail
        # (not the new node) means it cooks the whole chain and can't blank a loaded scene.
        # No-ops when a view already exists.
        QtCore.QTimer.singleShot(60, lambda ctx=context: self._ensure_default_view(ctx))


