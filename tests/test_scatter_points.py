from __future__ import annotations

import importlib.util
import random
import sys
import time
from pathlib import Path

import pytest
from pxr import Gf, Usd, UsdGeom

from usd_chunks.chunk_stage import initialize as _chunk_usd_init
from usd_chunks import chunk_registry, chunk, chunk_stage as _chunk_usd
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.frontends.qt.factory import ChunkWidgetDocker


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _mk(mod, scene):
    cs = mod.create() if mod.loaded else mod.load()
    for c in cs:
        scene.add_chunk(c)
    return cs[0]


def _set_proto(scatter, path, row=0, weight=None, scale=None):
    """Set row `row` of the Prototypes list (creating rows as needed)."""

    rows = scatter.get_param("prototypes", chunk.is_inst(chunk.ListGroup))
    if rows.size <= row:
        rows.size = row + 1
    rows.get_indexed_param(rows.get_param("proto_path"), row).value = path
    if weight is not None:
        rows.get_indexed_param(rows.get_param("proto_weight"), row).value = weight
    if scale is not None:
        rows.get_indexed_param(rows.get_param("proto_scale"), row).value = scale
    return rows


def _wire(dst, src):
    # Wire through the dependency API alone and let ChunkStageSync's listener author
    # inputs:chunk. It writes into the chunk-prim bookkeeping (session) layer, which is where
    # the live graph's own wiring lands. Hand-authoring the connection first goes to the
    # stage's AMBIENT edit target -- the root layer -- and is doubly wrong: the listener then
    # sees the connection already present, skips its own session-layer write, and the
    # root-layer opinion is later dropped, leaving inputs:chunk with no connections at all.
    # The exec chain then reads as unwired nodes and the whole cook set collapses to "nothing
    # cooks" -- no /ScatterInstancer to read at all, rather than a wrong position.
    dst.add_dependency(src)


def test_scatter_is_reactive_and_ignores_downstream_transform() -> None:
    """Regression for the Transform1 -> Scatter -> Transform2 bug reported live in usdview.

    Scatter used to be a manual "Scatter" button reading the plain composed stage, which could
    pick up a downstream chunk's edit to the same prim (layer-strength order reflects DAG depth,
    not who's genuinely upstream of the reader). Scatter is now reactive (a plain module-level
    execute(), like Transform) instead of button-driven -- it runs as part of the same ordered
    DAG solve pass as its upstream, so a chunk wired *after* it is never part of that pass yet
    when it reads. No manual button, no auto-scatter toggle, no hidden-view trick required.
    """
    stage = Usd.Stage.CreateInMemory()

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    lm = chunk_registry.ChunkLayerManager()
    lm.initialize(stage)
    _chunk_usd_init(stage, scene)

    transform_mod = reg.get_chunk_module("transform")
    scatter_mod = reg.get_chunk_module("scatter_points")

    mesh = UsdGeom.Mesh.Define(stage, "/apple")
    mesh.GetPointsAttr().Set([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
    mesh.GetFaceVertexCountsAttr().Set([3])
    mesh.GetFaceVertexIndicesAttr().Set([0, 1, 2])

    t1 = _mk(transform_mod, scene)
    scatter = _mk(scatter_mod, scene)
    t2 = _mk(transform_mod, scene)
    _wire(scatter, t1)
    _wire(t2, scatter)

    t1.get_param("primpath_location").value = "/apple"
    scatter.get_param("surface_prim").value = "/apple"
    scatter.get_param("max_count").value = 1
    t2.get_param("primpath_location").value = "/apple"

    # No button, no toggle -- the old manual-exec affordances are gone.
    assert scatter.get_param("scatter") is None
    assert scatter.get_param("auto_scatter") is None

    def _x() -> float:
        return stage.GetPrimAtPath("/ScatterInstancer").GetAttribute("positions").Get()[0][0]

    t1.get_param("tx").value = 1.0
    x_after_t1 = _x()

    # Transform2 is downstream of Scatter -- must never affect an already-computed scatter.
    t2.get_param("tx").value = 99.0
    assert _x() == x_after_t1

    # Re-triggering via t1 (the real upstream) must still only reflect t1, never t2's 99.
    t1.get_param("tx").value = 5.0
    assert _x() == pytest.approx(x_after_t1 + 4.0, abs=1e-4)


def test_scatter_direct_param_edit_ignores_downstream_transform(qapp) -> None:
    """Regression reported live in usdview: Scatter -> Transform -> View (Transform targets
    the same mesh Scatter scatters from, e.g. to move the original out of the way of the new
    instances). Once a real View has been activated (wired to Transform, the tail), simply
    going back to Scatter and editing one of its own params directly -- not touching Transform
    or the view -- made the scattered instances "jump away" to Transform's offset.

    Root cause: activating the view runs a real recook_for_view pass, whose closing step ranks
    Transform (closer to the view) stronger than Scatter for their shared prim -- correctly, for
    the settled/visible result. But editing Scatter's own param afterward is a *plain* reactive
    param change; it never goes through recook_for_view at all, so nothing re-scopes the read --
    Scatter's execute() just reads the live composed stage, where Transform's edit now
    permanently outranks Scatter's own view of the same prim, regardless of real DAG order.
    execute() now resolves surface_prim/proto_prim through get_upstream_stage(), which is
    unaffected by that ranking -- a pure read-only composition, not dependent on recook state.

    Needs the real Qt graph widget + real view activation (view:slot + _view_state.set_slot) to
    establish that ranking in the first place -- a hand-wired chunk graph with no view/recook
    activity doesn't reach the same layer-strength state (confirmed while investigating the
    equivalent recook_for_view bug earlier)."""

    stage = Usd.Stage.CreateInMemory()

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    lm = chunk_registry.ChunkLayerManager()
    lm.initialize(stage)
    _chunk_usd_init(stage, scene)

    docker = ChunkWidgetDocker()
    docker.resize(500, 600)
    docker.show()
    qapp.processEvents()

    transform_mod = reg.get_chunk_module("transform")
    scatter_mod = reg.get_chunk_module("scatter_points")
    view_mod = reg.get_chunk_module("view")

    mesh = UsdGeom.Mesh.Define(stage, "/apple")
    mesh.GetPointsAttr().Set([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
    mesh.GetFaceVertexCountsAttr().Set([3])
    mesh.GetFaceVertexIndicesAttr().Set([0, 1, 2])

    scatter = _mk(scatter_mod, scene)
    transform = _mk(transform_mod, scene)
    qapp.processEvents()

    scatter.get_param("surface_prim").value = "/apple"
    scatter.get_param("max_count").value = 1
    transform.get_param("primpath_location").value = "/apple"  # same mesh scatter reads from
    _wire(transform, scatter)
    qapp.processEvents()

    def _pump(seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            qapp.processEvents()

    # Activate a real view wired to transform (the tail) -- this is what establishes the
    # "closer to view wins" rank that later poisons a plain param edit on scatter.
    gv = docker.tab_widget.get_current_graph_view()
    context = gv._graph_context()
    view = _mk(view_mod, scene)
    _wire(view, transform)
    qapp.processEvents()
    gv._activate_menu_view(view, context, stage)
    _pump(0.5)
    assert gv._view_state.active_slot, "view must be properly activated for this repro to be faithful"

    def _x() -> float:
        pos = stage.GetPrimAtPath("/ScatterInstancer").GetAttribute("positions").Get()
        return pos[0][0]

    transform.get_param("tx").value = 99.0
    _pump(0.5)

    # Going back to Scatter and editing one of ITS OWN params -- not transform, not the view.
    scatter.get_param("seed").value = 5
    _pump(0.5)

    x = _x()
    assert 0.0 <= x <= 1.0, (
        f"Scatter's own param edit must still only reflect its real upstream (the raw mesh, "
        f"local X in [0,1]) -- transform is downstream and must never leak in. Got x={x}"
    )


def test_scatter_survives_prototype_vanishing_after_upstream_read(monkeypatch) -> None:
    """Regression (issue #82): proto_prim is resolved off get_upstream_stage()'s scratch
    composition (see execute()'s own docstring for why), which can still show a prim whose
    real source (a sublayer, a reference chunk) has since been disconnected from the live
    stage by the time the internal reference is actually authored against it -- a real gap
    between "upstream read" and "write", the same one a shake-to-disconnect recook hits live.
    The unresolved reference used to leave ref_prim typeless and crash Set() on its
    xformOpOrder attr; it should fall back to the same default cube used when no prototype is
    set at all."""

    stage = Usd.Stage.CreateInMemory()

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    lm = chunk_registry.ChunkLayerManager()
    lm.initialize(stage)
    _chunk_usd_init(stage, scene)

    scatter_mod = reg.get_chunk_module("scatter_points")
    scatter = _mk(scatter_mod, scene)

    surface = UsdGeom.Mesh.Define(stage, "/surface")
    surface.GetPointsAttr().Set([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
    surface.GetFaceVertexCountsAttr().Set([3])
    surface.GetFaceVertexIndicesAttr().Set([0, 1, 2])

    scatter.get_param("surface_prim").value = "/surface"
    _set_proto(scatter, "/vanished_proto")
    scatter.get_param("max_count").value = 1

    # get_upstream_stage() is what proto_prim actually gets read through -- point it at a
    # separate stage where the prototype still resolves, while the real stage (what the
    # internal reference is authored against) never had it at all.
    scratch = Usd.Stage.CreateInMemory()
    UsdGeom.Cube.Define(scratch, "/vanished_proto")
    monkeypatch.setattr(_chunk_usd, "get_upstream_stage", lambda *a, **k: scratch)

    # Cooked the way the framework cooks: cook() clears the chunk's layer before execute().
    # Calling execute() bare ran against the proto_0 an earlier cook had already typed as a
    # Cube, so the "typeless means unresolved" check never fired. It only ever passed
    # because the readout Label written at the end of execute() re-cooked the chunk -- a
    # Label no longer does (it is presentation_only).
    assert scatter._cook(), "precondition: the chunk is allowed to cook"  # must not raise

    proto = stage.GetPrimAtPath("/ScatterInstancer/Prototypes/proto_0")
    assert proto.IsValid()
    assert proto.GetTypeName() == "Cube", "should fall back to the default cube, not a dangling reference"
    assert not proto.HasAuthoredReferences(), "no reference should be left dangling"


# ---------------------------------------------------------------------------
# multi-prototype list, variants, stacking
# ---------------------------------------------------------------------------


def _bootstrap():
    """A stage + registry + a Scatter Points chunk, ready to execute() by hand."""

    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)

    mod = reg.get_chunk_module("scatter_points")
    return stage, mod, _mk(mod, scene)


def _plate(stage, path="/plate", half=20.0, height=0.0):
    """A flat quad in the XY plane at z=height."""
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.GetPointsAttr().Set(
        [(-half, -half, height), (half, -half, height), (half, half, height), (-half, half, height)]
    )
    mesh.GetFaceVertexCountsAttr().Set([4])
    mesh.GetFaceVertexIndicesAttr().Set([0, 1, 2, 3])
    return mesh


def _crayon(stage, path="/crayon", length=10.0, thickness=1.0):
    """A box that is long in X and thin in Y/Z -- a crayon lying down."""
    hl, ht = length * 0.5, thickness * 0.5
    mesh = UsdGeom.Mesh.Define(stage, path)
    pts = [
        (-hl, -ht, -ht), (hl, -ht, -ht), (hl, ht, -ht), (-hl, ht, -ht),
        (-hl, -ht, ht), (hl, -ht, ht), (hl, ht, ht), (-hl, ht, ht),
    ]
    mesh.GetPointsAttr().Set(pts)
    mesh.GetFaceVertexCountsAttr().Set([4, 4, 4, 4, 4, 4])
    mesh.GetFaceVertexIndicesAttr().Set(
        [0, 1, 2, 3, 4, 7, 6, 5, 0, 4, 5, 1, 1, 5, 6, 2, 2, 6, 7, 3, 3, 7, 4, 0]
    )
    return mesh


def _instancer(stage):
    return UsdGeom.PointInstancer(stage.GetPrimAtPath("/ScatterInstancer"))


def test_prototype_mix_is_an_exact_quota() -> None:
    """A row's Mix % has to mean an exact count, not a probability -- 25% of 100 is 25."""
    stage, mod, scatter = _bootstrap()
    _plate(stage)
    UsdGeom.Cube.Define(stage, "/proto_a")
    UsdGeom.Cube.Define(stage, "/proto_b")

    _set_proto(scatter, "/proto_a", row=0, weight=75.0)
    _set_proto(scatter, "/proto_b", row=1, weight=25.0)
    scatter.get_param("surface_prim").value = "/plate"
    scatter.get_param("auto_spacing").value = False
    scatter.get_param("spacing").value = 0.0
    scatter.get_param("max_count").value = 100

    mod.module.execute(scatter)

    indices = list(_instancer(stage).GetProtoIndicesAttr().Get())
    assert len(indices) == 100
    assert indices.count(0) == 75
    assert indices.count(1) == 25
    assert len(_instancer(stage).GetPrototypesRel().GetTargets()) == 2
    # Relative, not absolute: 3/1 is the same mix as 75/25.
    _set_proto(scatter, "/proto_a", row=0, weight=3.0)
    _set_proto(scatter, "/proto_b", row=1, weight=1.0)
    mod.module.execute(scatter)
    indices = list(_instancer(stage).GetProtoIndicesAttr().Get())
    assert indices.count(0) == 75 and indices.count(1) == 25

    # ... and the count/prototype outputs other chunks read are filled in.
    assert scatter.get_param("out_count").value == 100
    assert scatter.get_param("out_prototypes").value == 2


def test_a_deactivated_library_resolves_and_hides_its_sources() -> None:
    """Prototypes picked from deactivated entries resolve and are switched on; Hide Sources
    (on by default) deactivates sources that were visible, and off leaves them alone."""
    stage, mod, scatter = _bootstrap()
    _plate(stage)
    UsdGeom.Cube.Define(stage, "/Library/a").GetPrim().SetActive(False)
    UsdGeom.Cube.Define(stage, "/Library/b")

    _set_proto(scatter, "/Library/*")
    scatter.get_param("surface_prim").value = "/plate"
    scatter.get_param("max_count").value = 20
    assert scatter.get_param("hide_sources").value is True
    mod.module.execute(scatter)

    targets = _instancer(stage).GetPrototypesRel().GetTargets()
    assert len(targets) == 2
    assert all(stage.GetPrimAtPath(t).IsActive() for t in targets)
    assert all(stage.GetPrimAtPath(t).GetTypeName() == "Cube" for t in targets)
    assert not stage.GetPrimAtPath("/Library/b").IsActive()

    scatter.get_param("hide_sources").value = False
    mod.module.execute(scatter)
    assert stage.GetPrimAtPath("/Library/b").IsActive()


def test_prototype_variant_selection_in_the_path() -> None:
    """/prim{set=variant} selects per prototype; {set=*} expands to one per variant."""
    stage, mod, scatter = _bootstrap()
    _plate(stage)

    asset = stage.DefinePrim("/asset", "Xform")
    vset = asset.GetVariantSets().AddVariantSet("color")
    for name in ("red", "green", "blue"):
        vset.AddVariant(name)
        vset.SetVariantSelection(name)  # authoring lands in the *selected* variant
        with vset.GetVariantEditContext():
            UsdGeom.Cube.Define(stage, f"/asset/{name}_geom")
    vset.SetVariantSelection("red")

    scatter.get_param("surface_prim").value = "/plate"
    scatter.get_param("max_count").value = 30
    _set_proto(scatter, "/asset{color=blue}")
    mod.module.execute(scatter)

    proto = stage.GetPrimAtPath("/ScatterInstancer/Prototypes/proto_0")
    assert proto.GetVariantSets().GetVariantSet("color").GetVariantSelection() == "blue"
    assert proto.GetChild("blue_geom").IsValid(), "the selected variant's geometry must compose"
    assert not proto.GetChild("red_geom").IsValid(), "and only that variant's"
    assert stage.GetPrimAtPath("/asset").GetVariantSets().GetVariantSet("color").GetVariantSelection() == "red", (
        "the source asset's own selection must be left alone — the selection is per prototype"
    )

    # Wildcard: one prototype per variant, all from the single row.
    _set_proto(scatter, "/asset{color=*}")
    mod.module.execute(scatter)

    selections = [
        stage.GetPrimAtPath(f"/ScatterInstancer/Prototypes/proto_{i}")
        .GetVariantSets()
        .GetVariantSet("color")
        .GetVariantSelection()
        for i in range(3)
    ]
    assert selections == ["blue", "green", "red"], "one prototype per variant, in variant-set order"
    # Each prototype really composes its own variant's geometry, not a shared one.
    for i, name in enumerate(selections):
        proto = stage.GetPrimAtPath(f"/ScatterInstancer/Prototypes/proto_{i}")
        assert proto.GetChild(f"{name}_geom").IsValid()
        assert len(proto.GetChildren()) == 1
    indices = list(_instancer(stage).GetProtoIndicesAttr().Get())
    # One row, so the three variants split that row's share evenly.
    assert {indices.count(i) for i in range(3)} == {10}


def test_stack_mode_packs_tight_stands_up_and_rests_on_the_surface() -> None:
    """The crayons-in-a-cup case: pitch from the prototype's oriented bounds.

    Long axis turned to world up, every instance's bounding box bottom exactly on the plate,
    no two instances closer laterally than their footprint, and a second level lifted by
    exactly the prototype's height.
    """

    stage, mod, scatter = _bootstrap()
    _plate(stage, half=10.0, height=3.0)
    _crayon(stage, length=10.0, thickness=1.0)

    _set_proto(scatter, "/crayon")
    scatter.get_param("surface_prim").value = "/plate"
    scatter.get_param("scatter_mode").value = "Stack"
    scatter.get_param("max_count").value = 500
    mod.module.execute(scatter)

    inst = _instancer(stage)
    positions = list(inst.GetPositionsAttr().Get())
    orientations = list(inst.GetOrientationsAttr().Get())
    assert len(positions) > 50, f"a 20x20 plate should pack many 1-unit crayons, got {len(positions)}"

    # Standing up: the prototype's long axis (X) ends up along world up (Z).
    for quat in orientations[:20]:
        rotated = Gf.Quatf(quat.GetReal(), Gf.Vec3f(*quat.GetImaginary())).Transform(Gf.Vec3f(1, 0, 0))
        assert rotated[2] == pytest.approx(1.0, abs=1e-3), f"long axis should point up, got {rotated}"

    # Resting exactly on the plate: centre is half the crayon's length above it.
    for p in positions:
        assert p[2] == pytest.approx(3.0 + 5.0, abs=1e-3)

    # Tight but not intersecting: never closer than the 1-unit footprint.
    worst = min(
        (positions[i][0] - positions[j][0]) ** 2 + (positions[i][1] - positions[j][1]) ** 2
        for i in range(len(positions))
        for j in range(i + 1, len(positions))
    )
    assert worst ** 0.5 >= 1.0 - 1e-4, f"instances overlap: closest pair {worst ** 0.5}"

    # A second level sits exactly one crayon-height higher.
    scatter.get_param("pack_levels").value = 2
    mod.module.execute(scatter)
    zs = sorted({round(p[2], 3) for p in _instancer(stage).GetPositionsAttr().Get()})
    assert zs == [8.0, 18.0]


def test_stack_mode_respects_the_surface_outline_and_padding() -> None:
    """Conform drops lattice points with no surface under them, so the pack follows the mesh.

    Padding widens the pitch, which must reduce the count.
    """
    stage, mod, scatter = _bootstrap()
    # Two separate small plates with a gap between them: nothing may land in the gap.
    _plate(stage, path="/plate_a", half=5.0)
    left = _plate(stage, path="/plate_b", half=5.0)
    UsdGeom.Xformable(left).AddTranslateOp().Set((30.0, 0.0, 0.0))
    group = stage.DefinePrim("/plates", "Xform")
    stage.GetPrimAtPath("/plate_a").GetParent()  # plates are siblings; scatter both explicitly
    assert group.IsValid()

    UsdGeom.Cube.Define(stage, "/proto")
    _set_proto(scatter, "/proto")
    scatter.get_param("surface_prim").value = "/plate_a,/plate_b"
    scatter.get_param("scatter_mode").value = "Stack"
    scatter.get_param("max_count").value = 2000
    mod.module.execute(scatter)

    xs = [p[0] for p in _instancer(stage).GetPositionsAttr().Get()]
    assert xs, "expected instances on both plates"
    assert not any(6.0 < x < 24.0 for x in xs), "nothing may be placed in the gap between plates"

    tight = len(xs)
    scatter.get_param("pack_padding").value = 4.0
    mod.module.execute(scatter)
    assert len(_instancer(stage).GetPositionsAttr().Get()) < tight


def test_legacy_proto_prim_migrates_into_the_list() -> None:
    """A scene saved before the Prototypes list keeps working, and shows up in the new UI."""

    stage, mod, scatter = _bootstrap()
    _plate(stage)
    UsdGeom.Cube.Define(stage, "/old_proto")

    scatter.get_param("surface_prim").value = "/plate"
    scatter.get_param("max_count").value = 5
    scatter.get_param("proto_prim").value = "/old_proto"  # the pre-list field

    mod.module.execute(scatter)

    rows = scatter.get_param("prototypes", chunk.is_inst(chunk.ListGroup))
    assert rows.get_indexed_param(rows.get_param("proto_path"), 0).value == "/old_proto"
    assert not scatter.get_param("proto_prim").value, "legacy field is cleared once migrated"
    proto = stage.GetPrimAtPath("/ScatterInstancer/Prototypes/proto_0")
    assert proto.GetPrimStack()  # composed
    assert [str(r.assetPath or r.primPath) for r in proto.GetMetadata("references").ApplyOperations([])] == [
        "/old_proto"
    ]


def test_editing_a_prototype_row_recooks_on_its_own() -> None:
    """A list row has to behave like any other param: touching it re-cooks the scatter.

    Verified live before being written down (the whole point of the list is that it is
    editable in the UI, not just from execute()).
    """

    stage, _, scatter = _bootstrap()
    _plate(stage)
    UsdGeom.Cube.Define(stage, "/red")
    UsdGeom.Cube.Define(stage, "/blue")

    scatter.get_param("surface_prim").value = "/plate"
    scatter.get_param("max_count").value = 20

    rows = scatter.get_param("prototypes", chunk.is_inst(chunk.ListGroup))
    rows.get_indexed_param(rows.get_param("proto_path"), 0).value = "/red"
    proto = stage.GetPrimAtPath("/ScatterInstancer/Prototypes/proto_0")
    assert proto.IsValid() and proto.GetChildren() or proto.GetTypeName(), "row edit must re-cook"
    assert _instancer(stage).GetProtoIndicesAttr().Get(), "instances must exist after the row edit"

    rows.size = 2  # adding a row is a param change too
    rows.get_indexed_param(rows.get_param("proto_path"), 1).value = "/blue"
    assert len(_instancer(stage).GetPrototypesRel().GetTargets()) == 2
    indices = list(_instancer(stage).GetProtoIndicesAttr().Get())
    assert indices.count(0) == 10 and indices.count(1) == 10, "two equal rows split the mix evenly"


def test_stack_can_lay_prototypes_down_along_the_rows() -> None:
    """Lay Along Rows turns the long axis into the row direction -- a woodpile, not a cup."""

    stage, mod, scatter = _bootstrap()
    _plate(stage, half=10.0)
    _crayon(stage, length=10.0, thickness=1.0)

    _set_proto(scatter, "/crayon")
    scatter.get_param("surface_prim").value = "/plate"
    scatter.get_param("scatter_mode").value = "Stack"
    scatter.get_param("stack_orient").value = "Lay Along Rows"
    scatter.get_param("stack_row_axis").value = "Y"
    scatter.get_param("max_count").value = 50
    mod.module.execute(scatter)

    orientations = list(_instancer(stage).GetOrientationsAttr().Get())
    assert orientations
    for quat in orientations[:10]:
        rotated = Gf.Quatf(quat.GetReal(), Gf.Vec3f(*quat.GetImaginary())).Transform(Gf.Vec3f(1, 0, 0))
        assert abs(rotated[1]) == pytest.approx(1.0, abs=1e-3), f"long axis should lie along Y, got {rotated}"
    # Lying down, the crayon is only 1 unit tall, so it rests half a thickness up.
    for p in _instancer(stage).GetPositionsAttr().Get():
        assert p[2] == pytest.approx(0.5, abs=1e-3)


def test_spacing_default_and_collapsed_groups() -> None:
    """Spacing starts at 10, and the long groups start collapsed."""

    _, _, scatter = _bootstrap()
    assert scatter.get_param("spacing").default_value == pytest.approx(10.0)
    for name in ("Distribution", "Stacking", "Region", "Variation"):
        group = scatter.get_param(name)
        assert group is not None, f"missing group {name}"
        assert group.has_state(chunk.ChunkFlags.State.COLLAPSED), f"{name} should start collapsed"
    # Stack-only controls stay out of the way until the mode asks for them.
    assert scatter.get_param("Stacking").has_state(chunk.ChunkFlags.State.HIDDEN)
    scatter.get_param("scatter_mode").value = "Stack"
    assert not scatter.get_param("Stacking").has_state(chunk.ChunkFlags.State.HIDDEN)
    assert scatter.get_param("Region").has_state(chunk.ChunkFlags.State.HIDDEN)


def test_stack_with_no_target_packs_a_free_standing_block() -> None:
    """Stack in mid-air, with nothing to pack against.

    Wanted for "pencils into a cup", which nothing here does well: Volume fills a closed mesh's
    *solid* -- a cup's is its ceramic, so pencils land in the wall -- and Stack needs a surface.
    The route is to stack the block in mid-air, move it over the cup with a Transform and let
    the RBD Solver drop it in. That used to need a throwaway plane to stack on.
    """
    stage, mod, scatter = _bootstrap()
    UsdGeom.Cube.Define(stage, "/proto")
    _set_proto(scatter, "/proto")
    scatter.get_param("surface_prim").value = ""      # nothing to pack against
    scatter.get_param("scatter_mode").value = "Stack"
    scatter.get_param("max_count").value = 16
    mod.module.execute(scatter)

    inst = UsdGeom.PointInstancer(stage.GetPrimAtPath("/ScatterInstancer"))
    positions = inst.GetPositionsAttr().Get() or []
    assert len(positions) > 0, "an empty target still produced nothing"

    # A block, not a line: it has to read as a stack you can pick up and move.
    xs = {round(p[0], 3) for p in positions}
    ys = {round(p[1], 3) for p in positions}
    assert len(xs) > 1 and len(ys) > 1, f"not a 2D block: {len(xs)} x {len(ys)}"

    #
    # Centred on the origin so a Transform moves it predictably. Judged against the block's own
    # size rather than to the millimetre: hex rows offset by half a pitch and lose whichever
    # points fall off the edge, which leans the average slightly, and that is the pattern
    # working rather than a misplaced block.
    cx = sum(p[0] for p in positions) / len(positions)
    cy = sum(p[1] for p in positions) / len(positions)
    span_x = max(p[0] for p in positions) - min(p[0] for p in positions)
    span_y = max(p[1] for p in positions) - min(p[1] for p in positions)
    tol = max(span_x, span_y, 1.0) * 0.1
    assert abs(cx) < tol and abs(cy) < tol, f"block is not centred: {cx}, {cy} (tol {tol})"


def test_stack_still_prefers_a_real_surface_when_it_has_one() -> None:
    """Free-standing is the fallback, not the new behaviour."""
    stage, mod, scatter = _bootstrap()
    _plate(stage)
    UsdGeom.Cube.Define(stage, "/proto")
    _set_proto(scatter, "/proto")
    scatter.get_param("scatter_mode").value = "Stack"
    mod.module.execute(scatter)

    inst = UsdGeom.PointInstancer(stage.GetPrimAtPath("/ScatterInstancer"))
    positions = inst.GetPositionsAttr().Get() or []
    assert positions, "stacking on a real surface produced nothing"
    # The plate sits above the origin, so a surface-driven pack cannot be the origin block.
    assert max(p[2] for p in positions) > 0.01


def test_row_padding_is_independent_of_along_row_padding() -> None:
    """Reported live: crayons packed tight along X but left gaps along Y.

    One Padding for both lateral axes cannot fix that -- an asset's bounding box is usually
    only loose in one direction, so closing the gap with a single number ruins the axis that
    was already right. Negative values are the escape hatch for a box roomier than the model.
    """
    stage, mod, scatter = _bootstrap()
    UsdGeom.Cube.Define(stage, "/proto")
    _set_proto(scatter, "/proto")
    scatter.get_param("surface_prim").value = ""
    scatter.get_param("scatter_mode").value = "Stack"
    scatter.get_param("max_count").value = 16
    scatter.get_param("pack_pattern").value = "Grid"

    def spans():
        mod.module.execute(scatter)
        inst = UsdGeom.PointInstancer(stage.GetPrimAtPath("/ScatterInstancer"))
        pts = inst.GetPositionsAttr().Get() or []
        assert pts
        return (
            max(p[0] for p in pts) - min(p[0] for p in pts),
            max(p[1] for p in pts) - min(p[1] for p in pts),
        )

    base_x, base_y = spans()

    scatter.get_param("pack_row_padding").value = 5.0
    wide_x, wide_y = spans()
    assert wide_y > base_y + 1e-3, "Row Padding did not open the rows up"
    assert abs(wide_x - base_x) < 1e-3, "Row Padding leaked into the along-row axis"

    scatter.get_param("pack_row_padding").value = 0.0
    scatter.get_param("pack_padding").value = 5.0
    pad_x, pad_y = spans()
    assert pad_x > base_x + 1e-3, "Padding did not open the row up"
    assert abs(pad_y - base_y) < 1e-3, "Padding leaked into the row axis"


def _scatter_module():
    """chunks/modelling/scatter_points.py itself, for its orientation helpers."""

    path = Path(__file__).resolve().parent.parent / "chunks" / "modelling" / "scatter_points.py"
    spec = importlib.util.spec_from_file_location("_scatter_points_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look themselves up there
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("up", [Gf.Vec3f(0, 1, 0), Gf.Vec3f(0, 0, 1)], ids=["y_up", "z_up"])
def test_align_to_normal_keeps_an_upright_prototype_upright_on_either_up_axis(up) -> None:
    # A House stands along the stage's up axis. On flat ground the normal IS up, so aligning
    # to it must leave the instance as it is -- it tipped 90 degrees on a Y-up stage
    # (Houdini's) when the prototype's up was taken to be +Z.
    mod = _scatter_module()
    quat = mod._compute_orientation("Align to Normal", up, random.Random(0), None, up)
    rotated = Gf.Rotation(Gf.Quatd(quat.GetReal(), Gf.Vec3d(quat.GetImaginary()))).TransformDir(Gf.Vec3d(up))
    assert Gf.IsClose(rotated, Gf.Vec3d(up), 1e-5)

    # On a slope it still turns the prototype's up onto the surface normal.
    slope = Gf.Vec3f(0.6, 0.8, 0) if up[1] else Gf.Vec3f(0.6, 0, 0.8)
    quat = mod._compute_orientation("Align to Normal", slope, random.Random(0), None, up)
    rotated = Gf.Rotation(Gf.Quatd(quat.GetReal(), Gf.Vec3d(quat.GetImaginary()))).TransformDir(Gf.Vec3d(up))
    assert Gf.IsClose(rotated, Gf.Vec3d(slope), 1e-5)


def test_viewport_proxy_keeps_each_half_of_an_asset_with_its_own_purposes() -> None:
    """An asset with its own render/proxy meshes: Hydra resolves purpose inside a prototype,
    so the render instancer keeps only the render half and the proxy instancer the proxy
    half -- otherwise the viewport draws the render instancer's proxy meshes for every
    instance."""
    stage, mod, scatter = _bootstrap()
    _plate(stage)
    asset = UsdGeom.Xform.Define(stage, "/asset")
    full_mesh = UsdGeom.Cube.Define(stage, "/asset/render")
    full_mesh.CreatePurposeAttr().Set(UsdGeom.Tokens.render)
    light = UsdGeom.Cube.Define(stage, "/asset/proxy")
    light.CreatePurposeAttr().Set(UsdGeom.Tokens.proxy)
    _set_proto(scatter, str(asset.GetPath()))
    scatter.get_param("surface_prim").value = "/plate"
    scatter.get_param("max_count").value = 20
    scatter.get_param("proxy_mode").value = "Full"
    mod.module.execute(scatter)

    full = _instancer(stage).GetPrototypesRel().GetTargets()[0]
    proxy = UsdGeom.PointInstancer(stage.GetPrimAtPath("/ScatterInstancer_proxy")).GetPrototypesRel().GetTargets()[0]
    assert stage.GetPrimAtPath(full.AppendChild("render")).IsActive()
    assert not stage.GetPrimAtPath(full.AppendChild("proxy")).IsActive()
    assert stage.GetPrimAtPath(proxy.AppendChild("proxy")).IsActive()
    assert not stage.GetPrimAtPath(proxy.AppendChild("render")).IsActive()

    # Off: one instancer drawn everywhere keeps the asset whole.
    scatter.get_param("proxy_mode").value = "Off"
    mod.module.execute(scatter)
    full = _instancer(stage).GetPrototypesRel().GetTargets()[0]
    assert stage.GetPrimAtPath(full.AppendChild("proxy")).IsActive()


@pytest.mark.parametrize("mode", ["Bounds", "Full"])
def test_viewport_proxy_is_a_purpose_split_subset(mode) -> None:
    """Proxy on: the full scatter is purpose=render, a sibling `_proxy` instancer is
    purpose=proxy and keeps a seeded share of the same instances."""
    stage, mod, scatter = _bootstrap()
    _plate(stage)
    _crayon(stage)
    _set_proto(scatter, "/crayon")
    scatter.get_param("surface_prim").value = "/plate"
    scatter.get_param("auto_spacing").value = False
    scatter.get_param("spacing").value = 0.0
    scatter.get_param("max_count").value = 100

    mod.module.execute(scatter)
    assert not _instancer(stage).GetPurposeAttr().HasAuthoredValue()
    assert not stage.GetPrimAtPath("/ScatterInstancer_proxy")

    scatter.get_param("proxy_mode").value = mode
    scatter.get_param("proxy_share").value = 0.25
    mod.module.execute(scatter)

    full = _instancer(stage)
    proxy = UsdGeom.PointInstancer(stage.GetPrimAtPath("/ScatterInstancer_proxy"))
    assert full.GetPurposeAttr().Get() == UsdGeom.Tokens.render
    assert proxy.GetPurposeAttr().Get() == UsdGeom.Tokens.proxy
    assert len(full.GetPositionsAttr().Get()) == 100
    kept = list(proxy.GetPositionsAttr().Get())
    assert len(kept) == 25
    assert set(map(tuple, kept)) <= set(map(tuple, full.GetPositionsAttr().Get()))
    assert len(proxy.GetProtoIndicesAttr().Get()) == 25

    targets = proxy.GetPrototypesRel().GetTargets()
    assert len(targets) == 1
    assert targets[0].HasPrefix("/ScatterInstancer_proxy")
    # The render instancer keeps its own prototypes.
    assert full.GetPrototypesRel().GetTargets()[0].HasPrefix("/ScatterInstancer")
    if mode == "Bounds":
        assert stage.GetPrimAtPath(targets[0]).IsA(UsdGeom.Cube)


def test_without_scipy_it_places_nothing_and_says_why(monkeypatch) -> None:
    """scipy is a requirement: without it the spacing tests ran in Python, and a full
    scene cooked for minutes -- in Blender, long enough that the install prompt waiting for
    an idle moment never came. So a cook with no scipy places nothing and names it."""
    stage, mod, scatter = _bootstrap()
    # First: setting the params below cooks the chunk too.
    monkeypatch.setattr(mod.module, "find_spec", lambda name: None)
    _plate(stage)
    UsdGeom.Cube.Define(stage, "/proto_a")
    _set_proto(scatter, "/proto_a")
    scatter.get_param("surface_prim").value = "/plate"
    scatter.get_param("max_count").value = 100

    mod.module.execute(scatter)

    assert not stage.GetPrimAtPath("/ScatterInstancer")
    assert "needs scipy" in scatter.get_param("readout").value
