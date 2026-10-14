"""A Layer chunk's layer is as strong as the chunk's place in the wiring.

It goes in the chunk's own cook layer, like everything a chunk does. A look layer made
downstream of a model's chunks, inside the model's Edit Target, is stronger than them --
with a view or Output and without, the same scene composing the same way.
"""

from __future__ import annotations

import pytest
from pxr import Sdf, Usd, UsdGeom

from usd_chunks import Scene, chunk


def _model_then_look(scene, driven: bool):
    """model's cube coloured 0.1, then a look layer below it that tints it 0.8."""
    model = scene.create.layer(layer_name="model")
    with scene.create.edit_target(model, edit_target="model") as target:
        outer = scene.create.chunkgroup(target, name="outer")
        with outer:
            cube = scene.create.cube(prim_path="/world/c", name="c_mesh")
            scene.create.set_display_color(cube, prim_path="/world/c", color_r=0.1, name="color_model")
    if driven:
        scene.set_output(outer)
    look = scene.create.layer(outer, layer_name="look")
    with scene.create.edit_target(look, edit_target="look") as target:
        lookdev = scene.create.chunkgroup(target, name="lookdev")
        with lookdev:
            scene.create.set_display_color(prim_path="/world/c", color_r=0.8, name="tint")
    if driven:
        scene.set_output(lookdev)
    return lookdev


def _red(stage) -> float:
    return UsdGeom.Gprim(stage.GetPrimAtPath("/world/c")).GetDisplayColorAttr().Get()[0][0]


@pytest.mark.parametrize("driven", [False, True], ids=["headless", "output"])
def test_a_look_layer_downstream_wins(driven) -> None:
    with Scene() as scene:
        _model_then_look(scene, driven)
        assert _red(scene.get_stage()) == pytest.approx(0.8)


@pytest.mark.parametrize("bake", [True, False], ids=["baked", "procedural"])
def test_export_keeps_the_look_layer_a_sublayer_of_model(tmp_path, bake) -> None:
    """The look layer is under a cook layer, which is not exported: it is lifted out.

    Baked, model's chunk output is weaker than look, so it cannot go into model.usda
    itself, whose own opinions beat every sublayer: it goes below look, in model.cook.usda.
    """
    with Scene() as scene:
        lookdev = _model_then_look(scene, driven=True)
        scene.create.export_usd(lookdev, output_dir=str(tmp_path), bake_all=bake, keep_session_layer=False)
        scene.commit()
    model = Sdf.Layer.FindOrOpen(str(tmp_path / "model.usda"))
    if bake:
        assert list(model.subLayerPaths) == ["look.usda", "model.cook.usda"]
        assert _red(Usd.Stage.Open(str(tmp_path / "model.usda"))) == pytest.approx(0.8)
    else:
        assert list(model.subLayerPaths) == ["look.usda"]


def test_the_look_layer_stays_put_through_its_chunks_recook() -> None:
    """A cook clears the chunk's layer first; the look layer must not drop out meanwhile,
    or an Edit Target on it resets itself."""
    with Scene() as scene:
        _model_then_look(scene, driven=True)
        layer_chunk = next(c for c in scene.get_chunks() if c.get_param("layer_name") is not None
                           and c.get_param("layer_name").value == "look")
        layer_chunk.execute()
        targets = [c.get_param("edit_target").value for c in scene.get_chunks() if c.get_param("edit_target")]
        assert "look" in targets
        assert _red(scene.get_stage()) == pytest.approx(0.8)


def _shot(tmp_path):
    """shot.usda on disk, sublayering fx, anim and layout: anim sets opacity, layout colour."""
    def make(name, build):
        stage = Usd.Stage.CreateNew(str(tmp_path / name))
        build(stage)
        stage.GetRootLayer().Save()
    make("fx.usda", lambda st: None)
    make("anim.usda", lambda st: UsdGeom.Gprim(st.OverridePrim("/world/c")).CreateDisplayOpacityAttr([0.2]))
    make("layout.usda", lambda st: UsdGeom.Gprim(UsdGeom.Cube.Define(st, "/world/c")).CreateDisplayColorAttr([(0.5, 0.5, 0.5)]))
    shot = Sdf.Layer.CreateNew(str(tmp_path / "shot.usda"))
    shot.subLayerPaths = ["fx.usda", "anim.usda", "layout.usda"]
    shot.Save()
    return str(tmp_path / "shot.usda")


def _light_below_anim(scene, driven: bool):
    """A light layer placed below anim.usda in the opened shot, tinting the cube 0.9."""
    stage = scene.get_stage()
    target = scene.create.edit_target(edit_target=stage.GetRootLayer().GetDisplayName())
    light = scene.create.layer(target, layer_name="light", place_below="anim.usda")
    with scene.create.edit_target(light, edit_target="light") as target:
        key = scene.create.set_display_color(target, prim_path="/world/c", color_r=0.9, name="key")
    if driven:
        scene.set_output(key)
    return light, key


def _root_sublayers(stage) -> list[str]:
    return [p.rsplit(":", 1)[-1] for p in stage.GetRootLayer().subLayerPaths if "graph_" not in p]


@pytest.mark.parametrize("driven", [False, True], ids=["headless", "output"])
def test_place_below_slots_a_layer_into_an_opened_shot(tmp_path, driven) -> None:
    """Over layout, under anim -- the same headless and with an Output."""
    with Scene.open(_shot(tmp_path)) as scene:
        light, _key = _light_below_anim(scene, driven)
        stage = scene.get_stage()
        assert _root_sublayers(stage) == ["fx.usda", "anim.usda", "light", "layout.usda"]
        assert light.get_param("place_below").menu_items == ["(Wired)", "fx.usda", "anim.usda", "layout.usda"]
        gprim = UsdGeom.Gprim(stage.GetPrimAtPath("/world/c"))
        assert _red(stage) == pytest.approx(0.9), "stronger than layout"
        assert list(gprim.GetDisplayOpacityAttr().Get()) == pytest.approx([0.2]), "anim's own value"


def test_a_placed_layer_follows_its_chunk_off_and_out(tmp_path) -> None:
    """Muted while the chunk is disabled, back when it is not; gone with Wired and on delete."""
    with Scene.open(_shot(tmp_path)) as scene:
        light, _key = _light_below_anim(scene, driven=True)
        stage = scene.get_stage()
        placed = light.get_param("_new_layer_id").value

        light.set_state(chunk.ChunkFlags.State.DISABLED)
        assert stage.IsLayerMuted(placed)
        light.unset_state(chunk.ChunkFlags.State.DISABLED)
        assert not stage.IsLayerMuted(placed)

        light.get_param("place_below").value = "(Wired)"
        assert _root_sublayers(stage) == ["fx.usda", "anim.usda", "layout.usda"]
        light.get_param("place_below").value = "fx.usda"
        assert _root_sublayers(stage) == ["fx.usda", "light", "anim.usda", "layout.usda"]

        scene.remove_chunk(light)
        assert _root_sublayers(stage) == ["fx.usda", "anim.usda", "layout.usda"]


@pytest.mark.parametrize("bake", [True, False], ids=["baked", "procedural"])
def test_export_keeps_a_placed_layer_where_it_was_placed(tmp_path, bake) -> None:
    with Scene.open(_shot(tmp_path)) as scene:
        _light, key = _light_below_anim(scene, driven=True)
        out = tmp_path / "out"
        scene.create.export_usd(key, output_dir=str(out), bake_all=bake, keep_session_layer=False)
        scene.commit()
    shot = Sdf.Layer.FindOrOpen(str(out / "shot.usda"))
    assert [p for p in shot.subLayerPaths if p.endswith(("anim.usda", "light.usda", "layout.usda"))][-3:] == [
        "anim.usda", "light.usda", "layout.usda"]
