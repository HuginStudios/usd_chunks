"""Undo has to move the sliders too, not just the prim.

Reported live in usdview: Ctrl+Z moves the object back, but the chunk's translate values
stay where they were -- and only catch up once you touch the gizmo again.

**These pass, so they do not reproduce the report.** Kept as a regression guard for the
mirror they do exercise, and as a record of what has been ruled out.

ChunkStageSync mirrors USD back into params in _on_objects_changed, but only for paths in
GetChangedInfoOnlyPaths(). The theory was that undo restores whole layer contents, which
USD would report as a *resync* the mirror never looks at. Modelling undo as a layer-content
restore, USD still reports it as info-only and the mirror runs -- so that theory is wrong,
or at least incomplete, and no fix has been made on the strength of it.

The real engine cannot be driven from here: hugin is a binary built for Python 3.11 against
the bundle's USD and does not import under this interpreter. diagnose_redo.py's watch() /
report() print which of the two USD actually uses during a real Ctrl+Z, and whether the
guards at the top of the handler suppressed it.
"""
from __future__ import annotations

from pxr import Sdf, Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins.chunk_usd import initialize as _chunk_usd_init


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _session(stage):
    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)
    return reg, scene


def _snapshot(stage):
    return {
        layer.identifier: layer.ExportToString()
        for layer in stage.GetLayerStack(includeSessionLayers=True)
    }


def _restore(stage, snap):
    """Undo, as usd-undo does it: put layer contents back wholesale.

    This is the crux -- a wholesale content restore is reported by USD as a resync of the
    affected paths, not as an info-only change, which is why the param mirror missed it.
    """
    for layer in stage.GetLayerStack(includeSessionLayers=True):
        text = snap.get(layer.identifier)
        if text is not None and layer.ExportToString() != text:
            layer.ImportFromString(text)


def _transform_chunk(reg, scene):
    mod = reg.get_chunk_module("transform")
    chunk = (mod.create() if mod.loaded else mod.load())[0]
    scene.add_chunk(chunk)
    return chunk


def test_undo_restores_the_param_not_just_the_prim() -> None:
    stage = Usd.Stage.CreateInMemory()
    reg, scene = _session(stage)
    chunk = _transform_chunk(reg, scene)
    chunk.get_param("primpath_location").value = "/World/Cube"

    chunk.get_param("tx").value = 5.0
    before = _snapshot(stage)

    chunk.get_param("tx").value = 11.0
    assert chunk.get_param("tx").value == 11.0

    _restore(stage, before)

    assert chunk.get_param("tx").value == 5.0, (
        "the slider kept the undone value -- the object moves back but the chunk disagrees"
    )


def test_redo_moves_the_param_forward_again() -> None:
    stage = Usd.Stage.CreateInMemory()
    reg, scene = _session(stage)
    chunk = _transform_chunk(reg, scene)
    chunk.get_param("primpath_location").value = "/World/Cube"

    chunk.get_param("tx").value = 5.0
    before = _snapshot(stage)
    chunk.get_param("tx").value = 11.0
    after = _snapshot(stage)

    _restore(stage, before)
    _restore(stage, after)

    assert chunk.get_param("tx").value == 11.0


def test_an_ordinary_edit_still_syncs() -> None:
    """The info-only path is the common one, and the control for the two above."""
    stage = Usd.Stage.CreateInMemory()
    reg, scene = _session(stage)
    chunk = _transform_chunk(reg, scene)

    chunk.get_param("tx").value = 3.0
    attr_path = getattr(chunk.get_param("tx"), "_param_attr_path", None)
    assert attr_path, "param never got an attribute to mirror"

    # Into the SESSION layer. Chunk inputs are authored there, and it outranks the root
    # layer -- writing through the stage's ambient edit target instead leaves the resolved
    # value untouched, so the mirror correctly does nothing and the test fails blaming it.
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        stage.GetAttributeAtPath(Sdf.Path(attr_path)).Set(7.5)

    assert chunk.get_param("tx").value == 7.5
