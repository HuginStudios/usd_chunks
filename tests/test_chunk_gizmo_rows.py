"""Handles that come and go with a chunk's params: one per row of a list.

A module's `gizmos(node)` hook (gizmo.runtime.GIZMOS_HOOK) returns handles worked out from
the chunk as it is now, and a driver entry can name one row of a list -- `doors.u[2]`. A
row added gets its handle; a row removed loses it.
"""
from __future__ import annotations

import pytest
from chunk_stubs import register_stub_module

from usd_chunks import Scene, chunk
from usd_chunks.gizmo import runtime as chunk_gizmo


def _doors_main():
    c = chunk.Chunk("Doors")
    c.add_param(chunk.LineEdit("primpath_location", "Location", default_value="/World/Wall"))
    doors = chunk.ListGroup("doors", "Doors")
    doors.add_param(chunk.Slider("u", "U", default_value=0.0, min_value=0.0, max_value=20.0))
    c.add_param(doors)
    return c


def _gizmos(node):
    rows = node.get_param("doors").size
    return [chunk.Gizmo(f"door_{i}", type="cube", axis="X", driver=f"doors.u[{i}]") for i in range(rows)]


def _execute(node):
    node.get_stage().DefinePrim("/World/Wall", "Xform")


@pytest.fixture
def doors():
    with Scene() as scene:
        module = register_stub_module(main=_doors_main, gizmos=_gizmos, execute=_execute)
        (c,) = module.load()
        scene.add_chunk(c)
        yield scene, c


def _prim(scene, c):
    return scene.get_stage().GetPrimAtPath(f"/World/Wall")


def _handles(scene, c):
    return sorted(h.name for h in chunk_gizmo.resolve_gizmos(_prim(scene, c)) if h.chunk_prim)


def test_each_row_gets_a_handle(doors):
    scene, c = doors
    c.get_param("doors").size = 3

    assert [g.name for g in chunk_gizmo.get_gizmos(c)] == ["door_0", "door_1", "door_2"]
    assert _handles(scene, c) == ["door_0", "door_1", "door_2"]


def test_a_handle_drives_its_own_row(doors):
    scene, c = doors
    lst = c.get_param("doors")
    lst.size = 3

    chunk_gizmo.apply_bindings(c, "door_1", 7.5)

    assert [lst.get_indexed_param("u", i).value for i in range(3)] == [0.0, 7.5, 0.0]


def test_a_rows_handle_connects_to_its_column(doors):
    scene, c = doors
    c.get_param("doors").size = 2

    (handle,) = [h for h in chunk_gizmo.resolve_gizmos(_prim(scene, c)) if h.name == "door_1"]
    assert [str(p).rsplit(".", 1)[-1] for p in handle.driver] == ["inputs:doors:u"]


def test_a_removed_rows_handle_goes(doors):
    scene, c = doors
    lst = c.get_param("doors")
    lst.size = 3

    lst.size = 1

    assert _handles(scene, c) == ["door_0"]
    chunk_prim = scene.get_stage().GetPrimAtPath(chunk_gizmo.resolve_gizmos(_prim(scene, c))[0].chunk_prim)
    assert not [p for p in chunk_prim.GetPropertyNames() if p.startswith("ui:gizmo:door_2")]


def test_every_row_removed_takes_every_handle(doors):
    scene, c = doors
    lst = c.get_param("doors")
    lst.size = 2
    chunk_prim_path = chunk_gizmo.resolve_gizmos(_prim(scene, c))[0].chunk_prim

    lst.size = 0

    chunk_prim = scene.get_stage().GetPrimAtPath(chunk_prim_path)
    assert not [p for p in chunk_prim.GetPropertyNames() if p.startswith("ui:gizmo:")]


def test_a_row_entry_that_names_no_row_is_a_declaration_error(doors):
    _scene, c = doors
    c.get_param("doors").size = 1

    with pytest.raises(ValueError, match="no param"):
        chunk_gizmo._param_for_entry(c, "doors.u[5]")
