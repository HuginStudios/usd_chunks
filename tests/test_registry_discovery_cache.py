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
import os
import sys

from usd_chunks import chunk_registry
from usd_chunks.chunk_registry import ChunkRegistry


_CHUNK_SRC = """
from usd_chunks import chunk


def main():
    return chunk.Chunk("{label}")


def config():
    return chunk.DefaultConfig(category="{label}")
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
    stamp = path.stat().st_mtime_ns + 1_000_000
    os.utime(path, ns=(stamp, stamp))

    second = reg._discover_module(path, "cachetest_b")
    assert second is not first
    assert second[0].config.category == "Second"


def _new_process(monkeypatch, cache_file):
    """What a new process starts with: no modules discovered, the config cache read afresh."""
    monkeypatch.setattr(chunk_registry, "_DISCOVERED_MODULES", {})
    monkeypatch.setattr(chunk_registry, "_CONFIG_CACHE", chunk_registry._ConfigCache(str(cache_file)))


def _no_parsing(monkeypatch):
    def parse(*_args, **_kwargs):
        raise AssertionError("the module was parsed")

    monkeypatch.setattr(chunk_registry, "_pre_load_module", parse)


def test_a_config_comes_back_from_disk_in_the_next_process(tmp_path, monkeypatch):
    # Discovery parsed every chunk module and ran its config() on every process start --
    # a third of a second for this repo's chunks. The answer is kept beside the user's other
    # caches and read back while the file is unchanged.
    cache = tmp_path / "configs.pickle"
    _new_process(monkeypatch, cache)
    path = _write_chunk(tmp_path, "cachetest_disk", "First")
    (first,) = ChunkRegistry()._discover_module(path, "cachetest_disk")
    chunk_registry._CONFIG_CACHE.save()

    _new_process(monkeypatch, cache)
    _no_parsing(monkeypatch)
    monkeypatch.delitem(sys.modules, "cachetest_disk", raising=False)
    (again,) = ChunkRegistry()._discover_module(path, "cachetest_disk")

    assert again is not first
    assert again.config.category == "First"
    assert again.has_main()


def test_an_edited_file_is_parsed_again_in_the_next_process(tmp_path, monkeypatch):
    cache = tmp_path / "configs.pickle"
    _new_process(monkeypatch, cache)
    path = _write_chunk(tmp_path, "cachetest_disk_edit", "First")
    ChunkRegistry()._discover_module(path, "cachetest_disk_edit")
    chunk_registry._CONFIG_CACHE.save()

    _write_chunk(tmp_path, "cachetest_disk_edit", "Second")
    stamp = path.stat().st_mtime_ns + 1_000_000
    os.utime(path, ns=(stamp, stamp))
    _new_process(monkeypatch, cache)
    monkeypatch.delitem(sys.modules, "cachetest_disk_edit", raising=False)
    (again,) = ChunkRegistry()._discover_module(path, "cachetest_disk_edit")

    assert again.config.category == "Second"


def test_a_config_that_reads_module_data_is_not_kept(tmp_path, monkeypatch):
    # Module-level data can depend on more than the file -- what is installed, the platform
    # -- so a config() that reaches it is run afresh in every process.
    cache = tmp_path / "configs.pickle"
    _new_process(monkeypatch, cache)
    path = tmp_path / "cachetest_disk_data.py"
    path.write_text(textwrap.dedent("""
        from usd_chunks import chunk

        CATEGORY = "Data"


        def main():
            return chunk.Chunk("Data")


        def config():
            return chunk.DefaultConfig(category=CATEGORY)
    """))
    (mod,) = ChunkRegistry()._discover_module(path, "cachetest_disk_data")
    chunk_registry._CONFIG_CACHE.save()

    assert mod.config.category == "Data"
    assert not cache.exists() or str(path) not in chunk_registry._ConfigCache(str(cache))._load()


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
