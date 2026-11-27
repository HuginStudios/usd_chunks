"""A slow chunk below a dragged handle cooks a preview, and in full when the drag ends.

An IK bake over a hundred frames, under the handle of the path it follows, took seconds a
drag step: the arm moved a second after the mouse. chunk.preview_cook(node) lets such a
chunk cook something rougher while a drag is open, and the framework cooks it again, in
full, when the last drag ends -- so the result left behind is the one a plain edit gives.
"""
from __future__ import annotations

from chunk_stubs import register_stub_module
from pxr import Sdf, UsdGeom

from usd_chunks import Scene, chunk, chunk_core

TARGET = "/World/target"


def _main():
    c = chunk.Chunk("Handle")
    c.add_param(chunk.Slider("amount", "Amount", default_value=0.0, min_value=-1.0, max_value=1.0))
    return c


def _write(node):
    """The handle: where the target is."""
    prim = UsdGeom.Xform.Define(node.get_stage(), TARGET).GetPrim()
    prim.CreateAttribute("amount", Sdf.ValueTypeNames.Double).Set(float(node.get_param("amount").value))


def _chain(scene):
    """A handle chunk and a slow chunk below it that reads the target, logging each cook."""
    cooks = []

    def slow_execute(node):
        prim = node.get_stage().GetPrimAtPath(TARGET)
        amount = prim.GetAttribute("amount").Get() if prim else None
        cooks.append((chunk.preview_cook(node), amount))

    handle = register_stub_module(main=_main, execute=_write).load()[0]
    slow = register_stub_module(execute=slow_execute).load()[0]
    scene.add_chunk(handle)
    scene.add_chunk(slow)
    scene.connect(handle, slow)
    scene.set_output(slow)
    cooks.clear()
    return handle, slow, cooks


def test_cooks_in_a_drag_are_previews_and_the_release_cooks_in_full():
    with Scene() as scene:
        handle, _slow, cooks = _chain(scene)
        handle.start_gizmo()
        scene.set(handle, amount=0.2)
        scene.set(handle, amount=0.4)
        assert cooks == [(True, 0.2), (True, 0.4)]
        handle.stop_gizmo()
        assert cooks[2:] == [(False, 0.4)], "the release cooks the previewed chunk once, in full"


def test_a_cook_outside_a_drag_is_the_real_one():
    with Scene() as scene:
        handle, _slow, cooks = _chain(scene)
        scene.set(handle, amount=0.3)
        assert cooks == [(False, 0.3)]


def test_a_full_cook_on_release_is_not_repeated(monkeypatch):
    """The release's own write (the drag throttle's, a stop hook) can reach the chunk. It
    cooks in full then, after the drag has closed, and is not cooked a second time."""
    with Scene() as scene:
        handle, slow, cooks = _chain(scene)
        monkeypatch.setattr(chunk_core, "GIZMO_STOP_HOOKS", [lambda _c: slow.execute()])
        handle.start_gizmo()
        scene.set(handle, amount=0.2)
        handle.stop_gizmo()
        assert cooks == [(True, 0.2), (False, 0.2)]
