"""A relative USD_CHUNK_DIRS entry (CI sets `chunks`) is a folder, not a path to re-read.

Chunk modules are found up front and loaded on first use. A module found under a relative
directory kept a relative file path, so one first used after the working directory changed
(a test's monkeypatch.chdir, a script's os.chdir) could not be loaded: making it returned no
chunk, and `scene.create.reference(...)` failed with "not enough values to unpack". Which
tests hit it depended on how pytest-xdist spread the files, so CI failed on some runs only.
"""
from __future__ import annotations

from usd_chunks import Scene, chunk_registry

_CHUNK = '''from __future__ import annotations

from usd_chunks import chunk


def main():
    return chunk.Chunk("Relative Dir Probe")


def execute(node):
    pass


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.EXAMPLES,
        description="A chunk a test finds through a relative folder.\\n\\nNothing else.",
    )
'''


def test_a_chunk_found_through_a_relative_dir_loads_after_a_chdir(tmp_path, monkeypatch):
    home, elsewhere = tmp_path / "home", tmp_path / "elsewhere"
    (home / "relchunks").mkdir(parents=True)
    elsewhere.mkdir()
    (home / "relchunks" / "relative_dir_probe.py").write_text(_CHUNK, encoding="utf-8")
    monkeypatch.chdir(home)
    monkeypatch.setenv("USD_CHUNK_DIRS", "relchunks")
    with Scene():  # discovers the chunk folders, as every Scene does
        module = chunk_registry.ChunkRegistry().get_chunk_module("relative_dir_probe")
        assert module is not None and not module.loaded
        # Loading checks module_file.exists() -- relative, that asks the working directory.
        assert module.module_file.is_absolute(), module.module_file

    monkeypatch.chdir(elsewhere)
    assert module.module_file.exists()
    with Scene() as scene:
        assert scene.create.relative_dir_probe() is not None


def test_registered_dirs_are_absolute(tmp_path, monkeypatch):
    (tmp_path / "relchunks2").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("USD_CHUNK_DIRS", "relchunks2")
    dirs = chunk_registry.ChunkRegistry().register_chunk_environ_directories()
    assert dirs and all(d.is_absolute() for d in dirs)
