"""The Mute Layer chunk: mute layers picked from a list or matched by a path pattern."""

from __future__ import annotations

import os

from pxr import Sdf

from usd_chunks import Scene, chunk, chunk_registry


def _layer(stage, name):
    return next(layer for layer in stage.GetUsedLayers() + [Sdf.Layer.Find(i) for i in stage.GetMutedLayers()]
                if layer is not None and layer.GetDisplayName() == name)


def _muted_names(stage):
    """Display names of the muted layers, leaving out the chunk graph's own.

    A muted file layer nothing holds is closed, so its name comes from its path.
    """
    names = []
    for i in stage.GetMutedLayers():
        layer = Sdf.Layer.Find(i)
        if layer is None:
            names.append(os.path.basename(i))
        elif not chunk_registry.is_chunk_internal_layer(layer):
            names.append(layer.GetDisplayName())
    return sorted(names)


def _three_layers(scene):
    for name in ("light_key", "light_fill", "fx"):
        scene.create.layer(layer_name=name)


def test_pattern_mutes_every_match() -> None:
    with Scene() as scene:
        _three_layers(scene)
        scene.create.mute_layer(pattern="light_*")
        assert _muted_names(scene.get_stage()) == ["light_fill", "light_key"]


def test_dropdown_mutes_the_picked_layer_and_lists_no_graph_layers() -> None:
    with Scene() as scene:
        _three_layers(scene)
        mute = scene.create.mute_layer(layer="fx")
        stage = scene.get_stage()
        assert _muted_names(stage) == ["fx"]
        items = mute.get_param("layer").menu_items
        assert {"light_key", "light_fill", "fx"} <= set(items)
        assert not [i for i in items if i.startswith("graph_")]
        assert mute.get_param("muted").value == "fx"


def test_changing_the_pattern_unmutes_what_no_longer_matches() -> None:
    with Scene() as scene:
        _three_layers(scene)
        mute = scene.create.mute_layer(pattern="*light*")
        scene.set(mute, pattern="*fill")
        assert _muted_names(scene.get_stage()) == ["light_fill"]


def test_several_patterns_and_case() -> None:
    """Patterns separated by spaces each match on their own; case matters."""
    with Scene() as scene:
        _three_layers(scene)
        scene.create.mute_layer(pattern="*_key FX fx")
        assert _muted_names(scene.get_stage()) == ["fx", "light_key"]


def test_disable_and_delete_unmute() -> None:
    with Scene() as scene:
        _three_layers(scene)
        mute = scene.create.mute_layer(pattern="fx")
        stage = scene.get_stage()

        mute.set_state(chunk.ChunkFlags.State.DISABLED)
        assert _muted_names(stage) == []
        mute.unset_state(chunk.ChunkFlags.State.DISABLED)
        assert _muted_names(stage) == ["fx"]

        scene.remove_chunk(mute)
        assert _muted_names(stage) == []


def test_never_unmutes_a_layer_it_did_not_mute() -> None:
    with Scene() as scene:
        _three_layers(scene)
        stage = scene.get_stage()
        fx = _layer(stage, "fx")  # held: a muted anonymous layer nobody holds is freed
        stage.MuteLayer(fx.identifier)
        mute = scene.create.mute_layer(pattern="fx")
        scene.set(mute, pattern="")
        assert _muted_names(stage) == ["fx"]


def test_only_mutes_while_the_view_reaches_it() -> None:
    with Scene() as scene:
        _three_layers(scene)
        other = scene.create.layer(layer_name="other")
        mute = scene.create.mute_layer(pattern="fx")
        stage = scene.get_stage()

        scene.view(other)  # the mute chunk is now off the viewed chain: gated off
        assert _muted_names(stage) == []
        scene.view(mute)
        assert _muted_names(stage) == ["fx"]


def test_mutes_again_when_the_saved_scene_is_opened(tmp_path) -> None:
    """Muting is not saved in any file, so the chunk has to mute again on load.

    On load it cooks before the Sublayer chunk has brought the file in, so it must also
    catch the layer when it shows up.
    """
    for name in ("layout", "cfx"):
        Sdf.Layer.CreateNew(str(tmp_path / f"{name}.usda")).Save()
    network = str(tmp_path / "network.usda")
    with Scene() as scene:
        subs = scene.create.sublayer()
        rows = subs.get_param("imports", chunk.is_inst(chunk.ListGroup))
        rows.size = 2
        for i, name in enumerate(("cfx", "layout")):
            rows.get_indexed_param("sublayer_path", i, chunk.is_param_t(str)).value = str(tmp_path / f"{name}.usda")
        scene.create.mute_layer(subs, pattern="cfx*", name="mute_cfx")
        assert _muted_names(scene.get_stage()) == ["cfx.usda"]
        scene.save(network)

    with Scene.open(network) as scene:
        assert _muted_names(scene.get_stage()) == ["cfx.usda"]
        assert scene.get_chunk("mute_cfx").get_param("muted").value == "cfx.usda"


def test_a_pattern_matches_the_full_path_too(tmp_path) -> None:
    """Patterns work as in prim path fields: `**` crosses folders, `*` stays in one."""
    folder = tmp_path / "shot_cfx"
    folder.mkdir()
    Sdf.Layer.CreateNew(str(folder / "sim.usda")).Save()
    with Scene() as scene:
        subs = scene.create.sublayer()
        rows = subs.get_param("imports", chunk.is_inst(chunk.ListGroup))
        rows.size = 1
        rows.get_indexed_param("sublayer_path", 0, chunk.is_param_t(str)).value = str(folder / "sim.usda")
        mute = scene.create.mute_layer(subs, pattern="*_cfx*")
        assert _muted_names(scene.get_stage()) == [], "`*` does not reach into the folder name"
        scene.set(mute, pattern="**/shot_cfx/**")
        assert _muted_names(scene.get_stage()) == ["sim.usda"]


def test_commas_separate_patterns_too() -> None:
    with Scene() as scene:
        _three_layers(scene)
        scene.create.mute_layer(pattern="light_key,fx")
        assert _muted_names(scene.get_stage()) == ["fx", "light_key"]
