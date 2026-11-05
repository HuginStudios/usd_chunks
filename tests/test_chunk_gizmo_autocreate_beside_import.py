"""A Transform made by dragging a prim goes right after the chunk that brings the prim in.

At the end of a long chain it moved the asset after everything that reads it: a chunk
after the import kept seeing the asset where it was.
"""
from __future__ import annotations

from pxr import Sdf, Usd, UsdGeom

from usd_chunks import chunk, chunk_stage
from usd_chunks.chunk_registry import ChunkLayerManager
from usd_chunks.gizmo import runtime as chunk_gizmo

from chunk_stubs import attach_stub_module
from test_chunk_gizmo_autocreate_edges import _prim, _sources, gathering, scene  # noqa: F401


def _chunk(registry, name):
    c = chunk.Chunk(name)
    attach_stub_module(c)
    registry.scene.add_chunk(c)
    return c, chunk_stage.get_chunk_prim_path(c)


def _chain(stage, registry, *paths):
    for upstream, downstream in zip(paths, paths[1:]):
        chunk_stage.connect_exec(stage, registry.scene, upstream, downstream)


def _define_in(stage, c, *prims):
    with Usd.EditContext(stage, ChunkLayerManager().get_layer(c)):
        for path in prims:
            UsdGeom.Xform.Define(stage, path)


def _import_then_reader(stage, registry, *prims):
    """Head -> Import (defines prims) -> Reader: an import early in a chain."""
    _head, head = _chunk(registry, "Head")
    importer, imp = _chunk(registry, "Import")
    _reader, reader = _chunk(registry, "Reader")
    _chain(stage, registry, head, imp, reader)
    _define_in(stage, importer, *prims)
    return importer, imp, reader


def _dragged(stage, path):
    return str(chunk_gizmo.ensure_transform_chunk(stage.GetPrimAtPath(path)).chunk_prim)


def test_the_transform_goes_right_after_the_import(scene) -> None:  # noqa: F811
    stage, registry, _module = scene
    _importer, imp, reader = _import_then_reader(stage, registry, "/Asset/Car")

    moved = _dragged(stage, "/Asset/Car")

    assert _sources(stage, moved) == [imp]
    assert _sources(stage, reader) == [moved], "the reader now sees the asset moved"


def test_a_second_drag_from_the_same_import_gathers_beside_it(gathering) -> None:  # noqa: F811
    stage, registry, _module, _head = gathering
    _importer, imp, reader = _import_then_reader(stage, registry, "/Asset/Car", "/Asset/Truck")

    car = _dragged(stage, "/Asset/Car")
    truck = _dragged(stage, "/Asset/Truck")

    merges = [p for p in stage.Traverse() if chunk_stage.is_merge_prim(p)]
    assert len(merges) == 1
    merge = str(merges[0].GetPath())
    assert _sources(stage, merge) == [truck, car, imp]
    assert _sources(stage, reader) == [merge]


def test_a_third_drag_joins_that_merge(gathering) -> None:  # noqa: F811
    stage, registry, _module, _head = gathering
    _importer, imp, reader = _import_then_reader(stage, registry, "/Asset/A", "/Asset/B", "/Asset/C")

    paths = [_dragged(stage, f"/Asset/{n}") for n in "ABC"]

    (merge,) = [str(p.GetPath()) for p in stage.Traverse() if chunk_stage.is_merge_prim(p)]
    assert _sources(stage, merge) == [*reversed(paths), imp]
    assert _sources(stage, reader) == [merge]


def test_a_part_of_a_referenced_asset_goes_after_the_reference(scene) -> None:  # noqa: F811
    stage, registry, _module = scene
    asset = Sdf.Layer.CreateAnonymous("car.usda")
    asset_stage = Usd.Stage.Open(asset)
    UsdGeom.Xform.Define(asset_stage, "/Car")
    UsdGeom.Xform.Define(asset_stage, "/Car/Body")
    asset.defaultPrim = "Car"
    importer, imp, reader = _import_then_reader(stage, registry)
    with Usd.EditContext(stage, ChunkLayerManager().get_layer(importer)):
        stage.DefinePrim("/Asset/Car").GetReferences().AddReference(asset.identifier)

    moved = _dragged(stage, "/Asset/Car/Body")

    assert _sources(stage, moved) == [imp]
    assert _sources(stage, reader) == [moved]


def test_a_prim_from_a_sublayered_file_goes_after_the_sublayer(scene) -> None:  # noqa: F811
    stage, registry, _module = scene
    rocks = Sdf.Layer.CreateAnonymous("rocks.usda")
    UsdGeom.Xform.Define(Usd.Stage.Open(rocks), "/Rocks/Rock1")
    importer, imp, reader = _import_then_reader(stage, registry)
    ChunkLayerManager().get_layer(importer).subLayerPaths.append(rocks.identifier)

    moved = _dragged(stage, "/Rocks/Rock1")

    assert _sources(stage, moved) == [imp]
    assert _sources(stage, reader) == [moved]


def test_a_prim_in_the_scenes_own_file_still_goes_at_the_end(scene) -> None:  # noqa: F811
    """/World/A is defined in the scene file, not by a chunk: nothing to sit beside."""
    stage, registry, _module = scene
    _importer, _imp, reader = _import_then_reader(stage, registry, "/Asset/Car")

    moved = _dragged(stage, "/World/A")

    assert _sources(stage, moved) == [reader]


def test_an_over_on_the_prim_does_not_count_as_bringing_it_in(scene) -> None:  # noqa: F811
    """A chunk that only changes the prim (a material binding, say) is not its import."""
    stage, registry, _module = scene
    importer, imp, reader = _import_then_reader(stage, registry, "/Asset/Car")
    looks, _ = _chunk(registry, "Looks")
    with Usd.EditContext(stage, ChunkLayerManager().get_layer(looks)):
        stage.OverridePrim("/Asset/Car").CreateAttribute("look", Sdf.ValueTypeNames.Int).Set(1)

    moved = _dragged(stage, "/Asset/Car")

    assert _sources(stage, moved) == [imp]
