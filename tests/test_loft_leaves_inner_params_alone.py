"""Lofting a param onto a group must not touch the other chunks inside it.

Reported: loft a param up through two nested groups and editing it from the top gets slow,
with "Expression cycle at a: '@AddMesh.width@'" in the log. Adding the lofted param to the
group re-synced the group's params, and that walk went on into the chunks nested inside
it. Their params were re-bound to attributes on the group's prim, and each chunk's own
sync then connected its attributes to themselves -- a self-reference every change had to
break.
"""
from __future__ import annotations

import logging
import weakref

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
def nested(qapp):
    """Outer group > inner group > Mesh -> MathOps (a = @AddMesh.width@)."""
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
        prev = scene._current_context
        scene._current_context = weakref.ref(context) if context is not None else None
        try:
            for c in cs:
                scene.add_chunk(c)
        finally:
            scene._current_context = prev
        return cs[0]

    outer = mk("chunkgroup", name="Outer")
    inner_group = mk("chunkgroup", context=outer, name="Inner")
    mesh = mk("mesh", context=inner_group, name="AddMesh")
    ops = mk("math_ops", context=inner_group, name="MathOps")
    settle()
    ops.get_param("a").value = "@AddMesh.width@"
    settle()
    return dict(stage=stage, outer=outer, inner_group=inner_group, mesh=mesh, ops=ops,
                settle=settle)


def _self_connections(stage, chunk):
    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(chunk)))
    return [a.GetName() for a in prim.GetAttributes()
            if any(str(c) == str(a.GetPath()) for c in a.GetConnections())]


def _loft_twice(nested):
    """Loft AddMesh.width onto Inner, then Inner's copy onto Outer."""
    mesh, inner_group, outer = nested["mesh"], nested["inner_group"], nested["outer"]
    chunk_stage.loft_param_to_context(mesh.get_param("width"), inner_group)
    nested["settle"]()
    inner_lofted = next(p for p in inner_group.get_my_params() if getattr(p, "_lofted", False))
    chunk_stage.loft_param_to_context(inner_lofted, outer)
    nested["settle"]()
    return next(p for p in outer.get_my_params() if getattr(p, "_lofted", False))


def test_inner_chunks_keep_their_own_attributes(nested):
    _loft_twice(nested)
    for chunk in (nested["mesh"], nested["ops"]):
        own = get_chunk_prim_path(chunk) + "."
        for param in chunk_graph.iter_connectable_params(chunk):
            path = getattr(param, "_param_attr_path", None)
            assert path is None or path.startswith(own), f"{chunk.name}.{param.name} -> {path}"


def test_no_attribute_is_connected_to_itself(nested):
    top = _loft_twice(nested)
    top.value = 3.0
    nested["settle"]()
    for chunk in (nested["mesh"], nested["ops"]):
        assert _self_connections(nested["stage"], chunk) == []


def test_editing_the_top_param_reports_no_cycle(nested, caplog):
    top = _loft_twice(nested)
    with caplog.at_level(logging.WARNING, logger="usd_chunks"):
        for value in (3.0, 4.0):
            top.value = value
            nested["settle"]()
    assert not [r for r in caplog.records if "cycle" in r.getMessage()]
    assert nested["mesh"].get_param("width").value == 4.0
    assert nested["ops"].get_param("a").value == 4.0
