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
    return stage, sender, scene_layer


def _chunk_path(name: str) -> str:
    """Where a chunk added through the scene actually lands.

    add_chunk routes a context-less chunk into the scene's default chunkgroup, so its
    parent is that group rather than the /chunks scope. Resolved through the registry so
    the helpers here can keep taking plain names.
    """
    scene = chunk_registry.ChunkRegistry().scene
    ctx = scene.peek_root_context() if scene is not None else None
    root = chunk_usd.get_chunk_prim_path(ctx) if ctx is not None else "/chunks"
    return f"{root}/{name}"


def _mk(sender, module, name):
    chunk = module.main()
    chunk._config = ChunkConfig(module="transform")
    chunk._module = module
    chunk.name = name
    sender.add_chunk(chunk)
    chunk.get_param("primpath_location").value = "/cube"
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


def test_the_same_network_mints_the_same_op_name_twice(scene) -> None:
    """Build the same graph again and the ops come back with the same names.

    The suffix used to be uuid4, so a pipeline that regenerates its scene authored
    `SimDrop_3ab48045` one run and `SimDrop_1b9f959b` the next over identical transforms --
    nothing downstream could hold an opinion on an attribute whose name is re-rolled every
    time. Seeding off the chunk's prim path instead makes a whole network reproducible,
    which is the property that lets a recipe be shipped in place of its output.

    Two scenes built the same way stand in for two runs; with a random id this fails.
    """
    from usd_chunks import chunk_utils

    stage, sender, scene_layer = scene
    first = _mk(sender, transform_mod, "T1")
    first.get_param("ty").value = 5.0
    first.execute()
    once = chunk_utils.xform_op_name(first)

    rebuilt_stage = Usd.Stage.CreateInMemory()
    rebuilt_layer = Sdf.Layer.CreateAnonymous("scene")
    rebuilt_stage.GetRootLayer().subLayerPaths.append(rebuilt_layer.identifier)
    with Usd.EditContext(rebuilt_stage, rebuilt_layer):
        UsdGeom.Cube.Define(rebuilt_stage, "/cube")
    rebuilt_sender = _Scene(rebuilt_stage)
    chunk_registry.ChunkRegistry().initialize(rebuilt_sender, "pytest")
    chunk_registry.ChunkLayerManager().initialize(rebuilt_stage)
    chunk_usd.ChunkStageSync().initialize(rebuilt_stage, rebuilt_sender)

    second = _mk(rebuilt_sender, transform_mod, "T1")
    second.get_param("ty").value = 5.0
    second.execute()

    assert chunk_utils.xform_op_name(second) == once


def test_a_vacated_path_does_not_hand_its_op_to_a_newcomer(scene) -> None:
    """The one collision a derived id can hit that a random one cannot.

    Rename T1 and it keeps the suffix it recorded, but its old path is now free -- so a new
    chunk called T1 seeds off exactly the same path. Without the salt they would share an
    op, which is the failure distinct op names exist to prevent in the first place.
    """
    from usd_chunks import chunk_utils

    stage, sender, scene_layer = scene
    original = _mk(sender, transform_mod, "T1")
    original.get_param("ty").value = 5.0
    original.execute()
    kept = chunk_utils.xform_op_name(original)

    original.name = "RenamedLater"

    newcomer = _mk(sender, transform_mod, "T1")
    newcomer.get_param("ty").value = 3.0
    newcomer.execute()

    assert chunk_utils.xform_op_name(original) == kept
    assert chunk_utils.xform_op_name(newcomer) != kept


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

    assert prims == {_chunk_path("T1"), _chunk_path("T2")}


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
# who owns the prim: nearest the view, authored or not
# ---------------------------------------------------------------------------

def _wire(stage, downstream, upstream):
    """downstream consumes upstream, the way the graph chains chunks."""
    from usd_chunks.plugins.chunk_usd import _EXEC_INPUT, _EXEC_OUTPUT

    prim = stage.GetPrimAtPath(Sdf.Path(_chunk_path(downstream)))
    attr = prim.GetAttribute(_EXEC_INPUT)
    if not attr.IsValid():
        attr = prim.CreateAttribute(_EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False)
    attr.SetConnections([Sdf.Path(f"{_chunk_path(upstream)}.{_EXEC_OUTPUT}")])


def test_an_unauthored_downstream_chunk_still_owns_the_prim(scene) -> None:
    """Reported live against the Offset Transform node, since removed: adding it after a
    Transform and dragging kept editing the Transform, until a value was typed in by hand,
    which made it start authoring and so join the prim's layer stack.

    A downstream Transform that has not cooked yet stands in for it exactly -- the point was
    never the node type, only that ownership must not depend on whether a number has been
    entered.
    """
    stage, sender, scene_layer = scene
    transform = _mk(sender, transform_mod, "T1")
    transform.get_param("ty").value = 5.0
    transform.execute()
    _mk(sender, transform_mod, "Off")
    _wire(stage, "Off", "T1")

    handles = chunk_gizmo.placement_gizmos(stage.GetPrimAtPath("/cube"))

    assert str(handles[0].chunk_prim) == _chunk_path("Off"), (
        "the chunk nearest the view has the final say, authored or not"
    )


def test_the_upstream_chunk_is_still_offered(scene) -> None:
    """Ordered, not filtered -- a host that wants a specific chunk's handle still finds it."""
    stage, sender, scene_layer = scene
    transform = _mk(sender, transform_mod, "T1")
    transform.get_param("ty").value = 5.0
    transform.execute()
    _mk(sender, transform_mod, "Off")
    _wire(stage, "Off", "T1")

    prims = {str(d.chunk_prim) for d in chunk_gizmo.placement_gizmos(stage.GetPrimAtPath("/cube"))}

    assert prims == {_chunk_path("T1"), _chunk_path("Off")}


def test_ownership_does_not_flip_once_the_downstream_chunk_authors(scene) -> None:
    """The other half of the report: it used to change hands the moment a value was typed."""
    stage, sender, scene_layer = scene
    transform = _mk(sender, transform_mod, "T1")
    transform.get_param("ty").value = 5.0
    transform.execute()
    offset = _mk(sender, transform_mod, "Off")
    _wire(stage, "Off", "T1")
    before = str(chunk_gizmo.placement_gizmos(stage.GetPrimAtPath("/cube"))[0].chunk_prim)

    offset.get_param("tx").value = 1.0

    after = str(chunk_gizmo.placement_gizmos(stage.GetPrimAtPath("/cube"))[0].chunk_prim)
    assert before == after == _chunk_path("Off")


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
        Sdf.Path(_chunk_path("T2")), "transform",
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
    # Wired, as a graph actually is. Two chunks with no connection between them have no
    # order to derive -- see test_two_unconnected_chunks_have_no_defined_order.
    nudge.add_dependency(placed)
    nudge.execute()

    assert _translation(stage) == pytest.approx(Gf.Vec3d(0, 7, 0))
    assert placed.get_param("ty").value == 5.0, "the original placement is untouched"
    # Note Placed was never re-cooked: the order each chunk writes is derived from the graph
    # rather than from what it could see at the time, so the copies agree without anyone
    # having to re-run.


def test_removing_the_offset_leaves_the_placement(scene) -> None:
    """The other half of non-destructive, and the thing replacing could never give."""
    stage, sender, scene_layer = scene
    placed = _mk(sender, transform_mod, "Placed")
    placed.get_param("ty").value = 5.0
    placed.execute()
    nudge = _mk(sender, transform_mod, "Nudge")
    nudge.get_param("ty").value = 2.0
    nudge.add_dependency(placed)
    nudge.execute()
    assert _translation(stage) == pytest.approx(Gf.Vec3d(0, 7, 0)), "precondition"

    nudge.request_delete()
    placed.execute()

    assert _translation(stage) == pytest.approx(Gf.Vec3d(0, 5, 0))


def test_two_unconnected_chunks_have_no_defined_order(scene) -> None:
    """The honest limit of deriving the order from the graph: with no connection between
    them there is no graph relationship to derive, so which applies first is undefined.

    Not a gap to fix. Asserting the undefined case has a particular answer would invent a
    guarantee. What IS guaranteed is that both contribute -- neither silently vanishes,
    which is the failure this whole change exists to remove.
    """
    stage, sender, scene_layer = scene
    first = _mk(sender, transform_mod, "T1")
    second = _mk(sender, transform_mod, "T2")
    first.get_param("ty").value = 5.0
    second.get_param("ty").value = 3.0
    first.execute()
    second.execute()

    assert _translation(stage) == pytest.approx(Gf.Vec3d(0, 8, 0))


def test_a_scale_offset_multiplies(scene) -> None:
    """Scale composes by multiplication, which the offset node had to special-case and a
    composing Transform gets for free."""
    stage, sender, scene_layer = scene
    base = _mk(sender, transform_mod, "Base")
    base.get_param("sx").value = 2.0
    base.execute()
    nudge = _mk(sender, transform_mod, "Nudge")
    nudge.get_param("sx").value = 3.0
    nudge.add_dependency(base)
    nudge.execute()

    matrix = UsdGeom.Xformable(stage.GetPrimAtPath("/cube")).GetLocalTransformation(
        Usd.TimeCode.Default()
    )
    matrix = matrix[0] if isinstance(matrix, tuple) else matrix
    assert matrix.GetRow3(0).GetLength() == pytest.approx(6.0)
