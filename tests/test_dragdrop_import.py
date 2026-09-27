from __future__ import annotations

import os
import time

import pytest
from pxr import Sdf, Usd, UsdUtils
from PySide6 import QtCore

from usd_chunks.chunk_stage import get_chunk_prim_path, initialize as _chunk_usd_init
from usd_chunks import chunk_stage
from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.chunk_flags import ChunkFlags
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


@pytest.fixture
def graph(qapp):
    """A fresh stage per test (unlike tests/test_copy_paste.py's module-scoped `graph`) --
    the tests in this file export_chunks() the WHOLE stage to a file and re-import it, so
    leftover nodes from an earlier test in the same module would silently bleed into a later
    test's exported file and its node counts. chunk_stage.initialize() leaks a global notice
    listener per call with no reset hook, same as noted there, but this file's small test
    count keeps that an acceptable tradeoff for isolation."""
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


def _pump(qapp, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()


def _exec_ports(node):
    prim_path = str(node.prim.GetPath())
    return node.get_port(f"{prim_path}.inputs:chunk"), node.get_port(f"{prim_path}.outputs:chunk")


def _pick_action_by_text(text):
    def _exec(menu, _pos):
        for act in menu.actions():
            if act.text() == text:
                return act
        return None
    return _exec


def _count_layers_named(stage, name: str) -> int:
    """Count distinct layers reachable from stage (session + root, recursing through
    subLayerPaths) whose display name (the anon layer's tag) equals `name`."""
    seen: set = set()
    count = 0

    def _walk(layer) -> None:
        nonlocal count
        if layer is None or layer.identifier in seen:
            return
        seen.add(layer.identifier)
        if layer.GetDisplayName() == name:
            count += 1
        for sub in layer.subLayerPaths:
            _walk(Sdf.Layer.Find(sub) or Sdf.Layer.FindOrOpen(sub))

    _walk(stage.GetSessionLayer())
    _walk(stage.GetRootLayer())
    return count


def _export_selected(scene, chunks, path) -> None:
    """Export only the given chunks' composed prim specs (with their real wiring) to path --
    unlike chunk_stage.export_chunks, which always dumps the WHOLE stage. Used to build a
    dropped-file fixture that contains exactly one chain, isolated from unrelated chunks
    that may also exist live on the same test stage.

    Copies the parent (tab/context) prim's own REAL spec from the live stage when one
    exists there, rather than Sdf.CreatePrimInLayer's synthesized stand-in -- that defaults
    to SpecifierOver, and an "over" with no "def" anywhere in a standalone single-layer file
    means the parent prim doesn't compose at all, silently hiding every child from a
    default-predicate stage.Traverse() (confirmed: chunk_stage.export_chunks itself never hits
    this because it always includes the real context prim's own spec, def and all, in
    exactly this situation)."""
    stage = scene.get_stage()
    flat = UsdUtils.FlattenLayerStack(stage)
    dst = Sdf.Layer.CreateAnonymous()
    for c in chunks:
        p = Sdf.Path(get_chunk_prim_path(c))
        # Every missing ancestor, not just the immediate parent. A chunk sits inside the
        # scene's default chunkgroup now, so copying its parent spec means writing at
        # /chunks/<group> -- which Sdf refuses while /chunks itself has no spec to hold it.
        ancestors = []
        parent = p.GetParentPath()
        while not parent.isEmpty and parent != Sdf.Path.absoluteRootPath:
            ancestors.append(parent)
            parent = parent.GetParentPath()
        for ancestor in reversed(ancestors):
            if dst.GetPrimAtPath(ancestor):
                continue
            Sdf.CreatePrimInLayer(dst, ancestor)
            if flat.GetPrimAtPath(ancestor):
                Sdf.CopySpec(flat, ancestor, dst, ancestor)
        Sdf.CopySpec(flat, p, dst, p)
    dst.Export(str(path))


def _make_nodes(graph, module="transform", n=1):
    qapp, reg, scene, docker = graph
    mod = reg.get_chunk_module(module)
    chunks = [_mk(mod, scene) for _ in range(n)]
    gv = docker.tab_widget.get_current_graph_view()
    paths = [get_chunk_prim_path(c) for c in chunks]
    end = time.monotonic() + 2.0
    while time.monotonic() < end and any(p not in gv.scene()._nodes for p in paths):
        qapp.processEvents()
    nodes = [gv.scene()._nodes[p] for p in paths]
    return scene, gv, chunks, nodes


def test_import_as_chunks_does_not_duplicate_create_layer_side_effect(graph, tmp_path) -> None:
    """Regression: reported live -- importing a chunkgroup containing a Create Layer chunk
    (layer_name 'asd') produced TWO orphaned 'asd' anon layers in addition to the source's
    own, instead of exactly one new, cleanly-nested layer for the imported copy. Root cause
    (two independent premature-execute sites, both now fixed):

      1. _build_clip_node_from_prim's throwaway, never-added-to-any-scene snapshot-reading
         chunk instance -- restoring its value via chunk_stage._load_wire_inputs ran Create
         Layer's REAL reactive execute(), because ChunkBase.get_stage() returns the real
         live stage regardless of scene membership, not None for an unregistered instance.
      2. _materialize_at applied copied/imported values to the newly materialized chunk
         BEFORE blocking it, so the same premature real execute() fired a second time, on
         the real (not throwaway) new chunk, before it was ever wired/positioned.

    Both are fixed by blocking (which chunk_notice.py's dispatch checks before running a
    module's real execute()) BEFORE any value gets applied, not just before add_chunk."""
    qapp, reg, scene, docker = graph
    mod = reg.get_chunk_module("create_layer")
    cl = _mk(mod, scene)
    gv = docker.tab_widget.get_current_graph_view()
    cl_path = get_chunk_prim_path(cl)
    end = time.monotonic() + 2.0
    while time.monotonic() < end and cl_path not in gv.scene()._nodes:
        qapp.processEvents()

    cl.get_param("layer_name").value = "asd"
    _pump(qapp, 0.3)
    stage = gv.scene()._stage
    assert _count_layers_named(stage, "asd") == 1  # sanity: the source itself is clean

    path = tmp_path / "create_layer_asd.usda"
    _export_selected(scene, [cl], path)

    before_paths = set(gv.scene()._nodes.keys())
    gv._import_as_chunks(str(path), QtCore.QPointF(14000, 14000))
    _pump(qapp, 0.5)

    new_paths = set(gv.scene()._nodes.keys()) - before_paths
    assert len(new_paths) == 1
    imported = gv.scene()._nodes[new_paths.pop()].chunk

    # A create_layer chunk that never gets a real, legitimate execute (still blocked --
    # target context already had a view by this point, from cl's own auto-view) must add NO
    # new "asd" layer at all; one that does execute for real must add exactly one. Either
    # way, no orphaned extras from the import process itself -- the two premature-execute
    # sites above always added one each, REGARDLESS of the imported chunk's final blocked
    # state, which is what made this reproducible independent of view/timing specifics.
    expected = 1 if imported.is_blocked() else 2
    assert _count_layers_named(stage, "asd") == expected


def test_import_as_chunks_restores_value_driven_widget_visibility(graph, tmp_path) -> None:
    """Regression: reported live -- importing a Create Layer chunk with insert_mode="Index"
    restored the value correctly (visible in the field) but the Index spinbox widget itself
    stayed missing, because insert_mode's own add_callback handler (which unhides
    insert_index -- chunks/usd/create_layer.py's _insert_mode_changed, registered on
    ChunkNoticeAlias.VALUE_CHANGED) never got a chance to run: the fix for the duplicate-layer
    bug above used set_blocked(True) during value restoration, which chunk_notice.py's
    value_changed() also treats as "suppress every per-param notice", not just the reserved
    module.execute() hook. Switched to guarding with _executing instead (see _materialize_at),
    which only gates the reserved hook -- insert_index must end up NOT hidden once the import
    settles, matching what typing "Index" into a live, already-created chunk would do."""
    qapp, reg, scene, docker = graph
    mod = reg.get_chunk_module("create_layer")
    cl = _mk(mod, scene)
    gv = docker.tab_widget.get_current_graph_view()
    cl_path = get_chunk_prim_path(cl)
    end = time.monotonic() + 2.0
    while time.monotonic() < end and cl_path not in gv.scene()._nodes:
        qapp.processEvents()

    cl.get_param("insert_mode").value = "Index"
    _pump(qapp, 0.3)
    assert not cl.get_param("insert_index").has_state(ChunkFlags.State.HIDDEN)  # sanity: source is fine

    path = tmp_path / "create_layer_index.usda"
    _export_selected(scene, [cl], path)

    before_paths = set(gv.scene()._nodes.keys())
    gv._import_as_chunks(str(path), QtCore.QPointF(15000, 15000))
    _pump(qapp, 0.5)

    new_paths = set(gv.scene()._nodes.keys()) - before_paths
    assert len(new_paths) == 1
    imported = gv.scene()._nodes[new_paths.pop()].chunk
    assert imported.get_param("insert_mode").value == "Index"
    assert not imported.get_param("insert_index").has_state(ChunkFlags.State.HIDDEN)


def test_detect_dropped_usd_kind_classifies_chunkgroup_vs_scene(graph, tmp_path) -> None:
    scene, gv, chunks, nodes = _make_nodes(graph)
    chunks_path = tmp_path / "saved_chunkgroup.usda"
    chunk_stage.export_chunks(scene, str(chunks_path))
    assert nodegraph._detect_dropped_usd_kind(str(chunks_path)) == "chunks"

    scene_path = tmp_path / "plain_scene.usda"
    scene_stage = Usd.Stage.CreateNew(str(scene_path))
    scene_stage.DefinePrim("/geo", "Xform")
    scene_stage.GetRootLayer().Save()
    assert nodegraph._detect_dropped_usd_kind(str(scene_path)) == "scene"

    not_usd = tmp_path / "notes.txt"
    not_usd.write_text("hello")
    assert nodegraph._detect_dropped_usd_kind(str(not_usd)) is None


def test_detect_and_snapshot_work_for_a_non_default_tab_root(graph, tmp_path) -> None:
    """Regression: a chunk's root is its tab's own prim_path (chunk_core.add_context names a
    new tab "/" + the tab name), not a fixed "/chunks" -- that's only what this test file's
    OTHER tests happen to get because their default tab is literally named "chunks". A real
    usdview session's tab is normally named something else (e.g. "Main"), so a chunkgroup
    saved from it has its chunks under "/Main", not "/chunks" -- detection/snapshot must not
    assume the fixed root or they silently find nothing, as originally shipped."""
    qapp, reg, scene, docker = graph
    scene.add_context("Workflow")
    mod = reg.get_chunk_module("transform")
    t = _mk(mod, scene)
    assert get_chunk_prim_path(t).startswith("/Workflow/")
    t.get_param("tx").value = 7.25

    path = tmp_path / "named_tab_chunkgroup.usda"
    chunk_stage.export_chunks(scene, str(path))

    assert nodegraph._detect_dropped_usd_kind(str(path)) == "chunks"

    layer = Sdf.Layer.FindOrOpen(str(path))
    clip_nodes, clip_edges = nodegraph._snapshot_layer_chunks(layer)
    assert len(clip_nodes) == 1
    assert clip_nodes[0].module_name == "transform"


def test_snapshot_layer_chunks_recreates_node_with_same_param_value(graph, tmp_path) -> None:
    scene, gv, chunks, nodes = _make_nodes(graph)
    (a,) = nodes
    a.chunk.get_param("tx").value = 5.5

    path = tmp_path / "one_chunk.usda"
    chunk_stage.export_chunks(scene, str(path))

    layer = Sdf.Layer.FindOrOpen(str(path))
    clip_nodes, clip_edges = nodegraph._snapshot_layer_chunks(layer)
    assert len(clip_nodes) == 1
    assert clip_nodes[0].module_name == "transform"

    pasted = gv._materialize_at(clip_nodes, clip_edges, QtCore.QPointF(9000, 9000))
    assert len(pasted) == 1
    new_chunk = pasted[0]
    assert new_chunk is not a.chunk
    assert new_chunk.get_param("tx").value == 5.5

    new_chunk.get_param("tx").value = 99.0
    assert a.chunk.get_param("tx").value == 5.5


def test_snapshot_layer_chunks_preserves_wiring(graph, tmp_path) -> None:
    qapp, reg, scene, docker = graph
    _scene, gv, chunks, nodes = _make_nodes(graph, n=2)
    a, b = nodes
    a_in, a_out = _exec_ports(a)
    b_in, b_out = _exec_ports(b)
    gv._author_connection(a_out, b_in)
    gv.scene()._build_edges()

    scene.set_selected_chunks([a.chunk, b.chunk])
    path = tmp_path / "wired_chunks.usda"
    chunk_stage.export_chunks(scene, str(path))

    layer = Sdf.Layer.FindOrOpen(str(path))
    clip_nodes, clip_edges = nodegraph._snapshot_layer_chunks(layer)
    assert len(clip_nodes) == 2
    assert len(clip_edges) == 1

    pasted = gv._materialize_at(clip_nodes, clip_edges, QtCore.QPointF(9500, 9500))
    assert len(pasted) == 2
    _pump(qapp, 0.3)

    a2, b2 = pasted
    a2_path = get_chunk_prim_path(a2)
    b2_path = get_chunk_prim_path(b2)
    stage = gv.scene()._stage
    b2_in_attr = stage.GetAttributeAtPath(Sdf.Path(f"{b2_path}.inputs:chunk"))
    assert b2_in_attr and b2_in_attr.IsValid()
    assert [str(p) for p in b2_in_attr.GetConnections()] == [f"{a2_path}.outputs:chunk"]


def test_import_as_sublayer_creates_prefilled_node(graph, tmp_path) -> None:
    qapp, reg, scene, docker = graph
    _, gv, _, _ = _make_nodes(graph)  # forces a tab (and its graph view) to exist

    scene_path = tmp_path / "friend_scene.usda"
    friend_stage = Usd.Stage.CreateNew(str(scene_path))
    friend_stage.DefinePrim("/geo", "Xform")
    friend_stage.GetRootLayer().Save()

    gv._import_as_sublayer(str(scene_path), QtCore.QPointF(10000, 10000))
    _pump(qapp, 0.3)

    sublayer_chunks = [c for c in scene.get_chunks() if c.get_config() and c.get_config().module == "sublayer"]
    assert len(sublayer_chunks) == 1
    sub = sublayer_chunks[0]
    list_group = sub.get_param("imports")
    assert list_group.size == 1
    row = list_group.get_indexed_param("sublayer_path", 0)
    assert row.value == str(scene_path)

    edit_layer = sub.get_edit_context()
    assert edit_layer is not None
    assert list(edit_layer.subLayerPaths) == [str(scene_path)]


def test_import_as_sublayer_writes_sublayer_paths_even_with_an_active_view(graph, tmp_path) -> None:
    """Regression: reported live -- dropping a scene file onto a graph tab that ALREADY had
    an active view produced a Sublayer node with the path visibly in its field but
    subLayerPaths never actually written (an "empty sublayer"). Root cause: _import_as_sublayer
    used to block the new chunk whenever view_exists (mirroring _materialize_at's Create-Layer
    guard), but chunk_notice.py's data_changed/send_self return early while ANY ancestor in the
    owner chain is_blocked() -- so the ATTRIBUTE_CHANGED notice that drives Sublayer's own
    add_callback handler (the thing that actually authors subLayerPaths) never fired at all.
    My other, otherwise-passing sublayer test didn't catch this because it ran before any
    view existed yet, when the (now-removed) blocking condition was never true."""
    qapp, reg, scene, docker = graph
    _, gv, _, nodes = _make_nodes(graph)  # forces a tab (and its graph view) to exist
    (existing,) = nodes
    gv._create_view_node_for_prim(get_chunk_prim_path(existing.chunk))
    _pump(qapp, 0.3)
    assert gv._context_has_view(scene, gv._graph_context())

    scene_path = tmp_path / "bowl_apple.usda"
    friend_stage = Usd.Stage.CreateNew(str(scene_path))
    friend_stage.DefinePrim("/geo", "Xform")
    friend_stage.GetRootLayer().Save()

    gv._import_as_sublayer(str(scene_path), QtCore.QPointF(11000, 11000))
    _pump(qapp, 0.3)

    sublayer_chunks = [c for c in scene.get_chunks() if c.get_config() and c.get_config().module == "sublayer"]
    assert len(sublayer_chunks) == 1
    sub = sublayer_chunks[0]
    row = sub.get_param("imports").get_indexed_param("sublayer_path", 0)
    assert row.value == str(scene_path)

    edit_layer = sub.get_edit_context()
    assert edit_layer is not None
    assert list(edit_layer.subLayerPaths) == [str(scene_path)]

    # Regression: reported live separately -- a dropped, deliberately-unconnected Sublayer's
    # content still rendered in the viewport. Its subLayerPaths write persists once authored
    # (unlike a reserved-execute()-driven chunk's content, which just never gets authored at
    # all while blocked), so hiding it needs an actual mute -- which only happens as a side
    # effect of a real recook_for_view pass. _import_as_sublayer must force one when a view
    # already exists, since this new node is never going to trigger one on its own (no wire
    # event ever touches it).
    assert sub.is_blocked()
    assert gv.scene()._stage.IsLayerMuted(edit_layer.identifier)


def test_handle_dropped_file_prompts_for_plain_scene_without_a_chunks_option(graph, tmp_path, monkeypatch) -> None:
    """A plain scene (no chunk prims) used to skip the popup entirely and silently import as
    Sublayer -- reasonable when Sublayer was the only real choice, but it would make
    "Import as Reference" unreachable for the single most common drop (a plain USD asset)
    now that it's an option. The popup shows either way; only "Import as Chunks" (which
    needs actual chunk prims to mean anything) stays conditional on the file having some."""
    qapp, reg, scene, docker = graph
    _, gv, _, _ = _make_nodes(graph)  # forces a tab (and its graph view) to exist

    seen_menus: list = []

    def _record_and_pick(text):
        def _exec(menu, _pos):
            seen_menus.append([act.text() for act in menu.actions()])
            for act in menu.actions():
                if act.text() == text:
                    return act
            return None
        return _exec
    monkeypatch.setattr(gv, "_exec_menu", _record_and_pick("Import as Sublayer"))

    scene_path = tmp_path / "prompted_plain_scene.usda"
    plain_stage = Usd.Stage.CreateNew(str(scene_path))
    plain_stage.DefinePrim("/geo", "Xform")
    plain_stage.GetRootLayer().Save()

    gv._handle_dropped_file(str(scene_path), QtCore.QPointF(11000, 11000), QtCore.QPoint(0, 0))
    _pump(qapp, 0.3)

    assert seen_menus, "a plain scene must still prompt now that there's a real choice to make"
    assert "Import as Chunks" not in seen_menus[0], "no chunk prims means this option would be a dead end"
    assert "Import as Reference" in seen_menus[0]

    sublayer_chunks = [c for c in scene.get_chunks() if c.get_config() and c.get_config().module == "sublayer"]
    assert any(
        c.get_param("imports").get_indexed_param("sublayer_path", 0).value == str(scene_path)
        for c in sublayer_chunks
    )


def test_import_as_reference_creates_prefilled_node(graph, tmp_path) -> None:
    qapp, reg, scene, docker = graph
    _, gv, _, _ = _make_nodes(graph)  # forces a tab (and its graph view) to exist

    scene_path = tmp_path / "friend_asset.usda"
    friend_stage = Usd.Stage.CreateNew(str(scene_path))
    friend_stage.DefinePrim("/geo", "Xform")
    friend_stage.GetRootLayer().Save()

    gv._import_as_reference(str(scene_path), QtCore.QPointF(20000, 20000))
    _pump(qapp, 0.3)

    ref_chunks = [c for c in scene.get_chunks() if c.get_config() and c.get_config().module == "reference"]
    assert len(ref_chunks) == 1
    ref = ref_chunks[0]
    assert ref.get_param("file_path").value == str(scene_path)

    # file_changed (reference.py's own param callback) derives this from the asset's name.
    prim_path = ref.get_param("primpath_location").value
    assert prim_path

    prim = gv.scene()._stage.GetPrimAtPath(prim_path)
    assert prim.IsValid()
    assert prim.HasAuthoredReferences()


def test_import_as_reference_authors_even_with_an_active_view(graph, tmp_path) -> None:
    """Mirrors test_import_as_sublayer_writes_sublayer_paths_even_with_an_active_view:
    Reference's real work runs on the reserved execute() hook, which a blocked chunk skips
    entirely -- so if _import_as_reference blocked the chunk before setting file_path (the
    way _materialize_at does for Create Layer), the reference would just never get
    authored at all. It must stay unblocked while setting the value, then only get
    blocked+muted afterward, as a side effect of the view recook that hides it for being
    unconnected."""
    qapp, reg, scene, docker = graph
    _, gv, _, nodes = _make_nodes(graph)  # forces a tab (and its graph view) to exist
    (existing,) = nodes
    gv._create_view_node_for_prim(get_chunk_prim_path(existing.chunk))
    _pump(qapp, 0.3)
    assert gv._context_has_view(scene, gv._graph_context())

    scene_path = tmp_path / "friend_asset2.usda"
    friend_stage = Usd.Stage.CreateNew(str(scene_path))
    friend_stage.DefinePrim("/geo", "Xform")
    friend_stage.GetRootLayer().Save()

    gv._import_as_reference(str(scene_path), QtCore.QPointF(21000, 21000))
    _pump(qapp, 0.3)

    ref_chunks = [c for c in scene.get_chunks() if c.get_config() and c.get_config().module == "reference"]
    assert len(ref_chunks) == 1
    ref = ref_chunks[0]
    prim_path = ref.get_param("primpath_location").value
    assert prim_path

    edit_layer = ref.get_edit_context()
    assert edit_layer is not None
    assert edit_layer.GetPrimAtPath(prim_path) is not None, "execute() must have run before the mute"

    # Regression-shaped guard: a dropped, deliberately-unconnected Reference must not render
    # in the viewport either, same as the equivalent Sublayer case.
    assert ref.is_blocked()
    assert gv.scene()._stage.IsLayerMuted(edit_layer.identifier)


def test_import_view_targets_imported_tail_not_unrelated_existing_node(graph) -> None:
    """Regression: reported live -- dropping a chunkgroup file created a view wired to an
    unrelated pre-existing node (e.g. Edit Target) instead of the imported chain's own
    terminal node. Root cause: build()'s generic _ensure_default_view scans every
    disconnected exec tail across the WHOLE context -- a standalone pre-existing node with no
    downstream wiring is just as much a 'tail' as the freshly imported chain's real end, and
    it picks whichever comes first. _ensure_view_for_imported must scope to just the
    imported set."""
    qapp, reg, scene, docker = graph
    mod = reg.get_chunk_module("transform")

    # A pre-existing, unrelated, unwired node already in the tab -- stands in for the
    # "Edit Target" node from the live report.
    existing = _mk(mod, scene)
    gv = docker.tab_widget.get_current_graph_view()
    existing_path = get_chunk_prim_path(existing)
    end = time.monotonic() + 2.0
    while time.monotonic() < end and existing_path not in gv.scene()._nodes:
        qapp.processEvents()
    assert not gv._context_has_view(scene, gv._graph_context())

    # The "imported" chain: b -> c, wired, c is its own tail (and positioned to its right,
    # matching auto-layout's left-to-right topological-depth convention).
    b = _mk(mod, scene)
    c = _mk(mod, scene)
    b_path = get_chunk_prim_path(b)
    c_path = get_chunk_prim_path(c)
    end = time.monotonic() + 2.0
    while time.monotonic() < end and (b_path not in gv.scene()._nodes or c_path not in gv.scene()._nodes):
        qapp.processEvents()
    b_node = gv.scene()._nodes[b_path]
    c_node = gv.scene()._nodes[c_path]
    b_in, b_out = _exec_ports(b_node)
    c_in, c_out = _exec_ports(c_node)
    gv._author_connection(b_out, c_in)
    gv.scene()._build_edges()
    c_node.setPos(b_node.pos().x() + 300, b_node.pos().y())

    gv._ensure_view_for_imported([b, c])
    _pump(qapp, 0.3)

    assert gv._context_has_view(scene, gv._graph_context())
    assert gv._suppress_default_view is False

    stage = gv.scene()._stage
    views = [ch for ch in scene.get_chunks() if chunk_stage.is_view_chunk(ch)]
    assert len(views) == 1
    view_path = get_chunk_prim_path(views[0])
    exec_in = stage.GetAttributeAtPath(Sdf.Path(f"{view_path}.inputs:chunk"))
    assert exec_in and exec_in.IsValid()
    conns = [str(p.GetPrimPath()) for p in exec_in.GetConnections()]
    assert conns == [c_path]
    assert existing_path not in conns


def test_import_as_chunks_suppresses_generic_default_view_race(graph, tmp_path) -> None:
    """The generic build()->_ensure_default_view (an 80ms timer armed on every rebuild
    triggered by scene.add_chunk during materialize) must not win the race against
    _import_as_chunks's own 150ms-deferred, correctly-scoped view creation and grab an
    unrelated pre-existing node as the view target."""
    qapp, reg, scene, docker = graph
    mod = reg.get_chunk_module("transform")
    existing = _mk(mod, scene)
    gv = docker.tab_widget.get_current_graph_view()
    existing_path = get_chunk_prim_path(existing)
    end = time.monotonic() + 2.0
    while time.monotonic() < end and existing_path not in gv.scene()._nodes:
        qapp.processEvents()

    b = _mk(mod, scene)
    c = _mk(mod, scene)
    b_path = get_chunk_prim_path(b)
    c_path = get_chunk_prim_path(c)
    end = time.monotonic() + 2.0
    while time.monotonic() < end and (b_path not in gv.scene()._nodes or c_path not in gv.scene()._nodes):
        qapp.processEvents()
    b_node = gv.scene()._nodes[b_path]
    c_node = gv.scene()._nodes[c_path]
    b_in, b_out = _exec_ports(b_node)
    c_in, c_out = _exec_ports(c_node)
    gv._author_connection(b_out, c_in)
    gv.scene()._build_edges()

    path = tmp_path / "chain_bc.usda"
    _export_selected(scene, [b, c], path)  # only the wired chain -- not the pre-existing node

    gv._import_as_chunks(str(path), QtCore.QPointF(13000, 13000))
    _pump(qapp, 0.5)  # let both the 80ms generic timer and the 150ms scoped one fire

    assert gv._context_has_view(scene, gv._graph_context())
    stage = gv.scene()._stage
    views = [ch for ch in scene.get_chunks() if chunk_stage.is_view_chunk(ch)]
    assert len(views) == 1
    view_path = get_chunk_prim_path(views[0])
    exec_in = stage.GetAttributeAtPath(Sdf.Path(f"{view_path}.inputs:chunk"))
    conns = [str(p.GetPrimPath()) for p in exec_in.GetConnections()] if exec_in and exec_in.IsValid() else []
    assert existing_path not in conns


def test_handle_dropped_file_prompts_for_chunkgroup_file(graph, tmp_path, monkeypatch) -> None:
    scene, gv, chunks, nodes = _make_nodes(graph)
    (a,) = nodes
    a.chunk.get_param("tx").value = 42.0

    path = tmp_path / "prompted_chunks.usda"
    chunk_stage.export_chunks(scene, str(path))

    monkeypatch.setattr(gv, "_exec_menu", _pick_action_by_text("Import as Chunks"))
    before = len(scene.get_chunks())
    gv._handle_dropped_file(str(path), QtCore.QPointF(12000, 12000), QtCore.QPoint(0, 0))
    after = len(scene.get_chunks())
    assert after > before


_SCRIPT = (
    "stage.DefinePrim('/from_file_{tag}', 'Xform')\n"
    "result = '{tag}'\n"
)


def _script_runners(scene):
    return [c for c in scene.get_chunks() if c.get_config() and c.get_config().module == "script_runner"]


def _write_script(path, tag, bump_ns=0):
    path.write_text(_SCRIPT.format(tag=tag), encoding="utf-8")
    if bump_ns:
        # Two writes inside one timestamp tick would look like no save at all.
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + bump_ns))


def _drop_script(graph, tmp_path, name, pos):
    qapp, _reg, scene, _docker = graph
    _, gv, _, _ = _make_nodes(graph)  # forces a tab (and its graph view) to exist
    script = tmp_path / name
    _write_script(script, "v1")
    gv._handle_dropped_file(str(script), QtCore.QPointF(pos, pos), QtCore.QPoint(0, 0))
    _pump(qapp, 0.3)
    (runner,) = _script_runners(scene)
    return gv, script, runner


def test_dropping_a_python_file_creates_a_script_runner_running_it(graph, tmp_path) -> None:
    gv, script, runner = _drop_script(graph, tmp_path, "tool.py", 22000)

    assert runner.get_param("script_file").value == str(script)
    assert runner.get_param("code").has_state(ChunkFlags.State.HIDDEN), "the file replaces the code box"
    assert gv.scene()._stage.GetPrimAtPath("/from_file_v1").IsValid()
    assert runner.get_param("result").value == "v1"


def test_saving_the_script_file_runs_it_again(graph, tmp_path) -> None:
    qapp = graph[0]
    gv, script, runner = _drop_script(graph, tmp_path, "tool_live.py", 23000)
    assert runner.get_param("result").value == "v1"

    _write_script(script, "v2", bump_ns=2_000_000_000)
    _pump(qapp, 1.5)

    assert runner.get_param("result").value == "v2"
    stage = gv.scene()._stage
    assert stage.GetPrimAtPath("/from_file_v2").IsValid()
    assert not stage.GetPrimAtPath("/from_file_v1").IsValid(), "the rerun replaces the old output"


def test_a_script_file_is_not_rerun_after_its_node_is_deleted(graph, tmp_path) -> None:
    qapp = graph[0]
    gv, script, runner = _drop_script(graph, tmp_path, "tool_gone.py", 24000)
    runner.request_delete()
    _pump(qapp, 0.3)

    _write_script(script, "v2", bump_ns=2_000_000_000)
    _pump(qapp, 1.5)

    assert not gv.scene()._stage.GetPrimAtPath("/from_file_v2").IsValid()
