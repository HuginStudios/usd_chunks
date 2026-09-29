"""Regression: reloading a saved multi-chunk scene (Sublayer -> Transform -> Layer ->
Edit Target -> Scatter Points) re-executed the last chunk in the chain several times more than
necessary. Root cause: load_chunks' "recreate each existing named top-level context" phase
(scene.add_context, run before chunk_notice._loading_stage is ever set) can itself trigger
layer creation (via CHUNK_LOADED -> get_edit_context() -> get_or_create_layer()), whose
subLayerPaths.insert() fires a global STAGE_CONTENTS_CHANGED -- which an Edit Target chunk's
globally-listening refresh handler reacts to by reassigning its menu_items, reactively
re-executing everything downstream of it (chunk_core.py's value_changed -> execute()) before a
single chunk has even been restored yet. chunk_notice._loading_stage now covers this whole
window, not just each individual _add_prim_mods call, so value_changed's existing
_loading_stage guard suppresses this cascade like it already does for every other premature
reactive path during a bulk load.
"""
from __future__ import annotations

from pxr import Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import export_chunks, initialize as _chunk_usd_init
from usd_chunks.chunk_stage import initialize as _chunk_usd_init2


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _make_scene(stage: Usd.Stage) -> SceneSender:
    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)
    return scene


def _make(scene: SceneSender, module_name: str):
    mod = chunk_registry.ChunkRegistry().get_chunk_module(module_name)
    chunks = mod.create() if mod.loaded else mod.load()
    primary = chunks[0]
    scene.add_chunk(primary)
    return primary


def test_reload_bounds_downstream_reexecution_across_the_full_chain(tmp_path) -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)

    sub = _make(scene, "sublayer")
    tr = _make(scene, "transform")
    cl = _make(scene, "layer")
    et = _make(scene, "edit_target")
    sc = _make(scene, "scatter_points")

    tr.add_dependency(sub)
    cl.add_dependency(tr)
    et.add_dependency(cl)
    sc.add_dependency(et)

    et_menu = et.get_param("edit_target")
    if et_menu is not None and "(Follow Upstream Layer)" in (et_menu.menu_items or []):
        et_menu.value = "(Follow Upstream Layer)"

    path = str(tmp_path / "chain.usda")
    export_chunks(scene, path)

    stage2 = Usd.Stage.Open(path)

    class _StageScene2(_DummyScene):
        def get_stage(self):
            return stage2

    scene2 = _StageScene2()
    chunk_registry.ChunkRegistry().initialize(scene2, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage2)

    reg = chunk_registry.ChunkRegistry()
    mod_wrapper = reg.get_chunk_module("scatter_points")
    real_execute = mod_wrapper.module.execute
    call_count = {"n": 0}

    def traced_execute(node):
        call_count["n"] += 1
        return real_execute(node)

    mod_wrapper.module.execute = traced_execute
    try:
        _chunk_usd_init2(stage2, scene2)  # runs load_chunks internally
    finally:
        mod_wrapper.module.execute = real_execute

    # Legitimate sources still bounded: one root-driven solve, plus the settling
    # reconcile_edit_targets does once after restore_edit_target_selections resolves real
    # Edit Target values. Anything beyond that is the context-creation-phase cascade this
    # test guards against, which ran into the dozens before it was fixed -- the bound is
    # there to catch that shape, not to hold the count to an exact number.
    #
    # Was 5, while a load still migrated bookkeeping OUT of the opened file's root layer.
    # That move settled the chain in one fewer cook, but it settled it by deleting the spec
    # from the layer it had just read: Sdf caches layers per process, so the next
    # Usd.Stage.Open of the same path got an emptied layer and restored only the context
    # (see test_double_load.py). Protecting the root layer costs this one extra cook, which
    # is the right side of that trade.
    assert call_count["n"] <= 6, (
        "reloading the full chain must not re-execute the last chunk in it repeatedly from "
        f"premature, pre-restore reactive cascades -- got {call_count['n']}"
    )
