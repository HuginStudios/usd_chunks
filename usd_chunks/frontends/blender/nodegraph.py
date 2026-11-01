"""The Blender graph frontend -- a chunk network as a native `bpy.types.NodeTree`.

Where the Qt frontend builds a `QGraphicsScene` and the Flet one paints a canvas, this one
**draws nothing**. Blender has a single node editor that renders whatever tree type is
active, so registering a tree type gets pan, zoom, box select, drag-to-link, the Shift+A
search menu, frames, reroutes, mute, duplicate, snapping, the N-panel and theming for free
-- the same editor Geometry Nodes uses, as a sibling tree type rather than nodes inside it
(adding nodes *into* Geometry Nodes is the thing that is genuinely impossible).

That makes this the thinnest of the three frontends, and it is why `drawing and input are
deliberately absent` from `GraphFrontend` matters: Blender supplies both, and only the
sixteen verbs are left.

**The tree is a view; the `.usda` is the model.** This has to be decided on day one and is
decided here: `reload()` rebuilds the tree from USD and nothing is ever read back out of a
saved `.blend`. Blender serialises node trees into the `.blend` file whether or not you
want it to, so a tree treated as authoritative would fork from the USD the moment a file
was saved in both -- two sources of truth that drift silently. `chunk_graph` is the
reconciler; `_NODE_PATH` on each node is the only link, and it holds a prim path.

Between reloads the tree is kept honest from both sides. What Blender does to the tree on
its own -- a wire cut, a node deleted, an undo -- is caught (`UsdChunksNodeTree.update`
diffs links against the model; the add-on shadows the stock node operators inside this
tree type and rebuilds after undo) and written to the model or discarded. What the model
does on its own -- a cook filling an output, a param lofted, a chunk going stale -- arrives
as chunk notices and is mirrored onto the touched nodes one tick later (`flush`), so an
output shows its value without anyone reloading the graph.

**Node classes are generated per chunk type**, from `main()`, because Blender fixes RNA
properties at class registration. Params lofted later (a Wedge's ranges, a Stage
Expression's variables) cannot be RNA and become per-instance ID properties instead -- see
`chunk_blender`, which owns that split.

Requires: bpy (Blender 4.2+), and usd_chunks running under Blender's own interpreter so it
uses Blender's `pxr` rather than a second USD. Blender namespaces its USD build
(`pxrBlender_v26_03__pxrReserved__`), so objects from a pip `usd-core` are a different C++
type and cannot be handed to it -- which is why `usd-core` is an optional extra.
"""
from __future__ import annotations

import logging
import re
import textwrap
import time
import uuid

import bpy
from usd_chunks import chunk_core as core
from usd_chunks import chunk_registry
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_notice import ChunkNoticeAlias, NoticeConnection
from usd_chunks.frontends.blender import factory as chunk_blender
from usd_chunks.graph import engine as chunk_graph
from usd_chunks import chunk_stage
from usd_chunks import chunk_utils

logger = logging.getLogger(__name__)

TREE_ID = "UsdChunksNodeTree"
SOCKET_ID = "UsdChunksSocket"

#: Where a node records which chunk prim it mirrors. A name rather than a pointer: Blender
#: keeps nodes across a reload while the chunk objects are rebuilt, so anything holding a
#: live reference would be pointing at a dead chunk by the time it was read.
_NODE_PATH = "chunk_path"

#: Blender's node editor is Y-up like USD, unlike the Qt and Flet canvases which are Y-down
#: and negate. So positions pass through unflipped -- and must, or a graph laid out in
#: Blender opens mirrored in the other two.
#:
#: They do pass through SCALED. A position in USD is a Qt scene pixel, where a node is 220
#: wide and its body is the compact ChunkWidget; a Blender node is NODE_WIDTH wide and
#: gives every socket its own row, so it is bigger in both directions (measured on a
#: Transform: 260 x 671 here against roughly 220 x 330 in Qt). A chain Qt laid out at 260
#: per column opened here with nodes touching edge to edge. 1.25 opens that to a 65px gap
#: at the same layout; it divides back out on write (pull_positions_from_tree, the Add
#: cursor), so a graph arranged here lands in Qt at Qt's own spacing.
_SCALE = 1.25

#: Port colours by the engine's port-type vocabulary. Blender wants RGBA 0-1.
_PORT_COLOURS = {
    "exec": (0.85, 0.85, 0.85, 1.0),
    "float": (0.60, 0.60, 0.60, 1.0),
    "int": (0.40, 0.80, 0.40, 1.0),
    "bool": (0.80, 0.50, 0.80, 1.0),
    "string": (0.40, 0.70, 1.00, 1.0),
    "color": (0.95, 0.80, 0.30, 1.0),
}
_DEFAULT_COLOUR = (0.7, 0.7, 0.7, 1.0)

#: The name (and port type) of a collapsed node's "+" sockets, one on each side: a link
#: made to one is taken back out and becomes a pick of which param it means -- see
#: UsdChunksNodeTree.update and BlenderGraphView.join. Not a param name, so nothing
#: resolves it as a port.
_PLUS = "+"

#: Header colour every chunk node gets. Without it Blender falls back to its default
#: colour for a custom Node subclass with no bl_color/use_custom_color, which reads as an
#: unclassified pink -- distracting across a graph that is entirely chunk nodes.
_NODE_HEADER_COLOR = (0.15, 0.15, 0.15)

#: The other header colours, matching what Qt paints so a graph reads the same in both:
#: a chunkgroup is near-black, the ACTIVE view is warm orange and an inactive one dim
#: grey-brown, a locked node (authored in a layer that is not the edit target) is dark
#: amber, and a stale one -- inputs changed since it last cooked -- is bright amber. Only
#: `node.color` is available to a custom node, so the header carries all of it: Blender
#: offers no border, badge or opacity for a Python node.
_GROUP_HEADER_COLOR = (0.08, 0.08, 0.08)
_VIEW_ACTIVE_COLOR = (0.78, 0.47, 0.19)
_VIEW_INACTIVE_COLOR = (0.29, 0.27, 0.24)
_LOCKED_HEADER_COLOR = (0.23, 0.19, 0.13)
_STALE_HEADER_COLOR = (0.88, 0.56, 0.16)
_FAILED_HEADER_COLOR = (0.62, 0.17, 0.17)

#: Default node width. Wider than Blender's 140 because an input socket row here draws the
#: param's label and its control, not just a socket name.
NODE_WIDTH = 260.0


def tag_redraw() -> None:
    """Ask every node editor to repaint.

    Blender's own node operators post an NC_NODE notifier from C, and that notifier is what
    makes the editor rebuild its cached node layout. A Python operator posts nothing, so a
    tree rewritten from `reload()` kept drawing the layout it had -- a chunk created with
    Shift+A existed in USD and in `tree.nodes` and still did not appear until a pan or a
    click happened to force a repaint.

    Every node editor, not the active one: `dive_into` and a rebuild after a cook change
    the same tree in whatever areas are showing it.
    """
    windows = getattr(getattr(bpy.context, "window_manager", None), "windows", None) or ()
    for window in windows:
        for area in window.screen.areas:
            if area.type == "NODE_EDITOR":
                area.tag_redraw()
                # Also each region: area.tag_redraw() alone was not always enough to
                # refresh a tree rebuilt from a bpy.app.timers callback rather than an
                # operator (see _poll_new_trees in the Blender add-on) -- an operator's own
                # redraw pump seems to cover the area/region gap that a timer's does not.
                for region in area.regions:
                    region.tag_redraw()


def _port_name(attr_path: str) -> str:
    """The socket name for a full `/prim.inputs:name` attribute path.

    Must agree with `PortRef.name`, which is the last colon-separated segment -- so
    `inputs:chunk` and `outputs:chunk` are both the socket `chunk`, told apart by which
    collection they live in. Splitting on "." instead keeps the `inputs:` prefix and the
    edge lookup then matches nothing, which loses every wire silently.
    """
    return attr_path.rsplit(":", 1)[-1]


# ---------------------------------------------------------------------------
# Registered Blender types
# ---------------------------------------------------------------------------

class UsdChunksNodeTree(bpy.types.NodeTree):
    """The tree type. Its whole job is to exist, so the node editor has something to draw --
    except for update(), which exists because dragging a wire in Blender's OWN editor is
    otherwise pure cosmetics: Blender creates the bpy.types.NodeLink itself, entirely
    internally, and nothing else here ever hears about it. See update()'s docstring.
    """

    bl_idname = TREE_ID
    bl_label = "USDChunks Editor"
    bl_icon = "NODETREE"

    def update(self) -> None:
        """Blender's own hook for "this tree's topology changed" -- a link or node added
        or removed. Without this, a wire the user just dragged would author nothing: no USD
        .connect, no @Node.attr@ expression text, so the destination param's displayed
        value would never follow the wire it visually has.

        Blender's update() does not say what changed -- and it is not only topology:
        Blender also calls it for every node PROPERTY change, so it runs on each tick of a
        slider drag. So this diffs the tree's links against the model's edges and touches
        only the difference: a link the model lacks is connected, an edge the tree lacks
        is disconnected. Connecting every link on every call, which is what stood here,
        meant chunk_graph.connect() re-authoring each wire and reporting a change on every
        slider tick; the reload that scheduled recreated every node -- which Blender
        creates SELECTED -- half a second into the drag, and took the drag with it.

        The disconnect half is what makes Ctrl+RMB link-cut, and re-dragging a wire from
        one source to another, mean anything: without it the USD connection survived and
        the wire came back on the next reload.

        Removed edges go first. Re-dragging an input's wire is a remove plus an add on the
        SAME socket, and disconnect clears that socket's attribute -- run after the connect
        it would clear the wire the user just made.

        heal=False, because a wire the user cut in the editor is one wire. Healing is for
        detaching a whole node from a chain (delete does that); here it took A -> B -> View,
        cut B's input, and rewired View to A, which is not what a cut wire means.

        Guarded against _rebuild_tree's own link.new() calls (see _rebuilding) and against
        firing for a tree that is not the one currently open (Blender calls this for any
        tree of this type, not only the visible one).
        """
        if _rebuilding:
            return
        view = active_view()
        if view is None or view.tree is not self:
            return
        # A link to or from a collapsed node's "+" is not a wire yet: which param it means
        # is picked first. Taken out of the tree here, and joined one tick later, off
        # Blender's own call stack (as _reload_after_connect is).
        for link in [lk for lk in self.links if _PLUS in (lk.from_socket.name, lk.to_socket.name)]:
            a = _plus_or_port(view, link.from_node, link.from_socket, is_input=False)
            b = _plus_or_port(view, link.to_node, link.to_socket, is_input=True)
            self.links.remove(link)
            if a is not None and b is not None:
                bpy.app.timers.register(lambda a=a, b=b: _join_later(view, a, b), first_interval=0.0)
        changed = False
        present = {(link.from_node.name, link.from_socket.name,
                    link.to_node.name, link.to_socket.name) for link in self.links}
        expected = set()
        for edge in view.edges:
            key = (edge.src_node_path, _port_name(edge.src_attr_path),
                   edge.dst_node_path, _port_name(edge.dst_attr_path))
            expected.add(key)
            if key in present:
                continue
            # Only a wire the tree COULD have shown counts as cut. An edge whose node
            # never got a class, or whose socket never got made, was never on screen, and
            # clearing it from USD would punish the model for the view's gap. A node
            # removed from the tree behind the frontend's back is the same case: the
            # model wins, and the next reload brings node and wires back together.
            src, dst = self.nodes.get(key[0]), self.nodes.get(key[2])
            if src is None or dst is None or key[1] not in src.outputs or key[3] not in dst.inputs:
                continue
            port = _port_ref_named(view, edge.dst_node_path, key[3], is_input=True)
            if port is None:
                continue
            chunk_graph.disconnect(view.stage, port, view.prims, view.chunks, heal=False)
            changed = True
        for link in self.links:
            if (link.from_node.name, link.from_socket.name,
                    link.to_node.name, link.to_socket.name) in expected:
                continue
            a = _link_port_ref(view, link.from_node, link.from_socket, is_input=False)
            b = _link_port_ref(view, link.to_node, link.to_socket, is_input=True)
            if a is not None and b is not None and chunk_graph.connect(
                    view.stage, a, b, view.chunks):
                changed = True
        if changed:
            bpy.app.timers.register(lambda: _reload_after_connect(view), first_interval=0.0)


def socket_param(socket, node):
    """The param a socket mirrors, or None for an exec port and for a name that is not one.

    Resolved through `iter_connectable_params` -- the same walk `iter_ports` used to make
    the socket -- rather than `get_param`, so the match cannot drift: a name that produced
    a port resolves here, and one that did not resolves nowhere.
    """
    if socket.port_type == chunk_graph.PORT_EXEC:
        return None
    chunk = node.get_chunk() if hasattr(node, "get_chunk") else None
    if chunk is None:
        return None
    return _connectable_by_name(chunk).get(socket.name)


#: id(chunk) -> (when built, the chunk, {name: param}). Every socket of every node asks on
#: every redraw, and the walk behind it types each of the chunk's params -- panning a
#: network of a few dozen nodes spent most of each frame there. Rebuilt at most this often,
#: which is how quickly a param added or re-typed shows on its socket.
_connectable_cache: dict = {}
_CONNECTABLE_TTL = 1.0


def _connectable_by_name(chunk) -> dict:
    now = time.monotonic()
    cached = _connectable_cache.get(id(chunk))
    if cached is not None and cached[1] is chunk and now - cached[0] < _CONNECTABLE_TTL:
        return cached[2]
    by_name: dict = {}
    for param in chunk_graph.iter_connectable_params(chunk):
        by_name.setdefault(param.name, param)  # the first match, as the walk returned it
    _connectable_cache[id(chunk)] = (now, chunk, by_name)
    return by_name


def socket_draws_value(socket, param) -> bool:
    """Whether this socket draws the param's control rather than just its name.

    Only an unlinked input does. An output cannot: Blender lays a node out as outputs, then
    `draw_buttons`, then inputs, and exposes neither an alignment flag nor a socket
    position, so an output row can never sit beside the field it reports -- it can only say
    which param it is. A linked input has a wire for its value, as in any Blender node.
    """
    if param is None or socket.is_output or socket.is_linked:
        return False
    if getattr(param, "item_images", None) is not None:
        # An ImageMenu's picture needs the body's room; a socket row is one line high and
        # squeezed it into a strip beside the name.
        return False
    if chunk_blender.list_row_of(param) is not None:
        # A list row's field belongs in its row box in the body, beside the row's other
        # fields; drawn on the socket it would sit above the body with row 0's value
        # and the box would show a row with one field missing.
        return False
    return not chunk_blender.is_hidden(param)


def socket_drawn_params(node) -> set:
    """Param names the node's input sockets draw, so the body does not draw them twice.

    A Vector is ONE port but several drawable params (a Transform's tx/ty/tz), so its
    socket claims its children -- which is also why this returns names rather than params.
    """
    names: set = set()
    for socket in node.inputs:
        param = socket_param(socket, node)
        if not socket_draws_value(socket, param):
            continue
        if hasattr(param, "get_type"):
            names.add(chunk_blender.prop_key(param))
        else:
            names.update(chunk_blender.prop_key(p) for p in chunk_blender.iter_drawable(param))
    return names


def sync_socket_visibility(node) -> None:
    """Hide the sockets of hidden params, so the node shrinks to what the chunk shows.

    Qt rebuilds its typed ports when a param's HIDDEN bit flips and the hidden ones go
    away; Blender's equivalent is socket.hide, which folds an unlinked socket out of the
    node (a linked one stays, as Blender always keeps a wired socket visible). Without
    this a chunk that shows one of several groups -- mesh's cube/tube/grid -- drew
    every group's rows at once and the menu that picks one visibly did nothing.

    A collapsed node (node.hide) also hides every unlinked param socket, which Blender
    would otherwise still draw down the collapsed node's ends: it shows only the wired end
    of each wired param, as chunk_graph.collapsed_ports has every frontend do, and a "+"
    on each side that has something left to wire.
    """
    collapsed = node.hide
    for collection in (node.inputs, node.outputs):
        spare = False  # a param on this side that is shown open but not wired
        for socket in collection:
            if socket.port_type == chunk_graph.PORT_EXEC or socket.name == _PLUS:
                continue
            param = socket_param(socket, node)
            hidden = param is not None and chunk_blender.is_hidden(param)
            if collapsed and not socket.is_linked:
                spare |= not hidden
                hidden = True
            if socket.hide != hidden:
                socket.hide = hidden
        plus = collection.get(_PLUS)
        if plus is not None and plus.hide != (not (collapsed and spare)):
            plus.hide = not (collapsed and spare)


class UsdChunksSocket(bpy.types.NodeSocket):
    """One port. Named for the USD attribute it mirrors (`inputs:chunk`, `inputs:<param>`),
    which is what lets the reconciler match a Blender link back to a USD connection without
    keeping a side table.

    An unlinked *input* draws its param's control right here, which is the whole reason the
    node body skips it. Blender will not interleave sockets with body rows -- see
    `socket_draws_value` -- so this is as close as the editor gets to Qt's row of
    [in port | control | out port]. What is left over is the output stack above the body,
    which at least names its params the way the rows below do.
    """

    bl_idname = SOCKET_ID
    bl_label = "chunk port"

    port_type: bpy.props.StringProperty(default="exec")

    def draw(self, context, layout, node, text):
        param = socket_param(self, node)
        if socket_draws_value(self, param):
            if hasattr(param, "get_type"):
                if chunk_blender.draw_param(node, layout, param):
                    return
            else:
                # A Vector: its components are what carry values, so the socket draws the
                # column of them under one port.
                col = layout.column(align=True)
                drawn = [chunk_blender.draw_param(node, col, child,
                                                  text=child.label or child.name)
                         for child in chunk_blender.iter_drawable(param)]
                if any(drawn):
                    return
        # The param's LABEL, not the attribute name: an output row reading
        # "primpath_location" while the input row below it reads "Prim Path" makes the two
        # stacks look like different things, which is the confusing half of Blender's
        # layout rather than a necessary part of it.
        layout.label(text=(param.label or text) if param is not None else text)

    def draw_color(self, context, node):
        return _PORT_COLOURS.get(self.port_type, _DEFAULT_COLOUR)


class UsdChunksNodeBase(bpy.types.Node):
    """Shared behaviour for every generated chunk node.

    Subclasses are built at registration time by `register_chunk_types`, one per chunk
    module, carrying that chunk's params as RNA properties.
    """

    # Registered and usable in its own right, not merely a base: a chunk whose module
    # failed to load has no generated class, and _rebuild_tree falls back to this so the
    # node still appears with its ports and wires rather than vanishing from the graph.
    bl_idname = "UsdChunksNode"
    bl_label = "Chunk"

    chunk_path: bpy.props.StringProperty(name="Chunk", default="")

    @classmethod
    def poll(cls, tree):
        return tree.bl_idname == TREE_ID

    def get_chunk(self):
        """The live chunk this node mirrors, or None.

        Resolved through the registry every time rather than cached: a reload rebuilds the
        chunk objects under a tree Blender has kept, so a cached reference outlives what it
        points at. The prim path is the stable identity.
        """
        view = active_view()
        if view is None:
            return None
        return view.chunks.get(self.chunk_path)

    def copy(self, node) -> None:
        """Blender's hook for a node made by duplicating `node` -- Shift+D, Ctrl+C/V, or
        the Node menu. It copies every RNA property, `chunk_path` included, so the copy
        pointed at the ORIGINAL prim: its fields wrote into the original chunk and its
        wires were authored onto the original's attributes. The chunk operators shadow
        those keys inside this tree type (see the add-on's keymap) and go through
        `duplicate_selection`/`paste_at`, which make a real chunk; a copy that still
        arrives here is a node with no chunk behind it, and blanking the path makes it
        exactly that -- it draws "<no chunk>" and the next reload drops it.
        """
        self.chunk_path = ""

    def draw_buttons(self, context, layout):
        chunk = self.get_chunk()
        if chunk is not None and chunk.has_state(ChunkFlags.State.FAILED):
            # The header is red (see apply_node_state); this says why.
            layout.label(text="Cook failed or was cancelled", icon="CANCEL")
        elif chunk is not None and chunk.has_state(ChunkFlags.State.STALE):
            # Qt draws an amber badge with this as its tooltip. The header is amber here
            # too (see apply_node_state), but a colour alone does not say what to do.
            layout.label(text="Needs recook", icon="ERROR")
        if chunk is not None and chunk_graph.node_help(chunk) is not None:
            # Qt's "?": hover for the module's description and requirements, click for
            # the same in a popup.
            row = layout.row()
            row.alignment = "RIGHT"
            row.operator(USDCHUNKS_OT_node_help.bl_idname, text="", icon="QUESTION",
                         emboss=False).node_path = self.name
        # Params with an input socket are drawn by that socket instead, beside their port.
        chunk_blender.draw_params(self, layout, chunk, skip=socket_drawn_params(self))


class USDCHUNKS_OT_param_button(bpy.types.Operator):
    """Click a chunk's Button param.

    An operator rather than a callback because Blender dispatches a click by operator name;
    there is nowhere in `layout.operator` to hang a bound method.
    """

    bl_idname = "usd_chunks.param_button"
    bl_label = "Chunk Action"
    bl_options = {"REGISTER", "UNDO"}

    node_path: bpy.props.StringProperty()
    param_name: bpy.props.StringProperty()

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None)
        node = tree.nodes.get(self.node_path) if tree else None
        chunk = node.get_chunk() if node else None
        param = chunk_blender.resolve_key(chunk, self.param_name) if chunk else None
        if param is None:
            self.report({"WARNING"}, f"no such param: {self.param_name}")
            return {"CANCELLED"}
        param.click()
        return {"FINISHED"}


class USDCHUNKS_OT_node_help(bpy.types.Operator):
    """Show what this chunk does and what it requires"""

    bl_idname = "usd_chunks.node_help"
    bl_label = "Chunk Help"

    node_path: bpy.props.StringProperty()

    @staticmethod
    def _help(context, node_path: str) -> chunk_graph.NodeHelp | None:
        tree = getattr(context.space_data, "edit_tree", None)
        node = tree.nodes.get(node_path) if tree else None
        chunk = node.get_chunk() if node else None
        return chunk_graph.node_help(chunk) if chunk is not None else None

    @classmethod
    def description(cls, context, properties):
        # The hover tooltip: the help itself rather than the docstring above.
        help_ = cls._help(context, properties.node_path)
        return help_.text if help_ is not None else cls.__doc__

    def execute(self, context):
        help_ = self._help(context, self.node_path)
        if help_ is None:
            return {"CANCELLED"}
        # Blender's popups do not wrap or scroll: wrap by hand, and stop at about the 400
        # pixels Qt's scrolls past.
        lines = [wrapped for para in help_.text.split("\n")
                 for wrapped in (textwrap.wrap(para, 70) or [""])]
        if len(lines) > 20:
            lines = lines[:19] + ["..."]

        def draw(menu, _context):
            for line in lines:
                menu.layout.label(text=line)

        context.window_manager.popup_menu(draw, title=self.node_path, icon="QUESTION")
        return {"FINISHED"}


class USDCHUNKS_OT_pin_prim(bpy.types.Operator):
    """Pull the viewport-picked prim(s) into a PRIM_SELECTION param.

    Blender's counterpart of chunk_qt_widgets.py's pin_prim() -- same read
    (SceneSender.get_selected_prims(), which chunk_blender_addon.BlenderScene answers from
    set_picked_prim_paths below), same join-by-comma write. Nothing selected is a misfire,
    not an instruction to erase: keeps whatever path was already there.
    """

    bl_idname = "usd_chunks.pin_prim"
    bl_label = "Pin Picked Prim"
    bl_options = {"REGISTER", "UNDO"}

    node_path: bpy.props.StringProperty()
    param_name: bpy.props.StringProperty()

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None)
        node = tree.nodes.get(self.node_path) if tree else None
        chunk = node.get_chunk() if node else None
        # A key, not a name: a prototype row's field is `prototypes.0.proto_path`.
        param = chunk_blender.resolve_key(chunk, self.param_name) if chunk else None
        if param is None:
            self.report({"WARNING"}, f"no such param: {self.param_name}")
            return {"CANCELLED"}
        scene = param.get_scene()
        prims = scene.get_selected_prims() if scene else []
        if not prims:
            self.report({"INFO"}, "nothing picked -- ctrl+click a prim in the viewport")
            return {"CANCELLED"}
        param.value = ",".join(str(p.GetPath()) for p in prims)
        return {"FINISHED"}


def _search_param(op):
    tree = bpy.data.node_groups.get(op.tree_name)
    node = tree.nodes.get(op.node_path) if tree else None
    chunk = node.get_chunk() if node else None
    return chunk_blender.resolve_key(chunk, op.param_name) if chunk else None


#: The last item list the search popup was handed: Blender does not own the strings an
#: items callback returns, so they are kept alive here (as factory._enum_cache does).
_search_items_alive: list = []


def _search_items(self, _context):
    param = _search_param(self)
    items = chunk_blender._enum_items(param) if param is not None else [("", "", "")]
    _search_items_alive[:] = [items]
    return items


#: How many thumbnails the picture search shows at once; typing narrows the rest.
_THUMB_RESULTS = 30
_THUMB_COLUMNS = 5


class USDCHUNKS_OT_pick_menu_item(bpy.types.Operator):
    """Pick this item."""

    bl_idname = "usd_chunks.pick_menu_item"
    bl_label = "Pick"
    bl_options = {"REGISTER", "UNDO"}

    tree_name: bpy.props.StringProperty(options={"HIDDEN"})
    node_path: bpy.props.StringProperty(options={"HIDDEN"})
    param_name: bpy.props.StringProperty(options={"HIDDEN"})
    item: bpy.props.StringProperty(options={"HIDDEN"})

    def execute(self, _context):
        param = _search_param(self)
        if param is None:
            return {"CANCELLED"}
        param.value = self.item
        return {"FINISHED"}


class USDCHUNKS_OT_search_menu(bpy.types.Operator):
    """Search this menu by typing.

    A menu with pictures (chunk.ImageMenu) opens a popup of its own: a filter field over a
    grid of large thumbnails. Blender's search popup, which plain menus get, has room only
    for a small icon beside each name.
    """

    bl_idname = "usd_chunks.search_menu"
    bl_label = "Search"
    bl_options = {"REGISTER", "UNDO"}
    # invoke_search_popup searches the property bl_property names.
    bl_property = "item"

    # By name, not from context: the popup's items callback runs with whatever context
    # Blender has then, which need not be the node editor the button was in.
    tree_name: bpy.props.StringProperty(options={"HIDDEN"})
    node_path: bpy.props.StringProperty(options={"HIDDEN"})
    param_name: bpy.props.StringProperty(options={"HIDDEN"})
    item: bpy.props.EnumProperty(items=_search_items)
    # TEXTEDIT_UPDATE: the grid narrows with every key, not only on Enter.
    filter: bpy.props.StringProperty(name="", options={"TEXTEDIT_UPDATE", "SKIP_SAVE"})

    def invoke(self, context, _event):
        param = _search_param(self)
        if getattr(param, "item_images", None) is not None:
            return context.window_manager.invoke_popup(self, width=110 * _THUMB_COLUMNS)
        context.window_manager.invoke_search_popup(self)
        return {"RUNNING_MODAL"}

    def draw(self, _context):
        """The picture search: only reached through invoke_popup, for an ImageMenu."""
        param = _search_param(self)
        if param is None:
            return
        layout = self.layout
        layout.activate_init = True  # the field takes the keyboard as the popup opens
        layout.prop(self, "filter", icon="VIEWZOOM")
        term = self.filter.lower()
        matches = [i for i in (param.menu_items or []) if i and term in i.lower()]
        images = param.item_images or {}
        grid = layout.grid_flow(row_major=True, columns=_THUMB_COLUMNS, even_columns=True)
        for item in matches[:_THUMB_RESULTS]:
            col = grid.column(align=True)
            icon = chunk_blender._icon_id(images.get(item, ""))
            if icon:
                col.template_icon(icon_value=icon, scale=5.0)
            else:
                col.label(text="", icon="IMAGE_DATA")
            op = col.operator(USDCHUNKS_OT_pick_menu_item.bl_idname, text=item,
                              depress=item == param.value)
            op.tree_name, op.node_path = self.tree_name, self.node_path
            op.param_name, op.item = self.param_name, item
        if len(matches) > _THUMB_RESULTS:
            layout.label(text=f"{len(matches) - _THUMB_RESULTS} more -- type to narrow")
        elif not matches:
            layout.label(text="Nothing matches")

    def execute(self, _context):
        param = _search_param(self)
        if param is None or not self.item:
            return {"CANCELLED"}
        param.value = self.item
        return {"FINISHED"}


class USDCHUNKS_OT_pick_plus(bpy.types.Operator):
    """Wire to this param."""

    bl_idname = "usd_chunks.pick_plus"
    bl_label = "Wire To"
    bl_options = {"REGISTER", "UNDO"}

    index: bpy.props.IntProperty(options={"HIDDEN"})

    def execute(self, _context):
        view = active_view()
        if view is None or view.pending_pick is None or not 0 <= self.index < len(view.pending_pick[0]):
            return {"CANCELLED"}
        view.choose(view.pending_pick[0][self.index])
        return {"FINISHED"}


def _draw_pick_menu(menu, _context) -> None:
    """One entry per param the pending "+" pick offers, labelled as Qt's menu is."""
    view = active_view()
    if view is None or view.pending_pick is None:
        return
    for index, ref in enumerate(view.pending_pick[0]):
        label = chunk_graph.param_menu_label(ref.param, view.chunks.get(ref.node_path))
        menu.layout.operator(USDCHUNKS_OT_pick_plus.bl_idname, text=label).index = index


def _open_pick_popup() -> None:
    """Show the active view's pending "+" pick at the mouse, in the node editor.

    A popup menu rather than a search popup, which drew its list only after the next
    mouse event. Reached from a timer (see UsdChunksNodeTree.update), where there is no
    window in the context, so one is overridden in. Background Blender has no window: the
    pick then waits in pending_pick for a script or test.
    """
    if bpy.app.background:
        return
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != "NODE_EDITOR":
                continue
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            with bpy.context.temp_override(window=window, area=area, region=region):
                bpy.context.window_manager.popup_menu(_draw_pick_menu, title="Wire To")
            return


class USDCHUNKS_OT_revert_override(bpy.types.Operator):
    """Put back the value a hosted network's file has for this param.

    The arrow beside a field drawn in alert red (chunk_blender.draw_param): the shot's
    opinion is dropped from every layer it owns and the file's shows through again. Qt
    offers the same as "Revert to <file>" on right-click.
    """

    bl_idname = "usd_chunks.revert_override"
    bl_label = "Revert Override"
    bl_options = {"REGISTER", "UNDO"}

    node_path: bpy.props.StringProperty()
    param_name: bpy.props.StringProperty()

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None)
        node = tree.nodes.get(self.node_path) if tree else None
        chunk = node.get_chunk() if node else None
        param = chunk_blender.resolve_key(chunk, self.param_name) if chunk else None
        if param is None:
            self.report({"WARNING"}, f"no such param: {self.param_name}")
            return {"CANCELLED"}
        source = chunk_stage.param_override_source(param)
        if source is None:
            self.report({"INFO"}, "not an override")
            return {"CANCELLED"}
        chunk_stage.revert_param_override(param)
        self.report({"INFO"}, "reverted to %s" % (source.GetDisplayName() or source.identifier))
        return {"FINISHED"}


class USDCHUNKS_OT_list_resize(bpy.types.Operator):
    """Add or remove a row of a ListGroup -- the +/- beside its header."""

    bl_idname = "usd_chunks.list_resize"
    bl_label = "Resize List"
    bl_options = {"REGISTER", "UNDO"}

    node_path: bpy.props.StringProperty()
    list_name: bpy.props.StringProperty()
    delta: bpy.props.IntProperty(default=1)

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None)
        node = tree.nodes.get(self.node_path) if tree else None
        chunk = node.get_chunk() if node else None
        group = chunk.get_param(self.list_name) if chunk else None
        if not isinstance(group, core.ChunkListGroupBase):
            self.report({"WARNING"}, f"no such list: {self.list_name}")
            return {"CANCELLED"}
        size = max(0, group.size + self.delta)
        if size == group.size:
            return {"CANCELLED"}
        # Growing sends PARAM_ADDED, shrinking deletes the row's params: either way the
        # view hears it and rebuilds, which is what seeds the new row's ID properties.
        group.size = size
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Generated node classes
# ---------------------------------------------------------------------------

_generated: dict = {}


def node_class_id(module_name: str) -> str:
    return "UsdChunksNode_" + module_name.replace(".", "_").replace("-", "_")


def register_chunk_types(scene=None) -> list:
    """Build and register one Node subclass per chunk module in the registry.

    Done from `main()` rather than from a live chunk because registration happens once, at
    add-on load, before any graph exists. A chunk whose `main()` raises -- an example that
    imports a missing dependency, say -- is skipped with a note rather than taking the
    whole add-on down with it, which mirrors what the registry itself does on discovery.
    """
    registry = chunk_registry.ChunkRegistry()
    made = []
    for module in registry.get_chunk_modules():
        name = getattr(module, "module_name", None)
        if not name or name in _generated:
            continue
        try:
            # load() rather than reading module.func: a discovered-but-unloaded module has
            # func=None, so reading it skipped every chunk and generated nothing at all.
            # Registration therefore pays the load cost for the whole library once, at
            # add-on startup, because Blender needs a class registered before a node of
            # that type can be created -- there is no lazy path.
            chunks = module.create() if module.loaded else module.load()
            if not chunks:
                continue
            props, buttons, skipped = chunk_blender.annotations_for(chunks[0])
        except Exception:
            logger.debug("skipping %s: main() did not build", name)
            continue

        if skipped:
            logger.debug("%s: no Blender control for %s", name, skipped)

        cls = type(node_class_id(name), (UsdChunksNodeBase,), {
            "bl_idname": node_class_id(name),
            "bl_label": name.rsplit(".", 1)[-1],
            "chunk_module": name,
            # Blender's 140 default assumes a socket row holds a name and nothing else.
            # These rows hold a label AND the param's field, so at 140 every label
            # truncated -- "Prim Path" drew as "Prim ...", which is worse than useless on
            # a node whose two string params then look identical.
            "bl_width_default": NODE_WIDTH,
            "__annotations__": dict(props, **UsdChunksNodeBase.__annotations__),
        })
        try:
            _register_once(cls)
        except Exception:
            # A warning, not debug: the chunk is missing from the Add menu and from every
            # graph that uses it, and nothing else says why.
            logger.warning("could not register a node class for %s", name, exc_info=True)
            continue
        _generated[name] = cls
        made.append(name)
    build_add_menu()
    return made


def unregister_chunk_types() -> None:
    for cls in list(_generated.values()):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:  # pragma: no cover - already gone
            pass
    _generated.clear()


# ---------------------------------------------------------------------------
# The frontend
# ---------------------------------------------------------------------------

_active_view = None

#: Set for the duration of BlenderGraphView._rebuild_tree, which recreates links from USD
#: via self.tree.links.new(...) -- each of those is itself a topology change, so without
#: this guard UsdChunksNodeTree.update below would re-enter for every edge on every
#: reload, re-authoring connections that were just read FROM the thing it would be writing
#: them back TO.
_rebuilding = False


def active_view():
    """The view a node should ask for its chunk.

    Module-level because a Blender node is constructed by Blender, not by us, so there is
    no constructor to hand a view to. One network is live at a time: the add-on switches
    it to whichever chunk tree the node editor shows.
    """
    return _active_view


#: The ID property naming a chunk tree for good: a tree is found by name first, and by
#: this when the name has changed.
TREE_UID = "usd_chunks_uid"


def tree_uid(tree) -> str:
    """The tree's lasting id, given one the first time it is asked for.

    Args:
        tree: A chunk node tree.

    Returns:
        The id, kept in the tree's ID properties so it saves with the .blend.
    """
    uid = tree.get(TREE_UID)
    if not uid:
        uid = uuid.uuid4().hex
        tree[TREE_UID] = uid
    return uid


def clear_active_view() -> None:
    """No network open: a loaded .blend with no chunk tree in it."""
    global _active_view
    _active_view = None


#: Prim paths the live-viewport picker (chunk_blender_addon.viewport, which owns the actual
#: Hydra pick) has most recently found under the cursor. Lives in this module -- the one
#: both viewport.py and chunk_blender_addon/__init__.py (BlenderScene.get_selected_prims())
#: already import -- rather than in either of those two directly, so neither has to depend
#: on the other.
_picked_prim_paths: list = []


def set_picked_prim_paths(paths) -> None:
    _picked_prim_paths[:] = [str(p) for p in paths]


def get_picked_prim_paths() -> list:
    return list(_picked_prim_paths)


def _link_port_ref(view, node, socket, is_input: bool):
    """The chunk_graph PortRef a Blender socket mirrors.

    Built the same way iter_ports built the socket in the first place (matched by
    is_input + bare name), rather than reconstructed from node.chunk_path + socket.name by
    hand -- that would have to re-derive the inputs:/outputs: naming, the merge multi-slot
    case (chunk1, chunk2, ...) and the exec-vs-typed split all over again, and drift from
    the one true naming the moment any of those changed.
    """
    path = getattr(node, "chunk_path", None)
    return _port_ref_named(view, path, socket.name, is_input) if path else None


def _plus_or_port(view, node, socket, is_input: bool):
    """A link end as the engine names it: a PlusRef for a "+" socket, else its PortRef."""
    path = getattr(node, "chunk_path", None)
    if not path:
        return None
    if socket.name == _PLUS:
        return chunk_graph.PlusRef(path, is_input)
    return _port_ref_named(view, path, socket.name, is_input)


def _join_later(view, a, b) -> None:
    """Timer callback for a link made to a "+" -- see UsdChunksNodeTree.update."""
    if active_view() is view:
        view.join(a, b)
    return None


def _port_ref_named(view, path: str, name: str, is_input: bool):
    """The PortRef for socket `name` on the node at prim path `path`, or None.

    Resolved from the prim rather than the Blender node so it still answers for a node
    the editor no longer has -- which is exactly the case when an edge is being
    disconnected because its node or wire was removed in the editor.
    """
    prim = view.prims.get(path)
    if prim is None:
        return None
    chunk = view.chunks.get(path)
    for port in chunk_graph.iter_ports(prim, chunk):
        if port.is_input == is_input and port.name == name:
            return port
    return None


def _chunk_of(sender):
    """The chunk a notice's sender belongs to, or None.

    A value change is sent by the param's AttributeSender, a state change by the chunk's or
    a param's, PARAM_ADDED by the chunk itself -- so the sender is anywhere from zero to two
    owners away from something with get_chunk. Walk up until one answers; a bounded walk,
    because an owner chain is a tree and a malformed one should cost a missed refresh, not
    a hang inside notice dispatch.
    """
    obj = sender
    for _ in range(6):
        if obj is None:
            return None
        # By type, not by attribute: the walk can reach the SceneSender, whose
        # get_chunk(chunk) is a lookup with a different signature altogether.
        if isinstance(obj, core.ChunkCommon):
            return obj.get_chunk()
        get_owner = getattr(obj, "get_owner", None)
        obj = get_owner() if get_owner is not None else None
    return None


def _is_locked(stage, prim) -> bool:
    """Qt's rule: a prim is locked when it is composed in -- its def comes from a foreign
    layer or across an arc (chunk_utils.composed_in_from) -- and that layer is not the
    current edit target, so edits here would not reach it.

    Its OWN def in a layer we own means it is ours to move, however much composes in
    beneath it: a Reference hosting a network has the network's group under its prim and
    is still this graph's node. Reading the prim stack without that test locked the host
    along with its contents.
    """
    src_layer = chunk_utils.composed_in_from(prim)
    return src_layer is not None and src_layer.identifier != stage.GetEditTarget().GetLayer().identifier


def _reload_after_connect(view) -> None:
    """Runs one tick after a link authored a connection -- see UsdChunksNodeTree.update.

    Not called synchronously from update(): reload() rebuilds self.tree.nodes/links, and
    doing that while still on Blender's own call stack for the very link.new() that is
    reporting itself changed is the kind of mutate-the-tree-from-its-own-topology-callback
    that Blender does not promise survives. One registered timer tick later, the callback
    has fully returned and it is safe.
    """
    if active_view() is view:
        view._recook()
        view.reload()
    return None


class BlenderGraphView:
    """`chunk_graph.GraphFrontend` over a Blender node tree.

    Holds `context` (the prim path of the graph being shown) and `selected_nodes` (a set of
    prim paths), as the Protocol documents.
    """

    def __init__(self, stage, scene, tree=None, context=chunk_graph.CHUNKS_ROOT):
        self.stage = stage
        self.scene = scene
        self.tree_name = ""
        self.tree_uid = ""
        self.tree = tree
        self.context = context
        self.chunks: dict = {}
        self.prims: dict = {}
        self.ports: list = []
        self.edges: list = []
        self.positions: dict = {}
        self.selected_nodes: set = set()
        #: Prim paths authored in a layer that is not the edit target -- Qt's "locked".
        #: Read at reload, shown as a dark-amber header, and handed to layout_graph so
        #: they stay put and are routed around.
        self.locked: set = set()
        self.status = ""
        #: A link that met a collapsed node's "+", waiting on a param to be picked:
        #: (the choices, what the pick joins). See join and choose; the popup only shows it.
        self.pending_pick: tuple[list, object] | None = None
        #: Guards ensure_view against the reload it can trigger through itself.
        self._ensuring_view = False
        #: What changed since the last flush -- node paths whose values or state moved,
        #: and whether the node SET did (which needs a rebuild, not a re-sync). Drained
        #: by `flush`, one tick later, so a cook that touches two hundred params costs one
        #: pass over the nodes it reached rather than two hundred redraws.
        self._dirty: set = set()
        self._reload_pending = False
        self._flush_scheduled = False
        #: {id(chunk): prim path}, so a notice's sender resolves to a node without a
        #: get_chunk_prim_path per notice. Rebuilt in reload with the chunks dict.
        self._path_of: dict = {}
        # The connections that keep the tree honest between reloads. Held on the instance
        # because NoticeConnection weakrefs its method: a connection left as a local dies
        # with the frame, and a bound method is only as alive as the object holding it.
        # Global rather than per chunk, because the chunk objects are rebuilt under this
        # view on every load and per-object connections would die with them. Each handler
        # filters by whether the sender's chunk is one this graph shows.
        self._notice_conns = [
            NoticeConnection(self._on_attribute_changed,
                             ChunkNoticeAlias.ATTRIBUTE_CHANGED, globally=True),
            NoticeConnection(self._on_attribute_changed,
                             ChunkNoticeAlias.STATE_CHANGED, globally=True),
            NoticeConnection(self._on_params_changed,
                             ChunkNoticeAlias.PARAM_ADDED, globally=True),
            NoticeConnection(self._on_params_changed,
                             ChunkNoticeAlias.PARAM_REMOVED, globally=True),
            NoticeConnection(self._on_chunk_deleted,
                             ChunkNoticeAlias.CHUNK_DELETED, globally=True),
            NoticeConnection(self._on_objects_changed,
                             ChunkNoticeAlias.OBJECT_CHANGED, globally=True),
        ]
        global _active_view
        _active_view = self

    @property
    def tree(self):
        """The node tree, looked up by NAME every time rather than held.

        The same rule `get_chunk` follows, for the same reason one step further out: an
        operator registered with UNDO makes Blender push an undo step, and a memfile undo
        step can re-create every ID datablock in the file. A Python object holding the old
        `bpy.types.NodeTree` is then pointing at freed memory -- touching it raises
        "StructRNA of type NodeTree has been removed", which surfaces as a chunk that gets
        created in USD and never appears in the editor.

        A renamed tree is found again by its lasting id (`tree_uid`). Taking any chunk tree
        instead, as this once did, picked the wrong one when there were two: rename a
        network and the view drew into, and cooked for, the other.
        """
        if not self.tree_name:
            # Never given a tree -- a headless view, driving USD with nothing to draw into.
            # Adopting a stray tree here would make such a view start rebuilding one.
            return None
        tree = bpy.data.node_groups.get(self.tree_name)
        if tree is not None and tree.bl_idname == TREE_ID and tree.get(TREE_UID, self.tree_uid) == self.tree_uid:
            return tree
        tree = next((t for t in bpy.data.node_groups
                     if t.bl_idname == TREE_ID and self.tree_uid and t.get(TREE_UID) == self.tree_uid), None)
        if tree is not None:
            self.tree_name = tree.name
        return tree

    @tree.setter
    def tree(self, tree) -> None:
        self.tree_name = tree.name if tree is not None else ""
        self.tree_uid = tree_uid(tree) if tree is not None else ""

    # -- reading -----------------------------------------------------------

    def ensure_view(self) -> None:
        """Give the context a view if it has none, as every other frontend does.

        Without one `recook_for_view` blocks everything and the graph cooks NOTHING -- a
        network would open here looking correct and do nothing at all, which is exactly how
        Flet and ImGui differed from Qt before they called this.

        Run from `reload`, not only when the network is opened. An EMPTY context has no exec
        tail, so `ensure_default_view` has nothing to wire a view to and returns None -- and
        a new network is empty at exactly the moment it opens. Called once at open, that is
        the only time it ever ran, so the first chunk added with Shift+A never got a view
        and nothing in the graph cooked. Qt has always re-run this after every build; this
        is the same rule, from the one place here that rebuilds.

        Never overrides a slot the file already records: `ensure_default_view` returns the
        EXISTING slot when a view is already wired to the tail, so setting it on every open
        would reset whichever view the user had chosen.
        """
        if self._ensuring_view:
            # Re-entered from the reload/activate below. The outer call finishes the job.
            return
        self._ensuring_view = True
        try:
            before = len(self.scene.get_chunks())
            slot, activate = chunk_graph.default_view_slot(
                self.stage, self.scene, self.context, chunk_graph.context_chunk(self.scene, self.chunks, self.context))
            if slot is None:
                if len(self.scene.get_chunks()) != before:
                    # An Output was made: the tree needs its node.
                    self.chunks = {chunk_stage.get_chunk_prim_path(c): c
                                   for c in self.scene.get_chunks()}
                    self.reload()
                return
            self.chunks = {chunk_stage.get_chunk_prim_path(c): c
                           for c in self.scene.get_chunks()}
            if activate:
                self.set_active_slot(slot)
        finally:
            self._ensuring_view = False

    def reload(self) -> None:
        """Rebuild the tree from USD. The tree is a view; this is the only writer of it."""
        # Blender owns dragging, so a moved node's new position lives ONLY in
        # node.location until something writes it back -- and _rebuild_tree below
        # unconditionally clears and recreates every node from USD. Without this, any
        # drag done since the last reload would be silently discarded the next time
        # anything triggered one: Shift+A, a view-slot change, even a recook. Pulled
        # against the OLD self.prims/tree, i.e. exactly the nodes about to be thrown away.
        self.pull_positions_from_tree()
        # Selection too, for the same reason: Blender owns clicking and box-select, so
        # the editor's selection is only in node.select until it is read -- and the
        # rebuild below makes every node afresh, which Blender does SELECTED. Without
        # this, any reload left the whole graph selected, and one that ran mid-drag
        # (see UsdChunksNodeTree.update) looked like the drag had grabbed the network.
        # Adopted only for nodes in the context being shown: a dive clears the selection
        # and the tree still holds the OUTER graph's nodes, and re-adopting those would
        # leave their chunks flagged SELECTED where the inner graph's X could reach them.
        tree_selected = ({n.name for n in self.tree.nodes if n.select}
                         if self.tree is not None else None)
        self.ensure_view()
        self.prims, self.ports, self.edges = chunk_graph.read_graph(
            self.stage, self.context, self.chunks)
        if tree_selected is not None:
            self.set_selection(tree_selected & set(self.prims))
        self.positions = {p: chunk_graph.node_position(prim)
                          for p, prim in self.prims.items()}
        self.locked = {p for p, prim in self.prims.items() if _is_locked(self.stage, prim)}
        self._path_of = {id(c): p for p, c in self.chunks.items()}
        if self.tree is not None:
            self._rebuild_tree()
        # A rebuild mirrored every node, so anything the notices queued before or during
        # it is already on screen. Dropping it here is also what keeps the reload that a
        # notice schedules from re-running once the operation that raised it has already
        # reloaded on its own -- create_chunk, delete_selected and the rest all do.
        self._dirty.clear()
        self._reload_pending = False

    def _rebuild_tree(self) -> None:
        """Discard and re-make the nodes.

        Rebuilt wholesale rather than diffed because USD is the model and a diff would have
        to be right about identity, position, ports *and* links to be worth it -- and being
        subtly wrong about any of them reintroduces exactly the drift this design exists to
        prevent. Graphs here are tens of nodes, not thousands.
        """
        global _rebuilding
        _rebuilding = True
        try:
            self._rebuild_tree_body()
        finally:
            _rebuilding = False

    def _rebuild_tree_body(self) -> None:
        self.tree.nodes.clear()
        made = {}
        for path, prim in self.prims.items():
            chunk = self.chunks.get(path)
            module = chunk_graph.node_kind(prim) or ""
            cls = _generated.get(module, UsdChunksNodeBase)
            try:
                node = self.tree.nodes.new(cls.bl_idname)
            except RuntimeError:
                logger.debug("no registered node class for %s", module)
                continue
            node.name = path
            node.label = chunk_graph.node_label(prim)
            node.chunk_path = path
            # nodes.new() hands back a selected node; the selection is the view's.
            node.select = path in self.selected_nodes
            node.use_custom_color = True
            x, y = self.positions.get(path, (0.0, 0.0))
            node.location = (x * _SCALE, y * _SCALE)
            if chunk is not None:
                chunk_blender.sync_to_node(node, chunk)
            self.apply_node_state(node, prim, chunk)
            made[path] = node

        # `ports` is {node path: [PortRef]}, not a flat list -- read_graph keys it so a
        # frontend can draw one node's ports without filtering the whole graph.
        for path, ports in self.ports.items():
            node = made.get(path)
            if node is None:
                continue
            for port in ports:
                collection = node.inputs if port.is_input else node.outputs
                if port.name in collection:
                    continue
                sock = collection.new(SOCKET_ID, port.name)
                sock.port_type = port.port_type
            chunk = self.chunks.get(path)
            if chunk is not None and chunk_graph.has_collapsible_body(chunk):
                # Shown only while collapsed -- see sync_socket_visibility. A diamond, so
                # it reads as something other than a param on a collapsed node, where
                # Blender draws no socket names.
                for collection in (node.inputs, node.outputs):
                    plus = collection.new(SOCKET_ID, _PLUS)
                    plus.port_type = _PLUS
                    plus.display_shape = "DIAMOND"
            sync_socket_visibility(node)

        for edge in self.edges:
            src, dst = made.get(edge.src_node_path), made.get(edge.dst_node_path)
            if src is None or dst is None:
                continue
            # An EdgeRef names its ends by full attr path; the socket is named for the
            # suffix after the dot, which is the same string iter_ports gave the socket.
            out = src.outputs.get(_port_name(edge.src_attr_path))
            inp = dst.inputs.get(_port_name(edge.dst_attr_path))
            if out is not None and inp is not None:
                self.tree.links.new(out, inp)

        tag_redraw()

    # -- keeping the tree honest between reloads ---------------------------

    def apply_node_state(self, node, prim, chunk) -> None:
        """Show on the node what Qt paints: disabled, collapsed, stale, locked, group,
        active view.

        Disabled is Blender's own mute flag and collapsed its own hide flag, so both read
        as what they are to a Blender user without inventing a second convention -- and
        both survive a reload, because the chunk's state bits are what is read, not what
        the tree happened to show. Everything else is the header
        colour, in the order Qt resolves it -- a view's colour says whether it is driving,
        a group is a group, a locked node is locked -- with stale on top of all of them,
        because "this result is out of date" is the one that asks for an action.
        """
        node.mute = chunk_graph.is_disabled(prim)
        if chunk is not None:
            node.hide = chunk.has_state(ChunkFlags.State.COLLAPSED)
        kind = chunk_graph.node_kind(prim)
        if kind == chunk_graph.VIEW_MODULE:
            slot = prim.GetAttribute(chunk_graph.VIEW_SLOT_ATTR).Get()
            active = bool(slot) and slot == self.active_slot()
            colour = _VIEW_ACTIVE_COLOR if active else _VIEW_INACTIVE_COLOR
        elif chunk is not None and chunk_stage.is_subnet(chunk):
            # is_subnet, not is_chunkgroup: a Reference/Sublayer/Payload hosting a network
            # file is a graph to dive into too, and reads as one.
            colour = _GROUP_HEADER_COLOR
        elif node.name in self.locked:
            colour = _LOCKED_HEADER_COLOR
        else:
            colour = _NODE_HEADER_COLOR
        if chunk is not None and chunk.has_state(ChunkFlags.State.FAILED):
            colour = _FAILED_HEADER_COLOR
        elif chunk is not None and chunk.has_state(ChunkFlags.State.STALE):
            colour = _STALE_HEADER_COLOR
        node.color = colour

    def _is_current(self) -> bool:
        """Whether this view is the one open. The connections are global and outlive a
        view that was replaced (opening another network makes a new one), and a replaced
        view answering notices for chunks it no longer shows -- id() reuse makes that
        possible -- would rebuild a tree nobody is looking at.
        """
        return active_view() is self

    def _mark_dirty(self, sender) -> None:
        if not self._is_current():
            return
        chunk = _chunk_of(sender)
        path = self._path_of.get(id(chunk)) if chunk is not None else None
        if path is not None:
            self._dirty.add(path)
            self._schedule_flush()

    def _on_attribute_changed(self, notice, sender) -> None:
        """A param's value or a chunk's/param's state moved. Re-sync that one node."""
        self._mark_dirty(sender)

    def _on_params_changed(self, notice, sender) -> None:
        """A param was lofted onto or removed from a chunk: its PORTS changed, and sockets
        are only made in _rebuild_tree_body -- so this is a rebuild, not a re-sync.
        """
        self._request_reload_for(sender)

    def _on_chunk_deleted(self, notice, sender) -> None:
        self._request_reload_for(sender)

    def _request_reload_for(self, sender) -> None:
        if not self._is_current():
            return
        chunk = _chunk_of(sender)
        if chunk is not None and id(chunk) in self._path_of:
            self._request_reload()

    def _on_objects_changed(self, notice, sender) -> None:
        """A prim-level resync under the context is a chunk added, removed, enabled or
        disabled by something other than this frontend -- the CLI, undo, another
        frontend on the same stage. Property-level changes are the hot path (a cook
        emits hundreds) and every one of them that matters here arrives as a chunk notice
        too, so those fall through.
        """
        if not sender or self.stage is None or not self._is_current():
            return
        try:
            if sender.GetRootLayer().identifier != self.stage.GetRootLayer().identifier:
                return
            resynced = notice.GetResyncedPaths()
        except (AttributeError, RuntimeError):
            return
        if any(p.IsPrimPath() and p.HasPrefix(self.context) for p in resynced):
            self._request_reload()

    def _request_reload(self) -> None:
        self._reload_pending = True
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        """One flush, next tick. Never synchronous: the notice that gets here is being
        dispatched from inside a param write or a cook, and rebuilding the tree -- or even
        assigning a node's RNA property, whose update callback writes a param -- from
        inside that is the re-entrancy every frontend guards against.
        """
        if self._flush_scheduled:
            return
        self._flush_scheduled = True
        try:
            bpy.app.timers.register(self._timer_flush, first_interval=0.0)
        except Exception:  # pragma: no cover - no timer system (a bare bpy module)
            self._flush_scheduled = False

    def _timer_flush(self):
        self._flush_scheduled = False
        if active_view() is self:
            self.flush()
        return None

    def flush(self) -> None:
        """Bring the tree up to date with everything the notices reported.

        A pending rebuild takes precedence and covers the rest; otherwise each touched
        node is re-mirrored in place (values, lofted ID properties, header colour) and the
        editor is asked to repaint. Callable directly -- a test, or a headless view with
        no event loop to run the timer, drives it by hand.
        """
        self._flush_scheduled = False
        if self._reload_pending:
            self.reload()
            return
        dirty, self._dirty = self._dirty, set()
        if not dirty:
            return
        tree = self.tree
        for path in dirty:
            prim, chunk = self.prims.get(path), self.chunks.get(path)
            node = tree.nodes.get(path) if tree is not None else None
            if node is None or prim is None or not prim.IsValid():
                continue
            if chunk is not None:
                chunk_blender.sync_to_node(node, chunk)
            self.apply_node_state(node, prim, chunk)
            sync_socket_visibility(node)
        tag_redraw()

    # -- selection ---------------------------------------------------------

    def set_selection(self, paths, add: bool = False) -> None:
        """Select these node paths, mirroring onto each chunk's SELECTED bit.

        The bit, not a private attribute: the shared operations read the chunks, so a
        selection kept only here would be invisible to them.
        """
        self.selected_nodes = chunk_graph.apply_selection(
            self.chunks, paths, self.selected_nodes, add)
        if self.tree is not None:
            for node in self.tree.nodes:
                node.select = node.name in self.selected_nodes

    def selected_chunks(self) -> list:
        return chunk_graph.chunks_for(self.chunks, self.selected_nodes)

    def pull_selection_from_tree(self) -> None:
        """Adopt Blender's own selection. Blender owns the box-select and click handling,
        so the frontend follows the editor rather than the other way round.
        """
        if self.tree is None:
            return
        self.set_selection({n.name for n in self.tree.nodes if n.select})

    # -- editing -----------------------------------------------------------

    def delete_selected(self) -> None:
        chosen = self.selected_chunks()
        if not chosen:
            self.status = "nothing to delete"
            return
        removed = chunk_graph.delete_chunks(chosen)
        self.selected_nodes = set()
        self.status = f"deleted {removed}"
        self._recook()
        self.reload()

    def _recook(self) -> None:
        """After a wire, delete, create or paste: recook to what drives this context.

        Only what the active view or Output reaches cooks; chunk_graph.recook_active
        unblocks and cooks what an edit brought into reach and blocks what it took out.
        """
        if failed := chunk_graph.recook_active(self.stage, self.scene, self.context, self.chunks):
            self.status = failed

    def toggle_disable_selected(self) -> None:
        """Flip DISABLED. A disabled chunk stays visible -- muted, in Blender's terms."""
        chunk_graph.toggle_disabled(self.selected_chunks())
        self.reload()

    def collapse_selected_or_all(self) -> None:
        """Collapse the selection, or the whole graph when nothing is selected -- Qt's C.

        One direction for the lot, as Qt: if anything in the set is still expanded they
        all collapse, and only a set that is already entirely collapsed expands again.
        Chunkgroups and views are left alone for Qt's reasons (a collapsed group hides
        the control that expands it; a view has no body and is what happens to be
        selected after the graph auto-creates one).

        The state bit is what is written; `node.hide` -- Blender's own collapse, the one
        its H key toggles -- follows it in apply_node_state, so a collapse made here
        survives a reload and one made in Qt shows up here.
        """
        candidates = [(p, c) for p, c in self.chunks.items()
                      if p in self.prims and not chunk_stage.is_subnet(c)
                      and not chunk_stage.is_view_chunk(c)]
        selected = [(p, c) for p, c in candidates if p in self.selected_nodes]
        targets = selected or candidates
        if not targets:
            return
        collapse = any(not c.has_state(ChunkFlags.State.COLLAPSED) for _p, c in targets)
        for _p, chunk in targets:
            if collapse:
                chunk.set_state(ChunkFlags.State.COLLAPSED)
            else:
                chunk.unset_state(ChunkFlags.State.COLLAPSED)
        self.flush()

    def join(self, a, b) -> None:
        """Wire a to b -- through a param picked from a list when an end is a "+".

        With a "+" at either end the engine says what to offer (plus_step); the choice
        waits in pending_pick for choose(), which joins it on and may ask again when both
        ends were a "+". The search popup only shows it.
        """
        step = chunk_graph.plus_step(self.chunks, a, b)
        if step is None:
            if chunk_graph.connect(self.stage, a, b, self.chunks) is not None:
                _reload_after_connect(self)
            return
        choices, other = step
        if not choices:
            self.status = "nothing there takes that wire"
            return
        self.pending_pick = (choices, other)
        _open_pick_popup()

    def choose(self, ref) -> None:
        """Join the picked param to whatever waited on it (see join)."""
        if self.pending_pick is None:
            return
        _choices, other = self.pending_pick
        self.pending_pick = None
        self.join(ref, other)

    def snapshot_selection(self):
        return chunk_graph.snapshot_selection(
            self.chunks, self.prims, self.positions, self.scene, self.stage,
            self.selected_nodes)

    def copy_selection(self) -> None:
        nodes, edges = self.snapshot_selection()
        if not nodes:
            self.status = "nothing to copy"
            return
        # The engine's own lists, so a selection copied here pastes in Qt or Flet.
        self.status = f"copied {chunk_graph.copy_to_clipboard(nodes, edges)}"

    def cut_selection(self) -> None:
        nodes, edges = self.snapshot_selection()
        if not nodes:
            self.status = "nothing to cut"
            return
        count = chunk_graph.copy_to_clipboard(nodes, edges)
        self.delete_selected()
        self.status = f"cut {count}"

    def paste_at(self, x: float, y: float, nodes=None, edges=None) -> None:
        """Materialize the clipboard and re-wire it.

        Synchronous, like Flet and unlike Qt: Blender builds nodes immediately, so there is
        no deferred-proxy window to wire around.

        Wiring is chunk_graph.rewire_pasted, which reads the real port types off the stage
        so copied param wires survive alongside exec wires.
        """
        nodes = chunk_graph.node_clipboard if nodes is None else nodes
        edges = chunk_graph.edge_clipboard if edges is None else edges
        if not nodes or self.scene is None:
            self.status = "clipboard empty"
            return

        made = chunk_graph.paste_nodes(
            self.scene, self.stage, nodes, edges,
            to_usd=lambda rx, ry: (x + rx, y + ry),
            nest=chunk_graph.nest_target(chunk_graph.context_chunk(self.scene, self.chunks, self.context)),
            chunks=self.chunks)

        self.chunks = {chunk_stage.get_chunk_prim_path(c): c for c in self.scene.get_chunks()}
        self._recook()
        self.reload()
        self.set_selection({chunk_stage.get_chunk_prim_path(c) for c in made.values()})
        self.status = f"pasted {len(made)}"

    def duplicate_selection(self) -> None:
        """Copy and paste at an offset without touching the clipboard, so duplicating never
        clobbers what was last explicitly copied.
        """
        nodes, edges = self.snapshot_selection()
        if not nodes:
            self.status = "nothing to duplicate"
            return
        origin = chunk_graph.selection_origin(self.positions, self.selected_nodes)
        if origin is None:
            return
        # Minus, where the canvas frontends add: Blender's node editor is Y-up like USD.
        self.paste_at(origin[0] + 40, origin[1] - 40, nodes, edges)

    def group_selection(self) -> None:
        """Put the selection in a chunkgroup that takes its place in the exec chain.

        The whole sequence is chunk_graph.group_into_chunkgroup, shared with Flet and
        ImGui. What stood here instead drove itself through dive_into/paste_at/dive_out and
        re-wired the boundary with add_dependency -- a second implementation of the same
        idea, and the one that drifted.
        """
        group_path, count = chunk_graph.group_into_chunkgroup(
            self.stage, self.scene, self.chunks, self.prims, self.positions,
            self.selected_nodes, to_usd=lambda x, y: (x, y))
        if not group_path:
            self.status = "nothing to group"
            return
        self.chunks = {chunk_stage.get_chunk_prim_path(c): c for c in self.scene.get_chunks()}
        self._recook()
        self.reload()
        self.set_selection([group_path] if group_path in self.prims else ())
        self.status = f"grouped {count}"

    def create_chunk(self, entry, x: float, y: float) -> None:
        """Create one registry MenuEntry's chunk(s) at a layout point.

        Takes an entry rather than a module name so a chunk's `variants` work, and takes no
        menu: how the user picked it is Blender's business (Shift+A).
        """
        created, error = chunk_graph.create_from_entry(
            self.scene, entry, (x, y),
            chunk_graph.nest_target(chunk_graph.context_chunk(self.scene, self.chunks, self.context)))
        if error:
            self.status = error
        if not created:
            return
        self.chunks = {chunk_stage.get_chunk_prim_path(c): c
                       for c in self.scene.get_chunks()}

        made = [chunk_stage.get_chunk_prim_path(c) for c in created]
        # BEFORE the rebuild, not after it. add_chunk routes a context-less chunk into the
        # scene's default chunkgroup, which is not the graph being shown when a brand-new
        # network opens at CHUNKS_ROOT -- so follow the chunk to where it actually landed,
        # or Shift+A appears to do nothing at all on a new network. Doing it after a first
        # rebuild would also run `reload` once at the old context, and the view it creates
        # records its slot on the CONTEXT prim: the slot would land on /chunks while the
        # view itself lives in /chunks/ChunkGroup, and the graph would cook nothing.
        landed = made[0].rsplit("/", 1)[0]
        if landed != self.context:
            self.context = landed
        self._recook()
        self.reload()

        self.set_selection(set(made))
        self.status = f"created {created[0].name}"

    # -- navigation --------------------------------------------------------

    def can_dive(self, path: str) -> bool:
        """Keyed on is_subnet, never on the CONTEXT flag: a chunk-in-chunk carries
        CONTEXT too and is an implementation detail, not a subnet.
        """
        return chunk_graph.can_dive(self.chunks, path)

    def dive_into(self, path: str) -> None:
        if not self.can_dive(path):
            return
        self.context = path
        self.selected_nodes = set()
        self.status = f"in {path.rsplit('/', 1)[-1]}"
        self.reload()
        self._cook_shown_graph()

    def dive_out(self) -> None:
        """Back up to the enclosing graph.

        parent_context walks up to a path that actually holds chunks. A single rsplit,
        which is what stood here, lands on the group's parent scope -- a context only
        sometimes, and an empty graph the rest of the time.
        """
        parent = chunk_graph.parent_context(self.context,
                                            chunk_graph.contexts(self.stage))
        if parent is None:
            self.status = "already at the top"
            return
        inner, self.context = self.context, parent
        self.selected_nodes = set()
        self.reload()
        self._cook_shown_graph()
        if inner in self.prims:
            self.set_selection([inner])

    def _cook_shown_graph(self) -> None:
        """Cook for the graph now on screen, with Qt's recook_focus.

        Inside a group that keeps the group's upstream, cooks the group's own chain and
        mutes what is downstream of it; back out, the outer graph cooks again. Without it a
        dive changed only the tree, and the viewport kept showing the outer graph's cook.
        """
        context_chunk = chunk_graph.context_chunk(self.scene, self.chunks, self.context)
        slot = self.active_slot()
        view_chunk = (chunk_stage.find_view_chunk_for_slot(
            slot, self.stage, self.scene, context_chunk) if slot else None)
        try:
            chunk_stage.recook_focus(self.stage, self.scene, context_chunk, view_chunk)
        except Exception as exc:  # noqa: BLE001 - a chunk's own code can raise anything
            logger.exception("recook failed after changing graph")
            self.status = f"recook failed: {type(exc).__name__}"

    # -- views -------------------------------------------------------------

    def active_slot(self) -> int:
        return chunk_graph.read_active_slot(self.stage, self.context)

    def set_active_slot(self, slot: int) -> None:
        """Switch which view drives execution, and recook for it.

        Resolve BEFORE writing: writing first persisted a slot with no view behind it, so
        the attribute said 3, nothing cooked, and the file carried that to the next open.
        Slot 0 is the Output, and is refused in a graph with none, as Qt refuses it: it
        would cook nothing.
        """
        if slot == 0 and not chunk_stage.graph_outputs(
                self.stage, self.scene,
                chunk_graph.context_chunk(self.scene, self.chunks, self.context)):
            self.status = "no Output in this graph"
            return
        changed, self.status = chunk_graph.activate_view_slot(
            self.stage, self.scene, self.context, self.chunks, slot)
        if changed and not self.status.startswith("recook failed"):
            # Named as the key and the node show it, not by the stored slot: pressing 2
            # and reading "view 1 active" looked like the wrong view had been picked.
            self.status = ("Output active" if slot == 0
                           else f"View {chunk_graph.view_number(slot)} active")
        if changed:
            self.reload()

    def create_view_node(self) -> None:
        """Wire a view to the selected node, or to the end of the chain -- Qt's V.

        Creation is chunk_graph.new_view_slot, shared with the other frontends: slot
        allocation is scoped to the container, and a view already wired to the target is
        reused rather than duplicated. The new view becomes the active one, as it does
        everywhere else -- pressing V means "show me this".
        """
        if self.scene is None:
            self.status = "no chunk session"
            return
        chosen = [p for p in self.selected_nodes if p in self.prims]
        slot = chunk_graph.new_view_slot(self.stage, self.scene,
                                         chunk_graph.context_chunk(self.scene, self.chunks, self.context),
                                         chosen[0] if chosen else None)
        if slot is None:
            self.status = "no view created"
            return
        self.chunks = {chunk_stage.get_chunk_prim_path(c): c for c in self.scene.get_chunks()}
        self.reload()
        self.set_active_slot(slot)
        self.status = f"View {chunk_graph.view_number(slot)}"

    # -- layout ------------------------------------------------------------

    def _write_position(self, path: str, x: float, y: float) -> None:
        """Persist a node position in the coordinates USD stores.

        Blender's node editor is Y-up like USD, so unlike the Qt and Flet canvases there is
        no flip to undo here -- see _SCALE.
        """
        chunk_graph.write_position(self.prims.get(path), x, y, owner=self.chunks.get(path))

    def pull_positions_from_tree(self) -> None:
        """Write back positions after the user has dragged nodes in Blender's editor.

        Blender owns dragging, so there is no move event to hook; this is called when the
        graph is saved or closed. Without it a layout done in Blender is lost, because the
        tree is a view and the next reload overwrites it from USD.
        """
        if self.tree is None:
            return
        for node in self.tree.nodes:
            # A locked node's position belongs to the layer it came from. Qt refuses the
            # drag; Blender cannot, so the drag is dropped here instead and the next
            # reload puts the node back where its file has it.
            if node.name in self.prims and node.name not in self.locked:
                self._write_position(node.name, node.location[0] / _SCALE,
                                     node.location[1] / _SCALE)

    def _node_size(self, path: str) -> tuple[float, float]:
        """A node's size in USD (layout) units.

        Blender's drawn size when it has one -- node.dimensions is filled in by the draw
        and is zero until then (headless, or a node the editor has not shown yet), where
        the engine's default stands in. Qt measures its items the same way; the default
        is 200 x 90 and a Transform here draws at 260 x 671, so laying out from the
        default stacked nodes through each other. Read back through _SCALE, since the
        layout is computed in USD units and the result is scaled up on the way out.
        """
        node = self.tree.nodes.get(path) if self.tree is not None else None
        if node is not None and node.dimensions.x > 0 and node.dimensions.y > 0:
            return node.dimensions.x / _SCALE, node.dimensions.y / _SCALE
        return chunk_graph.node_size(self.prims[path])

    def auto_layout(self) -> None:
        """Lay out the selection's connected subgraph, or everything.

        The computation is `chunk_graph.layout_graph`, shared with the other three
        frontends; what is left here is writing the result back, which is the part that
        genuinely differs -- Blender stores position on the node and USD stores it on the
        prim, so both have to be set.
        """
        if not self.prims:
            return
        placed = chunk_graph.layout_graph(
            self.prims,
            size_of=self._node_size,
            current_pos=lambda path: self.positions.get(path, (0.0, 0.0)),
            seeds=[p for p in self.selected_nodes if p in self.prims] or None,
            # As Qt: a node from a referenced layer stays where its file put it and the
            # rest are laid out around it.
            is_locked=lambda path: path in self.locked,
        )
        for path, (x, y) in placed.items():
            self.positions[path] = (x, y)
            self._write_position(path, x, y)
            node = self.tree.nodes.get(path) if self.tree else None
            if node is not None:
                node.location = (x * _SCALE, y * _SCALE)

    # -- viewport ----------------------------------------------------------

    def frame(self, paths) -> None:
        """Fit those nodes in the editor.

        Blender frames its own *selection*, so this selects and asks the editor to view it
        -- rather than computing a bounding box and setting a transform the way a
        custom canvas must.
        """
        paths = set(paths) or set(self.prims)
        self.set_selection(paths)
        try:
            bpy.ops.node.view_selected()
        except RuntimeError:  # pragma: no cover - needs a node editor context
            self.status = "no node editor to frame in"


# ---------------------------------------------------------------------------
# The Add menu
#
# Shift+A in a node editor opens NODE_MT_add, so that is what this appends to -- rather
# than inventing a separate way in. The categories are drawn straight into it, the way the
# Geometry Node Editor draws its own, so the menu a Blender user already knows is the menu
# the chunk library appears in.
#
# The shape comes from chunk_registry.MenuEntry.namespace, which is a menu path with the
# label last, so the submenu tree is that path and nothing here decides the taxonomy.
# ---------------------------------------------------------------------------

#: The entries the menu was last built from. The operator carries an index into this rather
#: than a module name, because a MenuEntry is a module *plus* its variant presets -- two
#: entries can share a module and differ only in what they preset, so the module name does
#: not identify what to create.
_menu_entries: list = []
_menu_classes: dict = {}


def _menu_id(prefix: tuple) -> str:
    slug = "_".join(prefix) if prefix else "root"
    return "USDCHUNKS_MT_add_" + re.sub(r"[^0-9A-Za-z_]", "_", slug)


def _menu_tree(entries) -> dict:
    """The entries as a tree of {"subs": {name: node}, "items": [(label, index)]}."""
    root: dict = {"subs": {}, "items": []}
    for index, entry in enumerate(entries):
        namespace = tuple(entry.namespace) or (entry.module.module_name,)
        node = root
        for part in namespace[:-1]:
            node = node["subs"].setdefault(part, {"subs": {}, "items": []})
        node["items"].append((namespace[-1], index))
    return root


def _draw_entries(layout, node: dict, prefix: tuple) -> None:
    """Submenus first, then leaves, each sorted -- so the menu is stable between sessions.

    Registry order follows filesystem discovery, which is not something a user should have
    to learn twice.
    """
    for name in sorted(node["subs"]):
        layout.menu(_menu_id(prefix + (name,)), text=name)
    for label, index in sorted(node["items"]):
        layout.operator(USDCHUNKS_OT_add_node.bl_idname, text=label).entry_index = index


class USDCHUNKS_OT_add_node(bpy.types.Operator):
    """Create a chunk from the Add menu, at the Add cursor."""

    bl_idname = "usd_chunks.add_node"
    bl_label = "Add Chunk"
    bl_options = {"REGISTER", "UNDO"}

    entry_index: bpy.props.IntProperty(default=-1)

    @classmethod
    def poll(cls, context):
        return getattr(getattr(context, "space_data", None), "tree_type", "") == TREE_ID

    def execute(self, context):
        view = active_view()
        if view is None:
            self.report({"WARNING"}, "no chunk network open")
            return {"CANCELLED"}
        if not 0 <= self.entry_index < len(_menu_entries):
            # The index is only meaningful against the list the menu was drawn from. A
            # rebuild between draw and click is unlikely but not impossible, and creating
            # the wrong chunk would be worse than declining.
            self.report({"WARNING"}, "that menu entry is stale; reopen the Add menu")
            return {"CANCELLED"}

        # Blender keeps the Add cursor in node space, the same space node.location uses, so
        # it converts back to USD exactly as pull_positions_from_tree does.
        cursor = getattr(context.space_data, "cursor_location", None)
        x, y = (cursor[0] / _SCALE, cursor[1] / _SCALE) if cursor else (0.0, 0.0)
        view.create_chunk(_menu_entries[self.entry_index], x, y)
        if view.status.startswith("create failed") or view.status.startswith("no chunk"):
            self.report({"WARNING"}, view.status)
            return {"CANCELLED"}
        self.report({"INFO"}, view.status)
        return {"FINISHED"}


def _register_menu(node: dict, prefix: tuple) -> None:
    """Register a Menu class for this node, children first.

    Children first because a parent draws `layout.menu(child_id)` and Blender resolves that
    id when the parent is drawn -- a missing child is a silent empty row.
    """
    for name, sub in node["subs"].items():
        _register_menu(sub, prefix + (name,))
    idname = _menu_id(prefix)
    cls = type(idname, (bpy.types.Menu,), {
        "bl_idname": idname,
        "bl_label": prefix[-1] if prefix else "USDChunks",
        "_node": node,
        "_prefix": prefix,
        "draw": lambda self, context: _draw_entries(self.layout, self._node, self._prefix),
    })
    _register_once(cls)
    _menu_classes[prefix] = cls


def build_add_menu() -> int:
    """(Re)build the Add menu from the registry. Returns how many entries it holds.

    Called wherever the node classes are built, because both read the same registry and a
    menu listing a chunk with no registered node class would offer something that cannot be
    created.
    """
    unregister_add_menu()
    _menu_entries[:] = chunk_graph.menu_entries()
    _register_menu(_menu_tree(_menu_entries), ())
    return len(_menu_entries)


def unregister_add_menu() -> None:
    for cls in list(_menu_classes.values()):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:  # pragma: no cover - already gone
            pass
    _menu_classes.clear()
    _menu_entries.clear()


def _draw_add_menu(self, context) -> None:
    """Appended to NODE_MT_add. Draws nothing outside a chunk tree."""
    if getattr(getattr(context, "space_data", None), "tree_type", "") != TREE_ID:
        return
    root = _menu_classes.get(())
    if root is None:
        self.layout.label(text="no chunks registered")
        return
    _draw_entries(self.layout, root._node, ())


# ---------------------------------------------------------------------------
# Add-on registration
# ---------------------------------------------------------------------------

_CLASSES = (UsdChunksNodeTree, UsdChunksSocket, USDCHUNKS_OT_param_button,
            USDCHUNKS_OT_node_help, USDCHUNKS_OT_pin_prim, USDCHUNKS_OT_search_menu, USDCHUNKS_OT_pick_menu_item,
            USDCHUNKS_OT_pick_plus, USDCHUNKS_OT_revert_override,
            USDCHUNKS_OT_list_resize, USDCHUNKS_OT_add_node)


def _register_once(cls) -> None:
    """Register cls unless Blender already has it.

    Idempotent because registering twice is a ValueError, and this is called more than once
    in practice: re-enabling the add-on, a script reload, and a test suite that builds a
    fresh graph per test all land here with the classes already in place. Checking
    `is_registered` is cheaper and clearer than catching the error, which would also
    swallow a genuinely malformed class.
    """
    if getattr(cls, "is_registered", False):
        return
    bpy.utils.register_class(cls)


def register() -> None:
    for cls in _CLASSES:
        _register_once(cls)
    _register_once(UsdChunksNodeBase)
    made = register_chunk_types()
    logger.info("usd_chunks: %d chunk node type(s) registered", len(made))
    bpy.types.NODE_MT_add.append(_draw_add_menu)


def unregister() -> None:
    chunk_blender.release_previews()
    try:
        bpy.types.NODE_MT_add.remove(_draw_add_menu)
    except Exception:  # pragma: no cover - never appended
        pass
    unregister_add_menu()
    unregister_chunk_types()
    try:
        bpy.utils.unregister_class(UsdChunksNodeBase)
    except Exception:  # pragma: no cover
        pass
    for cls in reversed(_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:  # pragma: no cover
            pass
