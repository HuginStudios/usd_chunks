"""A shot of department layers and a referenced network, opened in the Qt graph as usdview does.

layout.usda (A1 -> A2 -> Output) and lighting.usda (B1 -> Output, and a reference to the
house network at /World/house/chunks), sublayered by shot.usda. Headless this cooked; in
the graph it opened on the house's tab -- the last one added -- and focusing a group mutes
everything outside it, so only the house showed. It also got two tabs.
"""

from __future__ import annotations

import pytest
from pxr import Sdf, Usd, UsdGeom

from network_files import save_cubes, save_network
from qt_wait import pump_until
from usd_chunks import chunk_registry, chunk_stage
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.frontends.qt.factory import ChunkWidgetDocker


def _z(stage, name):
    xform = UsdGeom.Xformable(stage.GetPrimAtPath(f"/{name}"))
    return xform.ComputeLocalToWorldTransform(Usd.TimeCode.Default()).ExtractTranslation()[2]


@pytest.fixture(params=["panel_after_load", "panel_before_load"])
def opened(request, qapp, tmp_path):
    save_cubes(tmp_path / "set.usda", "abcd")
    save_network(tmp_path / "house.usda", {"H1": ("/d", 4.0)}, ends=("output",))
    save_network(tmp_path / "layout.usda", {"A1": ("/a", 1.0), "A2": ("/b", 2.0)}, ends=("output",))
    save_network(tmp_path / "lighting.usda", {"B1": ("/c", 3.0)}, ends=("output",))
    light = Sdf.Layer.FindOrOpen(str(tmp_path / "lighting.usda"))
    spec = Sdf.CreatePrimInLayer(light, "/World/house/chunks")
    spec.specifier = Sdf.SpecifierDef
    spec.referenceList.Prepend(Sdf.Reference("./house.usda", "/chunks"))
    for path in ("/World", "/World/house"):
        light.GetPrimAtPath(path).specifier = Sdf.SpecifierDef
    light.Save()
    shot = Sdf.Layer.CreateNew(str(tmp_path / "shot.usda"))
    shot.subLayerPaths[:] = ["./lighting.usda", "./layout.usda", "./set.usda"]
    shot.Save()

    stage = Usd.Stage.Open(str(tmp_path / "shot.usda"))

    class _Scene(SceneSender):
        load_cooks_active_views_only = True  # as usdview

        def get_stage(self):
            return stage

        def get_selected_prims(self):
            return []

    scene = _Scene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    # usdview builds the panel first and loads the shot into it; tabs then arrive one by
    # one during the load instead of all at once when the panel is built.
    if request.param == "panel_before_load":
        docker = ChunkWidgetDocker()
        chunk_stage.initialize(stage, scene)
    else:
        chunk_stage.initialize(stage, scene)
        docker = ChunkWidgetDocker()
    docker.resize(800, 600)
    docker.show()
    pump_until(qapp, lambda: False, seconds=1.5)
    yield qapp, stage, scene, docker
    docker.close()
    docker.deleteLater()


def test_every_cube_moves_on_open(opened):
    _, stage, _, _ = opened
    assert [_z(stage, n) for n in "abcd"] == pytest.approx([1.0, 2.0, 3.0, 4.0])


def test_it_opens_on_the_shots_own_graph_with_one_tab_per_group(opened):
    _, _, scene, docker = opened
    tabs = docker.tab_widget
    contexts = [tabs.widget(i).ctx() for i in range(tabs.count())]
    assert len({id(c) for c in contexts}) == len(contexts), "a group got two tabs"
    assert docker._current_context() is scene.get_root_context()


def test_a_live_edit_inside_the_referenced_network_cooks(opened):
    qapp, stage, scene, _ = opened
    h1 = next(c for c in scene.get_chunks() if c.name == "H1")
    h1.get_param("tz").value = 6.0
    assert pump_until(qapp, lambda: abs(_z(stage, "d") - 6.0) < 1e-6), _z(stage, "d")


def test_a_live_edit_in_layout_cooks(opened):
    qapp, stage, scene, _ = opened
    a1 = next(c for c in scene.get_chunks() if c.name == "A1")
    a1.get_param("tz").value = 9.0
    assert pump_until(qapp, lambda: abs(_z(stage, "a") - 9.0) < 1e-6), _z(stage, "a")
