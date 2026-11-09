"""In a script, the last input into a Merge wins, as a chunk chained after the others would.

`scene.create.merge(a, b)` wired a into slot 0, the strongest, so a won: the opposite of
the graph, where a branch joined at the Merge's empty slot goes into slot 0 and wins, and of
a chain, where the chunk after the rest wins.
"""
from __future__ import annotations

from pxr import Usd, UsdGeom

from usd_chunks import Scene
from usd_chunks.chunk_naming import iter_exec_inputs


def _two_radii(scene):
    ball = scene.create.prim(primpath_location="/World/ball", prim_type="Sphere", name="ball")
    a = scene.create.property_editor(ball, prim="/World/ball", edits="double radius = 1", name="a")
    b = scene.create.property_editor(ball, prim="/World/ball", edits="double radius = 2", name="b")
    return a, b


def _radius(scene) -> float:
    return UsdGeom.Sphere(scene.get_stage().GetPrimAtPath("/World/ball")).GetRadiusAttr().Get()


def _slots(path, name) -> list[str]:
    stage = Usd.Stage.Open(str(path))
    prim = next(p for p in stage.Traverse() if p.GetName() == name and p.HasAttribute("inputs:chunk"))
    return [c.GetPrimPath().name for a in iter_exec_inputs(prim) for c in a.GetConnections()]


def test_the_last_input_into_a_merge_wins(tmp_path) -> None:
    path = tmp_path / "merge.usda"
    with Scene() as scene:
        a, b = _two_radii(scene)
        scene.set_output(scene.create.merge(a, b, name="merge"))
        assert _radius(scene) == 2.0, "the first input won"
        scene.save(str(path))
    assert _slots(path, "merge") == ["b", "a"]

    with Scene.open(str(path)) as scene:
        assert _radius(scene) == 2.0, "reopened, the first input won"


def test_a_switch_keeps_its_inputs_in_argument_order(tmp_path) -> None:
    path = tmp_path / "switch.usda"
    with Scene() as scene:
        a, b = _two_radii(scene)
        scene.create.switch(a, b, name="switch")
        scene.save(str(path))
    assert _slots(path, "switch") == ["a", "b"]
