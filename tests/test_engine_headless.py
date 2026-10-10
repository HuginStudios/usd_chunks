"""The graph engine's shared operations, driven directly rather than through a frontend.

Every frontend (Qt, Flet, ImGui, Blender) calls these, and until now they were only
exercised through one of them -- so a regression here showed up as "the Flet paste test
failed", pointing at a frontend. These run them headless, the way the engine promises it
can be run (it imports no toolkit):

- copy/paste: value and list-size snapshots, name/disabled, snapshot_selection, paste_nodes,
- grouping: exec_boundary, attach_group, group_into_chunkgroup, nest_target,
- layout: layout_adjacency, layout_graph,
- views: context_has_view, ensure_default_view, default_view_slot, new_view_slot,
- set_param_visibly on a blocked chunk, and disconnecting from an output port.
"""
from __future__ import annotations

import pytest
from pxr import Sdf, Usd

from usd_chunks import chunk, chunk_cook, chunk_registry, chunk_stage
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.graph import engine as chunk_graph

from chunk_stubs import register_stub_module


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []

    def should_install_package(self, package):
        return False


def _main():
    c = chunk.Chunk("node")
    c.add_param(chunk.Slider("p", default_value=1.0))
    c.add_param(chunk.LineEdit("s", default_value="d"))
    c.add_param(chunk.Checkbox("b", default_value=False))
    c.add_param(chunk.Menu("m", menu_items=["a", "b"], default_value="a"))
    rows = chunk.ListGroup("rows")
    rows.add_param(chunk.Slider("item", default_value=0.0))
    c.add_param(rows)
    return c


class _Graph:
    """A session on an in-memory stage, with a stub module whose chunks have every param kind."""

    def __init__(self):
        self.stage = Usd.Stage.CreateInMemory()
        self.scene = _Scene(self.stage)
        chunk_registry.ChunkRegistry().initialize(self.scene, "pytest")
        chunk_registry.ChunkLayerManager().initialize(self.stage)
        chunk_stage.ChunkStageSync().initialize(self.stage, self.scene)
        self.module = register_stub_module(main=_main).module_name

    def make(self, name, pos=(0.0, 0.0)):
        (c,) = chunk.create_chunks(self.module)
        c.name = name
        chunk_graph.write_position(self.prim(c), *pos, owner=c)
        return c

    def path(self, c):
        return chunk_stage.get_chunk_prim_path(c)

    def prim(self, c):
        return self.stage.GetPrimAtPath(self.path(c))

    @property
    def context(self):
        any_chunk = next(c for c in self.scene.get_chunks() if c.get_config()
                         and c.get_config().module == self.module)
        return str(Sdf.Path(self.path(any_chunk)).GetParentPath())

    def view_state(self, context=None):
        """(chunks, prims, positions) for the nodes of a graph, as a frontend holds them."""
        context = context or self.context
        prims = {str(p.GetPath()): p for p in chunk_graph.resolve_graph_nodes(self.stage, context)}
        by_path = {self.path(c): c for c in self.scene.get_chunks()}
        chunks = {p: by_path[p] for p in prims if p in by_path}
        positions = {p: chunk_graph.graph_position(prim) for p, prim in prims.items()}
        return chunks, prims, positions

    def wire_exec(self, a, b, input_name=chunk_graph.EXEC_INPUT):
        chunk_stage.connect_exec(self.stage, self.scene, self.path(a), self.path(b),
                                 input_name=input_name, append=True)

    def wire_param(self, a, b, name="p"):
        pa, pb = self.path(a), self.path(b)
        chunks, _, _ = self.view_state()
        out = chunk_graph.PortRef(pa, f"{pa}.outputs:{name}", chunk_graph.PORT_FLOAT, False)
        inp = chunk_graph.PortRef(pb, f"{pb}.inputs:{name}", chunk_graph.PORT_FLOAT, True,
                                  param=b.get_param(name))
        return chunk_graph.connect(self.stage, out, inp, chunks)

    def exec_sources(self, c, input_name=chunk_graph.EXEC_INPUT):
        attr = self.prim(c).GetAttribute(input_name)
        return [str(conn.GetPrimPath()) for conn in attr.GetConnections()] if attr else []

    def of_module(self):
        return [c for c in self.scene.get_chunks()
                if c.get_config() and c.get_config().module == self.module]


@pytest.fixture()
def graph():
    return _Graph()


def _identity(x, y):
    return (x, y)


# -- value and list snapshots ------------------------------------------------------------------

def test_values_snapshot_onto_another_chunk_of_the_module(graph):
    a, b = graph.make("a"), graph.make("b")
    a.get_param("p").value = 5.0
    a.get_param("s").value = "x"
    a.get_param("b").value = True
    a.get_param("m").value = "b"

    chunk_graph.apply_param_values(b, chunk_graph.snapshot_param_values(a))

    assert (b.get_param("p").value, b.get_param("s").value,
            b.get_param("b").value, b.get_param("m").value) == (5.0, "x", True, "b")


def test_a_value_that_will_not_apply_is_skipped_not_fatal(graph):
    a = graph.make("a")
    values = chunk_graph.snapshot_param_values(a)
    values[0] = object()  # nothing a Slider accepts
    values[1] = "kept"

    chunk_graph.apply_param_values(a, values)

    assert a.get_param("s").value == "kept"


def test_hidden_and_transient_params_are_not_copied(graph):
    a = graph.make("a")
    hidden = a.add_param(chunk.Slider("internal", default_value=0.0))
    hidden.set_state(ChunkFlags.State.HIDDEN)

    names = [p.name for p in chunk_graph.iter_copyable_params(a)]

    assert "internal" not in names and "p" in names


def _fresh(graph):
    """A chunk of the module not yet in the scene -- what a paste applies a clipboard to."""
    (c,) = chunk_registry.ChunkRegistry().get_chunk_module(graph.module).create()
    return c


def test_list_sizes_grow_the_rows_back_before_values_land(graph):
    a, b = graph.make("a"), _fresh(graph)
    rows = a.get_param("rows")
    rows.size = 3
    rows.get_indexed_param("item", 2).value = 7.0
    clip = chunk_graph.snapshot_chunk_node(a, graph.scene, graph.stage, (0, 0))

    chunk_graph.apply_clipboard_node(b, clip)

    assert b.get_param("rows").size == 3
    assert b.get_param("rows").get_indexed_param("item", 2).value == pytest.approx(7.0)


def test_a_snapshot_keeps_the_name_and_disabled_state(graph):
    a = graph.make("named")
    a.set_state(ChunkFlags.State.DISABLED)
    clip = chunk_graph.snapshot_chunk_node(a, graph.scene, graph.stage, (0, 0))
    fresh = _fresh(graph)

    chunk_graph.apply_name(fresh, clip)
    graph.scene.add_chunk(fresh)
    chunk_graph.apply_disabled(fresh, clip)

    assert fresh.name.startswith("named") and fresh.name != "named"  # made unique on add
    assert fresh.has_state(ChunkFlags.State.DISABLED)


def test_a_chunk_without_a_module_has_no_snapshot(graph):
    bare = chunk.Chunk("bare")

    assert chunk_graph.snapshot_chunk_node(bare, graph.scene, graph.stage, (0, 0)) is None


# -- copy / paste through the engine -----------------------------------------------------------

def test_a_selection_snapshot_is_relative_to_its_top_left(graph):
    a, b = graph.make("a", (100, 50)), graph.make("b", (300, 80))
    chunks, prims, positions = graph.view_state()

    nodes, _ = chunk_graph.snapshot_selection(chunks, prims, positions, graph.scene, graph.stage,
                                             {graph.path(a), graph.path(b)})

    # In graph coordinates (graph_position), whose Y runs opposite to USD's.
    pa, pb = positions[graph.path(a)], positions[graph.path(b)]
    origin = (min(pa[0], pb[0]), min(pa[1], pb[1]))
    expected = sorted((p[0] - origin[0], p[1] - origin[1]) for p in (pa, pb))
    assert sorted(n.rel_pos for n in nodes) == expected
    assert (0.0, 0.0) in [(x, 0.0) for x, _ in expected] and expected[-1][0] == 200.0


def test_a_wire_with_one_end_outside_the_selection_is_dropped(graph):
    a, b, c = graph.make("a"), graph.make("b"), graph.make("c")
    graph.wire_exec(a, b)
    graph.wire_exec(b, c)
    chunks, prims, positions = graph.view_state()

    _, edges = chunk_graph.snapshot_selection(chunks, prims, positions, graph.scene, graph.stage,
                                             {graph.path(b), graph.path(c)})

    assert len(edges) == 1, "only b -> c has both ends copied"


def test_paste_recreates_nodes_values_and_the_wires_between_them(graph):
    a, b = graph.make("a", (0, 0)), graph.make("b", (200, 0))
    a.get_param("p").value = 4.0
    graph.wire_exec(a, b)
    graph.wire_param(a, b)
    chunks, prims, positions = graph.view_state()
    nodes, edges = chunk_graph.snapshot_selection(chunks, prims, positions, graph.scene,
                                                  graph.stage, {graph.path(a), graph.path(b)})
    before = set(map(id, graph.of_module()))

    made = chunk_graph.paste_nodes(graph.scene, graph.stage, nodes, edges, _identity,
                                   chunks=chunks)

    assert len(made) == 2
    new = [c for c in graph.of_module() if id(c) not in before]
    new_a = next(c for c in new if c.get_param("p").raw_value == 4.0)
    new_b = next(c for c in new if c is not new_a)
    assert graph.exec_sources(new_b) == [graph.path(new_a)], "the exec wire was not recreated"
    assert new_b.get_param("p").raw_value == f"@{new_a.name}.p@", "the param wire points at the original"
    new_a.get_param("p").value = 9.0
    assert new_b.get_param("p").value == pytest.approx(9.0)


def test_a_pasted_chunk_records_its_list_rows_on_its_prim(graph):
    # The rows land before the chunk has a prim, so only adding it can write them. Unwritten,
    # a pasted Wedge drove its outports until the save and reopened with no ranges at all.
    a = graph.make("a")
    rows = a.get_param("rows")
    rows.size = 2
    rows.get_indexed_param("item", 1).value = 7.0
    chunks, prims, positions = graph.view_state()
    nodes, edges = chunk_graph.snapshot_selection(chunks, prims, positions, graph.scene,
                                                  graph.stage, {graph.path(a)})

    (new,) = chunk_graph.paste_nodes(graph.scene, graph.stage, nodes, edges, _identity,
                                     chunks=chunks).values()

    attr = graph.prim(new).GetAttribute("inputs:rows:item")
    assert attr and list(attr.Get()) == pytest.approx([0.0, 7.0])


def test_pasting_nothing_makes_nothing(graph):
    assert chunk_graph.paste_nodes(graph.scene, graph.stage, [], [], _identity) == {}


def test_the_shared_clipboard_is_filled_in_place(graph):
    nodes_list, edges_list = chunk_graph.node_clipboard, chunk_graph.edge_clipboard
    a = graph.make("a")
    chunks, prims, positions = graph.view_state()
    nodes, edges = chunk_graph.snapshot_selection(chunks, prims, positions, graph.scene,
                                                  graph.stage, {graph.path(a)})

    assert chunk_graph.copy_to_clipboard(nodes, edges) == 1
    assert chunk_graph.node_clipboard is nodes_list and chunk_graph.edge_clipboard is edges_list
    chunk_graph.copy_to_clipboard([], [])


# -- grouping ----------------------------------------------------------------------------------

def test_exec_boundary_reads_feeds_and_consumer_slots(graph):
    a, b, m = graph.make("a"), graph.make("b"), graph.make("m")
    graph.wire_exec(a, b)
    graph.wire_exec(b, m, input_name="inputs:chunk1")

    feeds, consumers = chunk_graph.exec_boundary(graph.stage, {graph.path(b)})

    assert feeds == [graph.path(a)]
    assert consumers == [(graph.path(m), "inputs:chunk1")]


def test_attach_group_wires_a_node_into_the_old_slots(graph):
    a, g, m = graph.make("a"), graph.make("g"), graph.make("m")

    chunk_graph.attach_group(graph.stage, graph.scene, graph.path(g), [graph.path(a)],
                             [(graph.path(m), "inputs:chunk1")])

    assert graph.exec_sources(g) == [graph.path(a)]
    assert graph.exec_sources(m, "inputs:chunk1") == [graph.path(g)]


def test_grouping_puts_the_selection_in_a_group_that_takes_its_place(graph):
    a, b, c = graph.make("a", (0, 0)), graph.make("b", (200, 0)), graph.make("c", (400, 0))
    b.get_param("p").value = 6.0
    graph.wire_exec(a, b)
    graph.wire_exec(b, c)
    context = graph.context
    chunks, prims, positions = graph.view_state()

    group_path, moved = chunk_graph.group_into_chunkgroup(
        graph.stage, graph.scene, chunks, prims, positions, {graph.path(b)}, _identity)

    assert moved == 1 and group_path
    assert graph.exec_sources(c) == [group_path], "the consumer was not rewired to the group"
    assert [str(x.GetPrimPath()) for x in graph.stage.GetPrimAtPath(group_path)
            .GetAttribute(chunk_graph.EXEC_INPUT).GetConnections()] == [graph.path(a)]
    group = graph.scene.get_context_for_prim_path(group_path)
    inside = chunk_graph.iter_group_children(group, graph.scene, graph.stage)
    assert [ch.get_param("p").value for ch in inside] == [6.0]
    assert not graph.stage.GetPrimAtPath(f"{context}/b").IsValid(), "the original was left behind"


def test_grouping_nothing_does_nothing(graph):
    chunks, prims, positions = graph.view_state() if graph.scene.get_chunks() else ({}, {}, {})

    assert chunk_graph.group_into_chunkgroup(graph.stage, graph.scene, chunks, prims, positions,
                                             set(), _identity) == ("", 0)


def test_nest_target_is_the_group_being_shown_or_nothing(graph):
    a = graph.make("a")
    chunks, prims, positions = graph.view_state()
    group_path, _ = chunk_graph.group_into_chunkgroup(
        graph.stage, graph.scene, chunks, prims, positions, {graph.path(a)}, _identity)
    group = graph.scene.get_context_for_prim_path(group_path)

    assert chunk_graph.nest_target(group) is group
    assert chunk_graph.nest_target(graph.make("plain")) is None
    assert chunk_graph.nest_target(None) is None


# -- layout ------------------------------------------------------------------------------------

def test_layout_adjacency_counts_exec_and_param_wires(graph):
    a, b, c = graph.make("a"), graph.make("b"), graph.make("c")
    graph.wire_exec(a, b)
    graph.wire_param(a, c)
    _, prims, _ = graph.view_state()

    up_of, down_of = chunk_graph.layout_adjacency(prims)

    assert down_of[graph.path(a)] == {graph.path(b), graph.path(c)}
    assert up_of[graph.path(c)] == {graph.path(a)}


def test_layout_places_a_chain_left_to_right(graph):
    a, b, c = graph.make("a", (0, 0)), graph.make("b", (0, 0)), graph.make("c", (0, 0))
    graph.wire_exec(a, b)
    graph.wire_exec(b, c)
    _, prims, positions = graph.view_state()

    placed = chunk_graph.layout_graph(prims, lambda p: (100.0, 50.0), positions.get)

    xs = [placed.get(graph.path(n), positions[graph.path(n)])[0] for n in (a, b, c)]
    assert xs[0] < xs[1] < xs[2]


def test_layout_selected_reaches_only_the_connected_part(graph):
    a, b = graph.make("a"), graph.make("b")
    loner = graph.make("loner", (999, 999))
    graph.wire_exec(a, b)
    _, prims, positions = graph.view_state()

    placed = chunk_graph.layout_graph(prims, lambda p: (100.0, 50.0), positions.get,
                                      seeds={graph.path(a)})

    assert graph.path(loner) not in placed


def test_a_locked_node_does_not_move(graph):
    a, b = graph.make("a", (500, 500)), graph.make("b", (0, 0))
    graph.wire_exec(a, b)
    _, prims, positions = graph.view_state()

    placed = chunk_graph.layout_graph(prims, lambda p: (100.0, 50.0), positions.get,
                                      is_locked=lambda p: p == graph.path(a))

    assert placed.get(graph.path(a), positions[graph.path(a)]) == positions[graph.path(a)]


# -- views -------------------------------------------------------------------------------------

def _context_chunk(graph):
    return graph.scene.get_context_for_prim_path(graph.context)


def _count_executes(graph) -> list:
    """The chunks graph's stub module runs execute() on, from here on, in order."""
    ran = []
    module = chunk_registry.ChunkRegistry().get_chunk_module(graph.module).module
    module.execute = lambda node: ran.append(node.name)
    return ran


def test_a_chunk_created_beside_an_active_view_waits_until_it_is_wired(graph):
    """Only what the active view or Output reaches cooks. A new node is wired to nothing,
    so creating it cooks nothing -- not only when a frontend blocked it first."""
    a = graph.make("a")
    ctx = _context_chunk(graph)
    slot = chunk_graph.ensure_default_view(graph.stage, graph.scene, ctx)
    graph.stage.GetPrimAtPath(graph.context).CreateAttribute(
        "chunk:activeView", Sdf.ValueTypeNames.Int, custom=True).Set(slot)
    view = chunk_stage.find_view_chunk_for_slot(slot, graph.stage, graph.scene, ctx)
    ran = _count_executes(graph)

    # Not graph.make: its rename after creating is an edit, and this is about creating.
    (b,) = chunk.create_chunks(graph.module)
    assert ran == [], "created unwired, it cooks nothing"
    assert chunk_cook.is_cook_stale(b)

    # What a frontend does after a create (Flet, ImGui and Blender's _recook, Qt's own):
    # the recook blocks it, so an edit before it is wired does not cook it either.
    chunks, _, _ = graph.view_state()
    assert chunk_graph.recook_active(graph.stage, graph.scene, graph.context, chunks) == ""
    b.get_param("p").value = 2.0
    assert b.is_blocked() and b.name not in ran

    graph.wire_exec(a, b)
    chunk_stage.connect_exec(graph.stage, graph.scene, graph.path(b), graph.path(view))
    chunks, _, _ = graph.view_state()
    assert chunk_graph.recook_active(graph.stage, graph.scene, graph.context, chunks) == ""

    assert ran.count(b.name) == 1, "wired to the view, it cooks"
    assert not b.is_blocked()


def test_a_chunk_created_where_nothing_drives_the_graph_cooks():
    """A graph with no view and no Output is not view-driven -- a script building one --
    and a chunk created in it cooks straight away, as it always has."""
    graph = _Graph()
    ran = _count_executes(graph)

    (a,) = chunk.create_chunks(graph.module)

    assert ran == [a.name]


def test_a_fresh_context_gets_a_view_on_the_tail_of_its_chain(graph):
    a, b = graph.make("a"), graph.make("b")
    graph.wire_exec(a, b)
    ctx = _context_chunk(graph)
    assert not chunk_graph.context_has_view(graph.scene, ctx)

    slot = chunk_graph.ensure_default_view(graph.stage, graph.scene, ctx)

    assert slot == 1
    assert chunk_graph.context_has_view(graph.scene, ctx)
    assert chunk_graph.existing_view_slot_for(graph.stage, graph.path(b)) == 1


def test_a_context_that_has_a_view_gets_no_second_one(graph):
    graph.make("a")
    ctx = _context_chunk(graph)
    chunk_graph.ensure_default_view(graph.stage, graph.scene, ctx)

    assert chunk_graph.ensure_default_view(graph.stage, graph.scene, ctx) is None


def test_on_open_a_context_gets_an_output_on_its_tail_and_no_view(graph):
    a, b = graph.make("a"), graph.make("b")
    graph.wire_exec(a, b)
    ctx = _context_chunk(graph)

    assert chunk_graph.default_view_slot(graph.stage, graph.scene, graph.context, ctx) == (None, False)

    assert not chunk_graph.context_has_view(graph.scene, ctx)
    (output,) = [c for c in graph.scene.get_chunks() if chunk_stage.is_output_chunk(c)]
    exec_in = graph.stage.GetPrimAtPath(chunk_stage.get_chunk_prim_path(output)).GetAttribute(
        chunk_graph.EXEC_INPUT
    )
    assert [str(p.GetPrimPath()) for p in exec_in.GetConnections()] == [graph.path(b)]


def test_on_open_a_context_gets_one_output_only(graph):
    graph.make("a")
    ctx = _context_chunk(graph)
    chunk_graph.default_view_slot(graph.stage, graph.scene, graph.context, ctx)
    chunk_graph.default_view_slot(graph.stage, graph.scene, graph.context, ctx)
    assert len([c for c in graph.scene.get_chunks() if chunk_stage.is_output_chunk(c)]) == 1


def test_a_file_with_a_view_gets_no_output(graph):
    """Saved before Outputs: its active view still drives it, and the file is left alone."""
    graph.make("a")
    ctx = _context_chunk(graph)
    chunk_graph.ensure_default_view(graph.stage, graph.scene, ctx)
    chunk_graph.default_view_slot(graph.stage, graph.scene, graph.context, ctx)
    assert not chunk_graph.context_has_output(graph.scene, ctx)


def test_new_view_slot_on_a_node_takes_the_next_slot(graph):
    a, b = graph.make("a"), graph.make("b")
    graph.wire_exec(a, b)
    ctx = _context_chunk(graph)
    chunk_graph.new_view_slot(graph.stage, graph.scene, ctx)  # slot 1, on the tail b

    assert chunk_graph.new_view_slot(graph.stage, graph.scene, ctx, graph.path(a)) == 2
    assert chunk_graph.new_view_slot(graph.stage, graph.scene, ctx, graph.path(a)) == 2, \
        "a second view on the same node must reuse its slot"


def test_new_view_slot_without_a_scene_does_nothing(graph):
    assert chunk_graph.new_view_slot(graph.stage, None, None) is None


# -- renaming a node ---------------------------------------------------------------------------

def test_renaming_a_node_moves_its_prim_and_keeps_its_wires(graph):
    a, b = graph.make("a"), graph.make("b")
    graph.wire_exec(a, b)
    graph.wire_param(a, b)

    a.name = "source"

    assert graph.prim(a).GetName() == "source"
    assert graph.exec_sources(b) == [graph.path(a)]
    assert b.get_param("p").raw_value == "@source.p@"


def test_renaming_a_node_to_a_siblings_name_numbers_it(graph):
    """Reported by these tests: Qt's "Rename chunk" with a name in use raised from USD (the
    prim was moved onto its sibling's path) and left two chunks answering to one name."""
    a, b = graph.make("a"), graph.make("b")

    b.name = "a"

    assert b.name == "a1"
    assert graph.prim(b).GetName() == "a1"
    assert graph.scene.get_chunk("a") is a and graph.scene.get_chunk("a1") is b


def test_renaming_a_param_to_a_siblings_name_numbers_it(graph):
    a = graph.make("a")

    a.get_param("s").name = "p"

    assert a.get_param("p").default_value == 1.0, "the original p was shadowed"
    assert {"p", "p1"} <= {p.name for p in a.get_my_params()}


# -- set_param_visibly, output-port disconnect -------------------------------------------------

def test_set_param_visibly_writes_a_blocked_chunk_and_leaves_it_blocked(graph):
    cooks = []
    module = register_stub_module(main=_main, execute=lambda node: cooks.append(node.name))
    (c,) = chunk.create_chunks(module.module_name)
    c.set_blocked(True)
    cooks.clear()

    chunk_graph.set_param_visibly(c, c.get_param("p"), 8.0)

    assert c.get_param("p").value == pytest.approx(8.0)
    assert c.is_blocked()
    assert cooks == [], "writing the value must not run the module"


def test_set_param_visibly_on_an_ordinary_object_just_sets_it(graph):
    a = graph.make("a")

    chunk_graph.set_param_visibly(object(), a.get_param("p"), 3.0)

    assert a.get_param("p").value == pytest.approx(3.0)


def test_disconnecting_an_output_port_leaves_its_wires(graph):
    """A wire lives on its consumer's input; the output end owns nothing to clear.

    Frontends cut a wire from its input end. This pins what the output end does, so a
    frontend that starts offering it does not assume it works.
    """
    a, b = graph.make("a"), graph.make("b")
    graph.wire_param(a, b)
    chunks, prims, _ = graph.view_state()
    out = chunk_graph.PortRef(graph.path(a), f"{graph.path(a)}.outputs:p", chunk_graph.PORT_FLOAT, False)

    change = chunk_graph.disconnect(graph.stage, out, prims, chunks)

    assert not change.touched_nodes
    assert b.get_param("p").raw_value == "@a.p@"
