"""Every chunk shows the same params after a reopen as before it, with the open cache on.

With the open cache on, as it is for users, a reopened chunk's result is read back from the
cache and execute() never runs; a restored value sends no change either. A chunk that shows
or hides params only from execute() or a change callback then reopens showing the wrong ones:
a USD Cube hid its Size, a Vine showed its animation frames with Animate off.
"""
from __future__ import annotations

import pytest

from usd_chunks import Scene, chunk_catalog, chunk_registry, cook_cache, open_cache, styles
from usd_chunks.chunk_flags import ChunkFlags

#: Not made from a bare create: they need inputs, files or optional libraries to build at all.
_SKIP_MODULES = ("math_ops", "vector_math_ops", "mtlx_shader", "kitti_writer")


def _visible(owner, prefix="") -> set[str]:
    out = set()
    for p in owner.get_my_params():
        if p.has_state(ChunkFlags.State.HIDDEN):
            continue
        out.add(prefix + p.name)
        if hasattr(p, "get_my_params") and not hasattr(p, "get_type"):
            out |= _visible(p, prefix + p.name + ".")
    return out


def _names() -> list[str]:
    with Scene():
        entries = chunk_catalog.entries(chunk_registry.ChunkRegistry())
        # A style is a Reference to a saved network, not a chunk module, and it would download
        # its assets: tests/test_styles.py covers them.
        style_names = {s.name for s in styles.find_styles()}
    return [e.name for e in entries if e.category != "Examples" and e.module not in _SKIP_MODULES
            and e.name not in style_names]


@pytest.fixture
def cached(monkeypatch, tmp_path):
    monkeypatch.setenv(open_cache.ENV, "1")
    monkeypatch.setenv(cook_cache.ENV, "1")
    monkeypatch.setenv(cook_cache.ENV_DIR, str(tmp_path / "cache"))
    monkeypatch.setenv("USD_CHUNK_AUTO_INSTALL", "0")


def test_every_chunk_reopens_showing_the_same_params(cached, tmp_path) -> None:
    names = _names()
    assert len(names) > 80
    differ = {}
    for name in names:
        path = str(tmp_path / f"{name}.usda")
        scene = Scene.new(path=path)
        made = getattr(scene.create, name)(name="C")
        scene.set_output(made)
        before = _visible(made)
        scene.save(path)
        after = _visible(Scene.open(path).get_chunk("C"))
        if before != after:
            differ[name] = {"lost": sorted(before - after), "gained": sorted(after - before)}
    assert not differ, differ
