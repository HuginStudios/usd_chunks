"""A pasted connection must not retype the destination param's attribute.

Reported after duplicating a ChunkGroup holding a Transform: dragging tx afterwards raised,
over and over,

    Type mismatch for </chunks/.../Transform.inputs:tx>: expected 'string', got 'double'

_author_raw_connection wires a pasted group's children directly at the USD level, and when
the destination attribute does not exist yet it has to create one. It created every
non-exec attribute as String. A Transform whose tx had never been authored got an
inputs:tx of type String, and every later numeric write to it failed -- permanently, since
an attribute's type is fixed once it is created.

The destination's type belongs to the destination param, not to the wire arriving at it.
"""
from __future__ import annotations

import pytest
from pxr import Sdf, Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks import chunk_stage
from usd_chunks.frontends.qt.nodegraph import _author_raw_connection


class _Scene(SceneSender):
    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture
def scene(qapp):
    stage = Usd.Stage.CreateInMemory()
    s = _Scene()
    s._stage = stage
    chunk_registry.ChunkRegistry().initialize(s, "pytest")
    chunk_registry.ChunkLayerManager().reset()
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_stage.initialize(stage, s)
    return s, stage


def _mk(scene_obj, key):
    mod = chunk_registry.ChunkRegistry().get_chunk_module(key)
    cs = mod.create() if mod.loaded else mod.load()
    for c in cs:
        scene_obj.add_chunk(c)
    return cs[0]


def _wire(stage, dst_chunk, dst_suffix, src_chunk, src_suffix):
    _author_raw_connection(
        stage,
        chunk_stage.get_chunk_prim_path(dst_chunk), dst_suffix, dst_chunk,
        chunk_stage.get_chunk_prim_path(src_chunk), src_suffix,
    )


def test_a_float_param_keeps_a_float_attribute(scene, qapp):
    s, stage = scene
    src = _mk(s, "transform")
    dst = _mk(s, "transform")
    qapp.processEvents()

    dst_prim = stage.GetPrimAtPath(Sdf.Path(chunk_stage.get_chunk_prim_path(dst)))
    assert not dst_prim.GetAttribute("inputs:tx").IsValid(), (
        "precondition: tx must be unauthored, which is when the attribute gets created here"
    )

    _wire(stage, dst, "inputs:tx", src, "outputs:tx")

    attr = dst_prim.GetAttribute("inputs:tx")
    assert attr.IsValid(), "the connection should have created the attribute"
    assert attr.GetTypeName() == Sdf.ValueTypeNames.Float, (
        f"tx was created as {attr.GetTypeName()}; it is a float param, and an attribute's "
        "type cannot be changed once created, so a wrong one raises on every later write"
    )


def test_the_new_attribute_accepts_the_value_the_param_will_write(scene, qapp):
    """The type is not cosmetic: this is the write that was failing in the report."""
    s, stage = scene
    src = _mk(s, "transform")
    dst = _mk(s, "transform")
    qapp.processEvents()

    _wire(stage, dst, "inputs:tx", src, "outputs:tx")

    attr = stage.GetPrimAtPath(
        Sdf.Path(chunk_stage.get_chunk_prim_path(dst))
    ).GetAttribute("inputs:tx")
    attr.Set(4.25)          # raised: expected 'string', got 'double'
    assert attr.Get() == pytest.approx(4.25)


def test_a_genuine_string_param_is_still_a_string(scene, qapp):
    """The fix reads the destination param, so a string destination stays a string."""
    s, stage = scene
    src = _mk(s, "script_runner")
    dst = _mk(s, "duplicate_prim")
    qapp.processEvents()

    _wire(stage, dst, "inputs:dest_path", src, "outputs:result")

    attr = stage.GetPrimAtPath(
        Sdf.Path(chunk_stage.get_chunk_prim_path(dst))
    ).GetAttribute("inputs:dest_path")
    assert attr.IsValid()
    assert attr.GetTypeName() == Sdf.ValueTypeNames.String
