"""The Blender add-on: what Blender enables, and what it wires up.

`chunk_blender_nodegraph` is the frontend and `chunk_blender` the widgets; neither knows
anything about being installed. This package is the part Blender actually loads -- a
manifest, `register()`/`unregister()`, a SceneSender, and the operators that open a network.

**A node tree corresponds to a `.usda` file.** Blender has no ambient USD stage the way
usdview does, so something has to say which stage a graph edits. Tying it to a file the
user opens is the honest answer: it matches what `usdchunks run` takes, it makes the tree a
view of something that exists on disk, and it means saving the `.blend` never becomes a way
of saving the graph. The path is kept on the tree itself (`chunk_network_path`), so two
trees can edit two networks.

**Where it appears.** The node editor's editor-type dropdown gains "USDChunks Editor",
alongside Geometry Node Editor and the rest; the sidebar gets a USDChunks tab, and Shift+A
lists the whole chunk library by the namespace each chunk declares.

**Installing.** Blender 4.2+ extension: zip this directory and use Install from Disk. It
needs `usd_chunks` importable -- either shipped as wheels in the manifest, or, for
development, installed into Blender's own Python:

    <blender>/<version>/python/bin/python -m pip install -e /path/to/usd_chunks

Do **not** install `usd-core` alongside. Blender already ships the bindings, under a
private namespace (`pxrBlender_v26_03__pxrReserved__`), so a pip build is a different C++
type and cannot exchange objects with Blender's. That is why `usd-core` is an optional
extra of the package rather than a dependency.
"""
from __future__ import annotations

import logging

import bpy
from pxr import Gf, Usd

from usd_chunks import chunk_registry, configure_logging
from usd_chunks.chunk_scene import SceneSender
from usd_chunks import chunk_stage
from usd_chunks.frontends.blender import nodegraph as bg
from usd_chunks.frontends.blender.addon import dependencies
from usd_chunks.frontends.blender.addon import gizmos
from usd_chunks.frontends.blender.addon import viewport

logger = logging.getLogger(__name__)


class BlenderScene(SceneSender):
    """The host interface, backed by Blender.

    Most of what a SceneSender exposes is about a *viewer* -- the active camera, the
    viewport renderer, the current frame -- and Blender has all of those, but a chunk graph
    editing a `.usda` on disk does not need them to be truthful to be useful. They answer
    honestly where Blender can and return a neutral value where the question does not apply,
    rather than raising: a chunk asking for the frame should get a number, not an exception
    that stops a cook.
    """

    is_app = True

    def __init__(self, stage=None):
        super().__init__()
        self._stage = stage

    # -- the two that actually matter -------------------------------------

    def get_stage(self):
        return self._stage

    def set_stage(self, stage) -> None:
        self._stage = stage

    def get_selected_prims(self):
        """The prim(s) last Ctrl+Click-picked in the live Hydra viewport.

        NOT Blender's own object selection: a USD prim shown by the live draw handler
        (viewport.py) has no Blender object at all to select in the first place, so that
        road is a dead end -- this instead reads
        chunk_blender_nodegraph.get_picked_prim_paths(), written by viewport.py's
        USDCHUNKS_OT_pick_prim from its own UsdImagingGL.Engine.TestIntersection call.
        """
        if self._stage is None:
            return []
        prims = []
        for path in bg.get_picked_prim_paths():
            prim = self._stage.GetPrimAtPath(path)
            if prim and prim.IsValid():
                prims.append(prim)
        return prims

    def get_selected_property(self):
        return None

    # -- viewer questions, answered where Blender can ---------------------

    def get_active_camera(self):
        camera = bpy.context.scene.camera if bpy.context.scene else None
        if camera is None:
            return None
        gf = Gf.Camera()
        matrix = camera.matrix_world
        gf.transform = Gf.Matrix4d(*[v for row in matrix.transposed() for v in row])
        return gf

    def get_viewport_renderer(self) -> str:
        scene = bpy.context.scene
        return str(scene.render.engine) if scene else ""

    def set_viewport_renderer_settings(self, settings) -> None:
        """Not applicable: Blender's render engines are not Hydra delegates taking USD
        render settings. Ignored rather than raised so a render chunk can still cook.
        """
        return None

    def get_current_frame(self) -> float:
        return float(bpy.context.scene.frame_current) if bpy.context.scene else 0.0

    def get_end_frame(self) -> float:
        return float(bpy.context.scene.frame_end) if bpy.context.scene else 0.0

    def should_install_packages(self, packages, chunk_name: str) -> bool:
        """Offer the install in a moment, through a dialog; no for now.

        Blender cannot block on a question while it loads chunks -- see dependencies.py,
        which asks once the add-on is idle and registers the chunk's node type after a yes.
        Packages go to the per-version dependency dir, never into Blender's own Python.
        """
        dependencies.queue(packages, chunk_name)
        return False

    def run_install(self, packages, chunk_name: str, run):
        # Called on dependencies.py's worker thread (or inline, with USD_CHUNK_AUTO_INSTALL=1):
        # report_line only stores the text for the modal operator to show.
        return run(dependencies.report_line)


#: One scene and one view per Blender session. A second node tree re-points them rather
#: than making a second set, because the chunk registry is a singleton keyed to one scene --
#: two live scenes would have the registry answering for whichever initialised last.
_scene: BlenderScene | None = None


def get_view():
    return bg.active_view()


def _open_stage(stage, context_path=None, tree=None) -> None:
    """Point the session at `stage` and rebuild the tree from it.

    `tree` overrides the usual `_active_tree()` guess -- needed by `_poll_new_trees`
    below, whose caller already knows exactly which tree it found stale and cannot trust
    `_active_tree()` (or `bpy.context.space_data` generally) to point at the same one.
    """
    global _scene
    _scene = BlenderScene(stage)
    chunk_registry.ChunkRegistry().initialize(_scene, "blender")
    chunk_registry.ChunkLayerManager().initialize(stage)
    # ChunkStageSync.initialize loads the file's chunks itself, with the dedup set that
    # stops a prim loading twice. A second load_chunks after it -- which stood here --
    # had no such set and loaded every chunk again: each got a fresh prim (Reference ->
    # Reference8, View1 -> View11) laid out in a row off to the right of the real
    # network, which read as "the file's positions are wrong".
    chunk_stage.ChunkStageSync().initialize(stage, _scene)
    # Node classes are generated from the registry, and the registry only discovers chunks
    # once a scene exists -- so this cannot happen in register(), where there is no scene
    # yet and every class would be generated from an empty library. Idempotent, so opening
    # a second network costs a dict lookup per already-registered type.
    made = bg.register_chunk_types()
    if made:
        logger.info("usd_chunks: %d chunk node type(s) registered", len(made))

    if tree is None:
        tree = _active_tree()
    if tree is not None:
        # Marks this tree as USD-backed, so _poll_new_trees below knows not to
        # re-initialize it (which would replace whatever stage/content it already has)
        # the next time the user merely switches back to looking at it.
        tree.chunk_network_initialized = True
    chunks = {chunk_stage.get_chunk_prim_path(c): c for c in _scene.get_chunks()}
    # chunk_graph.default_context, not peek_root_context: after load_chunks the scene has
    # no root context to peek at, and CHUNKS_ROOT shows the default chunkgroup and none of
    # the work inside it. The engine already knows which graph a file wants opened.
    context = context_path or bg.chunk_graph.default_context(stage)
    view = bg.BlenderGraphView(stage, _scene, tree=tree, context=context)
    view.chunks = chunks
    view.reload()
    # A context with no view cooks nothing, so this is not cosmetic -- see ensure_view.
    view.ensure_view()


# ---------------------------------------------------------------------------
# Catching Blender's own "+ New" (bpy.ops.node.new_node_tree)
# ---------------------------------------------------------------------------
#
# Blender always draws its stock ID-template "+ New" / browse widget in a node editor's
# header, for every registered tree type including ours -- there is no per-type opt-out,
# and overriding NODE_HT_header.draw wholesale risks fighting Blender's own View/Select/
# Add/Node menus across versions. That widget calls bpy.ops.node.new_node_tree(), which
# knows nothing about USD: it leaves a blank tree with no stage, no scene, no view behind
# it -- nothing executes (the Add menu comes from the registry, which only a real
# _open_stage populates), and switching away and back "loses" the ORIGINAL network's
# connections because active_view() is a single global and would point at this second,
# empty view instead.
#
# bpy.msgbus.subscribe_rna on SpaceNodeEditor.node_tree does not fire reliably for this,
# because a msgbus callback runs with whatever bpy.context Blender happens to
# have at notification time, which is not guaranteed to be the node editor whose property
# actually changed. A timer callback's context has the same problem, so this does not
# trust bpy.context.space_data either -- it scans every NODE_EDITOR area directly, the way
# viewport.py's _redraw() already does for VIEW_3D areas.

def _uninitialized_chunk_trees() -> list:
    """Every ChunkNetwork tree currently shown in a node editor that _open_stage has never
    touched -- i.e. created by Blender's own "+ New" rather than one of our operators.
    """
    found = []
    for window in getattr(bpy.context.window_manager, "windows", ()):
        for area in window.screen.areas:
            if area.type != "NODE_EDITOR":
                continue
            space = area.spaces.active
            tree = getattr(space, "node_tree", None)
            if (tree is not None and tree.bl_idname == bg.TREE_ID
                    and not tree.chunk_network_initialized and tree not in found):
                found.append(tree)
    return found


def _poll_new_trees() -> float:
    for tree in _uninitialized_chunk_trees():
        # A tree saved by a session before chunk_network_initialized existed defaults to
        # False too -- but if it already names a file, it is not blank, it just has not
        # been touched THIS session. Reopen the file rather than blanking a real network.
        stage = Usd.Stage.Open(tree.chunk_network_path) if tree.chunk_network_path else None
        _open_stage(stage if stage is not None else Usd.Stage.CreateInMemory(), tree=tree)
    return 0.1  # reschedule in 0.1s -- this is the latency between "+ New" and Shift+A working


def _active_tree():
    space = getattr(bpy.context, "space_data", None)
    tree = getattr(space, "edit_tree", None) if space else None
    if tree is not None and tree.bl_idname == bg.TREE_ID:
        return tree
    return next((t for t in bpy.data.node_groups if t.bl_idname == bg.TREE_ID), None)


def _show_tree(context, tree) -> None:
    """Pin the node editor to `tree`.

    `space_data.edit_tree` (what `_active_tree` reads) is read-only -- it is derived from
    `node_tree` plus any node-group diving depth. Without this, New/Open would create or
    open a tree that nothing on screen points at: the editor would keep showing whatever
    tree (or nothing) it already had pinned, `_active_tree`'s fallback would pick among
    ChunkNetwork trees by iteration order, and a graph could be built into a tree the user
    is not looking at.
    """
    space = getattr(context, "space_data", None)
    if space is not None and hasattr(space, "node_tree"):
        space.node_tree = tree


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class USDCHUNKS_OT_open_network(bpy.types.Operator):
    """Open a .usda chunk network and mirror it into a node tree."""

    bl_idname = "usd_chunks.open_network"
    bl_label = "Open Chunk Network"
    bl_options = {"REGISTER"}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.usda;*.usd;*.usdc", options={"HIDDEN"})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        stage = Usd.Stage.Open(self.filepath)
        if stage is None:
            self.report({"ERROR"}, f"could not open {self.filepath}")
            return {"CANCELLED"}
        tree = _active_tree()
        if tree is None:
            tree = bpy.data.node_groups.new("ChunkNetwork", bg.TREE_ID)
        tree.chunk_network_path = self.filepath
        _show_tree(context, tree)
        _open_stage(stage, tree=tree)
        self.report({"INFO"}, f"opened {self.filepath}")
        return {"FINISHED"}


class USDCHUNKS_OT_new_network(bpy.types.Operator):
    """Start an empty chunk network in a new node tree."""

    bl_idname = "usd_chunks.new_network"
    bl_label = "New Chunk Network"
    bl_options = {"REGISTER"}

    def execute(self, context):
        tree = bpy.data.node_groups.new("ChunkNetwork", bg.TREE_ID)
        _show_tree(context, tree)
        _open_stage(Usd.Stage.CreateInMemory(), tree=tree)
        self.report({"INFO"}, "new chunk network")
        return {"FINISHED"}


class USDCHUNKS_OT_save_network(bpy.types.Operator):
    """Write node positions back and save the .usda."""

    bl_idname = "usd_chunks.save_network"
    bl_label = "Save Chunk Network"
    bl_options = {"REGISTER"}

    def execute(self, context):
        view = get_view()
        if view is None:
            self.report({"WARNING"}, "no chunk network open")
            return {"CANCELLED"}
        # Blender owns dragging, so there is no move event to hook; positions are collected
        # here instead. Without this a layout done in Blender is lost, because the tree is
        # a view and the next reload overwrites it from USD.
        view.pull_positions_from_tree()
        view.stage.GetRootLayer().Save()
        self.report({"INFO"}, "saved")
        return {"FINISHED"}


class USDCHUNKS_OT_auto_layout(bpy.types.Operator):
    """Lay out the selection's connected subgraph, or everything."""

    bl_idname = "usd_chunks.auto_layout"
    bl_label = "Auto Layout"
    bl_options = {"REGISTER"}

    def execute(self, context):
        view = get_view()
        if view is None:
            return {"CANCELLED"}
        view.pull_selection_from_tree()
        view.auto_layout()
        return {"FINISHED"}


class USDCHUNKS_OT_set_view_slot(bpy.types.Operator):
    """Switch which view drives execution, and recook for it."""

    bl_idname = "usd_chunks.set_view_slot"
    bl_label = "Set Active View"
    bl_options = {"REGISTER"}

    slot: bpy.props.IntProperty(default=1, min=0, max=2)

    def execute(self, context):
        view = get_view()
        if view is None:
            return {"CANCELLED"}
        view.set_active_slot(self.slot)
        self.report({"INFO"}, view.status)
        return {"FINISHED"}


class USDCHUNKS_OT_dive_into(bpy.types.Operator):
    """Open the selected chunkgroup as its own graph."""

    bl_idname = "usd_chunks.dive_into"
    bl_label = "Dive Into Group"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        # Only in our own editor. This shares Tab with Blender's node.group_edit, and a
        # poll that said yes in a shader editor would hijack entering a shader group.
        return getattr(context.space_data, "tree_type", "") == bg.TREE_ID

    def execute(self, context):
        view = get_view()
        if view is None:
            return {"CANCELLED"}
        view.pull_selection_from_tree()
        targets = [p for p in view.selected_nodes if view.can_dive(p)]
        if not targets:
            # Nothing selected that can be dived, but the graph is one group and nothing
            # else: that group is what is meant. It is the shape every saved network has
            # at its top, and "select it first" is a step nobody should have to learn.
            groups = [p for p in view.prims if view.can_dive(p)]
            if len(groups) == 1 and len(view.prims) == 1:
                targets = groups
        if not targets:
            self.report({"WARNING"}, "select a chunkgroup to dive into")
            return {"CANCELLED"}
        view.dive_into(targets[0])
        self.report({"INFO"}, view.status)
        return {"FINISHED"}


class USDCHUNKS_OT_publish_network(bpy.types.Operator):
    """Write the selected chunkgroup to its own file and host it from there.

    Afterwards the group's chunks compose in from that file and anything this graph does
    to them is an override -- the sequence half of the sequence/shot split. Localise is
    the way back.
    """

    bl_idname = "usd_chunks.publish_network"
    bl_label = "Publish Network"
    bl_options = {"REGISTER"}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.usda;*.usd;*.usdc", options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        return getattr(context.space_data, "tree_type", "") == bg.TREE_ID

    def invoke(self, context, event):
        chunk = _selected_chunk(predicate=chunk_stage.is_chunkgroup)
        if chunk is None:
            self.report({"WARNING"}, "select a chunkgroup to publish")
            return {"CANCELLED"}
        self.filepath = f"{chunk.name}.usda"
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        chunk = _selected_chunk(predicate=chunk_stage.is_chunkgroup)
        if chunk is None or not self.filepath:
            return {"CANCELLED"}
        chunk_stage.publish_network(chunk, self.filepath)
        view = get_view()
        if view is not None:
            view.reload()
        self.report({"INFO"}, f"published {self.filepath}")
        return {"FINISHED"}


class USDCHUNKS_OT_localise_network(bpy.types.Operator):
    """Take a hosted network into this file as an ordinary chunkgroup.

    The file's chunks, with whatever this graph overrode on top, become local defs and the
    arc is dropped -- "Allow Editing of Contents". The inverse of Publish.
    """

    bl_idname = "usd_chunks.localise_network"
    bl_label = "Localise Network"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return getattr(context.space_data, "tree_type", "") == bg.TREE_ID

    def execute(self, context):
        chunk = _selected_chunk(predicate=chunk_stage.is_localisable)
        if chunk is None:
            self.report({"WARNING"}, "select a hosted network to localise")
            return {"CANCELLED"}
        name = chunk.name
        chunk_stage.localise_network(chunk)
        view = get_view()
        if view is not None:
            view.reload()
        self.report({"INFO"}, f"localised {name}")
        return {"FINISHED"}


def _selected_chunk(predicate):
    """The one selected chunk the predicate accepts, or -- when nothing selected fits --
    the graph's single candidate. Same "do not make me select the only thing there"
    courtesy dive_into already extends, and both verbs act on exactly one chunk.
    """
    view = get_view()
    if view is None:
        return None
    view.pull_selection_from_tree()
    chosen = [c for p, c in view.chunks.items()
              if p in view.selected_nodes and predicate(c)]
    if not chosen:
        candidates = [c for p, c in view.chunks.items() if p in view.prims and predicate(c)]
        if len(candidates) == 1:
            chosen = candidates
    return chosen[0] if len(chosen) == 1 else None


class USDCHUNKS_OT_dive_out(bpy.types.Operator):
    """Back up to the graph that holds this one."""

    bl_idname = "usd_chunks.dive_out"
    bl_label = "Dive Out"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return getattr(context.space_data, "tree_type", "") == bg.TREE_ID

    def execute(self, context):
        view = get_view()
        if view is None:
            return {"CANCELLED"}
        view.dive_out()
        self.report({"INFO"}, view.status)
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# The editing verbs, shadowing Blender's own node operators inside this tree type
#
# X, Shift+D, Ctrl+C/X/V, M and H all have stock node-editor operators, and every one of them
# edits the bpy tree and nothing else. The tree is a view, so that is the wrong half: a
# deleted node came back on the next reload, a duplicate copied `chunk_path` and so edited
# and wired the ORIGINAL prim, M set node.mute and left the chunk enabled. These take the
# same keys (add-on keymaps win over the defaults) and poll only in a chunk tree, so in a
# shader editor each key still means what it always did.
# ---------------------------------------------------------------------------

class _ChunkTreeOperator(bpy.types.Operator):
    """Shared shape: only in a chunk tree, only with a view, selection read from the
    editor first because Blender owns clicking and box-select.
    """

    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return getattr(context.space_data, "tree_type", "") == bg.TREE_ID

    def run(self, context, view) -> None:
        raise NotImplementedError

    def execute(self, context):
        view = get_view()
        if view is None:
            self.report({"WARNING"}, "no chunk network open")
            return {"CANCELLED"}
        view.pull_selection_from_tree()
        self.run(context, view)
        if view.status:
            self.report({"INFO"}, view.status)
        return {"FINISHED"}


class USDCHUNKS_OT_delete(_ChunkTreeOperator):
    """Delete the selected chunks, healing the exec chain across the gap."""

    bl_idname = "usd_chunks.delete"
    bl_label = "Delete Chunks"

    def run(self, context, view):
        view.delete_selected()


class USDCHUNKS_OT_duplicate(_ChunkTreeOperator):
    """Duplicate the selected chunks as new chunks, offset from the originals."""

    bl_idname = "usd_chunks.duplicate"
    bl_label = "Duplicate Chunks"

    def run(self, context, view):
        view.duplicate_selection()


class USDCHUNKS_OT_copy(_ChunkTreeOperator):
    """Copy the selection to the chunk clipboard -- the one every frontend shares."""

    bl_idname = "usd_chunks.copy"
    bl_label = "Copy Chunks"

    def run(self, context, view):
        view.copy_selection()


class USDCHUNKS_OT_cut(_ChunkTreeOperator):
    """Cut the selection to the chunk clipboard."""

    bl_idname = "usd_chunks.cut"
    bl_label = "Cut Chunks"

    def run(self, context, view):
        view.cut_selection()


class USDCHUNKS_OT_paste(_ChunkTreeOperator):
    """Paste the chunk clipboard at the cursor."""

    bl_idname = "usd_chunks.paste"
    bl_label = "Paste Chunks"

    def run(self, context, view):
        cursor = getattr(context.space_data, "cursor_location", None)
        x, y = (cursor[0] / bg._SCALE, cursor[1] / bg._SCALE) if cursor else (0.0, 0.0)
        view.paste_at(x, y)


class USDCHUNKS_OT_toggle_disable(_ChunkTreeOperator):
    """Disable or re-enable the selected chunks. A disabled chunk stays, muted."""

    bl_idname = "usd_chunks.toggle_disable"
    bl_label = "Toggle Disable"

    def run(self, context, view):
        view.toggle_disable_selected()


class USDCHUNKS_OT_collapse(_ChunkTreeOperator):
    """Collapse the selection, or the whole graph; expand again once all are collapsed."""

    bl_idname = "usd_chunks.collapse"
    bl_label = "Collapse Chunks"

    def run(self, context, view):
        view.collapse_selected_or_all()


class USDCHUNKS_OT_group(_ChunkTreeOperator):
    """Put the selection in a chunkgroup that takes its place in the exec chain."""

    bl_idname = "usd_chunks.group"
    bl_label = "Group Into Chunkgroup"

    def run(self, context, view):
        view.group_selection()


class USDCHUNKS_OT_create_view(_ChunkTreeOperator):
    """Wire a view to the selected chunk (or the end of the chain) and make it active."""

    bl_idname = "usd_chunks.create_view"
    bl_label = "Create View"

    def run(self, context, view):
        view.create_view_node()


class USDCHUNKS_PT_panel(bpy.types.Panel):
    """The node editor sidebar."""

    bl_idname = "USDCHUNKS_PT_panel"
    bl_label = "USDChunks"
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "UI"
    bl_category = "USDChunks"

    @classmethod
    def poll(cls, context):
        space = context.space_data
        return getattr(space, "tree_type", "") == bg.TREE_ID

    def draw(self, context):
        layout = self.layout
        dependencies.draw_progress(layout)
        tree = _active_tree()
        layout.operator(USDCHUNKS_OT_new_network.bl_idname, icon="ADD")
        layout.operator(USDCHUNKS_OT_open_network.bl_idname, icon="FILE_FOLDER")
        layout.operator(USDCHUNKS_OT_save_network.bl_idname, icon="FILE_TICK")
        layout.separator()
        layout.operator(USDCHUNKS_OT_auto_layout.bl_idname, icon="GRAPH")
        row = layout.row(align=True)
        row.operator(USDCHUNKS_OT_group.bl_idname, text="Group", icon="GROUP")
        row.operator(USDCHUNKS_OT_create_view.bl_idname, text="View", icon="HIDE_OFF")

        view = get_view()
        box = layout.box()
        # Where you are. A network's top level is usually one group and nothing else, so
        # without this line the editor looks empty-but-for-a-node and gives no hint that
        # there is anywhere to go.
        box.label(text=(view.context.rsplit("/", 1)[-1] if view else "-"), icon="NODETREE")
        row = box.row(align=True)
        row.operator(USDCHUNKS_OT_dive_into.bl_idname, text="Dive In", icon="FULLSCREEN_ENTER")
        row.operator(USDCHUNKS_OT_dive_out.bl_idname, text="Dive Out", icon="FULLSCREEN_EXIT")
        row = box.row(align=True)
        row.operator(USDCHUNKS_OT_publish_network.bl_idname, text="Publish", icon="EXPORT")
        row.operator(USDCHUNKS_OT_localise_network.bl_idname, text="Localise", icon="IMPORT")

        box = layout.box()
        box.label(text="Active view")
        row = box.row(align=True)
        active = view.active_slot() if view else 0
        for slot in (0, 1, 2):
            op = row.operator(USDCHUNKS_OT_set_view_slot.bl_idname,
                              text="none" if slot == 0 else str(slot),
                              depress=(slot == active))
            op.slot = slot
        # The keys, so nobody has to find them in the keymap editor: they mirror the Qt
        # and Flet graphs (V, 0-2, L, Ctrl+G) where Blender had no habit to keep, and
        # Blender's own (X, Shift+D, Ctrl+C/X/V, M, Tab) where it did.
        box.label(text="V view  0-2 slot  L layout  Ctrl+G group", icon="EVENT_V")

        if tree is not None and tree.chunk_network_path:
            layout.label(text=tree.chunk_network_path.rsplit("\\", 1)[-1], icon="FILE")
        if view is not None and view.status:
            layout.label(text=view.status)


_CLASSES = (
    USDCHUNKS_OT_open_network,
    USDCHUNKS_OT_new_network,
    USDCHUNKS_OT_save_network,
    USDCHUNKS_OT_auto_layout,
    USDCHUNKS_OT_set_view_slot,
    USDCHUNKS_OT_dive_into,
    USDCHUNKS_OT_dive_out,
    USDCHUNKS_OT_publish_network,
    USDCHUNKS_OT_localise_network,
    USDCHUNKS_OT_delete,
    USDCHUNKS_OT_duplicate,
    USDCHUNKS_OT_copy,
    USDCHUNKS_OT_cut,
    USDCHUNKS_OT_paste,
    USDCHUNKS_OT_toggle_disable,
    USDCHUNKS_OT_collapse,
    USDCHUNKS_OT_group,
    USDCHUNKS_OT_create_view,
    USDCHUNKS_PT_panel,
)

#: (keymap, keymap_item) pairs, so unregister removes exactly what register added.
_keymaps: list = []


#: (operator, key, modifiers, operator properties). Blender's own node keys keep their
#: meaning -- Tab/Ctrl+Tab for groups, X and Delete, Shift+D, Ctrl+C/X/V, M, H -- routed to
#: the chunk verbs; the graph keys the Qt and Flet frontends share (V, 0-3, L, Ctrl+G)
#: fill in where Blender had no habit to keep.
_KEYS = (
    (USDCHUNKS_OT_dive_into, "TAB", {}, {}),
    (USDCHUNKS_OT_dive_out, "TAB", {"ctrl": True}, {}),
    (USDCHUNKS_OT_delete, "X", {}, {}),
    (USDCHUNKS_OT_delete, "DEL", {}, {}),
    (USDCHUNKS_OT_duplicate, "D", {"shift": True}, {}),
    (USDCHUNKS_OT_copy, "C", {"ctrl": True}, {}),
    (USDCHUNKS_OT_cut, "X", {"ctrl": True}, {}),
    (USDCHUNKS_OT_paste, "V", {"ctrl": True}, {}),
    (USDCHUNKS_OT_toggle_disable, "M", {}, {}),
    (USDCHUNKS_OT_collapse, "H", {}, {}),
    (USDCHUNKS_OT_group, "G", {"ctrl": True}, {}),
    (USDCHUNKS_OT_create_view, "V", {}, {}),
    (USDCHUNKS_OT_auto_layout, "L", {}, {}),
    (USDCHUNKS_OT_set_view_slot, "ZERO", {}, {"slot": 0}),
    (USDCHUNKS_OT_set_view_slot, "ONE", {}, {"slot": 1}),
    (USDCHUNKS_OT_set_view_slot, "TWO", {}, {"slot": 2}),
)


def _register_keymap() -> None:
    """Bind _KEYS in the add-on keyconfig, which Blender consults before its defaults --
    so inside a chunk tree these win over node.delete, node.duplicate_move and the rest,
    and everywhere else those operators' poll fails and the key falls through to what it
    always did.
    """
    kc = bpy.context.window_manager.keyconfigs.addon
    if kc is None:
        return  # background mode: no key configs, nothing to bind to
    km = kc.keymaps.new(name="Node Editor", space_type="NODE_EDITOR")
    for op, key, mods, props in _KEYS:
        kmi = km.keymap_items.new(op.bl_idname, key, "PRESS", **mods)
        for name, value in props.items():
            setattr(kmi.properties, name, value)
        _keymaps.append((km, kmi))


@bpy.app.handlers.persistent
def _on_undo(scene, *_args) -> None:
    """Re-derive the tree from USD after any undo or redo.

    Blender's undo restores the .blend -- the node tree, every RNA value on every node --
    and knows nothing about the stage. Left alone, that is the one way the view and the
    model were still allowed to disagree: Ctrl+Z after Shift+A took the node away and the
    chunk stayed, so it came back on the next reload; Ctrl+Z after a slider edit put the
    old number on the node while the param kept the new one. This is why the chunk
    operators no longer carry the UNDO option -- an undo step that only ever half-applied
    was worse than none -- and the property edits Blender records on its own are undone
    here by rebuilding from the model, which the tree was always meant to be a view of.

    A memfile undo can also recreate the NodeTree datablock itself; `BlenderGraphView.tree`
    already looks the tree up by name for exactly that reason, so a reload here lands in
    the recreated one.
    """
    view = get_view()
    if view is None:
        return
    try:
        view.reload()
    except Exception:  # pragma: no cover - an undo past the network's open
        logger.exception("could not rebuild the chunk tree after undo")


def _unregister_keymap() -> None:
    for km, kmi in _keymaps:
        km.keymap_items.remove(kmi)
    _keymaps.clear()


def register() -> None:
    configure_logging()
    bg.register()
    for cls in dependencies.CLASSES + _CLASSES:
        bg._register_once(cls)
    # On the tree, not in a global: two trees can edit two networks, and the path has to
    # survive a .blend save so reopening the file knows what it was pointed at.
    bpy.types.NodeTree.chunk_network_path = bpy.props.StringProperty(
        name="Chunk Network", subtype="FILE_PATH", default="")
    # Also per-tree: whether _open_stage has ever run for it. See _poll_new_trees.
    bpy.types.NodeTree.chunk_network_initialized = bpy.props.BoolProperty(default=False)
    if not bpy.app.timers.is_registered(_poll_new_trees):
        # Short first interval: this is what stands between clicking "+ New" and the Add
        # menu / registry actually being ready, so 0.5s read as "Shift+A does nothing,
        # try again a moment later."
        bpy.app.timers.register(_poll_new_trees, first_interval=0.05, persistent=True)
    _register_keymap()
    if _on_undo not in bpy.app.handlers.undo_post:
        bpy.app.handlers.undo_post.append(_on_undo)
    if _on_undo not in bpy.app.handlers.redo_post:
        bpy.app.handlers.redo_post.append(_on_undo)
    viewport.register()
    gizmos.register()


def unregister() -> None:
    gizmos.unregister()
    viewport.unregister()
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _on_undo in handlers:
            handlers.remove(_on_undo)
    _unregister_keymap()
    if bpy.app.timers.is_registered(_poll_new_trees):
        bpy.app.timers.unregister(_poll_new_trees)
    dependencies.unregister()
    for cls in reversed(dependencies.CLASSES + _CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:  # pragma: no cover - already gone
            pass
    bg.unregister()
    if hasattr(bpy.types.NodeTree, "chunk_network_path"):
        del bpy.types.NodeTree.chunk_network_path
    if hasattr(bpy.types.NodeTree, "chunk_network_initialized"):
        del bpy.types.NodeTree.chunk_network_initialized
