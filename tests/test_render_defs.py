"""Tests for render_defs: renderer settings normalised from wherever they are declared.

Every renderer authors settings the same way -- namespaced attributes on the
RenderSettings prim -- but declares them differently. V-Ray ships an applied API schema;
RenderMan registers .args files into Sdr; Cycles ships neither and only tells Hydra at
runtime. These cover the shared model all three produce, and each backend against a real
renderer when one is registered.

The renderer-dependent tests skip rather than fail when no such renderer is installed:
the usd-core wheels carry no render delegates at all, and "no renderers describable" is
the correct answer there rather than an error.
"""
from __future__ import annotations

import pytest

from usd_chunks import render_defs


def _schema_defs():
    return [d for d in render_defs.iter_render_settings_defs() if d.source == "schema"]


def _delegate_defs():
    return [d for d in render_defs.iter_render_settings_defs() if d.source == "delegate"]


requires_renderer = pytest.mark.skipif(
    not _schema_defs(),
    reason="no renderer ships an applied API schema for RenderSettings in this USD build",
)

requires_delegate = pytest.mark.skipif(
    not _delegate_defs(),
    reason="no loadable render delegate here declares its settings only to Hydra",
)


class _Setting:
    """Stand-in for a Hydra render setting descriptor as the Python binding hands it out.

    Its type is an enum whose name is the only part read, so the real
    UsdImagingGL.RendererSettingType is not needed to exercise the mapping -- which
    matters, since the wheels these tests usually run against ship no imaging at all.
    """

    class _Type:
        def __init__(self, name):
            self.name = name

    def __init__(self, key, name, type_name, default):
        self.key = key
        self.name = name
        self.type = self._Type(type_name)
        self.defValue = default


class TestGrouping:
    """Grouping is what makes 258 settings navigable, so it is worth pinning precisely."""

    def _prop(self, attr_name):
        return render_defs.RenderSettingProp(
            name=attr_name.replace(":", "_"), attr_name=attr_name, sdf_type="float"
        )

    def test_second_namespace_segment_wins(self):
        props = [self._prop(n) for n in ("vray:cloud:upload", "vray:cloud:job",
                                         "vray:geom:x", "vray:geom:y")]
        groups = render_defs.group_props(props)

        assert [p.attr_name for p in groups["cloud"]] == ["vray:cloud:upload", "vray:cloud:job"]
        assert [p.attr_name for p in groups["geom"]] == ["vray:geom:x", "vray:geom:y"]

    def test_labels_drop_what_the_group_already_says(self):
        """A setting in a MotionBlur group is "bias", not "SettingsMotionBlur bias" --
        the path is the groups it sits in, and repeating it makes every label too wide."""
        props = [self._prop(f"vray:SettingsMotionBlur_{n}") for n in ("bias", "geom_samples")]
        groups = render_defs.group_props(props)

        assert [p.label for p in groups["SettingsMotionBlur"]] == ["bias", "geom samples"]

    def test_relabelling_leaves_names_and_attrs_alone(self):
        """Only the label is shortened: the param name must stay unique across groups and
        the attribute name is what gets authored."""
        props = [self._prop(f"vray:SettingsMotionBlur_{n}") for n in ("bias", "on")]
        groups = render_defs.group_props(props)

        assert [p.name for p in groups["SettingsMotionBlur"]] == [
            "vray_SettingsMotionBlur_bias", "vray_SettingsMotionBlur_on",
        ]
        assert [p.attr_name for p in groups["SettingsMotionBlur"]] == [
            "vray:SettingsMotionBlur_bias", "vray:SettingsMotionBlur_on",
        ]

    def test_shared_underscore_prefix_earns_a_group(self):
        props = [self._prop(f"vray:BakeView_{n}") for n in ("fov", "mode", "dilation")]
        groups = render_defs.group_props(props)

        assert "BakeView" in groups
        assert len(groups["BakeView"]) == 3

    def test_a_lone_prefix_falls_back_to_general(self):
        """One setting sharing a prefix with nothing is not a section of one."""
        props = [self._prop("vray:odd_one"), self._prop("vray:cloud:a"), self._prop("vray:cloud:b")]
        groups = render_defs.group_props(props)

        assert "odd" not in groups
        assert [p.attr_name for p in groups["General"]] == ["vray:odd_one"]

    def test_general_is_last(self):
        props = [self._prop("vray:loose"), self._prop("vray:cloud:a"), self._prop("vray:cloud:b")]
        groups = render_defs.group_props(props)

        assert list(groups)[-1] == "General"

    def test_a_big_group_nests_on_the_next_segment(self):
        props = [self._prop(f"vray:Opt_geom_{i}") for i in range(5)]
        props += [self._prop(f"vray:Opt_light_{i}") for i in range(5)]
        groups = render_defs.group_props(props)

        assert isinstance(groups["Opt"], dict)
        assert set(groups["Opt"]) == {"geom", "light"}
        assert len(groups["Opt"]["geom"]) == 5

    def test_a_small_group_is_left_flat(self):
        """Nesting a group that already fits costs a click and buys nothing."""
        props = [self._prop(f"vray:Opt_geom_{i}") for i in range(3)]
        groups = render_defs.group_props(props)

        assert isinstance(groups["Opt"], list)

    def test_an_unproductive_split_is_rejected(self):
        """A group that divides into a couple of pairs and a pile of leftovers reads
        worse split than flat -- BakeView splits into u, v and ten others."""
        props = [self._prop(f"vray:Bake_u_{i}") for i in range(2)]
        props += [self._prop(f"vray:Bake_v_{i}") for i in range(2)]
        props += [self._prop(f"vray:Bake_thing{i}") for i in range(10)]
        groups = render_defs.group_props(props)

        assert isinstance(groups["Bake"], list)
        assert len(groups["Bake"]) == 14

    def test_nesting_stops_at_max_depth(self):
        deep = [self._prop("vray:" + "_".join(f"s{d}" for d in range(8)) + f"_{i}") for i in range(20)]
        groups = render_defs.group_props(deep)

        def depth(node, d=0):
            if not isinstance(node, dict):
                return d
            return max(depth(v, d + 1) for v in node.values())

        assert depth(groups) <= render_defs._MAX_DEPTH + 1

    def test_every_prop_lands_somewhere_exactly_once(self):
        props = [self._prop(n) for n in (
            "vray:cloud:a", "vray:cloud:b", "vray:loose", "vray:Opt_geom_x", "vray:Opt_geom_y",
        )]
        groups = render_defs.group_props(props)

        def flatten(node):
            for value in node.values():
                if isinstance(value, dict):
                    yield from flatten(value)
                else:
                    yield from value

        assert sorted(p.attr_name for p in flatten(groups)) == sorted(p.attr_name for p in props)

    def test_unnamespaced_names_survive(self):
        props = [self._prop("plain")]
        groups = render_defs.group_props(props)

        assert groups["General"][0].attr_name == "plain"


class TestSchemaBackend:
    @requires_renderer
    def test_discovers_a_renderer(self):
        defs = _schema_defs()
        assert defs
        for definition in defs:
            assert definition.schema_name
            assert definition.namespace
            assert definition.props

    @requires_renderer
    def test_props_carry_type_default_and_docs(self):
        """Type, fallback and documentation all come from the schema, which is what
        makes a generated params UI worth having rather than a wall of strings."""
        definition = _schema_defs()[0]

        assert any(p.sdf_type for p in definition.props)
        assert any(p.default is not None for p in definition.props)
        assert any(p.help for p in definition.props), "docs become param tooltips"

    @requires_renderer
    def test_attr_names_are_namespaced_and_param_names_are_not(self):
        """':' is the USD namespace separator but reads badly in a param path, so the
        two names differ deliberately."""
        definition = _schema_defs()[0]
        prop = next(p for p in definition.props if ":" in p.attr_name)

        assert prop.attr_name.startswith(definition.namespace + ":")
        assert ":" not in prop.name

    @requires_renderer
    def test_every_prop_builds_a_param(self):
        definition = _schema_defs()[0]
        unmapped = [p for p in definition.props if render_defs.param_for_setting(p) is None]

        assert not unmapped, f"unmapped types: {sorted({p.sdf_type for p in unmapped})}"

    @requires_renderer
    def test_lookup_by_key_round_trips(self):
        definition = _schema_defs()[0]
        assert render_defs.get_render_settings_def(definition.key).key == definition.key

    def test_unknown_key_is_none_not_an_error(self):
        assert render_defs.get_render_settings_def("NotARenderer") is None

    def test_available_delegates_matches_the_defs(self):
        assert render_defs.available_delegates() == [
            d.label for d in render_defs.iter_render_settings_defs()
        ]


class TestDelegateBackend:
    """Cycles declares its settings in C++ and ships neither a schema nor .args, so the
    descriptors a loaded delegate hands out are the only description that exists."""

    def _settings(self):
        return [
            _Setting("cycles:samples", "Sample Count", "INT", 1024),
            _Setting("cycles:integrator:max_bounce", "Max Bounce", "INT", 7),
            _Setting("cycles:integrator:use_denoise", "Use Denoiser", "FLAG", False),
            _Setting("cycles:integrator:ao_factor", "AO Factor", "FLOAT", 0.0),
        ]

    def test_descriptors_become_a_def(self):
        definition = render_defs._def_from_delegate(
            "HdCyclesPlugin", "Cycles", self._settings()
        )

        assert definition.key == "delegate:HdCyclesPlugin"
        assert definition.label == "Cycles"
        assert definition.namespace == "cycles"
        assert definition.source == "delegate"
        assert len(definition.props) == 4

    def test_no_schema_to_apply(self):
        """There is no API schema behind a descriptor, so nothing must claim one -- the
        chunk applies schema_name to the prim whenever it authors a setting."""
        definition = render_defs._def_from_delegate("HdCyclesPlugin", "Cycles", self._settings())

        assert definition.schema_name == ""

    def test_setting_types_map_to_sdf_types(self):
        definition = render_defs._def_from_delegate("HdCyclesPlugin", "Cycles", self._settings())
        by_attr = {p.attr_name: p for p in definition.props}

        assert by_attr["cycles:samples"].sdf_type == "int"
        assert by_attr["cycles:integrator:use_denoise"].sdf_type == "bool"
        assert by_attr["cycles:integrator:ao_factor"].sdf_type == "float"

    def test_the_key_is_the_attribute_to_author(self):
        """A descriptor key is already the namespaced attribute name, which is what makes
        this backend a matter of renaming rather than translation."""
        definition = render_defs._def_from_delegate("HdCyclesPlugin", "Cycles", self._settings())
        prop = next(p for p in definition.props if p.attr_name == "cycles:integrator:max_bounce")

        assert prop.name == "cycles_integrator_max_bounce"
        assert prop.default == 7

    def test_the_renderers_own_name_survives_as_help(self):
        """group_props rewrites labels to drop what the enclosing groups already say, so
        the tooltip is the only place a descriptor's name lives on -- and it is the only
        documentation there is, since a descriptor carries none."""
        definition = render_defs._def_from_delegate("HdCyclesPlugin", "Cycles", self._settings())
        prop = next(p for p in definition.props if p.attr_name == "cycles:integrator:use_denoise")

        assert prop.help == "Use Denoiser"

    def test_an_unmappable_type_is_skipped_not_guessed(self):
        settings = self._settings() + [_Setting("cycles:odd", "Odd", "MATRIX", None)]
        definition = render_defs._def_from_delegate("HdCyclesPlugin", "Cycles", settings)

        assert "cycles:odd" not in {p.attr_name for p in definition.props}

    def test_unnamespaced_settings_are_refused(self):
        """Storm's are engine-side viewport toggles, not scene description. Authoring
        them unnamespaced would collide with UsdRender's own attributes."""
        settings = [
            _Setting("enableTinyPrimCulling", "Tiny Prim Culling", "FLAG", False),
            _Setting("maxLights", "Max Lights", "INT", 16),
        ]

        assert render_defs._def_from_delegate("HdStormRendererPlugin", "Storm", settings) is None

    def test_a_delegate_with_nothing_to_say_is_not_a_def(self):
        assert render_defs._def_from_delegate("HdFooPlugin", "Foo", []) is None

    @requires_delegate
    def test_a_real_delegate_is_described(self):
        for definition in _delegate_defs():
            assert definition.namespace
            assert definition.props
            assert not definition.schema_name

    @requires_delegate
    def test_every_real_prop_builds_a_param(self):
        definition = _delegate_defs()[0]
        unmapped = [p for p in definition.props if render_defs.param_for_setting(p) is None]

        assert not unmapped, f"unmapped types: {sorted({p.sdf_type for p in unmapped})}"

    @requires_delegate
    def test_a_delegate_never_duplicates_a_schema(self):
        """Loading a delegate is the expensive part, so a renderer a schema already
        described is skipped by name -- and dropped on its namespace when the two
        spellings of that name did not match."""
        namespaces = [d.namespace for d in render_defs.iter_render_settings_defs()]

        assert len(namespaces) == len(set(namespaces))


class TestDelegateEnums:
    """A Hydra render setting descriptor has no field for allowed values, so an enum
    arrives as a bare int and its names are lost. The table restores them, which is the
    difference between a menu of denoiser names and a slider showing 4."""

    def _setting(self, key, default):
        return _Setting(key, "Denoiser Type", "INT", default)

    def test_a_known_enum_becomes_options_and_values(self):
        settings = [self._setting("cycles:integrator:denoiser_type", 4)]
        definition = render_defs._def_from_delegate("HdCyclesPlugin", "Cycles", settings)
        prop = definition.props[0]

        assert prop.options == ("none", "optix", "openimagedenoise")
        assert prop.option_values == (0, 2, 4)

    def test_the_numeric_default_becomes_its_label(self):
        """The param is a Menu of names, so its default has to be one of them."""
        settings = [self._setting("cycles:integrator:denoiser_type", 4)]
        definition = render_defs._def_from_delegate("HdCyclesPlugin", "Cycles", settings)

        assert definition.props[0].default == "openimagedenoise"

    def test_a_label_authors_its_number(self):
        settings = [self._setting("cycles:integrator:denoiser_type", 4)]
        prop = render_defs._def_from_delegate("HdCyclesPlugin", "Cycles", settings).props[0]

        assert render_defs.authored_value(prop, "none") == 0
        assert render_defs.authored_value(prop, "optix") == 2
        assert render_defs.authored_value(prop, "openimagedenoise") == 4

    def test_pairing_is_explicit_not_positional(self):
        """Cycles registers sampling_pattern's blue_noise_round (4) before
        blue_noise_first (3), so anything index-based writes the wrong pattern."""
        settings = [_Setting("cycles:integrator:sampling_pattern", "Sampling Pattern", "INT", 1)]
        prop = render_defs._def_from_delegate("HdCyclesPlugin", "Cycles", settings).props[0]

        assert render_defs.authored_value(prop, "blue_noise_round") == 4
        assert render_defs.authored_value(prop, "blue_noise_first") == 3
        assert prop.options.index("blue_noise_round") < prop.options.index("blue_noise_first")

    def test_an_unknown_label_authors_nothing(self):
        """Rather than writing a number the renderer would reject."""
        settings = [self._setting("cycles:integrator:denoiser_type", 4)]
        prop = render_defs._def_from_delegate("HdCyclesPlugin", "Cycles", settings).props[0]

        assert render_defs.authored_value(prop, "not-a-denoiser") is None

    def test_an_ordinary_setting_is_passed_straight_through(self):
        """Only an enum needs translating; everything else must be untouched."""
        settings = [_Setting("cycles:samples", "Sample Count", "INT", 1024)]
        prop = render_defs._def_from_delegate("HdCyclesPlugin", "Cycles", settings).props[0]

        assert prop.options == ()
        assert prop.option_values == ()
        assert render_defs.authored_value(prop, 77) == 77

    def test_a_known_enum_builds_a_menu(self):
        settings = [self._setting("cycles:integrator:denoiser_type", 4)]
        prop = render_defs._def_from_delegate("HdCyclesPlugin", "Cycles", settings).props[0]
        param = render_defs.param_for_setting(prop)

        assert type(param).__name__ == "Menu"
        assert list(param.menu_items) == ["none", "optix", "openimagedenoise"]

    def test_every_table_entry_is_self_consistent(self):
        """Transcribed by hand from the renderer's headers, so worth pinning."""
        for attr, pairs in render_defs._DELEGATE_ENUMS.items():
            labels = [label for label, _v in pairs]
            values = [value for _l, value in pairs]

            assert attr.count(":") >= 1, f"{attr} is not namespaced"
            assert len(set(labels)) == len(labels), f"{attr} repeats a label"
            assert len(set(values)) == len(values), f"{attr} repeats a value"
            assert all(isinstance(v, int) for v in values), f"{attr} has a non-int value"

    @requires_delegate
    def test_the_table_matches_what_the_delegate_declares(self):
        """An entry for a setting this renderer does not have is dead weight, and a
        default that is not one of the options would leave the menu on the wrong item."""
        for definition in _delegate_defs():
            for prop in definition.props:
                if prop.attr_name in render_defs._DELEGATE_ENUMS:
                    assert prop.default in prop.options, prop.attr_name
                    assert len(prop.options) == len(prop.option_values)


class TestNameNormalisation:
    """A delegate's display name and its schema's label are written by different people,
    and only match once the punctuation and the case are gone."""

    def test_a_delegate_name_matches_its_schema_label(self):
        assert render_defs._normalised("V-Ray") == render_defs._normalised("VRay")

    def test_different_renderers_still_differ(self):
        assert render_defs._normalised("Cycles") != render_defs._normalised("Storm")


class TestHostVariableDefaults:
    """V-Ray's schema is generated for Solaris, so its string defaults arrive full of
    Houdini expansions. Nothing here expands them, and a path default of
    "${HIP}/render/${HIPNAME}.<frame04>.exr" would be written out literally."""

    def test_houdini_expansions_are_dropped(self):
        for value in (
            "${HIP}/${HIPNAME}.vrscene",
            "${HIP}/render/${HIPNAME}.<frame04>.exr",
            "<TEMP>/",
            "$HIP/out.exr",
        ):
            assert render_defs._sanitize_default(value) == ""

    def test_ordinary_defaults_are_untouched(self):
        for value in ("beauty.exr", "", "C:/renders/out.exr", 2.0, False, None):
            assert render_defs._sanitize_default(value) == value

    @requires_renderer
    def test_no_installed_renderer_leaks_a_host_variable(self):
        for definition in _schema_defs():
            leaked = [
                p.attr_name for p in definition.props
                if isinstance(p.default, str) and render_defs._HOST_VARIABLE.search(p.default)
            ]
            assert not leaked, f"unresolvable defaults: {leaked[:5]}"


class TestDiscoveryIsCached:
    """Building a definition is not cheap -- V-Ray's is 258 attributes, each needing a
    spec, a fallback and its docs -- and it is asked for on every cook."""

    def test_repeated_reads_return_the_same_objects(self):
        first = list(render_defs.iter_render_settings_defs())
        second = list(render_defs.iter_render_settings_defs())

        assert len(first) == len(second)
        assert all(a is b for a, b in zip(first, second)), "should be cached, not rebuilt"

    def test_clear_cache_forces_a_rebuild(self):
        first = list(render_defs.iter_render_settings_defs())
        render_defs.clear_cache()
        second = list(render_defs.iter_render_settings_defs())

        assert len(first) == len(second)
        if first:
            assert first[0] is not second[0]
            assert first[0].key == second[0].key


class TestLabels:
    def test_schema_suffixes_are_stripped(self):
        assert render_defs._label_from_schema_name("VRayRendererSettingsAPI") == "VRay"
        assert render_defs._label_from_schema_name("FooRenderSettingsAPI") == "Foo"
        assert render_defs._label_from_schema_name("BarSettingsAPI") == "Bar"

    def test_unrecognised_name_is_left_alone(self):
        assert render_defs._label_from_schema_name("Something") == "Something"


class TestNamespaceDetection:
    def test_majority_wins_over_a_stray_unnamespaced_attribute(self):
        names = ["vray:a", "vray:b", "vray:c", "unrelated"]
        assert render_defs._namespace_of(names) == "vray"

    def test_no_namespace_at_all(self):
        assert render_defs._namespace_of(["plain", "names"]) == ""
