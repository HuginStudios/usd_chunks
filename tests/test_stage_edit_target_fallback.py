"""With no Edit Target chunk upstream, chunk prims follow the stage's edit target.

The workflow: a shot sublayers department files (light over layout), and the user picks a
department by setting the stage's edit target, the way USD picks where any other edit goes.
A chunk made then is saved in that department's file, and a slider moved on another
department's chunk is a sparse `over` there. Until the user picks a layer, chunks go wherever
the host's default edit target is: usdview's is the session layer, which these tests mirror.

Pinned here:

- a new chunk, and a `def` for the chunkgroup it sits in, land in the target sublayer, so
  that file opened on its own still has its chunks under a container;
- a later change of edit target does not drag an existing chunk along;
- views and the active-view choice follow the edit target like any other chunk edit;
- cook output (nearest_upstream_edit_layer) still falls back to the session layer.
"""
from __future__ import annotations

import pytest
from pxr import Sdf, Usd

from graph_fixtures import _Scene, make_network
from usd_chunks import chunk_registry
from usd_chunks.chunk_stage import get_chunk_prim_path, initialize, nearest_upstream_edit_layer
from usd_chunks.graph import engine

_TX = "inputs:translate_grp:tx"


def _open(path: str):
    """Open the way usdview does: Usd.Stage.Open targets the root layer, and usdview then
    points the edit target at the session layer (Usdviewq appController)."""
    stage = Usd.Stage.Open(path)
    stage.SetEditTarget(stage.GetSessionLayer())
    scene = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    initialize(stage, scene)
    return stage, scene


def _new_transform():
    module = chunk_registry.ChunkRegistry().get_chunk_module("transform")
    return (module.create() if module.loaded else module.load())[0]


def _defs(layer: Sdf.Layer) -> list[str]:
    found = []

    def visit(spec):
        if spec.typeName == "Chunk" and spec.specifier == Sdf.SpecifierDef:
            found.append(str(spec.path))
        for child in spec.nameChildren.values():
            visit(child)

    for root in layer.rootPrims.values():
        visit(root)
    return sorted(found)


@pytest.fixture
def shot(tmp_path):
    """shot.usda sublayering light.usda (empty) over layout.usda (a two-chunk network)."""
    layout_path, context = make_network(tmp_path / "layout.usda")
    light_path = str(tmp_path / "light.usda")
    Sdf.Layer.CreateNew(light_path).Save()
    shot_path = str(tmp_path / "shot.usda")
    shot_layer = Sdf.Layer.CreateNew(shot_path)
    shot_layer.subLayerPaths[:] = ["./light.usda", "./layout.usda"]
    shot_layer.Save()
    stage, scene = _open(shot_path)
    light = Sdf.Layer.Find(light_path)
    layout = Sdf.Layer.Find(layout_path)
    group = next(iter(scene.get_chunks())).get_context()
    assert str(group.prim_path) == context
    try:
        yield stage, scene, light, layout, group
    finally:
        for chunk in list(scene.get_chunks()):
            chunk.request_delete()


class TestNewChunksFollowTheStageEditTarget:
    def test_a_new_chunk_lands_in_the_target_sublayer(self, shot):
        stage, _, light, layout, group = shot
        stage.SetEditTarget(light)
        new = _new_transform()
        group.add_param(new)
        path = get_chunk_prim_path(new)
        assert path in _defs(light)
        assert stage.GetSessionLayer().GetPrimAtPath(path) is None
        assert not layout.dirty

    def test_its_chunkgroup_is_defined_there_too(self, shot):
        stage, _, light, _, group = shot
        stage.SetEditTarget(light)
        group.add_param(_new_transform())
        spec = light.GetPrimAtPath(str(group.prim_path))
        assert spec.specifier == Sdf.SpecifierDef
        assert spec.attributes["info:chunkType"].default == "chunkgroup"
        assert light.GetPrimAtPath("/chunks").specifier == Sdf.SpecifierDef

    def test_the_sublayer_opened_alone_loads_its_chunks(self, shot):
        stage, scene, light, _, group = shot
        stage.SetEditTarget(light)
        new = _new_transform()
        group.add_param(new)
        name = new.name
        light.Save()
        for chunk in list(scene.get_chunks()):
            chunk.request_delete()
        _, alone = _open(light.identifier)
        try:
            assert [c.name for c in alone.get_chunks()] == [name]
        finally:
            for chunk in list(alone.get_chunks()):
                chunk.request_delete()

    def test_until_a_target_is_chosen_it_lands_in_the_session(self, shot):
        """Opening a file must not write chunks into it."""
        stage, _, _, _, group = shot
        new = _new_transform()
        group.add_param(new)
        path = get_chunk_prim_path(new)
        assert path in _defs(stage.GetSessionLayer())
        assert stage.GetRootLayer().GetPrimAtPath(path) is None
        assert not stage.GetRootLayer().dirty

    def test_targeting_the_root_puts_it_in_the_root(self, shot):
        stage, _, _, _, group = shot
        stage.SetEditTarget(stage.GetRootLayer())
        new = _new_transform()
        group.add_param(new)
        assert get_chunk_prim_path(new) in _defs(stage.GetRootLayer())

    def test_moving_the_target_later_leaves_the_chunk_where_it_is(self, shot):
        """The chunk stays defined in light; an edit made with the target on the root is
        an `over` there, as for any other prim light.usda defines."""
        stage, _, light, _, group = shot
        stage.SetEditTarget(light)
        new = _new_transform()
        group.add_param(new)
        stage.SetEditTarget(stage.GetRootLayer())
        new.get_param("translate_grp.tx").value = 4.0
        path = get_chunk_prim_path(new)
        assert path in _defs(light)
        root_spec = stage.GetRootLayer().GetPrimAtPath(path)
        assert root_spec.specifier == Sdf.SpecifierOver
        assert stage.GetRootLayer().GetAttributeAtPath(f"{path}.{_TX}").default == 4.0
        assert stage.GetSessionLayer().GetPrimAtPath(path) is None

    def test_cook_output_still_falls_back_to_the_session_layer(self, shot):
        stage, _, light, _, group = shot
        stage.SetEditTarget(light)
        new = _new_transform()
        group.add_param(new)
        prim = stage.GetPrimAtPath(get_chunk_prim_path(new))
        assert nearest_upstream_edit_layer(stage, prim) == stage.GetSessionLayer()


class TestOverridesOnAnotherDepartmentsChunk:
    def test_a_slider_on_a_layout_chunk_is_an_over_in_light(self, shot):
        stage, scene, light, layout, _ = shot
        stage.SetEditTarget(light)
        transform = next(c for c in scene.get_chunks() if c.name == "Transform")
        transform.get_param("translate_grp.tx").value = 7.0
        path = get_chunk_prim_path(transform)
        assert light.GetAttributeAtPath(f"{path}.{_TX}").default == 7.0
        assert light.GetPrimAtPath(path).specifier == Sdf.SpecifierOver
        assert not layout.dirty


class TestViewsFollowTheEditTargetToo:
    def test_the_active_view_is_written_to_the_edit_target(self, shot):
        stage, _, light, _, group = shot
        stage.SetEditTarget(light)
        engine.write_active_slot(stage, str(group.prim_path), 2)
        attr = f"{group.prim_path}.{engine.ACTIVE_VIEW_ATTR}"
        assert light.GetAttributeAtPath(attr).default == 2
        assert stage.GetSessionLayer().GetAttributeAtPath(attr) is None
