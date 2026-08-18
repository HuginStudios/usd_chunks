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
        props = [self._prop(n) for n in ("vray:cloud:upload", "vray:cloud:job", "vray:geom:x")]
        groups = render_defs.group_props(props)

        assert list(groups["cloud"]) == props[:2]
        assert [p.attr_name for p in groups["geom"]] == ["vray:geom:x"]

    def test_shared_underscore_prefix_earns_a_group(self):
        props = [self._prop(f"vray:BakeView_{n}") for n in ("fov", "mode", "dilation")]
        groups = render_defs.group_props(props)

        assert "BakeView" in groups
        assert len(groups["BakeView"]) == 3

    def test_rare_prefix_falls_back_to_general(self):
        """Two settings sharing a prefix is a coincidence, not a section."""
        props = [self._prop("vray:odd_one"), self._prop("vray:odd_two")]
        groups = render_defs.group_props(props)

        assert "odd" not in groups
        assert len(groups["General"]) == 2

    def test_general_is_last(self):
        props = [self._prop("vray:loose"), self._prop("vray:cloud:upload")]
        groups = render_defs.group_props(props)

        assert list(groups)[-1] == "General"

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
