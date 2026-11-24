"""A new chunk fills only its first prim field from the selection, and only with a prim it takes.

Reported from Blender: an Atmosphere made with the cliff selected took the cliff as its sun and
as its sky camera, so there was no sun and no sky. Every PRIM_SELECTION field filled itself
from the selection as main() built it. The same seeding is in every host: a Scatter Points
made with the ground selected also used it as Grow Near and as its prototype, an Inherit
inherited from itself, a Render Settings took a mesh for its camera.
"""

from __future__ import annotations

import pytest
from pxr import Usd, UsdGeom

from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import initialize as chunk_usd_initialize

ROCK = "/World/rock"
CAMERA = "/World/cam"


@pytest.fixture
def make():
    """make(module, selected=[paths]) -> a new chunk of that module, made with those prims selected."""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Mesh.Define(stage, ROCK)
    UsdGeom.Camera.Define(stage, CAMERA)
    selection: list[str] = []

    class _Scene(SceneSender):
        def get_stage(self):
            return stage

        def get_selected_prims(self):
            return [stage.GetPrimAtPath(p) for p in selection]

    scene = _Scene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_usd_initialize(stage, scene)

    def build(module: str, selected=(ROCK,)):
        selection[:] = list(selected)
        mod = chunk_registry.ChunkRegistry().get_chunk_module(module)
        if not mod.loaded:
            mod.load()
        return mod.create()[0]

    return build


def _value(c, name):
    return c.get_param(name).value


def test_scatter_points_takes_the_selection_as_its_surface_only(make) -> None:
    c = make("scatter_points")

    assert _value(c, "surface_prim") == ROCK
    assert not _value(c, "near_prims")
    rows = c.get_param("prototypes", chunk.is_inst(chunk.ListGroup))
    assert rows.get_indexed_param("proto_path", 0).value == ""


def test_a_prototype_row_added_later_starts_empty(make) -> None:
    rows = make("scatter_points").get_param("prototypes", chunk.is_inst(chunk.ListGroup))

    rows.size = rows.size + 1

    assert [rows.get_indexed_param("proto_path", i).value for i in range(rows.size)] == ["", ""]


def test_an_inherit_does_not_inherit_from_itself(make) -> None:
    c = make("inherit")

    assert _value(c, "prim_path") == ROCK
    assert _value(c, "base_path") != ROCK


def test_a_collection_does_not_exclude_what_it_includes(make) -> None:
    c = make("collection")

    assert _value(c, "include_pattern") == ROCK
    assert not _value(c, "exclude_pattern")


def test_a_camera_field_passes_over_a_mesh(make) -> None:
    c = make("render_settings")

    assert _value(c, "camera") == c.get_param("camera").default_value


def test_a_camera_field_takes_a_camera(make) -> None:
    c = make("render_settings", selected=[CAMERA])

    assert _value(c, "camera") == CAMERA


def test_the_next_field_gets_what_a_typed_field_passed_over(make) -> None:
    """Frustum Prune's first field is its camera; with a mesh selected, the mesh is what to prune."""
    c = make("frustum_prune")

    assert not _value(c, "camera")
    assert _value(c, "prim_path") == ROCK
    assert not _value(c, "keep")


def test_nothing_selected_seeds_nothing(make) -> None:
    c = make("scatter_points", selected=[])

    assert not _value(c, "surface_prim")


def test_a_typed_field_accepts_an_applied_api_schema() -> None:
    stage = Usd.Stage.CreateInMemory()
    prim = stage.DefinePrim("/World/thing", "Xform")
    prim.ApplyAPI("CollectionAPI", "items")
    field = chunk.LineEdit("target", "Target", prim_types=("CollectionAPI",))

    assert field.accepts_prim(prim)
    assert not field.accepts_prim(stage.DefinePrim("/World/other", "Xform"))
