"""Chunk gizmos: declaration -> authored USD -> resolve -> write-back -> cook.

Every assertion here is about authored USD text or param values. Nothing draws a pixel --
the viewport layer lives in usd-guides, and the point of the schema is that this repo can
be fully verified without it.
"""
from __future__ import annotations

import importlib

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom

from usd_chunks import chunk, chunk_gizmo, chunk_registry
from usd_chunks.chunk_core import ChunkBase, ChunkConfig, ChunkFlags, GizmoBase, SceneSender
from usd_chunks.plugins.chunk_usd import (
    _CHUNKS_ROOT,
    _author_prim,
    ChunkStageSync,
)


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


chunk_registry.ChunkRegistry().initialize(_DummyScene(), "pytest")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _transform_chunk(name: str = "Transform", target: str = "/World/Cube") -> chunk.Chunk:
    """A stand-in for scripts/transform.py: nested translate params + a prim-path anchor."""
    c = chunk.Chunk(name)
    c.add_param(chunk.LineEdit(
        "primpath_location", "Prim Path", default_value=target,
        flags=ChunkFlags.ParamFlags.LABELED | ChunkFlags.ParamFlags.PRIM_SELECTION,
    ))
    # A PRIM_SELECTION param takes its value from the scene selection at construction, which
    # is empty here — so default_value alone leaves it blank, exactly as it would in a real
    # session before the user picks a prim.
    c.get_param("primpath_location").value = target
    grp = chunk.Group("translate_grp", "Translate")
    for axis in ("tx", "ty", "tz"):
        grp.add_param(chunk.Slider(axis, axis.upper(), min_value=-100.0, max_value=100.0, default_value=0.0))
    c.add_param(grp)
    c._config = ChunkConfig(module="transform", gizmos=[_body_gizmo()])
    return c


def _body_gizmo() -> GizmoBase:
    return chunk.Gizmo(
        "body", type="transform", anchor="primpath_location",
        driver={"translate": ("translate_grp.tx", "translate_grp.ty", "translate_grp.tz")},
    )


def _stage_with_cube(path: str = "/World/Cube") -> Usd.Stage:
    """Cube in the session layer, deliberately: transform.py's execute() clears whatever
    layer is the edit target, so a cube defined in the root layer would be wiped by the very
    cook the end-to-end test is trying to observe."""
    stage = Usd.Stage.CreateInMemory()
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        UsdGeom.Cube.Define(stage, path)

    # Point the registry's scene at this stage. transform.py resolves its targets through
    # `param.resolve_paths()`, which reads `get_stage()` off the scene -- and the registry is
    # a singleton, so without this a test passes or fails depending on which test ran before
    # it and what scene that one happened to leave behind.
    class _StagedScene(_DummyScene):
        def get_stage(self):
            return stage

    chunk_registry.ChunkRegistry().initialize(_StagedScene(), "pytest")
    return stage


def _touch_from_chunk_layer(stage: Usd.Stage, c, path: str = "/World/Cube") -> None:
    """Author a real spec on `path` from `c`'s own layer, the way a cook would.

    An empty `over` is not enough: USD elides one on a prim that already composes, leaving
    the layer genuinely empty and the prim stack unchanged -- which is exactly the state the
    layer-stack lookup is supposed to report as "no chunk touched this".
    """
    with Usd.EditContext(stage, c.get_edit_context()):
        UsdGeom.Xformable(stage.OverridePrim(path)).AddTransformOp().Set(Gf.Matrix4d(1))


def _authored(chunk_obj, stage=None):
    stage = stage or _stage_with_cube()
    _author_prim(stage, chunk_obj)
    return stage, stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/{chunk_obj.name}")


# ---------------------------------------------------------------------------
# declaration
# ---------------------------------------------------------------------------

def test_gizmo_is_available_on_the_public_chunk_module() -> None:
    assert issubclass(chunk.Gizmo, GizmoBase)


def test_config_carries_gizmos() -> None:
    cfg = ChunkConfig(module="transform", gizmos=[_body_gizmo()])
    assert [g.name for g in cfg.gizmos] == ["body"]


def test_config_defaults_to_no_gizmos() -> None:
    assert ChunkConfig(module="transform").gizmos == []


def test_string_driver_normalises_to_the_value_slot() -> None:
    """A chunk with one driven attribute should not have to spell out a slot dict."""
    g = chunk.Gizmo("zdist", type="cube", driver="focusDistance")
    assert g.driver == {"value": ("focusDistance",)}


def test_dict_driver_normalises_entries_to_tuples() -> None:
    g = chunk.Gizmo("body", type="transform", driver={"translate": ["tx", "ty", "tz"]})
    assert g.driver == {"translate": ("tx", "ty", "tz")}


def test_editmode_defaults_to_prim_selection() -> None:
    assert chunk.Gizmo("body", type="transform", driver="tx").editmode == ("onPrimSelection",)


def test_bare_editmode_string_normalises_to_a_tuple() -> None:
    g = chunk.Gizmo("body", type="transform", driver="tx", editmode="always")
    assert g.editmode == ("always",)


def test_axis_defaults_to_free_drag() -> None:
    """No axis is the common case: drag any direction, only the driven param changes."""
    assert chunk.Gizmo("zdist", type="cube", driver="focusDistance").axis is None


@pytest.mark.parametrize("kwargs, fragment", [
    ({"type": "wobble", "driver": "tx"}, "unknown type"),
    ({"type": "transform", "driver": {"wobble": ("tx",)}}, "unknown driver slot"),
    ({"type": "cube", "driver": "tx", "axis": "W"}, "unknown axis"),
    ({"type": "cube", "driver": "tx", "editmode": ["onWobble"]}, "unknown editmode"),
    ({"type": "transform", "driver": {}}, "driver is required"),
    ({"type": "transform", "driver": {"translate": ()}}, "binds no params"),
])
def test_malformed_declarations_fail_loudly(kwargs, fragment) -> None:
    """Loudly at load, not silently at drag time -- a handle that quietly does nothing is
    far more expensive to diagnose than a chunk that refuses to load."""
    with pytest.raises(ValueError, match=fragment):
        chunk.Gizmo("g", **kwargs)


# ---------------------------------------------------------------------------
# authoring
# ---------------------------------------------------------------------------

def test_authors_type_label_and_editmode() -> None:
    stage, prim = _authored(_transform_chunk())

    assert prim.GetAttribute("ui:gizmo:body:type").Get() == "transform"
    assert prim.GetAttribute("ui:gizmo:body:label").Get() == "Transform"
    assert list(prim.GetAttribute("ui:gizmo:body:editmode").Get()) == ["onPrimSelection"]


def test_gizmo_attrs_are_uniform() -> None:
    """A gizmo declaration is load-time config, never animated."""
    _, prim = _authored(_transform_chunk())
    attr = prim.GetAttribute("ui:gizmo:body:type")
    assert attr.GetVariability() == Sdf.VariabilityUniform


def test_anchor_points_at_the_resolved_scene_prim() -> None:
    """Not at the anchor param. A host must never have to evaluate a chunk param to find
    out where to draw."""
    _, prim = _authored(_transform_chunk(target="/World/Cube"))

    targets = prim.GetRelationship("ui:gizmo:body:anchor").GetTargets()
    assert [str(t) for t in targets] == ["/World/Cube"]


def test_anchor_source_records_the_param_for_our_own_use() -> None:
    _, prim = _authored(_transform_chunk())
    assert prim.GetAttribute("ui:gizmo:body:anchorSource").Get() == "primpath_location"


def test_driver_authors_connections_to_the_nested_param_paths() -> None:
    """transform.py's sliders live in a Group, so `tx` is stored as inputs:translate_grp:tx.
    Authoring inputs:tx instead would resolve to nothing and fail silently."""
    _, prim = _authored(_transform_chunk())

    conns = prim.GetAttribute("ui:gizmo:body:driver:translate").GetConnections()
    assert [str(c) for c in conns] == [
        "/chunks/Transform.inputs:translate_grp:tx",
        "/chunks/Transform.inputs:translate_grp:ty",
        "/chunks/Transform.inputs:translate_grp:tz",
    ]


def test_singular_driver_authors_an_unsuffixed_attr() -> None:
    """A chunk with one driven attribute reads as one, in the usda and in a host's parser."""
    c = chunk.Chunk("Camera")
    c.add_param(chunk.LineEdit("primpath_location", default_value="/World/Cube"))
    c.add_param(chunk.Slider("focusDistance", min_value=0.0, max_value=1000.0))
    c._config = ChunkConfig(module="camera", gizmos=[
        chunk.Gizmo("zdist", type="cube", driver="focusDistance", axis="Z"),
    ])

    _, prim = _authored(c)

    assert prim.GetAttribute("ui:gizmo:zdist:driver").GetConnections()
    assert not prim.GetAttribute("ui:gizmo:zdist:driver:value").IsValid()
    assert prim.GetAttribute("ui:gizmo:zdist:axis").Get() == "Z"


def test_omitted_axis_authors_no_attr() -> None:
    _, prim = _authored(_transform_chunk())
    assert not prim.GetAttribute("ui:gizmo:body:axis").IsValid()


def test_chunk_without_gizmos_authors_nothing() -> None:
    c = chunk.Chunk("Plain")
    c._config = ChunkConfig(module="prim")
    _, prim = _authored(c)

    assert not [a for a in prim.GetAttributes() if a.GetName().startswith("ui:gizmo:")]


def test_two_gizmos_of_the_same_type_coexist() -> None:
    """The namespace segment is the gizmo's name, never its type -- a camera wants a body
    handle and a distance handle, and both may be cubes."""
    c = _transform_chunk()
    c._config = ChunkConfig(module="transform", gizmos=[
        chunk.Gizmo("a", type="cube", anchor="primpath_location", driver="translate_grp.tx"),
        chunk.Gizmo("b", type="cube", anchor="primpath_location", driver="translate_grp.ty"),
    ])
    _, prim = _authored(c)

    assert prim.GetAttribute("ui:gizmo:a:type").Get() == "cube"
    assert prim.GetAttribute("ui:gizmo:b:type").Get() == "cube"


def test_one_bad_gizmo_does_not_take_the_chunk_prim_down() -> None:
    """The rest of the prim's bookkeeping is what makes the chunk work at all."""
    c = _transform_chunk()
    c._config = ChunkConfig(module="transform", gizmos=[
        chunk.Gizmo("broken", type="transform", anchor="nope", driver="translate_grp.tx"),
        _body_gizmo(),
    ])
    _, prim = _authored(c)

    assert prim.GetAttribute("outputs:chunk").IsValid()
    assert prim.GetAttribute("ui:gizmo:body:type").Get() == "transform"


# ---------------------------------------------------------------------------
# anchor inference
# ---------------------------------------------------------------------------

def test_anchor_inference_prefers_the_conventional_name() -> None:
    c = _transform_chunk()
    param = chunk_gizmo.resolve_anchor_param(c, chunk.Gizmo("g", type="transform", driver="translate_grp.tx"))
    assert param.name == "primpath_location"


def test_anchor_inference_falls_back_to_a_sole_flagged_param() -> None:
    """Only about half the chunks with a target path set PRIM_SELECTION, so the name is the
    primary signal -- but the flag has to work for the ones that use it and nothing else."""
    c = chunk.Chunk("Odd")
    c.add_param(chunk.LineEdit(
        "target_prim", default_value="/World/Cube",
        flags=ChunkFlags.ParamFlags.PRIM_SELECTION,
    ))
    c.add_param(chunk.Slider("amount"))

    param = chunk_gizmo.resolve_anchor_param(c, chunk.Gizmo("g", type="cube", driver="amount"))
    assert param.name == "target_prim"


def test_uninferrable_anchor_fails_loudly() -> None:
    c = chunk.Chunk("Odd")
    c.add_param(chunk.Slider("amount"))

    with pytest.raises(ValueError, match="cannot infer anchor"):
        chunk_gizmo.resolve_anchor_param(c, chunk.Gizmo("g", type="cube", driver="amount"))


def test_explicit_but_missing_anchor_param_fails_loudly() -> None:
    c = _transform_chunk()
    with pytest.raises(ValueError, match="does not exist"):
        chunk_gizmo.resolve_anchor_param(
            c, chunk.Gizmo("g", type="transform", anchor="nope", driver="translate_grp.tx")
        )


# ---------------------------------------------------------------------------
# read-back
# ---------------------------------------------------------------------------

def test_resolve_round_trips_the_declaration() -> None:
    _, prim = _authored(_transform_chunk())

    descs = chunk_gizmo.resolve_gizmos(prim)
    assert len(descs) == 1
    desc = descs[0]
    assert desc.name == "body"
    assert desc.type == "transform"
    assert desc.label == "Transform"
    assert desc.anchor_source == "primpath_location"
    assert [str(p) for p in desc.anchor] == ["/World/Cube"]
    assert desc.axis is None
    assert desc.space == "local"
    assert desc.editmode == ("onPrimSelection",)
    assert set(desc.driver) == {"translate"}
    assert len(desc.driver["translate"]) == 3


def test_resolve_is_a_plain_record() -> None:
    """No Qt types, no callbacks, nothing beyond what pxr gives every host."""
    _, prim = _authored(_transform_chunk())
    desc = chunk_gizmo.resolve_gizmos(prim)[0]

    for value in (desc.name, desc.type, desc.label, desc.anchor_source, desc.space):
        assert isinstance(value, str)
    for target in desc.anchor:
        assert isinstance(target, Sdf.Path)


def test_resolve_ignores_namespaced_attrs_that_are_not_gizmos() -> None:
    stage, prim = _authored(_transform_chunk())
    prim.CreateAttribute("ui:gizmo:ghost:label", Sdf.ValueTypeNames.Token).Set("nope")

    assert [d.name for d in chunk_gizmo.resolve_gizmos(prim)] == ["body"]


def test_resolve_on_an_unrelated_prim_returns_nothing() -> None:
    stage, _ = _authored(_transform_chunk())
    assert chunk_gizmo.resolve_gizmos(stage.GetPrimAtPath("/World/Cube")) == []


# ---------------------------------------------------------------------------
# scene prim -> chunk, via the layer stack
# ---------------------------------------------------------------------------

def _synced_scene():
    """A live ChunkStageSync, so chunks get real per-chunk layers to walk back from."""
    stage = _stage_with_cube()

    class _SyncScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _SyncScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    ChunkStageSync().initialize(stage, scene)
    return stage, scene


def test_resolve_from_the_selected_scene_prim() -> None:
    """The host selects the Cube, not the chunk prim -- ui:gizmo:* lives under /chunks,
    which is not in the viewport and is not what anyone clicks."""
    stage, scene = _synced_scene()
    c = _transform_chunk()
    scene.add_chunk(c)
    _author_prim(stage, c)
    _touch_from_chunk_layer(stage, c)

    descs = chunk_gizmo.resolve_gizmos(stage.GetPrimAtPath("/World/Cube"))
    assert [d.name for d in descs] == ["body"]


def test_chunks_for_prim_uses_the_layer_stack() -> None:
    stage, scene = _synced_scene()
    c = _transform_chunk()
    scene.add_chunk(c)
    _touch_from_chunk_layer(stage, c)

    assert [x.name for x in chunk_gizmo.chunks_for_prim(stage.GetPrimAtPath("/World/Cube"))] == ["Transform"]


def test_a_prim_the_chunk_never_touched_finds_no_chunk() -> None:
    stage, scene = _synced_scene()
    c = _transform_chunk()
    scene.add_chunk(c)
    UsdGeom.Cube.Define(stage, "/World/Other")

    assert chunk_gizmo.chunks_for_prim(stage.GetPrimAtPath("/World/Other")) == []


def test_muting_a_chunk_layer_drops_its_gizmos() -> None:
    """Correct for free: no spec in the stack, no handle. Nothing to special-case."""
    stage, scene = _synced_scene()
    c = _transform_chunk()
    scene.add_chunk(c)
    _author_prim(stage, c)
    _touch_from_chunk_layer(stage, c)
    assert chunk_gizmo.resolve_gizmos(stage.GetPrimAtPath("/World/Cube"))

    stage.MuteLayer(c.get_edit_context().identifier)
    assert chunk_gizmo.resolve_gizmos(stage.GetPrimAtPath("/World/Cube")) == []


# ---------------------------------------------------------------------------
# write-back
# ---------------------------------------------------------------------------

def test_apply_bindings_writes_the_translate_params() -> None:
    c = _transform_chunk()

    chunk_gizmo.apply_bindings(c, "body", {"translate": Gf.Vec3d(1.0, 2.0, 3.0)})

    assert c.get_param("tx").value == pytest.approx(1.0)
    assert c.get_param("ty").value == pytest.approx(2.0)
    assert c.get_param("tz").value == pytest.approx(3.0)


def test_apply_bindings_accepts_a_plain_sequence() -> None:
    c = _transform_chunk()
    chunk_gizmo.apply_bindings(c, "body", {"translate": [4.0, 5.0, 6.0]})
    assert c.get_param("tz").value == pytest.approx(6.0)


def test_apply_bindings_writes_a_scalar_through_the_value_slot() -> None:
    c = chunk.Chunk("Camera")
    c.add_param(chunk.LineEdit("primpath_location", default_value="/World/Cube"))
    c.add_param(chunk.Slider("focusDistance", min_value=0.0, max_value=1000.0))
    c._config = ChunkConfig(module="camera", gizmos=[
        chunk.Gizmo("zdist", type="cube", driver="focusDistance"),
    ])

    chunk_gizmo.apply_bindings(c, "zdist", {"value": 12.5})
    assert c.get_param("focusDistance").value == pytest.approx(12.5)


def test_unknown_slot_fails_loudly() -> None:
    """Silently dropping it looks exactly like a handle that does not work."""
    c = _transform_chunk()
    with pytest.raises(KeyError, match="no driver for slot"):
        chunk_gizmo.apply_bindings(c, "body", {"rotate": Gf.Vec3d(0, 0, 0)})


def test_unknown_gizmo_name_fails_loudly() -> None:
    c = _transform_chunk()
    with pytest.raises(ValueError, match="declares no gizmo"):
        chunk_gizmo.apply_bindings(c, "nope", {"translate": Gf.Vec3d(0, 0, 0)})


def test_wrong_value_count_fails_loudly() -> None:
    c = _transform_chunk()
    with pytest.raises(ValueError, match="drives 3 params"):
        chunk_gizmo.apply_bindings(c, "body", {"translate": (1.0, 2.0)})


def test_a_cross_chunk_driver_pointing_nowhere_is_survivable() -> None:
    """A full property path targets another chunk, which can be deleted or renamed
    independently. Losing it must cost that slot, not the whole drag -- unlike a bare name
    that does not resolve, which is the chunk's own config naming a param it lacks and stays
    loud. Cross-chunk resolution itself is covered in test_chunk_gizmo_cross_chunk.py."""
    c = _transform_chunk()
    c._config = ChunkConfig(module="transform", gizmos=[
        chunk.Gizmo("body", type="transform", anchor="primpath_location",
                    driver={"value": ("/chunks/Nowhere.inputs:tx",)}),
    ])

    chunk_gizmo.apply_bindings(c, "body", {"value": 1.0})  # must not raise


# ---------------------------------------------------------------------------
# the whole path
# ---------------------------------------------------------------------------

def test_drag_writes_params_and_the_cook_moves_the_prim() -> None:
    """The point of the feature: a handle value ends up as a real xform on a real prim,
    having gone through the chunk's params rather than around them."""
    transform_mod = importlib.import_module("scripts.transform")

    stage = _stage_with_cube()
    c = transform_mod.main()
    c._config = ChunkConfig(module="transform", gizmos=transform_mod.config().gizmos)
    c.get_param("primpath_location").value = "/World/Cube"

    _author_prim(stage, c)
    chunk_prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/Transform")
    desc = next(d for d in chunk_gizmo.resolve_gizmos(chunk_prim) if "translate" in d.driver)

    chunk_gizmo.apply_bindings(c, desc.name, {"translate": Gf.Vec3d(5.0, 0.0, -2.0)})

    class _FakeNode:
        def __init__(self, chunk_obj):
            self._chunk = chunk_obj

        def get_stage(self):
            return stage

        def get_param(self, name):
            return self._chunk.get_param(name)

    transform_mod.execute(_FakeNode(c))

    matrix = UsdGeom.Xformable(stage.GetPrimAtPath("/World/Cube")).GetLocalTransformation()
    assert matrix.ExtractTranslation() == Gf.Vec3d(5.0, 0.0, -2.0)


def test_the_shipped_transform_chunk_declares_gizmos() -> None:
    """The reference chunk, not a test fixture -- if this drifts, the feature has no user.

    Declared as one `gizmo=` kwarg per group rather than a gizmos block, which is why
    config() has none: spelling the same nine param paths out there was longer than every
    other line of the config put together.
    """
    transform_mod = importlib.import_module("scripts.transform")
    c = transform_mod.main()
    c._config = ChunkConfig(module="transform")

    gizmos = {g.name: g for g in chunk_gizmo.get_gizmos(c)}

    assert set(gizmos) == {"translate_grp", "rotate_grp", "scale_grp"}
    assert gizmos["translate_grp"].type == "translate"
    assert gizmos["translate_grp"].driver["translate"] == (
        "translate_grp.tx", "translate_grp.ty", "translate_grp.tz")
