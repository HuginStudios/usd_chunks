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
from pxr import Usd, UsdGeom, UsdRender

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import ChunkConfig, SceneSender
from usd_chunks import chunk_stage
from usd_chunks.chunk_stage import export_chunks
from usd_chunks.chunk_stage import initialize as init_chunk_usd
from usd_chunks.plugins.usdchunks_cli import cmd_run


class _DummyScene(SceneSender):
    def __init__(self):
        super().__init__()
        self._stage = None
        self.renderer = ""
        self.pushes = []

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []

    def get_viewport_renderer(self):
        return self.renderer

    def set_viewport_renderer_settings(self, settings):
        self.pushes.append(dict(settings))


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
    prev = chunk_stage._params_tab_create
    chunk_stage._params_tab_create = True
    try:
        scene.add_chunk(c)
    finally:
        chunk_stage._params_tab_create = prev
    return c


def _render_network(scene):
    """A var -> product -> settings chain, wired by prim path and cooked."""
    var_mod = importlib.import_module("scripts.usd.render_var")
    prod_mod = importlib.import_module("scripts.usd.render_product")
    set_mod = importlib.import_module("scripts.usd.render_settings")

    var = _make(scene, var_mod, "render_var")
    var.execute()

    product = _make(scene, prod_mod, "render_product")
    product.get_param("ordered_vars").value = var.get_param("primpath_location").value
    product.execute()

    settings = _make(scene, set_mod, "render_settings")
    settings.get_param("products").value = product.get_param("primpath_location").value
    settings.execute()

    return var, product, settings


class TestRenderVar:
    def test_authors_a_render_var_prim(self):
        scene, stage = _setup_stage()
        var, _product, _settings = _render_network(scene)

        prim = stage.GetPrimAtPath(var.get_param("primpath_location").value)
        assert prim, "Render Var should author a prim at its Reference path"

        render_var = UsdRender.Var(prim)
        assert render_var.GetDataTypeAttr().Get() == "color3f"
        assert render_var.GetSourceNameAttr().Get() == "Ci"
        assert render_var.GetSourceTypeAttr().Get() == "raw"

    def test_driver_params_author_namespaced_attrs(self):
        from usd_chunks import chunk

        scene, stage = _setup_stage()
        var_mod = importlib.import_module("scripts.usd.render_var")
        var = _make(scene, var_mod, "render_var")

        rows = var.get_param("driver_params", chunk.is_inst(chunk.ListGroup))
        rows.size = 4
        for i, (name, type_name, value) in enumerate([
            ("name", "string", "zDepth"),
            ("format", "token", "float"),
            ("vray:zDepth:depth_white", "float", "1.0"),
            ("driver:parameters:husk:multiSampled", "bool", "true"),
        ]):
            rows.get_indexed_param("dp_name", i).value = name
            rows.get_indexed_param("dp_type", i).value = type_name
            rows.get_indexed_param("dp_value", i).value = value
        var.execute()

        prim = stage.GetPrimAtPath(var.get_param("primpath_location").value)
        assert prim.GetAttribute("driver:parameters:aov:name").Get() == "zDepth"
        assert prim.GetAttribute("driver:parameters:aov:format").Get() == "float"
        # A bare name gets the aov: scope; one already spelling out driver:parameters:
        # is used verbatim, so it can sit outside that scope.
        assert prim.GetAttribute("driver:parameters:aov:vray:zDepth:depth_white").Get() == 1.0
        assert prim.GetAttribute("driver:parameters:husk:multiSampled").Get() is True

    def test_driver_params_skip_unusable_rows(self):
        from usd_chunks import chunk

        scene, stage = _setup_stage()
        var_mod = importlib.import_module("scripts.usd.render_var")
        var = _make(scene, var_mod, "render_var")

        rows = var.get_param("driver_params", chunk.is_inst(chunk.ListGroup))
        rows.size = 2
        rows.get_indexed_param("dp_name", 0).value = ""
        rows.get_indexed_param("dp_name", 1).value = "force_32_bit_output"
        rows.get_indexed_param("dp_type", 1).value = "int"
        rows.get_indexed_param("dp_value", 1).value = "not a number"
        var.execute()

        prim = stage.GetPrimAtPath(var.get_param("primpath_location").value)
        authored = [a.GetName() for a in prim.GetAttributes()
                    if a.GetName().startswith("driver:parameters:")]
        assert authored == []


class TestRenderProduct:
    def test_ordered_vars_resolves_the_wired_var(self):
        scene, stage = _setup_stage()
        var, product, _settings = _render_network(scene)

        prim = stage.GetPrimAtPath(product.get_param("primpath_location").value)
        assert prim

        targets = UsdRender.Product(prim).GetOrderedVarsRel().GetTargets()
        assert [str(t) for t in targets] == [var.get_param("primpath_location").value]

    def test_warns_when_usd_cannot_confirm_the_product(self, caplog):
        """USD checks for a written product by opening productName verbatim -- no frame
        expansion, no resolution -- so these two shapes report a product missing while
        the image is on disk."""
        import logging
        mod = importlib.import_module("scripts.usd.render_product")

        for name in ("renders/beauty.####.exr", "beauty.{frame}.exr", "renders/beauty.exr"):
            with caplog.at_level(logging.WARNING, logger=mod.logger.name):
                caplog.clear()
                mod._warn_if_usd_cannot_find_it(name)
            assert caplog.records, f"{name} should warn"

    def test_an_absolute_unnumbered_product_is_not_warned_about(self, caplog):
        import logging
        import os
        mod = importlib.import_module("scripts.usd.render_product")

        absolute = os.path.abspath("beauty.exr")
        with caplog.at_level(logging.WARNING, logger=mod.logger.name):
            caplog.clear()
            mod._warn_if_usd_cannot_find_it(absolute)
        assert not caplog.records, "an absolute path with no placeholder is checkable"

    def test_product_name_and_type_are_authored(self):
        scene, stage = _setup_stage()
        _var, product, _settings = _render_network(scene)

        product.get_param("product_name").value = "render.exr"
        product.execute()

        usd_product = UsdRender.Product(stage.GetPrimAtPath(product.get_param("primpath_location").value))
        assert usd_product.GetProductNameAttr().Get() == "render.exr"
        assert usd_product.GetProductTypeAttr().Get() == "raster"

    def test_unnamed_product_authors_no_product_name(self):
        """Output File is empty by default, and empty means silent.

        A product that has not been told where to write must not claim a filename of
        its own: the render would land in whichever directory it happened to run from,
        under a name nobody chose.
        """
        scene, stage = _setup_stage()
        _var, product, _settings = _render_network(scene)

        usd_product = UsdRender.Product(stage.GetPrimAtPath(product.get_param("primpath_location").value))
        assert not usd_product.GetProductNameAttr().HasAuthoredValue()
        assert usd_product.GetProductTypeAttr().Get() == "raster"


class TestRenderSettings:
    def test_authors_resolution_and_purposes(self):
        scene, stage = _setup_stage()
        _var, _product, settings = _render_network(scene)

        usd_settings = UsdRender.Settings(stage.GetPrimAtPath(settings.get_param("primpath_location").value))
        assert tuple(usd_settings.GetResolutionAttr().Get()) == (1920, 1080)
        assert list(usd_settings.GetIncludedPurposesAttr().Get()) == ["default", "render"]

    def test_products_relationship_points_at_the_wired_product(self):
        scene, stage = _setup_stage()
        _var, product, settings = _render_network(scene)

        usd_settings = UsdRender.Settings(stage.GetPrimAtPath(settings.get_param("primpath_location").value))
        targets = usd_settings.GetProductsRel().GetTargets()
        assert [str(t) for t in targets] == [product.get_param("primpath_location").value]

    def test_data_window_and_effect_switches_are_authored(self):
        scene, stage = _setup_stage()
        _var, _product, settings = _render_network(scene)

        settings.get_param("data_window_ndc").value = (0.25, 0.25, 0.75, 0.75)
        settings.get_param("disable_motion_blur").value = True
        settings.get_param("disable_depth_of_field").value = True
        settings.execute()

        usd_settings = UsdRender.Settings(stage.GetPrimAtPath(settings.get_param("primpath_location").value))
        assert tuple(usd_settings.GetDataWindowNDCAttr().Get()) == (0.25, 0.25, 0.75, 0.75)
        assert usd_settings.GetDisableMotionBlurAttr().Get() is True
        assert usd_settings.GetDisableDepthOfFieldAttr().Get() is True

    def test_material_binding_purposes_and_color_space(self):
        scene, stage = _setup_stage()
        _var, _product, settings = _render_network(scene)

        settings.get_param("material_binding_purposes").value = "full, preview"
        settings.get_param("rendering_color_space").value = "acescg"
        settings.execute()

        usd_settings = UsdRender.Settings(stage.GetPrimAtPath(settings.get_param("primpath_location").value))
        assert list(usd_settings.GetMaterialBindingPurposesAttr().Get()) == ["full", "preview"]
        assert usd_settings.GetRenderingColorSpaceAttr().Get() == "acescg"

    def test_empty_optional_tokens_are_left_unauthored(self):
        """An empty field must not author an empty token list — that would override
        the schema fallback with something meaningless."""
        scene, stage = _setup_stage()
        _var, _product, settings = _render_network(scene)

        prim = stage.GetPrimAtPath(settings.get_param("primpath_location").value)
        assert not prim.GetAttribute("materialBindingPurposes").HasAuthoredValue()
        assert not prim.GetAttribute("renderingColorSpace").HasAuthoredValue()

    def test_camera_from_a_messy_prim_selection_does_not_raise(self):
        """The camera param carries PRIM_SELECTION, so it is seeded from whatever was
        selected when the chunk was created. That can be several paths at once or a
        relative one, and a relationship target must be a single absolute path -- USD
        raises rather than ignoring a bad one, which took the whole cook down just by
        dropping the node onto the graph."""
        for value in ("not/absolute", "/cam_a,/cam_b", "   "):
            scene, stage = _setup_stage()
            from pxr import UsdGeom
            UsdGeom.Camera.Define(stage, "/cam_a")
            UsdGeom.Camera.Define(stage, "/cam_b")
            _var, product, settings = _render_network(scene)

            settings.get_param("camera").value = value
            product.get_param("camera").value = value
            settings.execute()
            product.execute()

    def test_multiple_selected_cameras_take_the_first(self):
        scene, stage = _setup_stage()
        from pxr import UsdGeom
        UsdGeom.Camera.Define(stage, "/cam_a")
        UsdGeom.Camera.Define(stage, "/cam_b")
        _var, _product, settings = _render_network(scene)

        settings.get_param("camera").value = "/cam_a,/cam_b"
        settings.execute()

        usd_settings = UsdRender.Settings(stage.GetPrimAtPath(settings.get_param("primpath_location").value))
        assert [str(t) for t in usd_settings.GetCameraRel().GetTargets()] == ["/cam_a"]

    def test_blank_camera_authors_no_relationship(self):
        scene, stage = _setup_stage()
        _var, _product, settings = _render_network(scene)

        settings.get_param("camera").value = "   "
        settings.execute()

        usd_settings = UsdRender.Settings(stage.GetPrimAtPath(settings.get_param("primpath_location").value))
        assert usd_settings.GetCameraRel().GetTargets() == []

    def test_stage_render_settings_metadata_is_set(self):
        """A renderer finds the settings prim through stage metadata, not by path
        convention — so the checkbox that writes it is what makes the network
        renderable without telling the renderer where to look."""
        scene, stage = _setup_stage()
        _var, _product, settings = _render_network(scene)

        stage_settings = UsdRender.Settings.GetStageRenderSettings(stage)
        assert stage_settings, "renderSettingsPrimPath should be authored by default"
        assert str(stage_settings.GetPath()) == settings.get_param("primpath_location").value

    def test_metadata_not_written_when_the_checkbox_is_off(self):
        scene, stage = _setup_stage()
        set_mod = importlib.import_module("scripts.usd.render_settings")

        settings = _make(scene, set_mod, "render_settings")
        settings.get_param("set_stage_settings").value = False
        settings.execute()

        assert not UsdRender.Settings.GetStageRenderSettings(stage)


def _populate_containing(owner, param_name):
    """Expand whichever lazy group holds param_name, so its param exists."""
    from usd_chunks.chunk_flags import ChunkFlags

    def walk(node):
        for child in node.get_my_params():
            if not hasattr(child, "get_my_params"):
                continue
            pending = getattr(child, "_pending_settings", None)
            if pending and any(p.name == param_name for p in pending):
                child.unset_state(ChunkFlags.State.COLLAPSED)
                return True
            if walk(child):
                return True
        return False

    return walk(owner)


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
        mod = importlib.import_module("scripts.usd.render_settings")
        return mod, _make(scene, mod, "render_settings")

    @requires_renderer
    def test_every_installed_renderer_gets_a_group(self):
        """All of them at once, not one selected renderer -- a RenderSettings prim is
        meant to carry several side by side, each in its own namespace."""
        scene, _stage = _setup_stage()
        _mod, settings = self._settings_chunk(scene)

        names = {p.name for p in settings.get_my_params()}
        for definition in _schema_delegates():
            assert definition.label in names

    def test_there_is_nothing_to_switch(self):
        """No delegate selector: one would have to be kept in step with the Render
        chunk's own renderer menu, and picking one would hide the others' settings."""
        scene, _stage = _setup_stage()
        _mod, settings = self._settings_chunk(scene)

        assert settings.get_param("delegate") is None

    @requires_renderer
    def test_an_untouched_renderer_leaves_no_trace_on_the_prim(self):
        """Installed is not the same as used: a renderer nobody has set a setting on
        should not claim its API schema on the prim."""
        scene, stage = _setup_stage()
        _mod, settings = self._settings_chunk(scene)
        settings.execute()

        prim = stage.GetPrimAtPath(settings.get_param("primpath_location").value)
        assert list(prim.GetAppliedSchemas()) == []

    @requires_renderer
    def test_groups_start_empty_and_fill_on_expand(self):
        """Building all 258 up front took twenty seconds before one was visible, so a
        collapsed group holds its settings until someone opens it."""
        from usd_chunks.chunk_flags import ChunkFlags

        scene, _stage = _setup_stage()
        _mod, settings = self._settings_chunk(scene)
        # every renderer is lofted on creation; nothing to select

        def first_pending(node):
            for child in node.get_my_params():
                if not hasattr(child, "get_my_params"):
                    continue
                if getattr(child, "_pending_settings", None):
                    return child
                if found := first_pending(child):
                    return found
            return None

        group = first_pending(settings)
        assert group is not None
        assert len(group.get_my_params()) == 0

        group.unset_state(ChunkFlags.State.COLLAPSED)
        assert len(group.get_my_params()) > 0

    @requires_renderer
    def test_a_saved_setting_survives_a_round_trip(self):
        """The values live on the chunk prim, but the params that hold them are built
        lazily -- and load restores inputs: attributes in prim order, so a setting can
        be restored before the delegate that creates its param. The value has to land
        anyway."""
        scene, _stage = _setup_stage()
        _mod, settings = self._settings_chunk(scene)
        definition = _schema_delegates()[0]
        # every renderer is lofted on creation; nothing to select

        target = next(p for p in definition.props if p.sdf_type == "bool" and p.default is False)
        _populate_containing(settings, target.name)
        settings.get_param(target.name).value = True
        settings.execute()

        net = __import__("tempfile").mkdtemp() + "/net.usda"
        export_chunks(scene, net)
        chunk_registry.ChunkLayerManager().reset()

        stage2 = Usd.Stage.Open(net)
        scene2 = _DummyScene()
        chunk_registry.ChunkRegistry().initialize(scene2, "pytest")
        scene2._stage = stage2
        init_chunk_usd(stage2, scene2)
        from usd_chunks.chunk_stage import load_chunks
        load_chunks(stage2, scene2)

        from usd_chunks.plugins.usdchunks_cli import _all_chunks

        reloaded = next(
            (c for c in _all_chunks(scene2.get_chunks())
             if c.get_param("set_stage_settings") is not None),
            None,
        )
        assert reloaded is not None, "the Render Settings chunk should come back on load"
        restored = reloaded.get_param(target.name)
        assert restored is not None, "the param holding a saved value must exist after load"
        assert restored.value is True

    @requires_renderer
    def test_api_schema_is_applied_once_a_setting_is_used(self):
        scene, stage = _setup_stage()
        _mod, settings = self._settings_chunk(scene)
        definition = _schema_delegates()[0]

        target = next(p for p in definition.props if p.sdf_type == "bool" and p.default is False)
        _populate_containing(settings, target.name)
        settings.get_param(target.name).value = True
        settings.execute()

        prim = stage.GetPrimAtPath(settings.get_param("primpath_location").value)
        assert definition.schema_name in prim.GetAppliedSchemas()

    @requires_renderer
    def test_defaults_are_not_authored(self):
        """258 attributes at their schema defaults would bury the few that were changed."""
        scene, stage = _setup_stage()
        _mod, settings = self._settings_chunk(scene)
        definition = _schema_delegates()[0]
        # every renderer is lofted on creation; nothing to select
        settings.execute()

        prim = stage.GetPrimAtPath(settings.get_param("primpath_location").value)
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
        # every renderer is lofted on creation; nothing to select

        target = next(p for p in definition.props if p.sdf_type == "bool" and p.default is False)
        _populate_containing(settings, target.name)
        settings.get_param(target.name).value = True
        settings.execute()

        prim = stage.GetPrimAtPath(settings.get_param("primpath_location").value)
        attr = prim.GetAttribute(target.attr_name)
        assert attr.HasAuthoredValue()
        assert attr.Get() is True


class TestCameraChoice:
    """Which camera a render used, and why. The third fallback is nobody's decision --
    whichever camera Traverse reaches first -- so a frame that looks nothing like the
    viewport needs the log to account for it."""

    def _stage(self):
        from pxr import Sdf, Usd, UsdGeom, UsdRender

        stage = Usd.Stage.CreateInMemory()
        UsdGeom.Camera.Define(stage, "/World/aaa_first")
        UsdGeom.Camera.Define(stage, "/World/shotCam")
        UsdRender.Settings.Define(stage, "/Render/settings")
        return stage, Sdf

    def _node(self, camera_value):
        return types.SimpleNamespace(
            get_param=lambda name: types.SimpleNamespace(value=camera_value)
        )

    def test_an_override_is_named_as_such(self, caplog):
        mod = importlib.import_module("scripts.usd.render")
        stage, _sdf = self._stage()

        with caplog.at_level("INFO"):
            camera = mod._resolve_camera(self._node("/World/shotCam"), stage, "/Render/settings")

        assert camera.GetPath() == "/World/shotCam"
        assert "set on this chunk" in caplog.text

    def test_a_settings_camera_is_named_as_such(self, caplog):
        mod = importlib.import_module("scripts.usd.render")
        stage, sdf = self._stage()
        from pxr import UsdRender

        settings = UsdRender.Settings(stage.GetPrimAtPath("/Render/settings"))
        settings.CreateCameraRel().SetTargets([sdf.Path("/World/shotCam")])

        with caplog.at_level("INFO"):
            camera = mod._resolve_camera(self._node(""), stage, "/Render/settings")

        assert camera.GetPath() == "/World/shotCam"
        assert "named by Render Settings" in caplog.text

    def test_the_arbitrary_fallback_says_nothing_chose_it(self, caplog):
        """The one worth flagging: it takes whichever camera sorts first, which is why
        a render can come out framed differently from the viewport."""
        mod = importlib.import_module("scripts.usd.render")
        stage, _sdf = self._stage()

        with caplog.at_level("INFO"):
            camera = mod._resolve_camera(self._node(""), stage, "/Render/settings")

        assert camera.GetPath() == "/World/aaa_first", "not the one anyone picked"
        assert "nothing chose it" in caplog.text

    def test_no_camera_still_returns_none(self):
        from pxr import Usd

        mod = importlib.import_module("scripts.usd.render")

        assert mod._resolve_camera(self._node(""), Usd.Stage.CreateInMemory(), "") is None


class TestViewportPush:
    """Settings handed straight to the running delegate, because most renderers read
    nothing else -- Cycles, RPR, Aurora and OSPRay all decline Hydra's renderSettings
    prim, so what the chunk authors for them reaches them through nothing at all.

    Driven off a stand-in def rather than a real renderer: what is under test is the
    diffing and the renderer filter, and those are the same whoever declared the
    settings.
    """

    def _def(self):
        from usd_chunks import render_defs

        return render_defs.RenderSettingsDef(
            key="delegate:HdFakePlugin",
            label="Fake",
            namespace="fake",
            source="delegate",
            props=[
                render_defs.RenderSettingProp(
                    name="fake_samples", attr_name="fake:samples",
                    sdf_type="int", default=64,
                ),
                render_defs.RenderSettingProp(
                    name="fake_bounces", attr_name="fake:bounces",
                    sdf_type="int", default=4,
                ),
            ],
        )

    def _rig(self, monkeypatch, values, renderer="HdFakePlugin"):
        """A node whose params are exactly `values`, and a scene that records pushes.

        The scene goes in through the registry the same way every other test here sets
        one up. Monkeypatching the attribute onto the singleton instead would delete it
        on teardown whenever it did not already exist, which takes out whichever test
        runs next and reads it.
        """
        from usd_chunks import render_defs

        mod = importlib.import_module("scripts.usd.render_settings")
        definition = self._def()
        monkeypatch.setattr(
            render_defs, "def_for_plugin",
            lambda plugin_id: definition if plugin_id == "HdFakePlugin" else None,
        )

        scene, _stage = _setup_stage()
        scene.renderer = renderer
        params = {name: types.SimpleNamespace(value=v) for name, v in values.items()}
        node = types.SimpleNamespace(get_param=params.get)
        return mod, node, scene, scene.pushes

    def test_only_params_that_exist_are_sent(self, monkeypatch):
        """A param is built when its group is first expanded, so one that does not exist
        has never been touched and the delegate already sits at the same fallback."""
        mod, node, _scene, pushes = self._rig(monkeypatch, {"fake_samples": 128})
        mod._push_to_viewport(node)

        assert pushes == [{"fake:samples": 128}]

    def test_an_unchanged_cook_sends_nothing(self, monkeypatch):
        """Each setting costs the viewport a render restart and execute() runs on every
        param drag, so re-sending what has not moved is what makes it unusable."""
        mod, node, _scene, pushes = self._rig(monkeypatch, {"fake_samples": 128})
        mod._push_to_viewport(node)
        mod._push_to_viewport(node)

        assert len(pushes) == 1

    def test_only_what_changed_is_sent(self, monkeypatch):
        mod, node, _scene, pushes = self._rig(
            monkeypatch, {"fake_samples": 128, "fake_bounces": 3}
        )
        mod._push_to_viewport(node)
        node.get_param("fake_samples").value = 256
        mod._push_to_viewport(node)

        assert pushes[-1] == {"fake:samples": 256}

    def test_a_value_back_at_its_default_is_still_sent(self, monkeypatch):
        """Nothing resolves a fallback on the delegate's side the way USD does for an
        unauthored attribute -- a setting stays wherever it was last put."""
        mod, node, _scene, pushes = self._rig(monkeypatch, {"fake_samples": 128})
        mod._push_to_viewport(node)
        node.get_param("fake_samples").value = 64  # the prop's declared default
        mod._push_to_viewport(node)

        assert pushes[-1] == {"fake:samples": 64}

    def test_another_renderer_gets_nothing(self, monkeypatch):
        """A delegate redraws for every setting handed to it and discards the namespaces
        it does not own, so sending it another renderer's is pure stall."""
        mod, node, _scene, pushes = self._rig(
            monkeypatch, {"fake_samples": 128}, renderer="HdOtherPlugin"
        )
        mod._push_to_viewport(node)

        assert pushes == []

    def test_switching_renderer_resends_everything(self, monkeypatch):
        """What the last delegate was told says nothing about what this one already has."""
        mod, node, scene, pushes = self._rig(monkeypatch, {"fake_samples": 128})
        mod._push_to_viewport(node)
        scene.renderer = "HdOtherPlugin"
        mod._push_to_viewport(node)
        scene.renderer = "HdFakePlugin"
        mod._push_to_viewport(node)

        assert pushes[-1] == {"fake:samples": 128}

    def test_no_viewport_is_not_an_error(self, monkeypatch):
        """A headless cook, a CLI run, or a DCC with no Hydra viewport at all."""
        mod, node, scene, pushes = self._rig(monkeypatch, {"fake_samples": 128})
        scene.renderer = ""
        mod._push_to_viewport(node)

        assert pushes == []


class TestViewportPushSeam:
    """The SceneSender seam the push goes through. Its defaults have to be inert, since
    every DCC that is not usdview inherits them."""

    def test_the_base_sender_does_nothing(self):
        class _Bare(SceneSender):
            def get_stage(self):
                return None

            def get_selected_prims(self):
                return []

        scene = _Bare()

        assert scene.get_viewport_renderer() == ""
        assert scene.set_viewport_renderer_settings({"fake:samples": 1}) is None


class TestRenderPass:
    """A pass sits above settings: it names which rendering this is and what it renders
    with. Also a possible way in -- a delegate may implement passes without implementing
    render settings prims."""

    def _pass(self, scene, settings):
        mod = importlib.import_module("scripts.usd.render_pass")
        rp = _make(scene, mod, "render_pass")
        rp.get_param("render_source").value = settings.get_param("primpath_location").value
        rp.execute()
        return rp

    def test_authors_a_render_pass_pointing_at_the_settings(self):
        scene, stage = _setup_stage()
        _var, _product, settings = _render_network(scene)
        rp = self._pass(scene, settings)

        prim = stage.GetPrimAtPath(rp.get_param("primpath_location").value)
        assert prim
        targets = UsdRender.Pass(prim).GetRenderSourceRel().GetTargets()
        assert [str(t) for t in targets] == [settings.get_param("primpath_location").value]

    def test_pass_type_and_input_passes(self):
        scene, stage = _setup_stage()
        _var, _product, settings = _render_network(scene)
        first = self._pass(scene, settings)

        mod = importlib.import_module("scripts.usd.render_pass")
        second = _make(scene, mod, "render_pass")
        second.get_param("primpath_location").value = "/Render/Passes/comp"
        second.get_param("pass_type").value = "comp"
        second.get_param("input_passes").value = first.get_param("primpath_location").value
        second.execute()

        prim = stage.GetPrimAtPath(second.get_param("primpath_location").value)
        usd_pass = UsdRender.Pass(prim)
        assert usd_pass.GetPassTypeAttr().Get() == "comp"
        assert [str(t) for t in usd_pass.GetInputPassesRel().GetTargets()] == [
            first.get_param("primpath_location").value
        ]

    def test_only_one_render_source_is_kept(self):
        """renderSource is what this pass renders, and a pass renders one thing."""
        scene, stage = _setup_stage()
        _var, _product, settings = _render_network(scene)

        mod = importlib.import_module("scripts.usd.render_pass")
        rp = _make(scene, mod, "render_pass")
        rp.get_param("render_source").value = "/Render/settings,/Render/Products/beauty"
        rp.execute()

        targets = UsdRender.Pass(stage.GetPrimAtPath(rp.get_param("primpath_location").value))
        assert len(targets.GetRenderSourceRel().GetTargets()) == 1

    def test_render_chunk_takes_settings_from_the_pass(self):
        """usdrecord's resolution order: explicit, then via the pass, then the stage."""
        scene, stage = _setup_stage()
        _var, _product, settings = _render_network(scene)
        rp = self._pass(scene, settings)

        mod = importlib.import_module("scripts.usd.render")
        rop = _make(scene, mod, "render")
        rop.get_param("pass_path").value = rp.get_param("primpath_location").value
        assert not rop.get_param("settings_path").value

        assert mod._resolve_settings_path(rop, stage) == settings.get_param("primpath_location").value
        assert mod._resolve_pass_path(rop, stage) == rp.get_param("primpath_location").value

    def test_an_explicit_settings_path_still_wins(self):
        scene, stage = _setup_stage()
        _var, _product, settings = _render_network(scene)
        rp = self._pass(scene, settings)

        mod = importlib.import_module("scripts.usd.render")
        rop = _make(scene, mod, "render")
        rop.get_param("pass_path").value = rp.get_param("primpath_location").value
        rop.get_param("settings_path").value = "/Render/other"

        assert mod._resolve_settings_path(rop, stage) == "/Render/other"

    def test_a_missing_pass_is_reported_not_used(self):
        scene, stage = _setup_stage()
        _render_network(scene)
        mod = importlib.import_module("scripts.usd.render")
        rop = _make(scene, mod, "render")
        rop.get_param("pass_path").value = "/Render/Passes/nope"

        assert mod._resolve_pass_path(rop, stage) == ""

    def test_pass_is_handed_to_the_delegate(self, monkeypatch, tmp_path):
        scene, stage = _setup_stage()
        _var, _product, settings = _render_network(scene)
        rp = self._pass(scene, settings)

        from pxr import UsdGeom
        UsdGeom.Camera.Define(stage, "/cameras/shotcam")

        mod = importlib.import_module("scripts.usd.render")
        rop = _make(scene, mod, "render")
        rop.get_param("mode").value = "Hydra"
        rop.get_param("pass_path").value = rp.get_param("primpath_location").value
        rop.get_param("output_image").value = str(tmp_path / "beauty.exr")

        captured = {}
        monkeypatch.setattr(mod, "_hydra_available", lambda: True)

        def _fake(node, settings_path="", pass_path=""):
            captured.update(settings=settings_path, pass_=pass_path)
            return _FakeRecorder()

        monkeypatch.setattr(mod, "_make_recorder", _fake)
        mod.commit(rop)

        assert captured["pass_"] == rp.get_param("primpath_location").value
        assert captured["settings"] == settings.get_param("primpath_location").value


class _FakeRecorder:
    """Stands in for UsdAppUtils.FrameRecorder, which the usd-core wheels do not ship."""

    def __init__(self, succeed=True):
        self.succeed = succeed
        self.calls = []
        self.plugin = None
        self.width = None
        self.complexity = None
        self.deleted = False
        self.settings_path = None

    def SetRendererPlugin(self, plugin):
        self.plugin = plugin

    def SetImageWidth(self, width):
        self.width = width

    def SetComplexity(self, complexity):
        self.complexity = complexity

    def SetColorCorrectionMode(self, mode):
        self.mode = mode

    def SetActiveRenderSettingsPrimPath(self, path):
        self.settings_path = str(path)

    def Record(self, stage, camera, time_code, path):
        self.calls.append((time_code.GetValue(), path))
        return self.succeed

    def DeleteGLResources(self):
        self.deleted = True


class TestFrameSpec:
    """usdrecord-style frame specs, parsed here rather than shelled out for."""

    def test_empty_spec_means_a_single_default_render(self):
        mod = importlib.import_module("scripts.usd.render")
        assert mod.parse_frames("") == []
        assert mod.parse_frames("   ") == []

    def test_single_frame_and_list(self):
        mod = importlib.import_module("scripts.usd.render")
        assert mod.parse_frames("7") == [7.0]
        assert mod.parse_frames("1,3,5") == [1.0, 3.0, 5.0]

    def test_range_is_inclusive(self):
        mod = importlib.import_module("scripts.usd.render")
        assert mod.parse_frames("1:4") == [1.0, 2.0, 3.0, 4.0]

    def test_strided_range(self):
        mod = importlib.import_module("scripts.usd.render")
        assert mod.parse_frames("1:9x4") == [1.0, 5.0, 9.0]

    def test_backwards_range_is_rejected(self):
        import pytest
        mod = importlib.import_module("scripts.usd.render")
        with pytest.raises(ValueError):
            mod.parse_frames("10:2")

    def test_non_positive_stride_is_rejected(self):
        import pytest
        mod = importlib.import_module("scripts.usd.render")
        with pytest.raises(ValueError):
            mod.parse_frames("1:10x0")


class TestOutputPathFormatting:
    def test_hashes_pad_to_their_own_width(self):
        mod = importlib.import_module("scripts.usd.render")
        assert mod.format_output_path("beauty.####.exr", 12) == "beauty.0012.exr"
        assert mod.format_output_path("beauty.##.exr", 7) == "beauty.07.exr"

    def test_frame_field_is_substituted(self):
        mod = importlib.import_module("scripts.usd.render")
        assert mod.format_output_path("beauty.{frame}.exr", 12) == "beauty.12.exr"

    def test_path_untouched_without_a_frame(self):
        mod = importlib.import_module("scripts.usd.render")
        assert mod.format_output_path("beauty.####.exr", None) == "beauty.####.exr"


class TestHydraMode:
    """The default path: render the composed stage in process, no staging, no PATH."""

    def _hydra_rop(self, monkeypatch, recorder=None):
        scene, stage = _setup_stage()
        _render_network(scene)
        mod = importlib.import_module("scripts.usd.render")
        rop = _make(scene, mod, "render")
        rop.get_param("mode").value = "Hydra"

        from pxr import UsdGeom
        UsdGeom.Camera.Define(stage, "/cameras/shotcam")

        fake = recorder or _FakeRecorder()
        monkeypatch.setattr(mod, "_hydra_available", lambda: True)
        monkeypatch.setattr(mod, "_make_recorder", lambda _n, settings_path="", pass_path="": fake)
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

    def test_settings_prim_is_handed_to_the_delegate(self, monkeypatch, tmp_path):
        """Not just read for a camera: a delegate implementing HdRenderSettings walks
        the RenderProducts and RenderVars it names and writes those files itself, which
        is what makes a network's AOVs come out instead of one beauty frame."""
        scene, stage = _setup_stage()
        _var, _product, settings = _render_network(scene)
        mod = importlib.import_module("scripts.usd.render")
        rop = _make(scene, mod, "render")
        rop.get_param("mode").value = "Hydra"
        rop.get_param("output_image").value = str(tmp_path / "beauty.png")
        rop.get_param("settings_path").value = settings.get_param("primpath_location").value

        from pxr import UsdGeom
        UsdGeom.Camera.Define(stage, "/cameras/shotcam")

        captured = {}
        monkeypatch.setattr(mod, "_hydra_available", lambda: True)

        def _fake_make(node, settings_path="", pass_path=""):
            captured["settings_path"] = settings_path
            fake = _FakeRecorder()
            if settings_path:
                fake.SetActiveRenderSettingsPrimPath(settings_path)
            return fake

        monkeypatch.setattr(mod, "_make_recorder", _fake_make)
        mod.commit(rop)

        assert captured["settings_path"] == settings.get_param("primpath_location").value

    def test_settings_prim_falls_back_to_the_stage_metadatum(self, monkeypatch, tmp_path):
        """A Render Settings chunk publishes renderSettingsPrimPath by default, so a
        network that wired nothing should still render the settings it authored."""
        scene, stage = _setup_stage()
        _var, _product, settings = _render_network(scene)
        mod = importlib.import_module("scripts.usd.render")
        rop = _make(scene, mod, "render")
        rop.get_param("mode").value = "Hydra"
        rop.get_param("output_image").value = str(tmp_path / "beauty.png")
        assert not rop.get_param("settings_path").value

        from pxr import UsdGeom
        UsdGeom.Camera.Define(stage, "/cameras/shotcam")

        captured = {}
        monkeypatch.setattr(mod, "_hydra_available", lambda: True)
        monkeypatch.setattr(
            mod, "_make_recorder",
            lambda node, settings_path="", pass_path="": (captured.setdefault("p", settings_path), _FakeRecorder())[1],
        )
        mod.commit(rop)

        assert captured["p"] == settings.get_param("primpath_location").value

    def test_relative_paths_resolve_against_the_working_directory(self, monkeypatch, tmp_path):
        """The same base USD gives a relative RenderProduct productName.

        Resolving against the scene file instead splits the two: the image lands beside
        the .usda while the product it declared is looked for beside the shell, and
        `usdchunks run scene.usda -o out.exr` writes somewhere the command line did not
        ask for.
        """
        import os
        mod = importlib.import_module("scripts.usd.render")

        scene_file = tmp_path / "shot" / "scene.usda"
        scene_file.parent.mkdir()
        stage = Usd.Stage.CreateNew(str(scene_file))
        stage.Save()

        cwd = tmp_path / "elsewhere"
        cwd.mkdir()
        monkeypatch.chdir(cwd)

        # Resolved against cwd, and specifically not against the directory holding the stage.
        assert mod._cwd_relative("renders/b.exr") == os.path.normpath(
            str(cwd / "renders" / "b.exr")
        )
        assert mod._cwd_relative("renders/b.exr") != os.path.normpath(
            str(tmp_path / "shot" / "renders" / "b.exr")
        )
        absolute = str(tmp_path / "abs.exr")
        assert mod._cwd_relative(absolute) == absolute

    def test_output_directory_is_created(self, monkeypatch, tmp_path):
        """No renderer creates it -- which is how a render that completed successfully
        still failed with "Unable to open file for write"."""
        mod, rop, fake, _stage = self._hydra_rop(monkeypatch)
        rop.get_param("output_image").value = str(tmp_path / "renders" / "beauty.exr")

        mod.commit(rop)

        assert (tmp_path / "renders").is_dir()
        assert fake.calls[0][1] == str(tmp_path / "renders" / "beauty.exr")

    def test_product_directories_are_created(self, monkeypatch, tmp_path):
        """The delegate writes RenderProducts itself, so their paths never pass through
        the recorder -- but they fail the same way when the folder is missing."""
        scene, stage = _setup_stage()
        _var, product, _settings = _render_network(scene)

        # Through the chunk's own param: a productName authored from outside is
        # overridden by the chunk layer that owns the prim.
        product.get_param("product_name").value = str(tmp_path / "aovs" / "beauty.exr")
        product.execute()

        mod = importlib.import_module("scripts.usd.render")
        rop = _make(scene, mod, "render")
        rop.get_param("mode").value = "Hydra"
        rop.get_param("output_image").value = str(tmp_path / "beauty.png")

        from pxr import UsdGeom
        UsdGeom.Camera.Define(stage, "/cameras/shotcam")

        monkeypatch.setattr(mod, "_hydra_available", lambda: True)
        monkeypatch.setattr(mod, "_make_recorder", lambda _n, settings_path="", pass_path="": _FakeRecorder())
        mod.commit(rop)

        assert (tmp_path / "aovs").is_dir()

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
        mod = importlib.import_module("scripts.usd.render")
        rop = _make(scene, mod, "render")
        rop.get_param("mode").value = "Hydra"
        rop.get_param("output_image").value = str(tmp_path / "beauty.png")

        fake = _FakeRecorder()
        monkeypatch.setattr(mod, "_hydra_available", lambda: True)
        monkeypatch.setattr(mod, "_make_recorder", lambda _n, settings_path="", pass_path="": fake)

        mod.commit(rop)
        assert not fake.calls, "should bail before recording when there is no camera"

    def test_falls_back_to_command_without_usdapputils(self, monkeypatch, tmp_path):
        """A build that cannot render in process should still render, not refuse."""
        scene, _stage = _setup_stage()
        _render_network(scene)
        mod = importlib.import_module("scripts.usd.render")
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
        mod = importlib.import_module("scripts.usd.render")
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
        mod = importlib.import_module("scripts.usd.render")
        monkeypatch.setattr(mod.os, "name", "nt")
        monkeypatch.setattr(mod.shutil, "which", lambda _c: r"C:\USD\bin\usdrecord")

        assert mod._resolve_command("usdrecord") == [sys.executable, r"C:\USD\bin\usdrecord"]

    def test_exe_runs_directly(self, monkeypatch):
        mod = importlib.import_module("scripts.usd.render")
        monkeypatch.setattr(mod.os, "name", "nt")
        monkeypatch.setattr(mod.shutil, "which", lambda _c: r"C:\USD\bin\usdrecord.exe")

        assert mod._resolve_command("usdrecord") == [r"C:\USD\bin\usdrecord.exe"]

    def test_py_script_runs_through_the_interpreter_on_any_platform(self, monkeypatch):
        import sys
        mod = importlib.import_module("scripts.usd.render")
        monkeypatch.setattr(mod.os, "name", "posix")
        monkeypatch.setattr(mod.shutil, "which", lambda _c: "/usr/bin/usdrecord.py")

        assert mod._resolve_command("usdrecord") == [sys.executable, "/usr/bin/usdrecord.py"]

    def test_missing_command_resolves_to_none(self, monkeypatch):
        mod = importlib.import_module("scripts.usd.render")
        monkeypatch.setattr(mod.shutil, "which", lambda _c: None)

        assert mod._resolve_command("nope") is None

    def test_spawn_failure_is_logged_not_raised(self, monkeypatch):
        """The Render button dispatches through the notice system, where an exception
        kills the callback instead of telling the user anything useful."""
        scene, _stage = _setup_stage()
        mod = importlib.import_module("scripts.usd.render")
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
        mod = importlib.import_module("scripts.usd.render")
        assert mod._available_renderers(), "renderer menu must never be empty"

    def test_falls_back_when_nothing_can_be_enumerated(self, monkeypatch):
        """usd-core wheels ship neither UsdAppUtils nor UsdImagingGL."""
        mod = importlib.import_module("scripts.usd.render")
        monkeypatch.setitem(__import__("sys").modules, "pxr.UsdAppUtils", None)

        renderers = mod._available_renderers()
        assert "GL" in renderers

    def test_renderer_param_is_a_menu_of_available_delegates(self):
        scene, _stage = _setup_stage()
        mod = importlib.import_module("scripts.usd.render")
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
        mod = importlib.import_module("scripts.usd.render")
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
        mod = importlib.import_module("scripts.usd.render")
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


class TestUnpatchedUsdBuild:
    """The Hydra path needs render-settings support in UsdAppUtils that stock USD does
    not have. Both of its absences are silent-ish -- one fails deep inside Record() with
    a message about an empty path, the other loses every AOV and says nothing -- so both
    are caught here instead, where the message can name the real cause."""

    def _hydra_render(self, monkeypatch, scene, stage, output_image):
        mod = importlib.import_module("scripts.usd.render")
        # Any camera will do: with none set anywhere, _resolve_camera refuses before
        # reaching the checks under test.
        UsdGeom.Camera.Define(stage, "/cam")
        _var, product, settings = _render_network(scene)
        product.get_param("product_name").value = "beauty.exr"
        product.execute()

        monkeypatch.setattr(mod, "_hydra_reads_render_products", lambda: False)
        # Nothing should reach a recorder; a build without the support cannot make one
        # that behaves, and a test that built one would be testing the delegate.
        monkeypatch.setattr(mod, "_make_recorder",
                            lambda *a, **k: pytest.fail("should have refused first"))

        node = _make(scene, mod, "render")
        node.get_param("output_image").value = output_image
        node.get_param("settings_path").value = settings.get_param("primpath_location").value
        return mod, node

    def test_refuses_product_driven_output(self, monkeypatch, caplog):
        import logging

        scene, stage = _setup_stage()
        mod, node = self._hydra_render(monkeypatch, scene, stage, "")

        with caplog.at_level(logging.ERROR, logger=mod.logger.name):
            mod._render_hydra(node, "")

        assert any("cannot let the render products decide" in r.message for r in caplog.records), \
            "an empty Output Image on an unpatched build must be refused by name"

    def test_warns_that_aovs_will_be_missing(self, monkeypatch, caplog):
        import logging

        scene, stage = _setup_stage()
        mod, node = self._hydra_render(monkeypatch, scene, stage, "beauty.exr")
        monkeypatch.setattr(mod, "_make_recorder", lambda *a, **k: None)

        with caplog.at_level(logging.WARNING, logger=mod.logger.name):
            mod._render_hydra(node, "beauty.exr")

        assert any("will not be in the output" in r.message for r in caplog.records), \
            "losing the render vars must not be silent"

    def test_says_nothing_on_a_build_that_supports_it(self, monkeypatch, caplog):
        import logging

        scene, stage = _setup_stage()
        mod, node = self._hydra_render(monkeypatch, scene, stage, "beauty.exr")
        monkeypatch.setattr(mod, "_hydra_reads_render_products", lambda: True)
        monkeypatch.setattr(mod, "_make_recorder", lambda *a, **k: None)

        with caplog.at_level(logging.WARNING, logger=mod.logger.name):
            mod._render_hydra(node, "beauty.exr")

        assert not [r for r in caplog.records if "will not be in the output" in r.message], \
            "a build with the support must not be warned about"
