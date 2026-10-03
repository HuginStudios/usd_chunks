"""The scalar half of the schema: a shape driving one param, plus `gizmo=` sugar.

Where a transform handle hands back a whole matrix, this is the one-dimensional handle --
grab a cube, drag, one float changes. Camera focusDistance is the reference case, and it
differs from transform.py on three axes at once: its params propagate through
connect_usd_attr rather than a recompute, its path param carries no PRIM_SELECTION flag, and
its handle has a meaningful direction.
"""
from __future__ import annotations

import importlib

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom

from usd_chunks import chunk, chunk_registry
from usd_chunks.gizmo import runtime as chunk_gizmo
from usd_chunks.chunk_core import ChunkConfig, ChunkFlags
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import _CHUNKS_ROOT
from chunk_prim_authoring import _author_prim

from chunk_stubs import attach_stub_module


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


chunk_registry.ChunkRegistry().initialize(_DummyScene(), "pytest")


def _stage(path: str = "/World/Cam") -> Usd.Stage:
    stage = Usd.Stage.CreateInMemory()
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        UsdGeom.Camera.Define(stage, path)

    class _StagedScene(_DummyScene):
        def get_stage(self):
            return stage

    chunk_registry.ChunkRegistry().initialize(_StagedScene(), "pytest")
    return stage


def _camera_like(gizmos=None, target: str = "/World/Cam"):
    """camera.py's shape: flat params, and a path param with no PRIM_SELECTION flag."""
    c = chunk.Chunk("Camera")
    c.add_param(chunk.LineEdit("primpath_location", "Prim Path", default_value=target,
                               flags=ChunkFlags.ParamFlags.LABELED))
    c.add_param(chunk.Slider("focusDistance", "Focus Distance", min_value=0.0, max_value=1000.0))
    c._config = ChunkConfig(module="camera", gizmos=gizmos or [])
    # The shape, not camera.py itself: hooks come from the module a config names, so naming
    # the real camera would run its gizmo hooks. A stub module has none unless a test adds one.
    attach_stub_module(c)
    return c


def _authored(stage, c):
    _author_prim(stage, c)
    return stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/{c.name}")


# ---------------------------------------------------------------------------
# the singular driver
# ---------------------------------------------------------------------------

def test_singular_driver_authors_one_connection() -> None:
    stage = _stage()
    prim = _authored(stage, _camera_like([
        chunk.Gizmo("zdist", type="cube", driver="focusDistance"),
    ]))

    conns = prim.GetAttribute("ui:gizmo:zdist:driver").GetConnections()
    assert [str(c) for c in conns] == ["/chunks/Camera.inputs:focusDistance"]


def test_value_slot_writes_the_scalar() -> None:
    c = _camera_like([chunk.Gizmo("zdist", type="cube", driver="focusDistance")])

    chunk_gizmo.apply_bindings(c, "zdist", 42.5)

    assert c.get_param("focusDistance").value == pytest.approx(42.5)


def test_resolve_round_trips_a_scalar_gizmo() -> None:
    stage = _stage()
    prim = _authored(stage, _camera_like([
        chunk.Gizmo("zdist", type="cube", label="Focus Distance", axis="Z", driver="focusDistance"),
    ]))

    desc = chunk_gizmo.resolve_gizmos(prim)[0]
    assert desc.type == "cube"
    assert desc.label == "Focus Distance"
    assert desc.axis == "Z"
    assert len(desc.driver) == 1


# ---------------------------------------------------------------------------
# axis
# ---------------------------------------------------------------------------

def test_axis_is_optional_and_absent_means_free_drag() -> None:
    """No axis is the common case: drag any direction, only the driven param changes."""
    stage = _stage()
    prim = _authored(stage, _camera_like([
        chunk.Gizmo("zdist", type="cube", driver="focusDistance"),
    ]))

    assert not prim.GetAttribute("ui:gizmo:zdist:axis").IsValid()
    assert chunk_gizmo.resolve_gizmos(prim)[0].axis is None


def test_axis_is_a_token_not_a_vector() -> None:
    """A float3 would need recomputing whenever the anchor moves, and one authored in world
    space is simply wrong the moment the anchor rotates."""
    stage = _stage()
    prim = _authored(stage, _camera_like([
        chunk.Gizmo("zdist", type="cube", axis="Z", driver="focusDistance"),
    ]))

    attr = prim.GetAttribute("ui:gizmo:zdist:axis")
    assert attr.GetTypeName() == Sdf.ValueTypeNames.Token
    assert attr.Get() == "Z"


@pytest.mark.parametrize("axis", ["X", "-X", "Y", "-Y", "Z", "-Z", "screen"])
def test_every_axis_token_is_accepted(axis) -> None:
    assert chunk.Gizmo("g", type="cube", axis=axis, driver="focusDistance").axis == axis


def test_an_invalid_axis_fails_at_load() -> None:
    with pytest.raises(ValueError, match="unknown axis"):
        chunk.Gizmo("g", type="cube", axis="diagonal", driver="focusDistance")


# ---------------------------------------------------------------------------
# sugar
# ---------------------------------------------------------------------------

def test_shape_type_sugars_onto_a_slider() -> None:
    c = chunk.Chunk("Camera")
    c.add_param(chunk.LineEdit("primpath_location", default_value="/World/Cam"))
    c.add_param(chunk.Slider("focusDistance", "Focus Distance", gizmo="cube"))
    c._config = ChunkConfig(module="camera")

    gizmos = chunk_gizmo.get_gizmos(c)
    assert [g.name for g in gizmos] == ["focusDistance"]
    assert gizmos[0].type == "cube"
    assert gizmos[0].label == "Focus Distance"
    assert gizmos[0].driver == ("focusDistance",)


def test_sugar_authors_the_same_usd_as_the_config_form() -> None:
    """Otherwise the short form is a second, subtly different schema."""
    sugared = chunk.Chunk("Camera")
    sugared.add_param(chunk.LineEdit("primpath_location", default_value="/World/Cam"))
    sugared.add_param(chunk.Slider("focusDistance", "Focus Distance", gizmo="cube"))
    sugared._config = ChunkConfig(module="camera")

    explicit = _camera_like([
        chunk.Gizmo("focusDistance", type="cube", label="Focus Distance", driver="focusDistance"),
    ])

    a = _authored(_stage(), sugared).GetPrim().GetStage().ExportToString()
    b = _authored(_stage(), explicit).GetPrim().GetStage().ExportToString()

    def gizmo_lines(text):
        return sorted(ln.strip() for ln in text.splitlines() if "ui:gizmo" in ln)

    assert gizmo_lines(a) == gizmo_lines(b)


def test_manipulator_type_sugars_onto_a_vector() -> None:
    c = chunk.Chunk("Aimer")
    c.add_param(chunk.LineEdit("primpath_location", default_value="/World/Cam"))
    c.add_param(chunk.Vector(
        "aim", "Look At", param=chunk.Spinbox(), length=3, suffixes="XYZ",
        default_value=(0.0, 0.0, 0.0), gizmo="translate",
    ))
    c._config = ChunkConfig(module="aimer")

    gizmos = chunk_gizmo.get_gizmos(c)
    assert [g.name for g in gizmos] == ["aim"]
    # One entry: a Vector is stored as a single vector-typed attr, not as x/y/z.
    assert gizmos[0].driver == ("aim",)


def test_transform_belongs_on_the_chunk_not_on_a_param() -> None:
    """It is a whole-chunk handle built from three groups, so there is nothing for it to bind
    to on a single param or vector."""
    with pytest.raises(ValueError, match="cannot go on"):
        chunk.Slider("focusDistance", gizmo="transform")
    with pytest.raises(ValueError, match="cannot go on"):
        chunk.Vector("aim", gizmo="transform")


def test_a_manipulator_type_on_a_slider_fails_at_load() -> None:
    """Rather than silently binding one axis, which looks like a broken handle instead of a
    broken declaration."""
    with pytest.raises(ValueError, match="cannot go on a scalar param"):
        chunk.Slider("focusDistance", gizmo="translate")


def test_a_shape_type_on_a_vector_fails_at_load() -> None:
    with pytest.raises(ValueError, match="cannot go on a Vector or param group"):
        chunk.Vector("aim", gizmo="cube")


def test_an_unknown_sugar_type_fails_at_load() -> None:
    with pytest.raises(ValueError, match="cannot go on"):
        chunk.Slider("focusDistance", gizmo="wobble")


def test_params_without_sugar_declare_nothing() -> None:
    c = _camera_like()
    assert chunk_gizmo.get_gizmos(c) == []


def test_an_explicit_gizmo_wins_over_sugar_of_the_same_name() -> None:
    """The escalation path from sugar to the config form is 'write the full one', so it has
    to actually replace the short one rather than sit beside it."""
    c = chunk.Chunk("Camera")
    c.add_param(chunk.LineEdit("primpath_location", default_value="/World/Cam"))
    c.add_param(chunk.Slider("focusDistance", "Focus Distance", gizmo="cube"))
    c._config = ChunkConfig(module="camera", gizmos=[
        chunk.Gizmo("focusDistance", type="sphere", axis="Z", driver="focusDistance"),
    ])

    gizmos = chunk_gizmo.get_gizmos(c)
    assert len(gizmos) == 1
    assert gizmos[0].type == "sphere"
    assert gizmos[0].axis == "Z"


# ---------------------------------------------------------------------------
# anchor inference without the flag
# ---------------------------------------------------------------------------

def test_anchor_infers_from_the_name_without_prim_selection() -> None:
    """camera.py names primpath_location but never flags it, and roughly half of chunks/ is
    the same -- which is why the name is the primary signal rather than the flag."""
    c = _camera_like([chunk.Gizmo("zdist", type="cube", driver="focusDistance")])
    assert not c.get_param("primpath_location").has_flag(ChunkFlags.ParamFlags.PRIM_SELECTION)

    prim = _authored(_stage(), c)
    targets = prim.GetRelationship("ui:gizmo:zdist:anchor").GetTargets()
    assert [str(t) for t in targets] == ["/World/Cam"]


# ---------------------------------------------------------------------------
# the shipped camera chunk
# ---------------------------------------------------------------------------

def test_shipped_camera_declares_a_focus_distance_handle() -> None:
    camera_mod = importlib.import_module("chunks.modelling.camera")
    gizmos = camera_mod.config().gizmos

    assert [g.name for g in gizmos] == ["zdist"]
    assert gizmos[0].type == "cube"
    assert gizmos[0].driver == ("focusDistance",)


def test_shipped_camera_pins_the_handle_to_the_cameras_view_direction() -> None:
    """Signed, and the sign matters: a camera looks down -Z, so an unsigned "Z" would put the
    focus handle out the back of the camera. Found by reconstructing the handle on paper from
    the authored USD alone -- exactly what that exercise is for."""
    camera_mod = importlib.import_module("chunks.modelling.camera")
    assert camera_mod.config().gizmos[0].axis == "-Z"


# ---------------------------------------------------------------------------
# group sugar -- the lightest declaration there is
# ---------------------------------------------------------------------------

def _grouped(gizmo_type: str = "translate"):
    """A chunk shaped like transform.py: a param group that *is* the handle."""
    c = chunk.Chunk("Mover")
    c.add_param(chunk.LineEdit("primpath_location", default_value="/World/Cam"))
    grp = chunk.Group("translate_grp", "Translate", gizmo=gizmo_type)
    for axis in ("tx", "ty", "tz"):
        grp.add_param(chunk.Slider(axis, axis.upper(), default_value=0.0))
    c.add_param(grp)
    c._config = ChunkConfig(module="mover")
    return c


def test_a_group_declares_a_handle_over_its_own_params() -> None:
    """Nothing needs listing: the group already *is* the params the handle drives. This is
    the whole reason the form exists -- spelling three paths out in config() was longer than
    the rest of the config put together."""
    gizmos = chunk_gizmo.get_gizmos(_grouped())

    assert [g.name for g in gizmos] == ["translate_grp"]
    assert gizmos[0].driver == ("translate_grp.tx", "translate_grp.ty", "translate_grp.tz")


def test_group_sugar_takes_the_group_label() -> None:
    assert chunk_gizmo.get_gizmos(_grouped())[0].label == "Translate"


def test_group_sugar_authors_the_same_usd_as_the_config_form() -> None:
    explicit = chunk.Chunk("Mover")
    explicit.add_param(chunk.LineEdit("primpath_location", default_value="/World/Cam"))
    grp = chunk.Group("translate_grp", "Translate")
    for axis in ("tx", "ty", "tz"):
        grp.add_param(chunk.Slider(axis, axis.upper(), default_value=0.0))
    explicit.add_param(grp)
    explicit._config = ChunkConfig(module="mover", gizmos=[
        chunk.Gizmo("translate_grp", type="translate", label="Translate",
                    driver=("translate_grp.tx", "translate_grp.ty", "translate_grp.tz")),
    ])

    def gizmo_lines(chunk_obj):
        stage = _stage()
        _author_prim(stage, chunk_obj)
        return sorted(ln.strip() for ln in stage.ExportToString().splitlines() if "ui:gizmo" in ln)

    assert gizmo_lines(_grouped()) == gizmo_lines(explicit)


@pytest.mark.parametrize("gizmo_type", ["translate", "rotate", "scale"])
def test_every_single_slot_manipulator_sugars_onto_a_group(gizmo_type) -> None:
    assert chunk_gizmo.get_gizmos(_grouped(gizmo_type))[0].type == gizmo_type


def test_transform_is_not_a_group_type_either() -> None:
    """A group names one part of a transform, not the whole of it."""
    with pytest.raises(ValueError, match="cannot go on"):
        chunk.Group("everything", "Everything", gizmo="transform")


def test_a_shape_type_on_a_group_fails_at_load() -> None:
    with pytest.raises(ValueError, match="cannot go on a Vector or param group"):
        chunk.Group("translate_grp", gizmo="cube")


def test_an_empty_group_declares_nothing() -> None:
    """A handle driving no params is not a handle. Warn rather than author a dead one."""
    c = chunk.Chunk("Mover")
    c.add_param(chunk.LineEdit("primpath_location", default_value="/World/Cam"))
    c.add_param(chunk.Group("empty_grp", "Empty", gizmo="translate"))
    c._config = ChunkConfig(module="mover")

    assert chunk_gizmo.get_gizmos(c) == []


def test_a_group_without_the_kwarg_declares_nothing() -> None:
    c = _grouped()
    c.get_param("translate_grp").gizmo = None
    assert chunk_gizmo.get_gizmos(c) == []


# ---------------------------------------------------------------------------
# offset and size
# ---------------------------------------------------------------------------

def test_offset_and_size_are_omitted_unless_set() -> None:
    """An absent size means 'host decides', which is usually right: a viewport wants a
    screen-constant handle and only it knows the camera distance."""
    stage = _stage()
    prim = _authored(stage, _camera_like([
        chunk.Gizmo("zdist", type="cube", driver="focusDistance"),
    ]))

    assert not prim.GetAttribute("ui:gizmo:zdist:offset").IsValid()
    assert not prim.GetAttribute("ui:gizmo:zdist:size").IsValid()

    desc = chunk_gizmo.resolve_gizmos(prim)[0]
    assert desc.offset is None
    assert desc.size is None


def test_offset_and_size_round_trip() -> None:
    stage = _stage()
    prim = _authored(stage, _camera_like([
        chunk.Gizmo("zdist", type="cube", driver="focusDistance",
                    offset=(0.0, 1.5, 0.0), size=2.0),
    ]))

    desc = chunk_gizmo.resolve_gizmos(prim)[0]
    assert tuple(desc.offset) == pytest.approx((0.0, 1.5, 0.0))
    assert desc.size == pytest.approx(2.0)


def test_offset_is_authored_as_a_float3() -> None:
    stage = _stage()
    prim = _authored(stage, _camera_like([
        chunk.Gizmo("zdist", type="cube", driver="focusDistance", offset=(1, 2, 3)),
    ]))

    assert prim.GetAttribute("ui:gizmo:zdist:offset").GetTypeName() == Sdf.ValueTypeNames.Float3


def test_a_malformed_offset_fails_at_load() -> None:
    with pytest.raises(ValueError, match="three numbers"):
        chunk.Gizmo("zdist", type="cube", driver="focusDistance", offset=(1.0, 2.0))


def test_a_non_positive_size_fails_at_load() -> None:
    """Zero or negative would draw nothing or draw inside out; neither is a thing to ship."""
    with pytest.raises(ValueError, match="size must be positive"):
        chunk.Gizmo("zdist", type="cube", driver="focusDistance", size=0.0)


# ---------------------------------------------------------------------------
# the whole-chunk transform handle
# ---------------------------------------------------------------------------

def _transform_shaped(chunk_gizmo_type="transform"):
    """transform.py's shape: three gizmo= groups under a chunk that combines them."""
    c = chunk.Chunk("Transform", gizmo=chunk_gizmo_type)
    c.add_param(chunk.LineEdit("primpath_location", default_value="/World/Cam"))
    for name, axes, part in [("translate_grp", "xyz", "translate"),
                             ("rotate_grp", "xyz", "rotate"),
                             ("scale_grp", "xyz", "scale")]:
        grp = chunk.Group(name, name.title(), gizmo=part)
        for axis in axes:
            grp.add_param(chunk.Slider(f"{part[0]}{axis}", axis.upper(), default_value=0.0))
        c.add_param(grp)
    c._config = ChunkConfig(module="transform")
    return c


def test_a_chunk_level_transform_absorbs_its_groups() -> None:
    """Three handles and one combined handle over the same nine params would fight for the
    same drag, and a host has no way to know they are the same thing."""
    gizmos = chunk_gizmo.get_gizmos(_transform_shaped())

    assert [g.name for g in gizmos] == ["transform"]
    assert len(gizmos[0].driver) == 9


def test_the_driver_order_is_translate_rotate_scale() -> None:
    """Fixed, because the payload is a matrix -- there is no key to match on, only order."""
    driver = chunk_gizmo.get_gizmos(_transform_shaped())[0].driver

    assert driver[:3] == ("translate_grp.tx", "translate_grp.ty", "translate_grp.tz")
    assert driver[3:6] == ("rotate_grp.rx", "rotate_grp.ry", "rotate_grp.rz")
    assert driver[6:] == ("scale_grp.sx", "scale_grp.sy", "scale_grp.sz")


def test_without_the_chunk_kwarg_the_groups_stay_separate() -> None:
    """Opting out is leaving it off, so a chunk wanting three handles needs no new concept."""
    gizmos = chunk_gizmo.get_gizmos(_transform_shaped(chunk_gizmo_type=None))

    assert sorted(g.name for g in gizmos) == ["rotate_grp", "scale_grp", "translate_grp"]


def test_a_transform_handle_takes_a_matrix_and_writes_nine_params() -> None:
    c = _transform_shaped()
    xform = Gf.Transform()
    xform.SetTranslation(Gf.Vec3d(1.0, 2.0, 3.0))
    xform.SetScale(Gf.Vec3d(2.0, 2.0, 2.0))

    chunk_gizmo.apply_bindings(c, "transform", xform.GetMatrix())

    assert c.get_param("tx").value == pytest.approx(1.0)
    assert c.get_param("tz").value == pytest.approx(3.0)
    assert c.get_param("sy").value == pytest.approx(2.0)


def test_only_transform_may_be_declared_on_a_chunk() -> None:
    """Anything else belongs on the param or group it drives, where the params are already
    named -- a chunk-level shape type would have nothing to bind to."""
    with pytest.raises(ValueError, match="only gizmo='transform'"):
        chunk.Chunk("Nope", gizmo="cube")


def test_an_incomplete_transform_chunk_falls_back_to_its_groups() -> None:
    """Missing a group means we cannot say which nine params a matrix would fill, so the
    combined handle is declined -- but the groups that *did* declare a handle still get one.
    Degrading to a working translate handle beats dropping every handle on the chunk over a
    presentation detail."""
    c = chunk.Chunk("Half", gizmo="transform")
    c.add_param(chunk.LineEdit("primpath_location", default_value="/World/Cam"))
    grp = chunk.Group("translate_grp", "Translate", gizmo="translate")
    for axis in "xyz":
        grp.add_param(chunk.Slider(f"t{axis}", axis.upper(), default_value=0.0))
    c.add_param(grp)
    c._config = ChunkConfig(module="half")

    gizmos = chunk_gizmo.get_gizmos(c)
    assert [g.name for g in gizmos] == ["translate_grp"]
    assert gizmos[0].type == "translate"


def test_a_transform_declared_by_hand_must_drive_nine() -> None:
    with pytest.raises(ValueError, match="drives 9 params"):
        chunk.Gizmo("body", type="transform", driver=("tx", "ty", "tz"))


# ---------------------------------------------------------------------------
# the camera carries its own transform
# ---------------------------------------------------------------------------

def _camera_chunk_and_stage():
    """The real camera chunk, cooking against a stage of its own."""
    camera_mod = importlib.import_module("chunks.modelling.camera")
    stage = Usd.Stage.CreateInMemory()

    class _StagedScene(_DummyScene):
        def get_stage(self):
            return stage

    chunk_registry.ChunkRegistry().initialize(_StagedScene(), "pytest")
    c = camera_mod.main()
    # Module-level hooks like gizmo_hidden and gizmo_changed are looked up on the registered
    # module this names, so without it they are simply never found.
    c._config = ChunkConfig(module="camera", gizmos=camera_mod.config().gizmos)
    return camera_mod, c, stage


class _CamNode:
    """What execute() gets in a real session: something with a stage and params.

    Attribute reads and writes go to the chunk, because in production execute()'s node *is*
    the chunk. A wrapper that kept them to itself splits the chunk's identity in two, and the
    op-name machinery is built on that identity: the camera minted `Transform_6f1418d7`
    against the wrapper and `Camera_a8da96e6` against the chunk, both landed in xformOpOrder,
    and the camera's transform applied twice.
    """

    def __init__(self, chunk_obj, stage):
        object.__setattr__(self, "_chunk", chunk_obj)
        object.__setattr__(self, "_stage", stage)

    def get_stage(self):
        return self._stage

    def get_param(self, name):
        return self._chunk.get_param(name)

    def get_scene(self):
        return self._chunk.get_scene()

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_chunk"), name)

    def __setattr__(self, name, value):
        setattr(self._chunk, name, value)


def test_the_camera_declares_both_handles() -> None:
    """A focus-distance handle and a whole-chunk transform -- and the transform drives the camera's *own* params, so nothing reaches across chunks."""
    camera_mod, c, _stage = _camera_chunk_and_stage()

    names = sorted(g.name for g in chunk_gizmo.get_gizmos(c))
    assert names == ["transform", "zdist"]


def test_the_camera_transform_drives_its_own_params() -> None:
    camera_mod, c, _stage = _camera_chunk_and_stage()
    transform = next(g for g in chunk_gizmo.get_gizmos(c) if g.name == "transform")

    assert transform.driver[:3] == ("translate_grp.tx", "translate_grp.ty", "translate_grp.tz")
    assert all(not entry.startswith("/") for entry in transform.driver), "no cross-chunk paths"


def test_dragging_the_camera_transform_moves_the_camera_prim() -> None:
    camera_mod, c, stage = _camera_chunk_and_stage()
    c.get_param("primpath_location").value = "/cameras/cam1"
    node = _CamNode(c, stage)
    camera_mod.execute(node)  # creates the prim

    moved = Gf.Transform()
    moved.SetTranslation(Gf.Vec3d(3.0, 0.0, -4.0))
    chunk_gizmo.apply_bindings(c, "transform", moved.GetMatrix())
    camera_mod.execute(node)

    prim = stage.GetPrimAtPath(c.get_param("primpath_location").value)
    matrix = UsdGeom.Xformable(prim).GetLocalTransformation()
    assert matrix.ExtractTranslation() == Gf.Vec3d(3.0, 0.0, -4.0)


def test_the_transform_updates_when_only_the_params_change() -> None:
    """execute() short-circuits when the prim path has not moved, which is exactly the case a
    handle drag produces. Before this was handled, dragging the camera did nothing at all."""
    camera_mod, c, stage = _camera_chunk_and_stage()
    c.get_param("primpath_location").value = "/cameras/cam1"
    node = _CamNode(c, stage)
    camera_mod.execute(node)

    c.get_param("ty").value = 7.0
    camera_mod.execute(node)  # same path -- the short-circuiting run

    prim = stage.GetPrimAtPath(c.get_param("primpath_location").value)
    matrix = UsdGeom.Xformable(prim).GetLocalTransformation()
    assert matrix.ExtractTranslation()[1] == pytest.approx(7.0)


def test_the_freecam_lock_keeps_the_transform_params_out_of_it() -> None:
    """While the lock is on the freecam re-authors the transform on every CAMERA_CHANGED, so
    a param write here would be silently overwritten a frame later. Not writing is the honest
    behaviour; the handle suppression itself is still an open question."""
    camera_mod, c, stage = _camera_chunk_and_stage()
    c.get_param("primpath_location").value = "/cameras/cam1"
    node = _CamNode(c, stage)
    camera_mod.execute(node)

    c.get_param("lock_to_freecam").value = True
    c.get_param("tx").value = 99.0
    camera_mod.execute(node)

    prim = stage.GetPrimAtPath(c.get_param("primpath_location").value)
    matrix = UsdGeom.Xformable(prim).GetLocalTransformation()
    assert matrix.ExtractTranslation()[0] != pytest.approx(99.0)


def test_the_camera_composes_transforms_the_same_way_transform_py_does() -> None:
    """Both chunks must invert to the same decomposition, or one handle drifts and the other
    does not -- which would look like a gizmo bug rather than a chunk bug."""
    camera_mod, c, _stage = _camera_chunk_and_stage()
    transform_mod = importlib.import_module("chunks.transforms.transform")

    for name, value in [("tx", 1.0), ("ty", -2.0), ("rx", 30.0), ("ry", 15.0), ("sz", 2.0)]:
        c.get_param(name).value = value

    t = transform_mod.main()
    for name, value in [("tx", 1.0), ("ty", -2.0), ("rx", 30.0), ("ry", 15.0), ("sz", 2.0)]:
        t.get_param(name).value = value

    from_camera = camera_mod._camera_matrix(c)
    expected = chunk_gizmo.decompose(from_camera)

    assert expected["translate"] == pytest.approx(Gf.Vec3d(1.0, -2.0, 0.0))
    assert expected["rotate"] == pytest.approx(Gf.Vec3d(30.0, 15.0, 0.0), abs=1e-6)


def test_an_unsigned_and_a_signed_axis_are_different_declarations() -> None:
    """Both valid, opposite meanings. The schema has to be able to say which."""
    assert chunk.Gizmo("g", type="cube", axis="Z", driver="focusDistance").axis == "Z"
    assert chunk.Gizmo("g", type="cube", axis="-Z", driver="focusDistance").axis == "-Z"


# ---------------------------------------------------------------------------
# a chunk hiding its own handle
# ---------------------------------------------------------------------------

def test_the_camera_hides_its_transform_handle_under_the_freecam_lock() -> None:
    """The freecam re-authors the camera transform on every CAMERA_CHANGED, so a drag would
    be overwritten a frame later. A handle that looks draggable and silently snaps back is
    worse than no handle."""
    camera_mod, c, stage = _camera_chunk_and_stage()
    c.get_param("primpath_location").value = "/cameras/cam1"
    _author_prim(stage, c)
    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/Camera")

    assert "transform" in [d.name for d in chunk_gizmo.resolve_gizmos(prim)]

    c.get_param("lock_to_freecam").value = True
    chunk_gizmo.refresh_gizmo_anchors(stage, prim, c)

    assert "transform" not in [d.name for d in chunk_gizmo.resolve_gizmos(prim)]


def test_the_focus_handle_survives_the_freecam_lock() -> None:
    """The lock only seeds focusDistance once, when the prim has no value yet, so that
    handle is never fighting anyone."""
    camera_mod, c, stage = _camera_chunk_and_stage()
    c.get_param("primpath_location").value = "/cameras/cam1"
    _author_prim(stage, c)
    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/Camera")

    c.get_param("lock_to_freecam").value = True
    chunk_gizmo.refresh_gizmo_anchors(stage, prim, c)

    assert "zdist" in [d.name for d in chunk_gizmo.resolve_gizmos(prim)]


def test_unticking_the_lock_brings_the_handle_back() -> None:
    """A flag that only ever goes one way would strand the handle forever."""
    camera_mod, c, stage = _camera_chunk_and_stage()
    c.get_param("primpath_location").value = "/cameras/cam1"
    _author_prim(stage, c)
    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/Camera")

    c.get_param("lock_to_freecam").value = True
    chunk_gizmo.refresh_gizmo_anchors(stage, prim, c)
    c.get_param("lock_to_freecam").value = False
    chunk_gizmo.refresh_gizmo_anchors(stage, prim, c)

    assert "transform" in [d.name for d in chunk_gizmo.resolve_gizmos(prim)]


def test_suppression_is_visible_in_the_usd_not_only_through_resolve() -> None:
    """So a host doing pure-USD discovery honours it too, rather than drawing a handle we
    have decided should not exist right now."""
    camera_mod, c, stage = _camera_chunk_and_stage()
    c.get_param("primpath_location").value = "/cameras/cam1"
    _author_prim(stage, c)
    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/Camera")

    c.get_param("lock_to_freecam").value = True
    chunk_gizmo.refresh_gizmo_anchors(stage, prim, c)

    assert prim.GetAttribute("ui:gizmo:transform:hidden").Get() is True


def test_a_chunk_without_the_hook_hides_nothing() -> None:
    c = _camera_like([chunk.Gizmo("zdist", type="cube", driver="focusDistance")])
    assert chunk_gizmo.is_gizmo_hidden(c, "zdist") is False


def test_a_failing_hook_leaves_the_handle_visible() -> None:
    """A visible handle that misbehaves is easier to diagnose than one that silently never
    appears at all."""

    c = _camera_like([chunk.Gizmo("zdist", type="cube", driver="focusDistance")])

    def gizmo_hidden(node, gizmo):
        raise RuntimeError("boom")

    attach_stub_module(c, gizmo_hidden=gizmo_hidden)

    assert chunk_gizmo.is_gizmo_hidden(c, "zdist") is False


def _fly_freecam_to(camera_mod, chunk_obj, stage, path, translate):
    """Place the prim the way `_apply_freecam_transform` does, rather than reimplementing it.

    It used to call MakeMatrixXform() here, which is what the freecam itself used to do --
    but that resets xformOpOrder to a lone `xformOp:transform`, so once the camera owned a
    named op the simulation stopped matching the thing it was simulating. The test then
    described a state the code could no longer produce, and failed for that instead of for a
    real regression. Migration from a genuine legacy op has its own test below.
    """
    matrix = Gf.Matrix4d().SetTranslate(Gf.Vec3d(*translate))
    # Recorded on the chunk as well, as the real callback does: the framework clears the
    # camera's layer before every cook, so the flight is kept on the chunk, not the prim.
    setattr(chunk_obj, camera_mod._FLOWN_MATRIX, matrix)
    camera_mod._author_matrix(chunk_obj, stage, stage.GetPrimAtPath(path), matrix)


def _cook(camera_mod, node):
    """Cook the way a session does: from a cleared layer.

    execute() is called directly in these tests, so nothing else clears the layer the camera
    authors into -- the stage's root here. The framework clears a chunk's layer before every
    cook, and a test that skipped it would find last cook's prim in the way of this one.
    """
    node.get_stage().GetRootLayer().Clear()
    camera_mod.execute(node)


def test_unlocking_the_freecam_leaves_the_camera_where_it_was_flown() -> None:
    """The regression that giving the camera its own transform introduced.

    While locked, the freecam places the prim and our params sit at their defaults. Unticking
    the lock used to author those defaults straight over it, snapping the camera to the
    origin -- losing a position you just flew to is a bad way to discover that two systems
    disagree about who owns the transform.
    """
    camera_mod, c, stage = _camera_chunk_and_stage()
    c.get_param("primpath_location").value = "/cameras/cam1"
    node = _CamNode(c, stage)
    _cook(camera_mod, node)

    # The freecam flies the camera somewhere, exactly as _apply_freecam_transform does.
    c.get_param("lock_to_freecam").value = True
    path = c.get_param("primpath_location").value
    _fly_freecam_to(camera_mod, c, stage, path, Gf.Vec3d(100.0, 50.0, -200.0))
    _cook(camera_mod, node)

    c.get_param("lock_to_freecam").value = False
    _cook(camera_mod, node)

    # Re-fetch: prim handles expire across the layer.Clear() inside execute().
    matrix = UsdGeom.Xformable(stage.GetPrimAtPath(path)).GetLocalTransformation()
    assert matrix.ExtractTranslation() == Gf.Vec3d(100.0, 50.0, -200.0)


def test_unlocking_also_seeds_the_params_so_the_handle_picks_up_from_there() -> None:
    """Staying put is only half of it -- the params have to agree, or the first drag would
    jump the camera back to the origin instead of moving it from where it is."""
    camera_mod, c, stage = _camera_chunk_and_stage()
    c.get_param("primpath_location").value = "/cameras/cam1"
    node = _CamNode(c, stage)
    _cook(camera_mod, node)

    c.get_param("lock_to_freecam").value = True
    path = c.get_param("primpath_location").value
    _fly_freecam_to(camera_mod, c, stage, path, Gf.Vec3d(100.0, 50.0, -200.0))
    _cook(camera_mod, node)

    c.get_param("lock_to_freecam").value = False
    _cook(camera_mod, node)

    assert c.get_param("tx").value == pytest.approx(100.0)
    assert c.get_param("tz").value == pytest.approx(-200.0)



def test_a_cook_survives_an_order_naming_an_op_nothing_defines() -> None:
    """The crash this authoring change exists for, reproduced on a camera prim.

    `xformOpOrder` can name an op that no layer defines with a type -- ordinary leftover state
    after a chunk is deleted and re-added, since the order and the attribute live in different
    layers and go away independently. `GetOrderedXformOps()` silently skips it and hands back
    an empty list, and `AddTransformOp()` then raises because the name *is* already in the
    order:

        The xformOp 'xformOp:transform' already exists in xformOpOrder [[xformOp:transform]].

    25.05 fails differently and also fails. Authoring the attribute outright sidesteps both.
    """
    camera_mod, c, stage = _camera_chunk_and_stage()
    c.get_param("primpath_location").value = "/cameras/cam1"
    node = _CamNode(c, stage)
    camera_mod.execute(node)
    path = c.get_param("primpath_location").value

    # The leftover state, established explicitly rather than left to whatever the cook
    # happened to author -- the op is *named* by the order and *defined* by nothing. In a
    # layer of its own, as it is in a session: the camera's own layer is cleared before every
    # cook, so the order that outlives the attribute is always somebody else's.
    prim = stage.GetPrimAtPath(path)
    prim.RemoveProperty(camera_mod._LEGACY_OP)
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        UsdGeom.Xformable(prim).GetXformOpOrderAttr().Set([camera_mod._LEGACY_OP])

    _cook(camera_mod, node)   # must not raise

    assert c.get_param("primpath_location").value == path

def test_a_camera_saved_before_named_ops_keeps_its_placement() -> None:
    """Migration, and the doubling it could cause.

    A camera authored before this chunk owned a named op carries a plain `xformOp:transform`
    holding its placement. The first cook has to adopt that into the params rather than
    author identity over it -- and must drop it from the order afterwards, or the camera's
    transform applies twice: once from the legacy op and once from the named one the params
    now write. That doubling is what turned a scale of 100 into 10000 in transform.py.
    """
    camera_mod, c, stage = _camera_chunk_and_stage()
    # Only a layer the framework does not clear can still hold a legacy op when a cook starts
    # -- a context's file layer. The stage's root stands in for it, and the chunk is told it
    # is its own, as it would be.
    c.get_edit_context = stage.GetRootLayer
    c.get_param("primpath_location").value = "/cameras/cam1"
    node = _CamNode(c, stage)
    camera_mod.execute(node)
    path = c.get_param("primpath_location").value

    # Rewind to how an older version of this chunk left the prim: the shared op holding the
    # placement, no named op of ours at all.
    prim = stage.GetPrimAtPath(path)
    named = camera_mod.chunk_utils.existing_xform_op_name(c)
    prim.GetAttribute(named).Clear()
    UsdGeom.Xformable(prim).MakeMatrixXform().Set(
        Gf.Matrix4d().SetTranslate(Gf.Vec3d(100.0, 50.0, -200.0)))

    camera_mod.execute(node)

    prim = stage.GetPrimAtPath(path)
    order = list(prim.GetAttribute("xformOpOrder").Get() or [])
    assert camera_mod._LEGACY_OP not in order, "the legacy op would apply the placement twice"
    assert UsdGeom.Xformable(prim).GetLocalTransformation().ExtractTranslation() ==         Gf.Vec3d(100.0, 50.0, -200.0)
    assert c.get_param("tx").value == pytest.approx(100.0)


def test_a_legacy_op_this_chunk_did_not_author_is_left_alone() -> None:
    """Only an op in *this chunk's* layer is reclaimed. A camera that genuinely ships with an
    `xformOp:transform` -- from a reference or a sublayer -- keeps it, and keeps its place at
    the front of the order.

    Tested against the rule directly rather than through a cook: execute() renames a path that
    already resolves, so pointing the chunk at a shipped camera makes it create a second one
    beside it, and the test ends up describing that instead.
    """
    camera_mod, c, stage = _camera_chunk_and_stage()
    asset = Sdf.Layer.CreateAnonymous("asset.usda")
    stage.GetRootLayer().subLayerPaths = [asset.identifier]
    with Usd.EditContext(stage, asset):
        cam = UsdGeom.Camera.Define(stage, "/cameras/shipped")
        UsdGeom.Xformable(cam).MakeMatrixXform().Set(
            Gf.Matrix4d().SetTranslate(Gf.Vec3d(7.0, 0.0, 0.0)))

    prim = stage.GetPrimAtPath("/cameras/shipped")
    order = [camera_mod._LEGACY_OP, "xformOp:transform:Camera_deadbeef"]

    # The edit target is the root layer, which holds no opinion on that op.
    assert camera_mod._without_legacy_op(c, stage, prim, order) == order
    assert prim.GetAttribute(camera_mod._LEGACY_OP).Get() is not None, "not ours to clear"



def test_unlocking_hands_back_the_flight_even_after_the_camera_was_moved() -> None:
    """Reported live. Move the camera with the gizmo, lock the freecam, fly somewhere, unlock
    -- and the camera jumped back to where the gizmo left it, throwing the flight away.

    Adopting only while every param sat at its default was protecting a "real edit" that is
    stale by definition: while the lock is on, the freecam *is* the authority. So the handover
    is edge-triggered on the lock coming off, not gated on the params.
    """
    camera_mod, c, stage = _camera_chunk_and_stage()
    c.get_param("primpath_location").value = "/cameras/cam1"
    node = _CamNode(c, stage)
    _cook(camera_mod, node)
    path = c.get_param("primpath_location").value

    # A real edit first, which is what used to disable the handover.
    moved = Gf.Transform()
    moved.SetTranslation(Gf.Vec3d(3.0, 0.0, -4.0))
    chunk_gizmo.apply_bindings(c, "transform", moved.GetMatrix())
    _cook(camera_mod, node)

    c.get_param("lock_to_freecam").value = True
    _fly_freecam_to(camera_mod, c, stage, path, Gf.Vec3d(100.0, 50.0, -200.0))
    _cook(camera_mod, node)

    c.get_param("lock_to_freecam").value = False
    _cook(camera_mod, node)

    assert UsdGeom.Xformable(stage.GetPrimAtPath(path)).GetLocalTransformation()         .ExtractTranslation() == Gf.Vec3d(100.0, 50.0, -200.0)
    assert c.get_param("tx").value == pytest.approx(100.0), "the params have to agree too"

    # And it fires once per lock, not on every cook afterwards.
    c.get_param("tx").value = 7.0
    _cook(camera_mod, node)
    assert c.get_param("tx").value == pytest.approx(7.0)


def test_the_freecam_lock_constrains_placement_but_not_scale() -> None:
    """Reported live: scale the camera, tick Lock to Free Camera, and the scale vanishes from
    the viewport while the sliders go on reading what you set.

    The freecam is a viewpoint. It says where the camera is and which way it looks, and has no
    scale worth having -- taking its matrix whole silently replaced the camera's scale with 1.
    Worst of both: the object snapped back to unscaled AND the params disagreed with it.
    """
    camera_mod, c, stage = _camera_chunk_and_stage()
    c.get_param("primpath_location").value = "/cameras/cam1"
    node = _CamNode(c, stage)
    camera_mod.execute(node)
    path = c.get_param("primpath_location").value

    for name in ("sx", "sy", "sz"):
        c.get_param(name).value = 5.0
    camera_mod.execute(node)

    c.get_param("lock_to_freecam").value = True
    camera_mod._author_matrix(
        c, stage, stage.GetPrimAtPath(path),
        camera_mod._with_local_scale(c, Gf.Matrix4d().SetTranslate(Gf.Vec3d(100.0, 50.0, -200.0))),
    )

    placed = Gf.Transform(UsdGeom.Xformable(stage.GetPrimAtPath(path)).GetLocalTransformation())
    assert placed.GetTranslation() == Gf.Vec3d(100.0, 50.0, -200.0), "the freecam still places it"
    assert placed.GetScale() == pytest.approx(Gf.Vec3d(5.0, 5.0, 5.0)), "and the scale is ours"

    # And it survives the handover, rather than coming back as 1 through adopt.
    c.get_param("lock_to_freecam").value = False
    camera_mod.execute(node)
    assert c.get_param("sx").value == pytest.approx(5.0)


def test_an_unscaled_camera_takes_the_freecam_matrix_untouched() -> None:
    """The common case must not go the long way round and pick up float noise."""
    camera_mod, c, _stage = _camera_chunk_and_stage()
    matrix = Gf.Matrix4d().SetTranslate(Gf.Vec3d(1.0, 2.0, 3.0))

    assert camera_mod._with_local_scale(c, matrix) == matrix

def test_adopting_never_overwrites_a_real_edit() -> None:
    """It only fires while every param is still at its default, so a user who has typed a
    position keeps it even if the prim carries something else."""
    camera_mod, c, stage = _camera_chunk_and_stage()
    c.get_param("primpath_location").value = "/cameras/cam1"
    node = _CamNode(c, stage)
    camera_mod.execute(node)

    c.get_param("ty").value = 5.0
    path = c.get_param("primpath_location").value
    _fly_freecam_to(camera_mod, c, stage, path, Gf.Vec3d(100.0, 50.0, -200.0))
    camera_mod.execute(node)

    assert c.get_param("ty").value == pytest.approx(5.0)
