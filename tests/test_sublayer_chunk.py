from __future__ import annotations

from pxr import Usd

from usd_chunks import chunk as _chunk
from usd_chunks import chunk_registry, chunk_core
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins.chunk_usd import export_chunks, initialize as _chunk_usd_init


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
    what load_chunks's own explicit, correctly-ordered `chunk.execute()` call (once per
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
