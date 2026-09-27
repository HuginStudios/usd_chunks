from __future__ import annotations

import time

import pytest
from pxr import Sdf, Usd
from PySide6 import QtCore, QtGui, QtWidgets

from usd_chunks.chunk_stage import get_chunk_prim_path, initialize as _chunk_usd_init
from usd_chunks import chunk_registry, chunk_widget_api
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.frontends.qt.factory import ChunkWidgetDocker
from usd_chunks.frontends.qt import nodegraph


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


@pytest.fixture()
def graph(qapp):
    """A stage/registry/docker per test -- see tests/test_multi_select.py's `graph` fixture
    for why this no longer has to be shared across the module.

    One test needed adjusting to survive the change: test_duplicate_preserves_internal_wiring
    below assumed a view already existed, which was only ever true because an earlier test in
    the file had made one.
    """
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


@pytest.fixture(autouse=True)
def _clear_selection_and_clipboard(graph):
    _qapp, _reg, scene, _docker = graph
    scene.clear_chunk_selection()
    nodegraph._node_clipboard.clear()
    nodegraph._edge_clipboard.clear()
    yield
    scene.clear_chunk_selection()
    nodegraph._node_clipboard.clear()
    nodegraph._edge_clipboard.clear()


def _make_nodes(graph, module="transform", n=1):
    qapp, reg, scene, docker = graph
    mod = reg.get_chunk_module(module)
    chunks = [_mk(mod, scene) for _ in range(n)]
    gv = docker.tab_widget.get_current_graph_view()
    # The deferred rebuild (QTimer.singleShot(0, ...)) needs more than one processEvents()
    # tick to settle once earlier tests in this module have left other timers queued up --
    # poll with a short timeout instead of assuming a single tick is enough.
    paths = [get_chunk_prim_path(c) for c in chunks]
    end = time.monotonic() + 2.0
    while time.monotonic() < end and any(p not in gv.scene()._nodes for p in paths):
        qapp.processEvents()
    nodes = [gv.scene()._nodes[p] for p in paths]
    return scene, gv, chunks, nodes


def _pump(qapp, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()


def _exec_ports(node):
    prim_path = str(node.prim.GetPath())
    return node.get_port(f"{prim_path}.inputs:chunk"), node.get_port(f"{prim_path}.outputs:chunk")


def _pick_action_by_text(text):
    """Stand-in for ChunkGraphView._exec_menu / ViewSlotState._exec_menu -- both are plain,
    overridable Python methods specifically so a test can pick a menu result without driving
    a real modal QMenu.exec() popup (PySide6 doesn't allow monkeypatching QMenu.exec itself:
    it's a bound virtual slot, and reassigning it on the class doesn't affect real calls,
    confirmed by a real .exec() hanging under offscreen even with a QTimer.singleShot(0)
    scheduled to trigger an action first)."""
    def _exec(menu, _pos):
        for act in menu.actions():
            if act.text() == text:
                return act
        return None
    return _exec


def test_copy_paste_creates_independent_duplicate_with_same_param_values(graph) -> None:
    scene, gv, chunks, nodes = _make_nodes(graph)
    (a,) = nodes
    tx = a.chunk.get_param("tx")
    tx.value = 5.5

    scene.set_selected_chunks([a.chunk])
    gv._copy_selected_to_clipboard()
    assert len(nodegraph._node_clipboard) == 1

    pasted = gv._paste_clipboard_at(QtCore.QPointF(500, 500))
    assert len(pasted) == 1
    new_chunk = pasted[0]

    assert new_chunk is not a.chunk
    assert new_chunk.get_param("tx").value == 5.5

    # Independence: editing the new one must not affect the original.
    new_chunk.get_param("tx").value = 99.0
    assert a.chunk.get_param("tx").value == 5.5


def test_copy_skips_hidden_bookkeeping_params(graph) -> None:
    qapp, reg, scene, docker = graph
    mod = reg.get_chunk_module("create_layer")
    cl = _mk(mod, scene)
    gv = docker.tab_widget.get_current_graph_view()
    cl_path = get_chunk_prim_path(cl)
    end = time.monotonic() + 2.0
    while time.monotonic() < end and cl_path not in gv.scene()._nodes:
        qapp.processEvents()

    cl.get_param("layer_name").value = "asd"
    # _new_layer_id is HIDDEN, execute()-owned bookkeeping caching this SPECIFIC chunk
    # instance's real anon Sdf.Layer identifier -- fake one in directly (as if execute()
    # had already run) rather than relying on a real cook, which this stage has no view
    # driving.
    cl.get_param("_new_layer_id").value = "anon:deadbeef:asd"

    scene.set_selected_chunks([cl])
    gv._copy_selected_to_clipboard()
    pasted = gv._paste_clipboard_at(QtCore.QPointF(7000, 7000))
    assert len(pasted) == 1
    new_cl = pasted[0]

    # The real, user-facing value copies...
    assert new_cl.get_param("layer_name").value == "asd"
    # ...but the HIDDEN bookkeeping param does not -- copying it verbatim would hand the
    # pasted node the original's cached layer identifier, so its own next execute() would
    # silently adopt and reuse the original's real Sdf.Layer instead of creating its own.
    assert new_cl.get_param("_new_layer_id").value != "anon:deadbeef:asd"


def test_pasted_widget_shows_the_copied_value_even_when_blocked(graph) -> None:
    """Regression: reported live -- a pasted node's Params-panel widget showed a blank
    field even though the underlying param value had actually copied correctly. Root
    cause: add_chunk's own notice dispatch can synchronously build that widget (when the
    target context already has an open tab), which reads its initial displayed text once,
    at construction -- so applying copied values AFTER add_chunk meant that first read saw
    the still-default value. A freshly pasted node also starts `blocked` whenever the
    target context already has an active view (exactly the case here), which suppresses
    the later reactive VALUE_CHANGED notice that would otherwise have corrected the
    widget -- so the blank became permanent instead of self-healing a moment later."""
    qapp, reg, scene, docker = graph
    mod = reg.get_chunk_module("create_layer")
    cl = _mk(mod, scene)
    gv = docker.tab_widget.get_current_graph_view()
    cl_path = get_chunk_prim_path(cl)
    end = time.monotonic() + 2.0
    while time.monotonic() < end and cl_path not in gv.scene()._nodes:
        qapp.processEvents()

    cl.get_param("layer_name").value = "hoj"
    qapp.processEvents()

    # A view driving this context is what makes the freshly pasted node start blocked --
    # the exact condition under which the bug was reported.
    gv._create_view_node_for_prim(cl_path)
    qapp.processEvents()
    assert gv._context_has_view(scene, gv._graph_context())

    scene.set_selected_chunks([cl])
    gv._copy_selected_to_clipboard()
    pasted = gv._paste_clipboard_at(QtCore.QPointF(8000, 8000))
    assert len(pasted) == 1
    new_cl = pasted[0]
    assert new_cl.is_blocked()
    assert new_cl.get_param("layer_name").value == "hoj"

    new_path = get_chunk_prim_path(new_cl)
    end = time.monotonic() + 2.0
    node = None
    while time.monotonic() < end:
        node = gv.scene()._nodes.get(new_path)
        if node is not None and getattr(node, "_chunk_widget", None) is not None:
            break
        qapp.processEvents()
    assert node is not None and node._chunk_widget is not None

    param_widget = next(
        w for w in node._chunk_widget.findChildren(QtWidgets.QWidget)
        if isinstance(w, chunk_widget_api.ChunkParamWidgetBase) and getattr(w.param, "name", None) == "layer_name"
    )
    line_edit = param_widget.findChild(QtWidgets.QLineEdit)
    assert line_edit is not None
    assert line_edit.text() == "hoj"


def test_paste_excludes_view_chunks_from_clipboard(graph) -> None:
    scene, gv, chunks, nodes = _make_nodes(graph, n=2)
    a, b = nodes
    # Simulate a view node the same way test_multi_select.py simulates a locked node --
    # flipping the cached flag directly rather than going through full view-creation
    # machinery, since only the clipboard's own filtering logic is under test here.
    b._is_view = True

    scene.set_selected_chunks([a.chunk, b.chunk])
    gv._copy_selected_to_clipboard()

    assert len(nodegraph._node_clipboard) == 1
    assert nodegraph._node_clipboard[0].module_name == a.chunk.get_config().module


def test_paste_lands_at_the_given_scene_position(graph) -> None:
    scene, gv, chunks, nodes = _make_nodes(graph)
    (a,) = nodes
    a.setPos(0, 0)

    scene.set_selected_chunks([a.chunk])
    gv._copy_selected_to_clipboard()
    pasted = gv._paste_clipboard_at(QtCore.QPointF(1234, 5678))
    new_chunk = pasted[0]

    new_node = gv.scene()._nodes[get_chunk_prim_path(new_chunk)]
    assert abs(new_node.pos().x() - 1234) < 1e-6
    assert abs(new_node.pos().y() - 5678) < 1e-6


def test_pasted_selection_becomes_the_new_selection(graph) -> None:
    scene, gv, chunks, nodes = _make_nodes(graph)
    (a,) = nodes

    scene.set_selected_chunks([a.chunk])
    gv._copy_selected_to_clipboard()
    pasted = gv._paste_clipboard_at(QtCore.QPointF(10, 10))

    assert set(scene.get_selected_chunks()) == set(pasted)


def test_paste_nests_into_the_current_chunkgroup_context(graph) -> None:
    qapp, reg, scene, docker = graph
    group_mod = reg.get_chunk_module("chunkgroup")
    group = _mk(group_mod, scene)
    qapp.processEvents()

    scene2, gv, chunks, nodes = _make_nodes(graph)
    (a,) = nodes
    scene.set_selected_chunks([a.chunk])
    gv._copy_selected_to_clipboard()

    graph_scene = gv.scene()
    prev_context = graph_scene._built_context
    graph_scene._built_context = group
    try:
        pasted = gv._paste_clipboard_at(QtCore.QPointF(0, 0))
    finally:
        graph_scene._built_context = prev_context

    assert len(pasted) == 1
    assert pasted[0].get_context() is group


def test_ctrl_v_pastes_and_does_not_also_create_a_view_node(graph, monkeypatch) -> None:
    scene, gv, chunks, nodes = _make_nodes(graph)
    (a,) = nodes
    scene.set_selected_chunks([a.chunk])
    gv._copy_selected_to_clipboard()

    view_created = []
    pasted_calls = []
    monkeypatch.setattr(gv, "_create_view_node", lambda: view_created.append(True))
    monkeypatch.setattr(gv, "_paste_at_cursor", lambda: pasted_calls.append(True))

    ctrl_v = QtGui.QKeyEvent(
        QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_V, QtCore.Qt.KeyboardModifier.ControlModifier
    )
    gv.keyPressEvent(ctrl_v)
    assert pasted_calls == [True]
    assert view_created == []

    plain_v = QtGui.QKeyEvent(
        QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_V, QtCore.Qt.KeyboardModifier.NoModifier
    )
    gv.keyPressEvent(plain_v)
    assert view_created == [True]
    assert pasted_calls == [True]


def test_ctrl_c_copies_current_selection(graph) -> None:
    scene, gv, chunks, nodes = _make_nodes(graph)
    (a,) = nodes
    scene.set_selected_chunks([a.chunk])

    ctrl_c = QtGui.QKeyEvent(
        QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_C, QtCore.Qt.KeyboardModifier.ControlModifier
    )
    gv.keyPressEvent(ctrl_c)

    assert len(nodegraph._node_clipboard) == 1
    assert nodegraph._node_clipboard[0].module_name == a.chunk.get_config().module


def test_copying_a_connected_chain_preserves_internal_wiring_on_paste(graph) -> None:
    qapp, reg, scene, docker = graph
    _scene, gv, chunks, nodes = _make_nodes(graph, n=3)
    a, b, c = nodes

    a_in, a_out = _exec_ports(a)
    b_in, b_out = _exec_ports(b)
    c_in, c_out = _exec_ports(c)
    gv._author_connection(a_out, b_in)
    gv._author_connection(b_out, c_in)
    gv.scene()._build_edges()

    # Copy only A and B -- C (and the real B->C wire) is deliberately left out.
    scene.set_selected_chunks([a.chunk, b.chunk])
    gv._copy_selected_to_clipboard()
    assert len(nodegraph._edge_clipboard) == 1  # only the internal A->B wire

    pasted = gv._paste_clipboard_at(QtCore.QPointF(3000, 3000))
    assert len(pasted) == 2
    _pump(qapp, 0.3)  # let the deferred (80ms) wiring pass run

    a2, b2 = pasted
    a2_path = get_chunk_prim_path(a2)
    b2_path = get_chunk_prim_path(b2)
    stage = docker.tab_widget.get_current_graph_view().scene()._stage

    b2_in_attr = stage.GetAttributeAtPath(Sdf.Path(f"{b2_path}.inputs:chunk"))
    assert b2_in_attr and b2_in_attr.IsValid()
    conns = [str(p) for p in b2_in_attr.GetConnections()]
    assert conns == [f"{a2_path}.outputs:chunk"]

    # The pasted B' must NOT pick up any connection to the real (uncopied) C.
    b2_out_attr = stage.GetAttributeAtPath(Sdf.Path(f"{b2_path}.outputs:chunk"))
    if b2_out_attr and b2_out_attr.IsValid():
        assert str(c.prim.GetPath()) not in "".join(str(p) for p in b2_out_attr.GetConnections())


def test_copying_without_the_wires_source_drops_it_on_paste(graph) -> None:
    qapp, reg, scene, docker = graph
    _scene, gv, chunks, nodes = _make_nodes(graph, n=2)
    a, b = nodes

    a_in, a_out = _exec_ports(a)
    b_in, b_out = _exec_ports(b)
    gv._author_connection(a_out, b_in)
    gv.scene()._build_edges()

    # Copy only B -- A (the real source of B's exec input) is left out.
    scene.set_selected_chunks([b.chunk])
    gv._copy_selected_to_clipboard()
    assert nodegraph._edge_clipboard == []

    pasted = gv._paste_clipboard_at(QtCore.QPointF(3500, 3500))
    assert len(pasted) == 1
    _pump(qapp, 0.3)

    b2_path = get_chunk_prim_path(pasted[0])
    stage = docker.tab_widget.get_current_graph_view().scene()._stage
    b2_in_attr = stage.GetAttributeAtPath(Sdf.Path(f"{b2_path}.inputs:chunk"))
    assert not b2_in_attr or not b2_in_attr.IsValid() or not b2_in_attr.GetConnections()


def test_duplicate_creates_offset_copy_without_touching_the_clipboard(graph) -> None:
    scene, gv, chunks, nodes = _make_nodes(graph, n=2)
    a, b = nodes
    a.setPos(100, 100)

    # Pre-populate the clipboard with B, distinct from what we're about to duplicate (A).
    scene.set_selected_chunks([b.chunk])
    gv._copy_selected_to_clipboard()
    assert nodegraph._node_clipboard[0].module_name == b.chunk.get_config().module

    scene.set_selected_chunks([a.chunk])
    gv._duplicate_selected()

    # Clipboard from the earlier Ctrl+C is untouched by Ctrl+D.
    assert len(nodegraph._node_clipboard) == 1
    assert nodegraph._node_clipboard[0].module_name == b.chunk.get_config().module

    selected = scene.get_selected_chunks()
    assert len(selected) == 1
    dup = selected[0]
    assert dup is not a.chunk
    dup_node = gv.scene()._nodes[get_chunk_prim_path(dup)]
    assert abs(dup_node.pos().x() - 140) < 1e-6
    assert abs(dup_node.pos().y() - 140) < 1e-6


def test_duplicate_preserves_internal_wiring(graph) -> None:
    qapp, reg, scene, docker = graph
    _scene, gv, chunks, nodes = _make_nodes(graph, n=2)
    a, b = nodes
    a.setPos(200, 200)
    b.setPos(400, 200)

    a_in, a_out = _exec_ports(a)
    b_in, b_out = _exec_ports(b)
    gv._author_connection(a_out, b_in)
    gv.scene()._build_edges()

    # Let the deferred auto-view settle BEFORE duplicating. _ensure_default_view runs on an
    # 80ms timer when a context has no view yet, and it selects the view it creates -- which
    # lands in the middle of this test and takes the selection the duplicates should hold.
    # Previously masked by module-scoped fixtures: an earlier test in the file had already
    # caused a view to exist, so the timer never fired here.
    _pump(qapp, 0.3)

    scene.set_selected_chunks([a.chunk, b.chunk])
    gv._duplicate_selected()
    _pump(qapp, 0.3)

    dup_chunks = list(scene.get_selected_chunks())
    assert len(dup_chunks) == 2
    # Identify a2/b2 by position instead of assuming order: whichever is at (240,240) is A'.
    a2_node = None
    b2_node = None
    for c in dup_chunks:
        node = gv.scene()._nodes[get_chunk_prim_path(c)]
        if abs(node.pos().x() - 240) < 1e-6:
            a2_node = node
        elif abs(node.pos().x() - 440) < 1e-6:
            b2_node = node
    assert a2_node is not None and b2_node is not None

    stage = docker.tab_widget.get_current_graph_view().scene()._stage
    b2_path = str(b2_node.prim.GetPath())
    a2_path = str(a2_node.prim.GetPath())
    b2_in_attr = stage.GetAttributeAtPath(Sdf.Path(f"{b2_path}.inputs:chunk"))
    assert b2_in_attr and b2_in_attr.IsValid()
    assert [str(p) for p in b2_in_attr.GetConnections()] == [f"{a2_path}.outputs:chunk"]


def test_ctrl_d_duplicates_and_does_not_also_toggle_disable(graph, monkeypatch) -> None:
    scene, gv, chunks, nodes = _make_nodes(graph)
    (a,) = nodes
    scene.set_selected_chunks([a.chunk])

    dup_calls = []
    disable_calls = []
    monkeypatch.setattr(gv, "_duplicate_selected", lambda: dup_calls.append(True))
    monkeypatch.setattr(gv, "_toggle_disable_selected", lambda: disable_calls.append(True))

    ctrl_d = QtGui.QKeyEvent(
        QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_D, QtCore.Qt.KeyboardModifier.ControlModifier
    )
    gv.keyPressEvent(ctrl_d)
    assert dup_calls == [True]
    assert disable_calls == []

    plain_d = QtGui.QKeyEvent(
        QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_D, QtCore.Qt.KeyboardModifier.NoModifier
    )
    gv.keyPressEvent(plain_d)
    assert disable_calls == [True]
    assert dup_calls == [True]


def _wait_for_node(qapp, gv, path, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end and path not in gv.scene()._nodes:
        qapp.processEvents()
    return gv.scene()._nodes.get(path)


def _isolated_graph(qapp):
    """A fresh, private stage/registry/docker -- see test_typed_param_connection_is_preserved
    _on_paste's docstring for why: isolation here is specifically to get a deterministic
    chunk count unaffected by whatever auto-view/reactive-rebuild timers a shared module
    stage's earlier tests may have left in flight (recursive group duplication triggers
    several chunk creations + a real exec connection, which was observed to be sensitive to
    that leftover activity in a way the simpler flat-node tests weren't)."""
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
    return reg, scene, docker


def test_cut_copies_and_deletes_the_selection(graph) -> None:
    qapp, reg, scene, docker = graph
    _scene, gv, chunks, nodes = _make_nodes(graph, n=2)
    a, b = nodes
    a_path = get_chunk_prim_path(a.chunk)
    b_path = get_chunk_prim_path(b.chunk)

    scene.set_selected_chunks([a.chunk, b.chunk])
    gv._cut_selected_to_clipboard()

    assert len(nodegraph._node_clipboard) == 2

    _pump(qapp, 0.3)
    assert a_path not in gv.scene()._nodes
    assert b_path not in gv.scene()._nodes

    # A subsequent paste from the cut clipboard still works, independent of the originals.
    pasted = gv._paste_clipboard_at(QtCore.QPointF(5000, 5000))
    assert len(pasted) == 2


def test_cut_skips_a_locked_node_but_still_cuts_the_rest_of_the_selection(graph) -> None:
    qapp, reg, scene, docker = graph
    _scene, gv, chunks, nodes = _make_nodes(graph, n=2)
    locked, normal = nodes
    locked._locked = True
    locked_path = get_chunk_prim_path(locked.chunk)
    normal_path = get_chunk_prim_path(normal.chunk)

    scene.set_selected_chunks([locked.chunk, normal.chunk])
    gv._cut_selected_to_clipboard()

    # Both are still copied...
    assert len(nodegraph._node_clipboard) == 2

    _pump(qapp, 0.3)
    # ...but only the unlocked one is actually removed from the graph.
    assert normal_path not in gv.scene()._nodes
    assert locked_path in gv.scene()._nodes


def test_ctrl_x_triggers_cut(graph, monkeypatch) -> None:
    scene, gv, chunks, nodes = _make_nodes(graph)
    (a,) = nodes
    scene.set_selected_chunks([a.chunk])

    cut_calls = []
    monkeypatch.setattr(gv, "_cut_selected_to_clipboard", lambda: cut_calls.append(True))

    ctrl_x = QtGui.QKeyEvent(
        QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_X, QtCore.Qt.KeyboardModifier.ControlModifier
    )
    gv.keyPressEvent(ctrl_x)
    assert cut_calls == [True]


def test_node_menu_copy_acts_on_the_whole_existing_selection(graph, monkeypatch) -> None:
    scene, gv, chunks, nodes = _make_nodes(graph, n=2)
    scene.clear_chunk_selection()
    a, b = nodes
    scene.set_selected_chunks([a.chunk, b.chunk])

    monkeypatch.setattr(gv._view_state, "_exec_menu", _pick_action_by_text("Copy"))
    gv._show_chunk_menu(a, QtCore.QPoint(0, 0))

    assert len(nodegraph._node_clipboard) == 2
    # Right-clicking a node that was already part of the selection must not collapse it.
    assert set(scene.get_selected_chunks()) == {a.chunk, b.chunk}


def test_node_menu_on_an_unselected_node_selects_it_first(graph, monkeypatch) -> None:
    scene, gv, chunks, nodes = _make_nodes(graph, n=2)
    scene.clear_chunk_selection()
    a, b = nodes
    scene.set_selected_chunks([a.chunk])  # b is NOT selected

    monkeypatch.setattr(gv._view_state, "_exec_menu", _pick_action_by_text("Duplicate"))
    gv._show_chunk_menu(b, QtCore.QPoint(0, 0))

    # Duplicate must have acted on b (now selected by the right-click), not on a.
    dup_selection = list(scene.get_selected_chunks())
    assert len(dup_selection) == 1
    assert dup_selection[0] is not a.chunk and dup_selection[0] is not b.chunk


def test_node_menu_cut_removes_the_selection(graph, monkeypatch) -> None:
    qapp, reg, scene, docker = graph
    _scene, gv, chunks, nodes = _make_nodes(graph, n=1)
    scene.clear_chunk_selection()
    (a,) = nodes
    a_path = get_chunk_prim_path(a.chunk)
    scene.set_selected_chunks([a.chunk])

    monkeypatch.setattr(gv._view_state, "_exec_menu", _pick_action_by_text("Cut"))
    gv._show_chunk_menu(a, QtCore.QPoint(0, 0))

    assert len(nodegraph._node_clipboard) == 1
    end = time.monotonic() + 2.0
    while time.monotonic() < end and a_path in gv.scene()._nodes:
        qapp.processEvents()
    assert a_path not in gv.scene()._nodes


def test_canvas_menu_paste_only_offered_when_clipboard_has_content(graph, monkeypatch) -> None:
    _scene, gv, chunks, nodes = _make_nodes(graph, n=1)
    _scene.clear_chunk_selection()

    # Empty clipboard: the quick menu must not appear at all -- straight to the fuzzy
    # "New Chunk" search popup, i.e. FilterableChunkMenu.select_module gets called. Stub it
    # to confirm that's the path taken, and make it a no-op (returns None -> nothing created).
    called = []
    monkeypatch.setattr(
        nodegraph.FilterableChunkMenu, "select_module", lambda self, pos: called.append(True) or None
    )
    far_empty = gv.mapFromScene(QtCore.QPointF(-40000, -40000))
    ev = QtGui.QContextMenuEvent(QtGui.QContextMenuEvent.Reason.Mouse, far_empty, far_empty)
    gv.contextMenuEvent(ev)
    assert called == [True]


def test_canvas_menu_new_chunk_option_falls_through_to_fuzzy_search(graph, monkeypatch) -> None:
    scene, gv, chunks, nodes = _make_nodes(graph, n=1)
    scene.clear_chunk_selection()
    (a,) = nodes
    scene.set_selected_chunks([a.chunk])
    gv._copy_selected_to_clipboard()

    called = []
    monkeypatch.setattr(
        nodegraph.FilterableChunkMenu, "select_module", lambda self, pos: called.append(True) or None
    )
    monkeypatch.setattr(gv, "_exec_menu", _pick_action_by_text("New Chunk..."))

    far_empty = gv.mapFromScene(QtCore.QPointF(-20000, -20000))
    ev = QtGui.QContextMenuEvent(QtGui.QContextMenuEvent.Reason.Mouse, far_empty, far_empty)
    gv.contextMenuEvent(ev)

    assert called == [True]


def test_copying_a_group_recursively_duplicates_its_children(qapp) -> None:
    # Isolated stage, and placed after every shared-`graph` test in this file -- see
    # test_typed_param_connection_is_preserved_on_paste's docstring for why an isolated
    # ChunkLayerManager.initialize() call must not run before a later shared-`graph` test.
    reg, scene, docker = _isolated_graph(qapp)
    transform_mod = reg.get_chunk_module("transform")
    group_mod = reg.get_chunk_module("chunkgroup")
    # A ChunkGroup created as the very first-ever chunk on a stage becomes its own root tab
    # (create_chunk_widget's "no parent context yet" branch) instead of a node inside the
    # default root context -- bootstrap that default context with a throwaway leaf chunk
    # first, same as every shared-`graph` test in this file implicitly already has by the
    # time it creates its own first group.
    _mk(transform_mod, scene)
    qapp.processEvents()
    group = _mk(group_mod, scene)
    qapp.processEvents()
    gv = docker.tab_widget.get_current_graph_view()

    with scene.creating_in(gv._nest_target(group)):
        child_a = _mk(transform_mod, scene)
        child_b = _mk(transform_mod, scene)
    qapp.processEvents()
    assert child_a.get_context() is group
    assert child_b.get_context() is group

    stage = gv.scene()._stage
    a_path = get_chunk_prim_path(child_a)
    b_path = get_chunk_prim_path(child_b)
    # No live Qt ports exist for a group's children unless someone's dived into it -- wire
    # them directly at the USD level, the same mechanism recursive paste itself uses.
    nodegraph._author_raw_connection(stage, b_path, "inputs:chunk", child_b, a_path, "outputs:chunk")

    group_path = get_chunk_prim_path(group)
    assert _wait_for_node(qapp, gv, group_path) is not None

    scene.set_selected_chunks([group])
    gv._copy_selected_to_clipboard()
    assert len(nodegraph._node_clipboard) == 1
    clip = nodegraph._node_clipboard[0]
    assert len(clip.children) == 2
    assert len(clip.child_edges) == 1

    pasted = gv._paste_clipboard_at(QtCore.QPointF(6000, 6000))
    assert len(pasted) == 1
    new_group = pasted[0]
    assert new_group is not group

    # get_context() returns self for a CONTEXT-flagged chunk, so filtering "children of
    # new_group" by get_context() equality alone would also match new_group against itself --
    # exclude it explicitly, same as production's _iter_group_children does.
    new_children = [
        c for c in scene.get_chunks()
        if c.get_context() is new_group and c is not new_group and not nodegraph.chunk_stage.is_view_chunk(c)
    ]
    assert len(new_children) == 2
    assert child_a not in new_children and child_b not in new_children

    new_paths = [get_chunk_prim_path(c) for c in new_children]
    wired = False
    for i, c in enumerate(new_children):
        attr = stage.GetAttributeAtPath(Sdf.Path(f"{new_paths[i]}.inputs:chunk"))
        if attr and attr.IsValid() and attr.GetConnections():
            other_path = new_paths[1 - i]
            assert [str(p) for p in attr.GetConnections()] == [f"{other_path}.outputs:chunk"]
            wired = True
    assert wired, "the new children must be wired to each other, not left disconnected"


def test_duplicating_a_nested_group_in_group_produces_independent_copies_at_both_levels(qapp) -> None:
    reg, scene, docker = _isolated_graph(qapp)
    transform_mod = reg.get_chunk_module("transform")
    group_mod = reg.get_chunk_module("chunkgroup")
    # See test_copying_a_group_recursively_duplicates_its_children's comment: a ChunkGroup
    # as the very first-ever chunk becomes its own root tab instead of a node inside the
    # default root context.
    _mk(transform_mod, scene)
    qapp.processEvents()
    outer = _mk(group_mod, scene)
    qapp.processEvents()
    gv = docker.tab_widget.get_current_graph_view()

    with scene.creating_in(gv._nest_target(outer)):
        inner = _mk(group_mod, scene)
    qapp.processEvents()
    # A nested ChunkGroup's own get_context() returns itself (documented quirk -- see
    # nodegraph._iter_group_children's docstring), never its structural parent, so "is inner
    # a child of outer" has to be checked via the same combined lookup _iter_group_children
    # (and _rebuild_nodes) use instead of get_context() equality.
    stage = gv.scene()._stage
    assert inner in nodegraph._iter_group_children(outer, scene, stage)

    with scene.creating_in(gv._nest_target(inner)):
        leaf = _mk(transform_mod, scene)
    qapp.processEvents()
    assert leaf.get_context() is inner

    outer_path = get_chunk_prim_path(outer)
    assert _wait_for_node(qapp, gv, outer_path) is not None

    scene.set_selected_chunks([outer])
    gv._duplicate_selected()

    dup_selection = list(scene.get_selected_chunks())
    assert len(dup_selection) == 1
    new_outer = dup_selection[0]
    assert new_outer is not outer

    new_inners = [
        c for c in nodegraph._iter_group_children(new_outer, scene, stage)
        if nodegraph.chunk_stage.is_group_chunk(c)
    ]
    assert len(new_inners) == 1
    new_inner = new_inners[0]
    assert new_inner is not inner

    new_leaves = [c for c in scene.get_chunks() if c.get_context() is new_inner and c is not new_inner]
    assert len(new_leaves) == 1
    assert new_leaves[0] is not leaf


def test_typed_param_connection_is_preserved_on_paste(qapp) -> None:
    # Deliberately NOT the shared module-scoped `graph` fixture, and deliberately the LAST
    # test in this file: ChunkRegistry/ChunkLayerManager are process-wide singletons, and
    # this test's own chunk_stage.initialize()/ChunkLayerManager.initialize() calls repoint
    # them at THIS test's isolated stage. conftest.py's autouse reset_chunk_layer_manager
    # fixture then resets ChunkLayerManager to uninitialized once this test function
    # returns -- which silently breaks prim/layer bookkeeping for any LATER test still using
    # the shared `graph` fixture's stage (observed as newly created chunks never showing up
    # in the graph at all). Keeping this test last, after every shared-`graph` test in this
    # file, avoids that ordering hazard entirely.
    #
    # The isolation itself is for a different reason: a "no active view in this context"
    # recook falls back to re-executing every chunk (see
    # ChunkGraphView._recook_active_view's docstring), and transform.py's own execute()
    # unconditionally Clear()s its chunk's bookkeeping layer on every run -- which, with
    # many other tests' worth of nodes and timers sharing one stage, was observed to
    # occasionally race and wipe the very connection this test just authored before it
    # could assert on it.
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

    mod = reg.get_chunk_module("transform")
    a, b = _mk(mod, scene), _mk(mod, scene)
    qapp.processEvents()
    gv = docker.tab_widget.get_current_graph_view()
    a = gv.scene()._nodes[get_chunk_prim_path(a)]
    b = gv.scene()._nodes[get_chunk_prim_path(b)]
    _pump(qapp, 0.5)  # let proxy widgets + typed ports build (deferred singleShot chain)

    a_path = str(a.prim.GetPath())
    b_path = str(b.prim.GetPath())
    a_tx_out = a.get_port(f"{a_path}.outputs:tx")
    b_tx_in = b.get_port(f"{b_path}.inputs:tx")
    assert a_tx_out is not None and b_tx_in is not None, "typed ports must exist by now"
    gv._author_connection(a_tx_out, b_tx_in)
    gv.scene()._build_edges()

    scene.set_selected_chunks([a.chunk, b.chunk])
    gv._copy_selected_to_clipboard()
    assert any(e.dst_attr == "inputs:tx" for e in nodegraph._edge_clipboard)

    pasted = gv._paste_clipboard_at(QtCore.QPointF(4000, 4000))
    assert len(pasted) == 2
    _pump(qapp, 0.5)

    a2, b2 = pasted
    b2_path = get_chunk_prim_path(b2)
    stage = docker.tab_widget.get_current_graph_view().scene()._stage
    b2_tx_in_attr = stage.GetAttributeAtPath(Sdf.Path(f"{b2_path}.inputs:tx"))
    assert b2_tx_in_attr and b2_tx_in_attr.IsValid() and b2_tx_in_attr.GetConnections()


def test_canvas_menu_paste_pastes_at_the_clicked_point(qapp) -> None:
    # Isolated stage, and placed absolute-last in this file -- see
    # test_typed_param_connection_is_preserved_on_paste's docstring just above for why an
    # isolated ChunkLayerManager.initialize() call must never run before a later shared-
    # `graph` test in this file.
    reg, scene, docker = _isolated_graph(qapp)
    mod = reg.get_chunk_module("transform")
    a_chunk = _mk(mod, scene)
    gv = docker.tab_widget.get_current_graph_view()
    a_path = get_chunk_prim_path(a_chunk)
    end = time.monotonic() + 2.0
    while time.monotonic() < end and a_path not in gv.scene()._nodes:
        qapp.processEvents()

    scene.set_selected_chunks([a_chunk])
    gv._copy_selected_to_clipboard()
    assert len(nodegraph._node_clipboard) == 1

    far_empty = gv.mapFromScene(QtCore.QPointF(-30000, -30000))
    gv._exec_menu = _pick_action_by_text("Paste")
    ev = QtGui.QContextMenuEvent(QtGui.QContextMenuEvent.Reason.Mouse, far_empty, far_empty)
    gv.contextMenuEvent(ev)

    selected = scene.get_selected_chunks()
    assert len(selected) == 1
    new_chunk = selected[0]
    assert new_chunk is not a_chunk
    new_node = gv.scene()._nodes[get_chunk_prim_path(new_chunk)]
    scene_pos = gv.mapToScene(far_empty)
    assert abs(new_node.pos().x() - scene_pos.x()) < 1e-6
    assert abs(new_node.pos().y() - scene_pos.y()) < 1e-6
