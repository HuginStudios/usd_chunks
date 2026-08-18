"""ChunkDependencyManager registers its shutdown cache-write once per process.

initialize() runs again on every stage reload and on every ChunkRegistry().initialize().
An unguarded atexit.register() accumulated one handler per call, each rewriting the same
cache file at exit and each logging after the interpreter has closed its streams -- which
surfaces as a wall of "ValueError: I/O operation on closed file" *after* a test run reports
success. Harmless in itself, but it buries any real shutdown error in the noise.
"""
from __future__ import annotations

import atexit

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def test_repeated_initialize_registers_one_handler() -> None:
    manager = chunk_registry.ChunkDependencyManager()
    manager.initialize()

    before = atexit._ncallbacks()
    for _ in range(5):
        manager.initialize()

    assert atexit._ncallbacks() == before


def test_repeated_registry_initialize_does_not_accumulate_handlers() -> None:
    """The path the test suite itself takes: a fixture pointing the registry at a fresh
    scene per test used to leak a handler each time."""
    chunk_registry.ChunkRegistry().initialize(_DummyScene(), "pytest")

    before = atexit._ncallbacks()
    for _ in range(3):
        chunk_registry.ChunkRegistry().initialize(_DummyScene(), "pytest")

    assert atexit._ncallbacks() == before


def test_the_cache_write_is_still_registered() -> None:
    """The guard must not switch the handler off altogether -- the cache exists to keep
    dependency discovery from re-running on every launch."""
    manager = chunk_registry.ChunkDependencyManager()
    manager.initialize()

    if not manager.package_dir:
        return  # no dependency dir configured in this environment; nothing to register

    atexit.unregister(manager._write_cache)
    before = atexit._ncallbacks()
    atexit.register(manager._write_cache)

    assert atexit._ncallbacks() == before + 1
