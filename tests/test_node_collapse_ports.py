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
from PySide6 import QtWidgets

from usd_chunks.chunk_stage import get_chunk_prim_path, initialize as _chunk_usd_init
from usd_chunks import chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.frontends.qt.factory import ChunkWidgetDocker
from usd_chunks.frontends.qt.widgets import ContainerWidget
from usd_chunks.frontends.qt import nodegraph

from qt_wait import pump_until


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
    pump_until(QtWidgets.QApplication.instance(),
                lambda: a.get_port(f"{a_path}.outputs:tx") and b.get_port(f"{b_path}.inputs:tx"))
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


def test_a_node_collapsed_before_its_proxy_settles_keeps_its_body_hidden(graph) -> None:
    """Regression: a chunk collapsed the instant it's created (e.g. restored already-COLLAPSED
    from a saved scene) has its state set before ChunkNodeItem's own STATE_CHANGED listener
    exists to react to it -- there is no transition to trigger _set_collapsed's hide.

    create_proxy() itself is aware of this (checks has_state(COLLAPSED) directly rather than
    relying on the notice), but the node's own height settling shortly after fires
    ChunkGraphScene._on_heights_settled -> ChunkGraphView._apply_lod -> set_body_visible(True)
    for every node in view range, and that path used to force every proxy visible regardless
    of collapse state -- silently undoing create_proxy()'s hide a moment later and leaving the
    node showing its full param body outside its own (correctly small) header-height box."""
    qapp, reg, scene, docker = graph
    mod = reg.get_chunk_module("transform")
    chunk = _mk(mod, scene)
    # Collapse immediately, before processEvents() lets the node/proxy get built at all.
    chunk.set_state(ChunkFlags.State.COLLAPSED)
    qapp.processEvents()
    gv = docker.tab_widget.get_current_graph_view()
    node = gv.scene()._nodes[get_chunk_prim_path(chunk)]
    _pump(qapp, 0.3)  # long enough for the proxy to build and heights to settle

    assert node._height == nodegraph._NODE_HEADER_H
    assert node._proxy is not None and not node._proxy.isVisible()


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


def _expanded_container(qapp, node, group):
    """The group's ContainerWidget, expanded and settled.

    scatter_points ships its param groups collapsed (it is a tall chunk), so a test about
    *collapsing* one has to open it first -- otherwise the click below is a no-op and the
    params never get the ports the test is checking.
    """
    container = _container_for(node, group)
    assert container is not None
    if not container.minimize_button.isChecked():
        container.minimize_button.setChecked(True)
        _pump(qapp, 0.3)
    return container


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
    container = _expanded_container(qapp, node, var_group)

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
    var_group = chunk.get_param("Variation")
    assert var_group is not None
    container = _expanded_container(qapp, node, var_group)
    a_out = node_a.get_port(f"{a_path}.outputs:tx")
    scale_min_in = node.get_port(f"{node_path}.inputs:scale_min")
    scale_max_in = node.get_port(f"{node_path}.inputs:scale_max")
    assert a_out is not None and scale_min_in is not None and scale_max_in is not None
    gv._author_connection(a_out, scale_min_in)
    a_out2 = node_a.get_port(f"{a_path}.outputs:ty")
    assert a_out2 is not None
    gv._author_connection(a_out2, scale_max_in)
    gv.scene()._build_edges()

    height_before = node._height
    container.minimize_button.setChecked(False)
    _pump(qapp, 0.3)  # no deselect anywhere in this test
    pump_until(qapp, lambda: node._height < height_before)

    scale_min_in = node.get_port(f"{node_path}.inputs:scale_min")
    scale_max_in = node.get_port(f"{node_path}.inputs:scale_max")
    assert scale_min_in is not None and scale_max_in is not None, "connected params must keep a port"
    assert scale_min_in.pos().y() != scale_max_in.pos().y(), "must not stack on the shared group anchor"
    assert node._height < height_before, "the node must shrink once the group collapses"


def test_a_plain_hidden_param_gets_no_floating_port(graph) -> None:
    """Regression: reported live -- a fresh Layer node showed two extra ports well
    below the node's own body, disconnected from anything drawn.

    _place_param_ports's rewrite for the collapsed-group case (see the other tests in
    this file) accidentally dropped the widget.isVisible() check for every OTHER param --
    Layer's Index field (insert_index) is HIDDEN by default (only shown once
    Insert == "Index"), is not inside any group at all, so _nearest_collapsed_group finds
    nothing for it, and it fell straight into the "just use the widget's own position"
    branch. Qt stops laying out a hidden widget, so that position is stale/meaningless
    rather than just off-screen -- which is exactly what put a port floating below the
    node. A param hidden for a reason that isn't a collapsed group must fall back to the
    nearest visible ContainerWidget ancestor (skipping entirely if there isn't one, as a
    top-level hidden param like this has none), same as it did before that rewrite."""
    qapp, reg, scene, docker = graph
    mod = reg.get_chunk_module("layer")
    chunk = _mk(mod, scene)
    qapp.processEvents()
    gv = docker.tab_widget.get_current_graph_view()
    node = gv.scene()._nodes[get_chunk_prim_path(chunk)]
    _pump(qapp, 0.3)

    insert_mode = chunk.get_param("insert_mode")
    assert insert_mode is not None and insert_mode.value == "Append", "Index starts hidden by default"

    node_path = str(node.prim.GetPath())
    assert node.get_port(f"{node_path}.inputs:insert_index") is None
    assert node.get_port(f"{node_path}.outputs:insert_index") is None

    # Nothing left over from the hidden field should sit outside the node's own body.
    for port in node._typed_ports:
        y = port.pos().y()
        assert 0 <= y <= node._height, f"{port.attr_path} floats outside the node at y={y}"


def test_a_param_re_hiding_drops_its_port_without_needing_deselect(graph) -> None:
    """Regression: reported live -- switching Layer's Insert from Append to Index
    (showing the Index field) and back to Append (hiding it again) left the Index port
    on screen until something unrelated (deselecting the node) forced a fresh layout
    pass. Showing it worked fine; only re-hiding didn't update promptly.

    Nothing previously told ChunkNodeItem that a plain param's own HIDDEN state had
    changed -- PARAM_ADDED/PARAM_REMOVED cover new/removed params, and a group's own
    toggle_anim.finished covers a *group* collapsing, but a param hidden directly via
    set_state(HIDDEN) (no group, no animation involved) had nothing wired to it at all,
    leaving it dependent on geometryChanged happening to fire from the resulting Qt
    layout change -- which, like the group-collapse case, does not reliably happen for a
    widget disappearing specifically."""
    qapp, reg, scene, docker = graph
    mod = reg.get_chunk_module("layer")
    chunk = _mk(mod, scene)
    qapp.processEvents()
    gv = docker.tab_widget.get_current_graph_view()
    node = gv.scene()._nodes[get_chunk_prim_path(chunk)]
    _pump(qapp, 0.3)

    node_path = str(node.prim.GetPath())
    insert_mode = chunk.get_param("insert_mode")
    assert insert_mode is not None

    insert_mode.value = "Index"
    pump_until(qapp, lambda: node.get_port(f"{node_path}.inputs:insert_index") is not None)
    assert node.get_port(f"{node_path}.inputs:insert_index") is not None, "Index must appear once shown"

    insert_mode.value = "Append"
    _pump(qapp, 0.3)  # no deselect anywhere in this test

    assert node.get_port(f"{node_path}.inputs:insert_index") is None, "Index must disappear once re-hidden"
    assert node.get_port(f"{node_path}.outputs:insert_index") is None
