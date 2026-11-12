"""Scene.set with several params cooks the chunk once, with all of them, not once each."""
from __future__ import annotations

from collections import Counter

import pytest
from pxr import UsdGeom

from usd_chunks import Scene, chunk_cook


@pytest.fixture
def cooks(monkeypatch):
    counts: Counter = Counter()
    real = chunk_cook.cook

    def counting(chunk, layer_of, stamp):
        counts[chunk.name] += 1
        return real(chunk, layer_of, stamp)

    monkeypatch.setattr(chunk_cook, "cook", counting)
    return counts


def _moved(scene):
    ground = scene.create.mesh(prim_path="/World/ground", mesh_type="grid", name="ground")
    move = scene.create.transform(ground, primpath_location="/World/ground", name="move")
    scene.set_output(move)
    return move


def test_several_params_cook_once(cooks) -> None:
    """set(birches, max_count=90, scale_max=1.6) cooked a scatter twice, the first time with
    half the edit -- in a birch forest, 0.3 s and then 4.2 s."""
    with Scene() as scene:
        move = _moved(scene)
        cooks.clear()
        scene.set(move, tx=1.0, ty=2.0, tz=3.0)
        assert cooks["move"] == 1, cooks
        xf = UsdGeom.Xformable(scene.get_stage().GetPrimAtPath("/World/ground"))
        moved = xf.GetLocalTransformation().ExtractTranslation()
        assert (moved[0], moved[1], moved[2]) == (1.0, 2.0, 3.0)


def test_one_param_still_cooks_at_once(cooks) -> None:
    with Scene() as scene:
        move = _moved(scene)
        cooks.clear()
        scene.set(move, tx=4.0)
        assert cooks["move"] == 1, cooks
