"""Undoing a chunk create and then redoing it must bring its node back.

**This does not currently reproduce the bug it was written for**, and is kept as a
regression guard for the path it does cover rather than as a fix's proof.

Reported live in usdview: undo until only the sublayer and view nodes are left, then redo
-- the chunk is in the Params tab and in the layer stack, but its node never reappears in
the graph. Modelled here by snapshotting and re-importing layer contents, which is how
usd-undo restores. What it restores is *USD*; the Python chunk objects are never unwound,
and that asymmetry is the interesting part -- the graph reconciles prims against live
chunks, so a prim coming back has to find its chunk again.

Under this model the graph reconciles correctly, both for a menu-created chunk and for one
auto-created by a gizmo drag. So the real cause is something the model does not capture:
the actual undo engine (`hugin`) cannot be driven from here -- it is a binary built for
Python 3.11 against the bundle's USD, and does not import under this interpreter.

Two things this DID surface, both real and neither the reported bug:

1. Undoing away *every* chunk makes /chunks invalid, which _rebuild_nodes treats as a
   transient recomposition: it keeps the graph and retries. After an undo that state is
   permanent, so nodes survive for prims that are genuinely gone. Not exercised below --
   the fixture keeps one chunk alive on purpose so this stays out of the way.
2. Restoring only the layers that existed at snapshot time does nothing at all, because a
   chunk's prim lives in a per-chunk layer created after the fact. See _restore.
"""
from __future__ import annotations

import time

from pxr import Usd, UsdGeom

from usd_chunks import chunk_gizmo, chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins.chunk_usd import get_chunk_prim_path, initialize as _chunk_usd_init


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _pump(qapp, seconds: float = 0.3) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()


def _snapshot(stage):
    """Layer contents, the way usd-undo records a restore point."""
    return {
        layer.identifier: layer.ExportToString()
        for layer in stage.GetLayerStack(includeSessionLayers=True)
    }


def _restore(stage, snap):
    """Put every layer back to its snapshotted text.

    A layer absent from the snapshot did not exist at that point -- creating a chunk makes
    it a fresh per-chunk layer -- so undoing past its creation empties it. Skipping those
    instead leaves the chunk prim authored in a layer nothing rolls back, and the "undo"
    quietly does nothing at all.
    """
    for layer in stage.GetLayerStack(includeSessionLayers=True):
        text = snap.get(layer.identifier)
        if text is None:
            if layer.empty:
                continue
            layer.Clear()
        elif layer.ExportToString() != text:
            layer.ImportFromString(text)


def test_redo_brings_the_node_back(qapp) -> None:
    from usd_chunks.plugins.chunk_qt import ChunkWidgetDocker

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
    docker.resize(500, 600)
    docker.show()
    qapp.processEvents()
    _pump(qapp)

    # One chunk that survives the undo, so /chunks stays valid throughout -- the reported
    # case is "undo until only the sublayer and view nodes are left", not "undo everything".
    # Emptying /chunks entirely takes a different branch (it reads as a transient
    # recomposition and keeps the graph), which is not the bug being chased here.
    keep_mod = reg.get_chunk_module("sublayer")
    keep = (keep_mod.create() if keep_mod.loaded else keep_mod.load())[0]
    scene.add_chunk(keep)
    _pump(qapp)

    before = _snapshot(stage)

    # Created the way the report was produced: dragging a prim nothing drives, which
    # auto-creates the chunk rather than going through the create menu.
    UsdGeom.Cube.Define(stage, "/World/Cube")
    desc = chunk_gizmo.ensure_transform_chunk(stage.GetPrimAtPath("/World/Cube"))
    assert desc is not None
    _pump(qapp)

    gv = docker.tab_widget.get_current_graph_view()
    path = str(desc.chunk_prim)
    assert path in gv.scene()._nodes, "node never appeared in the first place"
    after = _snapshot(stage)

    # Undo: the prim goes away. The Python chunk object does not -- it is still in the
    # Params tab, which is exactly the state the redo has to be reconciled against.
    _restore(stage, before)
    _pump(qapp)
    assert not stage.GetPrimAtPath(path).IsValid(), "undo did not remove the chunk prim"
    assert path not in gv.scene()._nodes

    _restore(stage, after)
    _pump(qapp)

    assert path in gv.scene()._nodes, "redo restored the prim but the graph never rebuilt it"
