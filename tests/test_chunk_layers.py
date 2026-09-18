"""A chunk's layer defines the ancestors of what it defines, so muting another chunk's layer
never leaves a prim sitting under an undefined parent."""

from __future__ import annotations

from pxr import Sdf, Usd

from usd_chunks import chunk_layers


def _stage_with_two_layers():
    stage = Usd.Stage.CreateInMemory()
    first, second = Sdf.Layer.CreateAnonymous("first"), Sdf.Layer.CreateAnonymous("second")
    stage.GetRootLayer().subLayerPaths[:] = [second.identifier, first.identifier]
    return stage, first, second


def test_a_later_chunk_defines_the_parents_the_first_one_created() -> None:
    stage, first, second = _stage_with_two_layers()
    with Usd.EditContext(stage, first):
        stage.DefinePrim("/World/lights/sun", "DistantLight")
    with Usd.EditContext(stage, second):
        stage.DefinePrim("/World/lights/lamp", "SphereLight")
    assert second.GetPrimAtPath("/World").specifier == Sdf.SpecifierOver, "USD's own behaviour"

    assert chunk_layers.define_ancestors(second) == 2

    stage.MuteLayer(first.identifier)
    assert stage.GetPrimAtPath("/World/lights/lamp").IsDefined()


def test_a_plain_override_stays_an_override() -> None:
    """An over with nothing defined beneath it overrides someone else's prim; making it a
    def would conjure an empty prim wherever that prim is missing."""
    stage, first, second = _stage_with_two_layers()
    with Usd.EditContext(stage, second):
        stage.OverridePrim("/Kitchen_set/Props/Cup")

    assert chunk_layers.define_ancestors(second) == 0
    assert not stage.GetPrimAtPath("/Kitchen_set").IsDefined()


def test_a_prim_defined_inside_a_variant_defines_the_ancestors_outside_it() -> None:
    stage, first, second = _stage_with_two_layers()
    with Usd.EditContext(stage, first):
        stage.DefinePrim("/World/shots")
    with Usd.EditContext(stage, second):
        shots = stage.OverridePrim("/World/shots")
        vset = shots.GetVariantSets().AddVariantSet("shot")
        vset.AddVariant("sh010")
        vset.SetVariantSelection("sh010")
        with vset.GetVariantEditContext():
            stage.DefinePrim("/World/shots/cam", "Camera")

    chunk_layers.define_ancestors(second)

    stage.MuteLayer(first.identifier)
    assert stage.GetPrimAtPath("/World/shots/cam").IsDefined()


# ---------------------------------------------------------------------------
# the flatten rescue
# ---------------------------------------------------------------------------

def test_a_healthy_layer_has_nothing_to_drop() -> None:
    """The scan runs on every Save Scene, so it must not touch a well-formed layer."""
    stage, first, _ = _stage_with_two_layers()
    with Usd.EditContext(stage, first):
        prim = stage.DefinePrim("/chunks/Light", "Chunk")
        prim.CreateAttribute("inputs:intensity", Sdf.ValueTypeNames.Float, custom=False).Set(10.0)
        prim.CreateAttribute("inputs:chunk", Sdf.ValueTypeNames.Token, custom=False).SetConnections(
            [Sdf.Path("/chunks/Other.outputs:chunk")]
        )

    assert chunk_layers.broken_property_specs(first) == []
    assert chunk_layers.drop_broken_property_specs([first]) == []
    assert first.GetAttributeAtPath("/chunks/Light.inputs:intensity").default == 10.0
