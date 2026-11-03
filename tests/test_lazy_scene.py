"""A lazy Scene writes the network; reads and the block's end cook it.

Scene(lazy=True): making chunks, wiring them and setting values only mark what they reach.
scene.cook(chunk) cooks what reading that chunk needs and nothing else; export and commit
cook everything, each chunk once; what is still marked when the Scene closes is dropped, as
closing removes every result. A chunk that builds params in its cook (Light) takes values
given at create the way a loaded network's stored values do -- in that first cook.
"""
from __future__ import annotations

import collections

import pytest
from chunk_stubs import attach_stub_module
from pxr import Gf, Usd, UsdGeom

from usd_chunks import Scene, chunk_cook


@pytest.fixture
def cooks(monkeypatch):
    """How many times each chunk ran its module, by name."""
    counts = collections.Counter()
    run = chunk_cook._run

    def counted(chunk, *args, **kwargs):
        counts[chunk.name] += 1
        return run(chunk, *args, **kwargs)

    monkeypatch.setattr(chunk_cook, "_run", counted)
    return counts


def _stage():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    return stage


def _build(scene):
    box = scene.create.cube(name="Box", prim_path="/geo/box")
    move = scene.create.transform(box, name="Move", primpath_location=box.get_param("prim_path"), tz=3.0)
    sun = scene.create.distant_light(move, name="Sun", intensity=7.0)
    return box, move, sun


def _moved(stage):
    return UsdGeom.Xformable(stage.GetPrimAtPath("/geo/box")).ComputeLocalToWorldTransform(0).ExtractTranslation()


def test_writing_the_network_cooks_nothing_and_cooking_it_cooks_each_chunk_once(cooks):
    stage = _stage()
    with Scene(stage, lazy=True) as scene:
        _, move, _ = _build(scene)
        scene.set(move, tz=5.0)
        assert not cooks
        assert not stage.GetPrimAtPath("/geo/box")
        scene.cook()
        assert cooks == {"Box": 1, "Move": 1, "Sun": 1}
        assert _moved(stage) == Gf.Vec3d(0, 0, 5)


def test_a_read_cooks_only_what_is_above_it(cooks):
    stage = _stage()
    with Scene(stage, lazy=True) as scene:
        _, move, sun = _build(scene)
        scene.set(move, tz=5.0)
        assert scene.cook(move) is move
        assert cooks == {"Box": 1, "Move": 1}
        assert _moved(stage) == Gf.Vec3d(0, 0, 5)
        scene.set(move, tz=6.0)
        scene.cook(move)
        assert _moved(stage) == Gf.Vec3d(0, 0, 6)
        assert cooks == {"Box": 1, "Move": 2}
    assert cooks == {"Box": 1, "Move": 2}


def _text(path):
    return "\n".join(ln for ln in path.read_text().splitlines() if "moduleHash" not in ln)


def test_lazy_gives_the_result_and_network_an_eager_scene_does(tmp_path):
    for lazy in (False, True):
        with Scene(_stage(), lazy=lazy) as scene:
            _, move, sun = _build(scene)
            scene.set(move, tz=5.0)
            scene.set_output(sun)
            scene.export(str(tmp_path / f"out{lazy}.usda"))
            scene.save(str(tmp_path / f"net{lazy}.usda"))
    assert _text(tmp_path / "outTrue.usda") == _text(tmp_path / "outFalse.usda")
    assert _text(tmp_path / "netTrue.usda") == _text(tmp_path / "netFalse.usda")
    assert "float inputs:intensity = 7" in _text(tmp_path / "outTrue.usda")


def test_a_network_saved_uncooked_reopens_to_the_same_result(tmp_path):
    """Saved before anything cooked, it lacks only what a cook writes on the chunk prims
    (the module hash, Transform's xformOp id), which its first cook on opening writes."""
    for lazy in (False, True):
        with Scene(_stage(), lazy=lazy) as scene:
            _, move, sun = _build(scene)
            scene.set(move, tz=5.0)
            scene.set_output(sun)
            scene.save(str(tmp_path / f"net{lazy}.usda"))
        with Scene.open(str(tmp_path / f"net{lazy}.usda")) as scene:
            scene.export(str(tmp_path / f"out{lazy}.usda"))
    assert _text(tmp_path / "outTrue.usda") == _text(tmp_path / "outFalse.usda")


def test_a_value_for_a_param_built_in_the_cook_reaches_the_cooked_stage(cooks):
    stage = _stage()
    with Scene(stage, lazy=True) as scene:
        sun = scene.create.distant_light(name="Sun", intensity=7.0)
        assert not cooks
        scene.cook(sun)
        assert sun.get_param("intensity").value == 7.0
        lights = [p for p in stage.Traverse() if p.GetTypeName() == "DistantLight"]
        assert [p.GetAttribute("inputs:intensity").Get() for p in lights] == [7.0]
    assert cooks == {"Sun": 1}


@pytest.mark.parametrize("lazy", [False, True])
def test_cook_recooks_the_chunk_it_is_given_though_nothing_marked_it(lazy):
    """A script can change what a cook reads behind the framework's back (a Scatter Brush's
    painted field is a Python attribute); scene.cook still runs that chunk again."""
    seen = []
    with Scene(_stage(), lazy=lazy) as scene:
        box = scene.create.cube(name="Box")
        attach_stub_module(box, execute=lambda node: seen.append(node.hidden))
        box.hidden = 1
        scene.cook(box)
        box.hidden = 2
        scene.cook(box)
    assert seen == [1, 2]


def test_a_misspelled_param_raises_at_the_first_cook_naming_chunk_and_param():
    with Scene(_stage(), lazy=True) as scene:
        sun = scene.create.distant_light(name="Sun", intensiti=7.0)
        with pytest.raises(KeyError, match="Sun.*'intensiti'"):
            scene.cook(sun)


def test_a_name_set_after_the_first_cook_is_checked_at_once():
    with Scene(_stage(), lazy=True) as scene:
        box = scene.create.cube(name="Box")
        scene.cook(box)
        with pytest.raises(KeyError, match="prim_pth"):
            scene.set(box, prim_pth="/x")


@pytest.mark.parametrize("own", [True, False])
def test_closing_drops_what_is_still_marked(cooks, own):
    with Scene(None if own else _stage(), lazy=True) as scene:
        _build(scene)
    assert not cooks
    assert not chunk_cook.is_deferring() and not chunk_cook._pending


def test_opening_lazily_loads_without_cooking(tmp_path, cooks):
    path = str(tmp_path / "net.usda")
    with Scene(_stage()) as scene:
        _, move, sun = _build(scene)
        scene.set_output(sun)
        scene.save(path)
    cooks.clear()
    with Scene.open(path, lazy=True) as scene:
        assert not cooks
        scene.cook(scene.get_chunk("Move"))
        assert cooks == {"Box": 1, "Move": 1}
        assert _moved(scene.get_stage()) == Gf.Vec3d(0, 0, 3)


def test_saving_cooks_only_the_chunks_that_build_a_param_a_value_waits_for(tmp_path, cooks):
    """Sun is below Box and Move, which stay uncooked: Sun cooks alone to build its
    intensity, so the saved network holds it, and cooks again in place with the rest."""
    path = str(tmp_path / "net.usda")
    with Scene(_stage(), lazy=True) as scene:
        _, _, sun = _build(scene)
        scene.save(path)
        assert cooks == {"Sun": 1}
        scene.cook(sun)
        assert cooks == {"Box": 1, "Move": 1, "Sun": 2}
    assert "float inputs:attr_grp:inputs:intensity = 7" in open(path).read()
    with Scene.open(path) as scene:
        assert scene.get_chunk("Sun").get_param("intensity").value == 7.0
