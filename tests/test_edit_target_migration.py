"""Edit Target is resolved from exec-chain topology, and downstream edits migrate.

The Edit Target chunk publishes the layer it points at onto its own prim
(chunk:editTargetLayer). A chunk's effective edit layer is the one published by the
nearest Edit Target chunk reachable by walking inputs:chunk upstream (session layer if
there is none) -- see chunk_stage.nearest_upstream_edit_layer. When that resolved target
changes (an Edit Target repoints, or is removed), the downstream chunk's bookkeeping AND
driven content move to the new layer, and its old edits are removed from the old one
(chunk_stage.reconcile_edit_targets).

Chunks are wired into the exec chain the way the Params tab auto-chains them
(prev -> new via inputs:chunk); the _make helper flips _params_tab_create to build that
linear chain, since the topology -- not execution order -- is what now decides the target.
"""
from __future__ import annotations

import gc
import importlib

from pxr import Sdf, Usd

from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_core import ChunkConfig
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import initialize as init_chunk_usd, get_chunk_prim_path
import usd_chunks.chunk_stage as chunk_stage


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
    prev = chunk_stage._params_tab_create
    chunk_stage._params_tab_create = True
    try:
        scene.add_chunk(c)
    finally:
        chunk_stage._params_tab_create = prev
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
        self.create_mod = importlib.import_module("chunks.usd.create_layer")
        self.edit_mod = importlib.import_module("chunks.usd.edit_target")
        self.prim_mod = importlib.import_module("chunks.usd.prim")
        self._scenes = []

    def teardown_method(self):
        """Delete each scene's chunks instead of just dropping the reference to the scene.

        create_layer and edit_target both register their refresh handlers with
        listen_globally=True, and a chunk left alive keeps answering those notices -- on
        whatever stage is current, since ChunkBase.get_stage() resolves the live one rather
        than the stage the chunk was built on. Left to leak, one test's chunks go on
        authoring layers into the next test's stage.
        """
        for scene in self._scenes:
            for ctx in list(scene.chunks.get_my_params()):
                ctx.request_delete()
        self._scenes = []
        # request_delete() alone doesn't guarantee these chunks (and their global
        # listeners) are actually gone before the next test starts -- they sit in
        # reference cycles (chunk <-> owner/scene) that only CPython's cycle collector
        # can break, and that doesn't run deterministically on plain refcounting alone.
        # Force it so leftover listeners can't intermittently fire during a later test.
        gc.collect()

    def _setup(self):
        scene = _DummyScene()
        chunk_registry.ChunkRegistry().initialize(scene, "pytest")
        stage = Usd.Stage.CreateInMemory()
        scene._stage = stage
        init_chunk_usd(stage, scene)
        self._scenes.append(scene)
        return scene, stage

    def _create_layer(self, scene, name, module_name=None):
        # module_name is what gets written to the prim as info:chunkType. The reload test
        # passes the real "create_layer" so the registry can rebuild a working chunk from it
        # and the layer this authors exists again on the other side of a reload; the rest
        # pass nothing and get a per-layer stand-in, which no module backs -- they only care
        # about where edits land, and a name the registry can't resolve keeps their chunks
        # from re-running module code as unrelated notices cascade past.
        c = _make(scene, self.create_mod, module_name or f"cl_{name}")
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
        downstream chunk re-homed under the right layer -- the reload path re-applies the
        stored selections (which a Menu drops on load until its layer is recreated),
        upstream-first, from a pre-read snapshot so a cascade can't corrupt a sibling's value.
        """
        export_cg = importlib.import_module("chunks.core.export_chunkgroup")
        scene, stage = self._setup()
        # CreateLayer(A) -> EditTarget(->A) -> CreateLayer(B) -> EditTarget(->B) -> Prim.
        # Real module names (create_layer/edit_target/prim) so the registry can rebuild the
        # chunks from info:chunkType on reload.
        self._create_layer(scene, "A", "create_layer")
        et_a = _make(scene, self.edit_mod, "edit_target")
        et_a.get_param("edit_target").value = "A"
        et_a.execute()
        self._create_layer(scene, "B", "create_layer")
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
        self._scenes.append(scene2)  # torn down with the rest, see teardown_method
        chunk_registry.ChunkRegistry().initialize(scene2, "pytest")
        stage2 = Usd.Stage.Open(str(graph_path))
        scene2._stage = stage2
        chunk_registry.ChunkLayerManager().initialize(stage2)
        chunk_stage.ChunkStageSync().initialize(stage2, scene2)

        chunks2 = list(scene2.get_chunks())
        et_values = {
            c.get_param("edit_target").value
            for c in chunks2 if chunk_stage.is_edit_target_chunk(c)
        }
        assert et_values == {"A", "B"}  # both selections survived, not reset to Session Layer

        # Regression companion to the selection check above: Create Layer's OWN params
        # (layer_name in particular) must also survive, and there must be exactly one
        # layer per Create Layer chunk -- not a second, orphaned one falling back to the
        # chunk's default name because layer_name came back empty (live-reported: the
        # Layer Stack panel showing a duplicate "CreateLayer" entry alongside the correctly
        # named "A"/"B" layers after a reload).
        create_layer_chunks = [
            c for c in chunks2 if (c.get_config() and c.get_config().module) == "create_layer"
        ]
        assert {c.get_param("layer_name").value for c in create_layer_chunks} == {"A", "B"}
        for name in ("A", "B"):
            # GetLayerStack() can legitimately list the same Sdf.Layer more than once when
            # it's reachable via more than one composition arc -- unique by identifier
            # rather than raw list length, or a layer genuinely shared between two valid
            # arcs would misreport as a duplicate.
            matches = {
                lyr.identifier for lyr in stage2.GetLayerStack(includeSessionLayers=True)
                if lyr.GetDisplayName() == name
            }
            assert len(matches) == 1, f"expected exactly one layer named {name!r}, found {matches}"
        assert not any(
            lyr.GetDisplayName() == "CreateLayer"
            for lyr in stage2.GetLayerStack(includeSessionLayers=True)
        ), "no orphaned layer should fall back to the chunk's default name"

        # The Prim chunk (downstream of the ->B Edit Target) must re-home under layer B, not
        # the session layer.
        prim2 = next(c for c in chunks2 if (c.get_config() and c.get_config().module) == "prim")
        ppath = Sdf.Path(get_chunk_prim_path(prim2))
        layer_b = next(
            lyr
            for lyr in stage2.GetLayerStack(includeSessionLayers=True)
            if lyr.GetDisplayName() == "B"
        )
        assert layer_b.GetPrimAtPath(ppath) is not None
        assert stage2.GetSessionLayer().GetPrimAtPath(ppath) is None

        # Every chunk prim must survive the reload's migrations still *defined* -- the node
        # graph iterates Usd.PrimIsDefined, so one demoted to a bare `over` stops drawing.
        undefined = [
            str(c2path)
            for c2 in chunks2
            if not stage2.GetPrimAtPath(c2path := Sdf.Path(get_chunk_prim_path(c2))).IsDefined()
        ]
        assert not undefined

    def test_migration_promotes_a_bare_over_at_the_destination(self):
        """A chunk prim whose destination layer already holds a bare `over` (left by an
        ordinary attr write that landed there while the `def` still lived elsewhere) must
        still end up defined.

        _migrate_chunk_bookkeeping deletes the source specs, so if the destination keeps its
        `over` specifier and empty typeName the prim becomes undefined and typeless across
        the whole stack -- invisible to every Usd.PrimIsDefined traversal, which is what the
        node graph iterates, so the chunk silently stops drawing a node.
        """
        scene, stage = self._setup()
        _, layer_a = self._create_layer(scene, "layerA")
        c = _make(scene, self.prim_mod, "prim")
        c.execute()

        cpath = Sdf.Path(get_chunk_prim_path(c))
        assert stage.GetSessionLayer().GetPrimAtPath(cpath) is not None
        # Seed the destination with the bare `over` an attr write would have left there.
        Sdf.CreatePrimInLayer(layer_a, cpath)
        assert layer_a.GetPrimAtPath(cpath).specifier == Sdf.SpecifierOver

        assert chunk_stage._migrate_chunk_bookkeeping(stage, str(cpath), layer_a) is True

        spec = layer_a.GetPrimAtPath(cpath)
        assert spec.specifier == Sdf.SpecifierDef
        assert spec.typeName == "Chunk"
        prim = stage.GetPrimAtPath(cpath)
        assert prim.IsDefined()
        assert prim.GetTypeName() == "Chunk"

    def test_deleting_a_context_never_leaves_a_gutted_chunk(self):
        """Tearing down a context must delete its sub-chunks, not reach past them and delete
        their params.

        The teardown's own PARAM_REMOVED / layer-stack notices cascade straight back into the
        graph -- an Edit Target's refresh callback re-runs, reconcile_edit_targets re-executes
        chunks. Any chunk that lost its params without being asked to delete is still live to
        all of that, and every chunk script crashes on a get_param() that now returns None.
        """
        scene, stage = self._setup()
        self._create_layer(scene, "layerA")
        et = _make(scene, self.edit_mod, "edit_target")
        et.get_param("edit_target").value = "layerA"
        et.execute()
        prim = _make(scene, self.prim_mod, "prim")
        prim.execute()

        chunks = list(scene.get_chunks())
        assert len(chunks) == 3

        for ctx in list(scene.chunks.get_my_params()):
            ctx.request_delete()

        # Marked before its own params go, so nothing re-executes it half-dismantled.
        assert all(getattr(c, "_deleting", False) for c in chunks)
        assert not scene.get_chunks()

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


class TestFollowUpstreamUsesExecEdgesOnly:
    """Which Create Layer "Follow Upstream" means is decided by the exec chain alone.

    reconcile_dag_dependencies mirrors param wires into the Python DAG as well, so
    chunk.get_dependencies() no longer answers "what precedes me". A param wire means a
    value flows; letting one pick the Create Layer would silently change where every
    downstream chunk authors, on a signal the user never meant as ordering.
    """

    def setup_method(self):
        self.edit_mod = importlib.import_module("chunks.usd.edit_target")
        self._scenes = []

    def teardown_method(self):
        self._scenes.clear()
        gc.collect()

    def _setup(self):
        scene = _DummyScene()
        chunk_registry.ChunkRegistry().initialize(scene, "pytest")
        stage = Usd.Stage.CreateInMemory()
        scene._stage = stage
        init_chunk_usd(stage, scene)
        self._scenes.append(scene)
        return scene, stage

    def _pair(self, scene):
        """A Create Layer stand-in and an Edit Target stand-in, deliberately NOT chained.

        Bare chunks rather than the real modules: this pins the topology question, and a
        stand-in keeps unrelated notices from re-running module code.
        """
        cl = chunk.Chunk("cl")
        cl._config = ChunkConfig(module="cl_standin")
        cl.add_param(chunk.LineEdit("_new_layer_id", default_value=""))
        cl.add_param(chunk.LineEdit("layer_name", default_value="L"))
        scene.add_chunk(cl)

        et = chunk.Chunk("et")
        et._config = ChunkConfig(module="et_standin")
        et.add_param(chunk.LineEdit("probe", default_value=""))
        scene.add_chunk(et)
        return cl, et

    def test_a_param_wired_create_layer_is_not_followed(self):
        scene, _stage = self._setup()
        cl, et = self._pair(scene)

        et.get_param("probe").value = "@cl.layer_name@"

        assert any(d is cl for _, d in et.get_dependencies()), (
            "precondition: the param wire was reconciled into a DAG edge"
        )
        assert self.edit_mod._find_upstream_create_layer(et) is None

    def test_an_exec_wired_create_layer_is_followed(self):
        scene, _stage = self._setup()
        cl, et = self._pair(scene)

        et.add_dependency(cl)  # authors inputs:chunk

        assert self.edit_mod._find_upstream_create_layer(et) is cl
