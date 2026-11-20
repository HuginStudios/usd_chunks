"""Styles: a saved network put on an object in one go, from the Styles menu.

* the styles in chunks/styles are found, with their pictures and summaries, and each is a
  menu entry under Styles that takes the prim selected now as its Location;
* a style works on the object it is put on, whatever its prim paths say: /crate stays a
  mesh, the style cooks once it is wired, and a Scatter's prototype rows resolve under it;
* the same style on two objects doesn't collide, and vines stay on a moved object.
"""
from __future__ import annotations

import os
import shutil

import numpy as np
import pytest
from pxr import UsdGeom, UsdShade, Vt

from tests.polyhaven_fake import fake_polyhaven  # noqa: F401 -- autouse: no downloads
from usd_chunks import Scene, chunk_registry, styles


def _grid_steps(stage, path, offset=0.0, n=41, size=2.0, rise=0.25):
    """Two steps as one dense grid mesh: an inner corner at the foot of the riser."""
    xs = np.linspace(0.0, size, n)
    gx, gz = np.meshgrid(xs, xs, indexing="ij")
    height = np.where(gz < size / 2, rise, 0.0)
    pts = np.stack([gx.ravel() + offset, height.ravel(), gz.ravel()], axis=1)
    idx = [v for i in range(n - 1) for j in range(n - 1)
           for v in (i * n + j, i * n + j + 1, (i + 1) * n + j + 1, (i + 1) * n + j)]
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(pts.astype(np.float32)))
    mesh.CreateFaceVertexCountsAttr([4] * ((n - 1) ** 2))
    mesh.CreateFaceVertexIndicesAttr(idx)
    return mesh


def _primvar(stage, path, name):
    pv = UsdGeom.PrimvarsAPI(stage.GetPrimAtPath(path)).GetPrimvar(name)
    return np.asarray(pv.Get()) if pv and pv.HasValue() else None


def test_the_styles_are_found_with_their_pictures_and_summaries():
    found = {s.name: s for s in styles.find_styles()}

    assert {"worn_paint", "dirty_stone", "rubble", "hanging_vines", "concrete_debris", "bushy_nature",
            "forest_hideout", "scrap_yard", "abandoned_cottage", "overgrown_ruin", "ruins_ground", "forest_floor",
            "asphalt_lot"} <= set(found)
    worn = found["worn_paint"]
    assert worn.label == "Worn Paint"
    assert worn.thumbnail is not None and worn.thumbnail.name == "worn_paint.png"
    assert worn.summary.startswith("Paint worn off the edges")


def test_a_style_folder_on_the_variable_comes_first(tmp_path, monkeypatch):
    own = {s.name: s for s in styles.find_styles()}["rubble"].path
    shutil.copy(own, tmp_path / "rubble.usda")
    monkeypatch.setenv(styles.STYLE_DIRS_ENV, str(tmp_path))

    rubble = {s.name: s for s in styles.find_styles()}["rubble"]

    assert rubble.path == tmp_path / "rubble.usda"
    assert rubble.thumbnail is None, "no picture beside this copy"


def test_each_style_is_a_menu_entry_under_styles():
    with Scene():
        entries = {e.namespace: e for e in chunk_registry.ChunkRegistry().get_menu_entries()}

    entry = entries[("Styles", "Worn Paint")]
    assert entry.module.module_name == "reference"
    assert entry.presets["file_path"].endswith("worn_paint.usda")


def test_a_style_entry_takes_the_prim_selected_now_as_its_location():
    with Scene() as scene:
        stage = scene.get_stage()
        UsdGeom.Mesh.Define(stage, "/crate")
        entry = next(e for e in chunk_registry.ChunkRegistry().get_menu_entries()
                     if e.namespace == ("Styles", "Worn Paint"))

        scene.get_selected_prims = lambda: [stage.GetPrimAtPath("/crate")]
        assert entry.create()[0].get_param("primpath_location").value == "/crate"

        scene.get_selected_prims = lambda: [stage.GetPseudoRoot()]
        assert entry.create()[0].get_param("primpath_location").value == "/worn_paint",             "nothing picked: a place of its own, not the whole stage"


def test_a_style_with_nothing_picked_makes_an_empty_place_of_its_own():
    with Scene() as scene:
        stage = scene.get_stage()
        scene.create.rubble()

        assert stage.GetPrimAtPath("/rubble").IsValid()
        scatter = next(c for c in scene.get_chunks() if c.name == "Scatter")
        assert scatter.get_param("inst_path").value == "/rubble/style_rubble"


def test_worn_paint_wears_the_mesh_it_is_put_on():
    with Scene() as scene:
        stage = scene.get_stage()
        crate = scene.create.cube(prim_path="/crate", dimx=21, dimy=21, dimz=21, width=2.0, height=2.0, depth=2.0)
        scene.create.worn_paint(crate, primpath_location="/crate")

        prim = stage.GetPrimAtPath("/crate")
        assert prim.GetTypeName() == "Mesh", "the Location is not redefined as an Xform"
        wear = _primvar(stage, "/crate", "wear")
        assert wear is not None and 0.0 < (wear > 0).mean() < 0.6, "worn on the edges, not all over"
        assert UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()[0].GetPath() == "/materials/worn_paint"


def test_the_same_style_on_two_objects_does_not_collide():
    with Scene() as scene:
        stage = scene.get_stage()
        _grid_steps(stage, "/a")
        _grid_steps(stage, "/b", offset=5.0)
        first = scene.create.rubble(primpath_location="/a")
        scene.create.rubble(first, primpath_location="/b")

        for root, low, high in (("/a", 0.0, 2.0), ("/b", 5.0, 7.0)):
            stones = UsdGeom.PointInstancer(stage.GetPrimAtPath(f"{root}/style_rubble"))
            assert stones, f"{root} has its own rubble"
            positions = np.asarray(stones.GetPositionsAttr().Get())
            assert len(positions) > 10
            assert ((positions[:, 0] >= low - 0.1) & (positions[:, 0] <= high + 0.1)).all()
            assert (np.abs(positions[:, 2] - 1.0) < 0.3).all(), "gathered at the foot of the riser"
            # What it copies is the style's own kit, found under the object: not the cube Scatter
            # draws when a prototype path names nothing.
            for target in stones.GetPrototypesRel().GetTargets():
                assert stage.GetPrimAtPath(target).GetTypeName() != "Cube"


def test_hanging_vines_stay_on_a_moved_object():
    with Scene() as scene:
        stage = scene.get_stage()
        wall = scene.create.cube(prim_path="/wall", dimx=25, dimy=19, dimz=5, width=2.0, height=1.5, depth=0.3)
        moved = scene.create.transform(wall, primpath_location="/wall", tx=-6.0)
        scene.create.hanging_vines(moved, primpath_location="/wall", growth=60.0)

        cache = UsdGeom.BBoxCache(0, ["default", "render"])
        wall_box = cache.ComputeWorldBound(stage.GetPrimAtPath("/wall")).ComputeAlignedRange()
        curves = stage.GetPrimAtPath("/wall/style_vines/Curves")
        assert curves and len(UsdGeom.BasisCurves(curves).GetPointsAttr().Get()) > 0
        vine_box = cache.ComputeWorldBound(curves).ComputeAlignedRange()
        assert vine_box.GetMin()[0] > wall_box.GetMin()[0] - 0.3
        assert vine_box.GetMax()[0] < wall_box.GetMax()[0] + 0.3, "not moved a second time"


def test_a_preset_can_be_worked_out_as_the_chunk_is_made():
    with Scene():
        module = chunk_registry.ChunkRegistry().registered_modules["edge_field"]
        seen = []

        def width(chunk):
            seen.append(chunk.name)
            return 0.5

        made = chunk_registry.MenuEntry(module, ("Edge",), presets={"width": width, "angle": lambda c: None}).create()

        assert made[0].get_param("width").value == pytest.approx(0.5)
        assert made[0].get_param("angle").value == pytest.approx(40.0), "None leaves the param alone"
        assert seen == [made[0].name]


def test_a_variant_label_from_the_top_is_its_own_menu():
    with Scene():
        entries = chunk_registry.ChunkRegistry().get_menu_entries()
        styles_at = [i for i, e in enumerate(entries) if e.namespace[:1] == ("Styles",)]
        usd_at = [i for i, e in enumerate(entries) if e.namespace[:1] == ("USD",)]

    assert styles_at and usd_at
    assert all(i > max(usd_at) for i in styles_at) or all(i < min(usd_at) for i in styles_at), \
        "sorted as a category of their own, not mixed into USD"


def test_list_rows_put_prim_paths_under_the_namespace():
    """The rows of a list read through the namespace like any prim path param."""
    with Scene() as scene:
        stage = scene.get_stage()
        _grid_steps(stage, "/steps")
        scene.create.rubble(primpath_location="/steps")
        scatter = next(c for c in scene.get_chunks() if c.name == "Scatter")
        rows = scatter.get_param("prototypes")
        row = rows.get_indexed_param(rows.get_param("proto_path"), 0)

        assert row.raw_value == "/style_kit/chunks/Prototypes/*"
        assert row.value == "/steps/style_kit/chunks/Prototypes/*"
        assert stage.GetPrimAtPath(row.value.rsplit("/", 1)[0]).IsValid(), "the pieces it copies, under the object"


def test_a_style_has_a_handle_on_the_object_for_its_width():
    """Picked in the viewport, the object shows a handle per Edge Field of its styles, named
    for what it marks; dragging it widens the band, as an override in the scene."""
    from usd_chunks.gizmo import runtime as chunk_gizmo

    with Scene() as scene:
        stage = scene.get_stage()
        _grid_steps(stage, "/steps")
        scene.create.dirty_stone(primpath_location="/steps")
        prim = stage.GetPrimAtPath("/steps")
        before = (_primvar(stage, "/steps", "dirt") > 0).mean()

        handles = {h.label: h for h in chunk_gizmo.resolve_gizmos(prim) if h.type == "cube"}
        assert {"Dirt", "Chips"} <= set(handles)
        dirt = next(c for c in scene.get_chunks() if c.name == "Dirt")
        chunk_gizmo.apply_bindings(dirt, handles["Dirt"].name, 0.5)

        assert dirt.get_param("width").value == pytest.approx(0.5)
        assert (_primvar(stage, "/steps", "dirt") > 0).mean() > before


def test_an_earlier_chunk_folder_wins_on_a_style_name(tmp_path, monkeypatch):
    for name in ("b_first", "a_second"):
        (tmp_path / name / "styles").mkdir(parents=True)
        (tmp_path / name / "styles" / "mossy.usda").write_text("#usda 1.0\n")
    monkeypatch.setenv("USD_CHUNK_DIRS", os.pathsep.join(str(tmp_path / n) for n in ("b_first", "a_second")))

    [mossy] = [s for s in styles.find_styles() if s.name == "mossy"]

    assert mossy.path.parent.parent.name == "b_first", "USD_CHUNK_DIRS order, not A-Z"


def _ground(stage, size=12.0):
    mesh = UsdGeom.Mesh.Define(stage, "/ground")
    h = size / 2
    mesh.CreatePointsAttr([(-h, 0, -h), (h, 0, -h), (h, 0, h), (-h, 0, h)])
    mesh.CreateFaceVertexCountsAttr([4])
    mesh.CreateFaceVertexIndicesAttr([0, 3, 2, 1])
    return mesh


def _positions(stage, path):
    instancer = UsdGeom.PointInstancer(stage.GetPrimAtPath(path))
    assert instancer, path
    return np.asarray(instancer.GetPositionsAttr().Get() or [])


def test_concrete_debris_piles_around_the_object_on_its_ground():
    with Scene() as scene:
        stage = scene.get_stage()
        _ground(stage)
        crate = scene.create.cube(prim_path="/crate", width=1.2, height=1.2, depth=1.2)
        scene.create.concrete_debris(crate, primpath_location="/crate", Around_ground="/ground")

        debris = _positions(stage, "/crate/style_debris")
        assert len(debris) > 50
        outside = np.maximum(np.abs(debris[:, 0]), np.abs(debris[:, 2])) - 0.6
        assert (outside > -0.2).all() and (outside < 1.9).all(), "around the crate, within reach"
        assert np.allclose(debris[:, 1], 0.0, atol=0.01), "on the ground"


def test_the_abandoned_cottage_grows_ivy_on_the_house_and_piles_plants_round_it():
    with Scene() as scene:
        stage = scene.get_stage()
        _ground(stage, 20.0)
        house = scene.create.house(house_path="/House", width=4.0, depth=3.6)
        scene.create.abandoned_cottage(house, primpath_location="/House", Around_ground="/ground")

        assert len(_positions(stage, "/House/style_vines/leaves")) > 100
        assert len(_positions(stage, "/House/style_plants")) > 50
        assert len(_positions(stage, "/House/style_debris")) > 50
        patches = next(c for c in scene.get_chunks() if c.name == "IvyPatches")
        meshes = int(patches.get_param("readout").value.split(" on ")[1].split()[0])
        assert meshes < 40, "the ivy reads the house only, not what the style makes after it"


def test_a_ground_biome_textures_the_ground_and_scatters_on_it():
    with Scene() as scene:
        stage = scene.get_stage()
        ground = scene.create.grid(prim_path="/field", width1=6.0, height2=6.0, dimx1=30, dimy1=30,
                                   grid_checker=False)
        scene.create.forest_floor(ground, primpath_location="/field")

        material = UsdShade.MaterialBindingAPI(stage.GetPrimAtPath("/field")).ComputeBoundMaterial()[0]
        assert material.GetPath() == "/materials/style_forest_leaves_02"
        assert len(_positions(stage, "/field/style_scatter")) > 20


def test_a_network_brought_in_with_prims_picked_keeps_what_it_saved():
    """A chunk loaded from USD has what was saved: a prim param left empty there is not filled
    from the viewport selection, as a chunk made new is."""
    with Scene() as scene:
        stage = scene.get_stage()
        _ground(stage)
        crate = scene.create.cube(prim_path="/crate", width=1.2, height=1.2, depth=1.2)
        scene.get_selected_prims = lambda: [stage.GetPrimAtPath("/crate"), stage.GetPrimAtPath("/ground")]
        scene.create.concrete_debris(crate, primpath_location="/crate")

        concrete = next(c for c in scene.get_chunks() if c.name == "Concrete")
        assert concrete.get_param("primpath_location").raw_value == ""
        bound = UsdShade.MaterialBindingAPI(stage.GetPrimAtPath("/ground")).ComputeBoundMaterial()[0]
        assert not bound, "the concrete look stays off the ground"
