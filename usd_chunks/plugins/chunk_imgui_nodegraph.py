"""A Dear ImGui view of a chunk network — the third frontend over one engine.

    python -m usd_chunks.plugins.chunk_imgui_nodegraph <network.usda> [context_prim_path]

Written to test whether the engine is genuinely frontend-agnostic or merely Qt-and-Flet
agnostic. ImGui is a different paradigm from both: immediate mode, no widget objects, no
retained scene. See docs/graph-frontends.md.

What this frontend does NOT contain, compared with the Flet one, is the interesting part:

- no `port_at` / `node_at` — imgui_node_editor owns hit-testing
- no zoom, pan or canvas transform — the editor owns navigation
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
import weakref

from imgui_bundle import hello_imgui, imgui, imgui_node_editor as ed
from pxr import Gf, Sdf, Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_scene import CHUNKGROUP_MODULE, SceneSender
from usd_chunks.chunk_registry import ChunkLayerManager
from usd_chunks.plugins import chunk_graph, chunk_graph_layout, chunk_imgui, chunk_usd
from usd_chunks.plugins.chunk_usd import get_chunk_prim_path, load_chunks

logger = logging.getLogger(__name__)

NODE_W = 220.0


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

    def key(self, value) -> "str | None":
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
            chunk_usd.ChunkStageSync().initialize(self.stage, self.scene)
            load_chunks(self.stage, self.scene)
            self.chunks = {get_chunk_prim_path(c): c for c in self.scene.get_chunks()}
        except (AttributeError, KeyError, RuntimeError, OSError, ValueError) as exc:
            self.session_error = f"no chunk session ({type(exc).__name__}); drawing only"

        self.available = self._contexts()
        self.context = context or (self.available[0] if self.available
                                   else chunk_graph.CHUNKS_ROOT)
        self.ids = _Ids()
        self.status = ""
        self.selected_nodes: set[str] = set()
        self._placed: set[str] = set()  # nodes whose position has been pushed to the editor
        self._editor = None
        self.reload()

    # -- reading -----------------------------------------------------------

    def _contexts(self) -> list:
        found, pending = [], [chunk_graph.CHUNKS_ROOT]
        while pending:
            root = pending.pop(0)
            nodes = chunk_graph.resolve_graph_nodes(self.stage, root)
            if not nodes:
                continue
            found.append(root)
            pending += [str(p.GetPath()) for p in nodes]
        return found

    def reload(self):
        self.available = self._contexts()
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

        ed.begin_node(node_id)
        label = chunk_graph.node_label(prim)
        if chunk_graph.is_disabled(prim):
            label += "  (disabled)"
        imgui.text(label)
        imgui.dummy(imgui.ImVec2(NODE_W, 1.0))

        ins = [p for p in self.ports[path] if p.is_input]
        outs = [p for p in self.ports[path] if not p.is_input]
        for port in ins:
            ed.begin_pin(ed.PinId(self.ids.get(port.attr_path)), ed.PinKind.input)
            imgui.text(f"-> {port.name}")
            ed.end_pin()

        chunk = self.chunks.get(path)
        if chunk is not None:
            for param in chunk_imgui.visible_params(chunk):
                chunk_imgui.draw_param(param, width=NODE_W - 60)

        for port in outs:
            ed.begin_pin(ed.PinId(self.ids.get(port.attr_path)), ed.PinKind.output)
            imgui.text(f"{port.name} ->")
            ed.end_pin()
        ed.end_node()

    def _draw_edge(self, edge):
        src = self.ids.get(edge.src_attr_path)
        dst = self.ids.get(edge.dst_attr_path)
        link_id = self.ids.get(f"link:{edge.src_attr_path}->{edge.dst_attr_path}")
        ed.link(ed.LinkId(link_id), ed.PinId(src), ed.PinId(dst))

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
        ):
            if imgui.is_key_pressed(key):
                action()
                return

    # -- the contract ------------------------------------------------------

    def set_selection(self, paths, add: bool = False) -> None:
        paths = set(paths)
        self.selected_nodes = (self.selected_nodes | paths) if add else paths
        for path, chunk in self.chunks.items():
            try:
                if path in self.selected_nodes:
                    chunk.set_state(ChunkFlags.State.SELECTED)
                else:
                    chunk.unset_state(ChunkFlags.State.SELECTED)
            except AttributeError:
                continue

    def selected_chunks(self) -> list:
        return [c for p, c in self.chunks.items() if p in self.selected_nodes]

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
        for chunk in chunks:
            if chunk.has_state(ChunkFlags.State.DISABLED):
                chunk.unset_state(ChunkFlags.State.DISABLED)
            else:
                chunk.set_state(ChunkFlags.State.DISABLED)
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
        chunk_graph.node_clipboard[:] = nodes
        chunk_graph.edge_clipboard[:] = edges
        self.status = f"copied {len(nodes)}"

    def cut_selection(self) -> None:
        nodes, edges = self.snapshot_selection()
        if not nodes:
            self.status = "nothing to cut"
            return
        chunk_graph.node_clipboard[:] = nodes
        chunk_graph.edge_clipboard[:] = edges
        self.delete_selected()
        self.status = f"cut {len(nodes)}"

    def _nest_target(self):
        chunk = self.chunks.get(self.context)
        if chunk is not None and chunk_usd.is_group_chunk(chunk):
            return chunk
        return None

    def _materialize(self, nodes, x: float, y: float) -> dict:
        registry = chunk_registry.ChunkRegistry()
        made = {}
        for i, item in enumerate(nodes):
            module = registry.get_chunk_module(item.module_name)
            if module is None:
                continue
            chunks = module.create() if module.loaded else module.load()
            if not chunks:
                continue
            primary = chunks[0]
            chunk_graph.apply_list_sizes(primary, getattr(item, "list_sizes", []))
            chunk_graph.apply_param_values(primary, item.values)
            position = (x + item.rel_pos[0], -(y + item.rel_pos[1]))
            with chunk_usd.creating_at(position):
                for c in chunks:
                    self.scene.add_chunk(c)
            made[i] = primary
        return made

    def paste_at(self, x: float, y: float, nodes=None, edges=None) -> None:
        nodes = chunk_graph.node_clipboard if nodes is None else nodes
        edges = chunk_graph.edge_clipboard if edges is None else edges
        if not nodes or self.scene is None:
            self.status = "clipboard empty"
            return
        with self.scene.creating_in(self._nest_target()):
            made = self._materialize(nodes, x, y)
        created = {i: get_chunk_prim_path(c) for i, c in made.items()}
        self.stage.GetRootLayer().Save()
        self.chunks = {get_chunk_prim_path(c): c for c in self.scene.get_chunks()}
        self.reload()
        for edge in edges:
            src_path, dst_path = created.get(edge.src_index), created.get(edge.dst_index)
            if not src_path or not dst_path:
                continue
            src = next((p for p in self.ports.get(src_path, [])
                        if p.attr_path == f"{src_path}.{edge.src_attr}"), None)
            dst = next((p for p in self.ports.get(dst_path, [])
                        if p.attr_path == f"{dst_path}.{edge.dst_attr}"), None)
            if src is not None and dst is not None:
                chunk_graph.connect(self.stage, src, dst, self.chunks)
        if edges:
            self.stage.GetRootLayer().Save()
            self.reload()
        self.set_selection(created.values())
        self.status = f"pasted {len(created)}"

    def duplicate_selection(self) -> None:
        nodes, edges = self.snapshot_selection()
        if not nodes:
            self.status = "nothing to duplicate"
            return
        chosen = [p for p in self.selected_nodes if p in self.positions]
        positions = self.positions
        left = min(positions[p][0] for p in chosen)
        top = min(positions[p][1] for p in chosen)
        self.paste_at(left + 40, top + 40, nodes, edges)

    def group_selection(self) -> None:
        chosen = [p for p in self.selected_nodes if p in self.prims]
        if not chosen or self.scene is None:
            self.status = "nothing to group"
            return
        positions = self.positions
        centre_x = sum(positions[p][0] for p in chosen) / len(chosen)
        centre_y = sum(positions[p][1] for p in chosen) / len(chosen)
        feeds, consumers = chunk_graph.exec_boundary(self.stage, set(chosen))
        nodes, edges = self.snapshot_selection()
        if not nodes:
            return
        self.delete_selected()

        registry = chunk_registry.ChunkRegistry()
        module = registry.get_chunk_module(CHUNKGROUP_MODULE)
        made = (module.create() if module.loaded else module.load()) if module else None
        if not made:
            self.status = "could not create a chunkgroup"
            return
        group = made[0]
        with chunk_usd.creating_at((centre_x, -centre_y)):
            self.scene.add_chunk(group)
        group_path = get_chunk_prim_path(group)

        with self.scene.creating_in(group):
            pasted = self._materialize(nodes, centre_x, centre_y)
        for edge in edges:
            src, dst = pasted.get(edge.src_index), pasted.get(edge.dst_index)
            if src is None or dst is None:
                continue
            if edge.src_attr.endswith("chunk") and edge.dst_attr.endswith("chunk"):
                chunk_usd.connect_exec(self.stage, self.scene,
                                       get_chunk_prim_path(src), get_chunk_prim_path(dst))
        for source in feeds:
            chunk_usd.connect_exec(self.stage, self.scene, source, group_path)
        for consumer in consumers:
            chunk_usd.connect_exec(self.stage, self.scene, group_path, consumer)
        self.stage.GetRootLayer().Save()
        self.chunks = {get_chunk_prim_path(c): c for c in self.scene.get_chunks()}
        self.reload()
        self.status = f"grouped {len(nodes)}"

    def menu_entries(self):
        try:
            return chunk_registry.ChunkRegistry().get_menu_entries()
        except (AttributeError, RuntimeError, OSError):
            return []

    def create_chunk(self, entry, x: float, y: float) -> None:
        if self.scene is None:
            self.status = "no chunk session; cannot create"
            return
        created = []
        try:
            with chunk_usd.creating_at((x, -y), pin=False), \
                    self.scene.creating_in(self._nest_target()):
                created = entry.create()
                for c in created:
                    self.scene.add_chunk(c)
        except (AttributeError, KeyError, TypeError, ValueError, RuntimeError) as exc:
            self.status = f"create failed: {type(exc).__name__}: {exc}"
            created = []
        if created:
            self.stage.GetRootLayer().Save()
            self.chunks = {get_chunk_prim_path(c): c for c in self.scene.get_chunks()}
            self.status = f"created {created[0].name}"
            self.reload()

    def can_dive(self, path: str) -> bool:
        chunk = self.chunks.get(path)
        return chunk is not None and chunk_usd.is_chunkgroup(chunk)

    def dive_into(self, path: str) -> None:
        if not self.can_dive(path):
            return
        self.context = path
        self.selected_nodes = set()
        self._placed = set()  # the inside of a group has its own coordinates
        self.status = f"in {path.rsplit('/', 1)[-1]}"
        self.reload()

    def dive_out(self) -> None:
        if not self.context or self.context == chunk_graph.CHUNKS_ROOT:
            self.status = "already at the top"
            return
        parent = self.context.rsplit("/", 1)[0]
        while parent:
            if parent in self.available:
                break
            if "/" not in parent.lstrip("/"):
                parent = chunk_graph.CHUNKS_ROOT
                break
            parent = parent.rsplit("/", 1)[0]
        inner, self.context = self.context, parent or chunk_graph.CHUNKS_ROOT
        self.selected_nodes = set()
        self._placed = set()
        self.reload()
        if inner in self.prims:
            self.set_selection([inner])

    def active_slot(self) -> int:
        prim = self.stage.GetPrimAtPath(Sdf.Path(self.context))
        if not (prim and prim.IsValid()):
            return 0
        attr = prim.GetAttribute("chunk:activeView")
        return int(attr.Get() or 0) if attr and attr.IsValid() else 0

    def set_active_slot(self, slot: int) -> None:
        prim = self.stage.GetPrimAtPath(Sdf.Path(self.context))
        if not (prim and prim.IsValid()) or self.scene is None:
            return
        context_chunk = self.chunks.get(self.context)
        view_chunk = None
        if slot:
            view_chunk = chunk_usd.find_view_chunk_for_slot(
                slot, self.stage, self.scene, context_chunk)
            if view_chunk is None:
                self.status = f"no view in slot {slot}"
                return
            attr = prim.GetAttribute("chunk:activeView")
            if not (attr and attr.IsValid()):
                attr = prim.CreateAttribute("chunk:activeView", Sdf.ValueTypeNames.Int,
                                            custom=True)
            attr.Set(int(slot))
        elif (attr := prim.GetAttribute("chunk:activeView")) and attr.IsValid():
            prim.RemoveProperty("chunk:activeView")
        try:
            chunk_usd.recook_for_view(self.stage, self.scene, context_chunk, view_chunk)
        except Exception as exc:  # noqa: BLE001 -- a recook runs arbitrary chunk code
            logger.exception("recook failed while switching view slot")
            self.status = f"recook failed: {type(exc).__name__}"
        else:
            self.status = f"view {slot} active" if slot else "no active view"
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

    def auto_layout(self) -> None:
        if not self.prims:
            return
        up_of, down_of = chunk_graph.layout_adjacency(self.prims)
        seeds = [p for p in self.selected_nodes if p in self.prims]
        if seeds:
            grown = chunk_graph_layout.grow_to_connected_subgraph(seeds, up_of, down_of)
            node_ids = sorted(grown & set(self.prims))
        else:
            node_ids = sorted(self.prims)
        if not node_ids:
            return
        wanted = set(node_ids)
        positions = self.positions
        placed = chunk_graph_layout.layout_positions(
            node_ids,
            size_of=lambda n: (NODE_W, 120.0),
            is_locked=lambda n: False,
            current_pos=lambda n: positions[n],
            up_of={n: {u for u in up_of.get(n, ()) if u in wanted} for n in node_ids},
            down_of={n: {d for d in down_of.get(n, ()) if d in wanted} for n in node_ids},
        )
        if self._editor is not None:
            ed.set_current_editor(self._editor)
        for path, (x, y) in placed.items():
            if self._editor is not None:
                ed.set_node_position(ed.NodeId(self.ids.get(path)),
                                     imgui.ImVec2(float(x), float(y)))
            prim = self.prims.get(path)
            if prim is not None:
                attr = prim.GetAttribute(chunk_graph.POS_ATTR)
                if not attr:
                    attr = prim.CreateAttribute(chunk_graph.POS_ATTR,
                                                Sdf.ValueTypeNames.Float2)
                attr.Set(Gf.Vec2f(float(x), float(-y)))
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

    scripts/displaymodes/display_modes.py is the one that bites here: it imports
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
            "usage: python -m usd_chunks.plugins.chunk_imgui_nodegraph <network.usda>")
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
