"""The scalar half of the schema: a shape driving one param, plus `gizmo=` sugar.

Where the transform gizmo is a multi-slot manipulator, this is the one-dimensional handle --
grab a cube, drag, one float changes. Camera focusDistance is the reference case, and it
differs from transform.py on three axes at once: its params propagate through
connect_usd_attr rather than a recompute, its path param carries no PRIM_SELECTION flag, and
its handle has a meaningful direction.
"""
from __future__ import annotations

import importlib

import pytest
from pxr import Sdf, Usd, UsdGeom

from usd_chunks import chunk, chunk_gizmo, chunk_registry
from usd_chunks.chunk_core import ChunkConfig, ChunkFlags, SceneSender
from usd_chunks.plugins.chunk_usd import _CHUNKS_ROOT, _author_prim


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

    chunk_gizmo.apply_bindings(c, "zdist", {"value": 42.5})

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
    assert set(desc.driver) == {"value"}


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


@pytest.mark.parametrize("axis", ["X", "Y", "Z", "screen"])
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
    assert gizmos[0].driver == {"value": ("focusDistance",)}


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
    assert gizmos[0].driver == {"translate": ("aim",)}


def test_transform_cannot_be_sugared_anywhere() -> None:
    """It needs several slots at once, so there is nothing sensible for sugar to bind."""
    with pytest.raises(ValueError, match="cannot be sugared"):
        chunk.Slider("focusDistance", gizmo="transform")
    with pytest.raises(ValueError, match="cannot be sugared"):
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
    """camera.py names primpath_location but never flags it, and roughly half of scripts/ is
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
    camera_mod = importlib.import_module("scripts.camera")
    gizmos = camera_mod.config().gizmos

    assert [g.name for g in gizmos] == ["zdist"]
    assert gizmos[0].type == "cube"
    assert gizmos[0].driver == {"value": ("focusDistance",)}


def test_shipped_camera_pins_the_handle_to_the_camera_axis() -> None:
    """Focus distance runs along -Z; a handle that slid in any direction would lose the one
    thing the number means."""
    camera_mod = importlib.import_module("scripts.camera")
    assert camera_mod.config().gizmos[0].axis == "Z"


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
    assert gizmos[0].driver == {"translate": ("translate_grp.tx", "translate_grp.ty", "translate_grp.tz")}


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
                    driver={"translate": ("translate_grp.tx", "translate_grp.ty", "translate_grp.tz")}),
    ])

    def gizmo_lines(chunk_obj):
        stage = _stage()
        _author_prim(stage, chunk_obj)
        return sorted(ln.strip() for ln in stage.ExportToString().splitlines() if "ui:gizmo" in ln)

    assert gizmo_lines(_grouped()) == gizmo_lines(explicit)


@pytest.mark.parametrize("gizmo_type", ["translate", "rotate", "scale"])
def test_every_single_slot_manipulator_sugars_onto_a_group(gizmo_type) -> None:
    assert chunk_gizmo.get_gizmos(_grouped(gizmo_type))[0].type == gizmo_type


def test_transform_cannot_be_sugared_onto_a_group_either() -> None:
    with pytest.raises(ValueError, match="cannot be sugared"):
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
