"""Tests for the UsdRender chunks: Render Var, Render Product, Render Settings, Render.

The first three author standard UsdRender scene description and hand their prim path
downstream through a Reference output. The fourth is the ROP: it authors nothing, and
does its work in the reserved commit() hook so a render happens once, after the solve
has settled, rather than on every cook.
"""
from __future__ import annotations

import importlib
import types

import pytest
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

    def test_data_window_and_effect_switches_are_authored(self):
        scene, stage = _setup_stage()
        _var, _product, settings = _render_network(scene)

        settings.get_param("data_window_ndc").value = (0.25, 0.25, 0.75, 0.75)
        settings.get_param("disable_motion_blur").value = True
        settings.get_param("disable_depth_of_field").value = True
        settings.execute()

        usd_settings = UsdRender.Settings(stage.GetPrimAtPath(settings.get_param("reference").value))
        assert tuple(usd_settings.GetDataWindowNDCAttr().Get()) == (0.25, 0.25, 0.75, 0.75)
        assert usd_settings.GetDisableMotionBlurAttr().Get() is True
        assert usd_settings.GetDisableDepthOfFieldAttr().Get() is True

    def test_material_binding_purposes_and_color_space(self):
        scene, stage = _setup_stage()
        _var, _product, settings = _render_network(scene)

        settings.get_param("material_binding_purposes").value = "full, preview"
        settings.get_param("rendering_color_space").value = "acescg"
        settings.execute()

        usd_settings = UsdRender.Settings(stage.GetPrimAtPath(settings.get_param("reference").value))
        assert list(usd_settings.GetMaterialBindingPurposesAttr().Get()) == ["full", "preview"]
        assert usd_settings.GetRenderingColorSpaceAttr().Get() == "acescg"

    def test_empty_optional_tokens_are_left_unauthored(self):
        """An empty field must not author an empty token list — that would override
        the schema fallback with something meaningless."""
        scene, stage = _setup_stage()
        _var, _product, settings = _render_network(scene)

        prim = stage.GetPrimAtPath(settings.get_param("reference").value)
        assert not prim.GetAttribute("materialBindingPurposes").HasAuthoredValue()
        assert not prim.GetAttribute("renderingColorSpace").HasAuthoredValue()

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


def _schema_delegates():
    from usd_chunks import render_defs
    return [d for d in render_defs.iter_render_settings_defs() if d.source == "schema"]


requires_renderer = pytest.mark.skipif(
    not _schema_delegates(),
    reason="no renderer ships an applied API schema for RenderSettings in this USD build",
)


class TestDelegateSettings:
    """Renderer settings lofted onto the RenderSettings chunk from whatever the renderer
    declares -- an applied API schema for V-Ray, .args in Sdr for RenderMan."""

    def _settings_chunk(self, scene):
        mod = importlib.import_module("scripts.render_settings")
        return mod, _make(scene, mod, "render_settings")

    def test_no_delegate_by_default(self):
        scene, _stage = _setup_stage()
        _mod, settings = self._settings_chunk(scene)

        assert settings.get_param("delegate").value == "None"

    def test_variants_offer_every_describable_renderer(self):
        mod = importlib.import_module("scripts.render_settings")
        labels = [label for label, _presets in mod.config().variants]

        from usd_chunks import render_defs
        assert labels == sorted(d.label for d in render_defs.iter_render_settings_defs())

    @requires_renderer
    def test_selecting_a_delegate_lofts_its_settings(self):
        scene, _stage = _setup_stage()
        _mod, settings = self._settings_chunk(scene)
        before = len(settings.get_my_params())

        settings.get_param("delegate").value = _schema_delegates()[0].label

        assert len(settings.get_my_params()) > before

    @requires_renderer
    def test_switching_delegates_clears_the_previous_ones(self):
        scene, _stage = _setup_stage()
        _mod, settings = self._settings_chunk(scene)
        label = _schema_delegates()[0].label

        settings.get_param("delegate").value = label
        lofted = len(settings.get_my_params())
        settings.get_param("delegate").value = "None"

        assert len(settings.get_my_params()) < lofted

    @requires_renderer
    def test_api_schema_is_applied_to_the_settings_prim(self):
        scene, stage = _setup_stage()
        _mod, settings = self._settings_chunk(scene)
        definition = _schema_delegates()[0]
        settings.get_param("delegate").value = definition.label
        settings.execute()

        prim = stage.GetPrimAtPath(settings.get_param("reference").value)
        assert definition.schema_name in prim.GetAppliedSchemas()

    @requires_renderer
    def test_defaults_are_not_authored(self):
        """258 attributes at their schema defaults would bury the few that were changed."""
        scene, stage = _setup_stage()
        _mod, settings = self._settings_chunk(scene)
        definition = _schema_delegates()[0]
        settings.get_param("delegate").value = definition.label
        settings.execute()

        prim = stage.GetPrimAtPath(settings.get_param("reference").value)
        authored = [
            a.GetName() for a in prim.GetAttributes()
            if a.GetName().startswith(definition.namespace + ":") and a.HasAuthoredValue()
        ]
        assert authored == []

    @requires_renderer
    def test_a_changed_setting_is_authored_namespaced(self):
        scene, stage = _setup_stage()
        _mod, settings = self._settings_chunk(scene)
        definition = _schema_delegates()[0]
        settings.get_param("delegate").value = definition.label

        target = next(p for p in definition.props if p.sdf_type == "bool" and p.default is False)
        settings.get_param(target.name).value = True
        settings.execute()

        prim = stage.GetPrimAtPath(settings.get_param("reference").value)
        attr = prim.GetAttribute(target.attr_name)
        assert attr.HasAuthoredValue()
        assert attr.Get() is True


class _FakeRecorder:
    """Stands in for UsdAppUtils.FrameRecorder, which the usd-core wheels do not ship."""

    def __init__(self, succeed=True):
        self.succeed = succeed
        self.calls = []
        self.plugin = None
        self.width = None
        self.complexity = None
        self.deleted = False

    def SetRendererPlugin(self, plugin):
        self.plugin = plugin

    def SetImageWidth(self, width):
        self.width = width

    def SetComplexity(self, complexity):
        self.complexity = complexity

    def SetColorCorrectionMode(self, mode):
        self.mode = mode

    def Record(self, stage, camera, time_code, path):
        self.calls.append((time_code.GetValue(), path))
        return self.succeed

    def DeleteGLResources(self):
        self.deleted = True


class TestFrameSpec:
    """usdrecord-style frame specs, parsed here rather than shelled out for."""

    def test_empty_spec_means_a_single_default_render(self):
        mod = importlib.import_module("scripts.render")
        assert mod.parse_frames("") == []
        assert mod.parse_frames("   ") == []

    def test_single_frame_and_list(self):
        mod = importlib.import_module("scripts.render")
        assert mod.parse_frames("7") == [7.0]
        assert mod.parse_frames("1,3,5") == [1.0, 3.0, 5.0]

    def test_range_is_inclusive(self):
        mod = importlib.import_module("scripts.render")
        assert mod.parse_frames("1:4") == [1.0, 2.0, 3.0, 4.0]

    def test_strided_range(self):
        mod = importlib.import_module("scripts.render")
        assert mod.parse_frames("1:9x4") == [1.0, 5.0, 9.0]

    def test_backwards_range_is_rejected(self):
        import pytest
        mod = importlib.import_module("scripts.render")
        with pytest.raises(ValueError):
            mod.parse_frames("10:2")

    def test_non_positive_stride_is_rejected(self):
        import pytest
        mod = importlib.import_module("scripts.render")
        with pytest.raises(ValueError):
            mod.parse_frames("1:10x0")


class TestOutputPathFormatting:
    def test_hashes_pad_to_their_own_width(self):
        mod = importlib.import_module("scripts.render")
        assert mod.format_output_path("beauty.####.exr", 12) == "beauty.0012.exr"
        assert mod.format_output_path("beauty.##.exr", 7) == "beauty.07.exr"

    def test_frame_field_is_substituted(self):
        mod = importlib.import_module("scripts.render")
        assert mod.format_output_path("beauty.{frame}.exr", 12) == "beauty.12.exr"

    def test_path_untouched_without_a_frame(self):
        mod = importlib.import_module("scripts.render")
        assert mod.format_output_path("beauty.####.exr", None) == "beauty.####.exr"


class TestHydraMode:
    """The default path: render the composed stage in process, no staging, no PATH."""

    def _hydra_rop(self, monkeypatch, recorder=None):
        scene, stage = _setup_stage()
        _render_network(scene)
        mod = importlib.import_module("scripts.render")
        rop = _make(scene, mod, "render")
        rop.get_param("mode").value = "Hydra"

        from pxr import UsdGeom
        UsdGeom.Camera.Define(stage, "/cameras/shotcam")

        fake = recorder or _FakeRecorder()
        monkeypatch.setattr(mod, "_hydra_available", lambda: True)
        monkeypatch.setattr(mod, "_make_recorder", lambda _n: fake)
        return mod, rop, fake, stage

    def test_records_one_frame_by_default(self, monkeypatch, tmp_path):
        mod, rop, fake, _stage = self._hydra_rop(monkeypatch)
        rop.get_param("output_image").value = str(tmp_path / "beauty.png")

        mod.commit(rop)

        assert len(fake.calls) == 1
        assert fake.calls[0][1] == str(tmp_path / "beauty.png")

    def test_records_every_frame_in_the_spec(self, monkeypatch, tmp_path):
        mod, rop, fake, _stage = self._hydra_rop(monkeypatch)
        rop.get_param("output_image").value = str(tmp_path / "beauty.####.png")
        rop.get_param("frames").value = "1:3"

        mod.commit(rop)

        assert [c[0] for c in fake.calls] == [1.0, 2.0, 3.0]
        assert [c[1].endswith(n) for c, n in zip(fake.calls, ("0001.png", "0002.png", "0003.png"))]

    def test_never_shells_out(self, monkeypatch, tmp_path):
        """The whole point of the default mode — no subprocess, no PATH lookup."""
        mod, rop, _fake, _stage = self._hydra_rop(monkeypatch)
        rop.get_param("output_image").value = str(tmp_path / "beauty.png")

        called = []
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: called.append(a))
        mod.commit(rop)

        assert not called

    def test_gl_resources_released_even_when_recording_fails(self, monkeypatch, tmp_path):
        mod, rop, fake, _stage = self._hydra_rop(monkeypatch, _FakeRecorder(succeed=False))
        rop.get_param("output_image").value = str(tmp_path / "beauty.png")

        mod.commit(rop)

        assert fake.deleted
        assert not rop.get_param("output").value

    def test_missing_camera_is_reported(self, monkeypatch, tmp_path):
        scene, _stage = _setup_stage()
        mod = importlib.import_module("scripts.render")
        rop = _make(scene, mod, "render")
        rop.get_param("mode").value = "Hydra"
        rop.get_param("output_image").value = str(tmp_path / "beauty.png")

        fake = _FakeRecorder()
        monkeypatch.setattr(mod, "_hydra_available", lambda: True)
        monkeypatch.setattr(mod, "_make_recorder", lambda _n: fake)

        mod.commit(rop)
        assert not fake.calls, "should bail before recording when there is no camera"

    def test_falls_back_to_command_without_usdapputils(self, monkeypatch, tmp_path):
        """A build that cannot render in process should still render, not refuse."""
        scene, _stage = _setup_stage()
        _render_network(scene)
        mod = importlib.import_module("scripts.render")
        rop = _make(scene, mod, "render")
        rop.get_param("mode").value = "Hydra"
        rop.get_param("output_image").value = str(tmp_path / "beauty.png")

        monkeypatch.setattr(mod, "_hydra_available", lambda: False)
        monkeypatch.setattr(mod, "_resolve_command", lambda _c: ["usdrecord"])
        calls = []
        monkeypatch.setattr(
            mod.subprocess, "run",
            lambda argv, **k: (calls.append(argv), types.SimpleNamespace(returncode=0, stdout="", stderr=""))[1],
        )

        mod.commit(rop)
        assert calls, "should have fallen back to the command path"


class TestRenderRop:
    """The ROP authors nothing and renders in commit()."""

    def _rop(self, scene):
        mod = importlib.import_module("scripts.render")
        rop = _make(scene, mod, "render")
        rop.get_param("mode").value = "Command"
        return mod, rop

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

        # Command resolution has its own tests; pin it here so this one is only
        # about how the flags are assembled.
        monkeypatch.setattr(mod, "_resolve_command", lambda _cmd: ["usdrecord"])
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


class TestRenderCommandResolution:
    """USD ships usdrecord as an extension-less Python script. shutil.which finds it,
    but Windows cannot CreateProcess a file it has no association for — which failed
    with WinError 2 while the tool was plainly installed."""

    def test_extension_less_script_runs_through_the_interpreter(self, monkeypatch):
        import sys
        mod = importlib.import_module("scripts.render")
        monkeypatch.setattr(mod.os, "name", "nt")
        monkeypatch.setattr(mod.shutil, "which", lambda _c: r"C:\USD\bin\usdrecord")

        assert mod._resolve_command("usdrecord") == [sys.executable, r"C:\USD\bin\usdrecord"]

    def test_exe_runs_directly(self, monkeypatch):
        mod = importlib.import_module("scripts.render")
        monkeypatch.setattr(mod.os, "name", "nt")
        monkeypatch.setattr(mod.shutil, "which", lambda _c: r"C:\USD\bin\usdrecord.exe")

        assert mod._resolve_command("usdrecord") == [r"C:\USD\bin\usdrecord.exe"]

    def test_py_script_runs_through_the_interpreter_on_any_platform(self, monkeypatch):
        import sys
        mod = importlib.import_module("scripts.render")
        monkeypatch.setattr(mod.os, "name", "posix")
        monkeypatch.setattr(mod.shutil, "which", lambda _c: "/usr/bin/usdrecord.py")

        assert mod._resolve_command("usdrecord") == [sys.executable, "/usr/bin/usdrecord.py"]

    def test_missing_command_resolves_to_none(self, monkeypatch):
        mod = importlib.import_module("scripts.render")
        monkeypatch.setattr(mod.shutil, "which", lambda _c: None)

        assert mod._resolve_command("nope") is None

    def test_spawn_failure_is_logged_not_raised(self, monkeypatch):
        """The Render button dispatches through the notice system, where an exception
        kills the callback instead of telling the user anything useful."""
        scene, _stage = _setup_stage()
        mod = importlib.import_module("scripts.render")
        rop = _make(scene, mod, "render")
        rop.get_param("mode").value = "Command"

        monkeypatch.setattr(mod, "_resolve_command", lambda _c: ["usdrecord"])

        def _boom(*_a, **_k):
            raise OSError(2, "The system cannot find the file specified")

        monkeypatch.setattr(mod.subprocess, "run", _boom)
        mod.commit(rop)

        assert not rop.get_param("output").value


class TestRendererMenu:
    def test_menu_is_populated_and_never_empty(self):
        mod = importlib.import_module("scripts.render")
        assert mod._available_renderers(), "renderer menu must never be empty"

    def test_falls_back_when_nothing_can_be_enumerated(self, monkeypatch):
        """usd-core wheels ship neither UsdAppUtils nor UsdImagingGL."""
        mod = importlib.import_module("scripts.render")
        monkeypatch.setitem(__import__("sys").modules, "pxr.UsdAppUtils", None)

        renderers = mod._available_renderers()
        assert "GL" in renderers

    def test_renderer_param_is_a_menu_of_available_delegates(self):
        scene, _stage = _setup_stage()
        mod = importlib.import_module("scripts.render")
        rop = _make(scene, mod, "render")

        renderer = rop.get_param("renderer")
        assert list(renderer.menu_items) == mod._available_renderers()
        assert renderer.value in renderer.menu_items


class TestCliRunRenders:
    """`usdchunks run` on a network containing a Render chunk triggers the render,
    because the CLI calls commit_all() once the solve has settled."""

    def test_run_invokes_the_render(self, monkeypatch, tmp_path):
        scene, _stage = _setup_stage()
        _render_network(scene)
        mod = importlib.import_module("scripts.render")
        rop = _make(scene, mod, "render")
        rop.get_param("mode").value = "Command"
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
        rop.get_param("mode").value = "Command"
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
