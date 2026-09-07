"""is_chunk_internal_layer must answer before ChunkLayerManager is initialized.

It reads `internal_layer_ids`, which initialize() creates -- but the question is asked
while a stage is still being set up: ChunkStageSync.initialize -> reconcile_edit_targets
-> _migratable_layers. A host (or a test fixture) that brings the sync up before the layer
manager therefore crashed with AttributeError, where the correct answer is simply that
nothing is internal yet.
"""
from __future__ import annotations

from pxr import Sdf

from usd_chunks import chunk_registry
from usd_chunks.chunk_registry import ChunkLayerManager


def test_it_answers_before_initialize():
    """The never-initialized state, reproduced on a singleton other tests have touched.

    Dropping the instance attribute is what makes this the pre-initialize case: the value
    then resolves to the class-level default, which is the whole fix. Restored afterwards
    rather than left dropped -- a singleton keeps whatever a test does to it.
    """
    lm = ChunkLayerManager()
    saved = lm.__dict__.pop("internal_layer_ids", None)
    try:
        assert chunk_registry.is_chunk_internal_layer(Sdf.Layer.CreateAnonymous("x")) is False
    finally:
        if saved is not None:
            lm.__dict__["internal_layer_ids"] = saved


def test_initialize_binds_a_fresh_set_on_the_instance():
    """The class default must stay the empty pre-initialize answer, never shared state."""
    lm = ChunkLayerManager()
    lm.initialize(None)
    lm.internal_layer_ids.add("mine")

    assert "mine" in lm.__dict__["internal_layer_ids"], "initialize() binds on the instance"
    assert ChunkLayerManager.internal_layer_ids == set(), (
        "so the class-level default is never written through"
    )
