"""Several transform-ish chunks on one prim: who wins, and do the numbers stay right.

Asked directly: "what happens if you have a translate + translate + offset translate etc,
does the gizmo still do the right thing? do we still get the right values?"

The short answer, pinned below:

- Transform is ABSOLUTE. Two of them on one prim is last-one-wins, not additive, because
  each authors xformOpOrder = [xformOp:transform] and USD resolves that attribute as a
  whole -- the strongest opinion decides, there is no merging. Use Offset Transform to add.
- Offset Transform is RELATIVE. It reads the placement in effect and adds to it.
- resolve_gizmos returns handles strongest-first, because chunks_for_prim walks
  GetPrimStack(), which is strength-ordered. So a host with no chunk selected driving
  handles[0] drives the chunk whose result is actually visible.
"""
from __future__ import annotations

import importlib

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom

from usd_chunks import chunk_gizmo, chunk_registry
from usd_chunks.chunk_core import ChunkConfig, SceneSender
from usd_chunks.plugins import chunk_usd

transform_mod = importlib.import_module("scripts.transform")
offset_mod = importlib.import_module("scripts.offset_transform")


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture
def scene():
    stage = Usd.Stage.CreateInMemory()
    scene_layer = Sdf.Layer.CreateAnonymous("scene")
    stage.GetRootLayer().subLayerPaths.append(scene_layer.identifier)
    with Usd.EditContext(stage, scene_layer):
        UsdGeom.Cube.Define(stage, "/cube")

    registry = chunk_registry.ChunkRegistry()
    sender = _Scene(stage)
    registry.initialize(sender, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_usd.ChunkStageSync().initialize(stage, sender)
    offset_mod._last_applied.clear()
    return stage, sender, scene_layer


def _mk(sender, module, name):
    chunk = module.main()
    is_transform = module is transform_mod
    chunk._config = ChunkConfig(module="transform" if is_transform else "offset_transform")
    chunk._module = module
    chunk.name = name
    sender.add_chunk(chunk)
    chunk.get_param("primpath_location" if is_transform else "prim_path").value = "/cube"
    return chunk


def _translation(stage):
    matrix = UsdGeom.Xformable(stage.GetPrimAtPath("/cube")).GetLocalTransformation(
        Usd.TimeCode.Default()
    )
    matrix = matrix[0] if isinstance(matrix, tuple) else matrix
    return matrix.ExtractTranslation()


def _bake(stage, layer, translation):
    """A placement already on the prim as a single matrix op -- what a Transform chunk
    leaves behind, and what a baked asset export looks like.

    Into the SCENE sublayer, never the session layer: the session layer outranks every
    chunk, so a base authored there is not a base at all -- the offset computes correctly
    and is then overridden, which reads as the fix not working.
    """
    with Usd.EditContext(stage, layer):
        UsdGeom.Xformable(stage.GetPrimAtPath("/cube")).AddTransformOp().Set(
            Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(*translation))
        )


# ---------------------------------------------------------------------------
# offset on top of a matrix placement
# ---------------------------------------------------------------------------

def test_an_offset_adds_to_a_matrix_placement(scene) -> None:
    """The bug found while reviewing chains. Offset Transform built its base by reading
    translate/rotate/scale ops, so a single `transform` matrix op -- what a Transform chunk
    authors, and what a baked asset carries -- was invisible to it. The base read as
    identity and the offset was applied to nothing instead of to the placement in effect.
    """
    stage, sender, scene_layer = scene
    _bake(stage, scene_layer, (0.0, 5.0, 0.0))

    offset = _mk(sender, offset_mod, "Off")
    offset.get_param("ty").value = 2.0

    assert _translation(stage) == Gf.Vec3d(0.0, 7.0, 0.0)


def test_an_offset_on_separate_ops_still_works(scene) -> None:
    """The spelling that always worked, kept so the matrix branch cannot break it."""
    stage, sender, scene_layer = scene
    with Usd.EditContext(stage, scene_layer):
        UsdGeom.Xformable(stage.GetPrimAtPath("/cube")).AddTranslateOp().Set(
            Gf.Vec3d(0.0, 5.0, 0.0)
        )

    offset = _mk(sender, offset_mod, "Off")
    offset.get_param("ty").value = 2.0

    assert _translation(stage) == Gf.Vec3d(0.0, 7.0, 0.0)


def test_an_offset_scales_from_the_matrix_base(scene) -> None:
    stage, sender, scene_layer = scene
    with Usd.EditContext(stage, scene_layer):
        UsdGeom.Xformable(stage.GetPrimAtPath("/cube")).AddTransformOp().Set(
            Gf.Matrix4d(1.0).SetScale(Gf.Vec3d(2.0, 2.0, 2.0))
        )

    offset = _mk(sender, offset_mod, "Off")
    offset.get_param("sx").value = 3.0

    matrix = UsdGeom.Xformable(stage.GetPrimAtPath("/cube")).GetLocalTransformation(
        Usd.TimeCode.Default()
    )
    matrix = matrix[0] if isinstance(matrix, tuple) else matrix
    assert matrix.GetRow3(0).GetLength() == pytest.approx(6.0), "2 x 3, not 3 and not 2"


# ---------------------------------------------------------------------------
# two Transforms on one prim
# ---------------------------------------------------------------------------

def test_two_transforms_compose(scene) -> None:
    """The point of the whole change, and the exact inverse of what this asserted before.

    Each chunk used to author the same attribute, so USD picked the strongest layer and
    discarded the other -- 5 or 3, never 8. Owning a named op each, they compose.
    """
    stage, sender, scene_layer = scene
    first = _mk(sender, transform_mod, "T1")
    second = _mk(sender, transform_mod, "T2")

    first.get_param("ty").value = 5.0
    second.get_param("ty").value = 3.0
    first.execute()
    second.execute()

    assert _translation(stage) == pytest.approx(Gf.Vec3d(0, 8, 0)), (
        "both contribute now; 5 or 3 alone would mean one had discarded the other"
    )


def test_each_transform_owns_a_distinct_op(scene) -> None:
    """What makes composing possible: they are no longer writing the same attribute."""
    from usd_chunks import chunk_utils

    stage, sender, scene_layer = scene
    first = _mk(sender, transform_mod, "T1")
    second = _mk(sender, transform_mod, "T2")
    first.get_param("ty").value = 5.0
    second.get_param("ty").value = 3.0
    first.execute()
    second.execute()

    names = {chunk_utils.xform_op_name(first), chunk_utils.xform_op_name(second)}
    order = list(UsdGeom.Xformable(stage.GetPrimAtPath("/cube")).GetXformOpOrderAttr().Get())

    assert len(names) == 2
    assert names <= set(order)


def test_a_chunks_op_id_survives_a_rename(scene) -> None:
    """Identity rests on the id, not the name, so renaming does not orphan the op a chunk
    has already authored."""
    from usd_chunks import chunk_utils

    stage, sender, scene_layer = scene
    node = _mk(sender, transform_mod, "T1")
    node.get_param("ty").value = 5.0
    node.execute()
    before = chunk_utils.xform_op_name(node)

    node.name = "RenamedLater"

    assert chunk_utils.xform_op_name(node) == before


def test_both_chunks_offer_a_handle(scene) -> None:
    """Neither is discarded any more, so both are draggable and both show up."""
    stage, sender, scene_layer = scene
    first = _mk(sender, transform_mod, "T1")
    second = _mk(sender, transform_mod, "T2")
    first.get_param("ty").value = 5.0
    second.get_param("ty").value = 3.0
    first.execute()
    second.execute()

    prims = {str(d.chunk_prim) for d in chunk_gizmo.placement_gizmos(stage.GetPrimAtPath("/cube"))}

    assert prims == {"/chunks/T1", "/chunks/T2"}


def test_dragging_moves_the_prim_by_what_was_dragged(scene) -> None:
    """End to end on a chain: whatever the ordering, driving handles[0] has to put the prim
    where the drag asked. A drag that writes to the losing chunk moves nothing on screen."""
    stage, sender, scene_layer = scene
    first = _mk(sender, transform_mod, "T1")
    second = _mk(sender, transform_mod, "T2")
    first.get_param("ty").value = 5.0
    second.get_param("ty").value = 3.0
    first.execute()
    second.execute()

    handles = chunk_gizmo.placement_gizmos(stage.GetPrimAtPath("/cube"))
    chunk_gizmo.apply_bindings(
        handles[0].chunk_prim, handles[0].name,
        Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(0.0, 9.0, 0.0)),
    )
    for c in (first, second):
        c.execute()

    assert _translation(stage) == Gf.Vec3d(0.0, 9.0, 0.0)


# ---------------------------------------------------------------------------
# mixed
# ---------------------------------------------------------------------------

def test_a_transform_and_an_offset_both_offer_handles(scene) -> None:
    stage, sender, scene_layer = scene
    _mk(sender, transform_mod, "T1")
    _mk(sender, offset_mod, "Off")

    handles = chunk_gizmo.placement_gizmos(stage.GetPrimAtPath("/cube"))

    assert len(handles) == 2
    assert {d.type for d in handles} == {"transform"}


def test_neither_a_transform_nor_an_offset_triggers_auto_create(scene, monkeypatch) -> None:
    """Both own the prim's placement, so neither may get a Transform stacked on it."""
    stage, sender, scene_layer = scene
    registry = chunk_registry.ChunkRegistry()

    class _Stub:
        loaded = True
        creates = 0

        def create(self):
            _Stub.creates += 1
            c = transform_mod.main()
            c._config = ChunkConfig(module="transform")
            c._module = transform_mod
            return [c]

        load = create

    stub = _Stub()
    monkeypatch.setitem(registry.registered_modules, "transform", stub)
    monkeypatch.setattr(chunk_usd, "_create_node_pos", None, raising=False)

    _mk(sender, offset_mod, "Off")
    chunk_gizmo.ensure_transform_chunk(stage.GetPrimAtPath("/cube"))

    assert stub.creates == 0


# ---------------------------------------------------------------------------
# dragging an Offset Transform: an offset is a delta, not a placement
# ---------------------------------------------------------------------------

def test_dragging_an_offset_writes_a_delta_not_the_absolute_value(scene) -> None:
    """Reported live: a Transform chunk adopting an asset's scale of 100, an Offset
    Transform added on the same prim, one drag, and the scale became 100 x 100.

    The handle emits an absolute local transform -- correct, and the same thing every
    transform handle emits. An offset is not a placement, so the chunk converts.
    """
    stage, sender, scene_layer = scene
    _bake(stage, scene_layer, (0.0, 5.0, 0.0))
    offset = _mk(sender, offset_mod, "Off")

    # Drag it to where it already is: the offset must come out as nothing.
    chunk_gizmo.apply_bindings(
        Sdf.Path("/chunks/Off"), "transform",
        Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(0.0, 5.0, 0.0)),
    )

    assert [offset.get_param(n).value for n in ("tx", "ty", "tz")] == pytest.approx([0, 0, 0])
    assert [offset.get_param(n).value for n in ("sx", "sy", "sz")] == pytest.approx([1, 1, 1])


def test_dragging_an_offset_moves_the_prim_where_it_was_dragged(scene) -> None:
    stage, sender, scene_layer = scene
    _bake(stage, scene_layer, (0.0, 5.0, 0.0))
    _mk(sender, offset_mod, "Off")

    chunk_gizmo.apply_bindings(
        Sdf.Path("/chunks/Off"), "transform",
        Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(0.0, 8.0, 0.0)),
    )

    assert _translation(stage) == pytest.approx(Gf.Vec3d(0.0, 8.0, 0.0)), (
        "dragged to 8 from a base of 5, so the offset is 3 and the prim lands on 8"
    )


def test_a_scale_offset_is_a_ratio_not_a_copy(scene) -> None:
    """The 100 x 100 case exactly. Scale composes by multiplication."""
    stage, sender, scene_layer = scene
    with Usd.EditContext(stage, scene_layer):
        UsdGeom.Xformable(stage.GetPrimAtPath("/cube")).AddTransformOp().Set(
            Gf.Matrix4d(1.0).SetScale(Gf.Vec3d(100.0, 100.0, 100.0))
        )
    offset = _mk(sender, offset_mod, "Off")

    chunk_gizmo.apply_bindings(
        Sdf.Path("/chunks/Off"), "transform",
        Gf.Matrix4d(1.0).SetScale(Gf.Vec3d(100.0, 100.0, 100.0)),
    )

    assert [offset.get_param(n).value for n in ("sx", "sy", "sz")] == pytest.approx([1, 1, 1])
    matrix = UsdGeom.Xformable(stage.GetPrimAtPath("/cube")).GetLocalTransformation(
        Usd.TimeCode.Default()
    )
    matrix = matrix[0] if isinstance(matrix, tuple) else matrix
    assert matrix.GetRow3(0).GetLength() == pytest.approx(100.0), "100, not 10000"


def test_dragging_twice_does_not_compound(scene) -> None:
    """Once the chunk has written, the prim's ops are its OWN output. Reading them back as
    the base would fold the offset into it and grow on every drag."""
    stage, sender, scene_layer = scene
    _bake(stage, scene_layer, (0.0, 5.0, 0.0))
    offset = _mk(sender, offset_mod, "Off")

    for _ in range(3):
        chunk_gizmo.apply_bindings(
            Sdf.Path("/chunks/Off"), "transform",
            Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(0.0, 8.0, 0.0)),
        )

    assert offset.get_param("ty").value == pytest.approx(3.0)
    assert _translation(stage) == pytest.approx(Gf.Vec3d(0.0, 8.0, 0.0))


# ---------------------------------------------------------------------------
# who owns the prim: nearest the view, authored or not
# ---------------------------------------------------------------------------

def _wire(stage, downstream, upstream):
    """downstream consumes upstream, the way the graph chains chunks."""
    from usd_chunks.plugins.chunk_usd import _EXEC_INPUT, _EXEC_OUTPUT

    prim = stage.GetPrimAtPath(Sdf.Path(f"/chunks/{downstream}"))
    attr = prim.GetAttribute(_EXEC_INPUT)
    if not attr.IsValid():
        attr = prim.CreateAttribute(_EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False)
    attr.SetConnections([Sdf.Path(f"/chunks/{upstream}.{_EXEC_OUTPUT}")])


def test_an_unauthored_downstream_chunk_still_owns_the_prim(scene) -> None:
    """Reported live: adding an Offset Transform after a Transform and dragging kept
    editing the Transform. Only after typing a value into the Offset by hand -- which made
    it start authoring, and so join the prim's layer stack -- did it take over.

    Ownership must not depend on whether a number has been entered.
    """
    stage, sender, scene_layer = scene
    transform = _mk(sender, transform_mod, "T1")
    transform.get_param("ty").value = 5.0
    transform.execute()
    _mk(sender, offset_mod, "Off")
    _wire(stage, "Off", "T1")

    handles = chunk_gizmo.placement_gizmos(stage.GetPrimAtPath("/cube"))

    assert str(handles[0].chunk_prim) == "/chunks/Off", (
        "the chunk nearest the view has the final say, authored or not"
    )


def test_the_upstream_chunk_is_still_offered(scene) -> None:
    """Ordered, not filtered -- a host that wants a specific chunk's handle still finds it."""
    stage, sender, scene_layer = scene
    transform = _mk(sender, transform_mod, "T1")
    transform.get_param("ty").value = 5.0
    transform.execute()
    _mk(sender, offset_mod, "Off")
    _wire(stage, "Off", "T1")

    prims = {str(d.chunk_prim) for d in chunk_gizmo.placement_gizmos(stage.GetPrimAtPath("/cube"))}

    assert prims == {"/chunks/T1", "/chunks/Off"}


def test_ownership_does_not_flip_once_the_offset_authors(scene) -> None:
    """The other half of the report: it used to change hands the moment a value was typed."""
    stage, sender, scene_layer = scene
    transform = _mk(sender, transform_mod, "T1")
    transform.get_param("ty").value = 5.0
    transform.execute()
    offset = _mk(sender, offset_mod, "Off")
    _wire(stage, "Off", "T1")
    before = str(chunk_gizmo.placement_gizmos(stage.GetPrimAtPath("/cube"))[0].chunk_prim)

    offset.get_param("tx").value = 1.0

    after = str(chunk_gizmo.placement_gizmos(stage.GetPrimAtPath("/cube"))[0].chunk_prim)
    assert before == after == "/chunks/Off"


def test_dragging_the_downstream_chunk_lands_where_it_was_dragged(scene) -> None:
    """The handle reports an absolute placement while a composing chunk contributes only its
    own share, so the chunk has to divide the upstream contribution back out. Getting that
    backwards conjugates the drag rather than merely offsetting it."""
    stage, sender, scene_layer = scene
    upstream = _mk(sender, transform_mod, "T1")
    downstream = _mk(sender, transform_mod, "T2")
    upstream.get_param("ty").value = 5.0
    upstream.execute()
    downstream.execute()
    _wire(stage, "T2", "T1")

    chunk_gizmo.apply_bindings(
        Sdf.Path("/chunks/T2"), "transform",
        Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(0.0, 9.0, 0.0)),
    )
    upstream.execute()
    downstream.execute()

    assert _translation(stage) == pytest.approx(Gf.Vec3d(0, 9, 0))
    assert upstream.get_param("ty").value == 5.0, "the upstream chunk must not be touched"
    assert downstream.get_param("ty").value == pytest.approx(4.0), "9 total, 5 from upstream"


# ---------------------------------------------------------------------------
# a second Transform IS an offset
# ---------------------------------------------------------------------------

@pytest.mark.xfail(
    reason="xformOpOrder is still one attribute: each chunk writes its own copy into its "
           "own layer, so which chunk's ORDER wins is decided by layer strength, and that "
           "depends on the creation/cook sequence. Cooking one chunk before the second "
           "exists leaves the first chunk's order (naming only its own op) winning, so the "
           "second contributes nothing. The known open item in the design -- rebuild the "
           "order from the exec chain rather than letting layer strength decide it.",
    strict=True,
)
def test_a_second_transform_offsets_the_first(scene) -> None:
    """The claim that makes offset_transform redundant.

    "I want the apple here, and now nudge it up a bit for the sim without destroying that
    placement" -- the case the Offset Transform node exists for. A second Transform now does
    it, because the two compose instead of one discarding the other.
    """
    stage, sender, scene_layer = scene
    placed = _mk(sender, transform_mod, "Placed")
    placed.get_param("ty").value = 5.0
    placed.execute()

    nudge = _mk(sender, transform_mod, "Nudge")
    nudge.get_param("ty").value = 2.0
    nudge.execute()

    assert _translation(stage) == pytest.approx(Gf.Vec3d(0, 7, 0))
    assert placed.get_param("ty").value == 5.0, "the original placement is untouched"


# A "deleting the offset leaves the placement" test belongs here and is deliberately absent:
# while the second chunk contributes nothing (see the xfails), deleting it and finding the
# placement intact passes for the wrong reason and would guard nothing. Add it once the
# ordering is derived from the exec chain.


@pytest.mark.xfail(reason="same xformOpOrder contention as above", strict=True)
def test_a_scale_offset_multiplies(scene) -> None:
    """Scale composes by multiplication, which the offset node had to special-case and a
    composing Transform gets for free."""
    stage, sender, scene_layer = scene
    base = _mk(sender, transform_mod, "Base")
    base.get_param("sx").value = 2.0
    base.execute()
    nudge = _mk(sender, transform_mod, "Nudge")
    nudge.get_param("sx").value = 3.0
    nudge.execute()

    matrix = UsdGeom.Xformable(stage.GetPrimAtPath("/cube")).GetLocalTransformation(
        Usd.TimeCode.Default()
    )
    matrix = matrix[0] if isinstance(matrix, tuple) else matrix
    assert matrix.GetRow3(0).GetLength() == pytest.approx(6.0)
