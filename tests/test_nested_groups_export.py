"""Nested chunkgroups through every way a scene leaves the session.

Each export path is checked against the live scene rather than against a hand-written
expectation of its file: a save reopens to the same network and cooks to the same
result; a baked export recomposes to what scene.export() writes; a procedural one reopens
and re-cooks to it. The network is three groups deep with a param lofted out of the
innermost, so anything that only works one level down shows up here.
"""

from __future__ import annotations

import os

import pytest
from pxr import Sdf, Usd, UsdGeom

from usd_chunks import Scene, chunk, chunk_scene, chunk_stage
from usd_chunks.chunk_stage import get_chunk_prim_path


def _nested(scene, depth: int = 3):
    """g0[ a -> g1[ b -> g2[ c -> move_c ] ] ], with move_c.tx lofted onto the innermost group.

    Every level makes a cube, so what is on the stage says which levels cooked.
    """
    groups = []
    upstream = None
    for level in range(depth):
        group = scene.create.chunkgroup(upstream, name=f"g{level}") if upstream else scene.create.chunkgroup(name=f"g{level}")
        groups.append(group)
        group.__enter__()
        upstream = scene.create.cube(prim_path=f"/world/m{level}", name=f"mesh{level}")
    move = scene.create.transform(upstream, primpath_location=f"/world/m{depth - 1}", tx=2.0, name="move")
    for group in reversed(groups):
        group.__exit__(None, None, None)
    chunk_stage.loft_param_to_context(move.get_param("tx"), groups[-1])
    move.get_param("tx").value = 5.0
    return groups, move


def _by_name(scene, name):
    return next(c for c in scene.get_chunks() if c.name == name)


def _tx(stage, path):
    return UsdGeom.Xformable(stage.GetPrimAtPath(path)).GetLocalTransformation().ExtractTranslation()[0]


def _chunk_paths(scene):
    return sorted({get_chunk_prim_path(c) for c in scene.get_chunks()})


def _text(path):
    return Sdf.Layer.FindOrOpen(str(path)).ExportToString()


# -- save / reopen -------------------------------------------------------------------


def test_every_level_cooks() -> None:
    with Scene() as scene:
        _nested(scene)
        stage = scene.get_stage()
        assert [stage.GetPrimAtPath(f"/world/m{i}").GetTypeName() for i in range(3)] == ["Mesh"] * 3
        assert _tx(stage, "/world/m2") == 5.0


def test_a_save_reopens_to_the_same_network_and_result(tmp_path) -> None:
    """Reopened: the same chunks at the same paths, cooking to byte-for-byte the same export."""
    with Scene() as scene:
        _nested(scene)
        live = _chunk_paths(scene)
        scene.save(str(tmp_path / "a.usda"))
        scene.export(str(tmp_path / "before.usda"))

    with Scene.open(str(tmp_path / "a.usda")) as scene:
        assert _chunk_paths(scene) == live
        assert _tx(scene.get_stage(), "/world/m2") == 5.0, "the lofted value comes back"
        scene.export(str(tmp_path / "after.usda"))

    assert _text(tmp_path / "after.usda") == _text(tmp_path / "before.usda")


def test_saving_a_reopened_scene_changes_nothing(tmp_path) -> None:
    """A save is a fixed point: reopen and save again, and the file is the same."""
    with Scene() as scene:
        _nested(scene)
        scene.save(str(tmp_path / "a.usda"))
    with Scene.open(str(tmp_path / "a.usda")) as scene:
        scene.save(str(tmp_path / "b.usda"))
    assert _text(tmp_path / "b.usda") == _text(tmp_path / "a.usda")


@pytest.mark.parametrize("depth", [1, 3])
def test_an_inner_edit_to_a_lofted_param_survives_the_next_save(tmp_path, depth) -> None:
    """After a reopen the loft synced only group -> inner. An edit made on the inner chunk
    (its widget, a gizmo, a script) cooked, but the group's interface attribute -- the one
    the file saves and the load reads -- kept the old value, so the next open undid it."""
    with Scene() as scene:
        groups, _move = _nested(scene, depth)
        inner_group = groups[-1].name
        scene.save(str(tmp_path / "a.usda"))

    with Scene.open(str(tmp_path / "a.usda")) as scene:
        _by_name(scene, "move").get_param("tx").value = 7.0
        group_prim = scene.get_stage().GetPrimAtPath(get_chunk_prim_path(_by_name(scene, inner_group)))
        assert group_prim.GetAttribute("inputs:move_tx").Get() == 7.0, "the interface follows the edit"
        scene.save(str(tmp_path / "b.usda"))

    with Scene.open(str(tmp_path / "b.usda")) as scene:
        assert _by_name(scene, "move").get_param("tx").value == 7.0
        assert _tx(scene.get_stage(), f"/world/m{depth - 1}") == 7.0


def test_a_shallow_group_wired_after_a_deep_one_cooks_after_it_on_reopen(tmp_path) -> None:
    """Load cooked the roots by nesting depth alone, so `tint` (two groups down) cooked
    before `/world/m2` (three groups down, in the group wired upstream of it) existed --
    and nothing cooked it again. Live, the wiring ordered them; reopened, the colour was gone."""
    with Scene() as scene:
        groups, _move = _nested(scene)
        look = scene.create.chunkgroup(groups[0], name="look")
        with look, scene.create.chunkgroup(name="shading"):
            scene.create.set_display_color(prim_path="/world/m2", color_r=0.8, name="tint")
        scene.save(str(tmp_path / "a.usda"))
        scene.export(str(tmp_path / "before.usda"))

    with Scene.open(str(tmp_path / "a.usda")) as scene:
        color = UsdGeom.Gprim(scene.get_stage().GetPrimAtPath("/world/m2")).GetDisplayColorAttr().Get()
        assert color is not None and tuple(color[0])[0] == pytest.approx(0.8)
        scene.export(str(tmp_path / "after.usda"))
    assert _text(tmp_path / "after.usda") == _text(tmp_path / "before.usda")


def test_a_reopened_loft_drives_the_inner_chunk(tmp_path) -> None:
    with Scene() as scene:
        _nested(scene)
        scene.save(str(tmp_path / "a.usda"))
    with Scene.open(str(tmp_path / "a.usda")) as scene:
        lofted = next(p for p in _by_name(scene, "g2").get_my_params() if p.name == "move_tx")
        lofted.value = 9.0
        assert _by_name(scene, "move").get_param("tx").value == 9.0
        assert _tx(scene.get_stage(), "/world/m2") == 9.0


def test_a_reopened_loft_can_be_lofted_up_again(tmp_path) -> None:
    """Pinned: a restored group param belongs to its group, so it lofts to the one above."""
    with Scene() as scene:
        _nested(scene)
        scene.save(str(tmp_path / "a.usda"))
    with Scene.open(str(tmp_path / "a.usda")) as scene:
        g1, g2 = _by_name(scene, "g1"), _by_name(scene, "g2")
        lofted = next(p for p in g2.get_my_params() if p.name == "move_tx")
        assert lofted.get_chunk() is g2
        chunk_stage.loft_param_to_context(lofted, g1)
        outer = next(p for p in g1.get_my_params() if getattr(p, "_lofted", False))
        outer.value = 11.0
        assert _tx(scene.get_stage(), "/world/m2") == 11.0



def _lofted_on(group):
    return [p for p in group.get_my_params() if getattr(p, "_lofted", False)]


def _lofted_to_the_top(scene):
    """_nested, with move.tx lofted on up from g2 through g1 to g0."""
    groups, move = _nested(scene)
    param = _lofted_on(groups[2])[0]
    for group in (groups[1], groups[0]):
        chunk_stage.loft_param_to_context(param, group)
        param = _lofted_on(group)[0]
    return groups, move, param


def test_a_loft_up_to_the_top_reopens_whole_and_with_its_value(tmp_path) -> None:
    with Scene() as scene:
        _, _, top = _lofted_to_the_top(scene)
        top.value = 8.0
        scene.save(str(tmp_path / "a.usda"))
    with Scene.open(str(tmp_path / "a.usda")) as scene:
        levels = [_lofted_on(_by_name(scene, n)) for n in ("g2", "g1", "g0")]
        assert [len(level) for level in levels] == [1, 1, 1]
        assert _by_name(scene, "move").get_param("tx").value == 8.0
        levels[2][0].value = 3.0
        assert _tx(scene.get_stage(), "/world/m2") == 3.0


def test_a_reopened_loft_up_to_the_top_unlofts_whole(tmp_path) -> None:
    with Scene() as scene:
        _lofted_to_the_top(scene)
        scene.save(str(tmp_path / "a.usda"))
    with Scene.open(str(tmp_path / "a.usda")) as scene:
        g2 = _by_name(scene, "g2")
        chunk_stage.unloft_param(chunk_stage.lofted_copy(_by_name(scene, "move").get_param("tx"), g2))
        assert [_lofted_on(_by_name(scene, n)) for n in ("g2", "g1", "g0")] == [[], [], []]

# -- export_network --------------------------------------------------------------------


def test_a_nested_group_exported_as_a_network_opens_and_cooks(tmp_path) -> None:
    with Scene() as scene:
        groups, _move = _nested(scene)
        root = chunk_stage.export_network(scene, groups[1], str(tmp_path / "g1.usda"))
    assert root == "/chunks/g1"

    layer = Sdf.Layer.FindOrOpen(str(tmp_path / "g1.usda"))
    text = layer.ExportToString()
    assert "/chunks/ChunkGroup" not in text, "every wire is rebased onto the new root"
    assert not layer.GetPropertyAtPath("/chunks/g1.inputs:chunk").connectionPathList.GetAddedOrExplicitItems()

    with Scene.open(str(tmp_path / "g1.usda")) as scene:
        stage = scene.get_stage()
        assert not stage.GetPrimAtPath("/world/m0"), "the level above is not part of it"
        assert stage.GetPrimAtPath("/world/m1").GetTypeName() == "Mesh"
        assert _tx(stage, "/world/m2") == 5.0, "the lofted value came with the group"


# -- Export USD chunk ------------------------------------------------------------------


def test_export_usd_flatten_matches_scene_export_and_save(tmp_path) -> None:
    """Flatten Baked writes what scene.export() writes; Flatten Procedural what scene.save() does."""
    with Scene() as scene:
        groups, _move = _nested(scene)
        baked = scene.create.export_usd(groups[0], flatten=True, output_file=str(tmp_path / "baked.usda"))
        scene.create.export_usd(baked, flatten=True, export_mode="Procedural",
                                output_file=str(tmp_path / "procedural.usda"))
        scene.commit()
        scene.export(str(tmp_path / "export.usda"))
        scene.save(str(tmp_path / "save.usda"))

    assert _text(tmp_path / "baked.usda") == _text(tmp_path / "export.usda")
    assert _text(tmp_path / "procedural.usda") == _text(tmp_path / "save.usda")


def _two_departments(scene, out_dir, bake: bool):
    """Nested groups behind two Edit Targets, and an Export USD writing every layer."""
    model = scene.create.layer(layer_name="model")
    with scene.create.edit_target(model, edit_target="model") as target:
        outer = scene.create.chunkgroup(target, name="outer")
        with outer:
            a = scene.create.cube(prim_path="/world/a", name="a_mesh")
            with scene.create.chunkgroup(a, name="mid"):
                b = scene.create.cube(prim_path="/world/b", name="b_mesh")
                with scene.create.chunkgroup(b, name="inner"):
                    scene.create.cube(prim_path="/world/c", name="c_mesh")
    look = scene.create.layer(outer, layer_name="look")
    with scene.create.edit_target(look, edit_target="look") as target:
        lookdev = scene.create.chunkgroup(target, name="lookdev")
        with lookdev:
            with scene.create.chunkgroup(name="shading"):
                scene.create.set_display_color(prim_path="/world/c", color_r=0.8, name="tint")
    return scene.create.export_usd(lookdev, output_dir=str(out_dir), bake_all=bake)


def _open_exported(out_dir, root_name):
    """The exported files composed the way the live stage was: the session layer over the root."""
    return Usd.Stage.Open(
        Sdf.Layer.FindOrOpen(str(out_dir / root_name)),
        Sdf.Layer.FindOrOpen(str(out_dir / "SessionLayer.usda")),
    )


def _content(layer):
    """What the layer composes to: every prim's type and attribute values, by path.

    Not the layer's text. Sibling prims each come from their own chunk's layer, and a
    reopened scene stacks those layers in a different (equally valid) order, which is the
    order a flatten lists the siblings in.
    """
    stage = Usd.Stage.Open(layer)
    return {
        str(prim.GetPath()): (
            prim.GetTypeName(),
            sorted((attr.GetName(), str(attr.Get())) for attr in prim.GetAttributes() if attr.HasValue()),
        )
        for prim in stage.Traverse()
    }


def test_baked_layers_recompose_to_the_live_scene(tmp_path) -> None:
    with Scene() as scene:
        _two_departments(scene, tmp_path, bake=True)
        stage = scene.get_stage()
        root_name = stage.GetRootLayer().GetDisplayName()
        live = _content(chunk_stage.flatten_without_chunks(stage))
        scene.commit()

    assert {"model.usda", "look.usda", "SessionLayer.usda"} <= set(os.listdir(tmp_path))
    for name in os.listdir(tmp_path):
        assert "anon:" not in _text(tmp_path / name), f"{name} points at a layer that only existed in the session"
    exported = _open_exported(tmp_path, root_name)
    assert _content(chunk_stage.flatten_without_chunks(exported)) == live


def test_procedural_layers_reopen_and_recook_to_the_live_scene(tmp_path) -> None:
    with Scene() as scene:
        _two_departments(scene, tmp_path, bake=False)
        stage = scene.get_stage()
        root_name = stage.GetRootLayer().GetDisplayName()
        live = _content(chunk_stage.flatten_without_chunks(stage))
        live_chunks = _chunk_paths(scene)
        scene.commit()

    exported = _open_exported(tmp_path, root_name)
    assert not exported.GetPrimAtPath("/world/c"), "procedural: the recipe, not the result"
    with Scene(exported) as scene:
        assert _chunk_paths(scene) == live_chunks
        assert _content(chunk_stage.flatten_without_chunks(scene.get_stage())) == live


def test_export_usd_inside_a_nested_group_commits(tmp_path) -> None:
    """scene.commit() reaches a writer however deep it sits."""
    out = tmp_path / "deep.usda"
    with Scene() as scene:
        groups, _move = _nested(scene)
        with groups[0], groups[1]:
            exporter = scene.create.export_usd(flatten=True, output_file=str(out))
        assert get_chunk_prim_path(exporter).count("/") == 5
        scene.commit()
    assert Usd.Stage.Open(str(out)).GetPrimAtPath("/world/m2")


def test_a_reopened_export_writes_the_same_files(tmp_path) -> None:
    """The Export USD chunk saved with its rows, reopened and committed again."""
    first, second = tmp_path / "first", tmp_path / "second"
    with Scene() as scene:
        exporter = _two_departments(scene, first, bake=True)
        exporter.get_param("layer_format", chunk.is_param_t(str)).value = ".usdc"
        scene.commit()
        scene.save(str(tmp_path / "scene.usda"))

    with Scene.open(str(tmp_path / "scene.usda")) as scene:
        exporter = next(c for c in chunk_scene.all_chunks(scene.get_chunks()) if c.name == "ExportUSD")
        exporter.get_param("output_dir", chunk.is_param_t(str)).value = str(second)
        scene.commit()

    # The root layer is named after the file it was opened from, so only the rest compare.
    for name in ("SessionLayer.usdc", "model.usdc", "look.usdc"):
        assert Sdf.Layer.FindOrOpen(str(first / name)).GetFileFormat().formatId == "usdc"
        assert _content(Sdf.Layer.FindOrOpen(str(second / name))) == _content(Sdf.Layer.FindOrOpen(str(first / name))), name
