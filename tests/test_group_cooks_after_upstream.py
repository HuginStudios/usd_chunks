"""A chunkgroup cooks after what feeds it, in the same pass.

recook_for_view used to recurse into a group while it was still blocking and muting the
context -- before any of the context's own chunks had been revealed and cooked. So a chunk
inside the group read the stage as its upstream had left it LAST time, and found none of
the prims it acts on.
"""
from __future__ import annotations

import sys
import types

import pytest
from pxr import Usd

from usd_chunks import chunk_registry, chunk_stage
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import connect_exec, get_chunk_prim_path

_PROBE = "_group_cooks_after_upstream_probe"


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


def _make(scene, key):
    mod = chunk_registry.ChunkRegistry().get_chunk_module(key)
    chunks = mod.create() if mod.loaded else mod.load()
    for c in chunks:
        scene.add_chunk(c)
    return chunks


@pytest.fixture
def probe():
    ns = types.SimpleNamespace(seen=[])
    sys.modules[_PROBE] = ns
    yield ns
    sys.modules.pop(_PROBE, None)


def _authors(path):
    return f"stage.DefinePrim({path!r}, 'Xform')\n"


def test_a_group_sees_what_its_upstream_authored_in_this_pass(qapp, probe):
    stage = Usd.Stage.CreateInMemory()
    scene = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().reset()
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_stage.initialize(stage, scene)

    (upstream,) = _make(scene, "script_runner")
    upstream.get_param("code").value = _authors("/Probe/old")
    group = _make(scene, "chunkgroup")[0]
    with scene.creating_in(group):
        (reader,) = _make(scene, "script_runner")
    reader.get_param("code").value = (
        f"import {_PROBE} as p\n"
        "p.seen.append(stage.GetPrimAtPath('/Probe/new').IsValid())\n"
    )
    view = next(c for c in _make(scene, "view") if chunk_stage.is_view_chunk(c))
    connect_exec(stage, scene, get_chunk_prim_path(upstream), get_chunk_prim_path(group))
    connect_exec(stage, scene, get_chunk_prim_path(group), get_chunk_prim_path(view))
    qapp.processEvents()
    context = view.get_context()
    chunk_stage.recook_for_view(stage, scene, context, view)
    assert stage.GetPrimAtPath("/Probe/old").IsValid(), "precondition: the chain cooks"

    # Change what the upstream authors while nothing is listening, so only the recook
    # below can bring it about.
    for c in (upstream, reader):
        c.set_blocked(True)
    upstream.get_param("code").value = _authors("/Probe/new")
    reader.get_param("code").value = reader.get_param("code").value + "\n"
    probe.seen.clear()

    chunk_stage.recook_for_view(stage, scene, context, view)

    assert probe.seen, "the chunk inside the group should have cooked"
    assert probe.seen[-1] is True, (
        "the group cooked before its upstream did, and read the stage as it was last pass"
    )
