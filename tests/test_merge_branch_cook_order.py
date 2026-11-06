"""A Merge's branches cook in the order they are wired in, not the order they were made in.

Nothing is wired between two branches of a Merge, so the cook queue ordered them by depth
alone, and chunks at the same depth cooked in the order they were made. A Transform made
before the Cube it moves cooked first when the scene was opened again, found no box, and
moved nothing. The branches now cook as a View cooks them: the Merge's last input first and
its first input last, so the first input sees what the others made.
"""
from __future__ import annotations

import pytest
from pxr import UsdGeom

from usd_chunks import Scene


def _box_z(scene) -> float:
    prim = scene.get_stage().GetPrimAtPath("/geo/box")
    return UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(0).ExtractTranslation()[2]


@pytest.mark.parametrize("transform_made_first", [True, False])
def test_a_merges_first_input_cooks_after_the_others_once_reopened(tmp_path, transform_made_first) -> None:
    path = str(tmp_path / "merge.usda")
    with Scene() as scene:
        if transform_made_first:
            move = scene.create.transform(name="Move", primpath_location="/geo/box")
            box = scene.create.cube(name="Box", prim_path="/geo/box")
        else:
            box = scene.create.cube(name="Box", prim_path="/geo/box")
            move = scene.create.transform(name="Move", primpath_location="/geo/box")
        scene.set_output(scene.create.merge(move, box, name="Merge"))
        scene.set(move, tz=5.0)
        assert _box_z(scene) == 5.0
        scene.save(path)

    with Scene.open(path) as scene:
        assert _box_z(scene) == 5.0, "the Transform cooked before the Cube and found no box"
