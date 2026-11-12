"""`scene.connect` wires chunks that already exist, into a Merge the way the graph does.

`chunk.add_dependency` put a new Merge branch in the weakest slot, so a branch wired in
later lost, the opposite of the graph and of `scene.create.merge(a, b)`.
"""
from __future__ import annotations

import pytest
from pxr import UsdGeom

from usd_chunks import Scene


def _radii(scene, *values):
    ball = scene.create.prim(primpath_location="/World/ball", prim_type="Sphere", name="ball")
    edits = [scene.create.property_editor(ball, prim="/World/ball", edits=f"double radius = {v}", name=f"r{v}")
             for v in values]
    return ball, edits


def _radius(scene) -> float:
    return UsdGeom.Sphere(scene.get_stage().GetPrimAtPath("/World/ball")).GetRadiusAttr().Get()


def _names(chunks) -> list[str]:
    return [c.name for c in chunks]


def test_a_branch_wired_into_a_merge_later_wins(tmp_path) -> None:
    path = tmp_path / "merge.usda"
    with Scene() as scene:
        _, (one, two) = _radii(scene, 1, 2)
        merge = scene.create.merge(one, name="merge")
        scene.set_output(merge)
        scene.connect(two, merge)
        assert _names(scene.get_inputs(merge)) == ["r2", "r1"]
        assert _radius(scene) == 2.0
        scene.save(str(path))

    with Scene.open(str(path)) as scene:
        assert _radius(scene) == 2.0, "reopened, the branch wired in later lost"


def test_a_slot_puts_the_branch_lower(tmp_path) -> None:
    with Scene() as scene:
        _, (one, two, three) = _radii(scene, 1, 2, 3)
        merge = scene.create.merge(one, name="merge")
        scene.set_output(merge)
        scene.connect(two, merge, slot=1)
        assert _names(scene.get_inputs(merge)) == ["r1", "r2"]
        scene.connect(three, merge, slot=1)
        assert _names(scene.get_inputs(merge)) == ["r1", "r3", "r2"]
        assert _radius(scene) == 1.0


def test_wiring_into_a_merge_that_cannot_take_it_raises_and_changes_nothing() -> None:
    with Scene() as scene:
        _, (one, two) = _radii(scene, 1, 2)
        merge = scene.create.merge(one, two, name="merge")
        before = _names(scene.get_inputs(merge))
        with pytest.raises(ValueError, match="already feeds"):
            scene.connect(one, merge)
        with pytest.raises(ValueError, match="slot must be"):
            scene.connect(scene.create.transform(name="t"), merge, slot=5)
        with pytest.raises(ValueError, match="not a Merge"):
            scene.connect(one, two, slot=0)
        assert _names(scene.get_inputs(merge)) == before


def test_any_other_chunk_gets_an_ordinary_wire() -> None:
    with Scene() as scene:
        ball, (one,) = _radii(scene, 1)
        lift = scene.create.transform(name="lift")
        scene.connect(one, lift)
        assert _names(scene.get_inputs(lift)) == ["r1"]
        assert _names(scene.get_inputs(one)) == [ball.name]
