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


def test_a_misspelt_row_param_raises_and_adds_no_row(scene) -> None:
    sub = scene.create.sublayer()

    with pytest.raises(KeyError, match="imports.0.sublayer_pth"):
        scene.set(sub, **{"imports.0.sublayer_pth": "a.usda"})

    assert sub.get_param("imports").size == 0
