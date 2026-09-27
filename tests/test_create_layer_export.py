"""Tests for CreateLayer/Edit Target layer nesting and Export USD's Export All Layers.

Builds a shot layer via Create Layer, points Edit Target at it, then creates
light/fx/layout sublayers (also via Create Layer) while that Edit Target is
active — they should nest directly under the shot layer, not the session
layer. Export All Layers should then write session/shot/light/fx/layout each
to their own file, with shot.usda's subLayerPaths rewritten to the sibling
filenames instead of left as session-local anon: identifiers.
"""
from __future__ import annotations

import importlib

from pxr import Sdf, Usd

from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_naming import CHUNKS_ROOT
from usd_chunks.chunk_stage import get_chunk_prim_path
from usd_chunks.chunk_core import ChunkConfig, SceneSender
from usd_chunks.chunk_stage import initialize as init_chunk_usd


class _DummyScene(SceneSender):
    def __init__(self):
        super().__init__()
        self._stage = None

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


def _load(module_path: str):
    return importlib.import_module(module_path)


def _make(scene, mod, module_name):
    c = mod.main()
    c._config = ChunkConfig(module=module_name)
    c._module = mod
    # Edit Target is now resolved from exec-chain topology (nearest upstream Edit Target
    # chunk), not execution order — so chunks must actually be wired into the chain, the
    # way the Params tab auto-chains them (prev -> new via inputs:chunk). Flip the same
    # flag the Params-tab create path sets so add_chunk builds that linear chain here too.
    import usd_chunks.chunk_stage as _cu
    prev = _cu._params_tab_create
    _cu._params_tab_create = True
    try:
        scene.add_chunk(c)
    finally:
        _cu._params_tab_create = prev
    return c


def _setup_stage():
    scene = _DummyScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    stage = Usd.Stage.CreateInMemory()
    scene._stage = stage
    init_chunk_usd(stage, scene)
    return scene, stage


class TestCreateLayerNestingAndExport:
    def setup_method(self):
        self.create_mod = _load("scripts.usd.create_layer")
        self.edit_mod = _load("scripts.usd.edit_target")
        self.export_mod = _load("scripts.core.export_usd")

    def _create_layer(self, scene, name, module_name):
        c = _make(scene, self.create_mod, module_name)
        c.get_param("layer_name").value = name
        c.execute()
        return c

    def test_light_fx_layout_nest_under_shot(self):
        scene, stage = _setup_stage()

        shot = self._create_layer(scene, "shot", "create_layer_shot")
        shot_id = shot.get_param("_new_layer_id").value
        shot_layer = Sdf.Layer.Find(shot_id)
        assert shot_layer is not None

        edit_target = _make(scene, self.edit_mod, "edit_target")
        # edit_target now stores/matches display names, not raw (session-ephemeral)
        # Sdf identifiers — see edit_target.py's _refresh_edit_target/_resolve_layer.
        edit_target.get_param("edit_target").value = "shot"
        edit_target.execute()

        light = self._create_layer(scene, "light", "create_layer_light")
        fx = self._create_layer(scene, "fx", "create_layer_fx")
        layout = self._create_layer(scene, "layout", "create_layer_layout")

        for name, c in [("light", light), ("fx", fx), ("layout", layout)]:
            layer_id = c.get_param("_new_layer_id").value
            assert layer_id in shot_layer.subLayerPaths, (
                f"{name} layer should be a direct sublayer of shot — Edit Target "
                f"was pointed at shot when it was created"
            )
            layer = Sdf.Layer.Find(layer_id)
            assert layer is not None and layer.GetDisplayName() == name

        session = stage.GetSessionLayer()
        assert shot_layer.identifier in session.subLayerPaths

    def test_refresh_after_layer_skips_unless_in_after_layer_mode(self):
        """_refresh_after_layer is registered listen_globally=True, so it fires for
        EVERY Create Layer instance on ANY stage change -- including an unrelated
        sibling's own edits. Its menu_items write used to run unconditionally, which
        reactively re-triggered this chunk's own execute() as a side effect of that
        unrelated activity, regardless of whether "after_layer" (hidden/unused outside
        "After Layer" mode) was even relevant. That cascade -- confirmed live -- turned
        one clean recook pass into hundreds of spurious re-executions across just a
        few sibling Create Layer chunks, scrambling Prepend/Append/Index ordering by
        making the "winner" depend on notice-dispatch timing instead of graph position.
        Guard: it must now no-op entirely unless insert_mode is actually "After Layer"."""
        scene, stage = _setup_stage()
        c = self._create_layer(scene, "shot", "create_layer_shot")
        assert c.get_param("insert_mode").value == "Append"  # default, not "After Layer"

        stale_items = ["stale", "items"]
        c.get_param("after_layer").menu_items = stale_items

        self.create_mod._refresh_after_layer(c, None, None)
        assert list(c.get_param("after_layer").menu_items) == stale_items, (
            'after_layer must be left untouched while insert_mode != "After Layer"'
        )

        # Reactively refreshes via _insert_mode_changed's explicit call into
        # _refresh_after_layer when switching INTO "After Layer" mode.
        c.get_param("insert_mode").value = "After Layer"
        assert list(c.get_param("after_layer").menu_items) != stale_items, (
            'switching into "After Layer" mode must actually refresh the menu'
        )

    def test_nested_edit_targets_bookkeeping_follows_and_chunkgroup_gathers_all(self, tmp_path):
        """Reproduces the nested workflow this architecture is for: create a layer, set
        Edit Target there, create another layer (nests under the first), set Edit
        Target to the new one too, add a Prim chunk (nests under the second).

        Chunk-prim bookkeeping now follows Edit Target (chunk_utils.get_bookkeeping_layer)
        instead of always the session layer — Prim's own bookkeeping prim should end up
        directly inside layer2, not session. Export ChunkGroup must still gather every
        chunk's prim into one file regardless of how scattered they are (it rebuilds
        each fresh from the live Python objects, not by copying wherever specs currently
        sit). A Baked export of layer2 alone must contain only its own real content, no
        chunk-graph bookkeeping mixed in even though it's directly inside layer2 too.
        """
        prim_mod = _load("scripts.usd.prim")
        export_chunkgroup_mod = _load("scripts.core.export_chunkgroup")
        scene, stage = _setup_stage()

        layer1 = self._create_layer(scene, "layer1", "create_layer_1")
        layer1_id = layer1.get_param("_new_layer_id").value

        edit_target1 = _make(scene, self.edit_mod, "edit_target_1")
        edit_target1.get_param("edit_target").value = "layer1"
        edit_target1.execute()

        layer2 = self._create_layer(scene, "layer2", "create_layer_2")
        layer2_id = layer2.get_param("_new_layer_id").value
        layer2_sdf = Sdf.Layer.Find(layer2_id)
        assert layer2_sdf is not None
        assert layer2_id in Sdf.Layer.Find(layer1_id).subLayerPaths

        edit_target2 = _make(scene, self.edit_mod, "edit_target_2")
        edit_target2.get_param("edit_target").value = "layer2"
        edit_target2.execute()

        prim = _make(scene, prim_mod, "prim")
        prim.execute()

        session = stage.GetSessionLayer()
        prim_path = Sdf.Path(get_chunk_prim_path(prim))
        assert layer2_sdf.GetPrimAtPath(prim_path) is not None
        assert session.GetPrimAtPath(prim_path) is None

        # Export ChunkGroup gathers every chunk's bookkeeping into one file regardless
        # of how scattered their live specs currently are.
        graph_path = tmp_path / "graph.usda"
        chunkgroup_chunk = _make(scene, export_chunkgroup_mod, "export_chunkgroup")
        chunkgroup_chunk.get_param("output_file").value = str(graph_path)
        export_chunkgroup_mod.export(chunkgroup_chunk.get_param("export"), None, None)
        graph = Sdf.Layer.FindOrOpen(str(graph_path))
        assert graph is not None
        assert graph.GetPrimAtPath(get_chunk_prim_path(prim)) is not None
        assert graph.GetPrimAtPath(get_chunk_prim_path(edit_target1)) is not None
        assert graph.GetPrimAtPath(get_chunk_prim_path(edit_target2)) is not None

        # Baked export of layer2 alone: real content only, no chunk-graph bookkeeping —
        # even though bookkeeping is now directly inside layer2 itself.
        export_usd_chunk = _make(scene, self.export_mod, "export_usd")
        # Bake is off by default now; the Bake all switch flips every row's checkbox.
        export_usd_chunk.get_param("bake_all").value = True
        export_usd_chunk.get_param("output_dir").value = str(tmp_path)
        self.export_mod.export_all(export_usd_chunk.get_param("export_all"), None, None)
        layer2_out = Sdf.Layer.FindOrOpen(str(tmp_path / "layer2.usda"))
        assert layer2_out is not None

        def _collect_type_names(spec):
            names = {spec.typeName} if spec.typeName else set()
            for child in spec.nameChildren.values():
                names |= _collect_type_names(child)
            return names

        assert "Chunk" not in _collect_type_names(layer2_out.pseudoRoot)

    def test_export_all_layers_writes_shot_and_sublayers_separately(self, tmp_path):
        scene, stage = _setup_stage()

        self._create_layer(scene, "shot", "create_layer_shot")
        edit_target = _make(scene, self.edit_mod, "edit_target")
        # edit_target now stores/matches display names, not raw (session-ephemeral)
        # Sdf identifiers — see edit_target.py's _refresh_edit_target/_resolve_layer.
        edit_target.get_param("edit_target").value = "shot"
        edit_target.execute()

        self._create_layer(scene, "light", "create_layer_light")
        self._create_layer(scene, "fx", "create_layer_fx")
        self._create_layer(scene, "layout", "create_layer_layout")

        export_chunk = _make(scene, self.export_mod, "export_usd")
        export_chunk.get_param("output_dir").value = str(tmp_path)
        export_param = export_chunk.get_param("export_all")
        self.export_mod.export_all(export_param, None, None)

        # Stage.GetRootLayer() is a separate real layer from the session layer (even
        # for an in-memory stage) and export_all writes it too — under whatever
        # display name it happens to have, not asserted here.
        written = {p.name for p in tmp_path.iterdir()}
        expected = {"SessionLayer.usda", "shot.usda", "light.usda", "fx.usda", "layout.usda"}
        assert expected <= written
        assert len(written) == len(expected) + 1, f"unexpected extra export files: {written - expected}"

        shot_out = Sdf.Layer.FindOrOpen(str(tmp_path / "shot.usda"))
        assert shot_out is not None
        # subLayerPaths must be rewritten from session-local anon: identifiers to the
        # sibling export filenames — not left dangling as in-session-only references.
        assert set(shot_out.subLayerPaths) == {"light.usda", "fx.usda", "layout.usda"}

    def test_keep_session_layer_unchecked_skips_it(self, tmp_path):
        """Unchecking Keep Session Layer skips writing SessionLayer.usda — only the
        real, driven-content layers get exported."""
        scene, stage = _setup_stage()

        self._create_layer(scene, "shot", "create_layer_shot")
        edit_target = _make(scene, self.edit_mod, "edit_target")
        # edit_target now stores/matches display names, not raw (session-ephemeral)
        # Sdf identifiers — see edit_target.py's _refresh_edit_target/_resolve_layer.
        edit_target.get_param("edit_target").value = "shot"
        edit_target.execute()

        export_chunk = _make(scene, self.export_mod, "export_usd")
        export_chunk.get_param("output_dir").value = str(tmp_path)
        export_chunk.get_param("keep_session_layer").value = False
        export_param = export_chunk.get_param("export_all")
        self.export_mod.export_all(export_param, None, None)

        written = {p.name for p in tmp_path.iterdir()}
        assert "SessionLayer.usda" not in written
        assert "shot.usda" in written

    def test_export_unchecked_skips_layer_and_drops_reference(self, tmp_path):
        """Unchecking a row's Export checkbox skips that layer's own file, and any
        other exported layer that sublayered it has that reference dropped rather
        than left dangling."""
        scene, stage = _setup_stage()

        self._create_layer(scene, "shot", "create_layer_shot")
        edit_target = _make(scene, self.edit_mod, "edit_target")
        # edit_target now stores/matches display names, not raw (session-ephemeral)
        # Sdf identifiers — see edit_target.py's _refresh_edit_target/_resolve_layer.
        edit_target.get_param("edit_target").value = "shot"
        edit_target.execute()

        self._create_layer(scene, "light", "create_layer_light")
        self._create_layer(scene, "fx", "create_layer_fx")
        self._create_layer(scene, "layout", "create_layer_layout")

        export_chunk = _make(scene, self.export_mod, "export_usd")

        class _FakeOwner:
            def get_chunk(self):
                return export_chunk

            def get_stage(self):
                return stage

        self.export_mod._refresh_layer_rows(_FakeOwner(), None, None)
        rows = export_chunk.get_param("export_layers", chunk.is_inst(chunk.ListGroup))
        name_t = rows.get_param("layer_name")
        export_t = rows.get_param("export_enabled")
        bake_t = rows.get_param("bake")
        fx_bake = None
        for i in range(rows.size):
            if rows.get_indexed_param(name_t, i).value == "fx":
                fx_bake = rows.get_indexed_param(bake_t, i)
                rows.get_indexed_param(export_t, i).value = False

        # Unchecking Export clears that same row's Bake checkbox — its .value, which is the
        # only thing a ProxyParam keeps per row. Asserting a per-row DISABLED state instead
        # could never hold: state is a group flag the proxy forwards to the shared template,
        # so it is the whole column's, and the winner was whichever row's callback fired last.
        assert fx_bake is not None
        assert fx_bake.value is False

        export_chunk.get_param("output_dir").value = str(tmp_path)
        export_param = export_chunk.get_param("export_all")
        self.export_mod.export_all(export_param, None, None)

        written = {p.name for p in tmp_path.iterdir()}
        assert "fx.usda" not in written
        assert {"shot.usda", "light.usda", "layout.usda"} <= written

        shot_out = Sdf.Layer.FindOrOpen(str(tmp_path / "shot.usda"))
        assert shot_out is not None
        assert set(shot_out.subLayerPaths) == {"light.usda", "layout.usda"}

    def test_procedural_leaves_recipe_only_no_baked_content(self, tmp_path):
        """Unchecking a layer's Bake checkbox (Procedural) in Layers to Export exports
        the chunk-graph bookkeeping only — each chunk's real content stays in its own
        private layer (not copied in), so reloading the recipe re-runs execute() to
        regenerate it, rather than the file already containing a baked-out copy."""
        light_mod = _load("scripts.modelling.light")
        scene, stage = _setup_stage()

        self._create_layer(scene, "shot", "create_layer_shot")
        edit_target = _make(scene, self.edit_mod, "edit_target")
        # edit_target now stores/matches display names, not raw (session-ephemeral)
        # Sdf identifiers — see edit_target.py's _refresh_edit_target/_resolve_layer.
        edit_target.get_param("edit_target").value = "shot"
        edit_target.execute()

        light = _make(scene, light_mod, "light")
        light.execute()

        export_chunk = _make(scene, self.export_mod, "export_usd")

        class _FakeOwner:
            def get_chunk(self):
                return export_chunk

            def get_stage(self):
                return stage

        # STAGE_CONTENTS_CHANGED already auto-refreshed the list as layers were created;
        # call it once more directly to be sure it reflects every layer before we edit it.
        self.export_mod._refresh_layer_rows(_FakeOwner(), None, None)

        rows = export_chunk.get_param("export_layers", chunk.is_inst(chunk.ListGroup))
        name_t = rows.get_param("layer_name")
        bake_t = rows.get_param("bake")
        found_shot_row = False
        for i in range(rows.size):
            if rows.get_indexed_param(name_t, i).value == "shot":
                rows.get_indexed_param(bake_t, i).value = False  # Procedural
                found_shot_row = True
        assert found_shot_row, "shot should appear in Layers to Export"

        export_chunk.get_param("output_dir").value = str(tmp_path)
        export_param = export_chunk.get_param("export_all")
        self.export_mod.export_all(export_param, None, None)

        shot_out = Sdf.Layer.FindOrOpen(str(tmp_path / "shot.usda"))
        assert shot_out is not None

        light_spec = shot_out.GetPrimAtPath(get_chunk_prim_path(light))
        assert light_spec is not None, "Light chunk's bookkeeping prim should be in the recipe export"
        assert light_spec.GetInfo("typeName") == "Chunk"

        # No baked content — Light's real DiskLight stays in its own private layer,
        # not copied into this recipe-only export.
        recipe_stage = Usd.Stage.Open(shot_out)
        assert not [p for p in recipe_stage.Traverse() if p.GetTypeName() == "DiskLight"]

    def test_baked_strips_every_chunk_regardless_of_how_many_nested_there(self, tmp_path):
        """Chunk-prim bookkeeping follows Edit Target now (get_bookkeeping_layer), so any
        chunk that authors/re-authors its own bookkeeping while shot is the active Edit
        Target ends up with that bookkeeping directly inside shot — not just Light,
        whose real content is meant to be there. Export USD's own params get set here
        (via _refresh_layer_rows, after Edit Target -> shot), landing its bookkeeping in
        shot too. Bake chunk outputs checked (via the Bake all switch — rows default to
        unchecked now) must strip *all* of it
        regardless of how many different chunks ended up nested there, leaving only
        Light's real DiskLight."""
        light_mod = _load("scripts.modelling.light")
        scene, stage = _setup_stage()

        self._create_layer(scene, "shot", "create_layer_shot")
        edit_target = _make(scene, self.edit_mod, "edit_target")
        # edit_target now stores/matches display names, not raw (session-ephemeral)
        # Sdf identifiers — see edit_target.py's _refresh_edit_target/_resolve_layer.
        edit_target.get_param("edit_target").value = "shot"
        edit_target.execute()

        light = _make(scene, light_mod, "light")
        light.execute()

        export_chunk = _make(scene, self.export_mod, "export_usd")
        export_chunk.get_param("bake_all").value = True

        class _FakeOwner:
            def get_chunk(self):
                return export_chunk

            def get_stage(self):
                return stage

        # Setting these params (after Edit Target -> shot) reactively executes Export
        # USD's own params, landing its own bookkeeping in shot alongside Light's.
        self.export_mod._refresh_layer_rows(_FakeOwner(), None, None)

        export_chunk.get_param("output_dir").value = str(tmp_path)
        export_param = export_chunk.get_param("export_all")
        self.export_mod.export_all(export_param, None, None)

        shot_out = Sdf.Layer.FindOrOpen(str(tmp_path / "shot.usda"))
        assert shot_out is not None

        def _collect_type_names(spec):
            names = {spec.typeName} if spec.typeName else set()
            for child in spec.nameChildren.values():
                names |= _collect_type_names(child)
            return names

        assert "Chunk" not in _collect_type_names(shot_out.pseudoRoot)
        out_stage = Usd.Stage.Open(shot_out)
        assert [p for p in out_stage.Traverse() if p.GetTypeName() == "DiskLight"]

    def test_baked_chunk_unchanged_no_recipe_added(self, tmp_path):
        """A chunk whose Bake checkbox is checked (via Bake all — rows default to
        unchecked now) gets no
        bookkeeping prim added to the exported layer, only its real content."""
        light_mod = _load("scripts.modelling.light")
        scene, stage = _setup_stage()

        self._create_layer(scene, "shot", "create_layer_shot")
        edit_target = _make(scene, self.edit_mod, "edit_target")
        # edit_target now stores/matches display names, not raw (session-ephemeral)
        # Sdf identifiers — see edit_target.py's _refresh_edit_target/_resolve_layer.
        edit_target.get_param("edit_target").value = "shot"
        edit_target.execute()

        light = _make(scene, light_mod, "light")
        light.execute()

        export_chunk = _make(scene, self.export_mod, "export_usd")
        export_chunk.get_param("bake_all").value = True
        export_chunk.get_param("output_dir").value = str(tmp_path)
        export_param = export_chunk.get_param("export_all")
        self.export_mod.export_all(export_param, None, None)

        shot_out = Sdf.Layer.FindOrOpen(str(tmp_path / "shot.usda"))
        assert shot_out is not None
        assert shot_out.GetPrimAtPath(get_chunk_prim_path(light)) is None
        recipe_stage = Usd.Stage.Open(shot_out)
        assert [p for p in recipe_stage.Traverse() if p.GetTypeName() == "DiskLight"]

    def _setup_light_in_shot(self, tmp_path):
        light_mod = _load("scripts.modelling.light")
        scene, stage = _setup_stage()

        self._create_layer(scene, "shot", "create_layer_shot")
        edit_target = _make(scene, self.edit_mod, "edit_target")
        # edit_target now stores/matches display names, not raw (session-ephemeral)
        # Sdf identifiers — see edit_target.py's _refresh_edit_target/_resolve_layer.
        edit_target.get_param("edit_target").value = "shot"
        edit_target.execute()

        light = _make(scene, light_mod, "light")
        light.execute()

        export_chunk = _make(scene, self.export_mod, "export_usd")
        export_chunk.get_param("flatten").value = True
        export_chunk.get_param("output_file").value = str(tmp_path / "out.usda")
        return scene, stage, export_chunk, light

    def test_export_flatten_baked_strips_chunk_prims(self, tmp_path):
        """Flatten checked, Mode=Baked (default): one file with every chunk's real
        driven content merged in, but no chunk-graph bookkeeping prims at all."""
        scene, stage, export_chunk, light = self._setup_light_in_shot(tmp_path)

        export_param = export_chunk.get_param("export_all")
        self.export_mod.export_all(export_param, None, None)

        out = Sdf.Layer.FindOrOpen(str(tmp_path / "out.usda"))
        assert out is not None
        out_stage = Usd.Stage.Open(out)
        assert [p for p in out_stage.Traverse() if p.GetTypeName() == "DiskLight"]
        assert not [p for p in out_stage.Traverse() if p.GetTypeName() == "Chunk"]
        # The scope the chunks hung under is authored in its own right, so stripping
        # them all used to leave an empty `def Scope "chunks" {}` behind.
        assert not out_stage.GetPrimAtPath(Sdf.Path(CHUNKS_ROOT))

    def test_export_flatten_procedural_is_session_layer_only(self, tmp_path):
        """Flatten checked, Mode=Procedural: equivalent to exporting Session Layer
        directly — only the chunk graph, no driven content authored directly in the
        file. (Checked via the file's own Sdf specs, not a re-opened Usd.Stage — the
        exported file's subLayerPaths still literally references the shot layer's
        anon: identifier, which only resolves because it's still alive in this same
        test process; a real reload elsewhere would leave it dangling, per the
        Session Layer tooltip's "reloading re-runs execute()" design.)"""
        scene, stage, export_chunk, light = self._setup_light_in_shot(tmp_path)
        export_chunk.get_param("export_mode").value = "Procedural"

        export_param = export_chunk.get_param("export_all")
        self.export_mod.export_all(export_param, None, None)

        out = Sdf.Layer.FindOrOpen(str(tmp_path / "out.usda"))
        assert out is not None

        def _collect_type_names(spec):
            names = {spec.typeName} if spec.typeName else set()
            for child in spec.nameChildren.values():
                names |= _collect_type_names(child)
            return names

        # Chunk bookkeeping and the /chunks Scope that holds it, and nothing else --
        # no baked content. The scope is authored as a Scope (it is just a
        # scope, not a chunk prim) and export carries that typename across.
        assert _collect_type_names(out.pseudoRoot) == {"Chunk", "Scope"}

    def test_export_flatten_based_on_settings_adds_marked_recipes(self, tmp_path):
        """Flatten checked, Mode=Based on Settings: Baked content, plus the recipe of
        every chunk whose target layer is marked Procedural in Layers to Export —
        merged into the one output file rather than split by layer."""
        scene, stage, export_chunk, light = self._setup_light_in_shot(tmp_path)

        class _FakeOwner:
            def get_chunk(self):
                return export_chunk

            def get_stage(self):
                return stage

        self.export_mod._refresh_layer_rows(_FakeOwner(), None, None)
        rows = export_chunk.get_param("export_layers", chunk.is_inst(chunk.ListGroup))
        name_t = rows.get_param("layer_name")
        bake_t = rows.get_param("bake")
        for i in range(rows.size):
            if rows.get_indexed_param(name_t, i).value == "shot":
                rows.get_indexed_param(bake_t, i).value = False  # Procedural

        export_chunk.get_param("export_mode").value = "Based on Settings"
        export_param = export_chunk.get_param("export_all")
        self.export_mod.export_all(export_param, None, None)

        out = Sdf.Layer.FindOrOpen(str(tmp_path / "out.usda"))
        assert out is not None
        out_stage = Usd.Stage.Open(out)
        assert [p for p in out_stage.Traverse() if p.GetTypeName() == "DiskLight"]
        assert out.GetPrimAtPath(get_chunk_prim_path(light)) is not None

    def test_export_chunkgroup_is_self_contained(self, tmp_path):
        """Export ChunkGroup rebuilds every chunk prim fresh into one brand-new layer —
        no subLayerPaths at all, unlike Export USD's Mode: Procedural (a raw copy of
        the live session layer, which can carry forward subLayerPaths to in-memory
        anon: layers that don't resolve once the session ends)."""
        export_chunkgroup_mod = _load("scripts.core.export_chunkgroup")
        light_mod = _load("scripts.modelling.light")
        scene, stage = _setup_stage()

        self._create_layer(scene, "shot", "create_layer_shot")
        edit_target = _make(scene, self.edit_mod, "edit_target")
        # edit_target now stores/matches display names, not raw (session-ephemeral)
        # Sdf identifiers — see edit_target.py's _refresh_edit_target/_resolve_layer.
        edit_target.get_param("edit_target").value = "shot"
        edit_target.execute()

        light = _make(scene, light_mod, "light")
        light.execute()

        export_chunk = _make(scene, export_chunkgroup_mod, "export_chunkgroup")
        out_path = tmp_path / "graph.usda"
        export_chunk.get_param("output_file").value = str(out_path)
        export_param = export_chunk.get_param("export")
        export_chunkgroup_mod.export(export_param, None, None)

        out = Sdf.Layer.FindOrOpen(str(out_path))
        assert out is not None
        assert not out.subLayerPaths, "should be self-contained, no sublayer references"

        def _collect_type_names(spec):
            names = {spec.typeName} if spec.typeName else set()
            for child in spec.nameChildren.values():
                names |= _collect_type_names(child)
            return names

        # Chunk bookkeeping and the /chunks Scope that holds it, and nothing else --
        # no baked content. The scope is authored as a Scope (it is just a
        # scope, not a chunk prim) and export carries that typename across.
        assert _collect_type_names(out.pseudoRoot) == {"Chunk", "Scope"}
