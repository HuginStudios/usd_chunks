"""Live sessions: values sent to a running network reach its params and its cook."""
from __future__ import annotations

import threading

import pytest
from pxr import Usd, UsdGeom

from usd_chunks import chunk_stage, cli, live
from usd_chunks.chunk_script import Scene


def _width(stage):
    points = UsdGeom.Mesh(stage.GetPrimAtPath("/box")).GetPointsAttr().Get()
    return round(max(p[0] for p in points) - min(p[0] for p in points), 3)


def _red(stage):
    return round(UsdGeom.Gprim(stage.GetPrimAtPath("/box")).GetDisplayColorAttr().Get()[0][0], 3)


def _chunk(scene, name):
    return next(c for c in scene.get_chunks() if c.name == name)


@pytest.fixture
def running(tmp_path):
    """A box and a colour on it, saved, then opened again with a live session on it."""
    net = tmp_path / "net.usda"
    with Scene() as scene:
        box = scene.create.cube(prim_path="/box", name="box")
        scene.create.set_display_color(box, prim_path="/box", color_r=1.0, color_g=0.0, color_b=0.0,
                                       name="paint")
        chunk_stage.export_chunks(scene, net)
    stage = Usd.Stage.Open(str(net))
    session = live.LiveSession(stage, net)
    scene = Scene(stage, "cli")
    session.publish(scene)
    yield net, stage, scene, session
    session.close(scene)
    scene.close()


def test_a_value_reaches_a_param_still_at_its_default_and_recooks(running):
    """A default has no attribute yet: the manifest names the one to write."""
    net, stage, scene, session = running
    assert _width(stage) == 1.0

    live.send(net, {"box.width": 3.0})
    assert session.poll()

    assert _chunk(scene, "box").get_param("cube.width").value == 3.0
    assert _width(stage) == 3.0


def test_a_string_is_read_by_the_param_type(running):
    net, stage, scene, session = running
    live.send(net, {"paint.color_r": "0.25"})
    session.poll()
    assert _red(stage) == 0.25


def test_a_multi_line_value_arrives_with_its_newlines_unchanged(running):
    """Inline USD code is the usual one, and it does not parse with carriage returns in it."""
    net, stage, scene, session = running
    text = "line one\nline two\n"
    live.send(net, {"paint.prim_path": text})
    assert b"\r" not in live.overrides_path(net).read_bytes()
    session.poll()
    assert _chunk(scene, "paint").get_param("prim_path").value == text


def test_earlier_values_stay_until_reset(running):
    net, stage, scene, session = running
    live.send(net, {"box.width": 3.0})
    live.send(net, {"paint.color_r": 0.5})
    session.poll()
    assert (_width(stage), _red(stage)) == (3.0, 0.5)

    live.send(net, reset=True)
    session.poll()
    assert (_width(stage), _red(stage)) == (1.0, 1.0)


def test_a_chunk_switched_off_drops_its_output_and_back_on_restores_it(running):
    net, stage, scene, session = running
    live.send(net, {"paint.active": False})
    session.poll()
    assert UsdGeom.Gprim(stage.GetPrimAtPath("/box")).GetDisplayColorAttr().Get() is None

    live.send(net, {"paint.active": "true"})
    session.poll()
    assert _red(stage) == 1.0


def test_nothing_new_is_nothing_to_reload(running):
    net, stage, scene, session = running
    assert not session.poll()
    live.send(net, {"box.width": 2.0})
    assert session.poll()
    assert not session.poll()


def test_a_waiting_send_returns_once_the_session_has_cooked_it(running):
    net, stage, scene, session = running
    finished = []

    def client():
        finished.append(live.send(net, {"box.width": 2.0}, wait=True, timeout=10))

    thread = threading.Thread(target=client)
    thread.start()
    while thread.is_alive():
        if session.poll():
            session.publish(scene)
        thread.join(0.05)
    assert finished == [1]
    assert live.read_manifest(net)["done"] == 1


def test_the_network_file_is_never_written(running):
    net, stage, scene, session = running
    before = net.read_bytes()
    live.send(net, {"box.width": 3.0, "paint.color_r": 0.5})
    session.poll()
    session.publish(scene)
    assert net.read_bytes() == before


def test_an_unknown_name_suggests_close_ones(running):
    net, *_ = running
    with pytest.raises(live.LiveError, match="Did you mean"):
        live.send(net, {"box.widht": 3.0})


def test_a_name_two_chunks_share_is_listed_by_prim_path(running):
    """Names are unique within a group, not across groups."""
    net, stage, scene, session = running
    with scene.create.chunkgroup(name="other"):
        scene.create.cube(prim_path="/box2", name="box")
    session.publish(scene)
    params = live.read_manifest(net)["params"]
    assert "box.cube.width" not in params
    widths = [key for key in params if key.endswith(".cube.width") and key.startswith("/")]
    assert len(widths) == 2, widths


def test_without_a_session_send_says_how_to_start_one(tmp_path):
    with pytest.raises(live.LiveError, match="--live"):
        live.send(tmp_path / "net.usda", {"box.width": 3.0})


def test_an_ended_session_takes_no_more_values(running):
    net, stage, scene, session = running
    session.close(scene)
    with pytest.raises(live.LiveError, match="ended"):
        live.send(net, {"box.width": 3.0})


def test_send_lists_what_it_can_set(running, monkeypatch, capsys):
    net, *_ = running
    monkeypatch.setattr("sys.argv", ["usdchunks", "send", str(net), "--list"])
    cli.main()
    out = capsys.readouterr().out
    assert "box.cube.width  (float)" in out
