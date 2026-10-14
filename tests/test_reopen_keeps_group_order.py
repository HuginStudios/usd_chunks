"""A chunk wired after a chunkgroup is stronger than the chunks in it, built and reopened alike.

Its layer sorted by its depth past the group's inputs, which tied with the group's last
chunk: a fresh build happened to put it on top, a reopen did not, and the tint after a
model group came back the model's colour.
"""

from __future__ import annotations

import pytest
from pxr import UsdGeom

from usd_chunks import Scene, chunk_stage


def _red(scene) -> float:
    return UsdGeom.Gprim(scene.get_stage().GetPrimAtPath("/world/c")).GetDisplayColorAttr().Get()[0][0]


def _model_then_tint(scene, driven: bool, nested: bool = False):
    """A cube coloured 0.1 inside a group (inside another, nested), then a tint of 0.8 after it."""
    model = scene.create.layer(layer_name="model")
    with scene.create.edit_target(model, edit_target="model") as target:
        outer = scene.create.chunkgroup(target, name="outer")
        with outer:
            inner = scene.create.chunkgroup(name="inner") if nested else None
            with inner if nested else outer:
                cube = scene.create.cube(prim_path="/world/c", name="c_mesh")
                scene.create.set_display_color(cube, prim_path="/world/c", color_r=0.1, name="color_model")
    tint = scene.create.set_display_color(outer, prim_path="/world/c", color_r=0.8, name="tint")
    if driven:
        scene.set_output(tint)
    return tint


@pytest.mark.parametrize("nested", [False, True], ids=["group", "nested"])
@pytest.mark.parametrize("driven", [False, True], ids=["headless", "output"])
def test_the_chunk_after_a_group_wins_after_a_reopen(tmp_path, driven, nested) -> None:
    path = str(tmp_path / "net.usda")
    with Scene() as scene:
        _model_then_tint(scene, driven, nested)
        assert _red(scene) == pytest.approx(0.8)
        scene.save(path)
    with Scene.open(path) as scene:
        assert _red(scene) == pytest.approx(0.8)


def test_a_chunk_after_a_group_ranks_past_every_chunk_in_it() -> None:
    """Ranked over the whole scene, the root context and the groups included.

    A chunkgroup is its own context, so siblings come from prim parentage: by context, the
    group ranked among its own children and never against the chunk after it -- and the
    walk up from the root context, its own context too, never ended.
    """
    with Scene() as scene:
        tint = _model_then_tint(scene, driven=False, nested=True)
        keys = chunk_stage.nested_cook_keys(scene.get_chunks(), scene.get_stage())
        inside = [c for c in scene.get_chunks() if c.name in ("inner", "c_mesh", "color_model")]
        assert all(keys[id(tint)] > keys[id(c)] for c in inside)


@pytest.mark.parametrize("nested", [False, True], ids=["group", "nested"])
def test_an_edit_inside_a_group_recooks_what_is_wired_after_it(nested) -> None:
    """A group nested in another is owned by it, so a cook inside reaches the chunk after
    the outer group -- as one inside a group always did. Owned by the scene, it stopped at
    the nested group, and the tint never saw the cube that moved under it."""
    with Scene() as scene:
        tint = _model_then_tint(scene, driven=True, nested=nested)
        scene.set(tint, prim_path="/world/d")
        stage = scene.get_stage()
        assert not stage.GetPrimAtPath("/world/d"), "nothing there yet for the tint to colour"

        cube = next(c for c in scene.get_chunks() if c.name == "c_mesh")
        scene.set(cube, prim_path="/world/d")
        colour = UsdGeom.Gprim(stage.GetPrimAtPath("/world/d")).GetDisplayColorAttr().Get()
        assert colour is not None and colour[0][0] == pytest.approx(0.8)
