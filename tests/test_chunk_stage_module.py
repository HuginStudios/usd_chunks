"""chunk_stage is core: the chunk graph on the stage, moved out of plugins/chunk_usd.

The old import path keeps working for code written before the move, and nothing in this
repository uses it any more.
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

import pytest

from usd_chunks import chunk_registry, chunk_stage

REPO = pathlib.Path(__file__).resolve().parent.parent

#: The compatibility shim itself.
_SHIM = "usd_chunks/plugins/chunk_usd.py"

_PREDICATES = ["is_view_chunk", "is_group_chunk", "is_chunkgroup", "is_merge_chunk", "is_edit_target_chunk"]


def test_the_old_import_path_is_the_same_module():
    """The same object, not a copy, so a patch through either name reaches both."""
    from usd_chunks.plugins import chunk_usd
    import usd_chunks.plugins.chunk_usd as dotted

    assert chunk_usd is chunk_stage
    assert dotted is chunk_stage


def test_nothing_here_imports_the_old_path():
    offenders = []
    for folder in ("usd_chunks", "scripts", "tests"):
        for path in (REPO / folder).rglob("*.py"):
            rel = path.relative_to(REPO).as_posix()
            if rel == _SHIM or rel == "tests/test_chunk_stage_module.py":
                continue
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            if re.search(r"usd_chunks\.plugins\.chunk_usd\b|from usd_chunks\.plugins import .*\bchunk_usd\b", text):
                offenders.append(rel)
    assert not offenders, f"import usd_chunks.chunk_stage instead in: {offenders}"


def test_importing_core_loads_no_plugin():
    """Checked in a fresh interpreter: this one has long since imported every plugin."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO), os.environ.get("PYTHONPATH")]))
    code = ("import sys, usd_chunks.chunk_stage; "
            "print(sorted(m for m in sys.modules "
            "if m.startswith(('usd_chunks.plugins', 'usd_chunks.frontends', 'usd_chunks.hosts'))))")
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(REPO), env=env,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]"


@pytest.fixture
def registry(qapp):
    from usd_chunks.chunk_core import SceneSender

    class _Scene(SceneSender):
        def get_stage(self):
            return None

        def get_selected_prims(self):
            return []

    chunk_registry.ChunkRegistry().initialize(_Scene(), "pytest")


def _chunk(key):
    mod = chunk_registry.ChunkRegistry().get_chunk_module(key)
    return (mod.create() if mod.loaded else mod.load())[0]


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
