"""A param edit that would land in a locked file is refused; cooking ignores locks.

A pipeline locks a published department file by making it read-only, or by turning a
layer's edit permission off when it opens it. Cooking only reads such a file, so a shot
over a locked layout cooks as usual. Editing a layout chunk's param is an edit: it lands
wherever the stage's edit target sends it, and if that is the locked file, it is refused
and the param goes back to what USD composes.
"""

from __future__ import annotations

import os
import stat

import pytest
from pxr import Sdf

from network_files import run_and_read, save_cubes, save_network
from usd_chunks import chunk_stage, chunk_utils
from usd_chunks.chunk_script import Scene


@pytest.fixture
def locked_shot(tmp_path):
    save_cubes(tmp_path / "set.usda", "a")
    paths = save_network(tmp_path / "layout.usda", {"A1": ("/a", 1.0)}, ends=("output",))
    shot = Sdf.Layer.CreateNew(str(tmp_path / "shot.usda"))
    shot.subLayerPaths[:] = ["./layout.usda", "./set.usda"]
    shot.Save()
    layout_file = tmp_path / "layout.usda"
    os.chmod(layout_file, stat.S_IREAD)
    scene = Scene.open(str(tmp_path / "shot.usda"), "pytest")
    stage = scene.get_stage()
    a1 = next(c for c in scene.get_chunks() if chunk_stage.get_chunk_prim_path(c) == paths["A1"])
    layout = Sdf.Layer.Find(str(layout_file))
    try:
        yield stage, a1, layout
    finally:
        scene.close()
        os.chmod(layout_file, stat.S_IREAD | stat.S_IWRITE)


def test_a_read_only_file_is_locked(locked_shot):
    _, _, layout = locked_shot
    assert chunk_utils.is_layer_locked(layout)


def test_an_edit_into_the_shot_is_allowed(locked_shot):
    """The shot is the edit target: the edit is an override there, and layout is untouched."""
    stage, a1, layout = locked_shot
    stage.SetEditTarget(stage.GetRootLayer())
    a1.get_param("tz").value = 5.0
    assert a1.get_param("tz").value == pytest.approx(5.0)
    assert not layout.dirty


def test_an_edit_into_the_locked_file_is_refused(locked_shot):
    stage, a1, layout = locked_shot
    stage.SetEditTarget(layout)
    a1.get_param("tz").value = 5.0
    assert a1.get_param("tz").value == pytest.approx(1.0), "the param went back to the file's value"
    assert not layout.dirty


def test_edit_permission_off_locks_a_writable_file(tmp_path):
    layer = Sdf.Layer.CreateNew(str(tmp_path / "open.usda"))
    assert not chunk_utils.is_layer_locked(layer)
    layer.SetPermissionToEdit(False)
    assert chunk_utils.is_layer_locked(layer)


def test_a_shot_over_a_locked_file_still_cooks(tmp_path):
    """Cooking only reads the locked layout."""
    save_cubes(tmp_path / "set.usda", "a")
    save_network(tmp_path / "layout.usda", {"A1": ("/a", 1.0)}, ends=("output",))
    shot = Sdf.Layer.CreateNew(str(tmp_path / "shot.usda"))
    shot.subLayerPaths[:] = ["./layout.usda", "./set.usda"]
    shot.Save()
    os.chmod(tmp_path / "layout.usda", stat.S_IREAD)
    try:
        assert run_and_read(tmp_path / "shot.usda", "a")["a"] == pytest.approx(1.0)
    finally:
        os.chmod(tmp_path / "layout.usda", stat.S_IREAD | stat.S_IWRITE)
