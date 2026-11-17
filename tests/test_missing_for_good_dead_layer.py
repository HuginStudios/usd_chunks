"""missing_for_good walks every loaded layer, and one can die while it does.

Sdf.Layer.GetLoadedLayers hands out weak handles. A garbage collection during the walk can
free a layer nothing else holds -- the session layer of a stage a finished test left as
garbage -- and its handle then raises Boost.Python.ArgumentError on any call. A Transform's
cook asking whether its target is missing failed on that, now and then, in a full test run.
"""

from __future__ import annotations

import pytest
from pxr import Sdf, Usd

from usd_chunks import chunk_utils


class _DeadLayer:
    """A handle whose layer has been freed: every call raises, as Boost's does."""

    expired = True
    identifier = "anon:dead"

    def GetPrimAtPath(self, path):  # noqa: N802 - Sdf's name
        raise TypeError("Python argument types did not match C++ signature")


def test_a_layer_that_died_during_the_walk_holds_no_prim(monkeypatch):
    stage = Usd.Stage.CreateInMemory()
    loaded = Sdf.Layer.GetLoadedLayers()
    monkeypatch.setattr(Sdf.Layer, "GetLoadedLayers", staticmethod(lambda: [_DeadLayer(), *loaded]))

    assert chunk_utils.missing_for_good(stage, "/Nowhere/at/all")


def test_a_live_layer_that_raises_still_raises(monkeypatch):
    class _Broken(_DeadLayer):
        expired = False

    stage = Usd.Stage.CreateInMemory()
    monkeypatch.setattr(Sdf.Layer, "GetLoadedLayers", staticmethod(lambda: [_Broken()]))

    with pytest.raises(TypeError):
        chunk_utils.missing_for_good(stage, "/Nowhere")
