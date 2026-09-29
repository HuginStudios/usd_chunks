"""ChunkDependencyManager registers its shutdown cache-write once per process.

initialize() runs again on every stage reload and on every ChunkRegistry().initialize().
An unguarded atexit.register() accumulated one handler per call, each rewriting the same
cache file at exit and each logging after the interpreter has closed its streams -- which
surfaces as a wall of "ValueError: I/O operation on closed file" *after* a test run reports
success. Harmless in itself, but it buries any real shutdown error in the noise.
"""
from __future__ import annotations

import atexit
import io
import logging
import sys

from usd_chunks import chunk_registry
from usd_chunks.chunk_scene import SceneSender


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


def test_the_shutdown_write_stays_quiet_when_the_stream_is_already_closed() -> None:
    """The condition atexit actually runs under.

    By the time the handler fires, pytest has closed the stream the root logger writes to.
    save_json's logger.debug then fails, and logging reports that failure to stderr as a full
    traceback -- printed *after* the run already said "45 passed", which reads like a crash.

    Reproduced here by pointing a handler at a genuinely closed stream, because the real
    thing only happens during interpreter shutdown and cannot be asserted on from inside a
    test.
    """
    manager = chunk_registry.ChunkDependencyManager()
    manager.initialize()
    if not manager.package_dir:
        return  # no dependency dir in this environment; nothing gets written

    # write_package_cache logs at DEBUG, and this logger sits at INFO by default -- so the
    # message is filtered out before it reaches any handler and nothing goes wrong. Raising
    # it here is what makes the test exercise the path at all; without it the test passes
    # whether the fix is present or not, which is worse than having no test.
    serial_logger = logging.getLogger("usd_chunks.chunk_cache")
    previous_level = serial_logger.level
    serial_logger.setLevel(logging.DEBUG)

    closed_stream = io.StringIO()
    closed_stream.close()
    handler = logging.StreamHandler(closed_stream)
    handler.setLevel(logging.DEBUG)
    serial_logger.addHandler(handler)

    captured = io.StringIO()
    real_stderr, sys.stderr = sys.stderr, captured
    try:
        manager._write_cache()
    finally:
        sys.stderr = real_stderr
        serial_logger.removeHandler(handler)
        serial_logger.setLevel(previous_level)

    assert captured.getvalue() == "", f"logging complained on shutdown:\n{captured.getvalue()}"


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
