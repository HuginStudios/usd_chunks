"""Registered stand-in chunk modules, for tests that need a chunk to run hooks of their own.

A chunk's hooks -- execute, commit, the gizmo hooks -- come only from the registered module
its config names (ChunkBase.get_chunk_module), never from anything attached to the chunk.
So a test that wants a chunk to count its cooks, or a gizmo hook that raises, cannot hang a
fake module on the chunk: it registers a real one, the way a chunk directory would, and
points the chunk's config at it. The module's file is a minimal main()/config() pair; the
hooks a test passes are set on the module the registry loaded from it, so they may close
over the test's locals.

Each stub gets a name no chunk directory can collide with, and conftest's
unregister_stub_modules fixture drops every stub after each test, so no test sees another's.
"""
from __future__ import annotations

import sys
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import ChunkBase, ChunkConfig

_SOURCE = """from usd_chunks import chunk


def main():
    return chunk.Chunk({name!r})


def config():
    return chunk.DefaultConfig()
"""

# One folder per test process, removed when the process exits.
_DIR = tempfile.TemporaryDirectory(prefix="usd_chunks_stubs_")
_registered: list[str] = []


def register_stub_module(**hooks: Callable) -> chunk_registry.ChunkModule:
    """Register a new chunk module with the given hooks and return it.

    Args:
        **hooks: Module-level functions to define, by hook name, e.g. execute=fn. Each is
            called the way the framework calls a module's own: execute(node),
            commit(node), gizmo_changed(node, gizmo, value), ... main=fn replaces the
            stub's main(), for a test that reloads a scene and needs the chunk rebuilt with
            the params it had; the default builds a bare Chunk.

    Returns:
        The registered ChunkModule; its module_name is what a chunk's config names.
    """
    name = f"_stub_{uuid.uuid4().hex}"
    path = Path(_DIR.name) / f"{name}.py"
    path.write_text(_SOURCE.format(name=name), encoding="utf-8")
    (module,) = chunk_registry.ChunkModule.from_chunk_definition(path)
    registered = chunk_registry.ChunkRegistry().register_module(module)
    assert registered is module, f"stub chunk module {name} was not registered"
    for hook_name, fn in hooks.items():
        setattr(module.module, hook_name, fn)
    _registered.append(name)
    return module


def attach_stub_module(c: ChunkBase, **hooks: Callable) -> chunk_registry.ChunkModule:
    """Register a stub module (see register_stub_module) and make it c's module.

    Keeps the rest of c's config (gizmos, exec inputs, ...) and changes only the module it
    names, so a test's chunk behaves as before apart from where its hooks come from.

    Returns:
        The registered ChunkModule.
    """
    module = register_stub_module(**hooks)
    if (cfg := c._config) is not None:
        cfg.module = module.module_name
    else:
        c._config = ChunkConfig(module=module.module_name)
    return module


def unregister_stub_modules() -> None:
    """Drop every stub registered so far from the registry and from sys.modules."""
    registry = chunk_registry.ChunkRegistry()
    while _registered:
        name = _registered.pop()
        registry.registered_modules.pop(name, None)
        sys.modules.pop(name, None)
