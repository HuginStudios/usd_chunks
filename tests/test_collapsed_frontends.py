"""A collapsed node's "+" in the Flet and ImGui frontends, as Qt has it.

Both draw a collapsed node as its exec ports, the wired end of each wired param, and a
"+" on each side with something left to wire (chunk_graph.collapsed_node_ports). A wire
to or from a "+" waits in pending_pick for a param to be picked (chunk_graph.plus_step);
choose() joins it, and asks again when both ends were a "+".

Driven headless: the pick is the view's state, and the dialog or popup only shows it.
"""
from __future__ import annotations

import pytest
from pxr import Sdf

from graph_fixtures import content_nodes, make_network
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.graph import engine as chunk_graph


@pytest.fixture(params=["flet", "imgui"])
def view(request, tmp_path):
    path, context = make_network(tmp_path / "net.usda")
    if request.param == "flet":
        pytest.importorskip("flet", reason="flet is an optional extra")
        from usd_chunks.frontends.flet.nodegraph import GraphView
        v = GraphView(path, context)
        v.live = False  # no page; refresh() stays a no-op
    else:
        pytest.importorskip("imgui_bundle", reason="imgui-bundle is an optional extra")
        from usd_chunks.frontends.imgui.nodegraph import ImGuiGraphView
        v = ImGuiGraphView(path, context)
    assert len(content_nodes(v)) == 2
    return v


def _port(view, path, suffix):
    return next(p for p in view.ports[path] if p.attr_path.endswith(suffix))


def _connections(view, attr_path):
    attr = view.stage.GetAttributeAtPath(Sdf.Path(attr_path))
    return [str(c) for c in attr.GetConnections()] if attr else []


def test_a_wire_to_an_input_plus_waits_for_the_param_then_wires_it(view):
    first, second = content_nodes(view)
    out = _port(view, first, "outputs:tx")

    view.join(out, chunk_graph.PlusRef(second, True))
    choices, _other = view.pending_pick
    assert f"{second}.inputs:tx" in [r.attr_path for r in choices]
    assert all(r.is_input and r.port_type == out.port_type for r in choices)

    view.choose(next(r for r in choices if r.attr_path == f"{second}.inputs:tx"))
    assert view.pending_pick is None
    assert _connections(view, f"{second}.inputs:tx") == [f"{first}.inputs:tx"]


def test_plus_to_plus_asks_the_output_side_then_the_input_side(view):
    first, second = content_nodes(view)

    view.join(chunk_graph.PlusRef(second, True), chunk_graph.PlusRef(first, False))
    outs, _other = view.pending_pick
    assert outs and all(not r.is_input and r.node_path == first for r in outs)
    view.choose(next(r for r in outs if r.attr_path == f"{first}.outputs:tx"))

    ins, _other = view.pending_pick
    assert all(r.is_input and r.node_path == second for r in ins)
    view.choose(next(r for r in ins if r.attr_path == f"{second}.inputs:ty"))
    assert view.pending_pick is None
    assert _connections(view, f"{second}.inputs:ty") == [f"{first}.inputs:tx"]


def test_a_plus_and_a_port_of_its_own_direction_offer_nothing(view):
    first, second = content_nodes(view)

    view.join(_port(view, first, "inputs:tx"), chunk_graph.PlusRef(second, True))

    assert view.pending_pick is None and view.status


def test_flet_draws_a_collapsed_node_as_its_wired_ends_and_a_plus_each_side(view):
    if not hasattr(view, "shown_ports"):
        pytest.skip("Flet only: ImGui works out what to show while it draws")
    first, second = content_nodes(view)
    view.chunks[second].set_state(ChunkFlags.State.COLLAPSED)
    view.reload()

    assert all(p.port_type == chunk_graph.PORT_EXEC for p in view.shown_ports(second))
    assert view.pluses(second) == [chunk_graph.PlusRef(second, True), chunk_graph.PlusRef(second, False)]
    plus = view.plus_xy(chunk_graph.PlusRef(second, True))
    assert view.end_at(*plus) == chunk_graph.PlusRef(second, True)
    assert view.node_height(second) < view.node_height(first), "no body while collapsed"
