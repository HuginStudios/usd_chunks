"""Payload's Load and Sublayer's Load Payloads checkboxes unload payloads for a light stage.

An asset keeps its proxy outside the payload and its render mesh inside. Unloading the
payloads leaves only the proxies: the fast stage. Load state is the stage's, so a checked
box only loads back what the chunk itself unloaded, never one unloaded by hand.
"""

from __future__ import annotations

from pxr import Sdf

from usd_chunks import Scene, chunk


def _file(path, prims):
    layer = Sdf.Layer.CreateNew(str(path))
    for prim_path in prims:
        Sdf.CreatePrimInLayer(layer, prim_path).specifier = Sdf.SpecifierDef
    layer.Save()
    return str(path).replace("\\", "/")


def test_payload_load_checkbox(tmp_path) -> None:
    geo = _file(tmp_path / "geo.usda", ["/ball", "/ball/render"])
    with Scene() as scene:
        payload = scene.create.payload(file_path=geo, primpath_location="/ball")
        stage = scene.get_stage()
        assert stage.GetPrimAtPath("/ball/render").IsValid()

        scene.set(payload, load=False)
        assert not stage.GetPrimAtPath("/ball/render").IsValid(), "unloaded"
        scene.set(payload, load=True)
        assert stage.GetPrimAtPath("/ball/render").IsValid(), "loaded back"


def test_a_checked_box_leaves_a_manual_unload_alone(tmp_path) -> None:
    geo = _file(tmp_path / "geo.usda", ["/ball", "/ball/render"])
    with Scene() as scene:
        payload = scene.create.payload(file_path=geo, primpath_location="/ball")
        stage = scene.get_stage()
        stage.Unload("/ball")
        scene.set(payload, import_primpath="/ball")  # cooks the chunk again
        assert not stage.GetPrimAtPath("/ball/render").IsValid()


def test_sublayer_load_payloads_unloads_what_the_files_bring_in(tmp_path) -> None:
    geo = _file(tmp_path / "geo.usda", ["/ball", "/ball/render"])
    shot_layer = Sdf.Layer.CreateNew(str(tmp_path / "shot.usda"))
    ball = Sdf.CreatePrimInLayer(shot_layer, "/World/ball")
    ball.specifier = Sdf.SpecifierDef
    shot_layer.GetPrimAtPath("/World").specifier = Sdf.SpecifierDef
    ball.payloadList.Prepend(Sdf.Payload(geo, "/ball"))
    shot_layer.Save()

    with Scene() as scene:
        subs = scene.create.sublayer()
        rows = subs.get_param("imports", chunk.is_inst(chunk.ListGroup))
        rows.size = 1
        rows.get_indexed_param("sublayer_path", 0, chunk.is_param_t(str)).value = str(tmp_path / "shot.usda")
        stage = scene.get_stage()
        assert stage.GetPrimAtPath("/World/ball/render").IsValid()

        scene.set(subs, load_payloads=False)
        assert stage.GetPrimAtPath("/World/ball").IsValid(), "the prim with the payload stays"
        assert not stage.GetPrimAtPath("/World/ball/render").IsValid(), "what the payload brings is gone"
        scene.set(subs, load_payloads=True)
        assert stage.GetPrimAtPath("/World/ball/render").IsValid()
