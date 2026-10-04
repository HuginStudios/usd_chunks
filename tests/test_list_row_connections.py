"""List-group rows wired in the node graph, as the source of a wire and as its destination.

Reported: wiring the example chunk's listSlider row OUT to the Mesh chunk's radius left
radius at 0.0. The wire wrote @HelloWorld.listSlider@, which names the template every row
shares -- a param no widget shows -- so the destination read the template's default and
never heard the row move. A row is named by its index: @HelloWorld.listSlider[0]@.

test_proxy_param_expression.py covers a row as the destination; this covers the rest.
"""
from __future__ import annotations

import pytest
from pxr import Usd

from usd_chunks import chunk, chunk_registry, chunk_stage, chunk_utils
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.graph import engine as chunk_graph

from chunk_stubs import attach_stub_module


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


def _list_chunk(name, rows=2):
    c = chunk.Chunk(name)
    attach_stub_module(c)
    group = chunk.ListGroup("rows")
    group.add_param(chunk.Slider("item", default_value=0.0))
    c.add_param(group)
    return c, group


class _Graph:
    """A stage with a list chunk `src` (two rows) and a plain chunk `dst` holding p."""

    def __init__(self):
        self.stage = Usd.Stage.CreateInMemory()
        self.scene = _Scene(self.stage)
        chunk_registry.ChunkRegistry().initialize(self.scene, "pytest")
        chunk_registry.ChunkLayerManager().initialize(self.stage)
        chunk_stage.ChunkStageSync().initialize(self.stage, self.scene)

        self.src, self.rows = _list_chunk("src")
        self.dst = chunk.Chunk("dst")
        attach_stub_module(self.dst)
        self.dst.add_param(chunk.Slider("p", default_value=0.0))
        self.chunks = {}
        for c in (self.src, self.dst):
            self.add(c)
        self.rows.size = 2
        self.row(0).value = 10.0
        self.row(1).value = 20.0

    def add(self, c):
        self.scene.add_chunk(c)
        self.chunks[chunk_stage.get_chunk_prim_path(c)] = c

    def path(self, c):
        return chunk_stage.get_chunk_prim_path(c)

    def row(self, i, group=None):
        return (group or self.rows).get_indexed_param("item", i)

    def out_row(self, i, c=None, group=None):
        c = c or self.src
        path = self.path(c)
        return chunk_graph.PortRef(path, f"{path}.outputs:item", chunk_graph.PORT_FLOAT, False,
                                   param=self.row(i, group))

    def in_row(self, i, c, group):
        path = self.path(c)
        return chunk_graph.PortRef(path, f"{path}.inputs:item", chunk_graph.PORT_FLOAT, True,
                                   param=self.row(i, group))

    def in_param(self, c, name):
        path = self.path(c)
        return chunk_graph.PortRef(path, f"{path}.inputs:{name}", chunk_graph.PORT_FLOAT, True,
                                   param=c.get_param(name))

    def connect(self, a, b):
        return chunk_graph.connect(self.stage, a, b, self.chunks)


@pytest.fixture()
def graph():
    return _Graph()


# -- a row as the source ---------------------------------------------------------------

def test_a_row_wired_out_drives_the_destination(graph):
    p = graph.dst.get_param("p")
    graph.connect(graph.out_row(1), graph.in_param(graph.dst, "p"))

    assert p.raw_value == "@src.item[1]@"
    assert p.value == 20.0, "the destination read the template, not the row"


def test_the_destination_follows_the_row_when_it_moves(graph):
    p = graph.dst.get_param("p")
    graph.connect(graph.out_row(0), graph.in_param(graph.dst, "p"))
    seen = []
    p.add_callback(lambda notice, sender: seen.append(p.value), sender=p.get_attribute_sender("value"))

    graph.row(0).value = 4.0
    assert p.value == 4.0
    assert seen and seen[-1] == 4.0, "the destination was never told the row changed"

    graph.row(1).value = 99.0
    assert p.value == 4.0, "another row's change leaked through"
    graph.rows.get_param("item").value = 55.0
    assert p.value == 4.0, "the template's change leaked through"


def test_a_row_source_is_saved_and_reloaded(graph):
    graph.connect(graph.out_row(1), graph.in_param(graph.dst, "p"))
    prim = graph.stage.GetPrimAtPath(graph.path(graph.dst))
    # The connection names the template's attribute; only the text knows which row.
    assert chunk_utils.read_expression_text(prim.GetAttribute("inputs:p")) == "@src.item[1]@"

    reloaded = chunk.Chunk("dst")
    reloaded.add_param(chunk.Slider("p", default_value=0.0))
    chunk_stage._load_wire_inputs(prim, reloaded)
    assert reloaded.get_param("p").raw_value == "@src.item[1]@"


def test_the_usd_connection_names_a_real_path_on_the_source(graph):
    graph.connect(graph.out_row(1), graph.in_param(graph.dst, "p"))
    attr = graph.stage.GetPrimAtPath(graph.path(graph.dst)).GetAttribute("inputs:p")
    conns = attr.GetConnections()
    assert [str(c) for c in conns] == [f"{graph.path(graph.src)}.inputs:item"]


def test_disconnecting_a_row_source_restores_the_default(graph):
    p = graph.dst.get_param("p")
    in_port = graph.in_param(graph.dst, "p")
    graph.connect(graph.out_row(1), in_port)
    chunk_graph.disconnect(graph.stage, in_port, {}, graph.chunks)
    assert p.raw_value == 0.0
    graph.row(1).value = 3.0
    assert p.value == 0.0


def test_rewiring_to_another_row_follows_the_new_row_only(graph):
    p = graph.dst.get_param("p")
    in_port = graph.in_param(graph.dst, "p")
    graph.connect(graph.out_row(0), in_port)
    graph.connect(graph.out_row(1), in_port)
    assert p.value == 20.0
    graph.row(0).value = -1.0
    assert p.value == 20.0, "the old row still drives it"
    graph.row(1).value = 2.5
    assert p.value == 2.5


def test_a_row_ref_past_the_end_reads_the_default(graph):
    p = graph.dst.get_param("p")
    p.value = "@src.item[5]@"
    assert p.value == 0.0


def test_a_row_in_arithmetic(graph):
    p = graph.dst.get_param("p")
    p.value = "@src.item[0]@ + @src.item[1]@ * 2"
    assert p.value == 50.0
    graph.row(0).value = 1.0
    assert p.value == 41.0


def test_renaming_the_source_chunk_keeps_the_row_ref(graph):
    p = graph.dst.get_param("p")
    graph.connect(graph.out_row(1), graph.in_param(graph.dst, "p"))
    graph.src.name = "renamed"
    assert p.raw_value == "@renamed.item[1]@"
    assert p.value == 20.0


def test_renaming_the_template_keeps_the_row_ref(graph):
    p = graph.dst.get_param("p")
    graph.connect(graph.out_row(1), graph.in_param(graph.dst, "p"))
    graph.rows.get_param("item").name = "thing"
    assert p.raw_value == "@src.thing[1]@"
    assert p.value == 20.0


# -- deleting rows -----------------------------------------------------------------------

def _wired_to(graph, i):
    in_port = graph.in_param(graph.dst, "p")
    graph.connect(graph.out_row(i), in_port)
    return graph.dst.get_param("p"), graph.stage.GetPrimAtPath(graph.path(graph.dst)).GetAttribute("inputs:p")


def test_deleting_an_earlier_row_renumbers_the_ref(graph):
    p, _ = _wired_to(graph, 1)
    graph.rows.remove_index(0)
    assert p.raw_value == "@src.item[0]@"
    assert p.value == 20.0
    graph.row(0).value = 1.5
    assert p.value == 1.5, "the destination stopped following the row"


def test_deleting_a_later_row_leaves_the_ref(graph):
    graph.rows.size = 3
    p, _ = _wired_to(graph, 1)
    graph.rows.remove_index(2)
    assert p.raw_value == "@src.item[1]@"
    assert p.value == 20.0


def test_deleting_the_wired_row_drops_the_wire(graph):
    p, attr = _wired_to(graph, 1)
    graph.rows.remove_index(1)
    assert p.raw_value == 0.0, "the ref slid onto whatever row took its place"
    assert not attr.GetConnections(), "the USD connection outlived the wire"
    assert not chunk_utils.read_expression_text(attr)


def test_shrinking_the_list_drops_wires_to_the_rows_cut_off(graph):
    graph.rows.size = 3
    graph.row(2).value = 30.0
    p, attr = _wired_to(graph, 2)
    graph.rows.size = 1
    assert p.raw_value == 0.0
    assert not attr.GetConnections()


def test_shrinking_the_list_keeps_wires_to_the_rows_kept(graph):
    graph.rows.size = 3
    p, _ = _wired_to(graph, 0)
    graph.rows.size = 1
    assert p.raw_value == "@src.item[0]@"
    assert p.value == 10.0


def test_deleting_a_row_renumbers_a_ref_held_by_another_lists_row(graph):
    other, other_rows = _list_chunk("other")
    graph.add(other)
    other_rows.size = 1
    graph.connect(graph.out_row(1), graph.in_row(0, other, other_rows))
    graph.rows.remove_index(0)
    assert graph.row(0, other_rows).raw_value == "@src.item[0]@"
    assert graph.row(0, other_rows).value == 20.0


def test_deleting_a_row_leaves_refs_to_another_chunks_rows(graph):
    other, other_rows = _list_chunk("other")
    graph.add(other)
    other_rows.size = 2
    other_rows.get_indexed_param("item", 1).value = 5.0
    p = graph.dst.get_param("p")
    p.value = "@other.item[1]@"
    graph.rows.remove_index(0)
    assert p.raw_value == "@other.item[1]@"
    assert p.value == 5.0


def test_deleting_a_row_from_the_destination_list_keeps_its_other_wires(graph):
    other, other_rows = _list_chunk("other")
    graph.add(other)
    other_rows.size = 2
    graph.connect(graph.out_row(1), graph.in_row(1, other, other_rows))
    other_rows.remove_index(0)
    assert graph.row(0, other_rows).raw_value == "@src.item[1]@"
    assert graph.row(0, other_rows).value == 20.0


# -- row to row --------------------------------------------------------------------------

def test_a_row_wired_into_another_lists_row(graph):
    other, other_rows = _list_chunk("other")
    graph.add(other)
    other_rows.size = 3
    graph.connect(graph.out_row(1), graph.in_row(2, other, other_rows))

    assert graph.row(2, other_rows).value == 20.0
    graph.row(1).value = 8.0
    assert graph.row(2, other_rows).value == 8.0
    assert graph.row(0, other_rows).value == 0.0
    assert graph.row(1, other_rows).value == 0.0


def test_a_row_can_read_a_sibling_row_by_expression(graph):
    graph.rows.size = 3
    path = graph.path(graph.src)
    in_port = chunk_graph.PortRef(path, f"{path}.inputs:item", chunk_graph.PORT_FLOAT, True,
                                  param=graph.row(2))
    # The graph refuses a wire within one node; the expression it would write still works.
    assert chunk_graph.connect(graph.stage, graph.out_row(0), in_port, graph.chunks) is None
    graph.row(2).value = "@src.item[0]@"
    assert graph.row(2).value == 10.0
    graph.row(0).value = 6.0
    assert graph.row(2).value == 6.0
    assert graph.row(1).value == 20.0


# -- line edits: a row read inside a longer path ------------------------------------------

@pytest.fixture()
def paths(graph):
    """src gains a list of prim-path rows; dst gains a prim-path field."""
    locs = chunk.ListGroup("locs")
    locs.add_param(chunk.LineEdit("loc", default_value="", flags=chunk.ChunkFlags.ParamFlags.PRIM_SELECTION))
    graph.src.add_param(locs)
    locs.size = 3
    for i, value in enumerate(("/a", "/b", "/c")):
        locs.get_indexed_param("loc", i).value = value
    target = chunk.LineEdit("target", default_value="", flags=chunk.ChunkFlags.ParamFlags.PRIM_SELECTION)
    graph.dst.add_param(target)
    return graph, locs, target


def test_a_row_wired_into_a_line_edit(paths):
    graph, locs, target = paths
    path = graph.path(graph.src)
    out_port = chunk_graph.PortRef(path, f"{path}.outputs:loc", chunk_graph.PORT_STRING, False,
                                   param=locs.get_indexed_param("loc", 1))
    dst_path = graph.path(graph.dst)
    in_port = chunk_graph.PortRef(dst_path, f"{dst_path}.inputs:target", chunk_graph.PORT_STRING, True,
                                  param=target)
    change = graph.connect(out_port, in_port)
    assert not change.expression_error
    assert target.raw_value == "@src.loc[1]@"
    assert target.value == "/b"


def test_a_row_read_inside_a_longer_path(paths):
    graph, locs, target = paths
    target.value = "@src.loc[1]@/continued/prim/path"
    assert target.value == "/b/continued/prim/path"
    locs.get_indexed_param("loc", 1).value = "/moved"
    assert target.value == "/moved/continued/prim/path"
    locs.get_indexed_param("loc", 0).value = "/other"
    assert target.value == "/moved/continued/prim/path"


def test_two_rows_in_one_path(paths):
    graph, locs, target = paths
    target.value = "@src.loc[0]@/geo@src.loc[2]@"
    assert target.value == "/a/geo/c"


def test_a_path_through_a_row_is_saved_and_reloaded(paths):
    graph, locs, target = paths
    target.value = "@src.loc[1]@/geo"
    prim = graph.stage.GetPrimAtPath(graph.path(graph.dst))
    assert chunk_utils.read_expression_text(prim.GetAttribute("inputs:target")) == "@src.loc[1]@/geo"

    reloaded = chunk.Chunk("dst")
    reloaded.add_param(chunk.LineEdit("target", default_value="",
                                      flags=chunk.ChunkFlags.ParamFlags.PRIM_SELECTION))
    chunk_stage._load_wire_inputs(prim, reloaded)
    assert reloaded.get_param("target").raw_value == "@src.loc[1]@/geo"


def test_deleting_an_earlier_row_renumbers_a_path_through_a_later_one(paths):
    graph, locs, target = paths
    target.value = "@src.loc[2]@/geo"
    locs.remove_index(0)
    assert target.raw_value == "@src.loc[1]@/geo"
    assert target.value == "/c/geo"


def test_deleting_the_row_a_path_reads_drops_the_wire(paths):
    graph, locs, target = paths
    target.value = "@src.loc[1]@/geo"
    locs.remove_index(1)
    assert target.raw_value == "", "the path went on reading whichever row slid into place"
    attr = graph.stage.GetPrimAtPath(graph.path(graph.dst)).GetAttribute("inputs:target")
    assert not chunk_utils.read_expression_text(attr)
    assert not attr.GetConnections()


def test_renaming_the_source_chunk_keeps_a_path_through_a_row(paths):
    graph, locs, target = paths
    target.value = "@src.loc[1]@/geo"
    graph.src.name = "renamed"
    assert target.raw_value == "@renamed.loc[1]@/geo"
    assert target.value == "/b/geo"


def test_deleting_a_row_of_one_list_leaves_refs_to_anothers(paths):
    """Two lists on one chunk: removing a row of rows must not renumber refs into locs."""
    graph, locs, target = paths
    target.value = "@src.loc[1]@/geo"
    graph.rows.remove_index(0)
    assert target.raw_value == "@src.loc[1]@/geo"


# -- grammar -----------------------------------------------------------------------------

@pytest.mark.parametrize("text, refs", [
    ("@a.b@", [("a", "b")]),
    ("@a.b[0]@", [("a", "b[0]")]),
    ("@a.b[12]@ * @c.d@", [("a", "b[12]"), ("c", "d")]),
    ("@a.b[x]@", []),
    ("@a.b[]@", []),
    ("@a.b[0]@/continued/prim/path", [("a", "b[0]")]),
    ("/root/@a.b[1]@/x", [("a", "b[1]")]),
])
def test_parse_expression_reads_row_indices(text, refs):
    assert chunk_utils.parse_expression(text) == refs
    assert chunk_utils.has_expression(text) == bool(refs)


@pytest.mark.parametrize("name, split", [
    ("b", ("b", None)),
    ("b[0]", ("b", 0)),
    ("b[12]", ("b", 12)),
])
def test_split_row_ref(name, split):
    assert chunk_utils.split_row_ref(name) == split


@pytest.mark.parametrize("text, removed, expected", [
    ("@src.item[3]@", [1], ("@src.item[2]@", False)),
    ("@src.item[0]@", [1], ("@src.item[0]@", False)),
    ("@src.item[1]@", [1], ("@src.item[1]@", True)),
    ("@src.item[4]@ + @src.item[0]@", [1, 2], ("@src.item[2]@ + @src.item[0]@", False)),
    ("@src.other[3]@", [0], ("@src.other[3]@", False)),
    ("@dst.item[3]@", [0], ("@dst.item[3]@", False)),
    ("@src.item@", [0], ("@src.item@", False)),
])
def test_renumber_row_refs(text, removed, expected):
    assert chunk_utils.renumber_row_refs(text, "src", ["item"], removed) == expected


# -- the Qt view draws a row's wire from that row's port ---------------------------------

class _FakePort:
    def __init__(self, attr_path, param=None):
        self.attr_path, self.param = attr_path, param


class _FakePrim:
    def __init__(self, name):
        self._name = name

    def GetName(self):
        return self._name


class _FakeNode:
    """What ChunkGraphScene._source_ports reads off a ChunkNodeItem."""

    def __init__(self, name, ports):
        self.prim, self._ports = _FakePrim(name), ports

    def get_port(self, attr_path):
        return next((p for p in self._ports if p.attr_path == attr_path), None)


def test_the_qt_view_draws_a_row_wire_from_that_rows_port(graph):
    nodegraph = pytest.importorskip("usd_chunks.frontends.qt.nodegraph")
    graph.rows.size = 3
    out = f"{graph.path(graph.src)}.outputs:item"
    ports = [_FakePort(out, graph.row(i)) for i in range(3)]
    node = _FakeNode("src", ports)
    pick = nodegraph.ChunkGraphScene._source_ports

    assert pick(node, out, "@src.item[2]@") == [ports[2]]
    assert pick(node, out, "@src.item[0]@ + @src.item[2]@") == [ports[0], ports[2]]
    assert pick(node, out, "@src.item[2]@/geo") == [ports[2]]
    # Without a row index it is the port at the path, as for any other param.
    assert pick(node, out, "@src.item@") == [ports[0]]
    assert pick(node, out, None) == [ports[0]]
    # A row of another chunk's list of the same name is not this node's.
    assert pick(node, out, "@other.item[2]@") == [ports[0]]
