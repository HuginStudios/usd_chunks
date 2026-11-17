"""Cache keeps everything above it in one .usdc file and reads that instead.

Recache cooks the chunks above, flattens them into the file and switches them off; a change
above leaves the file showing and marks the Cache stale; opening the network reads the file
and cooks nothing above it.
"""

from __future__ import annotations

import os

import pytest
from pxr import Sdf, UsdGeom

from usd_chunks import Scene, chunk, chunk_cook
from usd_chunks.chunk_flags import ChunkFlags


@pytest.fixture
def cooked(monkeypatch):
    """The names of the chunks whose module ran, in order."""
    names = []
    real = chunk_cook.cook

    def counting(c, *args, **kwargs):
        ran = real(c, *args, **kwargs)
        if ran:
            names.append(c.name)
        return ran

    monkeypatch.setattr(chunk_cook, "cook", counting)
    return names


def _height(scene) -> float:
    prim = scene.get_stage().GetPrimAtPath("/World/box")
    return UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(0).ExtractTranslation()[1]


def _network(scene):
    """Box, three Transforms of 1 each, the Cache, and a Transform of 10 below it."""
    with chunk.change_block():
        last = scene.create.cube(name="Box", prim_path="/World/box")
        for i in range(3):
            last = scene.create.transform(last, name=f"T{i}", primpath_location="/World/box", ty=1.0)
        cache = scene.create.cache(last, name="Cache")
        below = scene.create.transform(cache, name="Below", primpath_location="/World/box", ty=10.0)
        scene.set_output(below)
    return cache


def test_without_a_file_it_lets_everything_above_through(tmp_path):
    with Scene.new(path=str(tmp_path / "net.usda")) as scene:
        cache = _network(scene)

        assert _height(scene) == pytest.approx(13.0)
        assert not scene.get_chunk("T0").is_blocked()
        assert cache.get_param("status").value.startswith("No cache yet")


def test_recache_writes_the_file_beside_the_network_and_reads_it(tmp_path):
    with Scene.new(path=str(tmp_path / "net.usda")) as scene:
        cache = _network(scene)

        cache.get_param("recache").click()

        assert cache.get_param("file").value == "./cache/net_Cache.usdc"
        path = tmp_path / "cache" / "net_Cache.usdc"
        assert path.is_file()
        assert Sdf.Layer.FindOrOpen(str(path)).GetPrimAtPath("/World/box") is not None
        assert all(scene.get_chunk(n).is_blocked() for n in ("Box", "T0", "T1", "T2"))
        assert _height(scene) == pytest.approx(13.0)
        assert not cache.has_state(ChunkFlags.State.STALE)


def test_a_change_above_keeps_the_file_and_marks_it_stale(tmp_path, cooked):
    with Scene.new(path=str(tmp_path / "net.usda")) as scene:
        cache = _network(scene)
        cache.get_param("recache").click()
        cooked.clear()

        scene.set(scene.get_chunk("T0"), ty=5.0)

        assert cooked == ["Cache"], "nothing above cooks; the Cache checks its file"
        assert _height(scene) == pytest.approx(13.0)
        assert cache.has_state(ChunkFlags.State.STALE)
        assert cache.get_param("status").value.startswith("Stale")

        cache.get_param("recache").click()
        assert _height(scene) == pytest.approx(17.0)
        assert not cache.has_state(ChunkFlags.State.STALE)


def test_opening_reads_the_file_and_cooks_nothing_above(tmp_path, cooked):
    path = tmp_path / "net.usda"
    with Scene.new(path=str(path)) as scene:
        cache = _network(scene)
        cache.get_param("recache").click()
        scene.save()
    cooked.clear()

    with Scene.open(str(path)) as scene:
        assert not {"Box", "T0", "T1", "T2"} & set(cooked)
        assert _height(scene) == pytest.approx(13.0)
        assert not scene.get_chunk("Cache").has_state(ChunkFlags.State.STALE)


def test_a_deleted_file_lets_everything_above_through_again(tmp_path):
    with Scene.new(path=str(tmp_path / "net.usda")) as scene:
        cache = _network(scene)
        cache.get_param("recache").click()
        os.remove(tmp_path / "cache" / "net_Cache.usdc")

        cache.execute()

        assert not scene.get_chunk("T0").is_blocked()
        assert _height(scene) == pytest.approx(13.0)


def test_a_cache_saved_stale_is_stale_again_after_opening(tmp_path, cooked):
    path = tmp_path / "net.usda"
    with Scene.new(path=str(path)) as scene:
        cache = _network(scene)
        cache.get_param("recache").click()
        scene.set(scene.get_chunk("T0"), ty=5.0)
        scene.save()
    cooked.clear()

    with Scene.open(str(path)) as scene:
        assert not {"Box", "T0", "T1", "T2"} & set(cooked)
        assert scene.get_chunk("Cache").has_state(ChunkFlags.State.STALE)
        assert cooked.count("Cache") == 1
