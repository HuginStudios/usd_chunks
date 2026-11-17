"""The Blender add-on registers extra Hydra delegates from the folders it is given.

`usd_chunks.frontends.blender.delegates` has no `bpy`, so this runs outside Blender. The
registry is a stand-in: registering a fake delegate with USD's real one would last for the
rest of the test process and show up in every later renderer list.
"""
from __future__ import annotations

import os

import pytest
from pxr import Gf, Sdf, Usd, UsdRender

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


def _stage_with_settings(*paths):
    stage = Usd.Stage.CreateInMemory()
    for path in paths:
        UsdRender.Settings.Define(stage, path)
    return stage


def test_the_live_view_renders_with_the_settings_the_stage_names():
    stage = _stage_with_settings("/Render/finals", "/Render/preview")
    stage.SetMetadata("renderSettingsPrimPath", "/Render/preview")

    assert delegates.render_settings_path(stage) == "/Render/preview"


def test_without_a_name_the_first_settings_under_render_are_used():
    stage = _stage_with_settings("/Render/finals")

    assert delegates.render_settings_path(stage) == "/Render/finals"


def test_a_stage_without_render_settings_gives_an_empty_path():
    stage = Usd.Stage.CreateInMemory()
    stage.DefinePrim("/Render/notSettings", "Scope")

    assert delegates.render_settings_path(stage).isEmpty


def test_a_delegates_own_settings_are_the_custom_namespaced_attributes():
    stage = _stage_with_settings("/Render/finals")
    prim = stage.GetPrimAtPath("/Render/finals")
    prim.CreateAttribute("aurora:max_samples", Sdf.ValueTypeNames.Int, custom=True).Set(64)
    prim.CreateAttribute("aurora:is_denoising_enabled", Sdf.ValueTypeNames.Bool, custom=True).Set(True)
    prim.CreateAttribute("aurora:min_samples", Sdf.ValueTypeNames.Int, custom=True)  # no value
    UsdRender.Settings(prim).CreateResolutionAttr(Gf.Vec2i(1920, 1080))  # schema: Hydra's business

    assert delegates.renderer_settings(prim) == {
        "aurora:max_samples": 64, "aurora:is_denoising_enabled": True}


def test_no_prim_has_no_settings():
    stage = Usd.Stage.CreateInMemory()
    assert delegates.renderer_settings(stage.GetPrimAtPath("/Render/none")) == {}
