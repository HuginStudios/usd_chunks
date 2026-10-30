"""scene.building(): a script makes many chunks, and each cooks about once instead of again and again."""
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


def _chain(scene, n=8):
    """A ground and n scatters, each wired after the last, set up the way a script does it."""
    ground = scene.create.mesh(prim_path="/World/ground", mesh_type="grid", name="ground")
    leaf = scene.create.prim(ground, primpath_location="/World/leaf", prim_type="Cube", name="leaf")
    last = leaf
    for i in range(n):
        last = scene.create.scatter_points(last, name=f"s{i}", inst_path=f"/World/s{i}", max_count=40, seed=i,
                                           **{"prototypes.0.proto_path": "/World/leaf"})
        scene.set(last, surface_prim="/World/ground")
    return last


def _positions(scene, n=8):
    stage = scene.get_stage()
    return [list(UsdGeom.PointInstancer(stage.GetPrimAtPath(f"/World/s{i}")).GetPositionsAttr().Get()) for i in range(n)]


def test_a_chain_built_in_a_block_cooks_each_chunk_at_most_twice(cooks) -> None:
    with Scene() as scene:
        with scene.building():
            _chain(scene)
        assert max(cooks.values()) <= 2, cooks


def test_a_block_builds_the_same_scene(cooks) -> None:
    with Scene() as scene:
        _chain(scene)
        plain = _positions(scene)
        plain_cooks = sum(cooks.values())
    cooks.clear()
    with Scene() as scene:
        with scene.building():
            _chain(scene)
        assert _positions(scene) == plain
        assert sum(cooks.values()) < plain_cooks


def test_a_light_made_in_a_block_takes_its_settings() -> None:
    """A Light builds its schema params in its cook, so it has to cook as it is made."""
    with Scene() as scene:
        with scene.building():
            light = scene.create.light(light_type="DistantLight", primpath_location="/World/sun")
            intensity = next(p for p in light.get_params() if p.name == "intensity" or p.name.endswith(":intensity"))
            intensity.value = 3.0
        assert intensity.value == 3.0
