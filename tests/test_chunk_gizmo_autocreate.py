"""Auto-creating a chunk for a prim that no chunk drives.

The rule these tests defend: **a viewport edit never lands on the prim.** A direct write is
not in the graph, so it does not survive the next cook and is not what gets saved with the
scene. Dragging a prim nothing owns therefore has to create the chunk that will own it --
the same move Houdini makes when editing geometry drops an Edit node in front of you.

Uses the real chunks/transforms/transform.py rather than a stand-in: the point is partly that its
param names and gizmo names are the ones the auto-create path reaches for.
"""
from __future__ import annotations

import importlib

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom, UsdUI

from usd_chunks import chunk, chunk_registry, chunk_utils
from usd_chunks.gizmo import runtime as chunk_gizmo
from usd_chunks.chunk_core import ChunkConfig, ChunkFlags
from usd_chunks.chunk_scene import SceneSender
from usd_chunks import chunk_stage

from chunk_stubs import attach_stub_module

transform_mod = importlib.import_module("chunks.transforms.transform")


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------

class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


class _StubModule:
    """Stands in for a registered ChunkModule.

    Only the things _create_chunk_at_tail touches, plus the module the chunks' hooks are
    looked up in. Registering the real module would drag in directory scanning and plugin
    filters, none of which is what is under test.
    """

    def __init__(self, factory):
        self._factory = factory
        self.loaded = True
        self.source_hash = None
        self.creates = 0
        # Registered under "transform", so this is where the chunks it creates find their
        # hooks (ChunkBase.get_chunk_module). Without it the chunk has no module to run:
        # execute() finds no hook and cooks nothing, silently.
        self.module = transform_mod

    def create(self):
        self.creates += 1
        c = self._factory()
        c._config = ChunkConfig(module="transform")
        return [c]

    def load(self):
        return self.create()


@pytest.fixture
def scene(monkeypatch):
    """A live registry with a stage, a cube, and `transform` available to create.

    The cube lives in a *sublayer*, which is the case that matters and the one the report
    came from ("added cube.usda as a sublayer"). Chunk layers insert at the front of the
    root's subLayerPaths, so they win over it -- put the cube in the session or root layer
    instead and it outranks every chunk, which quietly turns "the chunk drives the prim"
    into an assertion that can pass without the chunk doing anything at all.
    """
    stage = Usd.Stage.CreateInMemory()
    scene_layer = Sdf.Layer.CreateAnonymous("scene")
    stage.GetRootLayer().subLayerPaths.append(scene_layer.identifier)
    with Usd.EditContext(stage, scene_layer):
        UsdGeom.Cube.Define(stage, "/World/Cube")

    registry = chunk_registry.ChunkRegistry()
    scene_sender = _Scene(stage)
    registry.initialize(scene_sender, "pytest")
    # chunks_for_prim -- the route from a scene prim back to the chunk that touched it --
    # goes through the layer manager, and answers "nothing touched this" when it has never
    # been initialised. Reuse would look broken for the wrong reason.
    chunk_registry.ChunkLayerManager().initialize(stage)
    # The live sync is what turns an added chunk into a prim carrying `ui:gizmo:*`. Without
    # it a chunk can be created and still resolve to no handle, which is the session
    # behaviour these tests are standing in for.
    chunk_stage.ChunkStageSync().initialize(stage, scene_sender)

    module = _StubModule(transform_mod.main)
    monkeypatch.setitem(registry.registered_modules, "transform", module)

    # Creation goes through the Params-tab path, which reaches into the stage sync. Neither
    # flag should leak out of the call.
    monkeypatch.setattr(chunk_stage, "_params_tab_create", False, raising=False)
    monkeypatch.setattr(chunk_stage, "_pin_new_chunks", None, raising=False)
    monkeypatch.setattr(chunk_stage, "_create_node_pos", None, raising=False)

    yield stage, registry, module, scene_layer


def _cube(stage):
    return stage.GetPrimAtPath("/World/Cube")


def _place(stage, scene_layer, translation):
    """Put the cube somewhere via a layer that is not any chunk's -- a plain scene edit."""
    with Usd.EditContext(stage, scene_layer):
        xf = UsdGeom.Xformable(stage.GetPrimAtPath("/World/Cube"))
        xf.AddTransformOp().Set(Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(*translation)))


def _local(prim):
    matrix = UsdGeom.Xformable(prim).GetLocalTransformation(Usd.TimeCode.Default())
    return matrix[0] if isinstance(matrix, tuple) else matrix


# ---------------------------------------------------------------------------
# what counts as "already driven"
# ---------------------------------------------------------------------------

def test_a_prim_no_chunk_touches_has_no_handle(scene) -> None:
    stage, _registry, _module, scene_layer = scene
    assert chunk_gizmo.transform_desc(_cube(stage)) is None


def test_any_chunk_offering_a_transform_handle_counts(scene) -> None:
    """Not "is there a Transform chunk" -- an RBD solver declaring a transform handle owns
    the prim's placement just as much, and stacking a second chunk doing the same job on top
    of it is exactly the duplicate this check exists to prevent."""
    stage, registry, module, _scene_layer = scene

    solver = chunk.Chunk("RBDSolver", gizmo="transform")
    solver.add_param(chunk.LineEdit(
        "primpath_location", "Prim Path",
        flags=ChunkFlags.ParamFlags.LABELED | ChunkFlags.ParamFlags.PRIM_SELECTION,
    ))
    solver.get_param("primpath_location").value = "/World/Cube"
    # gizmo= on each group, as transform.py does: the chunk-level kwarg only says "present
    # these as one manipulator", so without the parts it declares nothing at all.
    for grp_name, prefix, part in (
        ("translate_grp", "t", "translate"),
        ("rotate_grp", "r", "rotate"),
        ("scale_grp", "s", "scale"),
    ):
        grp = chunk.Group(grp_name, grp_name, gizmo=part)
        for axis in "xyz":
            grp.add_param(chunk.Slider(f"{prefix}{axis}", axis.upper(), default_value=0.0))
        solver.add_param(grp)
    solver._config = ChunkConfig(module="rbd_solver")
    registry.scene.add_chunk(solver)

    # The solver has to have actually touched the prim -- that layer-stack link is how a
    # scene prim gets back to its chunk.
    with Usd.EditContext(stage, solver.get_edit_context()):
        UsdGeom.Xformable(stage.OverridePrim("/World/Cube")).AddTransformOp().Set(Gf.Matrix4d(1))

    desc = chunk_gizmo.ensure_transform_chunk(_cube(stage))

    assert desc is not None
    assert module.creates == 0, "should have reused the solver's handle, not created a chunk"


# ---------------------------------------------------------------------------
# creating one
# ---------------------------------------------------------------------------

def test_dragging_an_unowned_prim_creates_a_chunk(scene) -> None:
    stage, registry, module, _scene_layer = scene

    desc = chunk_gizmo.ensure_transform_chunk(_cube(stage))

    assert module.creates == 1
    assert desc is not None
    assert desc.type == "transform"
    created = chunk_gizmo.chunk_at_prim_path(desc.chunk_prim)
    assert created is not None
    assert created in registry.scene.get_chunks()


def test_the_new_chunk_targets_the_dragged_prim(scene) -> None:
    """Without this the chunk exists but drives nothing, and the drag goes nowhere."""
    stage, _registry, _module, scene_layer = scene

    desc = chunk_gizmo.ensure_transform_chunk(_cube(stage))
    created = chunk_gizmo.chunk_at_prim_path(desc.chunk_prim)

    assert created.get_param("primpath_location").value == "/World/Cube"


# ---------------------------------------------------------------------------
# where the node lands
# ---------------------------------------------------------------------------

def _pos(stage, prim_path):
    attr = UsdUI.NodeGraphNodeAPI(stage.GetPrimAtPath(Sdf.Path(str(prim_path)))).GetPosAttr()
    return (float(attr.Get()[0]), float(attr.Get()[1])) if attr.HasAuthoredValue() else None


def _existing_node(registry, stage, name, x):
    """A chunk already in the chain, sitting at a known place on the canvas."""
    c = chunk.Chunk(name)
    # A module of its own with no hooks: naming the real sublayer would cook it as one,
    # and a plain chunk has none of the params sublayer's execute() reads.
    attach_stub_module(c)
    registry.scene.add_chunk(c)
    prim = stage.GetPrimAtPath(Sdf.Path(chunk_stage.get_chunk_prim_path(c)))
    # Through the bookkeeping layer, as the node graph does when you drag a node. A plain
    # Set() goes to the ambient edit target, which loses to the layer _define_prim already
    # authored the position into -- so the node silently stays where it was.
    with Usd.EditContext(stage, chunk_utils.get_bookkeeping_layer(stage, c)):
        UsdUI.NodeGraphNodeAPI.Apply(prim).CreatePosAttr().Set(Gf.Vec2f(x, 0.0))
    assert _pos(stage, prim.GetPath()) == (x, 0.0), "test setup failed to move the node"
    return c, prim


def test_the_node_lands_after_the_chunk_it_was_wired_onto(scene) -> None:
    """The default placement is child-count * step, which counts a view as a child -- so the
    node is positioned by how many prims happen to exist rather than by where it sits in the
    chain, and ends up stacked behind the view instead of after the tail."""
    stage, registry, _module, _scene_layer = scene
    _tail, tail_prim = _existing_node(registry, stage, "Sublayer", x=500.0)

    desc = chunk_gizmo.ensure_transform_chunk(_cube(stage))

    tail_x = _pos(stage, tail_prim.GetPath())[0]
    assert _pos(stage, desc.chunk_prim)[0] == tail_x + chunk_stage._NODE_STEP


def test_no_tail_means_no_opinion(scene) -> None:
    """Nothing to sit after: leave the default placement alone rather than inventing one."""
    stage, _registry, _module, _scene_layer = scene

    desc = chunk_gizmo.ensure_transform_chunk(_cube(stage))

    assert _pos(stage, desc.chunk_prim) is not None  # still positioned, just not by us


def test_creation_does_not_leak_the_position_override(scene) -> None:
    """_create_node_pos is a module-level global on chunk_stage. Left set, every subsequent
    create in the session would land on top of this one."""
    stage, registry, _module, _scene_layer = scene
    _existing_node(registry, stage, "Sublayer", x=500.0)

    chunk_gizmo.ensure_transform_chunk(_cube(stage))

    assert chunk_stage._create_node_pos is None


def test_repeated_calls_reuse_the_first_chunk(scene) -> None:
    """The host's commit runs on every mouse-move, so this is called once per frame of a
    drag -- with no cook in between that the caller controls. Anything less than idempotent
    here stacks a Transform node per frame onto the same object.

    It holds because targeting the prim is itself a param change, so the chunk has cooked
    and put a spec in the prim's layer stack before the first call even returns.
    """
    stage, _registry, module, _scene_layer = scene

    descs = [chunk_gizmo.ensure_transform_chunk(_cube(stage)) for _ in range(5)]

    assert module.creates == 1
    assert len({d.chunk_prim for d in descs}) == 1


def test_create_false_reports_the_gap_without_filling_it(scene) -> None:
    """A caller that only wants to know whether a handle exists must not cause a side effect
    the size of a new node."""
    stage, _registry, module, _scene_layer = scene

    assert chunk_gizmo.ensure_transform_chunk(_cube(stage), create=False) is None
    assert module.creates == 0


def test_an_invalid_prim_creates_nothing(scene) -> None:
    stage, _registry, module, _scene_layer = scene

    assert chunk_gizmo.ensure_transform_chunk(stage.GetPrimAtPath("/nope")) is None
    assert module.creates == 0


# ---------------------------------------------------------------------------
# not moving anything by appearing
# ---------------------------------------------------------------------------

def test_the_new_chunk_starts_empty_and_moves_nothing(scene) -> None:
    """No seeding, deliberately.

    A chunk used to REPLACE the prim's transform, so a new one had to be pre-loaded with the
    prim's current placement or the object jumped to the origin -- and only the auto-created
    path did that, so the same node behaved differently depending on whether it was dragged
    into existence or added by hand. Now that it owns its own op and composes on top, an
    untouched chunk contributes identity: the params start empty and the prim does not move.
    """
    stage, _registry, _module, scene_layer = scene
    _place(stage, scene_layer, (5.0, 6.0, 7.0))

    desc = chunk_gizmo.ensure_transform_chunk(_cube(stage))
    created = chunk_gizmo.chunk_at_prim_path(desc.chunk_prim)
    created.recook()

    assert [created.get_param(n).value for n in ("tx", "ty", "tz")] == [0.0, 0.0, 0.0]
    assert Gf.IsClose(_local(_cube(stage)).ExtractTranslation(), Gf.Vec3d(5, 6, 7), 1e-6)


def test_seeding_survives_the_first_cook(scene) -> None:
    """The params agreeing is only half of it -- what the viewport shows is the cook."""
    stage, _registry, _module, scene_layer = scene
    _place(stage, scene_layer, (5.0, 6.0, 7.0))

    desc = chunk_gizmo.ensure_transform_chunk(_cube(stage))
    chunk_gizmo.chunk_at_prim_path(desc.chunk_prim).recook()

    assert Gf.IsClose(_local(_cube(stage)).ExtractTranslation(), Gf.Vec3d(5.0, 6.0, 7.0), 1e-6)


def test_a_prim_at_the_origin_is_left_alone(scene) -> None:
    stage, _registry, _module, scene_layer = scene

    desc = chunk_gizmo.ensure_transform_chunk(_cube(stage))
    created = chunk_gizmo.chunk_at_prim_path(desc.chunk_prim)

    assert [created.get_param(n).value for n in ("tx", "ty", "tz")] == [0.0, 0.0, 0.0]
    assert [created.get_param(n).value for n in ("sx", "sy", "sz")] == [1.0, 1.0, 1.0]


# ---------------------------------------------------------------------------
# the edit actually flows
# ---------------------------------------------------------------------------

def test_a_drag_through_the_new_chunk_moves_the_prim(scene) -> None:
    """End to end: the prim had no chunk, and after this it is placed by params that a save
    will write out -- which is the whole reason for auto-creating rather than writing it."""
    stage, _registry, _module, scene_layer = scene

    desc = chunk_gizmo.ensure_transform_chunk(_cube(stage))
    chunk_gizmo.apply_bindings(
        desc.chunk_prim, desc.name, Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(1.0, 2.0, 3.0))
    )
    created = chunk_gizmo.chunk_at_prim_path(desc.chunk_prim)
    created.recook()

    assert [created.get_param(n).value for n in ("tx", "ty", "tz")] == [1.0, 2.0, 3.0]
    assert Gf.IsClose(_local(_cube(stage)).ExtractTranslation(), Gf.Vec3d(1.0, 2.0, 3.0), 1e-6)


def test_creation_does_not_leak_the_params_tab_flags(scene) -> None:
    """They are module-level globals on chunk_stage. Left set, every subsequent node-graph
    create would silently auto-chain and auto-pin."""
    stage, _registry, _module, scene_layer = scene

    chunk_gizmo.ensure_transform_chunk(_cube(stage))

    assert chunk_stage._params_tab_create is False
    assert chunk_stage._pin_new_chunks is None
