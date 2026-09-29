"""Reading a chunk module's source costs what it costs once, not twice or quadratically.

Finding a scene's chunk modules parses each file and runs ``_ConfigTransformer`` over it,
which is most of what a process pays before its first chunk exists. Two things made that
seconds rather than a fraction of one: every node's source was sliced out with
``ast.get_source_segment``, which splits the whole file into lines on every call, and
every module was parsed a second time when its first chunk loaded, into a module object
that was then thrown away.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from usd_chunks import chunk_registry

REPO = Path(__file__).resolve().parent.parent

_MODULE = '''
from usd_chunks import chunk


def main():
    return chunk.Chunk("Read Once")


def config():
    return chunk.DefaultConfig(category="Test")
'''


@pytest.fixture
def module_file(tmp_path):
    path = tmp_path / "read_once_chunk.py"
    path.write_text(_MODULE, encoding="utf-8")
    yield path
    sys.modules.pop(path.stem, None)


@pytest.fixture
def reads(monkeypatch):
    """Count every parse of a chunk module's source."""
    calls = []
    real = chunk_registry._pre_load_module

    def counting(source, globs, module):
        calls.append(module.module_name)
        return real(source, globs, module)

    monkeypatch.setattr(chunk_registry, "_pre_load_module", counting)
    return calls


def test_loading_a_found_module_does_not_read_it_again(module_file, reads):
    module = chunk_registry.ChunkModule(module_name=module_file.stem, module_file=module_file)
    assert len(reads) == 1

    assert module._pre_load()
    assert len(reads) == 1, "the source was already read when the module was found"
    assert module.func is not None


def test_reload_chunks_still_reads_the_edited_file(module_file, reads):
    module = chunk_registry.ChunkModule(module_name=module_file.stem, module_file=module_file)
    module._pre_load()
    module.loaded = True

    module.unload()  # what Reload Chunks does before loading again
    module_file.write_text(_MODULE.replace('"Test"', '"Edited"'), encoding="utf-8")
    assert module._pre_load()

    assert len(reads) == 2
    assert module.config.category == "Edited"


#: Every line ending the parser accepts, a form feed it does not split at, and multi-byte
#: characters ahead of a node on its line -- column offsets are UTF-8 bytes.
_EDGE_CASES = [
    pytest.param("x = 1\r\ny = 'é'\rz = (1,\n 2)\n\x0cw = 3", id="line-endings"),
    pytest.param("a = 'ü' + 'x'\n\n\nb = [\n 1]", id="multibyte"),
]


@pytest.mark.parametrize("source", _EDGE_CASES)
def test_node_source_matches_ast_get_source_segment(source):
    transformer = chunk_registry._ConfigTransformer(source, {}, None)
    for node in ast.walk(ast.parse(source)):
        if hasattr(node, "lineno"):
            assert transformer._get_node_source(node) == ast.get_source_segment(source, node)


@pytest.mark.parametrize(
    "path",
    [pytest.param(p, id=p.relative_to(REPO).as_posix()) for p in sorted((REPO / "chunks").rglob("*.py"))],
)
def test_every_chunk_statement_slices_as_ast_does(path):
    """Statements only, and the first few hundred: what the transformer slices out is
    imports, config() and main(), and the reference is the quadratic thing being avoided."""
    source = path.read_text(encoding="utf-8-sig")
    transformer = chunk_registry._ConfigTransformer(source, {}, None)
    statements = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.stmt)][:300]
    for node in statements:
        assert transformer._get_node_source(node) == ast.get_source_segment(source, node), node.lineno
