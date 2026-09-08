"""A Flet view of a chunk network: draws it, and dispatches input into the engine.

    python -m usd_chunks.plugins.chunk_flet_nodegraph <network.usda> [context_prim_path]

    click a port          select it
    click a second port   connect, if chunk_graph says the pair is legal
    right-click a port    disconnect it
    drag a node           move it; the position is written back to USD
    click empty space     deselect

Every rule lives in `chunk_graph`: which nodes exist, what ports they have, what type each
carries, whether a wire is legal, and what authoring one does to the stage. This module
knows only where things sit on screen -- hit-testing and pixels. That division is the point:
if this file ever needs to know a rule the Qt view knows, the rule is in the wrong place.

Node positions come from USD, so this and the Qt view show the same arrangement of the same
file, and a node dragged here has moved there too.

One honest gap: authoring a *param* wire also wants the destination param set to an
`@Node.param@` expression, and that needs the live chunk objects, which this view does not
load -- it reads a stage, not a session. Exec wiring is complete; a param wire authored here
connects but does not yet write the expression.
"""
from __future__ import annotations

import sys
import weakref

import flet as ft
import flet.canvas as cv
from pxr import Gf, Sdf, Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_core import SceneSender
from usd_chunks.chunk_registry import ChunkLayerManager
from usd_chunks.plugins import chunk_flet, chunk_graph, chunk_usd
from usd_chunks.chunk_scene import CHUNKGROUP_MODULE
from usd_chunks.plugins.chunk_usd import get_chunk_prim_path, load_chunks

# Lifted verbatim from chunk_qt_nodegraph rather than picked to look similar. Two
# frontends onto the same graph that disagree about what a float port looks like are two
# different tools, and the muted palette is a deliberate choice there: saturated dots on a
# dark canvas read as errors.
PORT_COLOUR = {
    chunk_graph.PORT_EXEC: "#999999",
    chunk_graph.PORT_FLOAT: "#a06845",   # muted orange
    chunk_graph.PORT_INT: "#5d7ea8",     # muted blue
    chunk_graph.PORT_BOOL: "#9e8e4a",    # muted yellow
    chunk_graph.PORT_STRING: "#6a9070",  # muted green
    chunk_graph.PORT_COLOR: "#c87060",   # muted red
    chunk_graph.PORT_VECTOR: "#9a5da8",  # muted purple
}

#: Node chrome, also from the Qt view.
COL_NODE_BG = "#3a3a3a"
COL_NODE_HEADER = "#2a2a2a"
COL_NODE_BORDER = "#555555"
COL_ACCENT = "#cab46a"        # ChunkWidget.SELECTED_COLOR
COL_VIEW_HEADER = "#c87830"   # the ACTIVE view, the one driving execution
COL_CANVAS = "#252525"
COL_TEXT = "#cccccc"

NODE_W, HEADER_H, ROW_H = 260.0, 30.0, 16.0  # wider: Qt's nodes fit a path
BODY_PAD = 8.0  # the body Container's padding, which offsets every row inside it
PORT_R = 5.0
#: Expression badge on a wire. Same radius and glyph as Qt (ChunkEdgeItem._EXP_RADIUS)
#: so the two frontends read identically.
EXP_R = 12.0
GRAB_R = 11.0  # click tolerance, wider than the dot so a port is not fiddly to hit
MARGIN = 120.0
ZOOM_MIN, ZOOM_MAX, ZOOM_STEP = 0.25, 3.0, 1.1


class _Scene(SceneSender):
    """The minimum a chunk session needs to exist: a stage and a selection."""

    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


def contexts(stage) -> list[str]:
    """Every prim path holding Chunk prims, outermost first.

    A chunkgroup is both a node in its parent's graph and a graph of its own, so a file has
    several of these and they nest.
    """
    found, pending = [], [chunk_graph.CHUNKS_ROOT]
    while pending:
        root = pending.pop(0)
        nodes = chunk_graph.resolve_graph_nodes(stage, root)
        if not nodes:
            continue
        found.append(root)
        pending += [str(p.GetPath()) for p in nodes]
    return found


def default_context(stage) -> str:
    """The innermost context on the outermost chain.

    A file whose top level holds nothing but one chunkgroup is asking to be opened inside
    it, the same way double-clicking that group in Qt dives into it.
    """
    available = contexts(stage)
    context = available[0] if available else chunk_graph.CHUNKS_ROOT
    while True:
        nodes = chunk_graph.resolve_graph_nodes(stage, context)
        if len(nodes) == 1 and str(nodes[0].GetPath()) in available:
            context = str(nodes[0].GetPath())
            continue
        return context


class GraphView:
    """Pixels and pointer events. Every decision belongs to chunk_graph."""

    def __init__(self, network: str, context: str | None = None):
        self.network = network
        # Held for the life of the view: a Usd.Prim does not keep its stage alive, so
        # dropping this collects the stage and expires every prim -- which surfaces later
        # as "accessed invalid expired prim", nowhere near the cause.
        self.stage = Usd.Stage.Open(network)
        # A live session, the same as Qt has. Without one the view can still draw -- ports
        # and wires come off the prims -- but two things quietly differ: a Menu and a
        # LineEdit are both strings in USD, so only the chunk knows which port type to
        # show, and a param wire needs its @Node.param@ expression written onto the
        # destination param, which is a chunk operation. Falling back rather than failing,
        # because a stage is still worth looking at when the modules cannot be found.
        self.chunks: dict = {}
        self.session_error = ""
        try:
            self.scene = _Scene(self.stage)
            chunk_registry.ChunkRegistry().initialize(self.scene, "flet")
            ChunkLayerManager().initialize(self.stage)
            chunk_usd.ChunkStageSync().initialize(self.stage, self.scene)
            load_chunks(self.stage, self.scene)
            self.chunks = {get_chunk_prim_path(c): c for c in self.scene.get_chunks()}
        except Exception as exc:  # noqa: BLE001 -- a broken session must not stop drawing
            self.session_error = f"no chunk session ({type(exc).__name__}); drawing only"

        self.available = contexts(self.stage)
        self.context = context or default_context(self.stage)
        self.selected: chunk_graph.PortRef | None = None
        self.status = ""
        self.dragging: str | None = None
        self.wiring: chunk_graph.PortRef | None = None
        self.wire_to = (0.0, 0.0)
        self.canvas: cv.Canvas | None = None
        self.stack: ft.Stack | None = None
        self.param_widgets: list = []
        #: {node path: {param name: y within the body}} -- filled by node_controls, which
        #: is the only place that knows how the params were laid out.
        self.param_offsets: dict = {}
        #: Screen = layout * zoom + pan. Everything the view computes -- positions,
        #: port_xy, extent -- stays in unscaled layout space; the transform is applied
        #: once when drawing and inverted once when hit-testing, so no geometry code
        #: has to know the canvas is zoomed.
        self.zoom = 1.0
        self.pan = (0.0, 0.0)
        self.panning = False
        self.pan_container: ft.Container | None = None
        #: Set by main(). Needed to open a dialog; None in tests and headless use.
        self.page: ft.Page | None = None
        #: Selected node paths. Mirrored onto each chunk's SELECTED state bit, which is
        #: where Qt keeps it and what the shared operations read -- so a selection means
        #: the same thing to both frontends rather than being a private view detail.
        self.selected_nodes: set[str] = set()
        self.live = False  # nothing may update the client until page.add has run
        self.status_text: ft.Text | None = None
        self.reload()

    # -- state -------------------------------------------------------------

    def reload(self):
        self.prims, self.ports, self.edges = chunk_graph.read_graph(
            self.stage, self.context, self.chunks)
        raw = {path: chunk_graph.node_position(prim) for path, prim in self.prims.items()}
        # USD is Y-up, this canvas is Y-down, so USD y is negated -- exactly what Qt does
        # (chunk_qt_nodegraph writes _create_node_pos as (x, -y)). Without the flip the two
        # frontends render the same file mirrored vertically, and a node dragged in one
        # jumps to the opposite side when opened in the other.
        flipped = {path: (x, -y) for path, (x, y) in raw.items()}
        # MARGIN has to clear the port dots and their labels, which now hang off the left.
        # Screen space is USD space shifted so the top-left node sits at MARGIN. The offset
        # is kept because a drag has to be written back in the coordinates USD stores.
        self.origin = (min((x for x, _ in flipped.values()), default=0.0) - MARGIN,
                       min((y for _, y in flipped.values()), default=0.0) - MARGIN)
        self.positions = {p: (x - self.origin[0], y - self.origin[1])
                          for p, (x, y) in flipped.items()}

    def rows(self, path: str):
        node_ports = self.ports[path]
        ins = [p for p in node_ports if p.is_input]
        outs = [p for p in node_ports if not p.is_input]
        key = lambda p: (0 if p.port_type == chunk_graph.PORT_EXEC else 1, p.name)  # noqa: E731
        return sorted(ins, key=key), sorted(outs, key=key)

    def node_height(self, path: str) -> float:
        """Tall enough for the ports AND for the param body sitting beside them."""
        ins, outs = self.rows(path)
        ports = HEADER_H + ROW_H * max(len(ins), len(outs), 1) + 8
        chunk_obj = self.chunks.get(path)
        body = 0.0
        if chunk_obj is not None:
            # Rough: a labelled control is about 46px, a group heading about 22. Only used
            # for hit-testing and for spacing the graph, not for sizing the real control,
            # which measures itself.
            rows = len(list(chunk_obj.get_params()))
            body = HEADER_H + 40.0 + 46.0 * rows + 16
        return max(ports, body)

    # -- pointer -> layout -------------------------------------------------

    def _xy(self, event) -> tuple[float, float]:
        """A pointer event's position in layout space.

        Flet 0.80 reports a pointer as an Offset on `local_position`. This file used to
        read `event.local_x`, which does not exist on any of these event classes -- so
        every handler raised AttributeError on the first click and none of the graph
        interaction worked at all. Reading the source made it look implemented.

        The zoom/pan inverse lives here too, so there is exactly one place where a screen
        point becomes a layout point.
        """
        point = event.local_position
        return ((point.x - self.pan[0]) / self.zoom,
                (point.y - self.pan[1]) / self.zoom)

    def _layout_to_usd(self, x: float, y: float) -> tuple[float, float]:
        """Layout point -> the coordinates USD stores. Undoes the origin shift and the
        Y-up flip together; every write of a node position goes through here."""
        return (x + self.origin[0], -(y + self.origin[1]))

    def _to_screen(self, x: float, y: float) -> tuple[float, float]:
        """Layout point -> screen point. The inverse of _xy, for anyone who has a node
        position and needs to know where to aim a pointer at it."""
        return (x * self.zoom + self.pan[0], y * self.zoom + self.pan[1])

    def _delta(self, event) -> tuple[float, float]:
        """A drag delta in layout space -- a 10px drag moves a node 10px at zoom 1, and
        20 layout units at zoom 0.5, which is what keeps the node under the cursor."""
        delta = event.local_delta
        return (delta.x / self.zoom, delta.y / self.zoom)

    def port_xy(self, port: chunk_graph.PortRef):
        """Where a port's dot lands.

        Placement is the view's business: the engine says a port exists and what it
        carries, not where to draw it. Qt derives Y from live widget geometry; here it is a
        row, and both are legitimate answers to a question the engine does not ask.
        """
        x0, y0 = self.positions[port.node_path]
        x = x0 if port.is_input else x0 + NODE_W

        # A param port sits level with its own control. Qt reads that position off live
        # widget geometry; Flet cannot be asked, so build_chunk_controls reports where it
        # put each row and this places the dot against it. Without that the ports march
        # down a fixed grid while the params sit wherever their controls landed, and the
        # two only agree by accident.
        offsets = self.param_offsets.get(port.node_path) or {}
        if port.name in offsets:
            return x, y0 + HEADER_H + BODY_PAD + offsets[port.name]

        # Exec ports, and anything with no control of its own, fall back to the header.
        ins, outs = self.rows(port.node_path)
        row = (ins if port.is_input else outs).index(port)
        return x, y0 + HEADER_H / 2.0 + ROW_H * row

    # -- hit testing -------------------------------------------------------

    def port_at(self, x: float, y: float):
        """Nearest port within the grab radius.

        Only nodes whose rect the point could plausibly belong to are scanned: a click
        used to measure every port on every node, on every press and every drag frame.
        """
        best, best_distance = None, GRAB_R
        for path in self.prims:
            nx, ny = self.positions[path]
            if not (nx - GRAB_R - PORT_R <= x <= nx + NODE_W + GRAB_R + PORT_R):
                continue
            if not (ny - GRAB_R <= y <= ny + self.node_height(path) + GRAB_R):
                continue
            for port in self.ports[path]:
                px, py = self.port_xy(port)
                distance = ((px - x) ** 2 + (py - y) ** 2) ** 0.5
                if distance <= best_distance:
                    best, best_distance = port, distance
        return best

    def node_at(self, x: float, y: float):
        for path in self.prims:
            nx, ny = self.positions[path]
            if nx <= x <= nx + NODE_W and ny <= y <= ny + self.node_height(path):
                return path
        return None

    # -- input -------------------------------------------------------------

    def on_tap(self, event):
        x, y = self._xy(event)
        port = self.port_at(x, y)
        if port is None:
            node = self.node_at(x, y)
            self.selected = None
            if node is not None:
                # Flet reports no modifier on a tap, so a tap always replaces the
                # selection; Ctrl+A selects everything and Escape clears it.
                self.set_selection([node])
                self.status = f"selected {node.rsplit('/', 1)[-1]}"
            else:
                self.set_selection(())
                self.status = ""
            return self.refresh()

        if self.selected is None:
            self.selected = port
            self.status = f"selected {port.name} ({port.port_type})"
            return self.refresh()

        if port.attr_path == self.selected.attr_path:
            self.selected, self.status = None, ""
            return self.refresh()

        change = chunk_graph.connect(self.stage, self.selected, port, self.chunks)
        if change is None:
            # The engine's rule, reported rather than re-derived here.
            self.status = (f"cannot connect {self.selected.name} "
                           f"({self.selected.port_type}) to {port.name} ({port.port_type})")
        else:
            self.stage.GetRootLayer().Save()
            self.status = (f"connected {self.selected.name} -> {port.name}"
                           + (f" (expression refused: {change.expression_error})"
                              if change.expression_error else ""))
            self.reload()
        self.selected = None
        self.refresh(bodies=True)

    def on_secondary_tap(self, event):
        x, y = self._xy(event)
        port = self.port_at(x, y)
        if port is None:
            # Qt opens its chunk menu here. Flet used to do nothing on empty space, which
            # left no way to create a node at all.
            return self.open_create_menu(x, y)
        change = chunk_graph.disconnect(self.stage, port, self.prims, self.chunks)
        if change.edges_dirty:
            self.stage.GetRootLayer().Save()
            self.status = f"disconnected {port.name}"
            self.reload()
        else:
            self.status = f"{port.name} had nothing to disconnect"
        self.selected = None
        self.refresh()

    def on_pan_start(self, event):
        # A grab on a port starts a wire; anywhere else on a node moves it.
        x, y = self._xy(event)
        port = self.port_at(x, y)
        if port is not None:
            self.wiring = port
            self.wire_to = (x, y)
            self.dragging = None
            return self.refresh()
        self.dragging = self.node_at(x, y)
        # Empty canvas: drag the view itself. Nothing else claimed this gesture.
        self.panning = self.dragging is None

    def on_pan_update(self, event):
        if self.panning:
            # Pan is a SCREEN translation, so it uses the raw delta: dividing by
            # zoom here would make the canvas slide faster the further you zoom out.
            self.pan = (self.pan[0] + event.local_delta.x,
                        self.pan[1] + event.local_delta.y)
            return self.refresh()
        dx, dy = self._delta(event)
        if self.wiring is not None:
            x, y = self.wire_to
            self.wire_to = (x + dx, y + dy)
            return self.refresh()
        if not self.dragging:
            return
        x, y = self.positions[self.dragging]
        self.positions[self.dragging] = (x + dx, y + dy)
        self.refresh()

    # -- selection ---------------------------------------------------------

    def set_selection(self, paths, add: bool = False):
        """Select these nodes, mirroring onto the chunks' SELECTED state.

        The state bit is presentation only -- chunk_scheduler.propagate_presentation --
        so selecting cooks nothing. Writing it anyway is what lets an operation ask the
        chunks what is selected instead of being handed a list by whichever frontend
        happened to call it.
        """
        paths = set(paths)
        self.selected_nodes = (self.selected_nodes | paths) if add else paths
        for path, chunk in self.chunks.items():
            try:
                if path in self.selected_nodes:
                    chunk.set_state(ChunkFlags.State.SELECTED)
                else:
                    chunk.unset_state(ChunkFlags.State.SELECTED)
            except Exception:  # noqa: BLE001 -- a chunk that cannot hold state is not fatal
                continue

    def selected_chunks(self):
        return [c for p, c in self.chunks.items() if p in self.selected_nodes]

    # -- clipboard ---------------------------------------------------------

    def snapshot_selection(self):
        """(nodes, edges) for the current selection, in the engine's clipboard format.

        Positions are relative to the selection's top-left, so a paste lands as a shape
        rather than at absolute coordinates. A wire to a node OUTSIDE the selection is
        dropped rather than recreated against the original -- only wires between two
        selected nodes survive, which is what Qt does.
        """
        chosen = [p for p in self.selected_nodes if p in self.prims]
        if not chosen:
            return [], []
        min_x = min(self.positions[p][0] for p in chosen)
        min_y = min(self.positions[p][1] for p in chosen)
        nodes, index = [], {}
        for path in chosen:
            chunk = self.chunks.get(path)
            if chunk is None:
                continue
            rel = (self.positions[path][0] - min_x, self.positions[path][1] - min_y)
            clip = chunk_graph.snapshot_chunk_node(chunk, self.scene, self.stage, rel)
            if clip is None:
                continue  # no module behind it; nothing to recreate it from
            index[path] = len(nodes)
            nodes.append(clip)

        edges = []
        for path in chosen:
            dst_index = index.get(path)
            if dst_index is None:
                continue
            for dst_attr, src_path, src_attr in chunk_graph.iter_prim_connections(self.prims[path]):
                src_index = index.get(src_path)
                if src_index is None:
                    continue
                edges.append(chunk_graph.ClipboardEdge(src_index, src_attr, dst_index, dst_attr))
        return nodes, edges

    def copy_selection(self):
        nodes, edges = self.snapshot_selection()
        if not nodes:
            self.status = "nothing to copy"
            return self.refresh()
        # The engine's lists, not copies of them: Qt reads these same two objects, so a
        # selection copied there can be pasted here.
        chunk_graph.node_clipboard[:] = nodes
        chunk_graph.edge_clipboard[:] = edges
        self.status = "copied %d" % len(nodes)
        self.refresh()

    def cut_selection(self):
        nodes, edges = self.snapshot_selection()
        if not nodes:
            self.status = "nothing to cut"
            return self.refresh()
        chunk_graph.node_clipboard[:] = nodes
        chunk_graph.edge_clipboard[:] = edges
        self.delete_selected()
        self.status = "cut %d" % len(nodes)
        self.refresh()

    def paste_at(self, x: float, y: float, nodes=None, edges=None):
        """Materialize clipboard nodes at a layout point and re-wire them.

        Qt defers its wiring on a timer because its ports arrive in deferred proxy
        batches; here reload() builds everything synchronously, so the ports exist the
        moment it returns and the wiring is a straight second pass.
        """
        nodes = chunk_graph.node_clipboard if nodes is None else nodes
        edges = chunk_graph.edge_clipboard if edges is None else edges
        if not nodes or self.scene is None:
            self.status = "clipboard empty"
            return self.refresh()

        made = self._materialize(nodes, x, y)
        created = {i: get_chunk_prim_path(c) for i, c in made.items()}

        self.stage.GetRootLayer().Save()
        self.chunks = {get_chunk_prim_path(c): c for c in self.scene.get_chunks()}
        self.reload()

        for edge in edges:
            src_path = created.get(edge.src_index)
            dst_path = created.get(edge.dst_index)
            if not src_path or not dst_path:
                continue
            src = self._port(src_path, edge.src_attr)
            dst = self._port(dst_path, edge.dst_attr)
            if src is not None and dst is not None:
                chunk_graph.connect(self.stage, src, dst, self.chunks)
        if edges:
            self.stage.GetRootLayer().Save()
            self.reload()

        self.set_selection(created.values())
        self.status = "pasted %d" % len(created)
        self.refresh(bodies=True)

    def _materialize(self, nodes, x: float, y: float) -> dict:
        """Create clipboard nodes at a layout point. Returns {clipboard index: chunk}.

        Split out of paste_at because grouping needs the chunks without the port-based
        wiring: a paste into a group puts them somewhere this view is not showing, so
        they have no ports here to wire through.
        """
        registry = chunk_registry.ChunkRegistry()
        made = {}
        previous = chunk_usd._create_node_pos
        try:
            for i, item in enumerate(nodes):
                module = registry.get_chunk_module(item.module_name)
                if module is None:
                    continue
                chunks = module.create() if module.loaded else module.load()
                if not chunks:
                    continue
                primary = chunks[0]
                # Rows before values: a list group starts empty on a fresh chunk, so its
                # values have nowhere to land until the rows it had are back.
                chunk_graph.apply_list_sizes(primary, getattr(item, "list_sizes", []))
                chunk_graph.apply_param_values(primary, item.values)
                chunk_usd._create_node_pos = self._layout_to_usd(
                    x + item.rel_pos[0], y + item.rel_pos[1])
                for c in chunks:
                    self.scene.add_chunk(c)
                made[i] = primary
        finally:
            chunk_usd._create_node_pos = previous
        return made

    # -- grouping ----------------------------------------------------------

    def _exec_boundary(self, inside: set):
        """(feeds, consumers) -- the exec wires crossing into and out of `inside`.

        Read BEFORE anything is removed. Deleting the selection heals the exec chain
        closed as it goes, so afterwards nothing records what used to attach where.
        """
        feeds, consumers = [], []
        for prim in self.stage.TraverseAll():
            if prim.GetTypeName() != chunk_graph.CHUNK_TYPENAME:
                continue
            path = str(prim.GetPath())
            for attr in chunk_usd.iter_exec_inputs(prim):
                for conn in attr.GetConnections():
                    source = str(conn.GetPrimPath())
                    if path in inside and source not in inside and source not in feeds:
                        feeds.append(source)
                    elif source in inside and path not in inside and path not in consumers:
                        consumers.append(path)
        return feeds, consumers

    def group_selection(self):
        """Ctrl+G: put the selected chunks in a new chunkgroup.

        The sequence a user would do by hand -- read the boundary, cut, make a group,
        paste inside it, then wire the group in where the selection used to be -- so the
        group takes the selection's place in the chain rather than leaving a hole.
        """
        chosen = [p for p in self.selected_nodes if p in self.prims]
        if not chosen or self.scene is None:
            self.status = "nothing to group"
            return self.refresh()

        centre_x = sum(self.positions[p][0] for p in chosen) / len(chosen)
        centre_y = sum(self.positions[p][1] for p in chosen) / len(chosen)
        feeds, consumers = self._exec_boundary(set(chosen))

        nodes, edges = self.snapshot_selection()
        if not nodes:
            self.status = "nothing to group"
            return self.refresh()
        self.delete_selected()

        registry = chunk_registry.ChunkRegistry()
        module = registry.get_chunk_module(CHUNKGROUP_MODULE)
        if module is None:
            self.status = "no chunkgroup module"
            return self.refresh()
        made = module.create() if module.loaded else module.load()
        if not made:
            self.status = "could not create a chunkgroup"
            return self.refresh()
        group = made[0]

        previous = chunk_usd._create_node_pos
        chunk_usd._create_node_pos = self._layout_to_usd(centre_x, centre_y)
        try:
            self.scene.add_chunk(group)
        finally:
            chunk_usd._create_node_pos = previous
        group_path = get_chunk_prim_path(group)

        # Paste inside the group: pointing the scene's current context at it is what
        # diving in before a paste amounts to.
        prev_ctx = self.scene._current_context
        self.scene._current_context = weakref.ref(group)
        try:
            pasted = self._materialize(nodes, centre_x, centre_y)
        finally:
            self.scene._current_context = prev_ctx

        # Wire the pasted chunks to each other directly rather than through ports: they
        # are inside a group this view is not showing, so they have no ports here.
        for edge in edges:
            src = pasted.get(edge.src_index)
            dst = pasted.get(edge.dst_index)
            if src is None or dst is None:
                continue
            if not (edge.src_attr.endswith("chunk") and edge.dst_attr.endswith("chunk")):
                continue  # a typed param wire, not an exec one
            chunk_usd.connect_exec(self.stage, self.scene,
                                   get_chunk_prim_path(src), get_chunk_prim_path(dst))

        # And put the group where the selection was in the chain.
        for source in feeds:
            chunk_usd.connect_exec(self.stage, self.scene, source, group_path)
        for consumer in consumers:
            chunk_usd.connect_exec(self.stage, self.scene, group_path, consumer)

        self.stage.GetRootLayer().Save()
        self.chunks = {get_chunk_prim_path(c): c for c in self.scene.get_chunks()}
        self.reload()
        self.set_selection([group_path] if group_path in self.prims else ())
        self.status = "grouped %d" % len(nodes)
        self.refresh(bodies=True)

    def _port(self, node_path: str, attr_suffix: str):
        """The PortRef for node_path.attr_suffix, or None if that port is not exposed."""
        wanted = "%s.%s" % (node_path, attr_suffix)
        return next((p for p in self.ports.get(node_path, []) if p.attr_path == wanted), None)

    def duplicate_selection(self):
        """Copy and paste in one step at a fixed offset, leaving the clipboard alone -- so
        duplicating never clobbers whatever was last explicitly copied."""
        nodes, edges = self.snapshot_selection()
        if not nodes:
            self.status = "nothing to duplicate"
            return self.refresh()
        chosen = [p for p in self.selected_nodes if p in self.positions]
        left = min(self.positions[p][0] for p in chosen)
        top = min(self.positions[p][1] for p in chosen)
        self.paste_at(left + 40, top + 40, nodes, edges)

    # -- keyboard ----------------------------------------------------------

    def on_key(self, event):
        """Graph shortcuts, ignored while a param is being typed into.

        Flet has no per-control key handling: a page handler sees every keystroke,
        including the ones meant for a TextField inside a node. Without this guard,
        typing a prim path would delete the node on Delete and toggle disable on "d".
        """
        if chunk_flet.is_editing():
            return
        key = (event.key or "").lower()
        if key == "escape":
            self.wiring = None
            self.set_selection(())
            self.status = ""
            return self.refresh()
        if key == "delete":
            return self.delete_selected()
        if key == "d" and not event.ctrl:
            return self.toggle_disable_selected()
        if key == "f":
            return self.frame(self.selected_nodes or self.prims)
        if key == "c" and event.ctrl:
            return self.copy_selection()
        if key == "x" and event.ctrl:
            return self.cut_selection()
        if key == "v" and event.ctrl:
            # A key event carries no pointer position in Flet, so paste lands at the
            # middle of the viewport rather than at the cursor as it does in Qt.
            return self.paste_at(*self._xy_of_viewport_centre())
        if key == "d" and event.ctrl:
            return self.duplicate_selection()
        if key == "g" and event.ctrl:
            return self.group_selection()
        if key == "a" and event.ctrl:
            self.set_selection(self.prims)
            self.status = f"selected {len(self.selected_nodes)}"
            return self.refresh()

    def _xy_from_screen(self, x: float, y: float) -> tuple[float, float]:
        return ((x - self.pan[0]) / self.zoom, (y - self.pan[1]) / self.zoom)

    def _xy_of_viewport_centre(self) -> tuple[float, float]:
        vw = (self.page.width or 1200.0) if self.page is not None else 1200.0
        vh = (self.page.height or 800.0) if self.page is not None else 800.0
        return self._xy_from_screen(vw / 2, vh / 2)

    def delete_selected(self):
        chunks = self.selected_chunks()
        if not chunks:
            self.status = "nothing selected"
            return self.refresh()
        for chunk in chunks:
            chunk.request_delete()
        self.stage.GetRootLayer().Save()
        self.selected_nodes = set()
        self.chunks = {get_chunk_prim_path(c): c for c in self.scene.get_chunks()}
        self.status = f"deleted {len(chunks)}"
        self.reload()
        self.refresh(bodies=True)

    def toggle_disable_selected(self):
        chunks = self.selected_chunks()
        if not chunks:
            self.status = "nothing selected"
            return self.refresh()
        for chunk in chunks:
            if chunk.has_state(ChunkFlags.State.DISABLED):
                chunk.unset_state(ChunkFlags.State.DISABLED)
            else:
                chunk.set_state(ChunkFlags.State.DISABLED)
        self.stage.GetRootLayer().Save()
        self.status = f"toggled {len(chunks)}"
        self.reload()
        self.refresh(bodies=True)

    def frame(self, paths):
        """Fit `paths` in the viewport -- Qt's F.

        Needs the viewport size, which only the page knows; with no page (tests, headless)
        the zoom is still solved so the maths stays testable, using a nominal viewport.
        """
        paths = [p for p in paths if p in self.positions]
        if not paths:
            return self.refresh()
        xs = [self.positions[p][0] for p in paths]
        ys = [self.positions[p][1] for p in paths]
        left, top = min(xs), min(ys)
        right = max(x + NODE_W for x in xs)
        bottom = max(self.positions[p][1] + self.node_height(p) for p in paths)
        vw = (self.page.width or 1200.0) if self.page is not None else 1200.0
        vh = (self.page.height or 800.0) if self.page is not None else 800.0
        vh = max(1.0, vh - 60.0)  # the status bar is not canvas
        pad = 40.0
        zoom = min((vw - pad * 2) / max(1.0, right - left),
                   (vh - pad * 2) / max(1.0, bottom - top))
        self.zoom = min(ZOOM_MAX, max(ZOOM_MIN, zoom))
        # Centre what was framed rather than pinning it to a corner.
        self.pan = (vw / 2 - (left + right) / 2 * self.zoom,
                    vh / 2 - (top + bottom) / 2 * self.zoom)
        self.status = f"framed {len(paths)}"
        self.refresh()

    # -- creating nodes ----------------------------------------------------

    def menu_entries(self):
        """Every chunk the registry offers, as the frontend-agnostic MenuEntry list.

        Qt renders the same list. A module that declares ChunkConfig.variants therefore
        gains them here too, without this file knowing variants exist.
        """
        try:
            return chunk_registry.ChunkRegistry().get_menu_entries()
        except Exception as exc:  # noqa: BLE001 -- a broken registry must not kill the view
            self.status = f"no chunk menu ({type(exc).__name__})"
            return []

    def create_chunk(self, entry, x: float, y: float):
        """Create one menu entry's chunk(s) at a layout position.

        The same sequence Qt runs: set the module-level position hook chunk_usd reads
        while a chunk is being added, add through the scene so the normal load path
        authors the prim, then clear the hook. Bypassing add_chunk and writing a prim
        directly would skip the registration the rest of the framework relies on.
        """
        if self.scene is None:
            self.status = "no chunk session; cannot create"
            return self.refresh()
        previous = chunk_usd._create_node_pos
        chunk_usd._create_node_pos = self._layout_to_usd(x, y)
        chunk_usd._pin_new_chunks = False
        try:
            created = entry.create()
            for c in created:
                self.scene.add_chunk(c)
        except Exception as exc:  # noqa: BLE001 -- report, do not take the window down
            self.status = f"create failed: {type(exc).__name__}: {exc}"
            created = []
        finally:
            chunk_usd._create_node_pos = previous
            chunk_usd._pin_new_chunks = None

        if created:
            self.stage.GetRootLayer().Save()
            self.chunks = {get_chunk_prim_path(c): c for c in self.scene.get_chunks()}
            self.status = f"created {created[0].name}"
            self.reload()
        self.refresh(bodies=True)

    def open_create_menu(self, x: float, y: float):
        """A searchable list of chunks, landing the choice at (x, y).

        Flet has no context-menu primitive that can pop at an arbitrary point, so this is
        a dialog. The search field matches MenuEntry.search_terms, the same terms Qt's
        filterable menu matches, so typing "trans" finds the same entries in both.
        """
        entries = self.menu_entries()
        if not entries or self.page is None:
            return self.refresh()

        results = ft.ListView(spacing=1, height=360)

        def show(term: str = ""):
            term = term.strip().lower()
            results.controls = [
                ft.TextButton(
                    " / ".join(e.namespace) or e.label,
                    tooltip=e.tooltip or None,
                    on_click=lambda _ev, entry=e: pick(entry),
                )
                for e in entries
                if not term or any(term in t for t in e.search_terms)
            ][:200]  # a long list is a slow dialog; refine the search instead
            if self.live:
                results.update()

        def pick(entry):
            self.page.close(dialog)
            self.create_chunk(entry, x, y)

        field = ft.TextField(label="chunk", autofocus=True, dense=True,
                             on_change=lambda ev: show(ev.control.value))
        show()
        dialog = ft.AlertDialog(
            title=ft.Text("New chunk", size=14),
            content=ft.Column([field, results], width=420, height=420, tight=True),
            actions=[ft.TextButton("Cancel", on_click=lambda _ev: self.page.close(dialog))],
        )
        self.page.open(dialog)

    def on_scroll(self, event):
        """Wheel zoom, anchored on the pointer.

        The layout point under the cursor is computed BEFORE the zoom changes and the pan
        is then solved so that same point lands back under the cursor. Scaling about the
        origin instead is the usual bug: the graph appears to flee the pointer, and at 3x
        whatever you were looking at is off-screen.
        """
        point = event.local_position
        lx, ly = self._xy(event)
        step = ZOOM_STEP if event.scroll_delta.y < 0 else 1.0 / ZOOM_STEP
        zoom = min(ZOOM_MAX, max(ZOOM_MIN, self.zoom * step))
        if zoom == self.zoom:
            return
        self.zoom = zoom
        self.pan = (point.x - lx * zoom, point.y - ly * zoom)
        self.status = f"zoom {zoom:.2f}x"
        self.refresh()

    def reset_view(self):
        """Back to 1:1, top-left. The escape hatch when a zoom has lost the graph."""
        self.zoom, self.pan = 1.0, (0.0, 0.0)
        self.status = "view reset"
        self.refresh()

    def on_pan_end(self, event):
        if self.panning:
            self.panning = False
            return
        if self.wiring is not None:
            source, self.wiring = self.wiring, None
            target = self.port_at(*self.wire_to)
            if target is None:
                self.status = ""
            elif target.attr_path == source.attr_path:
                self.status = ""
            else:
                change = chunk_graph.connect(self.stage, source, target, self.chunks)
                if change is None:
                    self.status = (f"cannot connect {source.name} ({source.port_type}) "
                                   f"to {target.name} ({target.port_type})")
                else:
                    self.stage.GetRootLayer().Save()
                    self.status = (f"connected {source.name} -> {target.name}"
                                   + (f" (expression refused: {change.expression_error})"
                                      if change.expression_error else ""))
                    self.reload()
            self.selected = None
            return self.refresh()
        if not self.dragging:
            return
        # Back into USD, in USD's own coordinates. The position is the shared state that
        # makes this view and the Qt one show the same arrangement of the same file.
        x, y = self.positions[self.dragging]
        prim = self.prims[self.dragging]
        attr = prim.GetAttribute(chunk_graph.POS_ATTR)
        if not attr:
            attr = prim.CreateAttribute(chunk_graph.POS_ATTR, Sdf.ValueTypeNames.Float2)
        attr.Set(Gf.Vec2f(*(float(v) for v in self._layout_to_usd(x, y))))
        self.stage.GetRootLayer().Save()
        self.status = f"moved {self.dragging.rsplit('/', 1)[-1]}"
        self.dragging = None
        self.refresh()

    # -- drawing -----------------------------------------------------------

    def shapes(self):
        out = []
        if self.wiring is not None:
            x1, y1 = self.port_xy(self.wiring)
            x2, y2 = self.wire_to
            reach = max(40.0, abs(x2 - x1) * 0.5)
            out.append(cv.Path(
                [cv.Path.MoveTo(x1, y1),
                 cv.Path.CubicTo(x1 + reach, y1, x2 - reach, y2, x2, y2)],
                paint=ft.Paint(stroke_width=1.6, color=COL_ACCENT,
                               style=ft.PaintingStyle.STROKE)))
        for edge in self.edges:
            src_node, dst_node = edge.src_node_path, edge.dst_node_path
            if src_node not in self.positions or dst_node not in self.positions:
                continue
            src = next((p for p in self.ports[src_node]
                        if p.attr_path == edge.src_attr_path), None)
            dst = next((p for p in self.ports[dst_node]
                        if p.attr_path == edge.dst_attr_path), None)
            if src is None or dst is None:
                continue
            x1, y1 = self.port_xy(src)
            x2, y2 = self.port_xy(dst)
            # An expression-driven wire takes the accent, the way Qt highlights it.
            colour = COL_ACCENT if edge.expression else PORT_COLOUR.get(src.port_type, "#888888")
            reach = max(40.0, abs(x2 - x1) * 0.5)
            out.append(cv.Path(
                [cv.Path.MoveTo(x1, y1),
                 cv.Path.CubicTo(x1 + reach, y1, x2 - reach, y2, x2, y2)],
                paint=ft.Paint(stroke_width=2.0 if edge.is_exec else 1.2, color=colour,
                               style=ft.PaintingStyle.STROKE)))
            if edge.expression:
                # The straight midpoint is also the curve's own midpoint here: the control
                # points are offset horizontally by the same reach from each end, so the
                # cubic at t=0.5 reduces to ((x1+x2)/2, (y1+y2)/2). The badge sits ON the
                # wire rather than beside it, which is why Qt picks the same point.
                mx, my = (x1 + x2) / 2, (y1 + y2) / 2
                out.append(cv.Circle(mx, my, EXP_R, paint=ft.Paint(color=colour)))
                out.append(cv.Text(
                    mx, my, "Exp", ft.TextStyle(size=9, weight=ft.FontWeight.BOLD,
                                                color="#ffffff"),
                    alignment=ft.Alignment.CENTER))

        for path, prim in self.prims.items():
            x, y = self.positions[path]
            height = self.node_height(path)
            off = chunk_graph.is_disabled(prim)
            # The body is a real control now (see node_controls); the canvas keeps the
            # wires and the ports, which have to sit on the same surface as the curves
            # that meet them.
            for port in self.ports[path]:
                px, py = self.port_xy(port)
                if self.selected is not None and port.attr_path == self.selected.attr_path:
                    out.append(cv.Circle(px, py, PORT_R + 3, paint=ft.Paint(color=COL_ACCENT)))
                out.append(cv.Circle(px, py, PORT_R,
                                     paint=ft.Paint(color=PORT_COLOUR.get(port.port_type,
                                                                          "#888888"))))
                # No label on a param port: the control it sits beside is already
                # labelled, and printing the name twice is noise that also collides with
                # the wires. Exec ports keep theirs -- they have no control to borrow one
                # from -- and the header names the node.
                if port.port_type == chunk_graph.PORT_EXEC:
                    out.append(cv.Text(
                        px - PORT_R - 5 if port.is_input else px + PORT_R + 5, py - 7,
                        port.name, ft.TextStyle(size=9, color="#8f8f8f"),
                        alignment=ft.Alignment.TOP_RIGHT if port.is_input
                        else ft.Alignment.TOP_LEFT))
        return out

    def node_controls(self):
        """One positioned Container per node, holding that chunk's real param widgets.

        Qt embeds the actual ChunkWidget on its canvas through a proxy; a Flet canvas
        cannot host controls at all, so the graph is a Stack -- wires drawn on a canvas
        underneath, node bodies as ordinary positioned controls on top. Same result, and
        the params are live: moving a slider here changes the chunk.

        The widgets are kept on self because nothing else holds them. Each registers a
        notice callback, and a collected widget stops updating.
        """
        self.param_widgets = []
        controls = []
        for path, prim in self.prims.items():
            x, y = self.positions[path]
            chunk_obj = self.chunks.get(path)
            body = [ft.Container(
                ft.Text(chunk_graph.node_label(prim), size=12, color=COL_TEXT,
                        weight=ft.FontWeight.W_500),
                bgcolor="#242424" if chunk_graph.is_disabled(prim) else COL_NODE_HEADER,
                padding=ft.Padding.symmetric(vertical=5, horizontal=10),
                border_radius=ft.BorderRadius.only(top_left=6, top_right=6),
                width=NODE_W)]

            if chunk_obj is not None:
                param_controls, widgets, offsets = chunk_flet.build_chunk_controls(chunk_obj)
                # Where each param's control ended up, so a port lands level with it.
                self.param_offsets[path] = offsets
                if self.live:
                    chunk_flet.mark_live(widgets)
                self.param_widgets += widgets
                # No height cap and no inner scroll: a capped body silently hides params,
                # which is exactly what a node graph must not do -- you cannot tell a chunk
                # with four params from one with fourteen.
                body.append(ft.Container(
                    ft.Column(param_controls, spacing=3, tight=True),
                    padding=8, width=NODE_W,
                ))

            controls.append(ft.Container(
                ft.Column(body, spacing=0, tight=True),
                data=path,  # so a move can find this control without rebuilding it
                left=x, top=y, width=NODE_W,
                bgcolor="#2f2f2f" if chunk_graph.is_disabled(prim) else COL_NODE_BG,
                # Selection reads as a thicker accent border, the way Qt draws it. The
                # body is a real control sitting ON the canvas, so a highlight drawn
                # underneath as a canvas shape would be hidden by it.
                border=(ft.Border.all(2, COL_ACCENT) if path in self.selected_nodes
                        else ft.Border.all(1, COL_NODE_BORDER)),
                border_radius=6,
            ))
        return controls

    def extent(self):
        width = max((x for x, _ in self.positions.values()), default=0) + NODE_W + MARGIN * 2
        height = max((y for _, y in self.positions.values()), default=0) + 300
        return width, height

    def summary(self):
        return (f"{self.context}   —   {len(self.prims)} nodes · {len(self.edges)} wires · "
                f"{sum(len(v) for v in self.ports.values())} ports · "
                f"{len(self.available)} context(s)"
                + (f"   —   {self.session_error}" if self.session_error else "")
                + (f"   —   {self.status}" if self.status else ""))

    def refresh(self, bodies: bool = False):
        if not self.live:
            return
        """Redraw. `bodies` rebuilds the node controls too, which is only needed when the
        set of nodes changed -- rebuilding them on every drag frame would recreate every
        param widget sixty times a second and drop the notice callbacks they hold."""
        # Same ordering rule as main(): the bodies report the offsets the ports need.
        rebuilt = self.node_controls() if (bodies and self.stack is not None) else None
        if self.pan_container is not None:
            # One transform for the whole scene -- canvas AND the real param widgets in
            # the Stack -- so the wires cannot drift away from the bodies they touch.
            self.pan_container.scale = ft.Scale(self.zoom, alignment=ft.Alignment.TOP_LEFT)
            self.pan_container.left, self.pan_container.top = self.pan
        if self.canvas is not None:
            self.canvas.shapes = self.shapes()
            self.canvas.width, self.canvas.height = self.extent()
            self.canvas.update()
        if rebuilt is not None:
            self.stack.controls = [self.canvas] + rebuilt
            self.stack.update()
        elif self.stack is not None:
            # Positions moved but the controls are the same objects: move them in place.
            for control in self.stack.controls[1:]:
                path = getattr(control, "data", None)
                if path in self.positions:
                    control.left, control.top = self.positions[path]
            self.stack.update()
        if self.status_text is not None:
            self.status_text.value = self.summary()
            self.status_text.update()


def main(page: ft.Page, network: str, context: str | None = None):
    view = GraphView(network, context)
    view.page = page
    page.on_keyboard_event = view.on_key
    page.title = f"Chunk graph — {network.rsplit('/', 1)[-1]}"
    page.bgcolor = COL_CANVAS
    page.padding = 0

    width, height = view.extent()
    # Bodies BEFORE shapes. node_controls is what discovers where each param's control
    # landed, and shapes() needs those offsets to put a port level with its param -- built
    # the other way round the ports fall back to a fixed grid and silently miss.
    bodies = view.node_controls()
    view.canvas = cv.Canvas(view.shapes(), width=width, height=height)
    view.status_text = ft.Text(view.summary(), size=12, color=COL_ACCENT)
    # Canvas first in the Stack so the wires pass UNDER the node bodies, as in Qt.
    view.stack = ft.Stack([view.canvas] + bodies, width=width, height=height)

    # The scene sits in a positioned, scaled Container; the GestureDetector wraps the
    # viewport OUTSIDE it. That order matters: events must arrive in unscaled screen
    # coordinates for _xy to invert, and the detector has to cover the whole viewport
    # rather than just the drawn extent, or empty space would not be draggable.
    view.pan_container = ft.Container(view.stack, left=0, top=0,
                                      scale=ft.Scale(1.0, alignment=ft.Alignment.TOP_LEFT))
    viewport = ft.Stack([view.pan_container], expand=True, clip_behavior=ft.ClipBehavior.HARD_EDGE)

    page.add(
        ft.Column([
            ft.Container(view.status_text, padding=10, bgcolor=COL_NODE_HEADER),
            ft.Container(
                ft.GestureDetector(
                    content=viewport,
                    on_tap_down=view.on_tap,
                    on_secondary_tap_down=view.on_secondary_tap,
                    on_pan_start=view.on_pan_start,
                    on_pan_update=view.on_pan_update,
                    on_pan_end=view.on_pan_end,
                    on_scroll=view.on_scroll,
                ),
                expand=True,
            ),
        # No ScrollMode: panning replaces it. Left on, a wheel event would both scroll
        # the Column and zoom the graph, and the two fight.
        ], expand=True)
    )
    # Only now may the widgets speak to the client: before page.add their controls are not
    # mounted, and update() on an unmounted control can block rather than fail.
    chunk_flet.mark_live(view.param_widgets)
    view.live = True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m usd_chunks.plugins.chunk_flet_nodegraph <network.usda>")
    target = sys.argv[1]
    wanted = sys.argv[2] if len(sys.argv) > 2 else None
    ft.run(lambda page: main(page, target, wanted))
