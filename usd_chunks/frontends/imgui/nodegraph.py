"""A Dear ImGui view of a chunk network -- the third frontend over one engine.

    python -m usd_chunks.frontends.imgui.nodegraph <network.usda> [context_prim_path]

Written to test whether the engine is genuinely frontend-agnostic or merely Qt-and-Flet
agnostic. ImGui is a different paradigm from both: immediate mode, no widget objects, no
retained scene. See docs/graph-frontends.md.

What this frontend does NOT contain, compared with the Flet one, is the interesting part:

- no `port_at` / `node_at` -- imgui_node_editor owns hit-testing
- no zoom, pan or canvas transform -- the editor owns navigation
- no curve maths for drawing wires or for splice detection
- no widget classes, no notice callbacks, no focus tracker object; every frame re-reads the
  params, so a change made anywhere shows up on the next frame by construction

What it must still do is everything the engine does not: identity for the editor (which
speaks integer ids, not prim paths), and the sixteen contract verbs.
"""
from __future__ import annotations

import contextlib
import inspect
import logging
import sys

from imgui_bundle import hello_imgui, imgui, imgui_node_editor as ed
from pxr import Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_registry import ChunkLayerManager
from usd_chunks.frontends.imgui import factory as chunk_imgui
from usd_chunks import chunk_graph
from usd_chunks import chunk_stage
from usd_chunks.chunk_stage import get_chunk_prim_path

logger = logging.getLogger(__name__)

NODE_W = 220.0
SOCKET_R = 5.0          # radius of a pin's dot
SOCKET_BOX = 14.0       # the square a socket reserves, so labels line up
BODY_W = 300.0          # content width of a node, and what an output pin aligns to
SIDE_PAD = 8.0          # horizontal breathing room INSIDE the pin row, not on the border

#: Port colours, matching the Flet frontend so the same wire reads the same in both.
PORT_COLOUR = {
    chunk_graph.PORT_EXEC: (0.79, 0.79, 0.79),
    chunk_graph.PORT_FLOAT: (0.42, 0.71, 0.98),
    chunk_graph.PORT_INT: (0.50, 0.85, 0.62),
    chunk_graph.PORT_BOOL: (0.90, 0.55, 0.55),
    chunk_graph.PORT_STRING: (0.85, 0.75, 0.45),
    chunk_graph.PORT_COLOR: (0.85, 0.55, 0.85),
    chunk_graph.PORT_VECTOR: (0.55, 0.70, 0.90),
}
_FALLBACK_COLOUR = (0.6, 0.6, 0.6)


def _socket(port_type: str, connected: bool) -> None:
    """Draw a pin's dot and reserve its space.

    A pin's clickable area is whatever is drawn between begin_pin and end_pin, and this
    frontend drew only a text label -- so there was nothing round to aim at and the wire
    attached to the text baseline. Hence the dot, plus a pivot below that tells the editor
    to anchor links to its centre.

    Filled means wired, hollow means free -- the same read as every node editor, and it
    costs nothing here because the engine already knows which ports have edges.
    """
    red, green, blue = PORT_COLOUR.get(port_type, _FALLBACK_COLOUR)
    colour = imgui.IM_COL32(int(red * 255), int(green * 255), int(blue * 255), 255)
    origin = imgui.get_cursor_screen_pos()
    imgui.dummy(imgui.ImVec2(SOCKET_BOX, SOCKET_BOX))
    centre = imgui.ImVec2(origin.x + SOCKET_BOX * 0.5, origin.y + SOCKET_BOX * 0.5)
    draw = imgui.get_window_draw_list()
    if connected:
        draw.add_circle_filled(centre, SOCKET_R, colour)
    else:
        draw.add_circle(centre, SOCKET_R, colour, 0, 1.6)


class _Scene(SceneSender):
    """Enough of a host for the registry to load chunks against a stage."""

    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []

    def should_install_package(self, package):
        return False


class _Ids:
    """Stable integer ids for prim paths and ports.

    imgui_node_editor addresses everything by int. The mapping has to survive a reload --
    the editor keeps its own node positions and selection keyed by these -- so ids are
    handed out once per string and never reused, rather than being an index into a list
    that reorders.
    """

    def __init__(self):
        self._to_int: dict = {}
        self._to_key: dict = {}
        self._next = 1  # 0 means "no id" to the editor

    def get(self, key: str) -> int:
        if key not in self._to_int:
            self._to_int[key] = self._next
            self._to_key[self._next] = key
            self._next += 1
        return self._to_int[key]

    def key(self, value) -> str | None:
        return self._to_key.get(int(value.id()) if hasattr(value, "id") else int(value))


class ImGuiGraphView:
    """Draws a chunk network with imgui_node_editor and implements GraphFrontend."""

    def __init__(self, network: str, context: str | None = None):
        self.network = network
        # Held for the life of the view: a Usd.Prim does not keep its stage alive.
        self.stage = Usd.Stage.Open(network)
        self.chunks: dict = {}
        self.session_error = ""
        self.scene = None
        try:
            self.scene = _Scene(self.stage)
            chunk_registry.ChunkRegistry().initialize(self.scene, "imgui")
            ChunkLayerManager().initialize(self.stage)
            chunk_stage.ChunkStageSync().initialize(self.stage, self.scene)
            # ChunkStageSync.initialize loads the file's chunks itself, with the dedup set
            # that stops a prim loading twice. A second load_chunks here had no such set
            # and loaded every chunk again: each got a fresh prim (Reference -> Reference8,
            # View1 -> View11) laid out in a row off to the right of the real network.
            self.chunks = {get_chunk_prim_path(c): c for c in self.scene.get_chunks()}
        except (AttributeError, KeyError, RuntimeError, OSError, ValueError) as exc:
            self.session_error = f"no chunk session ({type(exc).__name__}); drawing only"

        self.available = chunk_graph.contexts(self.stage)
        self.context = context or (self.available[0] if self.available
                                   else chunk_graph.CHUNKS_ROOT)
        self.ids = _Ids()
        self.status = ""
        self.selected_nodes: set[str] = set()
        self._placed: set[str] = set()  # nodes whose position has been pushed to the editor
        #: Show every data pin, or only the ones carrying a wire. Off by default: a
        #: Transform exposes 28 ports and 2 of them are wired, so drawing all of them made
        #: the node twenty-four rows taller than its own body. Qt draws the equivalent
        #: line with its "Show param connections" toggle.
        self.show_all_pins = False
        self._editor = None
        self.reload()
        self._ensure_view()

    def _ensure_view(self) -> None:
        """Give the context a view on open.

        Without one nothing cooks, so a network opened here would look right and do
        nothing.
        """
        slot, activate = chunk_graph.default_view_slot(
            self.stage, self.scene, self.context, self.chunks.get(self.context))
        if slot is None:
            return
        self.chunks = {get_chunk_prim_path(c): c for c in self.scene.get_chunks()}
        self.reload()
        if activate:
            self.set_active_slot(slot)

    # -- reading -----------------------------------------------------------

    def reload(self):
        self.available = chunk_graph.contexts(self.stage)
        self.prims, self.ports, self.edges = chunk_graph.read_graph(
            self.stage, self.context, self.chunks)
        # Positions are pushed to the editor once per node; after that the editor owns
        # them and we read back on change, which is what makes its own drag work.
        self._placed -= set(self._placed) - set(self.prims)

    @property
    def positions(self) -> dict:
        """Canvas positions, read from the editor when it has them.

        The editor is the source of truth while a session is running -- it moves nodes
        itself -- so this asks it, and falls back to USD for a node it has not seen yet.
        """
        out = {}
        for path in self.prims:
            if path in self._placed and self._editor is not None:
                point = ed.get_node_position(ed.NodeId(self.ids.get(path)))
                out[path] = (point.x, point.y)
            else:
                out[path] = chunk_graph.graph_position(self.prims[path])
        return out

    # -- drawing -----------------------------------------------------------

    def draw(self):
        """One frame. Called by hello_imgui's render loop."""
        # A file dialog runs outside the frame loop; this applies one that has finished.
        chunk_imgui.poll_dialogs()
        imgui.text(self.summary())
        imgui.separator()
        self._shortcuts()

        ed.set_current_editor(self._editor)
        ed.begin("chunks")
        for path, prim in self.prims.items():
            self._draw_node(path, prim)
        for edge in self.edges:
            self._draw_edge(edge)
        self._handle_creation()
        self._handle_deletion()
        ed.end()

        self.selected_nodes = {
            key for key in (self.ids.key(n) for n in ed.get_selected_nodes()) if key
        }

    def _draw_node(self, path: str, prim):
        node_id = ed.NodeId(self.ids.get(path))
        if path not in self._placed:
            x, y = chunk_graph.graph_position(prim)
            ed.set_node_position(node_id, imgui.ImVec2(float(x), float(y)))
            self._placed.add(path)

        # Zero horizontal node padding: a socket drawn at x=0 then lands exactly on the
        # node's border, which is where a port belongs. The vertical padding stays, and
        # the row content indents itself by SIDE_PAD instead, so only the dots touch the
        # edge and the labels still have room.
        ed.push_style_var(ed.StyleVar.node_padding, imgui.ImVec4(0.0, 8.0, 0.0, 8.0))
        ed.begin_node(node_id)
        imgui.indent(SIDE_PAD)
        label = chunk_graph.node_label(prim)
        if chunk_graph.is_disabled(prim):
            imgui.text_disabled(label + "  (disabled)")
        else:
            imgui.text_colored(imgui.ImVec4(0.79, 0.71, 0.42, 1.0), label)
        imgui.dummy(imgui.ImVec2(BODY_W - SIDE_PAD * 2, 1.0))
        imgui.unindent(SIDE_PAD)

        wired = self._wired_attr_paths()
        shown = [p for p in self.ports[path] if self._pin_visible(p, wired)]
        hidden = len(self.ports[path]) - len(shown)
        ins = [p for p in shown if p.is_input]
        outs = [p for p in shown if not p.is_input]

        # Inputs down the left edge, outputs down the right, both anchored to their dot.
        # Drawn as two groups on one line so a node is as tall as its longest side rather
        # than the sum of both -- the previous stacked layout is why Transform ran off the
        # bottom of the window.
        # One row per pin index, inputs on the left border and outputs on the right, so a
        # node is as tall as its longer side rather than the sum of both -- and so a wire
        # meets the node where the border is, not somewhere inside it.
        for index in range(max(len(ins), len(outs))):
            start_y = imgui.get_cursor_screen_pos().y
            if index < len(ins):
                port = ins[index]
                ed.begin_pin(ed.PinId(self.ids.get(port.attr_path)), ed.PinKind.input)
                _socket(port.port_type, port.attr_path in wired)
                imgui.same_line(0.0, 4.0)
                imgui.text(port.name)
                ed.end_pin()
            else:
                imgui.dummy(imgui.ImVec2(SOCKET_BOX, SOCKET_BOX))

            if index < len(outs):
                port = outs[index]
                label_w = imgui.calc_text_size(port.name).x
                # Right-align to the node's content width: label, then the dot on the edge.
                imgui.same_line(BODY_W - SOCKET_BOX - label_w - 4.0)
                ed.begin_pin(ed.PinId(self.ids.get(port.attr_path)), ed.PinKind.output)
                imgui.text(port.name)
                imgui.same_line(0.0, 4.0)
                _socket(port.port_type, port.attr_path in wired)
                ed.end_pin()
            _ = start_y

        imgui.indent(SIDE_PAD)
        if hidden:
            imgui.text_disabled(f"+{hidden} unwired ports  (P)")

        chunk = self.chunks.get(path)
        if chunk is not None:
            imgui.separator()
            # draw_params, not a flattened walk: a group of plain numbers becomes one
            # inline row, which is what keeps a Transform node a readable height.
            chunk_imgui.draw_params(chunk, width=BODY_W - chunk_imgui.LABEL_W - SIDE_PAD * 3)
        imgui.unindent(SIDE_PAD)
        ed.end_node()
        ed.pop_style_var()

    def _pin_visible(self, port, wired: set) -> bool:
        """Whether to draw this pin.

        Exec pins always: they are the chain, and hiding one would hide the graph's
        structure. A data pin only when it carries a wire -- or when the user has asked
        for all of them.

        Safe by construction: a link only exists between two wired ports, and a wired port
        is always shown, so hiding can never leave a link pointing at a pin that was not
        drawn.
        """
        if self.show_all_pins or port.port_type == chunk_graph.PORT_EXEC:
            return True
        return port.attr_path in wired

    def _wired_attr_paths(self) -> set:
        """Every attr path with an edge on it, for filled-vs-hollow sockets."""
        wired = set()
        for edge in self.edges:
            wired.add(edge.src_attr_path)
            wired.add(edge.dst_attr_path)
        return wired

    def _draw_edge(self, edge):
        src = self.ids.get(edge.src_attr_path)
        dst = self.ids.get(edge.dst_attr_path)
        link_id = self.ids.get(f"link:{edge.src_attr_path}->{edge.dst_attr_path}")
        src_port = self._port_for_attr(edge.src_node_path, edge.src_attr_path)
        red, green, blue = PORT_COLOUR.get(
            src_port.port_type if src_port else "", _FALLBACK_COLOUR)
        ed.link(ed.LinkId(link_id), ed.PinId(src), ed.PinId(dst),
                imgui.ImVec4(red, green, blue, 1.0),
                2.5 if edge.is_exec else 1.5)

    def _port_for_attr(self, node_path: str, attr_path: str):
        return next((p for p in self.ports.get(node_path, [])
                     if p.attr_path == attr_path), None)

    # -- input -------------------------------------------------------------

    def _port_for(self, pin_id):
        """The PortRef behind an editor pin id."""
        attr_path = self.ids.key(pin_id)
        if not attr_path:
            return None
        node_path = attr_path.split(".", 1)[0]
        return next((p for p in self.ports.get(node_path, [])
                     if p.attr_path == attr_path), None)

    def _handle_creation(self):
        """A dragged link, judged by the engine rather than by this file."""
        # end_create() is only legal when begin_create() returned true -- the library
        # asserts otherwise ("Please call End() only when Begin() was successful"), which
        # surfaces as a RuntimeError out of the render loop and takes the window down.
        if not ed.begin_create():
            return
        start_pin, end_pin = ed.PinId(), ed.PinId()
        if ed.query_new_link(start_pin, end_pin):
            source, target = self._port_for(start_pin), self._port_for(end_pin)
            if source is not None and target is not None:
                if chunk_graph.can_connect(source, target) and ed.accept_new_item():
                    change = chunk_graph.connect(self.stage, source, target, self.chunks)
                    if change is not None:
                        self.stage.GetRootLayer().Save()
                        self.status = f"connected {source.name} -> {target.name}"
                        self.reload()
        ed.end_create()

    def _handle_deletion(self):
        if not ed.begin_delete():
            return  # same begin/end pairing rule as _handle_creation
        link_id = ed.LinkId()
        while ed.query_deleted_link(link_id):
            key = self.ids.key(link_id) or ""
            if key.startswith("link:") and ed.accept_deleted_item():
                _src, _, dst_attr = key[len("link:"):].partition("->")
                node_path = dst_attr.split(".", 1)[0]
                port = next((p for p in self.ports.get(node_path, [])
                             if p.attr_path == dst_attr), None)
                if port is not None:
                    chunk_graph.disconnect(self.stage, port, self.prims, self.chunks)
                    self.stage.GetRootLayer().Save()
                    self.reload()
        node_id = ed.NodeId()
        while ed.query_deleted_node(node_id):
            if ed.accept_deleted_item():
                path = self.ids.key(node_id)
                if path:
                    self.set_selection([path])
                    self.delete_selected()
        ed.end_delete()

    def _shortcuts(self):
        """Graph keys, ignored while a param is being typed into.

        chunk_imgui.is_editing() is the FocusTracker equivalent, and it is one call because
        ImGui owns every widget and already knows.
        """
        if chunk_imgui.is_editing():
            return
        for key, action in (
            (imgui.Key.delete, self.delete_selected),
            (imgui.Key.d, self.toggle_disable_selected),
            (imgui.Key.l, self.auto_layout),
            (imgui.Key.f, lambda: self.frame(self.selected_nodes or self.prims)),
            (imgui.Key.g, self.group_selection),
            (imgui.Key.p, self.toggle_all_pins),
            (imgui.Key.v, self.create_view),
        ):
            if imgui.is_key_pressed(key):
                action()
                return

    def toggle_all_pins(self) -> None:
        """P: show every data pin, or only the wired ones."""
        self.show_all_pins = not self.show_all_pins
        self.status = "all pins" if self.show_all_pins else "wired pins only"

    # -- the contract ------------------------------------------------------

    def set_selection(self, paths, add: bool = False) -> None:
        self.selected_nodes = chunk_graph.apply_selection(
            self.chunks, paths, self.selected_nodes, add)

    def selected_chunks(self) -> list:
        return chunk_graph.chunks_for(self.chunks, self.selected_nodes)

    def delete_selected(self) -> None:
        chunks = self.selected_chunks()
        if not chunks:
            self.status = "nothing selected"
            return
        chunk_graph.delete_chunks(chunks)
        self.stage.GetRootLayer().Save()
        self.selected_nodes = set()
        self.chunks = {get_chunk_prim_path(c): c for c in self.scene.get_chunks()}
        self.status = f"deleted {len(chunks)}"
        self.reload()

    def toggle_disable_selected(self) -> None:
        chunks = self.selected_chunks()
        chunk_graph.toggle_disabled(chunks)
        if chunks:
            self.stage.GetRootLayer().Save()
            self.status = f"toggled {len(chunks)}"
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
        self.status = f"copied {chunk_graph.copy_to_clipboard(nodes, edges)}"

    def cut_selection(self) -> None:
        nodes, edges = self.snapshot_selection()
        if not nodes:
            self.status = "nothing to cut"
            return
        count = chunk_graph.copy_to_clipboard(nodes, edges)
        self.delete_selected()
        self.status = f"cut {count}"

    def _nest_target(self):
        return chunk_graph.nest_target(self.chunks.get(self.context))

    def paste_at(self, x: float, y: float, nodes=None, edges=None) -> None:
        nodes = chunk_graph.node_clipboard if nodes is None else nodes
        edges = chunk_graph.edge_clipboard if edges is None else edges
        if not nodes or self.scene is None:
            self.status = "clipboard empty"
            return
        made = chunk_graph.paste_nodes(
            self.scene, self.stage, nodes, edges,
            to_usd=lambda rel_x, rel_y: (x + rel_x, -(y + rel_y)),
            nest=self._nest_target(), chunks=self.chunks)
        self.stage.GetRootLayer().Save()
        self.chunks = {get_chunk_prim_path(c): c for c in self.scene.get_chunks()}
        self.reload()
        self.set_selection(get_chunk_prim_path(c) for c in made.values())
        self.status = f"pasted {len(made)}"

    def duplicate_selection(self) -> None:
        nodes, edges = self.snapshot_selection()
        if not nodes:
            self.status = "nothing to duplicate"
            return
        origin = chunk_graph.selection_origin(self.positions, self.selected_nodes)
        if origin is None:
            return
        self.paste_at(origin[0] + 40, origin[1] + 40, nodes, edges)

    def group_selection(self) -> None:
        group_path, count = chunk_graph.group_into_chunkgroup(
            self.stage, self.scene, self.chunks, self.prims, self.positions,
            self.selected_nodes, to_usd=lambda x, y: (x, -y))
        if not group_path:
            self.status = "nothing to group"
            return
        self.stage.GetRootLayer().Save()
        self.chunks = {get_chunk_prim_path(c): c for c in self.scene.get_chunks()}
        self.reload()
        self.status = f"grouped {count}"

    def menu_entries(self):
        return chunk_graph.menu_entries()

    def create_chunk(self, entry, x: float, y: float) -> None:
        created, error = chunk_graph.create_from_entry(
            self.scene, entry, (x, -y), self._nest_target())
        if error:
            self.status = error
        if created:
            self.stage.GetRootLayer().Save()
            self.chunks = {get_chunk_prim_path(c): c for c in self.scene.get_chunks()}
            self.status = f"created {created[0].name}"
            self.reload()

    def can_dive(self, path: str) -> bool:
        return chunk_graph.can_dive(self.chunks, path)

    def dive_into(self, path: str) -> None:
        if not self.can_dive(path):
            return
        self.context = path
        self.selected_nodes = set()
        self._placed = set()  # the inside of a group has its own coordinates
        self.status = f"in {path.rsplit('/', 1)[-1]}"
        self.reload()

    def dive_out(self) -> None:
        parent = chunk_graph.parent_context(self.context, self.available)
        if parent is None:
            self.status = "already at the top"
            return
        inner, self.context = self.context, parent
        self.selected_nodes = set()
        self._placed = set()
        self.reload()
        if inner in self.prims:
            self.set_selection([inner])

    def create_view(self, target_path: str | None = None) -> None:
        """V: wire a view to the selected node, or to the end of the chain.

        A view is what drives execution -- recook_for_view cooks its upstream tree and
        blocks the rest -- so a context without one cooks nothing at all.
        """
        if self.scene is None:
            self.status = "no chunk session"
            return
        if target_path is None:
            chosen = [p for p in self.selected_nodes if p in self.prims]
            target_path = chosen[0] if chosen else None
        slot = chunk_graph.new_view_slot(self.stage, self.scene, self.context_chunk(),
                                         target_path)
        if slot is None:
            self.status = "no view created"
            return
        self.stage.GetRootLayer().Save()
        self.chunks = {get_chunk_prim_path(c): c for c in self.scene.get_chunks()}
        self.reload()
        self.set_active_slot(slot)

    def context_chunk(self):
        """The chunk object for the context being shown, if there is one."""
        return self.chunks.get(self.context)

    def active_slot(self) -> int:
        return chunk_graph.read_active_slot(self.stage, self.context)

    def set_active_slot(self, slot: int) -> None:
        changed, self.status = chunk_graph.activate_view_slot(
            self.stage, self.scene, self.context, self.chunks, slot)
        if not changed:
            return
        self.stage.GetRootLayer().Save()
        self.reload()

    def frame(self, paths) -> None:
        """The editor owns navigation, so framing is one call to it.

        Qt unions rects and calls fitInView; Flet solves a zoom and a pan by hand. Here
        navigate_to_content does the whole job -- which is the clearest single example of
        what a node-editor library buys you.
        """
        if self._editor is None:
            return  # no editor headless; navigation is the editor's job
        ed.set_current_editor(self._editor)
        for path in paths:
            if path in self.prims:
                ed.select_node(ed.NodeId(self.ids.get(path)), True)
        ed.navigate_to_content()

    # -- layout ------------------------------------------------------------

    def _node_size(self, path: str) -> tuple[float, float]:
        """A node's real size once the editor has drawn it, else a nominal one.

        get_node_size returns zeroes for a node that has not been through a frame yet, so
        the fallback is not defensive padding -- it is the first-layout case.
        """
        if self._editor is not None and path in self._placed:
            size = ed.get_node_size(ed.NodeId(self.ids.get(path)))
            if size.x > 1.0 and size.y > 1.0:
                return (size.x, size.y)
        return (NODE_W * 1.6, 200.0)

    def auto_layout(self) -> None:
        """L: lay out the selection's connected subgraph, or everything.

        Computation shared with the other frontends; applying it means handing the editor
        the new positions AND writing them to USD, since the editor owns them from then on.
        """
        if not self.prims:
            return
        positions = self.positions
        placed = chunk_graph.layout_graph(
            self.prims,
            size_of=self._node_size,
            current_pos=lambda path: positions[path],
            seeds=[p for p in self.selected_nodes if p in self.prims] or None,
        )
        for path, (x, y) in placed.items():
            if self._editor is not None:
                ed.set_node_position(ed.NodeId(self.ids.get(path)),
                                     imgui.ImVec2(float(x), float(y)))
            chunk_graph.write_position(self.prims.get(path), x, -y, owner=self.chunks.get(path))
        if placed:
            self.stage.GetRootLayer().Save()
        self.status = f"laid out {len(placed)}"

    # -- shell -------------------------------------------------------------

    def summary(self) -> str:
        return (f"{self.context}   —   {len(self.prims)} nodes · {len(self.edges)} wires"
                + (f"   —   {self.session_error}" if self.session_error else "")
                + (f"   —   {self.status}" if self.status else ""))


@contextlib.contextmanager
def _quiet_introspection():
    """Stop inspect.getmodule waking the registry's lazily-imported chunk modules.

    imgui_bundle monkeypatches hello_imgui.run to record its caller for a tutorial
    screenshot feature, and does it with inspect.stack(). That walks every frame calling
    getmodule(), which touches module objects -- and the chunk registry keeps discovered
    chunks in sys.modules behind a lazy loader, so touching one EXECUTES it. Any chunk
    that cannot import outside its host then raises from inside inspect, and the runner
    exits before opening a window with no traceback of its own.

    scripts/utils/displaymodes/display_modes.py is the one that bites here: it imports
    pxr.Usdviewq, which only exists inside usdview. Returning None from getmodule costs
    the patch a filename it does not use and wakes nothing.

    Worth knowing more generally: anything that walks frames -- a debugger, a profiler, an
    error reporter -- can trip the same wire.
    """
    original = inspect.getmodule
    inspect.getmodule = lambda *args, **kwargs: None
    try:
        yield
    finally:
        inspect.getmodule = original


def main(network: str, context: str | None = None):
    view = ImGuiGraphView(network, context)

    def gui():
        if view._editor is None:
            # settings_file off, deliberately. The editor would otherwise persist node
            # positions to NodeEditor.json in the working directory and restore from it on
            # the next open -- shadowing ui:nodegraph:node:pos, which is the position all
            # three frontends share. USD is the source of truth for where a node sits; a
            # per-frontend cache of the same thing can only disagree with it.
            config = ed.Config()
            config.settings_file = ""
            view._editor = ed.create_editor(config)
        view.draw()

    params = hello_imgui.RunnerParams()
    params.app_window_params.window_title = f"Chunk graph — {network.rsplit('/', 1)[-1]}"
    params.app_window_params.window_geometry.size = (1400, 850)
    # Window layout goes to the user config folder. The default is the CURRENT folder, so
    # running this from a repo drops a .ini next to the source.
    params.ini_folder_type = hello_imgui.IniFolderType.app_user_config_folder
    params.callbacks.show_gui = gui
    with _quiet_introspection():
        hello_imgui.run(params)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(
            "usage: python -m usd_chunks.frontends.imgui.nodegraph <network.usda>")
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
