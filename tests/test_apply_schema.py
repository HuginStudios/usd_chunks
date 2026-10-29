"""Apply Schema put down before a prim is picked.

Its schema menu is filled from the picked prim by a callback that also runs when the chunk
is created. With nothing picked it indexed an empty list, so every new Apply Schema logged
an IndexError traceback.
"""
from __future__ import annotations

import logging

import pytest
from pxr import Usd, UsdGeom

from usd_chunks.chunk_script import Scene


@pytest.fixture
def scene():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/geo")
    s = Scene(stage, "pytest")
    yield s
    s.close()


def test_putting_it_down_with_no_prim_logs_no_error(scene, caplog):
    with caplog.at_level(logging.ERROR):
        chunk = scene.add("apply_schema")

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], caplog.text
    assert list(chunk.get_param("schemas").menu_items) == []


def test_picking_a_prim_lists_the_schemas_it_takes(scene):
    chunk = scene.add("apply_schema")

    chunk.get_param("prim").value = "/geo"

    assert "UsdGeomModelAPI" in chunk.get_param("schemas").menu_items


def test_a_schema_set_from_a_script_is_the_one_applied(scene):
    """Live-reported: given "PhysicsRigidBodyAPI" (the name a .usda shows), the menu swapped
    it for its first entry and the prim got ColorSpaceAPI instead, with nothing said."""
    UsdGeom.Mesh.Define(scene.get_stage(), "/geo/box")
    chunk = scene.add("apply_schema", prim="/geo/box", schemas="PhysicsRigidBodyAPI")

    assert chunk.get_param("schemas").value == "PhysicsRigidBodyAPI"
    assert list(scene.get_stage().GetPrimAtPath("/geo/box").GetAppliedSchemas()) == ["PhysicsRigidBodyAPI"]


def test_a_name_that_is_no_schema_applies_nothing_and_says_so(scene, caplog):
    UsdGeom.Mesh.Define(scene.get_stage(), "/geo/box")
    with caplog.at_level(logging.WARNING):
        scene.add("apply_schema", prim="/geo/box", schemas="RigidBodyAPI")

    assert list(scene.get_stage().GetPrimAtPath("/geo/box").GetAppliedSchemas()) == []
    assert "no API schema called 'RigidBodyAPI'" in caplog.text
