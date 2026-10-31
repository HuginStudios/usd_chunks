"""Finding a chunk module imports only the usd_chunks names its config() reads.

Discovery ran every `from usd_chunks import ...` of every chunk module to evaluate its
config(), so a scene with no Bake Textures still imported numpy and scipy for it, and one
without Display Modes still imported Qt -- two thirds of a second of every start, for
modules the scene never used. A config() that needs more than the names it reads (a
module-level constant built from an import) still gets every import, and the same config.
"""

from __future__ import annotations

import builtins
import sys

import pytest

from usd_chunks import chunk_registry

_READS_CHUNK_ONLY = '''
from usd_chunks import chunk, sim_bake
from usd_chunks import texture_bake


def main():
    return chunk.Chunk("Lazy")


def config():
    return chunk.DefaultConfig(category="Test", description="only chunk")
'''

_READS_A_CONSTANT = '''
from usd_chunks import chunk, chunk_naming

DESCRIPTION = "typed " + chunk_naming.CHUNK_PRIM_TYPENAME


def main():
    return chunk.Chunk("Deferred")


def config():
    return chunk.DefaultConfig(category="Test", description=DESCRIPTION)
'''


@pytest.fixture
def discover(tmp_path):
    made = []

    def _discover(name, source):
        path = tmp_path / f"{name}.py"
        path.write_text(source, encoding="utf-8")
        made.append(name)
        return chunk_registry.ChunkModule(module_name=name, module_file=path)

    yield _discover
    for name in made:
        sys.modules.pop(name, None)


@pytest.fixture
def imported_names(monkeypatch):
    """Every name a `from usd_chunks import ...` asked for while the test ran."""
    names = []
    real = builtins.__import__

    def recording(name, globs=None, locs=None, fromlist=(), level=0):
        if name.split(".")[0] == "usd_chunks":
            names.extend(fromlist or ())
        return real(name, globs, locs, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", recording)
    return names


def test_a_config_gets_only_the_names_it_reads(discover, imported_names) -> None:
    module = discover("lazy_imports_chunk", _READS_CHUNK_ONLY)
    assert module.config.description == "only chunk"
    assert "chunk" in imported_names
    assert "sim_bake" not in imported_names and "texture_bake" not in imported_names


def test_a_config_reading_a_module_constant_still_gets_every_import(discover) -> None:
    module = discover("deferred_imports_chunk", _READS_A_CONSTANT)
    assert module.config.description.startswith("typed ")
