"""AMD MaterialX Library: a cook from upstream doesn't redo the download and assign."""
from __future__ import annotations

import pytest
from pxr import Usd, UsdGeom

from usd_chunks import chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import initialize as _chunk_usd_init

_MTLX_STANDIN = """#usda 1.0
def Scope "MaterialX" {
    def Scope "Materials" {
        def Material "Chrome" {}
    }
}
"""


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture
def library(tmp_path, monkeypatch):
    """A library node with Chrome applied to /cube, and the list of package fetches."""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Cube.Define(stage, "/cube")
    UsdGeom.Cube.Define(stage, "/ball")
    scene = _Scene(stage)
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)

    module = reg.get_chunk_module("AMDMaterialXLibrary")
    made = module.create() if module.loaded else module.load()
    live = module.module  # the copy the registry cooks with
    # No network: no catalog, and a local stand-in for the downloaded package.
    monkeypatch.setattr(live, "_load_catalog", lambda node: None)
    standin = tmp_path / "chrome.usda"
    standin.write_text(_MTLX_STANDIN)
    fetches = []

    def fetch(title, resolution, cache_dir):
        fetches.append(title)
        return str(standin).replace("\\", "/"), {"mtlx_material_name": "Chrome"}, None

    monkeypatch.setattr(live, "_fetch_package", fetch)
    for c in made:
        scene.add_chunk(c)
    node = made[0]
    node.get_param("target_primpath").value = "/cube"
    node.get_param("picked_title").value = "Chrome"
    live.execute(node)
    assert fetches, "the first apply fetches the package"
    return live, node, fetches


def test_a_cook_with_nothing_new_does_not_apply_again(library):
    live, node, fetches = library
    before = len(fetches)
    status = node.get_param("status").value

    live.execute(node)  # what a cook from upstream runs, e.g. after the cube moved

    assert len(fetches) == before
    assert node.get_param("status").value == status


def test_a_new_target_applies_again(library):
    live, node, fetches = library
    before = len(fetches)

    node.get_param("target_primpath").value = "/ball"
    live.execute(node)

    assert len(fetches) > before
    bind = live._child_of_type(node, "bind_material")
    assert bind.get_param("primpath_location").value == "/ball"
