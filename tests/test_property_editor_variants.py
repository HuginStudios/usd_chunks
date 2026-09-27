"""Property Editor authors edits inside the variant a Prim path selects.

show_attribute binds each generated param straight to a REAL prim's attribute -- not to a
chunk-prim storage attr -- so ChunkUsdAttributeSupport.data_changed's usual "wherever the
spec already lives" resolution (get_attr_home_layer) can't help: a variant's content is
keyed under a different internal Sdf path, invisible to a plain layer-stack walk, so an
edit fell through to the session layer and applied to every variant at once instead of
just the selected one. The fix records the variant selections active when the param was
built and re-enters them (chunk_utils.author_in_variant_selections) on every later edit.
"""
from __future__ import annotations

import importlib
import pathlib

import pytest
from pxr import Sdf, Usd, UsdGeom

from usd_chunks import chunk, chunk_registry, chunk_utils
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import initialize as _chunk_usd_init
from usd_chunks.chunk_stage import save_scene

property_editor = importlib.import_module("chunks.usd.property_editor")

_ATTR = "weight"  # a plain custom Float attr -- usd_type_map has no entry for "double",
# which rules out a schema's built-in scalar attrs (e.g. UsdGeomSphere.radius) for this test.


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []

    def should_install_package(self, package):
        return False


def _asset_with_colours(stage):
    """/asset with a "color" variant set (red, blue); /asset/geo's `weight` differs per variant."""
    asset = UsdGeom.Xform.Define(stage, "/asset").GetPrim()
    vset = asset.GetVariantSets().AddVariantSet("color")
    for name, weight in (("red", 1.0), ("blue", 2.0)):
        vset.AddVariant(name)
        vset.SetVariantSelection(name)
        with vset.GetVariantEditContext():
            geo = UsdGeom.Xform.Define(stage, "/asset/geo").GetPrim()
            geo.CreateAttribute(_ATTR, Sdf.ValueTypeNames.Float).Set(weight)
    vset.SetVariantSelection("blue")
    return asset


@pytest.fixture
def scene(qapp):
    stage = Usd.Stage.CreateInMemory()
    _asset_with_colours(stage)
    sc = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(sc, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, sc)
    return sc


def test_editing_a_variant_scoped_prims_attribute_lands_inside_that_variant(scene):
    stage = scene.get_stage()
    node = property_editor.main()
    scene.add_chunk(node)

    # Triggers build() itself: "prim" is wired with send_on_create=True as build's own
    # callback sender, so setting it fires the rebuild -- calling build() again here as
    # well would double-build and leave a stale connection behind from the first pass.
    node.get_param("prim").value = "/asset{color=red}/geo"

    dyn_grp = node.get_param("dyn_grp", chunk.is_group)
    geo_grp = next(g for g in dyn_grp.get_my_params() if g.name == "geo")
    weight_param = geo_grp.get_param(_ATTR)
    assert weight_param is not None
    assert weight_param.value == pytest.approx(1.0), "should read the red variant's weight, not blue's"

    weight_param.value = 9.0

    vset = stage.GetPrimAtPath("/asset").GetVariantSets().GetVariantSet("color")
    weight_attr = stage.GetPrimAtPath("/asset/geo").GetAttribute(_ATTR)

    # The active composed selection is back to "blue" (author_in_variant_selections
    # restores it on exit, same as author_in_variant_context always has) -- and blue's
    # own weight must be untouched by an edit made while "red" was the target.
    assert weight_attr.Get() == pytest.approx(2.0), (
        "editing the red-variant param must not leak the new value into blue"
    )

    vset.SetVariantSelection("red")
    assert weight_attr.Get() == pytest.approx(9.0), "the edit must land inside red"

    vset.SetVariantSelection("blue")
    assert weight_attr.Get() == pytest.approx(2.0), "blue is still untouched"


def test_a_plain_non_variant_prim_is_unaffected(scene):
    """The ordinary case -- no variant markers in the path -- keeps working exactly as
    before: no var_selections recorded, and the write goes through get_attr_home_layer."""
    stage = scene.get_stage()
    loose = UsdGeom.Xform.Define(stage, "/loose").GetPrim()
    loose.CreateAttribute(_ATTR, Sdf.ValueTypeNames.Float).Set(3.0)

    node = property_editor.main()
    scene.add_chunk(node)
    node.get_param("prim").value = "/loose"

    dyn_grp = node.get_param("dyn_grp", chunk.is_group)
    loose_grp = next(g for g in dyn_grp.get_my_params() if g.name == "loose")
    weight_param = loose_grp.get_param(_ATTR)
    assert weight_param.value == pytest.approx(3.0)

    weight_param.value = 7.0
    assert stage.GetPrimAtPath("/loose").GetAttribute(_ATTR).Get() == pytest.approx(7.0)


def test_a_prim_only_in_one_variant_takes_the_edit_in_the_chunks_layer(scene):
    """A prim that exists only inside red is not on the stage while blue is selected, which
    it is again as soon as the build restores the selection. Every write path looked the
    attribute up before re-entering red, found nothing, and dropped the edit -- a Light at
    /asset{look=b}/key reset to intensity 1 on every edit. The edit also has to land in the
    chunk's own layer, not the stage's edit target: a chunk's edits always go there."""
    stage = scene.get_stage()
    vset = stage.GetPrimAtPath("/asset").GetVariantSets().GetVariantSet("color")
    vset.SetVariantSelection("red")
    with vset.GetVariantEditContext():
        only = UsdGeom.Xform.Define(stage, "/asset/only_red").GetPrim()
        only.CreateAttribute(_ATTR, Sdf.ValueTypeNames.Float).Set(5.0)
    vset.SetVariantSelection("blue")

    mod = chunk_registry.ChunkRegistry().get_chunk_module("property_editor")
    node = (mod.create() if mod.loaded else mod.load())[0]
    scene.add_chunk(node)
    node.get_param("prim").value = "/asset{color=red}/only_red"

    dyn_grp = node.get_param("dyn_grp", chunk.is_group)
    only_grp = next(g for g in dyn_grp.get_my_params() if g.name == "only_red")
    only_grp.get_param(_ATTR).value = 8.0

    assert not stage.GetPrimAtPath("/asset/only_red").IsValid(), "blue has no such prim"
    vset.SetVariantSelection("red")
    assert stage.GetPrimAtPath("/asset/only_red").GetAttribute(_ATTR).Get() == pytest.approx(8.0)

    spec_path = "/asset{color=red}only_red." + _ATTR
    root_spec = stage.GetRootLayer().GetAttributeAtPath(spec_path)
    assert root_spec.default == pytest.approx(5.0), "the stage's edit target is left alone"
    chunk_spec = node.get_edit_context().GetAttributeAtPath(spec_path)
    assert chunk_spec and chunk_spec.default == pytest.approx(8.0), "the edit is in the chunk's layer"


def test_the_variant_scope_survives_a_save_and_reopen(scene, tmp_path):
    """_restore_dynamic_params (chunk_stage.py) re-promotes a reloaded param's direct
    real-attr binding into one its chunk storage attr drives -- by hand, not through
    connect_usd_attr -- so it used to skip _driven_var_selections and re-author the
    connection outside the variant on reload, even though the live edit (the test above)
    stayed correctly scoped in the same session.

    The chunk is made through the registry, so it saves as "property_editor" and reopens by
    re-running its module. Built from main() alone it saves as "runtime", which reopens
    without its attribute params -- this passed that way only while the live edit leaked
    into the root layer, which the hand-copy of /asset below then carried across. A chunk's
    edits go into its own layer, so the reopened chunk has to put 9.0 back itself."""
    stage = scene.get_stage()
    mod = chunk_registry.ChunkRegistry().get_chunk_module("property_editor")
    node = (mod.create() if mod.loaded else mod.load())[0]
    scene.add_chunk(node)
    node.get_param("prim").value = "/asset{color=red}/geo"
    node.get_param("dyn_grp", chunk.is_group)
    geo_grp = next(g for g in node.get_param("dyn_grp", chunk.is_group).get_my_params() if g.name == "geo")
    geo_grp.get_param(_ATTR).value = 9.0

    out = pathlib.Path(tmp_path) / "prop_editor_variant.usda"
    save_scene(scene, out)

    # save_scene/export_chunks only reproduces the chunk graph -- each chunk regenerates
    # its own content by re-running execute(). /asset was authored directly on the stage,
    # never through a chunk, so it is not part of that graph and would otherwise be absent
    # from `out`. Copy its spec across by hand, standing in for what a referenced/sublayered
    # published asset file would supply in a real scene.
    out_layer = Sdf.Layer.FindOrOpen(str(out))
    Sdf.CopySpec(stage.GetRootLayer(), "/asset", out_layer, "/asset")
    out_layer.Save()

    chunk_registry.ChunkLayerManager().reset()
    stage2 = Usd.Stage.Open(str(out))
    scene2 = _Scene(stage2)
    chunk_registry.ChunkRegistry().initialize(scene2, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage2)
    _chunk_usd_init(stage2, scene2)

    vset = stage2.GetPrimAtPath("/asset").GetVariantSets().GetVariantSet("color")
    weight_attr = stage2.GetPrimAtPath("/asset/geo").GetAttribute(_ATTR)

    assert weight_attr.Get() == pytest.approx(2.0), (
        "blue is the active selection after reopen; the reloaded connection must not "
        "leak red's 9.0 onto it"
    )
    vset.SetVariantSelection("red")
    assert weight_attr.Get() == pytest.approx(9.0), "red must still carry the saved edit"
    vset.SetVariantSelection("blue")
    assert weight_attr.Get() == pytest.approx(2.0), "and blue must still be untouched"


def test_re_entering_an_open_variant_scope_does_not_nest_it() -> None:
    """Usd.VariantSet.GetVariantEditContext() builds its mapping on top of whichever edit
    target is already active, so entering {color=Black} while already inside it maps
    /asset to /asset{color=Black}{color=Black}/... -- a spec path nothing composes, so the
    write silently goes nowhere and the file grows a variantSet nested inside its own
    variant.

    Not an exotic case: the Property Editor holds one scope open across show_attribute, and
    the PARAM_ADDED that add_param fires synchronously runs the whole
    _sync_chunk_inputs -> _drive_from_storage -> connect_usd_attr ->
    _author_driven_connections path, which re-enters the same recorded selections.
    """
    stage = Usd.Stage.CreateInMemory()
    asset = UsdGeom.Xform.Define(stage, "/asset").GetPrim()
    vset = asset.GetVariantSets().AddVariantSet("color")
    vset.AddVariant("Black")
    vset.SetVariantSelection("Black")
    with vset.GetVariantEditContext():
        UsdGeom.Xform.Define(stage, "/asset/geo")

    path = "/asset{color=Black}/geo"
    _, target, selections = chunk_utils.split_variant_path(path)

    with chunk_utils.author_in_variant_context(stage, path) as t:
        prim = stage.GetPrimAtPath(t)
        with chunk_utils.author_in_variant_selections(stage, selections):
            prim.CreateAttribute(_ATTR, Sdf.ValueTypeNames.Float).Set(0.5)

    text = stage.GetRootLayer().ExportToString()
    assert text.count('variantSet "color"') == 1, (
        f"the variant set must be declared once, not nested inside itself:\n{text}"
    )
    assert stage.GetAttributeAtPath(Sdf.Path(f"/asset/geo.{_ATTR}")).Get() == pytest.approx(0.5), (
        "the write must land on the prim inside the open variant, not on a nested spec "
        "path that nothing composes"
    )
    assert not chunk_utils._ACTIVE_VARIANT_SCOPES, "every entered scope must be popped on exit"


def test_the_property_editor_never_nests_a_variant_inside_itself(scene) -> None:
    """The end-to-end shape of the same bug: every layer a Property Editor edit touches
    must declare the variant set once, however many times the reactive PARAM_ADDED chain
    re-entered the recorded scope while build() still held it open."""
    stage = scene.get_stage()
    node = property_editor.main()
    scene.add_chunk(node)
    node.get_param("prim").value = "/asset{color=red}/geo"

    geo_grp = next(g for g in node.get_param("dyn_grp", chunk.is_group).get_my_params()
                   if g.name == "geo")
    geo_grp.get_param(_ATTR).value = 9.0

    for layer in stage.GetLayerStack(includeSessionLayers=True):
        text = layer.ExportToString()
        assert text.count('variantSet "color"') <= 1, (
            f"{layer.identifier} nested the variant set inside itself:\n{text}"
        )

