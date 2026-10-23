"""Unloft: remove a param a group publishes, leaving the inner param as it was."""
from __future__ import annotations

import contextlib

import pytest
from PySide6 import QtCore
from pxr import Sdf, Usd

from usd_chunks import chunk_registry, chunk_stage
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import get_chunk_prim_path, initialize as _chunk_usd_init
from usd_chunks.frontends.qt.factory import ChunkWidgetDocker
from usd_chunks.graph import engine as chunk_graph


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


def _loft(env, param, group):
    chunk_stage.loft_param_to_context(param, group)
    env["settle"]()
    return _lofted(group)[-1]


def _prim(stage, chunk):
    return stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(chunk)))


def test_unloft_removes_the_group_param_and_keeps_the_inner_value(env):
    stage = env["stage"]
    group = env["mk"]("chunkgroup", name="Group")
    inner = env["mk"]("transform", context=group)
    env["settle"]()
    tx = inner.get_param("tx")
    lofted = _loft(env, tx, group)
    attr_name = lofted._param_attr_path.rsplit(".", 1)[-1]
    lofted.value = 4.0
    env["settle"]()

    chunk_stage.unloft_param(lofted)
    env["settle"]()

    assert _lofted(group) == []
    assert not _prim(stage, group).GetAttribute(attr_name)
    inner_attr = stage.GetAttributeAtPath(Sdf.Path(tx._param_attr_path))
    assert list(inner_attr.GetConnections()) == []
    assert tx.value == 4.0
    tx.value = 6.0  # still an ordinary param
    env["settle"]()
    assert tx.value == 6.0


def test_unloft_also_removes_the_copy_lofted_further_up(env):
    outer = env["mk"]("chunkgroup", name="Outer")
    group = env["mk"]("chunkgroup", context=outer, name="Group")
    inner = env["mk"]("transform", context=group)
    env["settle"]()
    lofted = _loft(env, inner.get_param("tx"), group)
    _loft(env, lofted, outer)

    chunk_stage.unloft_param(lofted)
    env["settle"]()

    assert _lofted(group) == []
    assert _lofted(outer) == []


def test_nothing_is_left_connected_to_the_removed_attribute(env):
    stage = env["stage"]
    outside = env["mk"]("transform", name="Outside")
    group = env["mk"]("chunkgroup", context=outside.get_context(), name="Group")
    inner = env["mk"]("transform", context=group)
    env["settle"]()
    lofted = _loft(env, inner.get_param("tx"), group)
    removed = lofted._param_attr_path
    wire = _prim(stage, outside).CreateAttribute("inputs:from_group", Sdf.ValueTypeNames.Float)
    wire.SetConnections([Sdf.Path(removed)])

    chunk_stage.unloft_param(lofted)
    env["settle"]()

    dangling = [str(a.GetPath()) for p in stage.TraverseAll() for a in p.GetAttributes()
                if any(str(c) == removed for c in a.GetConnections())]
    assert dangling == []


def test_an_expression_reading_the_port_becomes_a_plain_value(env):
    stage = env["stage"]
    outside = env["mk"]("transform", name="Outside")
    group = env["mk"]("chunkgroup", context=outside.get_context(), name="Group")
    inner = env["mk"]("transform", context=group)
    env["settle"]()
    lofted = _loft(env, inner.get_param("tx"), group)
    src = next(p for p in chunk_graph.iter_ports(_prim(stage, group), group)
               if p.name == lofted.name and not p.is_input)
    dst = next(p for p in chunk_graph.iter_ports(_prim(stage, outside), outside)
               if p.name == "tx" and p.is_input)
    chunks = {get_chunk_prim_path(group): group, get_chunk_prim_path(outside): outside}
    assert chunk_graph.connect(stage, src, dst, chunks) is not None
    consumer = outside.get_param("tx")
    assert consumer.raw_value == f"@Group.{lofted.name}@"

    chunk_stage.unloft_param(lofted)
    env["settle"]()

    assert not isinstance(consumer.raw_value, str)


def test_unloft_removes_the_copy_lofted_up_to_the_tab(env):
    outer = env["mk"]("chunkgroup", name="Outer")
    env["settle"]()
    group = env["mk"]("chunkgroup", context=outer, name="Group")
    inner = env["mk"]("transform", context=group)
    env["settle"]()
    tab = outer._parent_context()  # the tab's own group, above Outer
    lofted = _loft(env, inner.get_param("tx"), group)
    lofted = _loft(env, lofted, outer)
    _loft(env, lofted, tab)

    # Unlofted from the bottom, as from the Transform's right-click menu.
    chunk_stage.unloft_param(_lofted(group)[0])
    env["settle"]()

    assert (_lofted(group), _lofted(outer), _lofted(tab)) == ([], [], [])


def test_deleting_the_inner_chunk_unlofts_its_params(env):
    stage = env["stage"]
    group = env["mk"]("chunkgroup", name="Group")
    inner = env["mk"]("transform", context=group)
    env["settle"]()
    lofted = _loft(env, inner.get_param("tx"), group)
    name = lofted._param_attr_path.rsplit(".", 1)[-1]

    inner.request_delete()
    env["settle"]()

    assert _lofted(group) == []
    assert not _prim(stage, group).GetAttribute(name)
