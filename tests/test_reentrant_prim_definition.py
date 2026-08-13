"""A new chunk's own prim can be invalidated mid-construction by a sibling chunk's
reentrant re-execution, triggered by the very exec-chain connection being wired into it.
See chunk_usd.py's _define_prim, the exec_in.SetConnections block."""
from __future__ import annotations

import importlib

from pxr import Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import ChunkConfig, SceneSender
from usd_chunks.plugins.chunk_usd import initialize as init_chunk_usd
import usd_chunks.plugins.chunk_usd as chunk_usd


class _DummyScene(SceneSender):
    def __init__(self):
        super().__init__()
        self._stage = None

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


def _make(scene, mod, module_name):
    c = mod.main()
    c._config = ChunkConfig(module=module_name)
    c._module = mod
    # Auto-chain prev -> new (Params-tab behavior), same as a user repeatedly clicking
    # "add chunk" in the Params tab -- this is what wires each new chunk's inputs:chunk
    # connection to its predecessor, the trigger for the reentrancy below.
    prev = chunk_usd._params_tab_create
    chunk_usd._params_tab_create = True
    try:
        scene.add_chunk(c)
    finally:
        chunk_usd._params_tab_create = prev
    return c


def test_chaining_several_create_layer_edit_target_pairs_does_not_crash():
    """Building N chained Create Layer -> Edit Target pairs one at a time (the Params
    tab's auto-chain behavior) must not crash, however many pairs.

    Wiring a new chunk's inputs:chunk connection to its predecessor changes exec
    topology -- and Create Layer's own execute() reactively re-ranks its layer against
    the active view on any exec-chain change, reentrant, before _define_prim (still
    building the NEW chunk's own prim) has returned. That reentrant run reassigns
    subLayerPaths (repositioning itself), forcing a stage recomposition that can
    invalidate the new chunk's own still-in-progress Usd.Prim handle if it composes
    through the same layer subtree -- even though the prim spec itself is untouched.
    Live-reproduced building a 3rd chained pair (crashed with "RuntimeError: Accessed
    invalid expired 'Chunk' prim"); this goes to 8 to stay well clear of that edge.
    """
    create_mod = importlib.import_module("scripts.create_layer")
    edit_mod = importlib.import_module("scripts.edit_target")

    scene = _DummyScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    stage = Usd.Stage.CreateInMemory()
    scene._stage = stage
    init_chunk_usd(stage, scene)

    try:
        chunks = []
        for i in range(8):
            cl = _make(scene, create_mod, f"cl_{i}")
            cl.get_param("layer_name").value = f"layer{i}"
            cl.execute()
            chunks.append(cl)

            et = _make(scene, edit_mod, f"et_{i}")
            et.get_param("edit_target").value = f"layer{i}"
            et.execute()
            chunks.append(et)

        assert len(list(scene.get_chunks())) == 16
    finally:
        for c in list(scene.chunks.get_my_params()):
            c.request_delete()
