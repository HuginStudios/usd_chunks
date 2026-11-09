"""Relative file paths in params resolve from the network's file, as they would in USD.

A chunk authors into its own anonymous layer, so a relative path there was found from the
working directory: a network worked only when run from the folder its paths were written
for (a benchmark agent ended up with os.chdir in its build script). Now:

- a search path (no ./ or ../) is found in the network's folder, by the resolver context a
  stage opened from a file has -- and Scene.new(path=...) gives an unsaved one;
- a ./ or ../ path is anchored to the network's folder, as in any USD file;
- saving writes absolute paths under the network's folder relative to it (Stage Settings'
  Relative File Paths), and re-anchors relative ones when saving elsewhere;
- an absolute path, a missing file and an unsaved ./ path are each warned about once.
"""
from __future__ import annotations

import logging
import os

import pytest
from pxr import Sdf

from usd_chunks import Scene, asset_paths


@pytest.fixture
def project(tmp_path, monkeypatch):
    """proj/inputs/set.usda (/World/chair), and a working directory that is not proj."""
    inputs = tmp_path / "proj" / "inputs"
    inputs.mkdir(parents=True)
    layer = Sdf.Layer.CreateNew(str(inputs / "set.usda"))
    world = Sdf.CreatePrimInLayer(layer, "/World")
    world.specifier, world.typeName = Sdf.SpecifierDef, "Xform"
    Sdf.CreatePrimInLayer(layer, "/World/chair").specifier = Sdf.SpecifierDef
    layer.defaultPrim = "World"
    layer.Save()
    (tmp_path / "proj" / "shot").mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    asset_paths._warned.clear()
    return tmp_path / "proj"


def _saved(path, attr="file_path"):
    """The value a saved network holds for its Reference's attr."""
    layer = Sdf.Layer.FindOrOpen(str(path))
    layer.Reload()
    found = []
    layer.Traverse(
        Sdf.Path.absoluteRootPath,
        lambda p: found.append(layer.GetAttributeAtPath(p).default)
        if p.IsPropertyPath() and p.name == f"inputs:{attr}" else None,
    )
    return found[0] if found else None


def _reopened_has_chair(net) -> bool:
    with Scene.open(str(net)) as scene:
        return bool(scene.get_stage().GetPrimAtPath("/ref/chair"))


def test_a_search_path_is_found_from_the_network_folder(project) -> None:
    net = project / "net.usda"
    with Scene.new(path=str(net)) as scene:
        ref = scene.create.reference(file_path="inputs/set.usda", primpath_location="/ref")
        scene.set_output(ref)
        assert scene.get_stage().GetPrimAtPath("/ref/chair"), "not found while building"
        scene.save()

    assert _saved(net) == "inputs/set.usda"
    assert _reopened_has_chair(net)


def test_a_dot_path_is_relative_to_the_network_file(project) -> None:
    net = project / "shot" / "net.usda"
    with Scene.new(path=str(net)) as scene:
        ref = scene.create.reference(file_path="../inputs/set.usda", primpath_location="/ref")
        sub = scene.create.sublayer(ref)
        sub.get_param("imports").size = 1
        scene.set(sub, **{"imports.0.sublayer_path": "../inputs/set.usda"})
        scene.set_output(sub)
        stage = scene.get_stage()
        assert stage.GetPrimAtPath("/ref/chair") and stage.GetPrimAtPath("/World/chair")
        scene.save()

    assert _saved(net) == "../inputs/set.usda"
    assert _reopened_has_chair(net)


@pytest.mark.parametrize("how", ["default", "argument", "stage_settings"])
def test_save_writes_paths_under_the_folder_relative(project, how) -> None:
    net = project / "net.usda"
    absolute = str(project / "inputs" / "set.usda")
    with Scene.new(path=str(net)) as scene:
        last = scene.create.reference(file_path=absolute, primpath_location="/ref")
        if how == "stage_settings":
            last = scene.create.stage_settings(last, relative_paths=False)
        scene.set_output(last)
        scene.save(relative_paths=False if how == "argument" else None)

    assert _saved(net) == ("inputs/set.usda" if how == "default" else absolute)


def test_saving_elsewhere_keeps_naming_the_same_file(project) -> None:
    with Scene.new(path=str(project / "net.usda")) as scene:
        scene.set_output(scene.create.reference(file_path="inputs/set.usda", primpath_location="/ref"))
        moved = project.parent / "net.usda"
        scene.save(str(moved))

    assert _saved(moved) == "proj/inputs/set.usda"
    assert _reopened_has_chair(moved)


def test_problems_are_warned_once(project, tmp_path, caplog) -> None:
    outside = tmp_path / "outside.usda"
    Sdf.Layer.CreateNew(str(outside)).Save()
    caplog.set_level(logging.WARNING, logger="usd_chunks.asset_paths")
    with Scene.new(path=str(project / "net.usda")) as scene:
        a = scene.create.reference(file_path=str(outside), primpath_location="/a")
        # A Reference refuses a file that is not there; a Sublayer row takes it.
        b = scene.create.sublayer(a)
        b.get_param("imports").size = 1
        scene.set(b, **{"imports.0.sublayer_path": "inputs/nope.usda"})
        scene.set_output(b)
        scene.set(a, primpath_location="/a2")  # a recook: no second warning
    text = caplog.text
    assert text.count("is an absolute path") == 1
    assert "inputs/nope.usda was not found. Looked in" in text and str(project) in text


def test_an_unsaved_dot_path_is_warned_about(project, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="usd_chunks.asset_paths")
    with Scene.new() as scene:
        scene.set_output(scene.create.reference(file_path="../proj/inputs/set.usda", primpath_location="/ref"))
        assert scene.get_stage().GetPrimAtPath("/ref/chair"), "found from the working directory"
    assert "not saved yet" in caplog.text


def test_a_chunk_reading_its_file_itself_finds_it_the_same_way(project) -> None:
    from usd_chunks import chunk

    with Scene.new(path=str(project / "shot" / "net.usda")) as scene:
        ref = scene.create.reference(file_path="../inputs/set.usda", primpath_location="/ref")
        found = chunk.resolve_file(ref, "../inputs/set.usda")
    assert os.path.samefile(found, project / "inputs" / "set.usda")
