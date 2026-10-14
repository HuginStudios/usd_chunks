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


_NESTED_NOT_OWNED = pytest.mark.xfail(
    strict=True,
    reason="A group nested in another is owned by the scene, not the group around it, so its "
    "cooks never reach what is wired after the outer group: on reopen the tint cooks before "
    "the nested group has made the cube, and authors nothing. Its layer order is right.",
)


@pytest.mark.parametrize("nested", [False, pytest.param(True, marks=_NESTED_NOT_OWNED)], ids=["group", "nested"])
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
