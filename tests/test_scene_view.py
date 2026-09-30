"""scene.view(chunk) makes the active View show that chunk, like wiring it in the graph.

Before it, a script had to call view.add_dependency(chunk): that kept the View's old wire
as well, and did not re-cook through the View, so the viewport could stay unchanged.
"""

from __future__ import annotations

from usd_chunks import Scene, chunk_stage


def _wires(scene, view):
    prim = scene.get_stage().GetPrimAtPath(chunk_stage.get_chunk_prim_path(view))
    return [str(p.GetPrimPath()).rsplit("/", 1)[-1] for p in prim.GetAttribute("inputs:chunk").GetConnections()]


def _is_off(scene, chunk):
    layer = chunk.get_edit_context()
    return layer is not None and scene.get_stage().IsLayerMuted(layer.identifier)


def _y(scene, path):
    from pxr import UsdGeom

    prim = scene.get_stage().GetPrimAtPath(path)
    if not (prim and prim.IsDefined()):
        return None
    return UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(0).ExtractTranslation()[1]


def test_view_makes_a_view_when_there_is_none() -> None:
    with Scene.new() as scene:
        box = scene.create.cube(prim_path="/box")
        lift = scene.create.transform(box, primpath_location=box.get_param("prim_path"), ty=3.0)

        view = scene.view(lift)

        assert view is not None and chunk_stage.is_view_chunk(view)
        assert _wires(scene, view) == ["Transform"]
        assert _y(scene, "/box") == 3.0


def test_view_replaces_the_view_input_and_switches_branches() -> None:
    with Scene.new() as scene:
        box = scene.create.cube(prim_path="/box")
        view = scene.view(box)
        lift = scene.create.transform(box, primpath_location=box.get_param("prim_path"), ty=3.0)

        assert scene.view(lift) is view, "the active View is rewired, not a new one made"
        assert _wires(scene, view) == ["Transform"], "the old wire is replaced"
        assert _y(scene, "/box") == 3.0

        ball = scene.create.sphere(prim_path="/ball")
        raised = scene.create.transform(ball, primpath_location=ball.get_param("prim_path"), ty=7.0)
        scene.view(raised)

        assert _wires(scene, view) == ["Transform1"]
        assert _y(scene, "/ball") == 7.0
        assert _is_off(scene, lift) and _is_off(scene, box), "the other branch is switched off"

        scene.view(lift)
        assert _y(scene, "/box") == 3.0 and not _is_off(scene, lift)
        assert _is_off(scene, raised)
