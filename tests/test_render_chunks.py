"""Tests for the UsdRender chunks: Render Var, Render Product, Render Settings, Render.

The first three author standard UsdRender scene description and hand their prim path
downstream through a Reference output. The fourth is the ROP: it authors nothing, and
does its work in the reserved commit() hook so a render happens once, after the solve
has settled, rather than on every cook.
"""
from __future__ import annotations

import importlib
import types

from pxr import Usd, UsdRender

from usd_chunks import chunk_registry, chunk_scheduler
from usd_chunks.chunk_core import ChunkConfig, SceneSender
from usd_chunks.plugins import chunk_usd
from usd_chunks.plugins.chunk_usd import export_chunks
from usd_chunks.plugins.chunk_usd import initialize as init_chunk_usd
from usd_chunks.plugins.usdchunks_cli import cmd_run


class _DummyScene(SceneSender):
    def __init__(self):
        super().__init__()
        self._stage = None

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


def _setup_stage():
    scene = _DummyScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    stage = Usd.Stage.CreateInMemory()
    scene._stage = stage
    init_chunk_usd(stage, scene)
    return scene, stage


def _make(scene, mod, module_name):
    """Create a chunk and chain it onto the previous one, as the Params tab does."""
    c = mod.main()
    c._config = ChunkConfig(module=module_name)
    c._module = mod
    prev = chunk_usd._params_tab_create
    chunk_usd._params_tab_create = True
    try:
        scene.add_chunk(c)
    finally:
        chunk_usd._params_tab_create = prev
    return c


def _render_network(scene):
    """A var -> product -> settings chain, wired by Reference outputs and cooked."""
    var_mod = importlib.import_module("scripts.render_var")
    prod_mod = importlib.import_module("scripts.render_product")
    set_mod = importlib.import_module("scripts.render_settings")

    var = _make(scene, var_mod, "render_var")
    var.execute()

    product = _make(scene, prod_mod, "render_product")
    product.get_param("ordered_vars").value = var.get_param("reference").value
    product.execute()

    settings = _make(scene, set_mod, "render_settings")
    settings.get_param("products").value = product.get_param("reference").value
    settings.execute()

    return var, product, settings


class TestRenderVar:
    def test_authors_a_render_var_prim(self):
        scene, stage = _setup_stage()
        var, _product, _settings = _render_network(scene)

        prim = stage.GetPrimAtPath(var.get_param("reference").value)
        assert prim, "Render Var should author a prim at its Reference path"

        render_var = UsdRender.Var(prim)
        assert render_var.GetDataTypeAttr().Get() == "color3f"
        assert render_var.GetSourceNameAttr().Get() == "Ci"
        assert render_var.GetSourceTypeAttr().Get() == "raw"

    def test_reference_output_is_the_authored_path(self):
        scene, stage = _setup_stage()
        var, _product, _settings = _render_network(scene)

        assert var.get_param("reference").value == var.get_param("primpath_location").value


class TestRenderProduct:
    def test_ordered_vars_resolves_the_wired_var(self):
        scene, stage = _setup_stage()
        var, product, _settings = _render_network(scene)

        prim = stage.GetPrimAtPath(product.get_param("reference").value)
        assert prim

        targets = UsdRender.Product(prim).GetOrderedVarsRel().GetTargets()
        assert [str(t) for t in targets] == [var.get_param("reference").value]

    def test_product_name_and_type_are_authored(self):
        scene, stage = _setup_stage()
        _var, product, _settings = _render_network(scene)

        usd_product = UsdRender.Product(stage.GetPrimAtPath(product.get_param("reference").value))
        assert usd_product.GetProductNameAttr().Get() == "render.exr"
        assert usd_product.GetProductTypeAttr().Get() == "raster"


class TestRenderSettings:
    def test_authors_resolution_and_purposes(self):
        scene, stage = _setup_stage()
        _var, _product, settings = _render_network(scene)

        usd_settings = UsdRender.Settings(stage.GetPrimAtPath(settings.get_param("reference").value))
        assert tuple(usd_settings.GetResolutionAttr().Get()) == (1920, 1080)
        assert list(usd_settings.GetIncludedPurposesAttr().Get()) == ["default", "render"]

    def test_products_relationship_points_at_the_wired_product(self):
        scene, stage = _setup_stage()
        _var, product, settings = _render_network(scene)

        usd_settings = UsdRender.Settings(stage.GetPrimAtPath(settings.get_param("reference").value))
        targets = usd_settings.GetProductsRel().GetTargets()
        assert [str(t) for t in targets] == [product.get_param("reference").value]

    def test_stage_render_settings_metadata_is_set(self):
        """A renderer finds the settings prim through stage metadata, not by path
        convention — so the checkbox that writes it is what makes the network
        renderable without telling the renderer where to look."""
        scene, stage = _setup_stage()
        _var, _product, settings = _render_network(scene)

        stage_settings = UsdRender.Settings.GetStageRenderSettings(stage)
        assert stage_settings, "renderSettingsPrimPath should be authored by default"
        assert str(stage_settings.GetPath()) == settings.get_param("reference").value

    def test_metadata_not_written_when_the_checkbox_is_off(self):
        scene, stage = _setup_stage()
        set_mod = importlib.import_module("scripts.render_settings")

        settings = _make(scene, set_mod, "render_settings")
        settings.get_param("set_stage_settings").value = False
        settings.execute()

        assert not UsdRender.Settings.GetStageRenderSettings(stage)


class TestRenderRop:
    """The ROP authors nothing and renders in commit()."""

    def _rop(self, scene):
        mod = importlib.import_module("scripts.render")
        return mod, _make(scene, mod, "render")

    def test_execute_authors_nothing(self):
        scene, stage = _setup_stage()
        mod, rop = self._rop(scene)

        before = len(list(stage.TraverseAll()))
        rop.execute()
        assert len(list(stage.TraverseAll())) == before

    def test_commit_skipped_when_render_on_commit_is_off(self, monkeypatch):
        scene, _stage = _setup_stage()
        mod, rop = self._rop(scene)
        rop.get_param("render_on_commit").value = False

        called = []
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: called.append(a))

        mod.commit(rop)
        assert not called

    def test_missing_render_binary_is_reported_not_raised(self, monkeypatch):
        """usd-core wheels ship no usdrecord, so a missing binary is the common case
        and must not take the whole run down with it."""
        scene, _stage = _setup_stage()
        mod, rop = self._rop(scene)
        monkeypatch.setattr(mod.shutil, "which", lambda _cmd: None)

        called = []
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: called.append(a))

        mod.commit(rop)
        assert not called, "should bail before invoking a command that is not there"

    def test_commit_builds_the_expected_command(self, monkeypatch, tmp_path):
        scene, _stage = _setup_stage()
        _var, _product, _settings = _render_network(scene)
        mod, rop = self._rop(scene)

        rop.get_param("renderer").value = "Embree"
        rop.get_param("output_image").value = str(tmp_path / "beauty.####.png")
        rop.get_param("frames").value = "1:4"
        rop.get_param("image_width").value = 512
        rop.get_param("extra_args").value = "--complexity high"

        monkeypatch.setattr(mod.shutil, "which", lambda _cmd: "/usr/bin/usdrecord")
        captured = {}

        def _fake_run(argv, **kwargs):
            captured["argv"] = argv
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(mod.subprocess, "run", _fake_run)
        mod.commit(rop)

        argv = captured["argv"]
        assert argv[0] == "usdrecord"
        assert argv[argv.index("--renderer") + 1] == "Embree"
        assert argv[argv.index("--frames") + 1] == "1:4"
        assert argv[argv.index("--imageWidth") + 1] == "512"
        assert "--complexity" in argv and "high" in argv
        assert argv[-1] == str(tmp_path / "beauty.####.png")
        assert argv[-2].endswith(".usda"), "the staged scene file is the last positional input"

    def test_staged_scene_file_is_removed_unless_kept(self, monkeypatch, tmp_path):
        scene, _stage = _setup_stage()
        _render_network(scene)
        mod, rop = self._rop(scene)

        monkeypatch.setattr(mod.shutil, "which", lambda _cmd: "/usr/bin/usdrecord")
        staged = {}

        def _fake_run(argv, **kwargs):
            staged["path"] = argv[-2]
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(mod.subprocess, "run", _fake_run)
        mod.commit(rop)

        import os
        assert not os.path.exists(staged["path"]), "staged scene file should be cleaned up"

    def test_output_param_records_the_written_image(self, monkeypatch, tmp_path):
        scene, _stage = _setup_stage()
        _render_network(scene)
        mod, rop = self._rop(scene)
        rop.get_param("output_image").value = str(tmp_path / "out.png")

        monkeypatch.setattr(mod.shutil, "which", lambda _cmd: "/usr/bin/usdrecord")
        monkeypatch.setattr(
            mod.subprocess, "run",
            lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="", stderr=""),
        )
        mod.commit(rop)

        assert rop.get_param("output").value == str(tmp_path / "out.png")

    def test_failed_render_leaves_output_unset(self, monkeypatch):
        scene, _stage = _setup_stage()
        _render_network(scene)
        mod, rop = self._rop(scene)

        monkeypatch.setattr(mod.shutil, "which", lambda _cmd: "/usr/bin/usdrecord")
        monkeypatch.setattr(
            mod.subprocess, "run",
            lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr="boom"),
        )
        mod.commit(rop)

        assert not rop.get_param("output").value


class TestCliRunRenders:
    """`usdchunks run` on a network containing a Render chunk triggers the render,
    because the CLI calls commit_all() once the solve has settled."""

    def test_run_invokes_the_render(self, monkeypatch, tmp_path):
        scene, _stage = _setup_stage()
        _render_network(scene)
        mod = importlib.import_module("scripts.render")
        rop = _make(scene, mod, "render")
        rop.get_param("output_image").value = str(tmp_path / "run.png")

        net = tmp_path / "net.usda"
        export_chunks(scene, net)
        assert net.exists()

        chunk_registry.ChunkLayerManager().reset()

        monkeypatch.setattr(mod.shutil, "which", lambda _cmd: "/usr/bin/usdrecord")
        calls = []

        def _fake_run(argv, **kwargs):
            calls.append(argv)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(mod.subprocess, "run", _fake_run)

        cmd_run(types.SimpleNamespace(usda=str(net), params=[], export=None, printout=False))

        assert calls, "run should have committed the Render chunk"

    def test_param_override_reaches_the_render(self, monkeypatch, tmp_path):
        """The automation case: the output path is a normal param, so it is
        overridable from the command line like any other.

        Uses the bare `param=value` form rather than `Chunk.param=value` on purpose.
        Chunk names currently gain a numeric suffix across an export/load round trip
        (a chunk authored as "Render" reloads as "Render1" — reproducible with any
        chunk, including Light and Prim, so it is not specific to these), which makes
        the qualified form unusable against a serialized network. The bare form
        searches every chunk for a matching param and is unaffected.
        """
        scene, _stage = _setup_stage()
        _render_network(scene)
        mod = importlib.import_module("scripts.render")
        rop = _make(scene, mod, "render")
        rop.get_param("output_image").value = str(tmp_path / "default.png")

        net = tmp_path / "net.usda"
        export_chunks(scene, net)
        chunk_registry.ChunkLayerManager().reset()

        monkeypatch.setattr(mod.shutil, "which", lambda _cmd: "/usr/bin/usdrecord")
        calls = []
        monkeypatch.setattr(
            mod.subprocess, "run",
            lambda argv, **k: (calls.append(argv), types.SimpleNamespace(returncode=0, stdout="", stderr=""))[1],
        )

        override = str(tmp_path / "overridden.png")
        cmd_run(types.SimpleNamespace(
            usda=str(net),
            params=[f"output_image={override}"],
            export=None,
            printout=False,
        ))

        assert calls, "run should have committed the Render chunk"
        assert calls[-1][-1] == override
