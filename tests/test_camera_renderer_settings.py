"""Renderer camera settings on the Camera chunk -- V-Ray's physical camera and the like.

A renderer that declares its camera settings as an API schema applying only to cameras
(VRayCameraAPI) gets a group on the Camera chunk, built and authored the way Render
Settings does it for render settings (usd_chunks.renderer_params). A renderer of the
test's own stands in, so these run whether or not one is installed.
"""
from __future__ import annotations

import importlib
import tempfile

import pytest
from pxr import Usd

from usd_chunks import chunk_registry, chunk_stage, render_defs, renderer_params
from usd_chunks.chunk_core import ChunkConfig
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import export_chunks, load_chunks
from usd_chunks.chunk_stage import initialize as init_chunk_usd
from usd_chunks.cli import _all_chunks

_PROPS = [
    render_defs.RenderSettingProp(name="fake_CameraPhysical_use", attr_name="fake:CameraPhysical_use",
                                  sdf_type="bool", default=False),
    render_defs.RenderSettingProp(name="fake_CameraPhysical_vignetting", attr_name="fake:CameraPhysical_vignetting",
                                  sdf_type="float", default=1.0),
]
_DEF = render_defs.RenderSettingsDef(
    key="FakeCameraAPI", label="Fake", namespace="fake", source="schema",
    schema_name="FakeCameraAPI", props=_PROPS,
)


class _Scene(SceneSender):
    def __init__(self):
        super().__init__()
        self._stage = None

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


def _setup():
    scene = _Scene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    stage = Usd.Stage.CreateInMemory()
    scene._stage = stage
    init_chunk_usd(stage, scene)
    return scene, stage


def _camera(scene):
    mod = importlib.import_module("chunks.modelling.camera")
    c = mod.main()
    c._config = ChunkConfig(module="camera")
    prev = chunk_stage._params_tab_create
    chunk_stage._params_tab_create = True
    try:
        scene.add_chunk(c)
    finally:
        chunk_stage._params_tab_create = prev
    return c


def _authored_schemas(prim):
    """apiSchemas as authored: GetAppliedSchemas drops a schema USD has no definition for,
    as the fake one here."""
    list_op = prim.GetMetadata("apiSchemas")
    return list(list_op.GetAddedOrExplicitItems()) if list_op else []


@pytest.fixture(autouse=True)
def _fake_renderer(monkeypatch):
    monkeypatch.setattr(render_defs, "iter_camera_defs", lambda: iter([_DEF]))


def test_each_renderer_gets_a_group():
    scene, _stage = _setup()
    camera = _camera(scene)

    assert "Fake" in {p.name for p in camera.get_my_params()}


def test_an_untouched_renderer_leaves_no_trace_on_the_camera():
    scene, stage = _setup()
    camera = _camera(scene)
    camera.execute()

    prim = stage.GetPrimAtPath(camera.get_param("primpath_location").value)
    assert "FakeCameraAPI" not in _authored_schemas(prim)
    assert not prim.GetAttribute("fake:CameraPhysical_use")


def test_a_changed_setting_is_authored_with_its_schema():
    scene, stage = _setup()
    camera = _camera(scene)
    renderer_params.populate_groups_holding(camera, {"fake_CameraPhysical_use"})
    camera.get_param("fake_CameraPhysical_use").value = True
    camera.execute()

    prim = stage.GetPrimAtPath(camera.get_param("primpath_location").value)
    assert prim.GetAttribute("fake:CameraPhysical_use").Get() is True
    assert "FakeCameraAPI" in _authored_schemas(prim)
    # Left at its default, so not written.
    assert not prim.GetAttribute("fake:CameraPhysical_vignetting")


def test_a_saved_setting_comes_back_and_is_authored_on_load():
    scene, _stage = _setup()
    camera = _camera(scene)
    renderer_params.populate_groups_holding(camera, {"fake_CameraPhysical_vignetting"})
    camera.get_param("fake_CameraPhysical_vignetting").value = 0.0
    camera.execute()
    prim_path = camera.get_param("primpath_location").value

    net = tempfile.mkdtemp() + "/net.usda"
    export_chunks(scene, net)
    chunk_registry.ChunkLayerManager().reset()

    stage2 = Usd.Stage.Open(net)
    scene2 = _Scene()
    chunk_registry.ChunkRegistry().initialize(scene2, "pytest")
    scene2._stage = stage2
    init_chunk_usd(stage2, scene2)
    load_chunks(stage2, scene2)

    reloaded = next(c for c in _all_chunks(scene2.get_chunks()) if c.get_param("lock_to_freecam") is not None)
    assert reloaded.get_param("fake_CameraPhysical_vignetting").value == 0.0
    assert stage2.GetPrimAtPath(prim_path).GetAttribute("fake:CameraPhysical_vignetting").Get() == 0.0
