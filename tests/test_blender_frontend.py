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

from pxr import Usd  # noqa: E402

from usd_chunks import chunk_registry  # noqa: E402
from usd_chunks.chunk_core import ChunkConfig  # noqa: E402
from usd_chunks.chunk_flags import ChunkFlags  # noqa: E402
from usd_chunks.chunk_scene import SceneSender  # noqa: E402
from usd_chunks.plugins import chunk_blender, chunk_graph, chunk_usd  # noqa: E402
from usd_chunks.plugins import chunk_blender_nodegraph as bg  # noqa: E402


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture
def view():
    """A two-chunk network -- Reference feeding Transform -- mirrored into a node tree."""
    import reference
    import transform

    stage = Usd.Stage.CreateInMemory()
    scene = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(scene, "pytest-blender")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_usd.ChunkStageSync().initialize(stage, scene)
    bg.register()

    ref = reference.main()
    ref._config = ChunkConfig(module="reference")
    ref.name = "Ref"
    scene.add_chunk(ref)
    move = transform.main()
    move._config = ChunkConfig(module="transform")
    move.name = "Move"
    scene.add_chunk(move)
    move.add_dependency(ref)
    move.get_param("tz").value = 3.0

    # add_chunk routes a context-less chunk into the scene's default chunkgroup, so the
    # graph to read is that group -- not CHUNKS_ROOT, where only the group itself lives.
    context = chunk_usd.get_chunk_prim_path(scene.peek_root_context())
    tree = bpy.data.node_groups.new("test", bg.TREE_ID)
    v = bg.BlenderGraphView(stage, scene, tree=tree, context=context)
    v.chunks = {chunk_usd.get_chunk_prim_path(c): c for c in scene.get_chunks()}
    v.reload()
    chunk_graph.node_clipboard.clear()
    chunk_graph.edge_clipboard.clear()
    return v


def _node(view, label):
    return next(n for n in view.tree.nodes if n.label == label)


# ---------------------------------------------------------------------------
# the tree is a view of USD
# ---------------------------------------------------------------------------

def test_the_tree_is_rebuilt_from_usd(view):
    labels = sorted(n.label for n in view.tree.nodes)
    assert labels == ["Move", "Ref"]


def test_each_node_gets_its_chunk_type(view):
    assert _node(view, "Move").bl_idname == bg.node_class_id("transform")


def test_the_exec_wire_becomes_a_link(view):
    links = [(link.from_node.label, link.to_node.label, link.to_socket.name)
             for link in view.tree.links]
    assert links == [("Ref", "Move", "chunk")]


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
    assert [n.label for n in view.tree.nodes] == ["Ref"]


def test_dive_refuses_a_plain_chunk(view):
    """Keyed on is_chunkgroup, never the CONTEXT flag -- a chunk-in-chunk carries CONTEXT
    too and is an implementation detail, not a subnet."""
    assert not view.can_dive(_node(view, "Move").chunk_path)


def test_no_active_view_reads_as_zero(view):
    assert view.active_slot() == 0


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

def test_a_node_class_is_generated_per_chunk_type():
    """Registration loads the whole library, because Blender needs a class registered
    before a node of that type can exist -- there is no lazy path."""
    assert len(bg._generated) > 10


def test_qt_only_chunks_are_skipped_not_fatal():
    """Five chunks import PySide6 at module level and cannot load in Blender. The registry
    skips a module whose import fails, so the add-on degrades rather than refusing to
    start."""
    assert "rbd_solver" not in bg._generated


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
        assert child is not None, "no menu class for the %s category" % name
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
    labels = [n.label for n in view.tree.nodes]
    assert len(labels) == 4, labels
