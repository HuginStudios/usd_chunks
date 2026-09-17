"""The layer manager does not follow a foreign stage's edit target.

ChunkLayerManager listens globally for StageEditTargetChanged so that chunks loaded before
the host had a stage can adopt the first one that appears. It adopted every one that
spoke: a second stage in the same process changed its edit target -- entering a variant
edit context does that -- and the manager switched to it, so the next get_or_create_layer
sublayered every chunk's layer under the foreign stage's session. The chunks' opinions
then composed over that stage; a Set Variant chunk's selection overrode the one the other
stage had just authored, and its variant edit context wrote into the wrong variant.
"""
from __future__ import annotations

from pxr import Usd, UsdGeom

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.chunk_stage import initialize as _chunk_usd_init


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


def _network(qapp):
    """A scene with one Set Variant chunk, which listens globally to stage changes."""
    stage = Usd.Stage.CreateInMemory()
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        vset = stage.DefinePrim("/thing", "Xform").GetVariantSets().AddVariantSet("color")
        for name in ("red", "blue"):
            vset.AddVariant(name)
    scene = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)
    module = chunk_registry.ChunkRegistry().get_chunk_module("set_variant")
    node = (module.create() if module.loaded else module.load())[0]
    scene.add_chunk(node)
    node.get_param("primpath_location").value = "/thing"
    node.get_param("variant_set").value = "color"
    node.get_param("variant_selection").value = "blue"
    qapp.processEvents()
    return stage, scene, node


def test_a_second_stages_edit_target_does_not_move_the_manager(qapp, tmp_path) -> None:
    stage, _scene, _node = _network(qapp)
    manager = chunk_registry.ChunkLayerManager()
    assert manager.stage is stage

    other = Usd.Stage.CreateNew(str(tmp_path / "other.usda"))
    with Usd.EditContext(other, other.GetSessionLayer()):
        pass

    assert manager.stage is stage, "the manager followed a stage it does not manage"


def test_authoring_variants_on_a_second_stage_is_not_overridden_by_chunk_layers(qapp, tmp_path) -> None:
    """The reported shape: an asset authored variant by variant on another stage, with a
    Set Variant chunk alive that selects a different variant of the same set name."""
    _network(qapp)

    other = Usd.Stage.CreateNew(str(tmp_path / "asset.usda"))
    cube = UsdGeom.Cube.Define(other, "/thing")
    colours = cube.GetPrim().GetVariantSets().AddVariantSet("color")
    for name, size in (("red", 1.0), ("green", 2.0), ("blue", 3.0)):
        colours.AddVariant(name)
        colours.SetVariantSelection(name)
        assert colours.GetVariantSelection() == name, "a chunk's selection composed over ours"
        with colours.GetVariantEditContext():
            cube.CreateSizeAttr(size)
    qapp.processEvents()

    assert other.GetSessionLayer().subLayerPaths == [], "no chunk layer was sublayered under a foreign stage"
    root = other.GetRootLayer().GetPrimAtPath("/thing")
    sizes = {v: spec.primSpec.properties["size"].default for v, spec in root.variantSets["color"].variants.items()}
    assert sizes == {"red": 1.0, "green": 2.0, "blue": 3.0}


def test_an_empty_manager_still_adopts_the_first_stage_it_hears() -> None:
    """The case the listener exists for: chunks loaded before the host had a stage."""
    manager = chunk_registry.ChunkLayerManager()
    manager.initialize(None)
    assert manager.stage is None

    first = Usd.Stage.CreateInMemory()
    with Usd.EditContext(first, first.GetSessionLayer()):
        pass

    assert manager.stage is first
