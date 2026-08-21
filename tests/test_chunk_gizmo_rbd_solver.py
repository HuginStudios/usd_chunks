"""Gizmos on the RBD solver, and the resolve gap it exposed.

Two offset handles -- Position Offset and Rotation Offset -- because those place the body
before the sim runs, which is precisely the thing you want to drag in the viewport rather
than type into a collapsed section.

Getting them to appear needed a change in chunk_gizmo. resolve_gizmos found chunks via the
layer stack, i.e. chunks that had already authored something on the prim. rbd_solver is
button-driven and authors nothing until you press Run, so its offset handles -- the ones
you want BEFORE a simulation -- would not have shown up until after the first one.
"""
from __future__ import annotations

import importlib

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom

from usd_chunks import chunk, chunk_gizmo, chunk_registry
from usd_chunks.chunk_core import ChunkConfig, SceneSender
from usd_chunks.plugins import chunk_usd

rbd_mod = importlib.import_module("scripts.rbd_solver")


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture
def solver():
    stage = Usd.Stage.CreateInMemory()
    scene_layer = Sdf.Layer.CreateAnonymous("scene")
    stage.GetRootLayer().subLayerPaths.append(scene_layer.identifier)
    with Usd.EditContext(stage, scene_layer):
        UsdGeom.Cube.Define(stage, "/World/Cube")

    registry = chunk_registry.ChunkRegistry()
    sender = _Scene(stage)
    registry.initialize(sender, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_usd.ChunkStageSync().initialize(stage, sender)

    node = rbd_mod.main()
    node._config = ChunkConfig(module="rbd_solver")
    node._module = rbd_mod
    sender.add_chunk(node)
    node.get_param("prim_path").value = "/World/Cube"
    return stage, node


def _by_name(node):
    return {g.name: g for g in chunk_gizmo.get_gizmos(node)}


# ---------------------------------------------------------------------------
# declaration
# ---------------------------------------------------------------------------

def test_the_solver_declares_both_offset_handles(solver) -> None:
    _stage, node = solver
    gizmos = _by_name(node)

    assert gizmos["offset_translate"].type == "translate"
    assert gizmos["offset_rotate"].type == "rotate"


def test_a_vector_drives_one_attribute_not_three(solver) -> None:
    """A Vector is stored as a single vector-typed attribute, so the driver is one entry --
    unlike transform.py's groups, where three sliders make three."""
    _stage, node = solver

    assert len(_by_name(node)["offset_translate"].driver) == 1


def test_no_whole_chunk_transform_is_invented(solver) -> None:
    """There is no scale offset, so a chunk-level gizmo="transform" could not resolve. The
    two handles must stay separate rather than being absorbed into one manipulator."""
    _stage, node = solver

    assert "transform" not in _by_name(node)


# ---------------------------------------------------------------------------
# resolving from the scene prim
# ---------------------------------------------------------------------------

def test_the_handles_resolve_before_any_simulation_has_run(solver) -> None:
    """The gap this exposed. rbd_solver authors nothing until Run is pressed, so the
    layer-stack route finds no chunk for the cube -- and the offsets are exactly what you
    want to set beforehand."""
    stage, _node = solver
    cube = stage.GetPrimAtPath("/World/Cube")
    assert not chunk_gizmo.chunks_for_prim(cube), "solver should not have authored anything yet"

    types = {d.type for d in chunk_gizmo.resolve_gizmos(cube)}

    assert {"translate", "rotate"} <= types


def test_the_anchor_points_at_the_target_prim(solver) -> None:
    stage, _node = solver
    cube = stage.GetPrimAtPath("/World/Cube")

    for desc in chunk_gizmo.resolve_gizmos(cube):
        assert [str(a) for a in desc.anchor] == ["/World/Cube"]


def test_a_prim_the_solver_does_not_target_gets_nothing(solver) -> None:
    """The fallback matches on the anchor, so it must not offer handles for every prim."""
    stage, _node = solver
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        UsdGeom.Cube.Define(stage, "/World/Other")

    assert chunk_gizmo.resolve_gizmos(stage.GetPrimAtPath("/World/Other")) == []


def test_retargeting_moves_the_handles(solver) -> None:
    stage, node = solver
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        UsdGeom.Cube.Define(stage, "/World/Other")

    node.get_param("prim_path").value = "/World/Other"

    assert chunk_gizmo.resolve_gizmos(stage.GetPrimAtPath("/World/Cube")) == []
    assert chunk_gizmo.resolve_gizmos(stage.GetPrimAtPath("/World/Other"))


# ---------------------------------------------------------------------------
# write-back
# ---------------------------------------------------------------------------

def test_dragging_the_position_handle_writes_the_offset(solver) -> None:
    stage, node = solver
    cube = stage.GetPrimAtPath("/World/Cube")
    desc = next(d for d in chunk_gizmo.resolve_gizmos(cube) if d.type == "translate")

    chunk_gizmo.apply_bindings(desc.chunk_prim, desc.name, Gf.Vec3d(1.0, 2.0, 3.0))

    assert node.get_param("offset_translate").value == pytest.approx((1.0, 2.0, 3.0))


def test_dragging_the_rotation_handle_writes_degrees(solver) -> None:
    stage, node = solver
    cube = stage.GetPrimAtPath("/World/Cube")
    desc = next(d for d in chunk_gizmo.resolve_gizmos(cube) if d.type == "rotate")

    chunk_gizmo.apply_bindings(desc.chunk_prim, desc.name, Gf.Vec3d(0.0, 90.0, 0.0))

    assert node.get_param("offset_rotate").value == pytest.approx((0.0, 90.0, 0.0))


def test_the_two_handles_do_not_write_each_other(solver) -> None:
    stage, node = solver
    cube = stage.GetPrimAtPath("/World/Cube")
    translate = next(d for d in chunk_gizmo.resolve_gizmos(cube) if d.type == "translate")

    chunk_gizmo.apply_bindings(translate.chunk_prim, translate.name, Gf.Vec3d(5.0, 5.0, 5.0))

    assert node.get_param("offset_rotate").value == pytest.approx((0.0, 0.0, 0.0))


# ---------------------------------------------------------------------------
# what the anchor fallback must NOT resurrect
# ---------------------------------------------------------------------------

def test_a_disabled_solver_offers_no_handles(solver) -> None:
    """The layer-stack route gets this free -- no spec in the stack, no handle. Anchors do
    not: they keep pointing at the prim regardless of whether the chunk is contributing, so
    the fallback has to check explicitly or a disabled chunk keeps its handles on screen."""
    stage, node = solver
    assert chunk_gizmo.resolve_gizmos(_cube_of(stage)), "no handles to begin with"

    node.set_state(chunk.ChunkFlags.State.DISABLED)

    assert chunk_gizmo.resolve_gizmos(_cube_of(stage)) == []


def test_a_muted_solver_layer_offers_no_handles(solver) -> None:
    stage, node = solver
    assert chunk_gizmo.resolve_gizmos(_cube_of(stage)), "no handles to begin with"

    stage.MuteLayer(node.get_edit_context().identifier)

    assert chunk_gizmo.resolve_gizmos(_cube_of(stage)) == []


def _cube_of(stage):
    return stage.GetPrimAtPath("/World/Cube")


# ---------------------------------------------------------------------------
# the solver owns the prim, so nothing may be stacked on top of it
# ---------------------------------------------------------------------------

def test_the_solver_blocks_the_auto_created_transform_chunk(solver, monkeypatch) -> None:
    """Reported live: a cube targeted by an RBD solver was dragged and got a Transform chunk
    anyway.

    The ownership test looked for a `transform`-type handle, and the solver declares
    `translate` and `rotate` -- it has no scale offset, so it cannot form a whole-chunk
    transform at all. It plainly owns where that body starts, so testing only for
    `transform` reported the prim as unowned and stacked a duplicate on top.
    """
    stage, _node = solver
    registry = chunk_registry.ChunkRegistry()

    class _StubTransform:
        loaded = True
        creates = 0

        def create(self):
            _StubTransform.creates += 1
            transform_mod = importlib.import_module("scripts.transform")
            c = transform_mod.main()
            c._config = ChunkConfig(module="transform")
            c._module = transform_mod
            return [c]

        load = create

    module = _StubTransform()
    monkeypatch.setitem(registry.registered_modules, "transform", module)
    monkeypatch.setattr(chunk_usd, "_params_tab_create", False, raising=False)
    monkeypatch.setattr(chunk_usd, "_pin_new_chunks", None, raising=False)
    monkeypatch.setattr(chunk_usd, "_create_node_pos", None, raising=False)

    chunk_gizmo.ensure_transform_chunk(_cube_of(stage))

    assert module.creates == 0, "auto-create fired on a prim the solver already owns"


def test_placement_gizmos_sees_the_part_handles(solver) -> None:
    stage, _node = solver

    types = {d.type for d in chunk_gizmo.placement_gizmos(_cube_of(stage))}

    assert types == {"translate", "rotate"}


def test_there_is_still_no_whole_transform_handle_to_return(solver) -> None:
    """Owned, but by parts. A caller wanting one matrix-taking handle correctly gets None
    and has to drive the parts individually -- which is what the host contract says."""
    stage, _node = solver

    assert chunk_gizmo.transform_desc(_cube_of(stage)) is None


# ---------------------------------------------------------------------------
# an offset is a delta here too
# ---------------------------------------------------------------------------

def _place(stage, y):
    """A body already placed, which is the case that exposes absolute-vs-relative. On a body
    at the origin the two coincide, which is how this passed a live test earlier."""
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        UsdGeom.Xformable(stage.GetPrimAtPath("/World/Cube")).AddTransformOp().Set(
            Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(0.0, y, 0.0))
        )


def test_dragging_the_position_handle_on_a_placed_body_writes_a_delta(solver) -> None:
    """Same shape as the offset_transform bug: writing the raw value in makes the offset the
    placement, so a body at y=5 dragged to y=8 would start at 13."""
    stage, node = solver
    _place(stage, 5.0)
    desc = next(d for d in chunk_gizmo.resolve_gizmos(_cube_of(stage)) if d.type == "translate")

    chunk_gizmo.apply_bindings(desc.chunk_prim, desc.name, Gf.Vec3d(0.0, 8.0, 0.0))

    assert node.get_param("offset_translate").value == pytest.approx((0.0, 3.0, 0.0))


def test_dragging_a_body_at_the_origin_is_unchanged(solver) -> None:
    """The case that already worked, kept so the delta conversion cannot regress it."""
    stage, node = solver
    desc = next(d for d in chunk_gizmo.resolve_gizmos(_cube_of(stage)) if d.type == "translate")

    chunk_gizmo.apply_bindings(desc.chunk_prim, desc.name, Gf.Vec3d(1.0, 2.0, 3.0))

    assert node.get_param("offset_translate").value == pytest.approx((1.0, 2.0, 3.0))


def test_the_rotation_handle_is_a_delta_too(solver) -> None:
    stage, node = solver
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        UsdGeom.Xformable(stage.GetPrimAtPath("/World/Cube")).AddTransformOp().Set(
            Gf.Matrix4d(1.0).SetRotate(Gf.Rotation(Gf.Vec3d(0, 1, 0), 30.0))
        )
    desc = next(d for d in chunk_gizmo.resolve_gizmos(_cube_of(stage)) if d.type == "rotate")

    chunk_gizmo.apply_bindings(desc.chunk_prim, desc.name, Gf.Vec3d(0.0, 90.0, 0.0))

    assert node.get_param("offset_rotate").value == pytest.approx((0.0, 60.0, 0.0), abs=1e-4)
