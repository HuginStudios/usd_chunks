"""The Blender frontend does what the contract says.

A fourth toolkit, and the first that draws *nothing*: Blender owns the node editor, so this
frontend supplies the sixteen verbs and lets `bpy.types.NodeTree` render them. If the
engine were quietly shaped around a canvas -- positions in view space, a frontend-owned
selection, a private clipboard -- this is where that would show.

Skipped wholesale outside Blender. `bpy` is not a normal dependency: it ships Blender
itself, and it is the *only* way to exercise this file at all. Inside Blender it is already
there.

One thing these tests deliberately assert, because it is the design and not an accident:
usd_chunks runs against **Blender's own USD**. Blender compiles its build under a private
namespace (`pxrBlender_v26_03__pxrReserved__`), so a pip `usd-core` installed beside it is a
different C++ type and cannot exchange objects with it -- which is why `usd-core` is an
optional extra rather than a dependency.
"""
from __future__ import annotations

import sys
import threading

import pytest

# Whether bpy is *already loaded*, not whether it can be imported. Those differ on any
# machine with the pip `bpy` wheel installed beside a standalone USD, and the difference is
# expensive: importing that wheel loads Blender's own USD libraries, which claim the DLLs
# `pxr` needs, so every later `from pxr import ...` in the process dies with "DLL load
# failed while importing _tf". pytest collects this file early (it sorts under "b"), so the
# one import here took the rest of the suite down with it -- measured on Blender 5.2.1:
# 1736 tests collected became 311 collected and 128 collection errors, none of them real.
#
# Inside Blender -- the only place these tests can actually run -- bpy is native and already
# in sys.modules before any script runs, so this admits exactly the case it means to and
# never performs the import that does the damage.
if "bpy" not in sys.modules:  # pragma: no cover - the usual case outside Blender
    pytest.skip("bpy only exists inside Blender", allow_module_level=True)

import bpy  # noqa: E402

from mathutils import Matrix, Vector  # noqa: E402
from pxr import Gf, Kind, Sdf, Usd, UsdGeom  # noqa: E402

from graph_fixtures import chunk_prims, make_network  # noqa: E402
from usd_chunks import chunk, chunk_registry  # noqa: E402
from usd_chunks.chunk_core import ChunkConfig  # noqa: E402
from usd_chunks.chunk_flags import ChunkFlags  # noqa: E402
from usd_chunks.chunk_scene import SceneSender  # noqa: E402
from usd_chunks.frontends.blender import factory as chunk_blender  # noqa: E402
from usd_chunks.graph import engine as chunk_graph  # noqa: E402
from usd_chunks import chunk_stage  # noqa: E402
from usd_chunks.frontends.blender import addon  # noqa: E402
from usd_chunks.frontends.blender import nodegraph as bg  # noqa: E402


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []

    def get_host(self):
        # These run inside Blender, so Blender-only modules (hosts=["blender>=5.1"]) load.
        return "blender", ".".join(str(part) for part in bpy.app.version)


def _make(module):
    """One chunk from a registry module, the way the frontends make them."""
    chunks = module.create() if module.loaded else module.load()
    chunk = chunks[0]
    chunk._config = ChunkConfig(module=module.module_name)
    return chunk


@pytest.fixture
def view():
    """A two-chunk network -- Reference feeding Transform -- mirrored into a node tree."""
    stage = Usd.Stage.CreateInMemory()
    scene = _Scene(stage)
    registry = chunk_registry.ChunkRegistry()
    registry.initialize(scene, "pytest-blender")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_stage.ChunkStageSync().initialize(stage, scene)
    bg.register()

    # Through the registry, which is what puts chunks/ on sys.path. A bare
    # `import reference` here only worked when some earlier test file had already
    # initialised a registry, so this file errored out when run on its own.
    ref = _make(registry.get_chunk_module("reference"))
    ref.name = "Ref"
    scene.add_chunk(ref)
    move = _make(registry.get_chunk_module("transform"))
    move.name = "Move"
    scene.add_chunk(move)
    move.add_dependency(ref)
    move.get_param("tz").value = 3.0

    # add_chunk routes a context-less chunk into the scene's default chunkgroup, so the
    # graph to read is that group -- not CHUNKS_ROOT, where only the group itself lives.
    context = chunk_stage.get_chunk_prim_path(scene.peek_root_context())
    tree = bpy.data.node_groups.new("test", bg.TREE_ID)
    v = bg.BlenderGraphView(stage, scene, tree=tree, context=context)
    v.chunks = {chunk_stage.get_chunk_prim_path(c): c for c in scene.get_chunks()}
    v.reload()
    chunk_graph.node_clipboard.clear()
    chunk_graph.edge_clipboard.clear()
    return v


def _node(view, label):
    return next(n for n in view.tree.nodes if n.label == label)


def _chunk_nodes(view):
    """The nodes that are chunks the fixture made -- not the Output that reload() wires to
    the end of the chain so the context cooks (see BlenderGraphView.ensure_view), nor a
    view a test made."""
    skip = {bg.node_class_id("view"), bg.node_class_id("output")}
    return [n for n in view.tree.nodes if n.bl_idname not in skip]


def _viewed(view, label="Move"):
    """Press V on `label`: a View wired to it, made the active one. Returns its path.

    A new network gets an Output rather than a view, so a test about views makes one.
    """
    view.set_selection({_node(view, label).name})
    view.create_view_node()
    return next(n.name for n in view.tree.nodes
                if n.bl_idname == bg.node_class_id("view")
                and [str(c.GetPrimPath()) for c in view.prims[n.name].GetAttribute(
                    "inputs:chunk").GetConnections()] == [_node(view, label).name])


def _flush(view):
    """What the one-tick timer does in Blender proper; headless there is no event loop."""
    view.flush()


# ---------------------------------------------------------------------------
# the tree is a view of USD
# ---------------------------------------------------------------------------

def test_the_tree_is_rebuilt_from_usd(view):
    labels = sorted(n.label for n in _chunk_nodes(view))
    assert labels == ["Move", "Ref"]


def test_opening_gives_the_context_an_output(view):
    """A context with neither an Output nor a view cooks nothing, so reload wires an Output
    to the chain's tail and cooks to it (slot 0) -- as every other frontend does on open."""
    outputs = [n for n in view.tree.nodes if n.bl_idname == bg.node_class_id("output")]
    assert len(outputs) == 1
    src = view.prims[outputs[0].name].GetAttribute("inputs:chunk").GetConnections()
    assert [str(c.GetPrimPath()) for c in src] == [_node(view, "Move").name]
    assert view.active_slot() == 0


def test_each_node_gets_its_chunk_type(view):
    assert _node(view, "Move").bl_idname == bg.node_class_id("transform")


def test_the_exec_wire_becomes_a_link(view):
    links = [(link.from_node.label, link.to_node.label, link.to_socket.name)
             for link in view.tree.links]
    assert ("Ref", "Move", "chunk") in links


def test_ports_are_named_for_their_usd_attributes(view):
    move = _node(view, "Move")
    assert "chunk" in move.inputs, "the exec input must exist"
    assert "tz" in move.inputs, "a connectable param must have a port"


# ---------------------------------------------------------------------------
# params bind both ways
# ---------------------------------------------------------------------------

def test_a_param_value_reaches_the_node(view):
    assert _node(view, "Move").tz == pytest.approx(3.0)


def test_editing_the_node_writes_the_param(view):
    """The `update=` callback is the whole point of generating RNA properties rather than
    using ID properties for everything -- without it a node body would be read-only."""
    _node(view, "Move").tz = 9.0
    assert view.chunks[_node(view, "Move").chunk_path].get_param("tz").value == pytest.approx(9.0)


def test_a_float_slider_is_not_an_int_property(view):
    """A Slider declares INT *and* FLOAT (161 = INT|FLOAT|1) because one class serves both,
    and `is_int` is what decides. Testing the flags in the wrong order made every float
    slider an IntProperty, which Blender then refused a float for -- and the symptom was a
    param silently failing to mirror, not an error."""
    _node(view, "Move").tz = 2.5
    assert _node(view, "Move").tz == pytest.approx(2.5)


def test_params_nested_in_a_vector_are_reached(view):
    """Transform keeps tx/ty/tz inside a Vector. Skipping containers rather than recursing
    produced a node with a port for tz and no control for it."""
    names = [p.name for p in chunk_blender.iter_drawable(view.chunks[_node(view, "Move").chunk_path])]
    assert {"tx", "ty", "tz"} <= set(names)


# ---------------------------------------------------------------------------
# the contract verbs
# ---------------------------------------------------------------------------

def test_selection_mirrors_onto_the_chunks(view):
    """The shared operations read the SELECTED bit, not a frontend attribute, so a
    selection kept privately would be invisible to them."""
    move, ref = _node(view, "Move"), _node(view, "Ref")
    view.set_selection({move.name})
    assert [c.name for c in view.selected_chunks()] == ["Move"]
    assert view.chunks[move.chunk_path].has_state(ChunkFlags.State.SELECTED)
    # The negative half, which is where a misspelled unset_state hid: the bit was set and
    # never cleared, so the shared operations saw a selection that only ever grew.
    view.set_selection({ref.name})
    assert not view.chunks[move.chunk_path].has_state(ChunkFlags.State.SELECTED)


def test_selection_mirrors_onto_the_blender_nodes(view):
    view.set_selection({_node(view, "Move").name})
    assert _node(view, "Move").select
    assert not _node(view, "Ref").select


def test_disable_greys_rather_than_hides(view):
    """A disabled chunk stays on the graph. Blender's own mute flag says so, rather than a
    second convention a Blender user would have to learn."""
    view.set_selection({_node(view, "Move").name})
    view.toggle_disable_selected()
    assert _node(view, "Move").mute
    # Toggling back matters as much as toggling: only the disable direction was exercised,
    # so the un-disable branch raised AttributeError for as long as it existed.
    view.toggle_disable_selected()
    assert not _node(view, "Move").mute


def test_copy_uses_the_shared_clipboard(view):
    """Copied here, pasteable in Qt or Flet: the engine's own lists, not a private format."""
    view.set_selection({_node(view, "Move").name})
    view.copy_selection()
    assert [n.module_name for n in chunk_graph.node_clipboard] == ["transform"]


def test_delete_removes_the_chunk(view):
    view.set_selection({_node(view, "Move").name})
    view.delete_selected()
    assert [n.label for n in _chunk_nodes(view)] == ["Ref"]


def test_dive_refuses_a_plain_chunk(view):
    """Keyed on is_chunkgroup, never the CONTEXT flag -- a chunk-in-chunk carries CONTEXT
    too and is an implementation detail, not a subnet."""
    assert not view.can_dive(_node(view, "Move").chunk_path)


def test_no_active_view_reads_as_zero(view):
    view.set_active_slot(0)
    assert view.active_slot() == 0


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

def test_a_node_class_is_generated_per_chunk_type():
    """Registration loads the whole library, because Blender needs a class registered
    before a node of that type can exist -- there is no lazy path."""
    assert len(bg._generated) > 10


def test_qt_only_chunks_are_skipped_not_fatal():
    """A chunk that imports PySide6 at module level cannot load in Blender (the
    custom_widget example still does). The registry skips a module whose import fails,
    so the add-on degrades rather than refusing to start."""
    assert "custom_widget" not in bg._generated


def test_uv_checker_loads_without_qt():
    """uv_checker imported Qt for one error popup; Qt is optional there now."""
    assert "uv_checker" in bg._generated


def test_a_chunk_that_only_waited_with_qt_now_loads():
    """rbd_solver imported Qt for one debounce timer; it waits through call_later now."""
    assert "rbd_solver" in bg._generated


def test_call_later_from_a_worker_runs_on_the_main_thread():
    ran = []
    worker = threading.Thread(target=lambda: addon.BlenderScene().call_later(
        lambda: ran.append(threading.get_ident())))
    worker.start()
    worker.join()
    cancel = addon.BlenderScene().call_later(lambda: ran.append("cancelled"))
    cancel()
    addon._run_due_calls()
    assert ran == [threading.get_ident()]


def test_the_material_library_loads_without_qt():
    """AMDMaterialXLibrary's thumbnail grid is Qt, but the chunk is not: without the import
    guard its nodes were missing from a loaded network, and every wire through them too."""
    assert "AMDMaterialXLibrary" in bg._generated


# ---------------------------------------------------------------------------
# the Add menu
# ---------------------------------------------------------------------------

def test_the_add_menu_lists_the_chunk_library():
    """Shift+A has to offer something, or the editor is read-only in practice."""
    assert bg.build_add_menu() > 10


def test_the_menu_is_a_tree_of_registered_blender_menus():
    """Blender resolves a submenu by id when the parent draws, so every id a parent names
    must already be a registered class -- a missing one is a silent empty row."""
    bg.build_add_menu()
    root = bg._menu_classes.get(())
    assert root is not None
    assert root._node["subs"], "the library has categories, so the menu must have submenus"
    for name in root._node["subs"]:
        child = bg._menu_classes.get((name,))
        assert child is not None, f"no menu class for the {name} category"
        assert child.is_registered
        assert child.bl_idname == bg._menu_id((name,))


def test_every_menu_index_resolves_to_an_entry():
    """The operator carries an index, not a module name, because a MenuEntry is a module
    plus its variant presets -- two entries can share a module."""
    bg.build_add_menu()
    seen = []

    def walk(node):
        for _label, index in node["items"]:
            seen.append(bg._menu_entries[index])
        for sub in node["subs"].values():
            walk(sub)

    walk(bg._menu_classes[()]._node)
    assert seen and all(hasattr(e, "create") for e in seen)


def test_adding_from_the_menu_puts_a_node_in_the_tree(view):
    """The whole path an Add click takes, minus the click: entry -> create_chunk ->
    chunk_graph.create_from_entry -> a node in the tree."""
    entry = next(e for e in bg.chunk_graph.menu_entries()
                 if e.module.module_name == "transform")
    before = len(view.tree.nodes)
    view.create_chunk(entry, 5.0, 6.0)
    assert len(view.tree.nodes) == before + 1


def test_the_editor_is_named_like_blenders_own():
    """It sits in the editor-type dropdown next to Geometry Node Editor, so it reads like
    one of them rather than like a module name."""
    assert bg.UsdChunksNodeTree.bl_label == "USDChunks Editor"


# ---------------------------------------------------------------------------
# the engine does the work
# ---------------------------------------------------------------------------

def test_grouping_goes_through_the_shared_operation(view):
    """The version that lived here drove itself through dive_into/paste_at/dive_out and
    re-wired the boundary with add_dependency -- a second implementation of what Flet and
    ImGui already shared, and the one that drifted."""
    view.set_selection({_node(view, "Move").name})
    view.group_selection()
    kinds = {n.bl_idname for n in view.tree.nodes}
    assert bg.node_class_id("chunkgroup") in kinds


def test_a_pasted_param_wire_is_not_forced_to_exec(view):
    """rewire_pasted reads the real ports off the stage. The hand-built PortRefs that stood
    here typed every wire PORT_EXEC, so can_connect rejected each copied param wire and the
    paste silently lost them."""
    view.set_selection({_node(view, "Ref").name, _node(view, "Move").name})
    view.copy_selection()
    view.paste_at(0.0, -300.0)
    labels = [n.label for n in _chunk_nodes(view)]
    assert len(labels) == 4, labels


# ---------------------------------------------------------------------------
# the tree stays honest between reloads
# ---------------------------------------------------------------------------

def _exec_input(view, label):
    prim = view.prims[_node(view, label).name]
    return prim.GetAttribute("inputs:chunk")


def _wire(view, src, dst):
    return next(link for link in view.tree.links
                if link.from_node.label == src and link.to_node.label == dst)


def test_a_cut_wire_disconnects_in_usd(view):
    """NodeTree.update reconciled links in one direction only: a link present was
    connected, a link gone was nothing -- so a wire cut in the editor kept its USD
    connection and came back on the next reload."""
    view.tree.links.remove(_wire(view, "Ref", "Move"))
    view.tree.update()
    assert _exec_input(view, "Move").GetConnections() == []
    view.reload()
    assert not any(link.from_node.label == "Ref" and link.to_node.label == "Move"
                   for link in view.tree.links), "the wire came back"


def test_a_cut_wire_does_not_heal_the_chain(view):
    """A wire the user cut is one wire. Healing would rewire the view past Move to Ref,
    which is what detaching a NODE means, not cutting a wire."""
    view_path = _viewed(view)
    before = list(view.prims[view_path].GetAttribute("inputs:chunk").GetConnections())
    view.tree.links.remove(_wire(view, "Ref", "Move"))
    view.tree.update()
    after = list(view.prims[view_path].GetAttribute("inputs:chunk").GetConnections())
    assert after == before


def test_update_with_nothing_changed_schedules_no_reload(view, monkeypatch):
    """Blender calls NodeTree.update for every node PROPERTY change, i.e. on each tick of
    a slider drag. Connecting every link on every call re-authored each wire, reported a
    change, and scheduled a reload that recreated every node (selected) mid-drag."""
    scheduled = []
    monkeypatch.setattr(bpy.app.timers, "register",
                        lambda fn, **kw: scheduled.append(fn))
    view.tree.update()
    assert scheduled == []
    # The other direction still works: a cut wire is a change. (links.remove fires
    # update() on its own as well; until the reload runs, every call sees the gap.)
    view.tree.links.remove(_wire(view, "Ref", "Move"))
    view.tree.update()
    assert scheduled


def test_a_reload_keeps_the_editors_selection(view):
    """nodes.new() hands back a selected node, so a rebuild used to leave the whole
    graph selected. The tree's selection is read first and put back."""
    for node in view.tree.nodes:
        node.select = node.label == "Move"
    view.reload()
    assert [n.label for n in view.tree.nodes if n.select] == ["Move"]
    assert [c.name for c in view.selected_chunks()] == ["Move"]


def test_diving_does_not_carry_the_outer_selection_in(view):
    """The tree still holds the outer nodes when a dive reloads; adopting their selection
    would leave outer chunks flagged SELECTED where the inner graph's X could reach them."""
    view.set_selection({_node(view, "Move").name})
    view.group_selection()
    group = next(n for n in view.tree.nodes if n.bl_idname == bg.node_class_id("chunkgroup"))
    group.select = True
    view.dive_into(group.name)
    assert view.selected_chunks() == []
    assert not any(n.select for n in view.tree.nodes)


def test_a_native_duplicate_never_aliases_the_original(view):
    """Blender's duplicate copies every RNA property, chunk_path included, so the copy
    edited and wired the ORIGINAL prim. copy() blanks it; the chunk verbs (Shift+D is
    bound to duplicate_selection) are how a real duplicate is made."""
    original = _node(view, "Move")
    dup = view.tree.nodes.new(original.bl_idname)
    dup.copy(original)
    assert dup.chunk_path == ""
    assert dup.get_chunk() is None


def test_a_param_change_reaches_the_node_without_a_reload(view):
    """sync_to_node ran only inside a rebuild, so an output a cook filled in -- or any
    value changed from the chunk side -- sat stale on the node until something else
    happened to reload the whole graph."""
    move = view.chunks[_node(view, "Move").name]
    move.get_param("tz").value = 5.0
    assert _node(view, "Move").tz == pytest.approx(3.0), "not yet: flush is one tick later"
    _flush(view)
    assert _node(view, "Move").tz == pytest.approx(5.0)


def test_a_stale_chunk_gets_the_amber_header(view):
    move = view.chunks[_node(view, "Move").name]
    assert tuple(_node(view, "Move").color) == pytest.approx(bg._NODE_HEADER_COLOR)
    move.set_state(ChunkFlags.State.STALE)
    _flush(view)
    assert tuple(_node(view, "Move").color) == pytest.approx(bg._STALE_HEADER_COLOR)
    move.unset_state(ChunkFlags.State.STALE)
    _flush(view)
    assert tuple(_node(view, "Move").color) == pytest.approx(bg._NODE_HEADER_COLOR)


def test_collapse_is_the_state_bit_shown_as_blenders_hide(view):
    """One direction for the lot, groups and views skipped; node.hide follows the bit so a
    collapse survives a reload and one made in Qt shows here."""
    # A view is created collapsed (it has no body), so Blender draws it hidden from the
    # start; what matters is that a sweep leaves it exactly as it was.
    view_path = _viewed(view)
    view_hidden = view.tree.nodes[view_path].hide
    view.set_selection(set())
    view.collapse_selected_or_all()
    for label in ("Ref", "Move"):
        assert view.chunks[_node(view, label).name].has_state(ChunkFlags.State.COLLAPSED)
        assert _node(view, label).hide
    assert view.tree.nodes[view_path].hide == view_hidden
    view.reload()
    assert _node(view, "Move").hide
    view.collapse_selected_or_all()
    assert not _node(view, "Move").hide


def test_a_chunk_deleted_behind_the_frontends_back_leaves_the_tree(view):
    """The CLI, another frontend, undo: none of them go through delete_selected. The
    CHUNK_DELETED notice schedules the rebuild instead."""
    move = view.chunks[_node(view, "Move").name]
    chunk_graph.delete_chunks([move])
    assert view._reload_pending
    _flush(view)
    assert [n.label for n in _chunk_nodes(view)] == ["Ref"]


def test_a_replaced_view_ignores_notices(view):
    """The notice connections are global. A view that another network replaced must not
    keep rebuilding a tree nobody is looking at -- id() reuse means its chunk table can
    match chunks that are not its own."""
    other = bg.BlenderGraphView(view.stage, view.scene, tree=None, context=view.context)
    assert bg.active_view() is other
    move = view.chunks[_node(view, "Move").name]
    move.get_param("tz").value = 9.0
    assert not view._dirty


# ---------------------------------------------------------------------------
# views, groups, locks: what Qt shows, shown here
# ---------------------------------------------------------------------------

def test_the_active_view_is_orange_and_an_inactive_one_dim(view):
    view_path = _viewed(view)
    assert tuple(view.tree.nodes[view_path].color) == pytest.approx(bg._VIEW_ACTIVE_COLOR)
    view.set_active_slot(0)
    assert tuple(view.tree.nodes[view_path].color) == pytest.approx(bg._VIEW_INACTIVE_COLOR)


def test_create_view_node_wires_the_selection_and_activates_it(view):
    # The path, not the node: create_view_node rebuilds the tree, and a bpy node held
    # across a rebuild is a wrapper over memory Blender reuses for a different node.
    first = _viewed(view)
    ref_path = _node(view, "Ref").name
    view.set_selection({ref_path})
    view.create_view_node()
    views = [n for n in view.tree.nodes if n.bl_idname == bg.node_class_id("view")]
    assert len(views) == 2
    made = next(n for n in views if n.name != first)
    src = view.prims[made.name].GetAttribute("inputs:chunk").GetConnections()
    assert [str(c.GetPrimPath()) for c in src] == [ref_path]
    assert view.active_slot() == 2
    assert tuple(made.color) == pytest.approx(bg._VIEW_ACTIVE_COLOR)
    assert tuple(_node(view, "View2").color) == pytest.approx(bg._VIEW_INACTIVE_COLOR)


def test_a_chunkgroup_gets_the_group_header(view):
    view.set_selection({_node(view, "Move").name})
    view.group_selection()
    group = next(n for n in view.tree.nodes if n.bl_idname == bg.node_class_id("chunkgroup"))
    assert tuple(group.color) == pytest.approx(bg._GROUP_HEADER_COLOR)


def test_locked_is_a_def_from_a_layer_that_is_not_the_edit_target(tmp_path):
    """Qt's rule, verbatim. A prim DEFINED by a sublayer is locked while the edit target
    is the root, and not once the target is that layer.

    A def, not a bare over: node collection is Usd.PrimIsDefined, so an over-only prim is
    never a node to lock in the first place, and requiring the def is what lets a chunk
    that merely has composed-in children -- a Reference hosting a network -- stay this
    graph's own node (chunk_utils.composed_in_from).
    """
    stage = Usd.Stage.CreateInMemory()
    layer = Sdf.Layer.CreateNew(str(tmp_path / "referenced.usda"))
    spec = Sdf.CreatePrimInLayer(layer, "/chunks/Ref")
    spec.specifier = Sdf.SpecifierDef
    layer.Save()
    stage.GetRootLayer().subLayerPaths.append(layer.identifier)
    prim = stage.GetPrimAtPath("/chunks/Ref")
    assert bg._is_locked(stage, prim)
    stage.SetEditTarget(Usd.EditTarget(layer))
    assert not bg._is_locked(stage, prim)


def test_a_chunk_defined_here_is_not_locked_by_what_composes_beneath_it(tmp_path):
    """The host case: its own def is local, the network's group composes in under it."""
    stage = Usd.Stage.CreateInMemory()
    layer = Sdf.Layer.CreateNew(str(tmp_path / "network.usda"))
    group = Sdf.CreatePrimInLayer(layer, "/chunks/Group")
    group.specifier = Sdf.SpecifierDef
    child = Sdf.CreatePrimInLayer(layer, "/chunks/Group/Inner")
    child.specifier = Sdf.SpecifierDef
    layer.Save()
    host = stage.DefinePrim("/chunks/Host")
    host.GetReferences().AddReference(layer.identifier, "/chunks/Group")
    assert not bg._is_locked(stage, host)
    assert bg._is_locked(stage, stage.GetPrimAtPath("/chunks/Host/Inner"))


def test_a_locked_node_is_amber_and_keeps_its_position(view):
    """Blender cannot refuse a drag, so a locked node's dragged position is dropped on
    write-back and auto-layout is told not to move it."""
    ref = _node(view, "Ref")
    view.locked = {ref.name}
    view.apply_node_state(ref, view.prims[ref.name], view.chunks[ref.name])
    assert tuple(ref.color) == pytest.approx(bg._LOCKED_HEADER_COLOR)
    before = chunk_graph.node_position(view.prims[ref.name])
    ref.location = (999.0, 999.0)
    view.pull_positions_from_tree()
    assert chunk_graph.node_position(view.prims[ref.name]) == before
    view.set_selection(set())
    view.auto_layout()
    assert chunk_graph.node_position(view.prims[ref.name]) == before


# ---------------------------------------------------------------------------
# positions: USD units, scaled for Blender's bigger nodes
# ---------------------------------------------------------------------------

def test_positions_are_scaled_in_and_unscaled_on_the_way_out(view):
    """A position in USD is a Qt scene pixel. Blender's nodes are bigger, so the tree
    shows them _SCALE apart -- and writes them back divided, so Qt sees Qt spacing."""
    ref = _node(view, "Ref")
    usd_x, usd_y = chunk_graph.node_position(view.prims[ref.name])
    assert tuple(ref.location) == pytest.approx((usd_x * bg._SCALE, usd_y * bg._SCALE))
    ref.location = (500.0, -250.0)
    view.pull_positions_from_tree()
    assert chunk_graph.node_position(view.prims[ref.name]) == pytest.approx(
        (500.0 / bg._SCALE, -250.0 / bg._SCALE))


def test_layout_uses_the_drawn_size_when_there_is_one(view):
    """node.dimensions is zero until the editor draws the node (always, headless), so the
    engine default stands in; once drawn, the real size is what the layout spaces by."""
    ref = _node(view, "Ref")
    assert view._node_size(ref.name) == chunk_graph.node_size(view.prims[ref.name])


# ---------------------------------------------------------------------------
# hidden groups hide, list rows edit
# ---------------------------------------------------------------------------

def _add(view, module_name):
    """Add one more chunk of `module_name` to the fixture scene; return its node."""
    chunk = _make(chunk_registry.ChunkRegistry().get_chunk_module(module_name))
    view.scene.add_chunk(chunk)
    view.chunks = {chunk_stage.get_chunk_prim_path(c): c for c in view.scene.get_chunks()}
    view.reload()
    return view.tree.nodes[chunk_stage.get_chunk_prim_path(chunk)]


def test_a_hidden_groups_params_are_hidden_with_it(view):
    """mesh keeps cube, tube and grid groups and shows the one its menu names. The
    params inside carry no HIDDEN bit of their own, so reading only theirs drew every
    group's fields at once and the Mesh Type menu visibly did nothing."""
    node = _add(view, "mesh")
    chunk = node.get_chunk()
    shown = [p.name for p in chunk_blender.iter_drawable(chunk)]
    assert shown == ["prim_path", "mesh_type", "cache_result"], shown  # the cook cache's toggle
    assert node.inputs["dimx1"].hide and node.inputs["dimx"].hide
    node.mesh_type = "grid"
    _flush(view)
    assert chunk.get_param("mesh_type").value == "grid"
    assert not node.inputs["dimx1"].hide, "the grid group's socket stays folded"
    assert node.inputs["dimx"].hide, "the cube group's socket unfolded"
    assert "dimx1" in [p.name for p in chunk_blender.iter_drawable(chunk)]


def test_the_write_guard_is_released_after_a_mirror(view):
    """sync_to_node takes the guard per param and must give it back: a leaked entry
    silently blocks every later edit of that param from the node."""
    _add(view, "mesh")
    assert not chunk_blender._writing


def test_list_rows_are_per_row_properties(view):
    """A ListGroup's rows share the template's param NAMES, so one RNA property per name
    reached the template and no row -- a Scatter's prototype could never be set. Rows
    are ID properties keyed list.row.param, resolved through get_indexed_param."""
    node = _add(view, "scatter_points")
    chunk = node.get_chunk()
    assert not hasattr(node, "proto_path"), "the template must not be an RNA property"
    assert "prototypes.0.proto_path" in node.keys()
    node["prototypes.0.proto_path"] = "/House"
    chunk_blender.sync_lofted(node, chunk)  # what the next draw does
    protos = chunk.get_param("prototypes")
    assert protos.get_indexed_param("proto_path", 0).value == "/House"
    assert chunk_blender.resolve_key(chunk, "prototypes.0.proto_path") is \
        protos.get_indexed_param("proto_path", 0)
    assert chunk_blender.prop_key(protos.get_indexed_param("proto_scale", 0)) == \
        "prototypes.0.proto_scale"


def test_growing_a_list_seeds_the_new_rows_properties(view):
    node = _add(view, "scatter_points")
    path = node.name
    node.get_chunk().get_param("prototypes").size = 2
    _flush(view)  # PARAM_ADDED asked for a rebuild
    node = view.tree.nodes[path]
    assert "prototypes.1.proto_path" in node.keys()


def test_a_list_rows_socket_labels_rather_than_edits(view):
    """The row's field lives in its row box with the row's other fields, not up on the
    socket where only row 0 could ever be shown."""
    node = _add(view, "scatter_points")
    socket = node.inputs["proto_path"]
    param = bg.socket_param(socket, node)
    assert param is not None
    assert not bg.socket_draws_value(socket, param)


# ---------------------------------------------------------------------------
# the add-on: keys route to the chunk verbs, undo re-derives the tree
# ---------------------------------------------------------------------------

def test_opening_a_file_through_the_addon_loads_each_chunk_once(tmp_path):
    """ChunkStageSync.initialize loads the file itself; the second load_chunks that
    followed it made a copy of every chunk (Reference -> Reference8, View1 -> View11),
    laid out in a row off to the right -- reported as "the file's positions are wrong"."""
    path, context = make_network(tmp_path / "addon.usda")
    tree = bpy.data.node_groups.new("addon", bg.TREE_ID)
    addon._open_stage(Usd.Stage.Open(path), context_path=context, tree=tree)
    view = addon.get_view()
    assert len(chunk_prims(view.stage)) == 4, chunk_prims(view.stage)
    assert len(view.tree.nodes) == 3  # two transforms and the view

def test_every_bound_key_is_a_registered_chunk_operator():
    """X, Shift+D, Ctrl+C/X/V and M have stock node operators that edit only the bpy
    tree. The add-on binds the same keys to the chunk verbs, so the model moves too."""
    registered = set(addon._CLASSES) | set(bg._CLASSES)
    bound = {op for op, _key, _mods, _props in addon._KEYS}
    assert all(op in registered for op in bound)
    idnames = {op.bl_idname for op in bound}
    for verb in ("delete", "duplicate", "copy", "cut", "paste", "toggle_disable",
                 "collapse", "group", "create_view", "auto_layout", "set_view_slot"):
        assert "usd_chunks." + verb in idnames, verb


def test_no_chunk_operator_pushes_an_undo_step():
    """Blender's undo restores the .blend and knows nothing about the stage, so an
    operator that pushed a step only ever half-undid. None of them do; undo_post
    rebuilds from USD instead."""
    for cls in addon._CLASSES + bg._CLASSES:
        assert "UNDO" not in getattr(cls, "bl_options", set()), cls.__name__


def test_undo_re_derives_the_tree_from_usd(view):
    """What Blender's undo does to the tree -- here, a node it takes away -- is not what
    the model says, and the model wins."""
    view.tree.nodes.remove(_node(view, "Move"))
    assert "Move" not in [n.label for n in view.tree.nodes]
    addon._on_undo(None)
    assert "Move" in [n.label for n in view.tree.nodes]


# ---------------------------------------------------------------------------
# hosted networks: a Reference pointed at a saved network is a subnet here too
# ---------------------------------------------------------------------------

@pytest.fixture
def hosted(view, tmp_path):
    """The fixture's own graph published to a file and hosted by a fresh Reference: the
    sequence/shot shape, built with the frontend's own verbs."""
    network = str(tmp_path / "seq.usda")
    chunk_stage.export_chunks(view.scene, network)
    registry = chunk_registry.ChunkRegistry()
    host = _make(registry.get_chunk_module("reference"))
    host.name = "Room"
    view.scene.add_chunk(host)
    host.get_param("file_path").value = network
    host.execute()
    view.chunks = {chunk_stage.get_chunk_prim_path(c): c for c in view.scene.get_chunks()}
    view.reload()
    return view, host, network


def test_a_host_is_diveable(hosted):
    view, host, _ = hosted
    path = chunk_stage.get_chunk_prim_path(host)
    assert chunk_stage.is_subnet(host)
    assert view.can_dive(path)


def test_a_host_is_not_locked_but_its_contents_are(hosted):
    """The lock rule read the prim stack across the arc, so the host -- which has the
    network's group under its own prim -- looked foreign and could not be dragged."""
    view, host, _ = hosted
    host_path = chunk_stage.get_chunk_prim_path(host)
    assert host_path not in view.locked
    view.dive_into(host_path)
    assert view.prims, "the network's chunks are the graph now"
    assert all(p in view.locked for p in view.prims
               if not chunk_stage.is_view_chunk(view.chunks[p])), view.locked


def test_a_host_wears_the_group_colour(hosted):
    view, host, _ = hosted
    node = next(n for n in view.tree.nodes
                if n.name == chunk_stage.get_chunk_prim_path(host))
    assert tuple(round(c, 3) for c in node.color) == tuple(round(c, 3) for c in bg._GROUP_HEADER_COLOR)


def test_an_inner_edit_is_an_override_the_frontend_can_see_and_revert(hosted):
    view, host, network = hosted
    host_path = chunk_stage.get_chunk_prim_path(host)
    view.dive_into(host_path)
    inner = next(c for p, c in view.chunks.items()
                 if p.startswith(host_path + "/") and c.name == "Move")
    param = inner.get_param("tz")
    assert param.value == 3.0
    assert chunk_stage.param_override_source(param) is None

    param.value = 8.0
    assert chunk_stage.param_override_source(param) == Sdf.Layer.Find(network)

    chunk_stage.revert_param_override(param)
    assert param.value == 3.0
    assert chunk_stage.param_override_source(param) is None
    assert not Sdf.Layer.Find(network).dirty


def test_localise_turns_a_host_into_a_chunkgroup(hosted):
    view, host, network = hosted
    host_path = chunk_stage.get_chunk_prim_path(host)
    group = chunk_stage.localise_network(host)
    assert group is not None and chunk_stage.is_chunkgroup(group)
    assert not chunk_stage.is_network_host(group)
    assert not view.stage.GetPrimAtPath(host_path).HasAuthoredReferences()


def test_publish_makes_a_group_host_its_own_file(view, tmp_path):
    out = str(tmp_path / "published.usda")
    group = view.scene.peek_root_context()
    chunk_stage.publish_network(group, out)
    assert view.stage.GetPrimAtPath(chunk_stage.get_chunk_prim_path(group)).HasAuthoredReferences()
    saved = Sdf.Layer.FindOrOpen(out)
    assert saved.GetAttributeAtPath(f"/chunks/{group.name}.info:chunkType").default == "chunkgroup"


def test_the_verbs_are_wired_to_operators_that_get_registered():
    """The sidebar's Publish/Localise and the revert arrow beside an overridden field are
    operator classes in the add-on's registration tuples, under the bl_idnames the panel
    and chunk_blender.draw_param name -- an operator missing from _CLASSES draws a button
    that reports "unknown operator" when clicked."""
    assert addon.USDCHUNKS_OT_publish_network in addon._CLASSES
    assert addon.USDCHUNKS_OT_localise_network in addon._CLASSES
    assert bg.USDCHUNKS_OT_revert_override in bg._CLASSES
    assert bg.USDCHUNKS_OT_revert_override.bl_idname == "usd_chunks.revert_override"
    assert addon.USDCHUNKS_OT_publish_network.bl_idname == "usd_chunks.publish_network"
    assert addon.USDCHUNKS_OT_localise_network.bl_idname == "usd_chunks.localise_network"


# ---------------------------------------------------------------------------
# viewport handles: the host half of docs/gizmo-host-contract.md
# ---------------------------------------------------------------------------

from usd_chunks.gizmo import runtime as chunk_gizmo  # noqa: E402
from usd_chunks.frontends.blender.addon import gizmos as gz  # noqa: E402
from usd_chunks.frontends.blender.addon import viewport as vp  # noqa: E402


@pytest.fixture
def driven(view):
    """A Cube placed by a Prim chunk and moved by a Transform chunk -- the shape a
    transform handle is declared on."""
    registry = chunk_registry.ChunkRegistry()
    maker = _make(registry.get_chunk_module("prim"))
    maker.name = "Maker"
    view.scene.add_chunk(maker)
    maker.get_param("prim_type").value = "Cube"
    maker.execute()
    target = maker.get_param("primpath_location").value
    xf = _make(registry.get_chunk_module("transform"))
    xf.name = "Place"
    view.scene.add_chunk(xf)
    xf.add_dependency(maker)
    xf.get_param("primpath_location").value = target
    xf.execute()
    view.chunks = {chunk_stage.get_chunk_prim_path(c): c for c in view.scene.get_chunks()}
    return view, xf, view.stage.GetPrimAtPath(target)


def _desc(prim):
    return next(d for d in chunk_gizmo.resolve_gizmos(prim) if d.type == "transform")


def test_a_driven_prim_offers_a_transform_handle(driven):
    view, xf, prim = driven
    descs = gz._visible_descs(prim, view)
    assert [d.type for d in descs] == ["transform"]
    assert str(descs[0].chunk_prim) == chunk_stage.get_chunk_prim_path(xf)


def test_handles_are_filtered_to_one_chunk(driven):
    """Two transform chunks on one prim must not both be driven by one gesture."""
    view, xf, prim = driven
    second = _make(chunk_registry.ChunkRegistry().get_chunk_module("transform"))
    second.name = "Place2"
    view.scene.add_chunk(second)
    second.add_dependency(xf)
    second.get_param("primpath_location").value = str(prim.GetPath())
    second.execute()
    assert len(chunk_gizmo.resolve_gizmos(prim)) > 1
    assert len({d.chunk_prim for d in gz._visible_descs(prim, view)}) == 1


def _components(translate=(0.0, 0.0, 0.0), rotate=(0.0, 0.0, 0.0), scale=(1.0, 1.0, 1.0)):
    return {"translate": list(translate), "rotate": list(rotate), "scale": list(scale)}


def test_a_gesture_writes_the_params_and_moves_the_prim(driven):
    view, xf, prim = driven
    desc = gz.placement_desc(prim, view)
    gz.write_components(desc, xf, _components(translate=(0.0, 4.0, 0.0)))
    assert xf.get_param("ty").value == 4.0
    moved = UsdGeom.Xformable(prim).GetLocalTransformation().ExtractTranslation()
    assert (moved[0], moved[1], moved[2]) == (0.0, 4.0, 0.0)


def test_a_rotate_gesture_leaves_translate_alone(driven):
    """All three components go back together, so the two a gesture is not touching are the
    ones the chunk already had rather than zero."""
    view, xf, prim = driven
    desc = gz.placement_desc(prim, view)
    gz.write_components(desc, xf, _components(translate=(3.0, 0.0, 0.0)))
    start = {k: gz._current(xf, k) for k in ("translate", "rotate", "scale")}
    start["rotate"] = gz.rotated(start["rotate"], "Z", 1.5707963)
    gz.write_components(desc, xf, start)
    assert xf.get_param("tx").value == pytest.approx(3.0)
    assert xf.get_param("rz").value == pytest.approx(90.0)
    assert xf.get_param("sx").value == pytest.approx(1.0)


def test_the_pivot_sits_where_the_prim_is(driven):
    view, xf, prim = driven
    desc = gz.placement_desc(prim, view)
    gz.write_components(desc, xf, _components(translate=(7.0, 0.0, 0.0)))
    assert tuple(gz.anchor_world(view.stage, desc)) == pytest.approx((7.0, 0.0, 0.0))


def test_an_axis_constraint_keeps_only_that_axis():
    """G then X: the drag still moves the mouse in two dimensions, and only one of them
    may reach the prim."""
    delta = Vector((3.0, 5.0, -2.0))
    assert tuple(gz.constrain(delta, "X", Matrix())) == pytest.approx((3.0, 0.0, 0.0))
    assert tuple(gz.constrain(delta, "Z", Matrix())) == pytest.approx((0.0, 0.0, -2.0))
    assert tuple(gz.constrain(delta, None, Matrix())) == pytest.approx(tuple(delta))


def test_a_constraint_follows_the_handles_own_axes():
    """A local-space handle constrains along the anchor's axes, not the world's -- so X on
    a prim turned 90 degrees about Z takes the world-Y part of the drag."""
    basis = Matrix.Rotation(1.5707963, 4, "Z")
    kept = gz.constrain(Vector((0.0, 4.0, 0.0)), "X", basis)
    assert tuple(kept) == pytest.approx((0.0, 4.0, 0.0), abs=1e-6)
    dropped = gz.constrain(Vector((4.0, 0.0, 0.0)), "X", basis)
    assert tuple(dropped) == pytest.approx((0.0, 0.0, 0.0), abs=1e-6)


def test_the_swept_angle_is_measured_about_the_pivot():
    centre = Vector((100.0, 100.0))
    quarter = gz.screen_angle(centre, Vector((110.0, 100.0)), Vector((100.0, 110.0)))
    assert quarter == pytest.approx(1.5707963, abs=1e-5)
    assert gz.screen_angle(centre, centre, Vector((110.0, 100.0))) == 0.0


def test_scale_is_the_ratio_of_distances_from_the_pivot():
    centre = Vector((0.0, 0.0))
    assert gz.screen_scale(centre, Vector((10.0, 0.0)), Vector((20.0, 0.0))) == pytest.approx(2.0)
    assert gz.screen_scale(centre, centre, Vector((5.0, 0.0))) == 1.0


def test_a_constrained_turn_only_moves_its_own_channel():
    out = gz.rotated([10.0, 20.0, 30.0], "Y", 1.5707963)
    assert out[0] == pytest.approx(10.0)
    assert out[1] == pytest.approx(110.0)
    assert out[2] == pytest.approx(30.0)


def test_an_unconstrained_turn_goes_through_decompose():
    """About the view axis, so it cannot be a channel add -- and the Euler convention stays
    in chunk_gizmo rather than being re-derived here."""
    out = gz.rotated([0.0, 0.0, 0.0], None, 1.5707963, view_axis=(0.0, 0.0, 1.0))
    assert out[2] == pytest.approx(90.0, abs=1e-4)
    assert out[0] == pytest.approx(0.0, abs=1e-4)


def test_scale_constrained_and_free():
    assert gz.scaled([1.0, 2.0, 3.0], None, 2.0) == pytest.approx([2.0, 4.0, 6.0])
    assert gz.scaled([1.0, 2.0, 3.0], "Y", 0.5) == pytest.approx([1.0, 1.0, 3.0])


def test_an_undriven_prim_gets_a_chunk_rather_than_a_direct_write(view):
    """Contract section 8: writing the prim would be erased by the next cook, so a gesture
    on an unowned prim makes the chunk that owns it."""
    loose = view.stage.DefinePrim("/Loose", "Cube")
    assert gz._visible_descs(loose, view) == []
    desc = chunk_gizmo.ensure_transform_chunk(loose)
    assert desc is not None
    chunk_gizmo.apply_bindings(desc.chunk_prim, desc.name,
                               Gf.Matrix4d().SetTranslate(Gf.Vec3d(1, 2, 3)))
    moved = UsdGeom.Xformable(
        view.stage.GetPrimAtPath("/Loose")).GetLocalTransformation().ExtractTranslation()
    assert (moved[0], moved[1], moved[2]) == (1.0, 2.0, 3.0)


def test_a_chunk_made_for_an_undriven_prim_is_found_at_once(view):
    """G on a prim nothing drives makes a Transform chunk, then moves it. The view's table
    only learns about the new chunk on its next reload, so looking there alone found
    nothing and the gesture cancelled."""
    loose = view.stage.DefinePrim("/Loose", "Cube")
    desc = chunk_gizmo.ensure_transform_chunk(loose)
    assert str(desc.chunk_prim) not in view.chunks
    found = gz._chunk_for(view, desc)
    assert found is not None
    assert chunk_stage.get_chunk_prim_path(found) == str(desc.chunk_prim)


def test_the_operator_registers_and_takes_the_transform_keys():
    assert gz.USDCHUNKS_OT_transform in gz._CLASSES
    assert {key for _op, key, _props in gz._KEYS} == {"G", "R", "S"}
    assert {props["mode"] for _op, _key, props in gz._KEYS} == {"translate", "rotate", "scale"}
    # is_registered, not a bpy.types lookup: Blender addresses an operator by bl_idname
    # and never publishes it on bpy.types under its class name, so a guard that asks
    # bpy.types registers twice and never unregisters.
    gz.register()
    try:
        assert gz.USDCHUNKS_OT_transform.is_registered
        assert hasattr(bpy.ops.usd_chunks, "transform")
        gz.register()  # idempotent: re-enabling the add-on must not raise
    finally:
        gz.unregister()
    assert not gz.USDCHUNKS_OT_transform.is_registered


def test_the_transform_keys_live_in_the_object_mode_keymap():
    """Blender checks "Object Mode" before "3D View", and its own G/R/S there took every
    press: bound in "3D View", G in the viewport never reached this operator."""
    assert gz._KEYMAP == ("Object Mode", "EMPTY")


def test_the_pivot_dot_follows_the_driven_anchor(driven):
    view, xf, prim = driven
    desc = gz.placement_desc(prim, view)
    gz.write_components(desc, xf, _components(translate=(2.0, 0.0, 5.0)))
    bg.set_picked_prim_paths([str(prim.GetPath())])
    try:
        assert tuple(gz.pivot_point(view.stage, view)) == pytest.approx((2.0, 0.0, 5.0))
    finally:
        bg.set_picked_prim_paths([])


def test_an_undriven_pick_still_gets_a_dot(view):
    """The only sign the click landed, and a gesture there makes the chunk."""
    view.stage.DefinePrim("/Loose", "Cube")
    bg.set_picked_prim_paths(["/Loose"])
    try:
        assert gz.pivot_point(view.stage, view) is not None
    finally:
        bg.set_picked_prim_paths([])


@pytest.fixture
def cup(view):
    """Kitchen_set's shape: the prim has its own transform beneath the chunk's op, and the
    geometry sits well away from the prim's origin."""
    cup = UsdGeom.Xform.Define(view.stage, "/Cup")
    cup.AddTranslateOp().Set(Gf.Vec3d(10, 0, 0))
    body = UsdGeom.Cube.Define(view.stage, "/Cup/Body")
    body.AddTranslateOp().Set(Gf.Vec3d(0, 4, 0))
    desc = chunk_gizmo.ensure_transform_chunk(cup.GetPrim())
    return view, desc, cup.GetPrim()


def _world(prim):
    return UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(0)


def test_the_pivot_sits_on_the_geometry_not_the_origin(cup):
    view, desc, prim = cup
    assert tuple(gz.anchor_world(view.stage, desc)) == pytest.approx((10.0, 4.0, 0.0))


def test_a_grab_keeps_what_is_beneath_the_chunk(cup):
    """The chunk divides its upstream out of what it is given, so a grab that handed it
    only its own TRS threw the cup by the inverse of the asset's transform."""
    view, desc, prim = cup
    local, parent = gz._anchor_matrices(view.stage, desc)
    moved = gz.gesture_local(local, parent, Gf.Matrix4d().SetTranslate(Gf.Vec3d(1, 0, 0)))
    chunk_gizmo.apply_bindings(desc.chunk_prim, desc.name, moved)
    t = _world(prim).ExtractTranslation()
    assert (t[0], t[1], t[2]) == pytest.approx((11.0, 0.0, 0.0))


def test_a_turn_holds_the_middle_of_the_geometry_still(cup):
    view, desc, prim = cup
    centre = gz.anchor_world(view.stage, desc)
    local, parent = gz._anchor_matrices(view.stage, desc)
    spun = gz.about(centre, gz.turn((0.0, 0.0, 1.0), 1.5707963))
    chunk_gizmo.apply_bindings(desc.chunk_prim, desc.name, gz.gesture_local(local, parent, spun))
    assert tuple(gz.anchor_world(view.stage, desc)) == pytest.approx((10.0, 4.0, 0.0), abs=1e-5)


def test_a_scale_holds_the_middle_of_the_geometry_still(cup):
    view, desc, prim = cup
    centre = gz.anchor_world(view.stage, desc)
    local, parent = gz._anchor_matrices(view.stage, desc)
    grown = gz.about(centre, gz.stretch(2.0, (1.0, 0.0, 0.0)))
    chunk_gizmo.apply_bindings(desc.chunk_prim, desc.name, gz.gesture_local(local, parent, grown))
    assert tuple(gz.anchor_world(view.stage, desc)) == pytest.approx((10.0, 4.0, 0.0), abs=1e-5)
    assert _world(prim).Transform(Gf.Vec3d(1, 0, 0))[0] == pytest.approx(12.0)


def test_a_click_picks_the_component_it_lands_in(view):
    """Clicking one wheel moves the car: the pick walks up to the component."""
    stage = view.stage
    UsdGeom.Xform.Define(stage, "/Set")
    car = UsdGeom.Xform.Define(stage, "/Set/Car").GetPrim()
    Usd.ModelAPI(car).SetKind(Kind.Tokens.component)
    UsdGeom.Xform.Define(stage, "/Set/Car/Wheels")
    UsdGeom.Cube.Define(stage, "/Set/Car/Wheels/FrontLeft")
    UsdGeom.Cube.Define(stage, "/Loose")
    assert vp.component_of(stage, "/Set/Car/Wheels/FrontLeft") == "/Set/Car"
    assert vp.component_of(stage, "/Set/Car") == "/Set/Car"
    assert vp.component_of(stage, "/Loose") == "/Loose", "no component: what was clicked"


def test_without_kinds_a_click_picks_the_referenced_asset(view, tmp_path):
    """A DCC export has no kinds; the prim the reference landed on is the asset."""
    asset = Usd.Stage.CreateNew(str(tmp_path / "car.usda"))
    root = UsdGeom.Xform.Define(asset, "/root").GetPrim()
    UsdGeom.Xform.Define(asset, "/root/Body")
    UsdGeom.Cube.Define(asset, "/root/Body/Wheel_FL")
    asset.SetDefaultPrim(root)
    asset.GetRootLayer().Save()
    car = view.stage.DefinePrim("/Car", "Xform")
    car.GetReferences().AddReference(str(tmp_path / "car.usda"))
    assert vp.component_of(view.stage, "/Car/Body/Wheel_FL") == "/Car"


def test_bounds_picking_finds_the_prim_under_the_ray(view):
    """The GL-free fallback: Hydra's own pick raises on some builds (pending GL errors from
    whoever drew last), and this is what keeps clicking working there."""
    stage = view.stage
    _near = UsdGeom.Xform.Define(stage, "/Near")
    UsdGeom.Cube.Define(stage, "/Near/Box")
    far = UsdGeom.Xform.Define(stage, "/Far")
    UsdGeom.Cube.Define(stage, "/Far/Box")
    far.AddTranslateOp().Set(Gf.Vec3d(0, 0, -20))

    hit = vp.pick_by_bounds(stage, (0.0, 0.0, 10.0), (0.0, 0.0, -1.0))
    assert hit == "/Near/Box", "the nearer of two boxes on the ray"
    assert vp.pick_by_bounds(stage, (50.0, 50.0, 10.0), (0.0, 0.0, -1.0)) is None


def test_bounds_picking_skips_invisible_prims(view):
    stage = view.stage
    cube = UsdGeom.Cube.Define(stage, "/Hidden")
    cube.MakeInvisible()
    assert vp.pick_by_bounds(stage, (0.0, 0.0, 10.0), (0.0, 0.0, -1.0)) is None


def test_bounds_picking_returns_the_leaf_not_the_scene_root(view):
    """An ancestor's box contains every child's, so a pick that considered ancestors would
    answer with the root for every click."""
    stage = view.stage
    UsdGeom.Xform.Define(stage, "/Group")
    UsdGeom.Cube.Define(stage, "/Group/Box")
    assert vp.pick_by_bounds(stage, (0.0, 0.0, 10.0), (0.0, 0.0, -1.0)) == "/Group/Box"


def test_a_click_on_a_mesh_finds_the_chunk_driving_its_parent(driven):
    """chunks_for_prim reads a prim's own spec stack, and a click lands on the gprim while
    a Transform chunk authors its xformOp on the Xform above. The host walks up."""
    view, xf, prim = driven
    mesh = view.stage.DefinePrim(prim.GetPath().AppendChild("RenderMesh"), "Mesh")
    assert chunk_gizmo.resolve_gizmos(mesh) == []
    bg.set_picked_prim_paths([str(mesh.GetPath())])
    try:
        found = gz._picked_prim(view.stage)
        assert found is not None and found.GetPath() == prim.GetPath()
    finally:
        bg.set_picked_prim_paths([])


def test_an_undriven_click_still_reports_the_prim_it_hit(view):
    """So ensure_transform_chunk has something to make a chunk for on the first drag."""
    _loose = view.stage.DefinePrim("/Loose", "Cube")
    bg.set_picked_prim_paths(["/Loose"])
    try:
        found = gz._picked_prim(view.stage)
        assert found is not None and str(found.GetPath()) == "/Loose"
    finally:
        bg.set_picked_prim_paths([])




# ---------------------------------------------------------------------------
# the viewport camera, both ways
# ---------------------------------------------------------------------------

def _camera_prim(stage, path="/cam", focal=35.0, aperture=36.0):
    cam = UsdGeom.Camera.Define(stage, path)
    cam.CreateFocalLengthAttr(focal)
    cam.CreateHorizontalApertureAttr(aperture)
    cam.CreateVerticalApertureAttr(aperture * 2 / 3)
    cam.CreateClippingRangeAttr(Gf.Vec2f(0.5, 500.0))
    UsdGeom.Xformable(cam).AddTranslateOp().Set(Gf.Vec3d(1, 2, 3))
    return cam.GetPrim()


@pytest.fixture
def cam_state():
    """Each test starts with no prim claimed, and leaves none behind."""
    yield
    addon.camera.release_viewport_camera()


def test_set_viewport_camera_mirrors_the_prim_onto_the_scene_camera(cam_state):
    """The Camera chunk's "Set as Viewport Camera" had nothing to land on in Blender."""
    stage = Usd.Stage.CreateInMemory()  # held: a prim expires with its stage
    prim = _camera_prim(stage, focal=50.0)
    addon.BlenderScene(prim.GetStage()).set_viewport_camera(prim)
    obj = bpy.context.scene.camera
    assert obj is not None and obj.name == addon.camera.MIRROR_NAME
    assert obj.data.lens == pytest.approx(50.0)
    assert obj.data.sensor_width == pytest.approx(36.0)
    assert obj.data.clip_start == pytest.approx(0.5)
    assert tuple(obj.matrix_world.translation) == pytest.approx((1, 2, 3))


def test_release_hands_back_the_camera_the_mirror_replaced(cam_state):
    scene = bpy.context.scene
    own = bpy.data.objects.new("own_cam", bpy.data.cameras.new("own_cam"))
    scene.collection.objects.link(own)
    scene.camera = own
    stage = Usd.Stage.CreateInMemory()  # held: a prim expires with its stage
    prim = _camera_prim(stage)
    sender = addon.BlenderScene(prim.GetStage())
    sender.set_viewport_camera(prim)
    sender.set_viewport_camera(prim)  # a locked camera re-claims on every move
    sender.release_viewport_camera()
    assert scene.camera == own


def test_the_mirror_follows_the_prim_at_the_timeline_frame(cam_state):
    """An animated camera plays in Blender: the mirror is sampled at the current frame."""
    stage = Usd.Stage.CreateInMemory()  # held: a prim expires with its stage
    prim = _camera_prim(stage)
    focal = UsdGeom.Camera(prim).GetFocalLengthAttr()
    focal.Set(20.0, 1)
    focal.Set(80.0, 11)
    scene = bpy.context.scene
    scene.frame_set(1)
    addon.camera.set_viewport_camera(prim)
    scene.frame_set(11)
    addon.camera._tick()
    assert scene.camera.data.lens == pytest.approx(80.0)


def test_the_view_reads_back_as_the_lens_it_was_built_from():
    """Apertures come out of the projection, so camera view's own lens reads true too."""
    lens, aperture = 50.0, 36.0
    near, far = 0.1, 100.0
    p00 = 2 * lens / aperture
    window = Matrix(((p00, 0, 0, 0), (0, p00 * 1.5, 0, 0),
                (0, 0, -(far + near) / (far - near), -2 * far * near / (far - near)),
                (0, 0, -1, 0)))
    view = Matrix.Translation((0, 0, -5))
    gf = addon.camera.gf_camera_from_view(view, window, lens, near, far)
    assert gf.horizontalAperture == pytest.approx(aperture)
    assert gf.verticalAperture == pytest.approx(aperture / 1.5)
    assert gf.transform.ExtractTranslation() == Gf.Vec3d(0, 0, 5)


def test_the_hydra_view_draws_at_the_timeline_frame():
    scene = bpy.context.scene
    scene.frame_set(7, subframe=0.5)
    try:
        assert vp.current_time() == Usd.TimeCode(7.5)
    finally:
        scene.frame_set(1)


# ---------------------------------------------------------------------------
# saving: the network goes wherever the .blend goes
# ---------------------------------------------------------------------------

@pytest.fixture
def addon_on():
    """The add-on registered, as an enabled add-on is: the tree properties saving reads exist.

    The view fixture registers the node types only, so these tests passed or failed by
    whether an earlier test had left the add-on registered.
    """
    addon.register()
    yield
    addon.unregister()


def test_a_new_network_saves_to_a_file_it_never_had(tmp_path, addon_on):
    """Save on a network started with New called Save() on an anonymous layer."""
    tree = bpy.data.node_groups.new("fresh", bg.TREE_ID)
    addon._open_stage(Usd.Stage.CreateInMemory(), tree=tree)
    out = tmp_path / "fresh.usda"
    addon.write_network(addon.get_view(), str(out))
    assert out.exists()


def test_a_new_network_saves_in_blenders_space(tmp_path, addon_on):
    """Blender is Z-up in metres times its Unit Scale; a saved network has to say so, or
    every other host reads it as USD's fallback Y-up centimetres and turns it wrong."""
    units = bpy.context.scene.unit_settings
    before = units.scale_length
    units.scale_length = 0.5
    try:
        tree = bpy.data.node_groups.new("spaced", bg.TREE_ID)
        addon._open_stage(addon.new_network_stage(), tree=tree)
        out = tmp_path / "spaced.usda"
        addon.write_network(addon.get_view(), str(out))
    finally:
        units.scale_length = before
    saved = Usd.Stage.Open(str(out))
    assert UsdGeom.GetStageUpAxis(saved) == UsdGeom.Tokens.z
    assert UsdGeom.GetStageMetersPerUnit(saved) == pytest.approx(0.5)


def test_an_opened_network_keeps_the_space_it_was_authored_in(tmp_path, addon_on):
    """A file that says its own space -- usdview's Y-up centimetres here -- keeps it."""
    path, context = make_network(tmp_path / "authored.usda")
    layer = Sdf.Layer.FindOrOpen(path)
    layer.pseudoRoot.SetInfo("upAxis", "Y")
    layer.pseudoRoot.SetInfo("metersPerUnit", 0.01)
    layer.Save()
    tree = bpy.data.node_groups.new("authored", bg.TREE_ID)
    addon._open_stage(Usd.Stage.Open(path), context_path=context, tree=tree)
    out = tmp_path / "authored_out.usda"
    addon.write_network(addon.get_view(), str(out))
    saved = Usd.Stage.Open(str(out))
    assert UsdGeom.GetStageUpAxis(saved) == UsdGeom.Tokens.y
    assert UsdGeom.GetStageMetersPerUnit(saved) == pytest.approx(0.01)


def test_a_saved_network_keeps_every_chunk_wherever_it_is_authored(tmp_path, addon_on):
    """Save wrote the root layer only. Behind an Edit Target, chunk prims live in the layer
    it names, and a chunk added in Blender in the session layer: both were dropped."""
    path, context = make_network(tmp_path / "targeted.usda", modules=("layer", "edit_target", "transform"),
                                 positions=((0.0, 0.0), (300.0, 0.0), (600.0, 0.0)))
    tree = bpy.data.node_groups.new("targeted", bg.TREE_ID)
    addon._open_stage(Usd.Stage.Open(path), context_path=context, tree=tree)
    view = addon.get_view()
    entry = next(e for e in chunk_registry.ChunkRegistry().get_menu_entries()
                 if e.module.module_name == "transform" and e.presets is None)
    view.create_chunk(entry, 900.0, 0.0)
    out = tmp_path / "saved.usda"

    addon.write_network(view, str(out))

    assert chunk_prims(Usd.Stage.Open(str(out))) == chunk_prims(view.stage)


def test_saving_the_blend_keeps_a_network_with_no_file(tmp_path, addon_on):
    """An untitled network rides along in the .blend and reopens from there."""
    path, context = make_network(tmp_path / "embed.usda")
    tree = bpy.data.node_groups.new("embed", bg.TREE_ID)
    addon._open_stage(Usd.Stage.Open(path), context_path=context, tree=tree)
    tree.chunk_network_path = ""
    addon._on_save_pre()
    assert tree.chunk_network_embedded
    stage = addon._stage_for_tree(tree)
    assert len(chunk_prims(stage)) == len(chunk_prims(addon.get_view().stage))


def test_saving_the_blend_flags_an_edited_network_but_leaves_its_file(tmp_path, addon_on):
    """The .usda is the user's: Ctrl+S in Blender warns about edits, never writes them."""
    path, context = make_network(tmp_path / "ride.usda")
    before = open(path, encoding="utf-8").read()
    tree = bpy.data.node_groups.new("ride", bg.TREE_ID)
    tree.chunk_network_path = str(path)
    addon._open_stage(Usd.Stage.Open(path), context_path=context, tree=tree)
    view = addon.get_view()
    node = next(n for n in view.tree.nodes if n.name in view.prims)
    node.location = (1234.0, -567.0)
    addon._on_save_pre()
    assert open(path, encoding="utf-8").read() == before
    assert addon.network_unsaved(view)
    assert not tree.chunk_network_embedded


# ---------------------------------------------------------------------------
# Blender Import: Blender objects into the chunk's layer
# ---------------------------------------------------------------------------

@pytest.fixture
def imported(view):
    module = chunk_registry.ChunkRegistry().get_chunk_module("blender_import")
    assert module is not None, "blender_import should load in Blender"
    bpy.ops.mesh.primitive_monkey_add()
    obj = bpy.context.object
    material = bpy.data.materials.new("ImportRed")
    obj.data.materials.append(material)
    node = _make(module)
    node.get_param("primpath_location").value = "/imported"
    yield node, obj, material, module.module
    bpy.data.objects.remove(obj)
    bpy.data.materials.remove(material)


def test_blender_objects_are_copied_into_the_chunk_layer(imported):
    node, obj, _material, mod = imported
    mod.execute(node)
    text = node.get_edit_context().ExportToString()
    assert "def Mesh" in text
    # A binding that pointed inside the export root is remapped with it.
    assert "</imported/_materials/ImportRed>" in text


def test_only_what_the_import_reads_changes_its_fingerprint(imported):
    node, obj, material, mod = imported
    before = mod._fingerprint(node)
    bpy.context.scene.cursor.location.x += 3
    bpy.context.view_layer.update()
    assert mod._fingerprint(node) == before
    obj.location.x += 1
    bpy.context.view_layer.update()
    moved = mod._fingerprint(node)
    assert moved != before
    material.diffuse_color = (1, 0, 0, 1)
    assert mod._fingerprint(node) != moved


def test_the_camera_mirror_is_never_imported(imported, cam_state):
    node, _obj, _material, mod = imported
    addon.camera.set_viewport_camera(_camera_prim(Usd.Stage.CreateInMemory()))
    assert addon.camera.MIRROR_NAME not in [o.name for o in mod._objects(node)]


def test_imported_objects_are_hidden_once_and_only_by_the_addon(imported, monkeypatch):
    """The Hydra view draws the import where Blender draws the original: hide the original
    while a chunk imports it, unhide it after -- but never unhide what the user hid."""
    node, obj, _material, _mod = imported
    mine = bpy.data.objects.new("users_own", None)
    bpy.context.scene.collection.objects.link(mine)
    mine.hide_set(True)
    monkeypatch.setattr(node, "is_blocked", lambda: False)
    scene = _Scene(None)
    monkeypatch.setattr(addon, "_scene", scene)
    monkeypatch.setattr(scene, "get_chunks", lambda: [node])
    try:
        addon._sync_host_sources()
        assert obj.hide_get() and mine.hide_get()

        node.get_param("hide_source").value = False
        addon._sync_host_sources()
        assert not obj.hide_get() and mine.hide_get()

        node.get_param("hide_source").value = True
        addon._sync_host_sources()
        monkeypatch.setattr(scene, "get_chunks", lambda: [])  # the chunk is deleted
        addon._sync_host_sources()
        assert not obj.hide_get() and mine.hide_get()
    finally:
        bpy.data.objects.remove(mine)


def test_an_image_menu_gives_blender_icon_items():
    """template_icon_view needs every enum item in the five-element form, icon included."""
    menu = chunk.ImageMenu("look", "Look", menu_items=["Gold", "Oak"], dynamic_items=True)
    items = chunk_blender._enum_items(menu)
    assert [len(i) for i in items] == [5, 5]
    assert [i[0] for i in items] == ["Gold", "Oak"]


def test_a_long_menu_is_searchable(view):
    """Blender's dropdown cannot be typed into; the search popup sets the pick instead."""
    node = next(n for n in view.tree.nodes if n.name in view.prims and n.get_chunk() is not None)
    chunk_ = node.get_chunk()
    menu = chunk.ImageMenu("look", "Look", menu_items=[f"m{i}" for i in range(40)],
                           dynamic_items=True)
    chunk_.add_param(menu)
    result = bpy.ops.usd_chunks.search_menu(tree_name=view.tree.name, node_path=node.name,
                                            param_name="look", item="m17")
    assert result == {"FINISHED"} and menu.value == "m17"


# ---------------------------------------------------------------------------
# the brush: painting through a chunk's brush handle
# ---------------------------------------------------------------------------

from usd_chunks.frontends.blender.addon import brush as br  # noqa: E402


def _floor(stage, path="/World/Floor", size=10.0, divisions=10):
    """A flat grid at z=0, from (0, 0) to (size, size)."""
    mesh = UsdGeom.Mesh.Define(stage, path)
    step = size / divisions
    row = divisions + 1
    mesh.GetPointsAttr().Set([Gf.Vec3f(ix * step, iy * step, 0.0)
                              for iy in range(row) for ix in range(row)])
    mesh.GetFaceVertexCountsAttr().Set([4] * divisions * divisions)
    mesh.GetFaceVertexIndicesAttr().Set(
        [i for iy in range(divisions) for ix in range(divisions)
         for i in (iy * row + ix, iy * row + ix + 1, (iy + 1) * row + ix + 1, (iy + 1) * row + ix)])
    return mesh


@pytest.fixture
def painted(view):
    """A Scatter Brush chunk painting a floor."""
    _floor(view.stage)
    # Not _make: that swaps in a bare config, and the brush handle lives in the module's.
    module = chunk_registry.ChunkRegistry().get_chunk_module("scatter_brush")
    painter = (module.create() if module.loaded else module.load())[0]
    painter.name = "Paint"
    view.scene.add_chunk(painter)
    painter.get_param("surface_prim").value = "/World/Floor"
    painter.get_param("radius").value = 2.0
    painter.get_param("strength").value = 1.0
    painter.execute()
    view.chunks = {chunk_stage.get_chunk_prim_path(c): c for c in view.scene.get_chunks()}
    view.selected_nodes = set()
    bg.set_picked_prim_paths([])
    yield view, painter
    bg.set_picked_prim_paths([])


def test_a_brush_selected_in_the_graph_is_the_one_to_paint_with(painted):
    view, painter = painted
    assert br.brush_desc(view.stage, view) is None
    view.selected_nodes = {chunk_stage.get_chunk_prim_path(painter)}
    desc = br.brush_desc(view.stage, view)
    assert desc is not None and desc.type == "brush"
    assert str(desc.chunk_prim) == chunk_stage.get_chunk_prim_path(painter)
    assert [str(p) for p in desc.anchor] == ["/World/Floor"]


def test_picking_the_painted_mesh_finds_its_brush(painted):
    view, painter = painted
    bg.set_picked_prim_paths(["/World/Floor"])
    desc = br.brush_desc(view.stage, view)
    assert desc is not None
    assert str(desc.chunk_prim) == chunk_stage.get_chunk_prim_path(painter)


def test_a_ray_hits_the_surface_with_a_normal_facing_it(view):
    _floor(view.stage)
    surface = br.Surface(view.stage, ["/World/Floor"])
    point, normal = surface.hit((3.0, 4.0, 10.0), (0.0, 0.0, -1.0))
    assert tuple(point) == pytest.approx((3.0, 4.0, 0.0), abs=1e-5)
    assert tuple(normal) == pytest.approx((0.0, 0.0, 1.0), abs=1e-5)
    # From below, the normal still faces the ray.
    _point, normal = surface.hit((3.0, 4.0, -10.0), (0.0, 0.0, 1.0))
    assert tuple(normal) == pytest.approx((0.0, 0.0, -1.0), abs=1e-5)
    assert surface.hit((30.0, 4.0, 10.0), (0.0, 0.0, -1.0)) == (None, None)


def test_the_hit_test_is_in_world_space(view):
    """A mesh under a moved parent is hit where it is drawn, not at its local points."""
    UsdGeom.Xform.Define(view.stage, "/World").AddTranslateOp().Set(Gf.Vec3d(0.0, 0.0, 5.0))
    _floor(view.stage)
    surface = br.Surface(view.stage, ["/World"])
    point, _normal = surface.hit((3.0, 4.0, 10.0), (0.0, 0.0, -1.0))
    assert tuple(point) == pytest.approx((3.0, 4.0, 5.0), abs=1e-5)


def test_a_surface_with_no_mesh_has_nothing_to_hit(view):
    view.stage.DefinePrim("/Empty", "Xform")
    surface = br.Surface(view.stage, ["/Empty", "/Missing"])
    assert surface.tree is None
    assert surface.hit((0.0, 0.0, 1.0), (0.0, 0.0, -1.0)) == (None, None)


def test_a_hit_sent_as_a_stroke_paints_the_floor(painted):
    """The whole path but the mouse: ray, hit, sample, field."""
    view, painter = painted
    view.selected_nodes = {chunk_stage.get_chunk_prim_path(painter)}
    desc = br.brush_desc(view.stage, view)
    point, normal = br.Surface(view.stage, desc.anchor).hit((5.0, 5.0, 10.0), (0.0, 0.0, -1.0))
    painter.start_gizmo()
    assert chunk_gizmo.apply_stroke(painter, desc.name,
                                    [(Gf.Vec3d(*point), 1.0, Gf.Vec3d(*normal))])
    painter.stop_gizmo()
    primvar = UsdGeom.PrimvarsAPI(view.stage.GetPrimAtPath("/World/Floor")).GetPrimvar(
        "scatterDensity")
    weights = list(primvar.ComputeFlattened())
    assert max(weights) > 0.0
    assert weights[0] == 0.0  # the corner at (0, 0) is outside a radius-2 brush at (5, 5)


def test_the_ring_is_sized_from_the_chunks_radius(painted):
    view, painter = painted
    view.selected_nodes = {chunk_stage.get_chunk_prim_path(painter)}
    desc = br.brush_desc(view.stage, view)
    assert br.brush_radius(painter, desc, default=0.5) == pytest.approx(2.0)
    painter.get_param("radius").value = 0.0
    assert br.brush_radius(painter, desc, default=0.5) == pytest.approx(0.5)


def test_the_ring_lies_in_the_surface_at_its_radius():
    centre, normal = Vector((1.0, 2.0, 3.0)), Vector((0.0, 1.0, 0.0))
    points = br.ring_points(centre, normal, 2.0)
    assert points[0] == pytest.approx(points[-1])
    lifted = centre + normal * 2.0 * br._LIFT
    for p in points:
        offset = Vector(p) - lifted
        assert offset.length == pytest.approx(2.0)
        assert offset.dot(normal) == pytest.approx(0.0, abs=1e-6)


def test_resizing_doubles_per_step_and_halves_back():
    step = br._RESIZE_PIXELS_PER_DOUBLING
    assert br.resized(1.0, step) == pytest.approx(2.0)
    assert br.resized(1.0, -step) == pytest.approx(0.5)
    assert br.resized(1.0, 0.0) == pytest.approx(1.0)


def test_the_brush_operator_registers_on_b():
    br.register()
    try:
        assert br.USDCHUNKS_OT_brush.is_registered
        assert hasattr(bpy.ops.usd_chunks, "brush")
        br.register()  # idempotent: re-enabling the add-on must not raise
    finally:
        br.unregister()
    assert not br.USDCHUNKS_OT_brush.is_registered


# ---------------------------------------------------------------------------
# number keys: 1 the Output, 2 and 3 the views -- as in the Qt graph
# ---------------------------------------------------------------------------

def test_one_picks_the_output_and_two_three_the_views():
    keys = {key: props["slot"] for op, key, _mods, props in addon._KEYS
            if op is addon.USDCHUNKS_OT_set_view_slot}
    assert keys == {"ONE": 0, "TWO": 1, "THREE": 2}


def test_slot_buttons_are_named_like_the_qt_graph():
    assert [addon.slot_label(s) for s in (0, 1, 2)] == ["Output [1]", "View [2]", "View [3]"]


def test_the_status_names_the_slot_as_the_key_does(view):
    """Pressing 2 and reading "view 1 active" looked like the wrong view was picked."""
    view.set_selection({_node(view, "Ref").name})
    view.create_view_node()
    assert view.status == "View 2"
    view.set_active_slot(0)
    assert view.status == "Output active"
    view.set_active_slot(1)
    assert view.status == "View 2 active"


def test_the_output_slot_is_refused_in_a_graph_without_one(view):
    """Qt never switches to what the graph lacks: an Output-less slot 0 cooks nothing."""
    outputs = [c for c in view.scene.get_chunks() if chunk_stage.is_output_chunk(c)]
    chunk_graph.delete_chunks(outputs)
    view.chunks = {chunk_stage.get_chunk_prim_path(c): c for c in view.scene.get_chunks()}
    before = view.active_slot()
    view.set_active_slot(0)
    assert view.status == "no Output in this graph"
    assert view.active_slot() == before


# ---------------------------------------------------------------------------
# diving cooks the graph on screen, as Qt does
# ---------------------------------------------------------------------------

def test_diving_in_and_out_cooks_the_graph_on_screen(view, monkeypatch):
    """A dive used to change only the tree. Inside, the viewport kept showing the outer
    graph's cook; that left the group's own meshes and the muting of what is downstream
    of it to whatever cooked next."""
    view.set_selection({_node(view, "Move").name})
    view.group_selection()
    group = next(p for p in view.prims if view.can_dive(p))
    top = view.context
    focused = []
    monkeypatch.setattr(chunk_stage, "recook_focus",
                        lambda stage, scene, context, view_chunk: focused.append(context))

    view.dive_into(group)
    view.dive_out()

    paths = [chunk_stage.get_chunk_prim_path(c) if c is not None else None for c in focused]
    assert paths == [group, top]


# ---------------------------------------------------------------------------
# a lofted param named like one of Blender's own node fields
# ---------------------------------------------------------------------------

def test_a_lofted_width_is_the_params_not_the_nodes(view):
    """Every Blender node has `width` and `height` (its size on screen). A group lofting a
    Mesh's width showed the node's size there, 100, and editing it resized the node."""
    mesh = _make(chunk_registry.ChunkRegistry().get_chunk_module("mesh"))
    mesh.name = "Slab"
    view.scene.add_chunk(mesh)
    mesh.get_param("mesh_type").value = "cube"
    mesh.get_param("width").value = 8.0
    view.chunks = {chunk_stage.get_chunk_prim_path(c): c for c in view.scene.get_chunks()}
    view.reload()
    view.set_selection({_node(view, "Slab").name})
    view.group_selection()
    group = next(p for p in view.prims if view.can_dive(p))
    lofted = {q.name for q in chunk_blender.iter_drawable(view.chunks[group])}
    assert "width" in lofted
    node = view.tree.nodes[group]
    assert node["width"] == pytest.approx(8.0)
    assert node.width != pytest.approx(8.0)
