"""Smoke tests for dot, bind_material, and AMDMaterialXLibrary chunks."""
from __future__ import annotations

import importlib
import io
import time
import zipfile

import pytest
from pxr import Usd, UsdShade, Sdf

from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_core import ChunkBase, ChunkConfig, ChunkFlags, SceneSender
from usd_chunks.chunk_stage import (
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
        self.mod = _load("chunks.core.dot")

    def test_main_returns_chunk(self):
        c = self.mod.main()
        assert isinstance(c, ChunkBase)
        assert c.name == "Dot"

    def test_no_params(self):
        c = self.mod.main()
        assert len(c.get_my_params()) == 0

    def test_config_category(self):
        cfg = self.mod.config()
        assert cfg.category == chunk.StandardCategory.CORE

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
        self.mod = _load("chunks.materials.bind_material")

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
        assert cfg.category == chunk.StandardCategory.MATERIALS

    def test_execute_binds_material(self):
        stage = Usd.Stage.CreateInMemory()
        mat_path = "/materials/Gold"
        prim_path = "/geo/mesh"

        # Define material and prim in the session layer -- always in the composed
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
        chunk_registry.ChunkLayerManager().initialize(stage)
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
# AMDMaterialXLibrary -- load-only (no network calls)
# main() creates a MaterialGridWidget so it requires a running QApplication.
# ---------------------------------------------------------------------------

class TestAMDMaterialXLibrary:
    @pytest.fixture(autouse=True)
    def _require_qapp(self, qapp):  # noqa: ARG002 -- injected for side-effect only
        """Ensure QApplication exists before creating Qt widgets in main()."""

    def setup_method(self):
        self.mod = _load("chunks.materials.AMDMaterialXLibrary")

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
        assert cfg.category == chunk.StandardCategory.MATERIALS

    def test_children_are_found_by_type_not_by_name(self):
        """A child is only called "Reference" if nothing had that name first. Looked up by
        name, a renamed one was not found and a second was made next to it."""
        c = self.mod.main()
        ref = chunk.Chunk("Reference3")
        ref._config = ChunkConfig(module="reference")
        c.add_param(ref)

        assert self.mod._child_of_type(c, "reference") is ref
        assert self.mod._child_of_type(c, "bind_material") is None


# ---------------------------------------------------------------------------
# AMDMaterialXLibrary -- child chunk creation (Reference / BindMaterial)
#
# Reproduces the real-app wiring: a CONTEXT chunk attached to a live stage via
# chunk_stage.initialize() (ChunkLayerManager + ChunkStageSync), then
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


def _chunk_path(name: str) -> str:
    """Where a chunk added through the scene actually lands.

    add_chunk routes a context-less chunk into the scene's default chunkgroup, so its
    parent is that group rather than the /chunks scope.
    """
    from usd_chunks import chunk_registry
    from usd_chunks.chunk_stage import get_chunk_prim_path

    scene = chunk_registry.ChunkRegistry().scene
    ctx = scene.peek_root_context() if scene is not None else None
    root = get_chunk_prim_path(ctx) if ctx is not None else "/chunks"
    return f"{root}/{name}"

class TestAMDMaterialXLibraryChildCreation:
    @pytest.fixture(autouse=True)
    def _require_qapp(self, qapp):  # noqa: ARG002 -- injected for side-effect only
        """Ensure QApplication exists before creating Qt widgets in main()."""

    def setup_method(self):
        self.mod = _load("chunks.materials.AMDMaterialXLibrary")
        # Ensure the child chunk modules _create_child resolves are registered.
        _load("chunks.usd.reference")
        _load("chunks.materials.bind_material")

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
        c._config = ChunkConfig(module="AMDMaterialXLibrary")
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
        that context, those prims must still be routed to the session layer --
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
        c._config = ChunkConfig(module="AMDMaterialXLibrary")
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
        ref_spec = session_layer.GetPrimAtPath(Sdf.Path(f'{_chunk_path("AMDMaterialXLibrary")}/Reference'))
        bind_spec = session_layer.GetPrimAtPath(Sdf.Path(f'{_chunk_path("AMDMaterialXLibrary")}/BindMaterial'))

        assert ref_spec is not None, (
            "Reference chunk prim must be authored in the user edit layer — "
            "it was likely written to AMD's anonymous execute layer instead"
        )
        assert bind_spec is not None, (
            "BindMaterial chunk prim must be authored in the user edit layer — "
            "it was likely written to AMD's anonymous execute layer instead"
        )
        assert amd_layer.GetPrimAtPath(Sdf.Path(f'{_chunk_path("AMDMaterialXLibrary")}/Reference')) is None, (
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
        from usd_chunks.chunk_stage import load_chunks, save_scene

        stage = Usd.Stage.CreateInMemory()

        class _StageScene(_DummyScene):
            def get_stage(self):
                return stage

        scene = _StageScene()
        chunk_registry.ChunkRegistry().initialize(scene, "pytest")
        init_chunk_usd(stage, scene)

        c = self.mod.main()
        c._config = ChunkConfig(module="AMDMaterialXLibrary")
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
        # registry resolves chunk modules by the short config name ("Foo"),
        # while this test file imported the same source under a different
        # sys.modules key ("chunks.examples.Foo") via _load(). Those are two
        # distinct module objects with independent globals, so patches on self.mod
        # are invisible to code executing as the registry's copy -- reach through
        # the registry to patch the module identity that will actually run.
        registry_mod = chunk_registry.ChunkRegistry().get_chunk_module("AMDMaterialXLibrary").module
        monkeypatch.setattr(registry_mod, "_on_chunk_loaded", lambda owner, notice, sender: None)
        monkeypatch.setattr(registry_mod, "execute", lambda node: None)
        init_chunk_usd(stage2, scene2)
        load_chunks(stage2, scene2)

        reloaded = next(
            (
                ch
                for ch in scene2.get_chunks()
                if (cfg := ch.get_config()) and cfg.module == "AMDMaterialXLibrary"
            ),
            None,
        )
        assert reloaded is not None, "AMD chunk was not reconstructed on reload at all"

        assert reloaded.get_param("target_primpath").value == "/geo/mesh"
        assert reloaded.get_param("mat_scope").value == "/materials"
        assert reloaded.get_param("picked_title").value == "Gold"
        assert reloaded.get_param("resolved_material_path").value == "/materials/Gold"


class TestBindMaterialResilience:
    """A binding must survive a moment where the stage cannot resolve it.

    bind_material.execute used to Clear() its layer first and only then check whether the
    material and target prims existed -- so any run while the stage was mid-recook (view-
    driven execution mutes/unmutes layers one at a time) permanently destroyed a good
    binding, and nothing re-ran the chunk once the stage settled. Live-reported as the
    applied material reverting to the original one after navigating the node graph.
    """

    def setup_method(self):
        self.mod = _load("chunks.materials.bind_material")

    @staticmethod
    def _node(stage, chunk_obj):
        class _FakeNode:
            def get_stage(self):
                return stage

            def get_param(self, name, pred=None):
                return chunk_obj.get_param(name, pred)

        return _FakeNode()

    def _setup(self):
        """Content lives in the session layer so execute's own layer.Clear() (which targets
        the root layer here) leaves it intact -- same arrangement as test_execute_binds_material."""
        stage = Usd.Stage.CreateInMemory()
        with Usd.EditContext(stage, stage.GetSessionLayer()):
            UsdShade.Material.Define(stage, "/mat")
            stage.DefinePrim("/geo/mesh", "Mesh")

        class _StageScene(_DummyScene):
            def get_stage(self):
                return stage

        scene = _StageScene()
        chunk_registry.ChunkRegistry().initialize(scene, "pytest")
        chunk_registry.ChunkLayerManager().initialize(stage)
        ChunkStageSync().initialize(stage, scene)

        c = self.mod.main()
        c._config = ChunkConfig(module="bind_material")
        c.get_param("material_path").value = "/mat"
        c.get_param("primpath_location").value = "/geo/mesh"
        scene.add_chunk(c)
        return stage, c, self._node(stage, c)

    @staticmethod
    def _bound(stage):
        prim = stage.GetPrimAtPath("/geo/mesh")
        if not (prim and prim.IsValid()):
            return None
        return str(UsdShade.MaterialBindingAPI(prim).GetDirectBinding().GetMaterialPath())

    def test_binding_survives_a_temporarily_missing_target(self):
        stage, c, node = self._setup()
        self.mod.execute(node)
        assert self._bound(stage) == "/mat", "test setup must be faithful -- it must bind first"

        # The mesh momentarily drops off the stage, exactly as it does while the layer
        # that brings it in is muted for part of a recook.
        with Usd.EditContext(stage, stage.GetSessionLayer()):
            stage.RemovePrim("/geo/mesh")
        self.mod.execute(node)
        with Usd.EditContext(stage, stage.GetSessionLayer()):
            stage.DefinePrim("/geo/mesh", "Mesh")

        assert self._bound(stage) == "/mat", (
            "binding must not be wiped by a run that happened while the target was absent"
        )

    def test_binding_survives_a_temporarily_missing_material(self):
        stage, c, node = self._setup()
        self.mod.execute(node)
        assert self._bound(stage) == "/mat"

        with Usd.EditContext(stage, stage.GetSessionLayer()):
            stage.RemovePrim("/mat")
        self.mod.execute(node)
        with Usd.EditContext(stage, stage.GetSessionLayer()):
            UsdShade.Material.Define(stage, "/mat")

        assert self._bound(stage) == "/mat", (
            "binding must not be wiped by a run that happened while the material was absent"
        )

    def test_clearing_the_param_still_unbinds(self):
        """Companion check: an explicitly emptied field is a real instruction to unbind."""
        stage, c, node = self._setup()
        self.mod.execute(node)
        assert self._bound(stage) == "/mat"

        c.get_param("material_path").value = ""
        self.mod.execute(node)
        assert not self._bound(stage), "emptying the field must still clear the binding"


class TestAMDMaterialXCaching:
    """Applying a material must not re-download a package already in the cache.

    _download_and_apply is a reactive hook: it re-runs on every scene load and on every
    recook (diving into the node and back out is two). It used to do a package-metadata
    lookup plus a full zip download every single time, re-extracting a package already
    sitting in the cache dir -- measured at ~3.5s of pure SSL/socket time out of a 4.2s
    scene load, and the same cost again on each dive.
    """

    @pytest.fixture(autouse=True)
    def _require_qapp(self, qapp):  # noqa: ARG002 -- injected for side-effect only
        """main() builds Qt widgets."""

    def setup_method(self):
        self.mod = _load("chunks.materials.AMDMaterialXLibrary")
        _load("chunks.usd.reference")
        _load("chunks.materials.bind_material")

    def test_second_apply_hits_the_cache_and_makes_no_network_call(self, monkeypatch, tmp_path):
        stage = Usd.Stage.CreateInMemory()

        class _StageScene(_DummyScene):
            def get_stage(self):
                return stage

        scene = _StageScene()
        chunk_registry.ChunkRegistry().initialize(scene, "pytest")
        init_chunk_usd(stage, scene)

        c = self.mod.main()
        c._config = ChunkConfig(module="AMDMaterialXLibrary")
        c.get_param("target_primpath").value = "/geo/mesh"
        c.get_param("mat_scope").value = "/materials"
        c.get_param("cache_dir").value = str(tmp_path)
        scene.add_chunk(c)

        self.mod._results_by_title["Gold"] = {
            "title": "Gold", "packages": ["pkg1"], "mtlx_material_name": "Gold",
        }
        picks: list = []
        gets: list = []

        def _pick(package_ids, resolution):
            picks.append(resolution)
            return {"file_url": "http://fake/pkg.zip", "label": "2k"}

        def _get(url, timeout=120):
            gets.append(url)
            return _FakeZipResponse(TestAMDMaterialXLibraryChildCreation._fake_zip_bytes())

        monkeypatch.setattr(self.mod, "_pick_package", _pick)
        monkeypatch.setattr(self.mod.requests, "get", _get)

        c.get_param("picked_title").value = "Gold"
        self.mod.execute(c)
        assert gets, "test setup must be faithful -- the first apply must actually download"
        first_downloads = len(gets)

        # Re-run exactly as a reload or a dive in/out would.
        self.mod.execute(c)

        assert len(gets) == first_downloads, (
            "a package already extracted in the cache dir must not be downloaded again"
        )
        assert len(picks) == 1, (
            "a cache hit must also skip the package-metadata lookup -- that is a network call too"
        )
        assert c.get_param("resolved_material_path").value == "/materials/Gold"


class TestAMDMaterialXAsyncApply:
    """Download & Assign must not block the host for the length of a download.

    A cold-cache download is seconds of network. Doing it inline froze usdview's event
    loop for the whole time -- no viewport, no interaction. apply_material now runs only
    the download on a worker thread and hops back via QTimer.singleShot(0, ...) for every
    USD/Qt touch (the project's standard main-thread hop).
    """

    @pytest.fixture(autouse=True)
    def _require_qapp(self, qapp):  # noqa: ARG002 -- injected for side-effect only
        """main() builds Qt widgets."""

    def setup_method(self):
        self.mod = _load("chunks.materials.AMDMaterialXLibrary")
        _load("chunks.usd.reference")
        _load("chunks.materials.bind_material")

    def test_apply_downloads_off_thread_and_assigns_on_the_main_thread(self, monkeypatch, tmp_path, qapp):
        import threading

        stage = Usd.Stage.CreateInMemory()

        class _StageScene(_DummyScene):
            def get_stage(self):
                return stage

        scene = _StageScene()
        chunk_registry.ChunkRegistry().initialize(scene, "pytest")
        init_chunk_usd(stage, scene)

        c = self.mod.main()
        c._config = ChunkConfig(module="AMDMaterialXLibrary")
        c.get_param("target_primpath").value = "/geo/mesh"
        c.get_param("mat_scope").value = "/materials"
        c.get_param("cache_dir").value = str(tmp_path)
        scene.add_chunk(c)

        self.mod._results_by_title["Gold"] = {
            "title": "Gold", "packages": ["pkg1"], "mtlx_material_name": "Gold",
        }
        monkeypatch.setattr(
            self.mod, "_pick_package",
            lambda package_ids, resolution: {"file_url": "http://fake/pkg.zip", "label": "2k"},
        )

        main_thread = threading.current_thread().ident
        download_threads: list = []
        release = threading.Event()

        def _slow_get(url, timeout=120):
            download_threads.append(threading.current_thread().ident)
            release.wait(5)  # hold the "download" open so a blocking impl would stall here
            return _FakeZipResponse(TestAMDMaterialXLibraryChildCreation._fake_zip_bytes())

        monkeypatch.setattr(self.mod.requests, "get", _slow_get)
        monkeypatch.setattr(self.mod, "_selected_title", "Gold")

        applied_on: list = []
        real_execute = self.mod.execute

        def _tracking_execute(node):
            applied_on.append(threading.current_thread().ident)
            return real_execute(node)

        monkeypatch.setattr(self.mod, "execute", _tracking_execute)

        self.mod.apply_material(c, None, None)

        # The call returned while the download is still held open -- that is the whole point.
        assert not release.is_set()
        assert c.get_param("picked_title").value != "Gold", (
            "the assign must not have happened yet -- the download is still in flight"
        )

        release.set()
        deadline = time.time() + 10
        while time.time() < deadline and not applied_on:
            qapp.processEvents()
            time.sleep(0.01)

        assert download_threads and main_thread not in download_threads, (
            "the download must run on a worker thread, not the main thread"
        )
        assert applied_on == [main_thread], (
            "the USD assign must hop back to the main thread"
        )
        assert c.get_param("picked_title").value == "Gold"
        assert c.get_param("resolved_material_path").value == "/materials/Gold"
