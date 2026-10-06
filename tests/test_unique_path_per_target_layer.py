"""A create chunk's path is made unique only against what lands in the same layer.

Modeling wrote two layers in one scene, geo and proxy, behind two Edit Targets. Both define
/ball, as department layers in USD do. The proxy branch's Prim chunk saw /ball from the geo
layer and made /ball1. Now a path counts as taken only when it is defined in the same real
layer: by another chunk behind the same Edit Target, or by that layer itself.
"""

from __future__ import annotations

from pxr import Sdf

from usd_chunks import Scene, chunk


def _path(c):
    return c.get_param("primpath_location").value


def test_two_chunks_in_one_layer_still_get_unique_paths() -> None:
    with Scene() as scene:
        first = scene.create.prim(primpath_location="/prim")
        second = scene.create.prim(first, primpath_location="/prim")
        assert (_path(first), _path(second)) == ("/prim", "/prim1")


def test_the_same_path_behind_two_edit_targets_keeps_its_name() -> None:
    with Scene() as scene:
        made = []
        for name in ("geo", "proxy"):
            layer = scene.create.layer(layer_name=name)
            with scene.create.edit_target(layer, edit_target=name):
                made.append(scene.create.prim(primpath_location="/ball"))
        assert [_path(c) for c in made] == ["/ball", "/ball"]


def test_two_chunks_behind_one_edit_target_still_get_unique_paths() -> None:
    with Scene() as scene:
        layer = scene.create.layer(layer_name="geo")
        with scene.create.edit_target(layer, edit_target="geo"):
            first = scene.create.prim(primpath_location="/ball")
            second = scene.create.prim(first, primpath_location="/ball")
        assert (_path(first), _path(second)) == ("/ball", "/ball1")


def test_a_path_another_file_defines_keeps_its_name(tmp_path) -> None:
    published = Sdf.Layer.CreateNew(str(tmp_path / "geo.usda"))
    Sdf.CreatePrimInLayer(published, "/ball").specifier = Sdf.SpecifierDef
    published.GetPrimAtPath("/ball").typeName = "Xform"
    published.Save()

    with Scene() as scene:
        subs = scene.create.sublayer()
        rows = subs.get_param("imports", chunk.is_inst(chunk.ListGroup))
        rows.size = 1
        rows.get_indexed_param("sublayer_path", 0, chunk.is_param_t(str)).value = str(tmp_path / "geo.usda")
        layer = scene.create.layer(subs, layer_name="lookdev")
        with scene.create.edit_target(layer, edit_target="lookdev"):
            ball = scene.create.prim(primpath_location="/ball")
        assert _path(ball) == "/ball", "another department's prim is not a collision"
