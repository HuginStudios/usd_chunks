"""Edit Target is resolved from exec-chain topology, and downstream edits migrate.

The Edit Target chunk publishes the layer it points at onto its own prim
(chunk:editTargetLayer). A chunk's effective edit layer is the one published by the
nearest Edit Target chunk reachable by walking inputs:chunk upstream (session layer if
there is none) — see chunk_usd.nearest_upstream_edit_layer. When that resolved target
changes (an Edit Target repoints, or is removed), the downstream chunk's bookkeeping AND
driven content move to the new layer, and its old edits are removed from the old one
(chunk_usd.reconcile_edit_targets).

Chunks are wired into the exec chain the way the Params tab auto-chains them
(prev -> new via inputs:chunk); the _make helper flips _params_tab_create to build that
linear chain, since the topology — not execution order — is what now decides the target.
"""
from __future__ import annotations

import importlib

from pxr import Sdf, Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import ChunkConfig, SceneSender
from usd_chunks.plugins.chunk_usd import initialize as init_chunk_usd, get_chunk_prim_path
import usd_chunks.plugins.chunk_usd as chunk_usd


class _DummyScene(SceneSender):
    def __init__(self):
        super().__init__()
        self._stage = None

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


def _make(scene, mod, module_name):
    c = mod.main()
    c._config = ChunkConfig(module=module_name)
    c._module = mod
    # Auto-chain prev -> new (Params-tab behavior) so the exec chain that resolution walks
    # actually exists.
    prev = chunk_usd._params_tab_create
    chunk_usd._params_tab_create = True
    try:
        scene.add_chunk(c)
    finally:
        chunk_usd._params_tab_create = prev
    return c


def _reachable_layers(root: Sdf.Layer) -> set[str]:
    """Identifiers of every layer transitively sublayered under root (root included)."""
    seen: set[str] = set()
    stack = [root]
    while stack:
        layer = stack.pop()
        if layer is None or layer.identifier in seen:
            continue
        seen.add(layer.identifier)
        for path in layer.subLayerPaths:
            sub = Sdf.Layer.Find(path)
            if sub:
                stack.append(sub)
    return seen


class TestEditTargetMigration:
    def setup_method(self):
        self.create_mod = importlib.import_module("scripts.create_layer")
        self.edit_mod = importlib.import_module("scripts.edit_target")
        self.prim_mod = importlib.import_module("scripts.prim")

    def _setup(self):
        scene = _DummyScene()
        chunk_registry.ChunkRegistry().initialize(scene, "pytest")
        stage = Usd.Stage.CreateInMemory()
        scene._stage = stage
        init_chunk_usd(stage, scene)
        return scene, stage

    def _create_layer(self, scene, name):
        c = _make(scene, self.create_mod, f"cl_{name}")
        c.get_param("layer_name").value = name
        c.execute()
        return c, Sdf.Layer.Find(c.get_param("_new_layer_id").value)

    def _driven_holder(self, stage, prim_path):
        """The layer authoring the def for prim_path, or None."""
        for layer in stage.GetLayerStack(includeSessionLayers=True):
            spec = layer.GetPrimAtPath(prim_path)
            if spec and spec.specifier == Sdf.SpecifierDef:
                return layer
        return None

    def test_downstream_nests_under_edit_target(self):
        """A chunk downstream of Edit Target(->layerA) authors both its bookkeeping and its
        driven content into layerA, not the session layer."""
        scene, stage = self._setup()
        _, layer_a = self._create_layer(scene, "layerA")
        et = _make(scene, self.edit_mod, "edit_target")
        et.get_param("edit_target").value = "layerA"
        et.execute()
        c = _make(scene, self.prim_mod, "prim")
        c.execute()

        cpath = Sdf.Path(get_chunk_prim_path(c))
        assert layer_a.GetPrimAtPath(cpath) is not None
        assert stage.GetSessionLayer().GetPrimAtPath(cpath) is None

        dpath = Sdf.Path(c.get_param("primpath_location").value)
        holder = self._driven_holder(stage, dpath)
        assert holder is not None
        assert holder.identifier in _reachable_layers(layer_a)

    def test_repoint_migrates_downstream_and_removes_old(self):
        """Repointing the Edit Target from layerA to layerB moves the downstream chunk's
        bookkeeping and driven content to layerB, removing both from layerA."""
        scene, stage = self._setup()
        _, layer_a = self._create_layer(scene, "layerA")
        _, layer_b = self._create_layer(scene, "layerB")
        et = _make(scene, self.edit_mod, "edit_target")
        et.get_param("edit_target").value = "layerA"
        et.execute()
        c = _make(scene, self.prim_mod, "prim")
        c.execute()

        cpath = Sdf.Path(get_chunk_prim_path(c))
        dpath = Sdf.Path(c.get_param("primpath_location").value)
        assert layer_a.GetPrimAtPath(cpath) is not None
        assert self._driven_holder(stage, dpath).identifier in _reachable_layers(layer_a)

        et.get_param("edit_target").value = "layerB"
        et.execute()

        # Bookkeeping moved to layerB, gone from layerA.
        assert layer_b.GetPrimAtPath(cpath) is not None
        assert layer_a.GetPrimAtPath(cpath) is None
        # Driven content moved to layerB, gone from layerA.
        holder = self._driven_holder(stage, dpath)
        assert holder.identifier in _reachable_layers(layer_b)
        assert holder.identifier not in _reachable_layers(layer_a)

    def test_selection_and_homing_survive_reload(self, tmp_path):
        """A saved graph of chained Edit Targets reloads with each selection intact and each
        downstream chunk re-homed under the right layer — the reload path re-applies the
        stored selections (which a Menu drops on load until its layer is recreated),
        upstream-first, from a pre-read snapshot so a cascade can't corrupt a sibling's value.
        """
        export_cg = importlib.import_module("scripts.export_chunkgroup")
        scene, stage = self._setup()
        # CreateLayer(A) -> EditTarget(->A) -> CreateLayer(B) -> EditTarget(->B) -> Prim.
        # Real module names (create_layer/edit_target/prim) so the registry can rebuild the
        # chunks from info:chunkType on reload.
        self._create_layer(scene, "A")
        et_a = _make(scene, self.edit_mod, "edit_target")
        et_a.get_param("edit_target").value = "A"
        et_a.execute()
        self._create_layer(scene, "B")
        et_b = _make(scene, self.edit_mod, "edit_target")
        et_b.get_param("edit_target").value = "B"
        et_b.execute()
        prim = _make(scene, self.prim_mod, "prim")
        prim.execute()

        graph_path = tmp_path / "graph.usda"
        cg = _make(scene, export_cg, "export_chunkgroup")
        cg.get_param("output_file").value = str(graph_path)
        export_cg.export(cg.get_param("export"), None, None)

        # Reload into a fresh scene/stage. Reset the layer-manager singleton first so the
        # build phase's now-dead anon layers don't leak into the reloaded stack (the real
        # reload path resets it too).
        chunk_registry.ChunkLayerManager().reset()
        scene2 = _DummyScene()
        chunk_registry.ChunkRegistry().initialize(scene2, "pytest")
        stage2 = Usd.Stage.Open(str(graph_path))
        scene2._stage = stage2
        chunk_registry.ChunkLayerManager().initialize(stage2)
        chunk_usd.ChunkStageSync().initialize(stage2, scene2)

        chunks2 = list(scene2.get_chunks())
        et_values = {
            c.get_param("edit_target").value
            for c in chunks2 if chunk_usd.is_edit_target_chunk(c)
        }
        assert et_values == {"A", "B"}  # both selections survived, not reset to Session Layer

        # The Prim chunk (downstream of the ->B Edit Target) must re-home under layer B, not
        # the session layer.
        prim2 = next(c for c in chunks2 if (c.get_config() and c.get_config().module) == "prim")
        ppath = Sdf.Path(get_chunk_prim_path(prim2))
        layer_b = next(
            l for l in stage2.GetLayerStack(includeSessionLayers=True) if l.GetDisplayName() == "B"
        )
        assert layer_b.GetPrimAtPath(ppath) is not None
        assert stage2.GetSessionLayer().GetPrimAtPath(ppath) is None

    def test_deleting_edit_target_falls_back_to_session(self):
        """Deleting the Edit Target leaves the downstream chunk with no upstream target, so
        its bookkeeping falls back to the session layer (removed from layerA)."""
        scene, stage = self._setup()
        _, layer_a = self._create_layer(scene, "layerA")
        et = _make(scene, self.edit_mod, "edit_target")
        et.get_param("edit_target").value = "layerA"
        et.execute()
        c = _make(scene, self.prim_mod, "prim")
        c.execute()

        cpath = Sdf.Path(get_chunk_prim_path(c))
        assert layer_a.GetPrimAtPath(cpath) is not None

        et.request_delete()

        assert stage.GetSessionLayer().GetPrimAtPath(cpath) is not None
        assert layer_a.GetPrimAtPath(cpath) is None
