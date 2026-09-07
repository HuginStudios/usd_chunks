"""Rediscovering an unchanged chunk file must not re-parse it.

Constructing a ChunkModule reads the file, AST-parses it and runs its config(). A second
ChunkRegistry.initialize() -- a re-init in the host, and every test that stands a scene up
-- paid that for the whole chunk directory again and threw the result away, since
registered_modules is a class attribute that already held the modules. That was ~1.4s per
initialize with the chunks in this repo, which is most of what the test suite spent its
time on.
"""
from __future__ import annotations

import textwrap

from usd_chunks import chunk_registry
from usd_chunks.chunk_registry import ChunkRegistry


_CHUNK_SRC = """
from usd_chunks import chunk


def main():
    return chunk.Chunk("{label}")


def config():
    return chunk.DefaulConfig(category="{label}")
"""


def _write_chunk(dir_path, name, label):
    f = dir_path / f"{name}.py"
    f.write_text(textwrap.dedent(_CHUNK_SRC.format(label=label)))
    return f


def test_unchanged_file_is_not_reparsed(tmp_path, monkeypatch):
    monkeypatch.setattr(chunk_registry, "_DISCOVERED_MODULES", {})
    _write_chunk(tmp_path, "cachetest_a", "First")

    reg = ChunkRegistry()
    first = reg._discover_module(tmp_path / "cachetest_a.py", "cachetest_a")
    again = reg._discover_module(tmp_path / "cachetest_a.py", "cachetest_a")

    assert again is first, "an unchanged file must come back from the cache"


def test_edited_file_is_rediscovered(tmp_path, monkeypatch):
    """The cache is keyed on modification time -- editing a chunk still takes effect."""
    monkeypatch.setattr(chunk_registry, "_DISCOVERED_MODULES", {})
    path = _write_chunk(tmp_path, "cachetest_b", "First")

    reg = ChunkRegistry()
    first = reg._discover_module(path, "cachetest_b")
    assert first[0].config.category == "First"

    _write_chunk(tmp_path, "cachetest_b", "Second")
    import os
    stamp = path.stat().st_mtime_ns + 1_000_000
    os.utime(path, ns=(stamp, stamp))

    second = reg._discover_module(path, "cachetest_b")
    assert second is not first
    assert second[0].config.category == "Second"


def test_rescanning_the_same_file_is_not_a_shadow(tmp_path, monkeypatch):
    """A module rediscovering itself is not two files claiming one name.

    Counting it as one made a second initialize() report every chunk in the directory as
    shadowed, which buried the genuinely ambiguous ones.
    """
    monkeypatch.setattr(chunk_registry, "_DISCOVERED_MODULES", {})
    path = _write_chunk(tmp_path, "cachetest_c", "First")

    reg = ChunkRegistry()
    # raising=False so monkeypatch removes these again on a registry that was never
    # initialize()d in this process -- an instance attribute set by hand on a singleton
    # would outlive the test.
    monkeypatch.setattr(reg, "plugin", "pytest", raising=False)
    monkeypatch.setattr(reg, "shadowed_modules", [], raising=False)
    monkeypatch.setitem(reg.registered_modules, "cachetest_c", reg._discover_module(path, "cachetest_c")[0])

    (mod,) = reg._discover_module(path, "cachetest_c")
    assert reg.register_module(mod) is reg.registered_modules["cachetest_c"]
    assert reg.shadowed_modules == []
