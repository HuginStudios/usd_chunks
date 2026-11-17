"""A network saved by an older version of a chunk gets the handles the chunk declares now.

A chunk's gizmos(node) hook can change between versions: a handle moves to another anchor, or a
box becomes flat. Reopening a network saved before the change kept the records the old version
wrote, since they were only re-synced when an anchor param changed or a list was resized, so a
host drew the old handle. Now a hook chunk's records are brought in line after its first cook.
"""
from __future__ import annotations

from chunk_stubs import register_stub_module
from pxr import Usd, UsdGeom

from usd_chunks import Scene, chunk
from usd_chunks.gizmo import runtime as chunk_gizmo

FACES = ("px", "nx", "py", "ny", "pz", "nz")


def _main():
    c = chunk.Chunk("Frame")
    c.add_params([chunk.Slider(n, n, default_value=0.5, min_value=-1.0, max_value=1.0) for n in FACES])
    return c


def _gizmos(node):
    return [chunk.Gizmo("frame", type="box", anchor="/World/Card", driver=FACES, axis="screen")]


def _execute(node):
    UsdGeom.Xform.Define(node.get_stage(), "/World/Card")


def _frame(scene):
    prim = scene.get_stage().GetPrimAtPath("/World/Card")
    return next(h for h in chunk_gizmo.resolve_gizmos(prim) if h.name == "frame")


def test_a_handle_saved_by_an_older_version_comes_back_as_declared_now(tmp_path):
    path = str(tmp_path / "net.usda")
    with Scene() as scene:
        module = register_stub_module(main=_main, gizmos=_gizmos, execute=_execute)
        (c,) = module.load()
        scene.add_chunk(c)
        scene.set_output(c)
        assert _frame(scene).axis == "screen"
        scene.save(path)

    # As an older version of the chunk wrote it: the box without its axis.
    stage = Usd.Stage.Open(path)
    chunk_prim = next(p for p in stage.Traverse() if p.HasAttribute("ui:gizmo:frame:axis"))
    chunk_prim.RemoveProperty("ui:gizmo:frame:axis")
    stage.GetRootLayer().Save()
    del stage

    with Scene.open(path) as scene:
        assert _frame(scene).axis == "screen"


def test_records_that_match_are_left_alone(tmp_path):
    """The sync writes nothing when the records already say what the chunk declares."""
    path = str(tmp_path / "net.usda")
    with Scene() as scene:
        module = register_stub_module(main=_main, gizmos=_gizmos, execute=_execute)
        (c,) = module.load()
        scene.add_chunk(c)
        scene.set_output(c)
        scene.save(path)
    before = open(path, encoding="utf-8").read()

    with Scene.open(path) as scene:
        prim = next(p for p in scene.get_stage().Traverse() if p.HasAttribute("ui:gizmo:frame:axis"))
        assert chunk_gizmo._sync_runtime_records(scene.get_stage(), prim, scene.get_chunk(c.name),
                                                 chunk_gizmo.get_gizmos(scene.get_chunk(c.name))) is False
    assert open(path, encoding="utf-8").read() == before
