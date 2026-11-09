"""Export USD writes the files an exported layer uses relative to where it writes it.

A chunk authors a file it uses absolute -- a Sublayer anchors `../inputs/set.usda` to the
network's folder, a file browser gives an absolute path -- so a shot exported beside its
network named its set by where it sat on the machine that exported it (a benchmark agent
added a script to rewrite it). Relative Paths, on by default, writes it relative to the
exported file; off, it stays as authored.
"""
from __future__ import annotations

import pytest
from pxr import Sdf

from usd_chunks import Scene


@pytest.fixture
def project(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    layer = Sdf.Layer.CreateNew(str(inputs / "set.usda"))
    world = Sdf.CreatePrimInLayer(layer, "/World")
    world.specifier, world.typeName = Sdf.SpecifierDef, "Xform"
    layer.Save()
    (tmp_path / "shot").mkdir()
    return tmp_path


@pytest.mark.parametrize("relative", [True, False])
def test_a_shot_names_its_set_relative_to_itself(project, relative) -> None:
    shot = project / "shot"
    with Scene.new(path=str(shot / "shot_network.usda")) as scene:
        last = scene.create.layer(layer_name="shot")
        last = scene.create.edit_target(last, edit_target="shot")
        last = scene.create.sublayer(last, imports=["../inputs/set.usda"])
        last = scene.create.edit_target(last, edit_target="Session Layer")
        export = scene.create.export_usd(
            last, output_dir=str(shot), keep_session_layer=False, relative_paths=relative,
        )
        scene.set_output(export)
        scene.commit()

    written = Sdf.Layer.FindOrOpen(str(shot / "shot.usda"))
    written.Reload()
    paths = list(written.subLayerPaths)
    set_path = str((project / "inputs" / "set.usda").resolve())
    if relative:
        assert "../inputs/set.usda" in paths, paths
    else:
        assert any(Sdf.Layer.FindOrOpen(p) and Sdf.Layer.FindOrOpen(p).realPath and
                   Sdf.Layer.FindOrOpen(p).realPath.lower() == set_path.lower() for p in paths), paths
        assert "../inputs/set.usda" not in paths
