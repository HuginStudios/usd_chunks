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
from usd_chunks.plugins import chunk_usd
from usd_chunks.plugins import chunk_blender_nodegraph as bg

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
        """Blender's selection is of Blender objects, not USD prims.

        A USD prim imported into Blender becomes an object and loses the prim path, so
        there is no honest mapping back. Empty rather than a guess -- a PRIM_SELECTION
        param that silently filled itself with the wrong paths would be worse than one the
        user types into.
        """
        return []

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


def _open_stage(stage, context_path=None) -> None:
    """Point the session at `stage` and rebuild the tree from it."""
    global _scene
    _scene = BlenderScene(stage)
    chunk_registry.ChunkRegistry().initialize(_scene, "blender")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_usd.ChunkStageSync().initialize(stage, _scene)
    # Node classes are generated from the registry, and the registry only discovers chunks
    # once a scene exists -- so this cannot happen in register(), where there is no scene
    # yet and every class would be generated from an empty library. Idempotent, so opening
    # a second network costs a dict lookup per already-registered type.
    made = bg.register_chunk_types()
    if made:
        logger.info("usd_chunks: %d chunk node type(s) registered", len(made))
    chunk_usd.load_chunks(stage, _scene)

    tree = _active_tree()
    chunks = {chunk_usd.get_chunk_prim_path(c): c for c in _scene.get_chunks()}
    # chunk_graph.default_context, not peek_root_context: after load_chunks the scene has
    # no root context to peek at, and CHUNKS_ROOT shows the default chunkgroup and none of
    # the work inside it. The engine already knows which graph a file wants opened.
    context = context_path or bg.chunk_graph.default_context(stage)
    view = bg.BlenderGraphView(stage, _scene, tree=tree, context=context)
    view.chunks = chunks
    view.reload()
    # A context with no view cooks nothing, so this is not cosmetic -- see ensure_view.
    view.ensure_view()


def _active_tree():
    space = getattr(bpy.context, "space_data", None)
    tree = getattr(space, "edit_tree", None) if space else None
    if tree is not None and tree.bl_idname == bg.TREE_ID:
        return tree
    return next((t for t in bpy.data.node_groups if t.bl_idname == bg.TREE_ID), None)


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
        _open_stage(stage)
        self.report({"INFO"}, "opened %s" % self.filepath)
        return {"FINISHED"}


class USDCHUNKS_OT_new_network(bpy.types.Operator):
    """Start a new node tree seeded with one ChunkGroup.

    An entirely empty stage has no chunk prim anywhere under it, so the graph opens with
    nothing to select, dive into, or hang a view off -- every other way of starting a
    network (open an existing file, or Shift+A into one already open) lands on a stage
    that has at least one chunk. Seeding with scripts/chunkgroup.py's entry, the same
    module Shift+A offers, keeps a brand new network on that same footing -- and gives the
    user a ChunkGroup, which is what this button actually hands them.
    """

    bl_idname = "usd_chunks.new_network"
    bl_label = "New ChunkGroup"
    bl_options = {"REGISTER"}

    def execute(self, context):
        bpy.data.node_groups.new("ChunkNetwork", bg.TREE_ID)
        _open_stage(Usd.Stage.CreateInMemory())
        view = get_view()
        entry = next(
            (e for e in bg.chunk_graph.menu_entries() if e.module.module_name == "chunkgroup"),
            None)
        if view is not None and entry is not None:
            view.create_chunk(entry, 0.0, 0.0)
        self.report({"INFO"}, "new chunkgroup")
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
    USDCHUNKS_PT_panel,
)


def register() -> None:
    bg.register()
    for cls in _CLASSES:
        bg._register_once(cls)
    # On the tree, not in a global: two trees can edit two networks, and the path has to
    # survive a .blend save so reopening the file knows what it was pointed at.
    bpy.types.NodeTree.chunk_network_path = bpy.props.StringProperty(
        name="Chunk Network", subtype="FILE_PATH", default="")


def unregister() -> None:
    for cls in reversed(_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:  # pragma: no cover - already gone
            pass
    bg.unregister()
    if hasattr(bpy.types.NodeTree, "chunk_network_path"):
        del bpy.types.NodeTree.chunk_network_path
