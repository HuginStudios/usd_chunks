"""get_upstream_stage must not resurrect a dormant chunk's layer.

Live-reported: on Sublayer -> Transform -> Scatter -> Prune -> View, shaking the Prune
node loose brought the apple back on the real stage, but Scatter went on reading it
through Prune's orphaned "active = false" -- so Scatter found no surface geometry at all
and fell back to scattering around the origin, which looked like the Transform feeding it
had silently stopped being applied.

The trigger is counter-intuitive: while Prune was still wired downstream, the exclusion
in get_upstream_stage already covered it. DISCONNECTING it is what let it back in, since
it was no longer downstream of anything -- so the stale opinion only started biting once
the node was cut loose.
"""
from __future__ import annotations

from pxr import Sdf, Usd, UsdGeom

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins import chunk_usd
from usd_chunks.plugins.chunk_usd import initialize as _chunk_usd_init


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _mk(mod, scene):
    cs = mod.create() if mod.loaded else mod.load()
    for c in cs:
        scene.add_chunk(c)
    return cs[0]


def _setup():
    stage = Usd.Stage.CreateInMemory()

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)

    UsdGeom.Xform.Define(stage, "/apple")

    prim_mod = reg.get_chunk_module("prim")
    dormant = _mk(prim_mod, scene)   # stands in for the disconnected Prune
    reader = _mk(prim_mod, scene)    # stands in for Scatter, reading its upstream

    lm = chunk_registry.ChunkLayerManager()
    layer = lm.get_or_create_layer(dormant)
    # What Prune authors: deactivate the prim.
    with Usd.EditContext(stage, layer):
        stage.OverridePrim("/apple").SetActive(False)
    assert stage.GetPrimAtPath("/apple").IsActive() is False, "setup must be faithful"

    return stage, scene, dormant, reader, layer


def test_a_muted_chunk_layer_is_not_resurrected_in_an_upstream_read():
    """A muted layer is one the real stage is currently showing nothing of -- its chunk is
    dormant, either blocked (outside the active view's cook set, e.g. just disconnected)
    or explicitly disabled. A read of "what is upstream of me" must not bring it back."""
    stage, scene, dormant, reader, layer = _setup()

    stage.MuteLayer(layer.identifier)
    assert stage.GetPrimAtPath("/apple").IsActive() is True, (
        "sanity: muting the dormant chunk's layer must revive the prim on the REAL stage"
    )

    up = chunk_usd.get_upstream_stage(stage, scene, reader)
    assert up.GetPrimAtPath("/apple").IsActive() is True, (
        "the dormant chunk's layer is muted -- invisible everywhere the user looks -- so it "
        "must not still be driving another chunk's upstream read"
    )


def test_an_unmuted_chunk_layer_is_still_honoured():
    """The companion check, so the test above cannot pass by excluding everything."""
    stage, scene, dormant, reader, layer = _setup()

    assert not stage.IsLayerMuted(layer.identifier)
    up = chunk_usd.get_upstream_stage(stage, scene, reader)
    assert up.GetPrimAtPath("/apple").IsActive() is False, (
        "a live chunk's opinion must still reach another chunk's upstream read"
    )
