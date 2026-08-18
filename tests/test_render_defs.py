"""Tests for render_defs: renderer settings normalised from wherever they are declared.

Every renderer authors settings the same way -- namespaced attributes on the
RenderSettings prim -- but declares them differently. V-Ray ships an applied API schema;
RenderMan registers .args files into Sdr. These cover the shared model both produce, and
the schema backend against a real renderer when one is registered.

The renderer-dependent tests skip rather than fail when no such renderer is installed:
the usd-core wheels carry no render delegates at all, and "no renderers describable" is
the correct answer there rather than an error.
"""
from __future__ import annotations

import pytest

from usd_chunks import render_defs


def _schema_defs():
    return [d for d in render_defs.iter_render_settings_defs() if d.source == "schema"]


requires_renderer = pytest.mark.skipif(
    not _schema_defs(),
    reason="no renderer ships an applied API schema for RenderSettings in this USD build",
)


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

        assert list(groups["cloud"]) == props[:2]
        assert [p.attr_name for p in groups["geom"]] == ["vray:geom:x", "vray:geom:y"]

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
