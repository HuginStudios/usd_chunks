"""Scatter Points says when its Density Primvar gives it nothing to go on.

* a name the surface doesn't carry: it places evenly, and says so;
* a field that is 0 everywhere (Edge Field on a flat grid): it places nothing, and says why,
  rather than pointing at the surface mesh and prototype paths, which are fine.
"""

from __future__ import annotations

import pytest
from pxr import Usd

from usd_chunks import chunk
from usd_chunks.chunk_script import Scene


@pytest.fixture
def scene():
    s = Scene(Usd.Stage.CreateInMemory(), "pytest")
    yield s
    s.close()


def _scatter(scene, density_primvar, field="edge_field", primvar="edges"):
    grid = scene.add("mesh", mesh_type="grid", prim_path="/ground", dimx1=10, dimy1=10)
    field = scene.add(field, grid, prim_path="/ground", primvar=primvar)
    proto = scene.add("mesh", field, mesh_type="cube", prim_path="/pebble", width=0.03, height=0.03, depth=0.03)
    node = scene.add("scatter_points", proto, surface_prim="/ground", inst_path="/pebbles", seed=1)
    rows = node.get_param("prototypes", chunk.is_inst(chunk.ListGroup))
    rows.get_indexed_param(rows.get_param("proto_path"), 0).value = "/pebble"
    node.get_param("density_primvar").value = density_primvar
    return node


def test_a_density_primvar_the_surface_lacks_is_named(scene):
    node = _scatter(scene, "nosuch")
    readout = node.get_param("readout").value
    assert node.get_param("out_count").value > 0
    assert "Density Primvar 'nosuch' isn't on the surface: placed evenly" in readout


def test_a_density_field_of_zero_says_so_instead_of_blaming_the_paths(scene):
    # A flat grid has no sharp edges, so Edge Field writes 0 on every point.
    node = _scatter(scene, "edges")
    readout = node.get_param("readout").value
    assert node.get_param("out_count").value == 0
    assert "Density Primvar 'edges' is 0 everywhere: nothing to place" in readout
    assert "check the surface mesh and prototype paths" not in readout


def test_no_note_when_the_field_has_weight_or_none_is_named(scene):
    node = _scatter(scene, "", field="noise_field", primvar="density")
    assert "Density Primvar" not in node.get_param("readout").value
    node.get_param("density_primvar").value = "density"
    assert node.get_param("out_count").value > 0
    assert "Density Primvar" not in node.get_param("readout").value
