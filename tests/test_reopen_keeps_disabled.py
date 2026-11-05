"""Closing a script's Scene leaves the file it opened as it was.

Deleting a chunk clears its DISABLED state so its last notices go out, and the stage sync
wrote that back as active = true -- into the opened file's own layer, which stays in memory.
The same file opened again in the process, and saved, came back with every disabled chunk on.
"""
from __future__ import annotations

from pxr import Sdf, Usd

from usd_chunks import Scene
from usd_chunks.graph import engine


def _active(path, name):
    stage = Usd.Stage.Open(Sdf.Layer.OpenAsAnonymous(str(path)))
    return next(p for p in stage.TraverseAll() if p.GetName() == name).IsActive()


def test_a_disabled_chunk_stays_disabled_after_close_and_reopen(tmp_path):
    first, second = tmp_path / "first.usda", tmp_path / "second.usda"
    with Scene(host_name="pytest") as scene:
        cube = scene.create.cube(prim_path="/a", name="A")
        engine.toggle_disabled([scene.create.transform(cube, name="B")])
        scene.save(str(first))
    assert not _active(first, "B")

    with Scene.open(str(first), lazy=True):
        pass
    with Scene.open(str(first), lazy=True) as scene:
        scene.save(str(second))
    assert not _active(second, "B")
