"""`usdchunks show` on a chunk that can't load says why, instead of listing no params.

An agent asked the MCP to describe KITTI Writer and got its description with no params and a
log line "Failed to run module kitti_writer": the host's pxr lacked a module the chunk imports.
It couldn't tell a chunk that takes nothing from one that didn't load.
"""

from __future__ import annotations

import sys

import pytest

from usd_chunks import Scene, chunk_catalog, chunk_registry

_MODULE = '''
from usd_chunks import chunk
import no_such_package_for_this_test


def main():
    return chunk.Chunk("Needs A Package")


def config():
    return chunk.DefaultConfig(category="Test", requirements=["no_such_package_for_this_test>=1"],
                               description="Needs a package that isn't there.")
'''


@pytest.fixture
def module_file(tmp_path):
    path = tmp_path / "needs_a_package.py"
    path.write_text(_MODULE, encoding="utf-8")
    yield path
    sys.modules.pop(path.stem, None)


def test_a_module_that_fails_to_load_says_why(module_file, monkeypatch):
    monkeypatch.setenv("USD_CHUNK_AUTO_INSTALL", "0")
    module = chunk_registry.ChunkModule(module_name=module_file.stem, module_file=module_file)
    assert module.load() == []
    assert "no_such_package_for_this_test" in module.load_error


def test_show_says_the_params_cant_be_listed_and_what_it_needs(module_file, monkeypatch):
    monkeypatch.setenv("USD_CHUNK_AUTO_INSTALL", "0")
    with Scene():
        registry = chunk_registry.ChunkRegistry()
        registry.register_module(chunk_registry.ChunkModule(module_name=module_file.stem, module_file=module_file))
        try:
            entry, description, _, params = chunk_catalog.describe(registry, "needs_a_package")
        finally:
            # The registry is shared by every test in this process: leave no stranger in it.
            registry.registered_modules.pop(module_file.stem, None)
    assert params == []
    assert "Its params can't be listed here: the module didn't load" in description
    assert "no_such_package_for_this_test" in description and "It needs no_such_package_for_this_test>=1" in description


def test_a_chunk_that_loads_says_nothing_of_the_kind():
    with Scene():
        _, description, _, params = chunk_catalog.describe(chunk_registry.ChunkRegistry(), "transform")
    assert params and "can't be listed" not in description
