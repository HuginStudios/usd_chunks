"""A chunk created inside a reachable group must still trigger a recook.

_could_affect_view_cook skips a recook when nothing that changed is reachable from the
active view, on the reasoning that "a brand-new disconnected node can't be reachable".
That holds for a node dropped into the graph, but not for a chunk created *inside* a
group: nothing in the outer graph points at it, yet it is part of that group's cooked
content and its layer still has to be ranked into the group's tier (the descendant
promotion in recook_for_view).

AMDMaterialXLibrary builds its Reference and BindMaterial children from a button
callback, so they arrive exactly this way. Skipping the recook left their layers appended
weakest -- below the Sublayer feeding the group -- so the material they authored lost to
the binding the referenced asset already carried, and the assigned material never showed.
"""
from __future__ import annotations

import pytest
from pxr import Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks import chunk_stage
from usd_chunks.chunk_stage import (
    ChunkStageSync, connect_exec, get_chunk_prim_path, initialize as _chunk_usd_init,
)
from usd_chunks.frontends.qt.nodegraph import ChunkGraphView


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


@pytest.fixture
def wired(qapp):
    """Sublayer -> Group -> View, with two chunks created inside the group."""
    stage = Usd.Stage.CreateInMemory()

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)

    def mk(key):
        mod = reg.get_chunk_module(key)
        cs = mod.create() if mod.loaded else mod.load()
        c = cs[0]
        scene.add_chunk(c)
        return c

    sub, group, view, stray = mk("sublayer"), mk("chunkgroup"), mk("view"), mk("transform")
    qapp.processEvents()

    def create_child(key, parent):
        # Exactly AMDMaterialXLibrary._create_child.
        with ChunkStageSync()._chunk_prim_edit_context():
            mod = reg.get_chunk_module(key)
            created = mod.create() if mod.loaded else mod.load()
            child = created[0] if created else None
            if child:
                parent.add_param(child)
        return child

    ref = create_child("reference", group)
    bind = create_child("bind_material", group)
    qapp.processEvents()

    sc = reg.scene
    connect_exec(stage, sc, get_chunk_prim_path(sub), get_chunk_prim_path(group))
    connect_exec(stage, sc, get_chunk_prim_path(group), get_chunk_prim_path(view))
    qapp.processEvents()

    return stage, sc, view, group, ref, bind, stray


def test_the_group_children_are_not_themselves_reachable(wired):
    """The precondition: this is why the gate used to skip them."""
    stage, scene, view, _group, ref, bind, _stray = wired

    cook_set = set(chunk_stage.view_cook_list(view, stage, scene))

    assert ref not in cook_set
    assert bind not in cook_set


def test_a_chunk_created_inside_a_reachable_group_forces_a_recook(wired):
    stage, scene, view, _group, ref, bind, _stray = wired

    assert ChunkGraphView._could_affect_view_cook([ref], stage, scene, view), (
        "a chunk created inside a reachable group is part of that group's cooked content; "
        "skipping the recook leaves its layer unranked and weakest"
    )
    assert ChunkGraphView._could_affect_view_cook([bind], stage, scene, view)


def test_an_unrelated_disconnected_chunk_still_skips_the_recook(wired):
    """The optimisation this gate exists for must survive."""
    stage, scene, view, _group, _ref, _bind, stray = wired

    assert not ChunkGraphView._could_affect_view_cook([stray], stage, scene, view)


def test_no_active_view_still_skips(wired):
    stage, scene, _view, _group, ref, _bind, _stray = wired

    assert not ChunkGraphView._could_affect_view_cook([ref], stage, scene, None)


def test_a_groups_children_outrank_the_sublayer_feeding_it(wired, qapp):
    """The invariant the gate exists to protect, end to end.

    The children author the material and its binding; the Sublayer brings in an asset that
    already carries its own binding for the same mesh. If the children's layers are weaker,
    the asset's binding wins and the assigned material silently never appears -- which is
    the reported symptom, visible in usdview's Layer Stack as Reference/BindMaterial sitting
    below the Sublayer.
    """
    stage, scene, view, group, ref, bind, _stray = wired
    lm = chunk_registry.ChunkLayerManager()

    chunk_stage.recook_for_view(stage, scene, None, view)
    qapp.processEvents()

    def strength(chunk):
        """Index in the graph layer's sublayers; 0 is strongest."""
        layer = lm.get_layer(chunk)
        assert layer is not None, f"{chunk.name} has no layer"
        for g in lm.graph_layers.values():
            if layer.identifier in g.subLayerPaths:
                return list(g.subLayerPaths).index(layer.identifier)
        raise AssertionError(f"{chunk.name}'s layer is in no graph layer")

    sub = next(c for c in scene.get_chunks() if chunk_stage.get_chunk_prim_path(c).endswith("Sublayer"))

    assert strength(ref) < strength(sub), "Reference must win over the sublayer it consumes"
    assert strength(bind) < strength(sub), "BindMaterial must win over the sublayer it consumes"
