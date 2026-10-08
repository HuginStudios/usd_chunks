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
