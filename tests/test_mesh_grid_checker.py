"""A new Grid or Ground has no material; UV Checker binds the checker texture on request.

Every grid bound a UV checker material whose texture was an absolute path into the
usd_chunks checkout, so a delivered ground pointed at a file on the author's machine. In an
agent benchmark, three agents out of three wrote a plane chunk of their own rather than use
Ground for that reason.
"""
from __future__ import annotations

from pxr import Sdf, Usd, UsdGeom, UsdShade

from usd_chunks import Scene

CHECKER = Sdf.Path("/materials/UVCheckerMaterial")


def _bound(stage, path="/World/ground"):
    material, _ = UsdShade.MaterialBindingAPI(stage.GetPrimAtPath(path)).ComputeBoundMaterial()
    return material.GetPath() if material else None


def test_a_new_ground_has_no_material() -> None:
    with Scene() as scene:
        scene.set_output(scene.create.ground(prim_path="/World/ground"))
        stage = scene.get_stage()
        assert stage.GetPrimAtPath("/World/ground").IsValid()
        assert _bound(stage) is None
        assert not stage.GetPrimAtPath(CHECKER).IsValid()


def test_a_plane_is_one_quad_sized_like_any_grid() -> None:
    """Plane: the word people search for a flat quad by; agents wrote plane chunks of their own."""
    with Scene() as scene:
        scene.set_output(scene.create.plane(prim_path="/World/floor", width1=10.0, height2=4.0))
        mesh = UsdGeom.Mesh(scene.get_stage().GetPrimAtPath("/World/floor"))
        assert list(mesh.GetFaceVertexCountsAttr().Get()) == [4]
        lo, hi = mesh.GetExtentAttr().Get() or UsdGeom.Boundable.ComputeExtentFromPlugins(mesh, 0)
        assert (hi[0] - lo[0], hi[2] - lo[2]) == (10.0, 4.0)
        assert _bound(scene.get_stage(), "/World/floor") is None


def test_a_cube_with_one_point_per_edge_is_still_a_box() -> None:
    """dimx/dimy/dimz count points along an edge, so 1 wrote no prim at all, silently."""
    with Scene() as scene:
        scene.set_output(scene.create.cube(prim_path="/World/box", dimx=1, dimy=1, dimz=1))
        mesh = UsdGeom.Mesh(scene.get_stage().GetPrimAtPath("/World/box"))
        assert mesh, "no prim written"
        assert len(mesh.GetPointsAttr().Get()) == 8


def test_uv_checker_binds_the_checker() -> None:
    with Scene() as scene:
        ground = scene.create.ground(prim_path="/World/ground")
        scene.set_output(ground)
        scene.set(ground, grid_checker=True)
        assert _bound(scene.get_stage()) == CHECKER


def test_a_grid_saved_before_the_option_keeps_its_checker(tmp_path) -> None:
    """Its file has no grid_checker opinion, as every grid saved before it existed."""
    path = str(tmp_path / "old.usda")
    with Scene() as scene:
        scene.set_output(scene.create.ground(prim_path="/World/ground"))
        scene.save(path)
    layer = Sdf.Layer.FindOrOpen(path)
    stage = Usd.Stage.Open(layer)
    attrs = [a for p in stage.Traverse() for a in p.GetAttributes() if a.GetName().endswith("grid_checker")]
    assert attrs, "precondition: a new grid saves its checker as off"
    for attr in attrs:
        attr.GetPrim().RemoveProperty(attr.GetName())
    layer.Save()

    with Scene.open(path) as scene:
        assert _bound(scene.get_stage()) == CHECKER, "an old grid lost its checker on reopening"
