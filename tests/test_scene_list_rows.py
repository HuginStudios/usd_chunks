"""A script sets a list's rows by name, and the list grows to hold them.

`imports.1.sublayer_path` on a new Sublayer raised KeyError: a list starts with no rows,
and a script had to set `get_param("imports").size` first, which nothing said. A
benchmark agent spent seven calls finding that out in the framework's source.
"""
from __future__ import annotations

import pytest
from pxr import Sdf

from usd_chunks import Scene, chunk


@pytest.fixture
def scene():
    with Scene(host_name="pytest") as scene:
        yield scene


def _layer(tmp_path, name, prim):
    layer = Sdf.Layer.CreateNew(str(tmp_path / f"{name}.usda"))
    Sdf.CreatePrimInLayer(layer, prim).specifier = Sdf.SpecifierDef
    layer.Save()
    return layer.identifier


@pytest.mark.parametrize("in_block", [False, True])
def test_rows_named_at_create_are_made(scene, tmp_path, in_block) -> None:
    a, b = _layer(tmp_path, "a", "/a"), _layer(tmp_path, "b", "/b")
    # Row 1 first: the order the names come in does not matter.
    values = {"imports.1.sublayer_path": b, "imports.0.sublayer_path": a}
    if in_block:
        with chunk.change_block():
            sub = scene.create.sublayer(**values)
    else:
        sub = scene.create.sublayer(**values)
    scene.set_output(sub)

    rows = sub.get_param("imports")
    assert rows.size == 2
    assert [rows.get_indexed_param("sublayer_path", i).value for i in range(2)] == [a, b]
    stage = scene.get_stage()
    assert stage.GetPrimAtPath("/a") and stage.GetPrimAtPath("/b")


def test_set_adds_a_row(scene, tmp_path) -> None:
    a, b = _layer(tmp_path, "a", "/a"), _layer(tmp_path, "b", "/b")
    sub = scene.create.sublayer(**{"imports.0.sublayer_path": a})
    scene.set_output(sub)

    scene.set(sub, **{"imports.1.sublayer_path": b})

    assert sub.get_param("imports").size == 2
    assert scene.get_stage().GetPrimAtPath("/b")


@pytest.mark.parametrize("in_block", [False, True])
def test_a_list_of_values_sets_one_param_rows(scene, tmp_path, in_block) -> None:
    a, b = _layer(tmp_path, "a", "/a"), _layer(tmp_path, "b", "/b")
    if in_block:
        with chunk.change_block():
            sub = scene.create.sublayer(imports=[a, b])
    else:
        sub = scene.create.sublayer(imports=[a, b])
    scene.set_output(sub)

    rows = sub.get_param("imports")
    assert [rows.get_indexed_param("sublayer_path", i).value for i in range(rows.size)] == [a, b]
    stage = scene.get_stage()
    assert stage.GetPrimAtPath("/a") and stage.GetPrimAtPath("/b")


def test_a_list_replaces_every_row(scene, tmp_path) -> None:
    a, b, c = (_layer(tmp_path, n, f"/{n}") for n in "abc")
    sub = scene.create.sublayer(imports=[a, b, c])
    scene.set_output(sub)

    scene.set(sub, imports=[c])

    rows = sub.get_param("imports")
    assert rows.size == 1
    assert rows.get_indexed_param("sublayer_path", 0).value == c
    assert not scene.get_stage().GetPrimAtPath("/a")


def test_dict_rows_set_their_params_and_default_the_rest(scene) -> None:
    scatter = scene.create.scatter_points(prototypes=[
        {"proto_path": "/rock", "proto_weight": 20.0},
        {"proto_path": "/bush"},
    ])
    rows = scatter.get_param("prototypes")

    def row(i, name):
        return rows.get_indexed_param(name, i).value

    assert rows.size == 2
    assert (row(0, "proto_path"), row(0, "proto_weight")) == ("/rock", 20.0)
    assert (row(1, "proto_path"), row(1, "proto_weight")) == ("/bush", 100.0)

    # Given again, a row left out of a dict is back at its default, not what it was.
    scene.set(scatter, prototypes=[{"proto_path": "/rock"}])
    assert rows.size == 1
    assert row(0, "proto_weight") == 100.0


def test_a_bare_value_for_a_row_of_several_params_raises(scene) -> None:
    with pytest.raises(TypeError, match="give each row as a dict"):
        scene.create.scatter_points(prototypes=["/rock"])


def test_a_misspelt_key_in_a_row_raises(scene) -> None:
    with pytest.raises(KeyError, match="prototypes.0.proto_pth"):
        scene.create.scatter_points(prototypes=[{"proto_pth": "/rock"}])


def test_a_misspelt_row_param_raises_and_adds_no_row(scene) -> None:
    sub = scene.create.sublayer()

    with pytest.raises(KeyError, match="imports.0.sublayer_pth"):
        scene.set(sub, **{"imports.0.sublayer_pth": "a.usda"})

    assert sub.get_param("imports").size == 0
