"""Undo and redo of Loft up / Unloft: the group's lofted params follow USD back and forth."""
from __future__ import annotations

import contextlib

import pytest
from PySide6 import QtCore
from pxr import Sdf, Usd

from usd_chunks import chunk_registry, chunk_stage
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import initialize as _chunk_usd_init
from usd_chunks.frontends.qt.factory import ChunkWidgetDocker


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


@pytest.fixture
def env(qapp):
    stage = Usd.Stage.CreateInMemory()

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)
    docker = ChunkWidgetDocker()
    docker.resize(700, 700)
    docker.show()
    qapp.processEvents()

    def settle():
        for _ in range(25):
            qapp.processEvents()
            QtCore.QThread.msleep(10)
        qapp.processEvents()

    def mk(key, context=None, name=None):
        mod = reg.get_chunk_module(key)
        cs = mod.create() if mod.loaded else mod.load()
        if name:
            cs[0].name = name
        with scene.creating_in(context) if context is not None else contextlib.nullcontext():
            for c in cs:
                scene.add_chunk(c)
        return cs[0]

    return dict(stage=stage, mk=mk, settle=settle)


def _lofted(group):
    return [p for p in group.get_my_params() if getattr(p, "_lofted", False)]


class _Specs:
    """The specs of some properties across the stage's layers, put back on request.

    Put back the way an undo does it: each spec removed and re-made inside one change
    block, which USD reports as a resync. Re-importing layer text would be reported as
    info-only, which a real undo never is.
    """

    def __init__(self, stage, paths):
        self.stage = stage
        self.paths = [Sdf.Path(p) for p in paths]
        self.saved = []  # (layer, path, backup layer or None)
        for layer in stage.GetUsedLayers():
            for path in self.paths:
                backup = None
                if layer.GetPropertyAtPath(path):
                    backup = Sdf.Layer.CreateAnonymous()
                    Sdf.CreatePrimInLayer(backup, path.GetPrimPath())
                    Sdf.CopySpec(layer, path, backup, path)
                self.saved.append((layer, path, backup))

    def restore(self):
        with Sdf.ChangeBlock():
            for layer, path, backup in self.saved:
                prim_spec = layer.GetPrimAtPath(path.GetPrimPath())
                if prim_spec and path.name in prim_spec.properties:
                    del prim_spec.properties[path.name]
                if backup is not None:
                    Sdf.CreatePrimInLayer(layer, path.GetPrimPath())
                    Sdf.CopySpec(backup, path, layer, path)


def _lofted_in_group(env):
    """A group with a Transform in it, and tx about to be lofted: (group, tx, its attr paths)."""
    group = env["mk"]("chunkgroup", name="Group")
    inner = env["mk"]("transform", context=group)
    env["settle"]()
    tx = inner.get_param("tx")
    tx.value = 2.0  # so the inner attribute exists before the loft, as it would after any edit
    env["settle"]()
    return group, tx


def _loft(env, tx, group):
    """Loft tx onto group, and return the specs from before the loft and after it."""
    group_attr = f"{group.prim_path}.inputs:Transform_tx"
    paths = [tx._param_attr_path, group_attr]
    before = _Specs(env["stage"], paths)
    chunk_stage.loft_param_to_context(tx, group)
    env["settle"]()
    assert [p._param_attr_path for p in _lofted(group)] == [group_attr]
    return before, _Specs(env["stage"], paths)


def test_undoing_a_loft_removes_the_lofted_param(env):
    group, tx = _lofted_in_group(env)
    before, _ = _loft(env, tx, group)

    before.restore()  # undo
    env["settle"]()

    assert _lofted(group) == []
    assert tx.value == 2.0


def test_redoing_a_loft_brings_the_lofted_param_back_working(env):
    group, tx = _lofted_in_group(env)
    before, after = _loft(env, tx, group)
    before.restore()  # undo
    env["settle"]()

    after.restore()  # redo
    env["settle"]()

    lofted = _lofted(group)
    assert [p.name for p in lofted] == ["Transform_tx"]
    lofted[0].value = 5.0
    env["settle"]()
    assert tx.value == 5.0


def test_undoing_an_unloft_brings_the_lofted_param_back(env):
    group, tx = _lofted_in_group(env)
    _, after = _loft(env, tx, group)
    chunk_stage.unloft_param(_lofted(group)[0])
    env["settle"]()
    assert _lofted(group) == []

    after.restore()  # undo the unloft
    env["settle"]()

    assert [p.name for p in _lofted(group)] == ["Transform_tx"]


def test_an_attribute_put_back_without_its_type_brings_back_no_param(env):
    """usd-undo's undo of Unloft: the group attribute returns with no type and no value."""
    stage = env["stage"]
    group, tx = _lofted_in_group(env)
    _, after = _loft(env, tx, group)
    chunk_stage.unloft_param(_lofted(group)[0])
    env["settle"]()

    with Sdf.ChangeBlock():  # one change, as the undo is
        after.restore()  # the connection and the attribute come back...
        for layer in stage.GetUsedLayers():  # ...but the attribute as an empty shell
            spec = layer.GetPropertyAtPath(Sdf.Path(f"{group.prim_path}.inputs:Transform_tx"))
            if spec:
                spec.ClearDefaultValue()
                spec.ClearInfo("typeName")
    env["settle"]()

    assert _lofted(group) == []
    tx.value = 4.0  # the inner param is still an ordinary param
    env["settle"]()
    assert tx.value == 4.0


def test_lofting_and_unlofting_make_no_extra_params(env):
    group, tx = _lofted_in_group(env)
    _loft(env, tx, group)
    assert len(_lofted(group)) == 1

    chunk_stage.unloft_param(_lofted(group)[0])
    env["settle"]()
    chunk_stage.loft_param_to_context(tx, group)
    env["settle"]()

    assert len(_lofted(group)) == 1
