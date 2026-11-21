"""The Blender add-on adds chunk folders to USD_CHUNK_DIRS from its preference and menu.

`usd_chunks.frontends.blender.chunk_dirs` has no `bpy`, so this runs outside Blender. The
registry is a stand-in and the environment a dict, so nothing leaks into later tests.
"""
from __future__ import annotations

import os
from pathlib import Path

from usd_chunks.frontends.blender import chunk_dirs


class _Registry:
    def __init__(self, initialized=True):
        self.initialized = initialized
        self.directories = []
        self.scanned = []

    def register_directory(self, path):
        self.directories.append(path)

    def register_chunk_modules(self, paths):
        self.scanned.append(list(paths))


def test_a_folder_goes_on_the_end_of_usd_chunk_dirs(tmp_path):
    first, added = tmp_path / "studio", tmp_path / "chunk_bank"
    first.mkdir()
    added.mkdir()
    env = {chunk_dirs.ENV_VAR: str(first)}
    assert chunk_dirs.add_folders([str(added)], _Registry(), env) == [str(added)]
    # After the environment's folders, so they keep winning on a duplicate chunk name.
    assert env[chunk_dirs.ENV_VAR].split(os.pathsep) == [str(first), str(added)]


def test_a_running_registry_scans_the_new_folder(tmp_path):
    registry = _Registry()
    chunk_dirs.add_folders([str(tmp_path)], registry, {})
    assert registry.directories == [str(tmp_path)]
    assert registry.scanned == [[Path(tmp_path)]]


def test_a_registry_not_started_yet_finds_it_through_the_environment(tmp_path):
    registry, env = _Registry(initialized=False), {}
    chunk_dirs.add_folders([str(tmp_path)], registry, env)
    assert registry.scanned == []
    assert env[chunk_dirs.ENV_VAR] == str(tmp_path)


def test_adding_again_or_a_missing_folder_does_nothing(tmp_path):
    registry, env = _Registry(), {chunk_dirs.ENV_VAR: str(tmp_path)}
    assert chunk_dirs.add_folders([str(tmp_path), str(tmp_path / "gone")], registry, env) == []
    assert registry.scanned == []
    assert env[chunk_dirs.ENV_VAR] == str(tmp_path)


def test_join_folder_appends_once(tmp_path):
    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    assert chunk_dirs.join_folder("", a) == a
    assert chunk_dirs.join_folder(a, b) == f"{a};{b}"
    assert chunk_dirs.join_folder(f"{a};{b}", a + os.sep) == f"{a};{b}"
