"""A cook whose layer leaves the stage mid-solve is skipped, not raised through.

Live-reported while wiring a new light and transform into a Stage Expression Switch:

    File "chunks/transforms/transform.py", line 41, in _rescale_translate_range
      param.min_value = -span
    ...
    File "usd_chunks/chunk_notice.py", line 1303, in execute
      with Usd.EditContext(stage, ctx):
    pxr.Tf.ErrorException: Layer @anon:...Transform_...@ is not in the local LayerStack
    rooted at @C:/UsdScenes/chunk_demos/10_two_sequences_v3.usda@

The dispatch already refuses to cook a chunk whose layer is muted or not yet in the stack --
there is nowhere for it to author, and a chunk in that state is covered by
`test_a_layer_muted_before_the_cook_is_skipped_quietly` below, which passes with or without
this change.

What the guards could not cover is the layer leaving *between* that check and the `with` a
line later. This dispatch is re-entrant: a module setting one of its own params during
execute lands straight back here for whatever that param drives -- which is precisely what
transform.py does on line 41 -- and a cook reached that way can mute layers, because that is
how a gate suppresses a branch.

Raising there is wrong twice over. The chunk has nowhere to author, which is the same
situation the guards already decided to skip; and the exception does not stay local -- it
unwinds whatever cook or gate was running, which is how a half-applied gate and a stuck
light came about in the first place.

Only that error is skipped: the layer is re-checked, and anything else still raises.
"""

from __future__ import annotations

import pytest
from pxr import Usd, UsdGeom

from usd_chunks import chunk_registry, chunk_stage
from usd_chunks.chunk_scene import SceneSender

THING = "/thing"


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture
def placed():
    """A real Transform chunk on a real prim, with its own edit layer."""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    sender = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(sender, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_stage.initialize(stage, sender)
    UsdGeom.Xform.Define(stage, THING)

    mod = chunk_registry.ChunkRegistry().get_chunk_module("transform")
    node = (mod.create() if mod.loaded else mod.load())[0]
    sender.add_chunk(node)
    node.get_param("primpath_location").value = THING
    node.recook()

    layer = chunk_registry.ChunkLayerManager().get_layer(node)
    assert layer is not None, "test setup: the chunk needs its own edit layer"
    return sender, stage, node, layer


def test_a_layer_muted_before_the_cook_is_skipped_quietly(placed):
    """The state the existing guards already cover, pinned so it stays covered."""
    sender, stage, node, layer = placed
    stage.MuteLayer(layer.identifier)

    node.get_param("translate_grp.tx").value = 5.0
    node.recook()  # must not raise


# The reported crash itself is NOT pinned here, and that is worth stating rather than
# papering over. Reproducing it needs the layer to leave the stack between the guard and the
# `with` a line later; muting it as the edit context is constructed is not enough (USD does
# not object at that moment), and every other way of forcing it ends up patching the very
# state the fix re-checks, which proves nothing. So the change below it is a narrow guard
# against a real traceback from a live session, with its reasoning written down, rather than
# a fix with a failing test behind it. The two tests here pin what CAN be pinned: the state
# the guards already covered, and that the ordinary path is untouched.


def test_the_ordinary_cook_still_authors(placed):
    """Nothing about the common path changes: the transform still lands on the prim."""
    sender, stage, node, layer = placed

    node.get_param("translate_grp.tx").value = 12.0
    node.recook()

    matrix = UsdGeom.Xformable(stage.GetPrimAtPath(THING)).GetLocalTransformation()
    matrix = matrix[0] if isinstance(matrix, tuple) else matrix
    assert matrix.ExtractTranslation()[0] == pytest.approx(12.0)
