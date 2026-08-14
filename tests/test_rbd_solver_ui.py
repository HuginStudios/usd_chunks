"""The RBD Solver's parameter surface, and the Wind/Turbulence forces driving it."""
from __future__ import annotations

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom

from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins.chunk_usd import initialize as _chunk_usd_init

import scripts.rbd_solver as rbd_mod


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


@pytest.fixture
def solver():
    chunk_registry.ChunkRegistry().initialize(_DummyScene(), "pytest")
    return rbd_mod.main()


# ---------------------------------------------------------------------------
# Parameter surface
# ---------------------------------------------------------------------------


def test_every_section_starts_collapsed(solver):
    """The solver has ~30 params. Showing them all meant scrolling past a wall of
    spinboxes to reach Run, so each section opens folded."""
    groups = [p for p in solver.get_my_params() if isinstance(p, chunk.Group)]
    assert groups, "the solver should organise its params into sections"
    expanded = [g.name for g in groups if not g.has_state(chunk.ChunkFlags.State.COLLAPSED)]
    assert expanded == [], f"these sections start expanded: {expanded}"


def test_the_essentials_stay_out_of_the_folded_sections(solver):
    """Target Prim and Run must not be buried inside a section the user has to open."""
    top_level = {p.name for p in solver.get_my_params() if not isinstance(p, chunk.Group)}
    assert {"prim_path", "run_sim", "reset_sim", "auto_sim"} <= top_level


def test_collision_shape_defaults_to_convex_hull(solver):
    """Convex Hull is the shape the body actually looks like. Sphere was only the default
    because it was the one shape that stayed put on a heightfield collider -- which turned
    out to be a defect in how those colliders were built, not a property of the hull."""
    shape = solver.get_param("shape_type")
    assert shape.value == "Convex Hull"
    assert "Convex Hull" in shape.menu_items


@pytest.mark.parametrize(
    "name", ["initial_velocity", "angular_velocity", "offset_translate", "offset_rotate", "wind"]
)
def test_spatial_params_are_real_three_component_vectors(solver, name):
    """Velocity used to be a single text box you typed "0 0 0" into, and the offsets were
    six loose spinboxes. Anything with an X/Y/Z meaning gets a proper vector widget."""
    param = solver.get_param(name)
    assert isinstance(param, chunk.Vector), f"{name} should be a Vector, got {type(param).__name__}"
    assert param.length == 3
    assert tuple(param.value) == (0.0, 0.0, 0.0)


@pytest.mark.parametrize("name", ["friction", "restitution"])
def test_zero_to_one_params_are_sliders(solver, name):
    """Friction and Bounce are physically bounded to 0..1, so they get a slider you can
    feel rather than a spinbox you have to guess sensible numbers for."""
    param = solver.get_param(name)
    assert isinstance(param, chunk.Slider)
    assert (param.min_value, param.max_value) == (0.0, 1.0)


def test_the_forces_section_exists_with_its_controls(solver):
    forces = next(p for p in solver.get_my_params() if isinstance(p, chunk.Group) and p.name == "Forces")
    assert {p.name for p in forces.get_my_params()} == {
        "wind",
        "turbulence",
        "turb_scale",
        "turb_speed",
        "turb_seed",
    }


# ---------------------------------------------------------------------------
# Forces
# ---------------------------------------------------------------------------

if not rbd_mod._HAS_MUJOCO:
    pytest.skip("mujoco not installed in this environment", allow_module_level=True)


def _drop(**forces):
    """Drop a unit cube from a height with no colliders, and return where it ends up."""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)

    mesh = UsdGeom.Mesh.Define(stage, "/ball")
    mesh.GetPointsAttr().Set(
        [(-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1), (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)]
    )
    mesh.GetFaceVertexCountsAttr().Set([4] * 6)
    mesh.GetFaceVertexIndicesAttr().Set([0, 1, 2, 3, 4, 5, 6, 7, 0, 4, 5, 1, 1, 5, 6, 2, 2, 6, 7, 3, 3, 7, 4, 0])
    UsdGeom.Xformable(mesh).AddTranslateOp().Set(Gf.Vec3d(0, 0, 50))

    layer = Sdf.Layer.CreateAnonymous("scratch")
    stage.GetRootLayer().subLayerPaths.insert(0, layer.identifier)

    mass = forces.pop("mass", 1.0)
    mats = rbd_mod.simulate_mujoco(
        stage,
        stage.GetPrimAtPath("/ball"),
        layer,
        frame_count=48,
        fps=24,
        gravity=-981.0,
        mass=mass,
        substeps=8,
        init_vel=[0, 0, 0],
        ang_vel=[0, 0, 0],
        start_frame=1,
        parent_world_inv=Gf.Matrix4d(1.0),
        otx=0, oty=0, otz=0, orx=0, ory=0, orz=0,
        collision_prims=None,
        shape_type="Convex Hull",
        **forces,
    )
    assert mats
    t = mats[-1].ExtractTranslation()
    return (round(float(t[0]), 2), round(float(t[1]), 2), round(float(t[2]), 2))


def test_wind_pushes_the_body_along_its_own_axis():
    still = _drop()
    blown = _drop(wind=(300.0, 0.0, 0.0))
    assert blown[0] > still[0] + 10.0, f"wind did not carry the body: {still} -> {blown}"


def test_wind_is_mass_independent():
    """Wind is applied as an ACCELERATION (scaled by each body's mass), so tuning Mass
    must not silently retune every force in the scene."""
    light = _drop(wind=(300.0, 0.0, 0.0), mass=1.0)
    heavy = _drop(wind=(300.0, 0.0, 0.0), mass=25.0)
    assert light[0] == pytest.approx(heavy[0], abs=0.5), (
        f"a 25x heavier body blew a different distance: {light[0]} vs {heavy[0]}"
    )


def test_turbulence_disturbs_an_otherwise_identical_drop():
    still = _drop()
    gusty = _drop(turbulence=400.0, turb_scale=30.0, turb_speed=3.0, turb_seed=1)
    assert gusty != still


def test_the_same_seed_reproduces_the_same_take():
    """A re-run has to reproduce the take that was just approved."""
    kw = dict(turbulence=400.0, turb_scale=30.0, turb_speed=3.0, turb_seed=1)
    assert _drop(**kw) == _drop(**kw)


def test_a_different_seed_gives_a_different_take():
    kw = dict(turbulence=400.0, turb_scale=30.0, turb_speed=3.0)
    assert _drop(turb_seed=1, **kw) != _drop(turb_seed=7, **kw)


def test_turbulence_left_at_zero_changes_nothing():
    """The whole Forces section is opt-in -- an untouched solver must simulate exactly as
    it did before the section existed."""
    assert _drop(turbulence=0.0, turb_scale=30.0, turb_speed=3.0, turb_seed=5) == _drop()
