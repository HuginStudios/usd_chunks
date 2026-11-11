"""Export USD never writes over the chunk network it is part of.

Opened as a file (`usdchunks run net.usda`), the network is the stage's root layer and so
one of the layers Export USD writes out. With Output Directory set to the network's folder,
that row's file was the network itself, and exporting replaced it with its chunk-less
result. An agent lost its network this way twice in one benchmark run.
"""
from __future__ import annotations

from pathlib import Path

from pxr import Sdf

from usd_chunks import Scene, chunk_naming


def _network(folder: Path) -> Path:
    path = folder / "net.usda"
    with Scene() as scene:
        ball = scene.create.prim(primpath_location="/World/ball", prim_type="Sphere")
        scene.set_output(scene.create.export_usd(ball, output_dir=str(folder), keep_session_layer=False))
        scene.save(str(path))
    return path


def test_exporting_into_the_networks_folder_keeps_the_network(tmp_path) -> None:
    net = _network(tmp_path)
    with Scene.open(str(net)) as scene:
        scene.commit()

    layer = Sdf.Layer.OpenAsAnonymous(str(net))
    assert layer.GetPrimAtPath(chunk_naming.CHUNKS_ROOT), "the export replaced the network file"
    with Scene.open(str(net)) as scene:
        assert any(c.name for c in scene.get_chunks()), "the network no longer opens as chunks"


def test_committing_right_after_saving_keeps_the_network(tmp_path, monkeypatch, capsys) -> None:
    # Built in memory, with Scene.new(path=...): the network is not a layer of the stage,
    # but the root layer is named after its file, and so was that layer's export -- as a
    # benchmark agent's script did it, with paths relative to its folder.
    monkeypatch.chdir(tmp_path)
    with Scene.new(path="shot_network.usda") as scene:
        last = scene.create.stage_settings(meters_per_unit=1.0)
        last = scene.create.layer(last, layer_name="shot")
        last = scene.create.edit_target(last, edit_target="shot")
        last = scene.create.prim(last, primpath_location="/World/ball", prim_type="Sphere")
        last = scene.create.edit_target(last, edit_target="Session Layer")
        scene.set_output(scene.create.export_usd(last, output_dir=".", keep_session_layer=False))
        scene.save()
        saved = (tmp_path / "shot_network.usda").read_bytes()
        scene.commit()

    assert "Not exporting layer 'shot_network.usda'" in capsys.readouterr().out
    assert (tmp_path / "shot_network.usda").read_bytes() == saved, "the export replaced the network file"


def test_an_export_writes_over_its_own_earlier_files(tmp_path, capsys) -> None:
    # Unbaked, the session layer carries the network's bookkeeping under /chunks, so the
    # guard took the file Export USD had written for a network and refused it the next time.
    # A benchmark agent wrote empty stub files over its outputs to get past it.
    with Scene.new(path=str(tmp_path / "net.usda")) as scene:
        last = scene.create.layer(layer_name="shot")
        last = scene.create.edit_target(last, edit_target="shot")
        last = scene.create.prim(last, primpath_location="/World/ball", prim_type="Sphere")
        last = scene.create.edit_target(last, edit_target="Session Layer")
        export = scene.create.export_usd(last, output_dir=".", session_file_name="shot")
        scene.set_output(export)
        scene.save()
        scene.commit()
        assert Sdf.Layer.OpenAsAnonymous(str(tmp_path / "shot.usda")).GetPrimAtPath(chunk_naming.CHUNKS_ROOT)
        capsys.readouterr()
        scene.set(export, layer_format=".usda")  # a change, so the export runs again
        scene.commit()

    assert "Not exporting layer 'SessionLayer'" not in capsys.readouterr().out
    assert Sdf.Layer.OpenAsAnonymous(str(tmp_path / "net.usda")).GetPrimAtPath(chunk_naming.CHUNKS_ROOT), \
        "the network itself is still protected"
