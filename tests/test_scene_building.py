"""scene.building(): a script makes many chunks, and each cooks about once instead of again and again."""
from __future__ import annotations

from collections import Counter

import pytest
from pxr import UsdGeom

from usd_chunks import Scene, chunk_cook, chunk_registry, chunk_stage


@pytest.fixture
def cooks(monkeypatch):
    counts: Counter = Counter()
    real = chunk_cook.cook

    def counting(chunk, layer_of, stamp):
        counts[chunk.name] += 1
        return real(chunk, layer_of, stamp)

    monkeypatch.setattr(chunk_cook, "cook", counting)
    return counts


def _chain(scene, n=8):
    """A ground and n scatters, each wired after the last, set up the way a script does it."""
    ground = scene.create.mesh(prim_path="/World/ground", mesh_type="grid", name="ground")
    leaf = scene.create.prim(ground, primpath_location="/World/leaf", prim_type="Cube", name="leaf")
    last = leaf
    for i in range(n):
        last = scene.create.scatter_points(last, name=f"s{i}", inst_path=f"/World/s{i}", max_count=40, seed=i,
                                           **{"prototypes.0.proto_path": "/World/leaf"})
        scene.set(last, surface_prim="/World/ground")
    return last


def _positions(scene, n=8):
    stage = scene.get_stage()
    return [list(UsdGeom.PointInstancer(stage.GetPrimAtPath(f"/World/s{i}")).GetPositionsAttr().Get()) for i in range(n)]


def test_a_chain_built_in_a_block_cooks_each_chunk_at_most_twice(cooks) -> None:
    with Scene() as scene:
        with scene.building():
            _chain(scene)
        assert max(cooks.values()) <= 2, cooks


def test_a_block_builds_the_same_scene(cooks) -> None:
    with Scene() as scene:
        _chain(scene)
        plain = _positions(scene)
        plain_cooks = sum(cooks.values())
    cooks.clear()
    with Scene() as scene:
        with scene.building():
            _chain(scene)
        assert _positions(scene) == plain
        assert sum(cooks.values()) < plain_cooks


def test_a_light_made_in_a_block_takes_its_settings() -> None:
    """A Light builds its schema params in its cook, so it has to cook as it is made."""
    with Scene() as scene:
        with scene.building():
            light = scene.create.light(light_type="DistantLight", primpath_location="/World/sun")
            intensity = next(p for p in light.get_params() if p.name == "intensity" or p.name.endswith(":intensity"))
            intensity.value = 3.0
        assert intensity.value == 3.0


# --- the block must give the same layer order, Merge results and Edit Target results ---


def _color(scene, path="/box"):
    return tuple(UsdGeom.Gprim(scene.get_stage().GetPrimAtPath(path)).GetDisplayColorAttr().Get()[0])


def _edit_target_with_a_group(scene):
    """An Edit Target, red, then a group setting blue: the group, downstream, must win."""
    layer = scene.create.layer(layer_name="look")
    with scene.create.edit_target(layer, edit_target="look"):
        box = scene.create.cube(prim_path="/box", name="box_mesh")
        red = scene.create.set_display_color(box, prim_path="/box", color_r=1.0, color_g=0.0, color_b=0.0,
                                             name="red")
        with scene.create.chunkgroup(red, name="recolor"):
            blue = scene.create.set_display_color(prim_path="/box", color_r=0.0, color_g=0.0, color_b=1.0,
                                                  name="blue")
    return blue


def _merge_with_unequal_branches(scene):
    """A Merge of a one-chunk branch (red) and a three-chunk one (green last): the first input wins."""
    box = scene.create.cube(prim_path="/box", name="box_mesh")
    first = scene.create.set_display_color(box, prim_path="/box", color_r=1.0, color_g=0.0, color_b=0.0,
                                           name="first")
    second = box
    for i, g in enumerate((0.2, 0.5, 1.0)):
        second = scene.create.set_display_color(second, prim_path="/box", color_r=0.0, color_g=g, color_b=0.0,
                                                name=f"second{i}")
    return scene.create.merge(first, second, name="merge")


@pytest.mark.parametrize("make", [_edit_target_with_a_group, _merge_with_unequal_branches],
                         ids=["edit-target-with-a-group", "merge"])
def test_a_block_gives_the_same_layer_order_and_result(make) -> None:
    with Scene() as scene:
        make(scene)
        plain = (_color(scene), _chunk_layer_order(scene))
    with Scene() as scene:
        with scene.building():
            make(scene)
        assert (_color(scene), _chunk_layer_order(scene)) == plain


def test_a_block_with_an_active_view_gives_the_same_result() -> None:
    with Scene() as scene:
        scene.set_view(_merge_with_unequal_branches(scene))
        plain = (_color(scene), _chunk_layer_order(scene))
    with Scene() as scene:
        with scene.building():
            scene.set_view(_merge_with_unequal_branches(scene))
        assert (_color(scene), _chunk_layer_order(scene)) == plain


def test_an_edit_target_with_a_group_built_in_a_block_still_follows_edits() -> None:
    """The reviewer's case: built in the block it came out red and stayed red after an edit."""
    with Scene() as scene:
        with scene.building():
            blue = _edit_target_with_a_group(scene)
        assert _color(scene) == (0.0, 0.0, 1.0)
        blue.get_param("color_g").value = 1.0
        assert _color(scene) == (0.0, 1.0, 1.0)


def test_a_group_under_an_edit_target_built_in_a_block_cooks_into_its_layer() -> None:
    # Layers placed at the block's end: the group's child was resolved while the Edit
    # Target's layer was in no stack, fell back to the session layer, and still showed blue
    # -- but exporting the layer would have left it out.
    with Scene() as scene:
        with scene.building():
            _edit_target_with_a_group(scene)
        lm = chunk_registry.ChunkLayerManager()
        targets = {c.name: lm.get_chunk_target(c.get_hash()).GetDisplayName()
                   for c in scene.get_chunks() if c.name in ("box_mesh", "red", "blue")}
        assert targets == {"box_mesh": "look", "red": "look", "blue": "look"}


def _chunk_layer_order(scene):
    """The chunks' layers in stack order, by chunk name, strongest first.

    Not a View's or an Output's: they never cook, so their layers are empty and where they
    sit cannot change what composes. Placed at the end of a block, a View's lands below the
    chunk it views rather than above it.
    """
    lm = chunk_registry.ChunkLayerManager()
    by_id = {lm.get_layer(c).identifier: c.name for c in scene.get_chunks()
             if lm.get_layer(c) is not None and not chunk_stage.is_terminal_chunk(c)}
    stack = [layer.identifier for layer in scene.get_stage().GetLayerStack()]
    return [by_id[i] for i in stack if i in by_id]


@pytest.fixture
def scans(monkeypatch):
    calls = []
    real = chunk_stage.ChunkStageSync._scan_for_new_chunks

    def counting(self, stage):
        calls.append(chunk_cook.is_building())
        return real(self, stage)

    monkeypatch.setattr(chunk_stage.ChunkStageSync, "_scan_for_new_chunks", counting)
    return calls


def test_a_block_scans_for_new_chunks_only_at_its_end(scans) -> None:
    # Scanned per stage change, a 300-chunk build walked 740,000 prims in 1,500 scans, all to
    # find the chunks the script had made itself. At the end: the held scan, and one more for
    # the layers being ranked.
    with Scene() as scene:
        with scene.building():
            _chain(scene, n=3)
        assert scans and len(scans) <= 2 and not any(scans), scans


def test_chunks_arriving_by_usd_in_a_block_join_at_its_end(tmp_path) -> None:
    # The scan is what turns chunk prims arriving by plain USD into chunks -- here a saved
    # network sublayered in by a script, not through a chunk. Held to the end of the block,
    # it still finds them.
    net = str(tmp_path / "net.usda")
    with Scene() as scene:
        scene.create.transform(name="Inner", primpath_location="/World/inner", ty=2.0)
        scene.save(net)
    with Scene() as plain:
        plain.get_stage().GetRootLayer().subLayerPaths.append(net)
        expected = sorted(c.name for c in plain.get_chunks())
    with Scene() as scene:
        with scene.building():
            scene.get_stage().GetRootLayer().subLayerPaths.append(net)
            assert "Inner" not in [c.name for c in scene.get_chunks()]
        assert sorted(c.name for c in scene.get_chunks()) == expected
        assert "Inner" in expected
