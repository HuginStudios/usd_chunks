"""A graph cooks to its Output: what it delivers, not where someone last looked.

A view is for looking. With no active view a graph cooks to its Output, and a headless
run always does, whatever view the file was saved with. A chunk that is not upstream of
the Output doesn't run.
"""

from __future__ import annotations

import pytest
from pxr import Sdf

from network_files import run_and_read, save_cubes, save_network


@pytest.fixture
def cubes(tmp_path):
    save_cubes(tmp_path / "set.usda", "abc")
    return tmp_path


def _with_set(path, network):
    """network sublayered over the cubes, so the run has prims to move."""
    layer = Sdf.Layer.FindOrOpen(str(path))
    layer.subLayerPaths.append("./set.usda")
    layer.Save()
    return network


def test_a_run_cooks_what_feeds_the_output(cubes):
    path = cubes / "net.usda"
    _with_set(path, save_network(
        path, {"A1": ("/a", 1.0), "A2": ("/b", 2.0)}, ends=("output",), loose={"C": ("/c", 3.0)},
    ))
    assert run_and_read(path, "abc") == pytest.approx({"a": 1.0, "b": 2.0, "c": 0.0})


def test_a_run_cooks_to_the_output_not_the_active_view(cubes):
    """The file was saved looking at A1; the run still delivers the whole chain."""
    path = cubes / "net.usda"
    _with_set(path, save_network(
        path, {"A1": ("/a", 1.0), "A2": ("/b", 2.0)}, ends=("output",), view_on="A1",
    ))
    assert run_and_read(path, "ab") == pytest.approx({"a": 1.0, "b": 2.0})


def test_a_file_with_no_output_still_cooks_to_its_view(cubes):
    path = cubes / "net.usda"
    _with_set(path, save_network(
        path, {"A1": ("/a", 1.0), "A2": ("/b", 2.0)}, ends=("view",), loose={"C": ("/c", 3.0)},
    ))
    assert run_and_read(path, "abc") == pytest.approx({"a": 1.0, "b": 2.0, "c": 0.0})
