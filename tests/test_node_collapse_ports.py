"""Regression coverage for collapsing a node (or the whole chunk) leaving its typed
(inputs:/outputs:) ports in a sane, non-overlapping state.

Before this fix, ChunkNodeItem._place_param_ports_collapsed placed EVERY connectable
param's ports at the exact same point -- header centre, which is also where the exec
in/out ports already live -- so a collapsed node with any params at all stacked its exec
ports, every param's input port, and every param's output port three-deep on two single
pixels. Whichever port item happened to be topmost in z-order ate all hover/click, so a
real wire could look like it had landed on an entirely different port than it actually
had, and the node never grew past header height to make room for any of it either.
"""

from __future__ import annotations

import time

import pytest
from pxr import Usd

from usd_chunks.plugins.chunk_usd import get_chunk_prim_path, initialize as _chunk_usd_init
from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.plugins.chunk_qt import ChunkWidgetDocker
from usd_chunks.plugins.chunk_qt_widgets import ContainerWidget
from usd_chunks.plugins import chunk_qt_nodegraph as nodegraph


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _mk(mod, scene):
    cs = mod.create() if mod.loaded else mod.load()
    for c in cs:
        scene.add_chunk(c)
    return cs[0]


def _pump(qapp, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()


@pytest.fixture
def graph(qapp):
    """Fresh stage per test -- this file is small enough that per-test isolation (see
    tests/test_dragdrop_import.py's fixture docstring for the leak this trades off
    against) is worth it over sharing state across collapse/expand transitions."""
    stage = Usd.Stage.CreateInMemory()

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    lm = chunk_registry.ChunkLayerManager()
    lm.initialize(stage)
    _chunk_usd_init(stage, scene)

    docker = ChunkWidgetDocker()
    docker.resize(500, 600)
    docker.show()
    qapp.processEvents()

    return qapp, reg, scene, docker


def _wire_tx(gv, a, b):
    a_path = str(a.prim.GetPath())
    b_path = str(b.prim.GetPath())
    a_out = a.get_port(f"{a_path}.outputs:tx")
    b_in = b.get_port(f"{b_path}.inputs:tx")
    assert a_out is not None and b_in is not None, "typed ports must exist by now"
    gv._author_connection(a_out, b_in)
    gv.scene()._build_edges()


def test_collapsing_an_unwired_node_leaves_no_typed_ports(graph) -> None:
    qapp, reg, scene, docker = graph
    mod = reg.get_chunk_module("transform")
    chunk = _mk(mod, scene)
    qapp.processEvents()
    gv = docker.tab_widget.get_current_graph_view()
    node = gv.scene()._nodes[get_chunk_prim_path(chunk)]
    _pump(qapp, 0.3)

    chunk.set_state(ChunkFlags.State.COLLAPSED)
    _pump(qapp, 0.2)

    assert node._height == nodegraph._NODE_HEADER_H
    assert node._typed_ports == []


def test_collapsing_a_wired_node_keeps_the_connected_ports_off_the_exec_ports(graph) -> None:
    qapp, reg, scene, docker = graph
    mod = reg.get_chunk_module("transform")
    a, b = _mk(mod, scene), _mk(mod, scene)
    qapp.processEvents()
    gv = docker.tab_widget.get_current_graph_view()
    node_a = gv.scene()._nodes[get_chunk_prim_path(a)]
    node_b = gv.scene()._nodes[get_chunk_prim_path(b)]
    _pump(qapp, 0.3)

    _wire_tx(gv, node_a, node_b)

    node_b.chunk.set_state(ChunkFlags.State.COLLAPSED)
    _pump(qapp, 0.2)

    # The node must have grown past a bare header to make room for the connected port.
    assert node_b._height > nodegraph._NODE_HEADER_H

    tx_in = node_b.get_port(f"{node_b.prim.GetPath()}.inputs:tx")
    assert tx_in is not None, "the connected param's port must still exist while collapsed"
    assert tx_in.pos().y() != node_b._exec_in_port.pos().y(), (
        "a connected data port must not land on the exec port's own position"
    )


def test_collapsing_a_node_with_two_connected_inputs_does_not_stack_their_ports(graph) -> None:
    qapp, reg, scene, docker = graph
    mod = reg.get_chunk_module("transform")
    a, b = _mk(mod, scene), _mk(mod, scene)
    qapp.processEvents()
    gv = docker.tab_widget.get_current_graph_view()
    node_a = gv.scene()._nodes[get_chunk_prim_path(a)]
    node_b = gv.scene()._nodes[get_chunk_prim_path(b)]
    _pump(qapp, 0.3)

    a_path = str(node_a.prim.GetPath())
    b_path = str(node_b.prim.GetPath())

    _wire_tx(gv, node_a, node_b)
    a_ty_out = node_a.get_port(f"{a_path}.outputs:ty")
    b_ty_in = node_b.get_port(f"{b_path}.inputs:ty")
    assert a_ty_out is not None and b_ty_in is not None
    gv._author_connection(a_ty_out, b_ty_in)
    gv.scene()._build_edges()

    node_b.chunk.set_state(ChunkFlags.State.COLLAPSED)
    _pump(qapp, 0.2)

    tx_in = node_b.get_port(f"{b_path}.inputs:tx")
    ty_in = node_b.get_port(f"{b_path}.inputs:ty")
    assert tx_in is not None and ty_in is not None
    # tx, ty and the exec-in port all share the same side (x=0) -- collapsing must not
    # collapse them onto the same point too.
    positions = {p.pos().y() for p in (tx_in, ty_in, node_b._exec_in_port)}
    assert len(positions) == 3


def _container_for(node, group):
    """The real ContainerWidget backing an inner Group param, found the way the actual
    minimize button lives -- not the chunk-level state API, which (per live testing) does
    not reliably drive the same collapse path a real click does."""
    for w in node._proxy.widget().findChildren(ContainerWidget):
        if w.group is group:
            return w
    return None


def test_collapsing_an_inner_group_hides_its_unconnected_params_ports(graph) -> None:
    """The design half of the report: a param whose group just collapsed has nothing to
    point a port at and no wire that needs one, so it should not draw one at all --
    matching how a fully collapsed node already treats its own unwired params."""
    qapp, reg, scene, docker = graph
    mod = reg.get_chunk_module("scatter_points")
    chunk = _mk(mod, scene)
    qapp.processEvents()
    gv = docker.tab_widget.get_current_graph_view()
    node = gv.scene()._nodes[get_chunk_prim_path(chunk)]
    _pump(qapp, 0.3)

    var_group = chunk.get_param("Variation")
    assert var_group is not None, "scatter_points must still have a Variation group with 2+ params"
    container = _container_for(node, var_group)
    assert container is not None

    container.minimize_button.setChecked(False)  # the real click path: toggled -> set_expanded
    _pump(qapp, 0.3)

    node_path = str(node.prim.GetPath())
    assert node.get_port(f"{node_path}.inputs:scale_min") is None
    assert node.get_port(f"{node_path}.inputs:scale_max") is None


def test_collapsing_an_inner_group_updates_immediately_and_spaces_wired_ports(graph) -> None:
    """The timing half of the report: the rebuild must not depend on some unrelated later
    event (e.g. deselecting the node, which _set_collapsed's own branch happens to also
    kick a rebuild for) -- collapsing the group itself must be enough on its own. And two
    params sharing that group's collapsed anchor must not land on each other's port."""
    qapp, reg, scene, docker = graph
    mod = reg.get_chunk_module("scatter_points")
    a = _mk(reg.get_chunk_module("transform"), scene)
    chunk = _mk(mod, scene)
    qapp.processEvents()
    gv = docker.tab_widget.get_current_graph_view()
    node_a = gv.scene()._nodes[get_chunk_prim_path(a)]
    node = gv.scene()._nodes[get_chunk_prim_path(chunk)]
    _pump(qapp, 0.3)

    node_path = str(node.prim.GetPath())
    a_path = str(node_a.prim.GetPath())
    a_out = node_a.get_port(f"{a_path}.outputs:tx")
    scale_min_in = node.get_port(f"{node_path}.inputs:scale_min")
    scale_max_in = node.get_port(f"{node_path}.inputs:scale_max")
    assert a_out is not None and scale_min_in is not None and scale_max_in is not None
    gv._author_connection(a_out, scale_min_in)
    a_out2 = node_a.get_port(f"{a_path}.outputs:ty")
    assert a_out2 is not None
    gv._author_connection(a_out2, scale_max_in)
    gv.scene()._build_edges()

    var_group = chunk.get_param("Variation")
    assert var_group is not None
    container = _container_for(node, var_group)
    assert container is not None

    height_before = node._height
    container.minimize_button.setChecked(False)
    _pump(qapp, 0.3)  # no deselect anywhere in this test

    scale_min_in = node.get_port(f"{node_path}.inputs:scale_min")
    scale_max_in = node.get_port(f"{node_path}.inputs:scale_max")
    assert scale_min_in is not None and scale_max_in is not None, "connected params must keep a port"
    assert scale_min_in.pos().y() != scale_max_in.pos().y(), "must not stack on the shared group anchor"
    assert node._height < height_before, "the node must shrink once the group collapses"
