"""Re-authoring an exec wire that already exists must not touch the rest of the scene.

AMD MaterialX Library calls connect_exec(Reference -> BindMaterial) on every cook, to wire
children it may have just created. On every cook after the first, that wire is already
there. connect_exec still ran a full, unprotected reconcile_edit_targets() afterwards, and
on a scene just opened from disk that reconcile:

  * moved every chunk's bookkeeping out of the file's own root layer by DELETING the
    original specs. Sdf caches layers per process, so anything reopening or saving that
    path got the gutted layer: opening a 60-chunk scene left 7 chunk definitions in its
    root layer, and a save from that session wrote a file holding exactly those and
    nothing else.
  * re-executed each chunk it moved, and each re-execute cascades a solve over everything
    downstream of it -- once per chunk above, so quadratic: a 60-chunk load ran 566 module
    executes, and the same cascade fired again on every dive into or out of a chunkgroup.

load_chunks already reconciles with cook=False and protect_root_layer=True for exactly
these two reasons. The re-entrant call from a chunk's own execute() bypassed both.
"""
from __future__ import annotations

import sys
import types

import pytest
from pxr import Sdf, Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks import chunk_stage
from usd_chunks.chunk_stage import (
    connect_exec,
    get_chunk_prim_path,
    initialize as _chunk_usd_init,
    save_scene,
)

# Scripts run inside script_runner's exec(); they reach the test through sys.modules.
_PROBE = "_connect_exec_repeat_wire_probe"


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _scene_for(stage):
    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    s = _StageScene()
    chunk_registry.ChunkRegistry().initialize(s, "pytest")
    chunk_registry.ChunkLayerManager().reset()
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, s)
    return s


def _mk(scene, key):
    mod = chunk_registry.ChunkRegistry().get_chunk_module(key)
    cs = mod.create() if mod.loaded else mod.load()
    for c in cs:
        scene.add_chunk(c)
    return cs[0]


def _chunk_defs(layer):
    found = []

    def visit(path):
        spec = layer.GetPrimAtPath(path)
        if spec and spec.typeName == "Chunk" and spec.specifier == Sdf.SpecifierDef:
            found.append(str(path))

    layer.Traverse(Sdf.Path.absoluteRootPath, visit)
    return sorted(found)


@pytest.fixture
def probe():
    ns = types.SimpleNamespace(tail_runs=0)
    sys.modules[_PROBE] = ns
    yield ns
    sys.modules.pop(_PROBE, None)


@pytest.fixture
def saved(qapp, tmp_path, probe):
    """A -> B -> C -> Tail, plus a Rewire chunk re-authoring A -> B when it cooks."""
    stage = Usd.Stage.CreateInMemory()
    scene = _scene_for(stage)

    a, b, c = _mk(scene, "prim"), _mk(scene, "prim"), _mk(scene, "prim")
    tail = _mk(scene, "script_runner")
    rewire = _mk(scene, "script_runner")
    qapp.processEvents()

    pa, pb, pc, pt = (get_chunk_prim_path(x) for x in (a, b, c, tail))
    connect_exec(stage, scene, pa, pb)
    connect_exec(stage, scene, pb, pc)
    connect_exec(stage, scene, pc, pt)
    qapp.processEvents()

    tail.get_param("code").value = f"import {_PROBE} as p\np.tail_runs += 1\n"
    rewire.get_param("code").value = (
        "from usd_chunks import chunk_registry\n"
        "from usd_chunks.chunk_stage import connect_exec\n"
        f"connect_exec(stage, chunk_registry.ChunkRegistry().scene, {pa!r}, {pb!r})\n"
    )
    qapp.processEvents()

    out = tmp_path / "repeat_wire.usda"
    save_scene(scene, out)
    on_disk = _chunk_defs(Sdf.Layer.OpenAsAnonymous(str(out)))
    assert len(on_disk) >= 5, f"fixture must save its chunks, got {on_disk}"
    return out, on_disk, (pa, pb)


def _reopen(qapp, out, probe):
    probe.tail_runs = 0
    stage = Usd.Stage.Open(str(out))
    scene = _scene_for(stage)
    qapp.processEvents()
    return stage, scene


def test_opening_the_scene_leaves_its_root_layer_whole(qapp, saved, probe):
    out, on_disk, _ = saved

    stage, _scene = _reopen(qapp, out, probe)

    assert _chunk_defs(stage.GetRootLayer()) == on_disk, (
        "a chunk re-wiring an existing connection while the scene loads must not delete "
        "chunk definitions from the file's root layer"
    )


def test_the_same_path_reopened_in_this_process_is_still_whole(qapp, saved, probe):
    """What a later open or save of that path is handed: Sdf's cached layer."""
    out, on_disk, _ = saved

    _stage, _scene = _reopen(qapp, out, probe)

    assert _chunk_defs(Sdf.Layer.FindOrOpen(str(out))) == on_disk


def test_the_tail_cooks_once_on_open(qapp, saved, probe):
    out, _on_disk, _ = saved

    _reopen(qapp, out, probe)

    assert probe.tail_runs == 1, (
        f"the chain's tail cooked {probe.tail_runs} times on open; re-wiring an existing "
        "connection must not re-run the chunks above it"
    )


def test_rewiring_an_existing_connection_cooks_nothing(qapp, saved, probe):
    out, on_disk, (pa, pb) = saved
    stage, scene = _reopen(qapp, out, probe)
    probe.tail_runs = 0

    connect_exec(stage, scene, pa, pb)
    qapp.processEvents()

    assert probe.tail_runs == 0
    assert _chunk_defs(stage.GetRootLayer()) == on_disk


def test_a_new_connection_is_still_authored(qapp, saved, probe):
    """The no-op is only for a wire that is already there."""
    out, _on_disk, (pa, pb) = saved
    stage, scene = _reopen(qapp, out, probe)
    c_path = next(get_chunk_prim_path(c) for c in scene.get_chunks()
                  if get_chunk_prim_path(c) not in (pa, pb)
                  and stage.GetPrimAtPath(get_chunk_prim_path(c)).GetAttribute("info:chunkType").Get() == "prim")

    connect_exec(stage, scene, pa, c_path)

    conns = stage.GetPrimAtPath(c_path).GetAttribute("inputs:chunk").GetConnections()
    assert conns == [Sdf.Path(f"{pa}.outputs:chunk")]


def test_rewiring_to_a_different_source_still_reconciles(qapp, saved, probe, monkeypatch):
    """A changed wire can move a chunk's Edit Target, so it must still be reconciled."""
    out, _on_disk, (pa, pb) = saved
    stage, scene = _reopen(qapp, out, probe)
    calls = []
    real = chunk_stage.reconcile_edit_targets
    monkeypatch.setattr(chunk_stage, "reconcile_edit_targets",
                        lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    c_path = next(get_chunk_prim_path(c) for c in scene.get_chunks()
                  if get_chunk_prim_path(c) not in (pa, pb)
                  and stage.GetPrimAtPath(get_chunk_prim_path(c)).GetAttribute("info:chunkType").Get() == "prim")

    connect_exec(stage, scene, c_path, pb)

    assert calls, "a genuinely new wire must still be reconciled"
