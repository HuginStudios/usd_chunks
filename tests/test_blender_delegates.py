"""The Blender add-on registers extra Hydra delegates from the folders it is given.

`usd_chunks.frontends.blender.delegates` has no `bpy`, so this runs outside Blender. The
registry is a stand-in: registering a fake delegate with USD's real one would last for the
rest of the test process and show up in every later renderer list.
"""
from __future__ import annotations

import os

import pytest

from usd_chunks.frontends.blender import delegates


class _Registry:
    def __init__(self):
        self.calls = []

    def RegisterPlugins(self, paths):  # noqa: N802 - USD's name
        self.calls.append(list(paths))


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(delegates, "_registered", set())
    monkeypatch.setattr(delegates, "_dll_handles", [])
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    if hasattr(os, "add_dll_directory"):
        monkeypatch.setattr(os, "add_dll_directory", lambda path: path)


def _delegate(root, nested=True):
    info = root / "resources" if nested else root
    info.mkdir(parents=True)
    (info / "plugInfo.json").write_text('{"Plugins": []}')
    return root


def test_a_folder_with_resources_registers_its_plug_info(tmp_path):
    folder = _delegate(tmp_path / "hdAurora")
    reg = _Registry()

    infos = delegates.register_folders([str(folder)], reg)

    assert infos == [str((folder / "resources").resolve())]
    assert reg.calls == [infos]


def test_the_library_folder_goes_on_the_dll_search_path(tmp_path):
    folder = _delegate(tmp_path / "hdAurora")

    delegates.register_folders([str(folder)], _Registry())

    path = os.environ["PATH"].split(os.pathsep)
    assert str(folder) in path
    assert str(folder / "resources") in path


def test_a_folder_holding_plug_info_itself_works_too(tmp_path):
    folder = _delegate(tmp_path / "hdThing", nested=False)

    assert delegates.register_folders([str(folder)], _Registry()) == [str(folder.resolve())]


def test_registering_twice_does_nothing_the_second_time(tmp_path):
    folder = _delegate(tmp_path / "hdAurora")
    reg = _Registry()

    delegates.register_folders([str(folder)], reg)
    assert delegates.register_folders([str(folder)], reg) == []
    assert len(reg.calls) == 1


def test_a_folder_without_plug_info_is_skipped_with_a_warning(tmp_path, caplog):
    empty = tmp_path / "nothing"
    empty.mkdir()
    reg = _Registry()

    assert delegates.register_folders([str(empty), str(tmp_path / "missing")], reg) == []
    assert reg.calls == []
    assert "no plugInfo.json" in caplog.text


def test_folders_split_on_semicolons_and_the_path_separator():
    text = 'C:\\a;  "C:\\b" ;' + os.pathsep + "C:\\c;;"
    assert delegates.split_folders(text) == ["C:\\a", "C:\\b", "C:\\c"]
    assert delegates.split_folders("") == []
