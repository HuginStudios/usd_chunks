"""A lofted param follows the inner param it publishes.

Reported: loft a Transform's translate X up to its chunkgroup, jump out, drag the gizmo --
and the group's param does not move. Editing the group's param DOES drive the gizmo, so
the bridge existed in one direction only: loft_param_to_context forwards the group's
param down into the inner one and nothing carries a change the other way.

The gizmo is only the way it was noticed. It writes `param.value = item` on the inner
param (chunk_gizmo.py, "writing the param is what dirties the chunk and propagates
downstream"), so this is the same gap for anything that writes an inner param -- a script,
another chunk, a reload.
"""
from __future__ import annotations

import weakref

import pytest
from PySide6 import QtCore
from pxr import Sdf, Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks import chunk_stage
from usd_chunks.plugins.chunk_qt import ChunkWidgetDocker
from usd_chunks.chunk_stage import get_chunk_prim_path, initialize as _chunk_usd_init

LOFTED = "Transform_tx"


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


@pytest.fixture
def lofted(qapp):
    """A chunkgroup publishing its inner Transform's tx."""
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

    def mk(key, context=None):
        mod = reg.get_chunk_module(key)
        cs = mod.create() if mod.loaded else mod.load()
        prev = scene._current_context
        scene._current_context = weakref.ref(context) if context is not None else None
        try:
            for c in cs:
                scene.add_chunk(c)
        finally:
            scene._current_context = prev
        return cs[0]

    group = mk("chunkgroup")
    inner = mk("transform", context=group)
    settle()
    inner.get_param("tx").value = 1.0
    settle()
    chunk_stage.loft_param_to_context(inner.get_param("tx"), group)
    settle()
    assert group.get_param(LOFTED) is not None
    return dict(stage=stage, group=group, inner=inner, settle=settle)


def test_the_group_param_follows_a_write_to_the_inner_param(lofted):
    """What a gizmo drag does: write the inner param."""
    inner, group = lofted["inner"], lofted["group"]

    inner.get_param("tx").value = 7.0
    lofted["settle"]()

    assert group.get_param(LOFTED).value == 7.0


def test_it_reaches_the_groups_usd_attribute(lofted):
    """Not just the Python param -- the interface a saved file and the CLI both read."""
    inner, group, stage = lofted["inner"], lofted["group"], lofted["stage"]

    inner.get_param("tx").value = 4.25
    lofted["settle"]()

    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(group)))
    assert prim.GetAttribute(f"inputs:{LOFTED}").Get() == pytest.approx(4.25)


def test_the_group_param_still_drives_the_inner_one(lofted):
    """Pinned: the direction that already worked must survive gaining the other one."""
    inner, group = lofted["inner"], lofted["group"]

    group.get_param(LOFTED).value = -3.0
    lofted["settle"]()

    assert inner.get_param("tx").value == -3.0


def test_a_write_settles_rather_than_ping_ponging(lofted):
    """Two params writing to each other is a loop unless something stops it.

    Counted rather than asserted by eye: one write should produce one update on the other
    side, not a cascade that happens to terminate.
    """
    inner, group = lofted["inner"], lofted["group"]
    seen = []
    group_param = group.get_param(LOFTED)
    group_param.add_callback(
        lambda *_a, **_k: seen.append(group_param.value),
        notice_alias=chunk_stage.chunk_notice.ChunkNoticeAlias.PARAM_CHANGED,
        send_on_create=False,
    )

    inner.get_param("tx").value = 2.5
    lofted["settle"]()

    assert inner.get_param("tx").value == 2.5
    assert group_param.value == 2.5
    assert len(seen) <= 2, f"one write should not cascade: {seen}"


def test_a_real_gizmo_drag_reaches_the_group(lofted):
    """End to end through the gizmo API, not just a param write that resembles one.

    apply_bindings is what a host's manipulator calls on every drag tick. The Transform's
    handle is matrix-driven through its own gizmo_changed solve hook, so this goes the
    whole way round -- matrix in, decomposed, written to the component params -- rather
    than standing in for it with a param write that merely resembles one.
    """
    from pxr import Gf

    from usd_chunks import chunk_gizmo

    inner, group = lofted["inner"], lofted["group"]

    chunk_gizmo.apply_bindings(inner, "transform", Gf.Matrix4d().SetTranslate(Gf.Vec3d(9.5, 0.0, 0.0)))
    lofted["settle"]()

    assert inner.get_param("tx").value == 9.5, "the gizmo writes the inner param"
    assert group.get_param(LOFTED).value == 9.5, "and the group's interface follows it"
