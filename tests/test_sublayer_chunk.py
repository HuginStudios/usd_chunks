from __future__ import annotations

from pxr import Usd

from usd_chunks import chunk as _chunk
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
    class _SyncScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _SyncScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)
    return scene


def _make_sublayer(scene: SceneSender, path: str):
    mod = chunk_registry.ChunkRegistry().get_chunk_module("sublayer")
    chunks = mod.create() if mod.loaded else mod.load()
    primary = chunks[0]
    scene.add_chunk(primary)

    list_group = primary.get_param("imports", _chunk.is_inst(_chunk.ListGroup))
    list_group.size = 1
    list_group.get_indexed_param("sublayer_path", 0).value = path
    return primary


def test_creating_live_authors_sublayer_paths_without_crashing() -> None:
    """Regression: reported live -- creating a Sublayer chunk and setting a path crashed
    with 'execute() missing 2 required positional arguments', because the ATTRIBUTE_CHANGED
    callback was named `execute`, colliding with the framework's reserved top-level reactive
    hook (chunk_notice.py calls `module.execute(node)`, one positional arg, whenever the DAG
    solves). Also guards the fix for THAT fix's own follow-up regression (see the reload
    test below): the reserved hook now exists for real (needed for reload), so this also
    confirms live creation still works cleanly, not just reload."""
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    primary = _make_sublayer(scene, "C:/dummy/scene.usda")

    edit_layer = primary.get_edit_context()
    assert edit_layer is not None
    assert list(edit_layer.subLayerPaths) == ["C:/dummy/scene.usda"]


def test_reload_reauthors_sublayer_paths(tmp_path) -> None:
    """Regression: reported live (PR review comment) -- does execute() need to be called
    for this to load from a chunkgroup? Yes: export_chunks only copies the imports param's
    own VALUE (a prim attribute), never the chunk's own edit layer's subLayerPaths (a
    layer-level field, not a prim attribute) -- so reopening a saved chunkgroup showed the
    correct path in the field but never actually brought the sublayer back in, unless
    something re-runs the "write subLayerPaths from imports" logic on load. That's exactly
    what load_chunks's own explicit, correctly-ordered `chunk.recook()` call (once per
    top-level chunk, after every chunk's own bookkeeping layer is registered) is for --
    which only does anything if the module has a real, single-arg execute(node) hook."""
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    _make_sublayer(scene, "C:/dummy/scene.usda")

    path = str(tmp_path / "sublayer_chunkgroup.usda")
    export_chunks(scene, path)

    stage2 = Usd.Stage.Open(path)
    scene2 = _make_scene(stage2)  # this alone already triggers load_chunks internally

    reloaded = [c for c in scene2.get_chunks() if c.get_config() and c.get_config().module == "sublayer"][0]
    list_group = reloaded.get_param("imports", _chunk.is_inst(_chunk.ListGroup))
    assert list_group.get_indexed_param("sublayer_path", 0).value == "C:/dummy/scene.usda"

    edit_layer = reloaded.get_edit_context()
    assert edit_layer is not None
    assert list(edit_layer.subLayerPaths) == ["C:/dummy/scene.usda"]


def test_reload_reauthors_sublayer_paths_without_repeated_execution(tmp_path) -> None:
    """Regression: reported live -- reloading a saved scene printed "Updated stage with
    sublayer" (this chunk's own execute() hook) many times per load instead of once or twice,
    and the same repeated-re-execution pattern showed up for every chunk downstream of a
    Sublayer in the exec chain. Root cause: restoring a list-group row's saved value (e.g.
    this chunk's own imports:sublayer_path) goes through ProxyParam.__setattr__'s "value" case
    (chunk_core.py), a different code path than ChunkBase.value_changed -- the only place
    chunk_notice._loading_stage was checked -- so it fired a real, immediate execute() during
    the bulk restore, once per restored list-group attribute, well before load_chunks' own
    single, correctly-ordered "trigger the DAG solve for root nodes" pass ever got a chance to
    run for real. Fixing that gap alone regressed the test above (this chunk's layer never got
    spliced into the stage at all): the repeated premature firing had been accidentally
    load-bearing, since it was the only thing that happened to register the layer early enough
    for _add_prim_mods' sublayer re-sort pass to include it. Both gaps are now closed together
    -- the notify is suppressed during the bulk restore, and _add_prim_mods pre-registers every
    eligible chunk's layer before the re-sort pass runs, not just chunks whose restore happens
    to trigger it as a side effect.

    Asserts <=2, not ==1: a SEPARATE, legitimate mechanism (reconcile_edit_targets, called once
    right after load_chunks in ChunkStageSync.initialize) can add one more -- export_chunks
    flattens bookkeeping straight into the exported file's root layer, so on first reload,
    before anything has consolidated it into the reopened stage's own session layer,
    _migrate_chunk_bookkeeping finds it split across both and moves it, re-executing as a
    documented side effect of a real, one-time migration. That's bounded and legitimate; the
    bug this test guards against is UNBOUNDED, repeated re-execution of the same chunk."""
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    _make_sublayer(scene, "C:/dummy/scene.usda")

    path = str(tmp_path / "sublayer_chunkgroup.usda")
    export_chunks(scene, path)

    stage2 = Usd.Stage.Open(path)

    class _StageScene2(_DummyScene):
        def get_stage(self):
            return stage2

    scene2 = _StageScene2()
    chunk_registry.ChunkRegistry().initialize(scene2, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage2)

    real_sublayer_module = chunk_registry.ChunkRegistry().get_chunk_module("sublayer").module
    call_count = {"n": 0}
    orig_execute = real_sublayer_module.execute

    def traced_execute(node):
        call_count["n"] += 1
        return orig_execute(node)

    real_sublayer_module.execute = traced_execute
    try:
        _chunk_usd_init2(stage2, scene2)  # runs load_chunks internally
    finally:
        real_sublayer_module.execute = orig_execute

    assert call_count["n"] <= 2, (
        f"reloading a saved Sublayer chunk must not repeatedly re-execute its reserved hook -- "
        f"expected at most 2 (the load itself, plus reconcile_edit_targets' one-time bookkeeping "
        f"migration), got {call_count['n']}"
    )

    reloaded = [c for c in scene2.get_chunks() if c.get_config() and c.get_config().module == "sublayer"][0]
    edit_layer = reloaded.get_edit_context()
    assert edit_layer is not None
    assert list(edit_layer.subLayerPaths) == ["C:/dummy/scene.usda"]
