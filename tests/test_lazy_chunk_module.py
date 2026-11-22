"""A discovered chunk module sits in sys.modules as a lazy module, run on first use. Reading what any module has
before it runs -- its __file__, __name__, __spec__ -- must not run it: libraries walk sys.modules for exactly that
(torch's import calls inspect.getmodule, which reads every module's __file__), and a chunk whose own dependency
is missing then failed their import. Anything else still loads the chunk."""
from __future__ import annotations

import inspect
import sys

import pytest

from usd_chunks import chunk_registry

SOURCE = '''from __future__ import annotations

import a_package_that_is_not_installed  # noqa: F401

from usd_chunks import chunk


def main():
    return chunk.Chunk("Lazy Broken")


def config():
    return chunk.DefaultConfig(description="Needs a package that is not there.")
'''


@pytest.fixture
def lazy_module(tmp_path):
    name = "lazy_broken_chunk_for_test"
    path = tmp_path / (name + ".py")
    path.write_text(SOURCE, encoding="utf-8")
    chunk_registry.ChunkModule(module_name=name, module_file=path)
    try:
        yield name, path
    finally:
        sys.modules.pop(name, None)


def test_reading_its_file_and_name_does_not_run_it(lazy_module):
    name, path = lazy_module
    module = sys.modules[name]
    assert module.__file__ == str(path)
    assert module.__name__ == name
    assert module.__spec__.name == name
    assert isinstance(module, type(sys)) and not isinstance(module, type)      # what torch's import asks next
    assert "main" not in module.__dict__                                       # still not run


def test_a_library_walking_sys_modules_is_not_broken_by_it(lazy_module):
    assert inspect.getmodule(test_a_library_walking_sys_modules_is_not_broken_by_it) is sys.modules[__name__]


def test_anything_else_still_runs_it(lazy_module):
    name, _ = lazy_module
    with pytest.raises(ModuleNotFoundError):
        sys.modules[name].main  # noqa: B018 - the load is the point
