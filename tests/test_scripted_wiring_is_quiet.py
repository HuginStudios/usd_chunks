"""A chunk a script makes wired below another says nothing about not being wired.

scene.create cooks a new chunk once before it wires it, so that first cook finds nothing
above it: a Transform of a prim a Sublayer brings in was "blocked: /World/box is made by
Sublayer, which isn't wired above Transform", and an Edit Target after a Layer "reads prims
that Layer edits, but isn't wired above it" -- in a correctly wired chain, whose cook once
wired is fine. A benchmark agent took them for real faults and rebuilt its network again and
again. They are held back until the chunk is wired; one that is not wired still says so.
"""
from __future__ import annotations

import logging

import pytest
from pxr import Sdf, UsdGeom

from usd_chunks import Scene, chunk


@pytest.fixture
def set_file(tmp_path):
    layer = Sdf.Layer.CreateNew(str(tmp_path / "set.usda"))
    world = Sdf.CreatePrimInLayer(layer, "/World")
    world.specifier, world.typeName = Sdf.SpecifierDef, "Xform"
    box = Sdf.CreatePrimInLayer(layer, "/World/box")
    box.specifier, box.typeName = Sdf.SpecifierDef, "Cube"
    layer.Save()
    return layer.identifier


def _wiring_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "wired above" in r.getMessage() or "is blocked" in r.getMessage()]


@pytest.mark.parametrize("in_block", [False, True])
def test_the_department_layer_recipe_builds_without_warnings(set_file, caplog, in_block) -> None:
    caplog.set_level(logging.WARNING)

    def build(scene):
        last = scene.create.sublayer(imports=[set_file])
        for dept in ("layout", "lighting"):
            last = scene.create.layer(last, layer_name=dept)
            last = scene.create.edit_target(last, edit_target=dept)
            if dept == "layout":
                last = scene.create.transform(last, primpath_location="/World/box", tx=3.0)
            else:
                last = scene.create.distant_light(last, primpath_location="/World/key")
            last = scene.create.edit_target(last, edit_target="Session Layer")
        return last

    with Scene.new() as scene:
        if in_block:
            with chunk.change_block():
                last = build(scene)
        else:
            last = build(scene)
        scene.set_output(last)
        box = UsdGeom.Xformable(scene.get_stage().GetPrimAtPath("/World/box"))
        assert box.ComputeLocalToWorldTransform(0).ExtractTranslation()[0] == pytest.approx(3.0)

    assert _wiring_warnings(caplog) == []


def test_a_chunk_that_is_not_wired_still_says_so(set_file, caplog) -> None:
    caplog.set_level(logging.WARNING)
    with Scene.new() as scene:
        sub = scene.create.sublayer(imports=[set_file])
        lone = scene.create.transform(primpath_location="/World/box", tx=3.0)  # a branch of its own
        scene.set_output(scene.create.merge(sub, lone))

    assert any("Transform is blocked" in m and "Sublayer" in m for m in _wiring_warnings(caplog))
