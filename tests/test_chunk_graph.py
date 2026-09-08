"""The graph engine, tested without a toolkit.

Deliberately no `qapp` fixture anywhere in this file. That is the point of the extraction:
if reading a network, typing its ports or judging a connection needed Qt, these tests could
not run at all. A frontend-agnostic engine whose test suite quietly builds a QApplication
is not one, and the absence is the evidence.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pxr import Sdf, Usd

from usd_chunks.plugins import chunk_graph

REPO = Path(__file__).resolve().parent.parent


NETWORK = """#usda 1.0

def Scope "chunks"
{
    def Chunk "Source"
    {
        uniform token info:chunkType = "reference"
        string inputs:primpath_location = "/World/thing"
        uniform token outputs:chunk
        uniform float2 ui:nodegraph:node:pos = (100, 50)
    }

    def Chunk "Middle"
    {
        uniform token info:chunkType = "transform"
        token inputs:chunk.connect = </chunks/Source.outputs:chunk>
        string inputs:primpath_location = "/World/thing"
        string inputs:primpath_location.connect = </chunks/Source.inputs:primpath_location>
        uniform token outputs:chunk
        uniform float2 ui:nodegraph:node:pos = (400, 50)
    }

    def Chunk "Off" (active = false)
    {
        uniform token info:chunkType = "transform"
        uniform token outputs:chunk
        uniform float2 ui:nodegraph:node:pos = (400, 300)
    }
}
"""


@pytest.fixture
def stage(tmp_path):
    path = tmp_path / "net.usda"
    path.write_text(NETWORK, encoding="utf-8")
    # Held by the fixture: a Usd.Prim does not keep its stage alive, so handing back only
    # prims lets the stage be collected and every prim expire.
    return Usd.Stage.Open(str(path))


class TestNoToolkit:
    def test_importing_the_engine_does_not_import_qt(self):
        """The whole premise, asserted rather than assumed.

        In a SUBPROCESS, because the assertion is about the engine's dependency chain and
        not about this process: run under the full suite, sibling modules have already
        imported PySide6, and checking sys.modules here would fail for a reason that has
        nothing to do with chunk_graph. A fresh interpreter that imports only the engine is
        the only place the question can honestly be asked.
        """
        source = (
            "import sys;"
            "import usd_chunks.plugins.chunk_graph;"
            "leaked=sorted(m for m in sys.modules if 'PySide' in m or 'shiboken' in m);"
            "print(','.join(leaked))"
        )
        result = subprocess.run([sys.executable, "-c", source],
                                capture_output=True, text=True, cwd=str(REPO))
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "", f"the engine pulled in Qt: {result.stdout}"


class TestReadingAGraph:
    def test_it_finds_every_chunk_including_disabled_ones(self, stage):
        """A disabled chunk is an INACTIVE prim and still belongs on the graph.

        Dropping it would take its wires with it, so the network would appear to rearrange
        itself when something is switched off.
        """
        names = sorted(p.GetName() for p in chunk_graph.resolve_graph_nodes(stage))
        assert names == ["Middle", "Off", "Source"]

    def test_a_disabled_chunk_is_reported_as_such(self, stage):
        prims = {p.GetName(): p for p in chunk_graph.resolve_graph_nodes(stage)}
        assert chunk_graph.is_disabled(prims["Off"])
        assert not chunk_graph.is_disabled(prims["Source"])

    def test_position_comes_from_usd(self, stage):
        """Both frontends read the same attribute, so both show the same arrangement."""
        prims = {p.GetName(): p for p in chunk_graph.resolve_graph_nodes(stage)}
        assert chunk_graph.node_position(prims["Middle"]) == (400.0, 50.0)

    def test_a_node_without_a_position_falls_back(self, stage):
        prim = stage.GetPrimAtPath("/chunks/Source")
        prim.GetAttribute(chunk_graph.POS_ATTR).Clear()
        assert chunk_graph.node_position(prim, default=(7.0, 9.0)) == (7.0, 9.0)


class TestPorts:
    def test_every_node_has_the_exec_pair(self, stage):
        _, ports, _ = chunk_graph.read_graph(stage)
        for path, node_ports in ports.items():
            kinds = {(p.name, p.is_input) for p in node_ports}
            assert ("chunk", True) in kinds, path
            assert ("chunk", False) in kinds, path

    def test_a_param_offers_both_directions(self, stage):
        """USD only carries the side that has a value or a connection.

        Source feeds primpath_location downstream, so on disk it has inputs: and no
        outputs: at all. Deriving ports from the authored attributes alone loses the port
        the wire starts at, and the wire then silently fails to draw.
        """
        prim = stage.GetPrimAtPath("/chunks/Source")
        assert not prim.GetAttribute("outputs:primpath_location")
        names = {(p.name, p.is_input) for p in chunk_graph.iter_ports(prim)}
        assert ("primpath_location", True) in names
        assert ("primpath_location", False) in names

    def test_a_string_param_types_as_string(self, stage):
        prim = stage.GetPrimAtPath("/chunks/Source")
        port = next(p for p in chunk_graph.iter_ports(prim)
                    if p.name == "primpath_location")
        assert port.port_type == chunk_graph.PORT_STRING


class TestEdges:
    def test_both_exec_and_param_wires_are_found(self, stage):
        _, _, edges = chunk_graph.read_graph(stage)
        assert sorted((e.is_exec, e.dst_attr_path.rsplit(":", 1)[-1]) for e in edges) == [
            (False, "primpath_location"), (True, "chunk")
        ]

    def test_a_wire_is_drawn_from_the_producers_output(self, stage):
        """The connection is owned by the consumer and names whichever attribute it was
        wired to -- including an inputs: one -- but it is drawn from an output dot."""
        _, _, edges = chunk_graph.read_graph(stage)
        param_edge = next(e for e in edges if not e.is_exec)
        assert param_edge.src_attr_path == "/chunks/Source.outputs:primpath_location"

    def test_param_wires_can_be_hidden(self, stage):
        prims = {str(p.GetPath()): p for p in chunk_graph.resolve_graph_nodes(stage)}
        edges = chunk_graph.build_edges(prims, show_param_edges=False)
        assert [e.is_exec for e in edges] == [True]

    def test_a_wire_to_a_node_outside_the_set_is_dropped(self, stage):
        """A connection can name a node in another context or one already deleted."""
        prims = {"/chunks/Middle": stage.GetPrimAtPath("/chunks/Middle")}
        assert chunk_graph.build_edges(prims) == []

    def test_adjacency_follows_the_exec_chain(self, stage):
        _, _, edges = chunk_graph.read_graph(stage)
        assert chunk_graph.build_adjacency(edges) == {
            "/chunks/Source": ["/chunks/Middle"]
        }


class TestConnectionRules:
    def _ports(self, stage):
        _, ports, _ = chunk_graph.read_graph(stage)
        return ports

    def test_an_output_may_meet_an_input(self, stage):
        ports = self._ports(stage)
        src = next(p for p in ports["/chunks/Source"] if p.name == "chunk" and not p.is_input)
        dst = next(p for p in ports["/chunks/Middle"] if p.name == "chunk" and p.is_input)
        assert chunk_graph.can_connect(src, dst)

    def test_two_inputs_may_not(self, stage):
        ports = self._ports(stage)
        a = next(p for p in ports["/chunks/Source"] if p.is_input)
        b = next(p for p in ports["/chunks/Middle"] if p.is_input)
        assert not chunk_graph.can_connect(a, b)

    def test_a_node_may_not_connect_to_itself(self, stage):
        ports = self._ports(stage)
        src = next(p for p in ports["/chunks/Source"] if not p.is_input)
        dst = next(p for p in ports["/chunks/Source"] if p.is_input)
        assert not chunk_graph.can_connect(src, dst)

    def test_exec_does_not_meet_data(self, stage):
        """An exec wire is control flow and a param wire is a value. Mixing them would
        author a connection the scheduler cannot read as either."""
        ports = self._ports(stage)
        exec_out = next(p for p in ports["/chunks/Source"]
                        if p.port_type == chunk_graph.PORT_EXEC and not p.is_input)
        data_in = next(p for p in ports["/chunks/Middle"]
                       if p.port_type != chunk_graph.PORT_EXEC and p.is_input)
        assert not chunk_graph.can_connect(exec_out, data_in)


class TestMutating:
    """Authoring, without a view anywhere.

    These are the ops a frontend calls when the user drags a wire. That they run here --
    no scene, no widgets, no QApplication -- is what makes a second frontend possible
    rather than merely drawable.
    """

    def _ports(self, stage):
        _, ports, _ = chunk_graph.read_graph(stage)
        return ports

    def test_connecting_exec_authors_the_connection(self, stage):
        ports = self._ports(stage)
        src = next(p for p in ports["/chunks/Middle"]
                   if p.name == "chunk" and not p.is_input)
        dst = next(p for p in ports["/chunks/Off"] if p.name == "chunk" and p.is_input)

        change = chunk_graph.connect(stage, src, dst)
        assert change is not None
        assert change.exec_changed
        assert change.touched_nodes == {"/chunks/Off"}

        attr = stage.GetAttributeAtPath(Sdf.Path("/chunks/Off.inputs:chunk"))
        assert [str(c) for c in attr.GetConnections()] == ["/chunks/Middle.outputs:chunk"]

    def test_the_order_of_the_two_ends_does_not_matter(self, stage):
        """A USD connection is owned by the consumer, so whichever end is the input
        becomes the destination regardless of which was grabbed first."""
        ports = self._ports(stage)
        src = next(p for p in ports["/chunks/Middle"]
                   if p.name == "chunk" and not p.is_input)
        dst = next(p for p in ports["/chunks/Off"] if p.name == "chunk" and p.is_input)

        chunk_graph.connect(stage, dst, src)  # deliberately reversed
        attr = stage.GetAttributeAtPath(Sdf.Path("/chunks/Off.inputs:chunk"))
        assert [str(c) for c in attr.GetConnections()] == ["/chunks/Middle.outputs:chunk"]

    def test_an_illegal_pair_authors_nothing(self, stage):
        ports = self._ports(stage)
        a = next(p for p in ports["/chunks/Source"] if p.is_input)
        b = next(p for p in ports["/chunks/Middle"] if p.is_input)
        assert chunk_graph.connect(stage, a, b) is None

    def test_a_param_wire_points_at_the_sources_input(self, stage):
        """The output dot is a drawing convention; the value lives on the source's
        inputs: attribute and that is what the connection names."""
        ports = self._ports(stage)
        src = next(p for p in ports["/chunks/Source"]
                   if p.name == "primpath_location" and not p.is_input)
        dst = next(p for p in ports["/chunks/Off"] if p.name == "chunk" and p.is_input)
        assert chunk_graph.connect(stage, src, dst) is None, "types differ, so no wire"

    def test_disconnect_heals_the_exec_chain(self, stage):
        """Source -> Middle -> Off, then detach Middle: Off must fall back to Source
        rather than being left wired to a node that is no longer in the chain."""
        ports = self._ports(stage)
        mid_out = next(p for p in ports["/chunks/Middle"]
                       if p.name == "chunk" and not p.is_input)
        off_in = next(p for p in ports["/chunks/Off"] if p.name == "chunk" and p.is_input)
        chunk_graph.connect(stage, mid_out, off_in)

        prims = {str(p.GetPath()): p for p in chunk_graph.resolve_graph_nodes(stage)}
        mid_in = next(p for p in ports["/chunks/Middle"]
                      if p.name == "chunk" and p.is_input)
        change = chunk_graph.disconnect(stage, mid_in, prims)

        assert "/chunks/Off" in change.touched_nodes
        attr = stage.GetAttributeAtPath(Sdf.Path("/chunks/Off.inputs:chunk"))
        assert [str(c) for c in attr.GetConnections()] == ["/chunks/Source.outputs:chunk"]

    def test_disconnecting_a_port_with_no_attribute_does_nothing(self, stage):
        """An unwired exec input has no attribute yet -- it is created on first connect.

        There is nothing to clear and nothing to heal, so the op reports no change rather
        than manufacturing an attribute in order to empty it.
        """
        prims = {str(p.GetPath()): p for p in chunk_graph.resolve_graph_nodes(stage)}
        assert not stage.GetAttributeAtPath(Sdf.Path("/chunks/Source.inputs:chunk"))
        port = next(p for p in self._ports(stage)["/chunks/Source"]
                    if p.name == "chunk" and p.is_input)
        change = chunk_graph.disconnect(stage, port, prims)
        assert change.touched_nodes == set()
        assert not change.edges_dirty

    def test_detaching_a_head_clears_its_successor(self, stage):
        """With no predecessor to heal to, the successor's slot is emptied -- not left
        pointing at the node that was just detached."""
        source = stage.GetPrimAtPath("/chunks/Source")
        source.CreateAttribute("inputs:chunk", Sdf.ValueTypeNames.Token, custom=False)
        prims = {str(p.GetPath()): p for p in chunk_graph.resolve_graph_nodes(stage)}
        port = next(p for p in self._ports(stage)["/chunks/Source"]
                    if p.name == "chunk" and p.is_input)

        change = chunk_graph.disconnect(stage, port, prims)

        assert "/chunks/Middle" in change.touched_nodes
        attr = stage.GetAttributeAtPath(Sdf.Path("/chunks/Middle.inputs:chunk"))
        assert [str(c) for c in attr.GetConnections()] == []

    def test_disconnect_works_without_a_live_chunk_session(self, stage):
        """The engine must not need a session to author.

        ChunkStageSync.clear_attr_connections returns silently when uninitialised, so
        routing every disconnect through it gave a frontend that reads a stage -- rather
        than joining a session -- an operation that reported success and changed nothing.
        These tests never initialise one, which is exactly the case that was broken.
        """
        prims = {str(p.GetPath()): p for p in chunk_graph.resolve_graph_nodes(stage)}
        port = next(p for p in self._ports(stage)["/chunks/Middle"]
                    if p.name == "chunk" and p.is_input)
        attr = stage.GetAttributeAtPath(Sdf.Path("/chunks/Middle.inputs:chunk"))
        assert attr.GetConnections(), "precondition: it starts wired"

        chunk_graph.disconnect(stage, port, prims)

        assert attr.GetConnections() == []
