"""A chunk can't do anything its module doesn't define.

So a saved file is always the whole description of what the network does: a file records a
chunk's module by name (info:chunkType), and reopening it runs that module and nothing else.
Two halves hold that up, and each is tested here:

* **Hooks come from the registry.** execute, commit and the gizmo hooks are looked up in the
  registered module the chunk's config names -- never on something attached to the chunk,
  which a reopen would not bring back.
* **A chunk type lives in its module.** A Chunk subclass defined anywhere else -- a script,
  the interpreter, a test -- would work for this session and come back from the file without
  whatever it added, so it is refused on entering a scene. Plain framework chunks stay
  allowed: they have no behaviour but their module's, and callbacks a script wires onto one
  are session-only by design, like a script editor's.
"""
from __future__ import annotations

import pytest

from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_core import ChunkConfig, UnregisteredChunkTypeError
from usd_chunks.chunk_scene import SceneSender

from chunk_stubs import register_stub_module


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


class _InlineChunk(chunk.Chunk):
    """A chunk type defined outside any chunk module: what the invariant rules out."""

    def extra(self):
        return "not in any module"


@pytest.fixture
def scene():
    scene = _DummyScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    return scene


# ---------------------------------------------------------------------------
# hooks come from the registry
# ---------------------------------------------------------------------------

def test_hooks_are_the_registered_modules_functions(scene) -> None:
    def execute(node):
        pass

    stub = register_stub_module(execute=execute)
    c = chunk.Chunk("c")
    c._config = ChunkConfig(module=stub.module_name)

    assert c.get_chunk_module() is stub
    assert c._module_hook("execute") is execute
    assert c.can_cook()


def test_the_registry_is_read_every_time(scene) -> None:
    """Nothing is cached on the chunk: what the registry holds -- after a reload too -- is
    what runs."""
    stub = register_stub_module(execute=lambda node: "first")
    c = chunk.Chunk("c")
    c._config = ChunkConfig(module=stub.module_name)

    def second(node):
        return "second"

    stub.module.execute = second

    assert c._module_hook("execute") is second


def test_a_chunk_with_no_module_has_no_hooks(scene) -> None:
    c = chunk.Chunk("c")

    assert c.get_chunk_module() is None
    assert c._module_hook("execute") is None
    assert not c.can_cook()


def test_a_chunk_naming_an_unregistered_module_has_no_hooks(scene) -> None:
    c = chunk.Chunk("c")
    c._config = ChunkConfig(module="no_such_chunk_module")

    assert c.get_chunk_module() is None
    assert not c.can_cook()


def test_a_non_callable_module_attribute_is_not_a_hook(scene) -> None:
    stub = register_stub_module(commit="not a function")
    c = chunk.Chunk("c")
    c._config = ChunkConfig(module=stub.module_name)

    assert c._module_hook("commit") is None


# ---------------------------------------------------------------------------
# a chunk type lives in its module
# ---------------------------------------------------------------------------

def test_a_plain_chunk_is_accepted(scene) -> None:
    assert scene.add_chunk(chunk.Chunk("plain")) is not None


def test_a_chunk_built_by_its_registered_module_is_accepted(scene) -> None:
    module = chunk_registry.ChunkRegistry().get_chunk_module("subclassed")
    if module is None:
        pytest.skip("chunks/examples/subclassed.py is not registered in this environment")
    (c,) = module.load()

    assert type(c).__name__ == "MyCustomChunk"
    assert scene.add_chunk(c) is not None


def test_an_inline_chunk_class_is_refused(scene) -> None:
    with pytest.raises(UnregisteredChunkTypeError, match="registered chunk module"):
        scene.add_chunk(_InlineChunk("inline"))


def test_naming_a_real_module_does_not_launder_an_inline_class(scene) -> None:
    """The class still is not in the module the file would record, so a reopen would
    rebuild it as that module's own type, without what the inline class added."""
    c = _InlineChunk("inline")
    c._config = ChunkConfig(module=register_stub_module().module_name)

    with pytest.raises(UnregisteredChunkTypeError):
        scene.add_chunk(c)


def test_an_inline_class_nested_in_a_plain_chunk_is_refused(scene) -> None:
    outer = chunk.Chunk("outer")
    scene.add_chunk(outer)

    with pytest.raises(UnregisteredChunkTypeError):
        outer.add_param(_InlineChunk("inner"))
