"""Override Chunk Prim: change a chunk from a weaker layer through a chunk of your own.

The workflow: a shot sublayers another department's network (layout here). Rather than
editing layout's chunk directly, which would write an `over` straight into the edit target,
an Override Chunk Prim names the chunk and takes on its params. Those changed are written
onto the chunk prim in its own layer, so:

- the target picks up the values and re-cooks, and the chunks below it follow;
- layout's file and the edit target hold nothing of the edit;
- setting a param back or deleting the chunk puts the target back;
- two overrides on one target are two layers, the downstream one stronger;
- the override is saved like any chunk, and reopening applies it again.
"""
from __future__ import annotations

import pytest
from pxr import Sdf, Usd

from graph_fixtures import _Scene, make_network
from usd_chunks import chunk_cook, chunk_registry
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_stage import (
    ChunkStageSync,
    export_chunks,
    get_chunk_prim_path,
    initialize,
    layer_label,
    param_held_by,
    sort_chunk_layers,
)

_TX = "inputs:translate_grp:tx"


def _open(path: str):
    stage = Usd.Stage.Open(path)
    stage.SetEditTarget(stage.GetSessionLayer())
    scene = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    initialize(stage, scene)
    return stage, scene


def _override(group, target, **values):
    """An Override Chunk Prim on target, with its params set: tx=5.0 sets translate_grp.tx."""
    module = chunk_registry.ChunkRegistry().get_chunk_module("override_chunk_prim")
    node = (module.create() if module.loaded else module.load())[0]
    group.add_param(node)
    node.get_param("chunk_path").value = get_chunk_prim_path(target)
    for name, value in values.items():
        node.get_param(f"translate_grp.{name}").value = value
    return node


def _by_name(scene, name):
    return next(c for c in scene.get_chunks() if c.name == name)


@pytest.fixture
def shot(tmp_path):
    """shot.usda sublayering layout.usda, a Transform -> Transform1 network."""
    layout_path, _ = make_network(tmp_path / "layout.usda")
    shot_path = str(tmp_path / "shot.usda")
    shot_layer = Sdf.Layer.CreateNew(shot_path)
    shot_layer.subLayerPaths[:] = ["./layout.usda"]
    shot_layer.Save()
    stage, scene = _open(shot_path)
    try:
        yield stage, scene, Sdf.Layer.Find(layout_path), shot_path
    finally:
        for c in list(scene.get_chunks()):
            c.request_delete()


@pytest.fixture
def cooks(monkeypatch):
    names = []
    real = chunk_cook.cook

    def counting(chunk, *args, **kwargs):
        names.append(chunk.name)
        return real(chunk, *args, **kwargs)

    monkeypatch.setattr(chunk_cook, "cook", counting)
    return names


class TestTheTargetFollows:
    def test_the_target_takes_the_value_and_recooks(self, shot, cooks):
        _, scene, _, _ = shot
        target = _by_name(scene, "Transform")
        _override(target.get_context(), target, tx=5.0)
        assert target.get_param("translate_grp.tx").value == 5.0
        assert "Transform" in cooks and "Transform1" in cooks

    def test_nothing_lands_in_layout_or_the_edit_target(self, shot):
        stage, scene, layout, _ = shot
        target = _by_name(scene, "Transform")
        _override(target.get_context(), target, tx=5.0)
        path = f"{get_chunk_prim_path(target)}.{_TX}"
        assert stage.GetAttributeAtPath(path).Get() == 5.0
        assert stage.GetEditTarget().GetLayer().GetAttributeAtPath(path) is None
        assert stage.GetRootLayer().GetAttributeAtPath(path) is None
        assert not layout.dirty

    def test_a_new_edit_replaces_the_old(self, shot):
        _, scene, _, _ = shot
        target = _by_name(scene, "Transform")
        node = _override(target.get_context(), target, tx=5.0)
        node.get_param("translate_grp.tx").value = 7.0
        assert target.get_param("translate_grp.tx").value == 7.0


class TestTakingItBack:
    def test_setting_it_back_restores_the_target(self, shot):
        _, scene, _, _ = shot
        target = _by_name(scene, "Transform")
        node = _override(target.get_context(), target, tx=5.0)
        node.get_param("translate_grp.tx").value = 0.0
        assert target.get_param("translate_grp.tx").value == 0.0

    def test_deleting_it_restores_the_target(self, shot, cooks):
        _, scene, _, _ = shot
        target = _by_name(scene, "Transform")
        node = _override(target.get_context(), target, tx=5.0)
        cooks.clear()
        node.request_delete()
        assert target.get_param("translate_grp.tx").value == 0.0
        assert "Transform" in cooks

    def test_pointing_it_elsewhere_starts_over(self, shot):
        """The params are rebuilt from the new target's values, so the old target gets its
        own back and the new one is untouched until something changes."""
        _, scene, _, _ = shot
        first, second = _by_name(scene, "Transform"), _by_name(scene, "Transform1")
        node = _override(first.get_context(), first, tx=5.0)
        node.get_param("chunk_path").value = get_chunk_prim_path(second)
        assert first.get_param("translate_grp.tx").value == 0.0
        assert node.get_param("translate_grp.tx").value == 0.0
        node.get_param("translate_grp.tx").value = 6.0
        assert second.get_param("translate_grp.tx").value == 6.0


class TestTwoOverrides:
    def test_the_downstream_one_wins_and_each_comes_off_alone(self, shot):
        _, scene, _, _ = shot
        target = _by_name(scene, "Transform")
        group = target.get_context()
        weak = _override(group, target, tx=1.0, ty=4.0)
        strong = _override(group, target, tx=2.0)
        strong.add_dependency(weak)
        # What a View's recook does after a wire; with no View the scene does it itself.
        sort_chunk_layers(chunk_registry.ChunkLayerManager(), scene.get_chunks())
        tx, ty = target.get_param("translate_grp.tx"), target.get_param("translate_grp.ty")
        assert (tx.value, ty.value) == (2.0, 4.0)
        strong.request_delete()
        assert (tx.value, ty.value) == (1.0, 4.0)


class TestReopening:
    def test_a_saved_override_applies_again(self, shot):
        _, scene, _, shot_path = shot
        target = _by_name(scene, "Transform")
        _override(target.get_context(), target, tx=5.0)
        export_chunks(scene, shot_path)
        # Torn down without touching the stage: deleting the chunks the usual way would
        # take the override back out of the root layer the reopen is about to read.
        with ChunkStageSync().discarding_stage():
            for c in list(scene.get_chunks()):
                c.request_delete()
        _, again = _open(shot_path)
        try:
            assert _by_name(again, "Transform").get_param("translate_grp.tx").value == 5.0
        finally:
            for c in list(again.get_chunks()):
                c.request_delete()


class TestTheTargetFollowsItsPrim:
    """The target's params are what its prim composes: whatever authors it, they follow."""

    def test_disabling_it_restores_the_target_and_enabling_reapplies(self, shot):
        _, scene, _, _ = shot
        target = _by_name(scene, "Transform")
        node = _override(target.get_context(), target, tx=5.0)
        tx = target.get_param("translate_grp.tx")
        node.set_state(ChunkFlags.State.DISABLED)
        assert tx.value == 0.0
        node.unset_state(ChunkFlags.State.DISABLED)
        assert tx.value == 5.0

    def test_an_attribute_set_in_a_stronger_layer_reaches_the_chunk(self, shot, cooks):
        """No override chunk at all: an `over` authored by hand, onto an input the chunk has
        never written, is taken up and cooked -- and taken back when the layer drops it."""
        stage, scene, _, _ = shot
        target = _by_name(scene, "Transform")
        stronger = Sdf.Layer.CreateAnonymous("stronger")
        stage.GetRootLayer().subLayerPaths.insert(0, stronger.identifier)
        prim = stage.GetPrimAtPath(get_chunk_prim_path(target))
        assert not prim.GetAttribute(_TX)
        cooks.clear()
        with Usd.EditContext(stage, stronger):
            prim.CreateAttribute(_TX, Sdf.ValueTypeNames.Double).Set(3.0)
        assert target.get_param("translate_grp.tx").value == 3.0
        assert "Transform" in cooks and "Transform1" in cooks
        stronger.Clear()
        assert target.get_param("translate_grp.tx").value == 0.0


class TestEditingAnOverriddenParam:
    """The target's own edit of a param an override holds: where it lands, and what says so.

    With the root layer as the edit target, as a shot opened in usdview has it: the target's
    edit goes there, under the override's layer.
    """

    def test_the_edit_is_kept_beneath_the_override_and_shows_once_it_goes(self, shot):
        stage, scene, _, _ = shot
        stage.SetEditTarget(stage.GetRootLayer())
        target = _by_name(scene, "Transform")
        node = _override(target.get_context(), target, tx=5.0)
        tx = target.get_param("translate_grp.tx")
        tx.value = 2.0
        own = chunk_registry.ChunkLayerManager().get_layer(node)
        path = f"{get_chunk_prim_path(target)}.{_TX}"
        assert own.GetAttributeAtPath(path).default == 5.0
        assert stage.GetRootLayer().GetAttributeAtPath(path).default == 2.0
        # The override is stronger: the target shows and cooks with its value.
        assert tx.value == 5.0
        node.request_delete()
        assert tx.value == 2.0

    def test_the_param_says_which_override_holds_it(self, shot):
        stage, scene, _, _ = shot
        stage.SetEditTarget(stage.GetRootLayer())
        target = _by_name(scene, "Transform")
        node = _override(target.get_context(), target, tx=5.0)
        tx, ty = target.get_param("translate_grp.tx"), target.get_param("translate_grp.ty")
        assert param_held_by(tx) == chunk_registry.ChunkLayerManager().get_layer(node)
        assert layer_label(param_held_by(tx)) == node.name
        assert param_held_by(ty) is None
        assert param_held_by(node.get_param("translate_grp.tx")) is None
