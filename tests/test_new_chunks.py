"""Smoke tests for dot, bind_material, and AMDMaterialXLibrary chunks."""
from __future__ import annotations

import importlib
import io
import zipfile

import pytest
from pxr import Usd, UsdShade, Sdf

from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_core import ChunkBase, ChunkConfig, ChunkFlags, SceneSender
from usd_chunks.plugins.chunk_usd import (
    _author_prim,
    _CHUNKS_ROOT,
    ChunkStageSync,
    get_chunk_prim_path,
    initialize as init_chunk_usd,
)


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


chunk_registry.ChunkRegistry().initialize(_DummyScene(), "pytest")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _load(module_path: str):
    return importlib.import_module(module_path)


# ---------------------------------------------------------------------------
# dot
# ---------------------------------------------------------------------------

class TestDot:
    def setup_method(self):
        self.mod = _load("scripts.dot")

    def test_main_returns_chunk(self):
        c = self.mod.main()
        assert isinstance(c, ChunkBase)
        assert c.name == "Dot"

    def test_no_params(self):
        c = self.mod.main()
        assert len(c.get_my_params()) == 0

    def test_config_category(self):
        cfg = self.mod.config()
        assert cfg.category == chunk.StandardCategory.UTILS

    def test_execute_is_noop(self):
        """execute clears the layer but doesn't raise."""
        stage = Usd.Stage.CreateInMemory()
        stage.GetEditTarget().GetLayer().Clear()

        c = self.mod.main()
        c._config = ChunkConfig(module="dot")

        class _FakeNode:
            def get_stage(self): return stage

        self.mod.execute(_FakeNode())  # must not raise

    def test_prim_has_exec_output(self):
        c = self.mod.main()
        c._config = ChunkConfig(module="dot")
        stage = Usd.Stage.CreateInMemory()
        _author_prim(stage, c)
        prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/Dot")
        assert prim.GetAttribute("outputs:chunk").IsValid()

    def test_prim_chunk_type(self):
        c = self.mod.main()
        c._config = ChunkConfig(module="dot")
        stage = Usd.Stage.CreateInMemory()
        _author_prim(stage, c)
        prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/Dot")
        assert prim.GetAttribute("info:chunkType").Get() == "dot"


# ---------------------------------------------------------------------------
# bind_material
# ---------------------------------------------------------------------------

class TestBindMaterial:
    def setup_method(self):
        self.mod = _load("scripts.bind_material")

    def test_main_returns_chunk(self):
        c = self.mod.main()
        assert isinstance(c, ChunkBase)
        assert c.name == "BindMaterial"

    def test_has_required_params(self):
        c = self.mod.main()
        assert c.get_param("material_path") is not None
        assert c.get_param("primpath_location") is not None

    def test_config_category(self):
        cfg = self.mod.config()
        assert cfg.category == chunk.StandardCategory.UTILS

    def test_execute_binds_material(self):
        stage = Usd.Stage.CreateInMemory()
        mat_path = "/materials/Gold"
        prim_path = "/geo/mesh"

        # Define material and prim in the session layer — always in the composed
        # stack but never the root-layer edit target, so execute's layer.Clear()
        # leaves them intact.
        with Usd.EditContext(stage, stage.GetSessionLayer()):
            UsdShade.Material.Define(stage, mat_path)
            stage.DefinePrim(prim_path, "Mesh")

        # Use ChunkStageSync so param.get_stage() works inside execute.
        class _StageScene(_DummyScene):
            def get_stage(self):
                return stage

        scene = _StageScene()
        chunk_registry.ChunkRegistry().initialize(scene, "pytest")
        ChunkStageSync().initialize(stage, scene)

        c = self.mod.main()
        c._config = ChunkConfig(module="bind_material")
        c.get_param("material_path").value = mat_path
        c.get_param("primpath_location").value = prim_path
        scene.add_chunk(c)

        class _FakeNode:
            def get_stage(self): return stage
            def get_param(self, name, pred=None):
                return c.get_param(name, pred)

        self.mod.execute(_FakeNode())

        prim = stage.GetPrimAtPath(prim_path)
        assert prim and prim.IsValid(), "Mesh prim must still be accessible via session layer"
        binding = UsdShade.MaterialBindingAPI(prim).GetDirectBinding()
        assert str(binding.GetMaterialPath()) == mat_path

    def test_execute_noop_when_no_material(self):
        """execute must not raise when material prim doesn't exist."""
        stage = Usd.Stage.CreateInMemory()
        stage.DefinePrim("/geo/mesh", "Mesh")

        c = self.mod.main()
        c.get_param("material_path").value = "/materials/NonExistent"
        c.get_param("primpath_location").value = "/geo/mesh"

        class _FakeNode:
            def get_stage(self): return stage
            def get_param(self, name, pred=None):
                return c.get_param(name, pred)

        self.mod.execute(_FakeNode())  # must not raise


# ---------------------------------------------------------------------------
# AMDMaterialXLibrary — load-only (no network calls)
# main() creates a MaterialGridWidget so it requires a running QApplication.
# ---------------------------------------------------------------------------

class TestAMDMaterialXLibrary:
    @pytest.fixture(autouse=True)
    def _require_qapp(self, qapp):  # noqa: ARG002 — injected for side-effect only
        """Ensure QApplication exists before creating Qt widgets in main()."""

    def setup_method(self):
        self.mod = _load("scripts.examples.AMDMaterialXLibrary")

    def test_main_returns_context_chunk(self):
        c = self.mod.main()
        assert isinstance(c, ChunkBase)
        assert c.name == "AMDMaterialXLibrary"
        assert c.has_flag(ChunkFlags.GroupFlags.CONTEXT)

    def test_has_required_params(self):
        c = self.mod.main()
        assert c.get_param("target_primpath") is not None
        assert c.get_param("material_grid") is not None
        assert c.get_param("resolution") is not None
        assert c.get_param("picked_title") is not None
        assert c.get_param("resolved_material_path") is not None
        assert c.get_param("status") is not None

    def test_config_category(self):
        cfg = self.mod.config()
        assert cfg.category == chunk.StandardCategory.EXAMPLES


# ---------------------------------------------------------------------------
# AMDMaterialXLibrary — child chunk creation (Reference / BindMaterial)
#
# Reproduces the real-app wiring: a CONTEXT chunk attached to a live stage via
# plugins.chunk_usd.initialize() (ChunkLayerManager + ChunkStageSync), then
# runs _download_and_apply/execute with the network calls stubbed out, to
# verify the Reference and BindMaterial children actually get created and
# stay reachable via get_param() on the context chunk.
# ---------------------------------------------------------------------------

class _FakeZipResponse:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        pass


@pytest.mark.skipif(
    Sdf.FileFormat.FindByExtension("mtlx") is None,
    reason="requires a USD build with the MaterialX file format plugin (absent from the usd-core wheels)",
)
class TestAMDMaterialXLibraryChildCreation:
    @pytest.fixture(autouse=True)
    def _require_qapp(self, qapp):  # noqa: ARG002 — injected for side-effect only
        """Ensure QApplication exists before creating Qt widgets in main()."""

    def setup_method(self):
        self.mod = _load("scripts.examples.AMDMaterialXLibrary")
        # Ensure the child chunk modules _create_child resolves are registered.
        _load("scripts.reference")
        _load("scripts.bind_material")

    @staticmethod
    def _fake_zip_bytes() -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("Materials/Gold/Gold.mtlx", "<materialx version='1.38'></materialx>")
        return buf.getvalue()

    def test_download_and_apply_creates_reference_and_bind_children(self, monkeypatch, tmp_path):
        stage = Usd.Stage.CreateInMemory()

        class _StageScene(_DummyScene):
            def get_stage(self):
                return stage

        scene = _StageScene()
        chunk_registry.ChunkRegistry().initialize(scene, "pytest")
        init_chunk_usd(stage, scene)  # wires ChunkLayerManager + ChunkStageSync, like the real app

        c = self.mod.main()
        c._config = ChunkConfig(module="examples.AMDMaterialXLibrary")
        c.get_param("target_primpath").value = "/geo/mesh"
        c.get_param("mat_scope").value = "/materials"
        c.get_param("cache_dir").value = str(tmp_path)
        scene.add_chunk(c)

        # Stub out the network calls _download_and_apply would otherwise make.
        self.mod._results_by_title["Gold"] = {
            "title": "Gold",
            "packages": ["pkg1"],
            "mtlx_material_name": "Gold",
        }
        monkeypatch.setattr(
            self.mod,
            "_pick_package",
            lambda package_ids, resolution: {"file_url": "http://fake/pkg.zip", "label": "2k"},
        )
        monkeypatch.setattr(
            self.mod.requests,
            "get",
            lambda url, timeout=120: _FakeZipResponse(self._fake_zip_bytes()),
        )

        c.get_param("picked_title").value = "Gold"
        self.mod.execute(c)

        ref_chunk = c.get_param("Reference", chunk.is_inst(chunk.Chunk))
        bind_chunk = c.get_param("BindMaterial", chunk.is_inst(chunk.Chunk))

        assert ref_chunk is not None, "Reference child chunk was not created/attached to the context"
        assert bind_chunk is not None, "BindMaterial child chunk was not created/attached to the context"
        assert ref_chunk.get_param("primpath_location").value == "/materials/Gold"
        assert ref_chunk.get_param("file_path").value.endswith(".mtlx")
        assert bind_chunk.get_param("material_path").value == "/materials/Gold"
        assert bind_chunk.get_param("primpath_location").value == "/geo/mesh"
        assert c.get_param("resolved_material_path").value == "/materials/Gold"

        # Live-reported: Reference "looked disconnected" in the node graph. Both children
        # start as bare, unwired chunk prims -- view-driven execution mutes anything in a
        # subnet not reachable from its inner view's upstream tree, and the graph's own
        # default-view logic only ever auto-wires ONE arbitrary disconnected tail, which
        # was landing on BindMaterial and leaving Reference unreachable (and its authored
        # content liable to vanish on the next recook). _download_and_apply must chain
        # Reference -> BindMaterial itself so only BindMaterial is ever a loose tail.
        bind_prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(bind_chunk)))
        bind_exec_in = bind_prim.GetAttribute("inputs:chunk")
        assert bind_exec_in.IsValid(), "BindMaterial must have an inputs:chunk exec port"
        assert [str(p) for p in bind_exec_in.GetConnections()] == [
            f"{get_chunk_prim_path(ref_chunk)}.outputs:chunk"
        ], "Reference must be wired into BindMaterial's exec chain, not left disconnected"

    def test_child_chunk_prims_authored_in_session_layer(self, monkeypatch, tmp_path):
        """Child chunk prims (Reference, BindMaterial) created inside execute()
        must be authored on the user's edit layer (session layer by default),
        not on AMD's anonymous execute layer.

        AMD's execute() runs inside a Usd.EditContext targeting AMD's own
        anonymous layer so that its USD output lands there. When add_param()
        creates child chunk prims via ChunkStageSync._define_prim() during
        that context, those prims must still be routed to the session layer —
        otherwise they live in AMD's anon layer, which is Clear()ed on every
        execute() run, and they are never saved to disk.
        """
        stage = Usd.Stage.CreateInMemory()

        class _StageScene(_DummyScene):
            def get_stage(self):
                return stage

        scene = _StageScene()
        chunk_registry.ChunkRegistry().initialize(scene, "pytest")
        init_chunk_usd(stage, scene)

        c = self.mod.main()
        c._config = ChunkConfig(module="examples.AMDMaterialXLibrary")
        c.get_param("target_primpath").value = "/geo/mesh"
        c.get_param("mat_scope").value = "/materials"
        c.get_param("cache_dir").value = str(tmp_path)
        scene.add_chunk(c)

        self.mod._results_by_title["Gold"] = {
            "title": "Gold",
            "packages": ["pkg1"],
            "mtlx_material_name": "Gold",
        }
        monkeypatch.setattr(
            self.mod,
            "_pick_package",
            lambda package_ids, resolution: {"file_url": "http://fake/pkg.zip", "label": "2k"},
        )
        monkeypatch.setattr(
            self.mod.requests,
            "get",
            lambda url, timeout=120: _FakeZipResponse(self._fake_zip_bytes()),
        )

        c.get_param("picked_title").value = "Gold"
        self.mod.execute(c)

        # The child chunk prims must exist in the user's persistent edit layer -- the
        # session layer by default (get_bookkeeping_layer's fallback for a CONTEXT chunk,
        # which AMD is) -- not in AMD's anonymous execute layer, which is Clear()ed on
        # every execute() run. Previously checked stage.GetRootLayer() here, which a bare
        # Usd.Stage.CreateInMemory() never authors bookkeeping into at all; that
        # mismatch silently failed this test regardless of whether the real bug (below)
        # was present, so it never actually caught it.
        session_layer = stage.GetSessionLayer()
        amd_layer = c.get_edit_context()
        ref_spec = session_layer.GetPrimAtPath(Sdf.Path("/chunks/AMDMaterialXLibrary/Reference"))
        bind_spec = session_layer.GetPrimAtPath(Sdf.Path("/chunks/AMDMaterialXLibrary/BindMaterial"))

        assert ref_spec is not None, (
            "Reference chunk prim must be authored in the user edit layer — "
            "it was likely written to AMD's anonymous execute layer instead"
        )
        assert bind_spec is not None, (
            "BindMaterial chunk prim must be authored in the user edit layer — "
            "it was likely written to AMD's anonymous execute layer instead"
        )
        assert amd_layer.GetPrimAtPath(Sdf.Path("/chunks/AMDMaterialXLibrary/Reference")) is None, (
            "Reference chunk prim must NOT also exist in AMD's own anonymous execute "
            "layer -- that layer is Clear()ed on every execute() run"
        )

    def test_own_params_survive_save_and_reload(self, monkeypatch, tmp_path):
        """Live-reported: "the AMD MaterialX chunk doesn't get saved settings at all
        when we save the scene."

        The child chunk prims (previous test) were never the problem -- they already
        round-tripped correctly. AMD's OWN direct params (Picked Material, Material
        Path, Prim Path, Material Scope) did not: load_chunks' CONTEXT-chunk branch
        added the freshly-reconstructed chunk to the scene without first calling
        _load_wire_inputs (the other two branches in the same function both do), so
        every one of AMD's own params silently reverted to its default on reload --
        even though the saved file correctly held their real values the whole time.
        AMD uses GroupFlags.CONTEXT purely for its collapsible/embedded-widget node
        styling, not because it is a structural Tab/ChunkGroup with nothing of its own
        to restore, which is what made this gap invisible for every OTHER CONTEXT
        chunk in the project.
        """
        from usd_chunks.plugins.chunk_usd import load_chunks, save_scene

        stage = Usd.Stage.CreateInMemory()

        class _StageScene(_DummyScene):
            def get_stage(self):
                return stage

        scene = _StageScene()
        chunk_registry.ChunkRegistry().initialize(scene, "pytest")
        init_chunk_usd(stage, scene)

        c = self.mod.main()
        c._config = ChunkConfig(module="examples.AMDMaterialXLibrary")
        c.get_param("target_primpath").value = "/geo/mesh"
        c.get_param("mat_scope").value = "/materials"
        c.get_param("cache_dir").value = str(tmp_path)
        scene.add_chunk(c)

        gold = {"title": "Gold", "packages": ["pkg1"], "mtlx_material_name": "Gold"}
        self.mod._results_by_title["Gold"] = gold
        monkeypatch.setattr(
            self.mod,
            "_pick_package",
            lambda package_ids, resolution: {"file_url": "http://fake/pkg.zip", "label": "2k"},
        )
        monkeypatch.setattr(
            self.mod.requests,
            "get",
            lambda url, timeout=120: _FakeZipResponse(self._fake_zip_bytes()),
        )
        c.get_param("picked_title").value = "Gold"
        self.mod.execute(c)

        out = tmp_path / "scene.usda"
        save_scene(scene, out)

        stage2 = Usd.Stage.Open(str(out))

        class _StageScene2(_DummyScene):
            def get_stage(self):
                return stage2

        scene2 = _StageScene2()
        chunk_registry.ChunkRegistry().initialize(scene2, "pytest")
        # Restoring picked_title correctly now (the fix under test) makes AMD's
        # reserved execute() hook -- and the CHUNK_LOADED-driven catalog fetch --
        # fire for real on reload, same as a genuine reopen. Both would otherwise
        # re-run the whole download-and-apply flow (already covered by
        # test_download_and_apply_creates_reference_and_bind_children) on a real
        # background thread; this test only cares whether the saved VALUES came back,
        # so stand both down rather than mock the network two levels deep.
        #
        # self.mod is NOT the module load_chunks' reconstruction actually runs: the
        # registry resolves chunk modules by the short config name ("examples.Foo"),
        # while this test file imported the same source under a different
        # sys.modules key ("scripts.examples.Foo") via _load(). Those are two
        # distinct module objects with independent globals, so patches on self.mod
        # are invisible to code executing as the registry's copy -- reach through
        # the registry to patch the module identity that will actually run.
        registry_mod = chunk_registry.ChunkRegistry().get_chunk_module("examples.AMDMaterialXLibrary").module
        monkeypatch.setattr(registry_mod, "_on_chunk_loaded", lambda owner, notice, sender: None)
        monkeypatch.setattr(registry_mod, "execute", lambda node: None)
        init_chunk_usd(stage2, scene2)
        load_chunks(stage2, scene2)

        reloaded = next(
            (
                ch
                for ch in scene2.get_chunks()
                if (cfg := ch.get_config()) and cfg.module == "examples.AMDMaterialXLibrary"
            ),
            None,
        )
        assert reloaded is not None, "AMD chunk was not reconstructed on reload at all"

        assert reloaded.get_param("target_primpath").value == "/geo/mesh"
        assert reloaded.get_param("mat_scope").value == "/materials"
        assert reloaded.get_param("picked_title").value == "Gold"
        assert reloaded.get_param("resolved_material_path").value == "/materials/Gold"
