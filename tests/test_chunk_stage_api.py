"""usd_chunks.chunk_stage is the API chunk scripts use to reach the graph on the stage.

Its names are the stable ones. The implementations mostly still live in
plugins.chunk_usd and are due to move into core; chunk scripts importing chunk_usd directly
would break when they do, so the scripts shipped here must not.
"""
from __future__ import annotations

import inspect
import pathlib
import re

import pytest

from usd_chunks import chunk_registry, chunk_stage
from usd_chunks.plugins import chunk_usd

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"

#: Not chunks: a debugging tool that reads chunk_usd's internals on purpose.
_INTERNAL_TOOLS = {"usd/test_value_clips/diagnose_redo.py"}

_PREDICATES = ["is_view_chunk", "is_group_chunk", "is_chunkgroup", "is_merge_chunk", "is_edit_target_chunk"]


def test_shipped_chunk_scripts_do_not_import_chunk_usd():
    offenders = []
    for path in SCRIPTS.rglob("*.py"):
        rel = path.relative_to(SCRIPTS).as_posix()
        if rel in _INTERNAL_TOOLS:
            continue
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        if re.search(r"^\s*(from usd_chunks\.plugins import .*\bchunk_usd\b|"
                     r"from usd_chunks\.plugins\.chunk_usd import)", text, re.M):
            offenders.append(rel)
    assert not offenders, f"use usd_chunks.chunk_stage instead of chunk_usd in: {offenders}"


@pytest.mark.parametrize("name", _PREDICATES)
def test_chunk_usd_still_answers_to_the_predicates(name):
    """Most of the codebase, and anything written against it before, reaches them there."""
    assert getattr(chunk_usd, name) is getattr(chunk_stage, name)


def test_every_forwarded_name_exists_in_chunk_usd():
    """A rename in chunk_usd must not leave a public name here pointing at nothing."""
    source = inspect.getsource(chunk_stage)
    forwarded = set(re.findall(r"_usd\(\)\.(\w+)", source))
    assert forwarded, "expected forwarding calls"
    missing = sorted(n for n in forwarded if not hasattr(chunk_usd, n))
    assert not missing


def _chunk(key):
    mod = chunk_registry.ChunkRegistry().get_chunk_module(key)
    return (mod.create() if mod.loaded else mod.load())[0]


@pytest.fixture
def registry(qapp):
    from usd_chunks.chunk_core import SceneSender

    class _Scene(SceneSender):
        def get_stage(self):
            return None

        def get_selected_prims(self):
            return []

    chunk_registry.ChunkRegistry().initialize(_Scene(), "pytest")


@pytest.mark.parametrize("key, true_for", [
    ("view", {"is_view_chunk"}),
    ("merge", {"is_merge_chunk"}),
    ("edit_target", {"is_edit_target_chunk"}),
    ("chunkgroup", {"is_chunkgroup", "is_group_chunk"}),
    ("prim", set()),
])
def test_predicates(registry, key, true_for):
    c = _chunk(key)
    assert {name for name in _PREDICATES if getattr(chunk_stage, name)(c)} == true_for


def test_a_chunk_says_when_it_is_being_deleted(registry):
    c = _chunk("prim")
    assert not c.is_deleting()
    c.request_delete()
    assert c.is_deleting()
