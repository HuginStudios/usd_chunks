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

from usd_chunks import chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks import chunk_stage
from usd_chunks.plugins import chunk_blender_nodegraph as bg
from usd_chunks.plugins.chunk_blender_addon import viewport

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
        road really is a dead end (which is what this docstring used to be about, before
        viewport picking existed) -- this instead reads
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
        render settings. Ignored rather than raised so a render chunk can still cook."""
        return None

    def get_current_frame(self) -> float:
        return float(bpy.context.scene.frame_current) if bpy.context.scene else 0.0

    def get_end_frame(self) -> float:
        return float(bpy.context.scene.frame_end) if bpy.context.scene else 0.0

    def should_install_package(self, package_name: str) -> bool:
        """Never install into Blender's Python behind the user's back.

        Blender's interpreter is shared by every add-on and is replaced wholesale on
        upgrade, so a silent pip install is both invasive and lost. A chunk whose
        requirements are missing is skipped by the registry, which is the safe failure.
        """
        logger.info("usd_chunks: %s is required by a chunk but will not be auto-installed",
                    package_name)
        return False


#: One scene and one view per Blender session. A second node tree re-points them rather
#: than making a second set, because the chunk registry is a singleton keyed to one scene --
#: two live scenes would have the registry answering for whichever initialised last.
_scene: "BlenderScene | None" = None


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
    chunk_stage.ChunkStageSync().initialize(stage, _scene)
    # Node classes are generated from the registry, and the registry only discovers chunks
    # once a scene exists -- so this cannot happen in register(), where there is no scene
    # yet and every class would be generated from an empty library. Idempotent, so opening
    # a second network costs a dict lookup per already-registered type.
    made = bg.register_chunk_types()
    if made:
        logger.info("usd_chunks: %d chunk node type(s) registered", len(made))
    chunk_stage.load_chunks(stage, _scene)

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
# it -- reported as "it doesn't seem to execute" (the Add menu comes from the registry,
# which only a real _open_stage populates), and switching away and back "loses" the
# ORIGINAL network's connections because active_view() is a single global and now points
# at this second, empty view instead.
#
# Tried bpy.msgbus.subscribe_rna on SpaceNodeEditor.node_tree first -- it did not fire
# reliably, because a msgbus callback runs with whatever bpy.context Blender happens to
# have at notification time, which is not guaranteed to be the node editor whose property
# actually changed. A timer callback's context has the same problem, so this does not
# trust bpy.context.space_data either -- it scans every NODE_EDITOR area directly, the way
# viewport.py's _redraw() already does for VIEW_3D areas.

def _uninitialized_chunk_trees() -> list:
    """Every ChunkNetwork tree currently shown in a node editor that _open_stage has never
    touched -- i.e. created by Blender's own "+ New" rather than one of our operators."""
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
    `node_tree` plus any node-group diving depth. Without this, New/Open created or opened
    a tree that nothing on screen ever pointed at: the editor kept showing whatever tree
    (or nothing) it already had pinned, `_active_tree`'s fallback picked among however many
    ChunkNetwork trees exist in the file by iteration order, and a graph could be built
    into a tree the user was not looking at -- reported as "the grid is there but there are
    no nodes on it".
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
            self.report({"ERROR"}, "could not open %s" % self.filepath)
            return {"CANCELLED"}
        tree = _active_tree()
        if tree is None:
            tree = bpy.data.node_groups.new("ChunkNetwork", bg.TREE_ID)
        tree.chunk_network_path = self.filepath
        _show_tree(context, tree)
        _open_stage(stage, tree=tree)
        self.report({"INFO"}, "opened %s" % self.filepath)
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
    bl_options = {"REGISTER", "UNDO"}

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

    slot: bpy.props.IntProperty(default=1, min=0, max=3)

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
        tree = _active_tree()
        layout.operator(USDCHUNKS_OT_new_network.bl_idname, icon="ADD")
        layout.operator(USDCHUNKS_OT_open_network.bl_idname, icon="FILE_FOLDER")
        layout.operator(USDCHUNKS_OT_save_network.bl_idname, icon="FILE_TICK")
        layout.separator()
        layout.operator(USDCHUNKS_OT_auto_layout.bl_idname, icon="GRAPH")

        view = get_view()
        box = layout.box()
        # Where you are. A network's top level is usually one group and nothing else, so
        # without this line the editor looks empty-but-for-a-node and gives no hint that
        # there is anywhere to go.
        box.label(text=(view.context.rsplit("/", 1)[-1] if view else "-"), icon="NODETREE")
        row = box.row(align=True)
        row.operator(USDCHUNKS_OT_dive_into.bl_idname, text="Dive In", icon="FULLSCREEN_ENTER")
        row.operator(USDCHUNKS_OT_dive_out.bl_idname, text="Dive Out", icon="FULLSCREEN_EXIT")

        box = layout.box()
        box.label(text="Active view")
        row = box.row(align=True)
        active = view.active_slot() if view else 0
        for slot in (0, 1, 2, 3):
            op = row.operator(USDCHUNKS_OT_set_view_slot.bl_idname,
                              text="none" if slot == 0 else str(slot),
                              depress=(slot == active))
            op.slot = slot

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
    USDCHUNKS_PT_panel,
)

#: (keymap, keymap_item) pairs, so unregister removes exactly what register added.
_keymaps: list = []


def _register_keymap() -> None:
    """Tab dives in, Ctrl+Tab dives out -- the keys Blender's own node editor uses for
    entering and leaving a node group, so the habit carries over. The operators poll on
    our tree type, so in a shader editor Tab still means what it always did."""
    kc = bpy.context.window_manager.keyconfigs.addon
    if kc is None:
        return  # background mode: no key configs, nothing to bind to
    km = kc.keymaps.new(name="Node Editor", space_type="NODE_EDITOR")
    _keymaps.append((km, km.keymap_items.new(USDCHUNKS_OT_dive_into.bl_idname, "TAB", "PRESS")))
    _keymaps.append((km, km.keymap_items.new(USDCHUNKS_OT_dive_out.bl_idname, "TAB", "PRESS", ctrl=True)))


def _unregister_keymap() -> None:
    for km, kmi in _keymaps:
        km.keymap_items.remove(kmi)
    _keymaps.clear()


def register() -> None:
    bg.register()
    for cls in _CLASSES:
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
    viewport.register()


def unregister() -> None:
    viewport.unregister()
    _unregister_keymap()
    if bpy.app.timers.is_registered(_poll_new_trees):
        bpy.app.timers.unregister(_poll_new_trees)
    for cls in reversed(_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:  # pragma: no cover - already gone
            pass
    bg.unregister()
    if hasattr(bpy.types.NodeTree, "chunk_network_path"):
        del bpy.types.NodeTree.chunk_network_path
    if hasattr(bpy.types.NodeTree, "chunk_network_initialized"):
        del bpy.types.NodeTree.chunk_network_initialized
