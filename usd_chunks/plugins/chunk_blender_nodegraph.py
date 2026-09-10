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

import bpy
from usd_chunks import chunk_registry
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.plugins import chunk_blender, chunk_graph, chunk_usd

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
_SCALE = 1.0

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
    """The tree type. Its whole job is to exist, so the node editor has something to draw."""

    bl_idname = TREE_ID
    bl_label = "USDChunks Editor"
    bl_icon = "NODETREE"


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
    for param in chunk_graph.iter_connectable_params(chunk):
        if param.name == socket.name:
            return param
    return None


def socket_draws_value(socket, param) -> bool:
    """Whether this socket draws the param's control rather than just its name.

    Only an unlinked input does. An output cannot: Blender lays a node out as outputs, then
    `draw_buttons`, then inputs, and exposes neither an alignment flag nor a socket
    position, so an output row can never sit beside the field it reports -- it can only say
    which param it is. A linked input has a wire for its value, as in any Blender node.
    """
    if param is None or socket.is_output or socket.is_linked:
        return False
    return not param.has_state(ChunkFlags.State.HIDDEN)


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
            names.add(param.name)
        else:
            names.update(p.name for p in chunk_blender.iter_drawable(param))
    return names


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

    def draw_buttons(self, context, layout):
        # Params with an input socket are drawn by that socket instead, beside their port.
        chunk_blender.draw_params(self, layout, self.get_chunk(),
                                  skip=socket_drawn_params(self))


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
        param = chunk.get_param(self.param_name) if chunk else None
        if param is None:
            self.report({"WARNING"}, "no such param: %s" % self.param_name)
            return {"CANCELLED"}
        param.click()
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
            logger.debug("could not register a node class for %s", name)
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


def active_view():
    """The view a node should ask for its chunk.

    Module-level because a Blender node is constructed by Blender, not by us, so there is
    no constructor to hand a view to. One graph editor at a time is the working assumption;
    a second tree would need this keyed by tree name.
    """
    return _active_view


class BlenderGraphView:
    """`chunk_graph.GraphFrontend` over a Blender node tree.

    Holds `context` (the prim path of the graph being shown) and `selected_nodes` (a set of
    prim paths), as the Protocol documents.
    """

    def __init__(self, stage, scene, tree=None, context=chunk_graph.CHUNKS_ROOT):
        self.stage = stage
        self.scene = scene
        self.tree_name = ""
        self.tree = tree
        self.context = context
        self.chunks: dict = {}
        self.prims: dict = {}
        self.ports: list = []
        self.edges: list = []
        self.positions: dict = {}
        self.selected_nodes: set = set()
        self.status = ""
        #: Guards ensure_view against the reload it can trigger through itself.
        self._ensuring_view = False
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

        Falls back to any chunk tree, so a renamed tree self-heals rather than leaving the
        view permanently blind to a tree that is plainly there.
        """
        if not self.tree_name:
            # Never given a tree -- a headless view, driving USD with nothing to draw into.
            # Adopting a stray tree here would make such a view start rebuilding one.
            return None
        tree = bpy.data.node_groups.get(self.tree_name)
        if tree is None or tree.bl_idname != TREE_ID:
            tree = next((t for t in bpy.data.node_groups if t.bl_idname == TREE_ID), None)
            if tree is not None:
                self.tree_name = tree.name
        return tree

    @tree.setter
    def tree(self, tree) -> None:
        self.tree_name = tree.name if tree is not None else ""

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
            slot, activate = chunk_graph.default_view_slot(
                self.stage, self.scene, self.context, self.chunks.get(self.context))
            if slot is None:
                return
            self.chunks = {chunk_usd.get_chunk_prim_path(c): c
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
        # drag done since the last reload was silently discarded the next time anything
        # triggered one: Shift+A, a view-slot change, even a recook. Reported as "I moved
        # a node and dropping a new one put everything back". Pulled against the OLD
        # self.prims/tree, i.e. exactly the nodes about to be thrown away.
        self.pull_positions_from_tree()
        self.ensure_view()
        self.prims, self.ports, self.edges = chunk_graph.read_graph(
            self.stage, self.context, self.chunks)
        self.positions = {p: chunk_graph.node_position(prim)
                          for p, prim in self.prims.items()}
        if self.tree is not None:
            self._rebuild_tree()

    def _rebuild_tree(self) -> None:
        """Discard and re-make the nodes.

        Rebuilt wholesale rather than diffed because USD is the model and a diff would have
        to be right about identity, position, ports *and* links to be worth it -- and being
        subtly wrong about any of them reintroduces exactly the drift this design exists to
        prevent. Graphs here are tens of nodes, not thousands.
        """
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
            x, y = self.positions.get(path, (0.0, 0.0))
            node.location = (x * _SCALE, y * _SCALE)
            # Blender's own mute flag, so a disabled chunk reads as disabled to a Blender
            # user without inventing a second convention for it.
            node.mute = chunk_graph.is_disabled(prim)
            if chunk is not None:
                chunk_blender.sync_to_node(node, chunk)
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
        so the frontend follows the editor rather than the other way round."""
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
        self.status = "deleted %d" % removed
        self.reload()

    def toggle_disable_selected(self) -> None:
        """Flip DISABLED. A disabled chunk stays visible -- muted, in Blender's terms."""
        chunk_graph.toggle_disabled(self.selected_chunks())
        self.reload()

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
        self.status = "copied %d" % chunk_graph.copy_to_clipboard(nodes, edges)

    def cut_selection(self) -> None:
        nodes, edges = self.snapshot_selection()
        if not nodes:
            self.status = "nothing to cut"
            return
        count = chunk_graph.copy_to_clipboard(nodes, edges)
        self.delete_selected()
        self.status = "cut %d" % count

    def paste_at(self, x: float, y: float, nodes=None, edges=None) -> None:
        """Materialize the clipboard and re-wire it.

        Synchronous, like Flet and unlike Qt: Blender builds nodes immediately, so there is
        no deferred-proxy window to wire around.

        Wiring is chunk_graph.rewire_pasted. The version that lived here built PortRefs by
        hand and typed every one of them PORT_EXEC, which silently dropped every copied
        param wire; the engine reads the real ports off the stage instead.
        """
        nodes = chunk_graph.node_clipboard if nodes is None else nodes
        edges = chunk_graph.edge_clipboard if edges is None else edges
        if not nodes or self.scene is None:
            self.status = "clipboard empty"
            return

        made = chunk_graph.paste_nodes(
            self.scene, self.stage, nodes, edges,
            to_usd=lambda rx, ry: (x + rx, y + ry),
            nest=chunk_graph.nest_target(self.chunks.get(self.context)),
            chunks=self.chunks)

        self.chunks = {chunk_usd.get_chunk_prim_path(c): c for c in self.scene.get_chunks()}
        self.reload()
        self.set_selection({chunk_usd.get_chunk_prim_path(c) for c in made.values()})
        self.status = "pasted %d" % len(made)

    def duplicate_selection(self) -> None:
        """Copy and paste at an offset without touching the clipboard, so duplicating never
        clobbers what was last explicitly copied."""
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
        self.chunks = {chunk_usd.get_chunk_prim_path(c): c for c in self.scene.get_chunks()}
        self.reload()
        self.set_selection([group_path] if group_path in self.prims else ())
        self.status = "grouped %d" % count

    def create_chunk(self, entry, x: float, y: float) -> None:
        """Create one registry MenuEntry's chunk(s) at a layout point.

        Takes an entry rather than a module name so a chunk's `variants` work, and takes no
        menu: how the user picked it is Blender's business (Shift+A).
        """
        created, error = chunk_graph.create_from_entry(
            self.scene, entry, (x, y),
            chunk_graph.nest_target(self.chunks.get(self.context)))
        if error:
            self.status = error
        if not created:
            return
        self.chunks = {chunk_usd.get_chunk_prim_path(c): c
                       for c in self.scene.get_chunks()}

        made = [chunk_usd.get_chunk_prim_path(c) for c in created]
        # BEFORE the rebuild, not after it. add_chunk routes a context-less chunk into the
        # scene's default chunkgroup, which is not the graph being shown when a brand-new
        # network opens at CHUNKS_ROOT -- so follow the chunk to where it actually landed,
        # or Shift+A appears to do nothing at all on a new network. Doing it after a first
        # rebuild also meant `reload` ran once at the old context, and the view it creates
        # records its slot on the CONTEXT prim: the slot was stamped on /chunks while the
        # view itself lived in /chunks/ChunkGroup, so the graph had a view node and still
        # cooked nothing.
        landed = made[0].rsplit("/", 1)[0]
        if landed != self.context:
            self.context = landed
        self.reload()

        self.set_selection(set(made))
        self.status = "created %s" % created[0].name

    # -- navigation --------------------------------------------------------

    def can_dive(self, path: str) -> bool:
        """Keyed on is_chunkgroup, never on the CONTEXT flag: a chunk-in-chunk carries
        CONTEXT too and is an implementation detail, not a subnet."""
        return chunk_graph.can_dive(self.chunks, path)

    def dive_into(self, path: str) -> None:
        if not self.can_dive(path):
            return
        self.context = path
        self.selected_nodes = set()
        self.status = "in %s" % path.rsplit("/", 1)[-1]
        self.reload()

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
        if inner in self.prims:
            self.set_selection([inner])

    # -- views -------------------------------------------------------------

    def _context_prim(self):
        return chunk_graph.context_prim(self.stage, self.context)

    def active_slot(self) -> int:
        return chunk_graph.read_active_slot(self.stage, self.context)

    def set_active_slot(self, slot: int) -> None:
        """Switch which view drives execution, and recook for it.

        Resolve BEFORE writing: writing first persisted a slot with no view behind it, so
        the attribute said 3, nothing cooked, and the file carried that to the next open.
        """
        changed, self.status = chunk_graph.activate_view_slot(
            self.stage, self.scene, self.context, self.chunks, slot)
        if changed:
            self.reload()

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
            if node.name in self.prims:
                self._write_position(node.name, node.location[0] / _SCALE,
                                     node.location[1] / _SCALE)

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
            size_of=lambda path: chunk_graph.node_size(self.prims[path]),
            current_pos=lambda path: self.positions.get(path, (0.0, 0.0)),
            seeds=[p for p in self.selected_nodes if p in self.prims] or None,
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
            USDCHUNKS_OT_add_node)


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
