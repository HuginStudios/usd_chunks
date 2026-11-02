"""Two questions about auto-create: multi-selection, and deleting the chunk it made.

Written to find out what actually happens, not to assert a decision already taken.
"""
from __future__ import annotations

import importlib

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom

from usd_chunks import chunk, chunk_naming, chunk_registry
from usd_chunks.gizmo import runtime as chunk_gizmo
from usd_chunks.chunk_core import ChunkConfig, ChunkFlags
from usd_chunks.chunk_scene import SceneSender
from usd_chunks import chunk_stage

from chunk_stubs import attach_stub_module

transform_mod = importlib.import_module("chunks.transforms.transform")
merge_mod = importlib.import_module("chunks.core.merge")


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


class _StubModule:
    def __init__(self, mod=transform_mod, name="transform"):
        self.loaded = True
        self.source_hash = None
        self.creates = 0
        # Registered under its name, so this is where the chunks it creates find their
        # hooks (ChunkBase.get_chunk_module): the real module's.
        self.module = mod
        self._name = name

    def create(self):
        self.creates += 1
        c = self.module.main()
        c._config = ChunkConfig(module=self._name, dynamic_exec_inputs=self._name == "merge")
        return [c]

    load = create


@pytest.fixture
def scene(monkeypatch):
    stage = Usd.Stage.CreateInMemory()
    scene_layer = Sdf.Layer.CreateAnonymous("scene")
    stage.GetRootLayer().subLayerPaths.append(scene_layer.identifier)
    with Usd.EditContext(stage, scene_layer):
        for name in ("A", "B", "C"):
            UsdGeom.Cube.Define(stage, f"/World/{name}")

    registry = chunk_registry.ChunkRegistry()
    sender = _Scene(stage)
    registry.initialize(sender, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_stage.ChunkStageSync().initialize(stage, sender)

    module = _StubModule()
    monkeypatch.setitem(registry.registered_modules, "transform", module)
    monkeypatch.setattr(chunk_stage, "_params_tab_create", False, raising=False)
    monkeypatch.setattr(chunk_stage, "_loft_new_chunks", None, raising=False)
    monkeypatch.setattr(chunk_stage, "_create_node_pos", None, raising=False)
    return stage, registry, module


def _prim(stage, name):
    return stage.GetPrimAtPath(f"/World/{name}")


# ---------------------------------------------------------------------------
# multi-selection
# ---------------------------------------------------------------------------

def test_each_selected_prim_gets_its_own_chunk(scene) -> None:
    """The host commits per prim, so N unowned prims in one drag make N chunks.

    That is noisy but correct, and the alternative is worse. transform.py authors ONE
    xformOp:transform on every path it targets, so a single chunk driving three prims would
    snap all three to the same place -- it has no per-prim delta to preserve their relative
    positions with. One chunk each keeps them where they are.
    """
    stage, _registry, module = scene

    descs = [chunk_gizmo.ensure_transform_chunk(_prim(stage, n)) for n in ("A", "B", "C")]

    assert module.creates == 3
    assert len({d.chunk_prim for d in descs}) == 3, "chunks must be distinct, not shared"


def test_each_chunk_targets_only_its_own_prim(scene) -> None:
    stage, _registry, _module = scene

    for name in ("A", "B", "C"):
        desc = chunk_gizmo.ensure_transform_chunk(_prim(stage, name))
        created = chunk_gizmo.chunk_at_prim_path(desc.chunk_prim)
        assert created.get_param("primpath_location").value == f"/World/{name}"


def test_multi_selection_keeps_prims_where_they_were(scene) -> None:
    """The reason one-chunk-per-prim matters: they start in different places."""
    stage, _registry, _module = scene
    for name, x in (("A", 1.0), ("B", 5.0), ("C", 9.0)):
        with Usd.EditContext(stage, stage.GetRootLayer().subLayerPaths and
                             Sdf.Layer.Find(stage.GetRootLayer().subLayerPaths[0])):
            UsdGeom.Xformable(_prim(stage, name)).AddTransformOp().Set(
                Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(x, 0, 0)))

    for name in ("A", "B", "C"):
        desc = chunk_gizmo.ensure_transform_chunk(_prim(stage, name))
        chunk_gizmo.chunk_at_prim_path(desc.chunk_prim).execute()

    for name, x in (("A", 1.0), ("B", 5.0), ("C", 9.0)):
        local = UsdGeom.Xformable(_prim(stage, name)).GetLocalTransformation(Usd.TimeCode.Default())
        local = local[0] if isinstance(local, tuple) else local
        assert Gf.IsClose(local.ExtractTranslation(), Gf.Vec3d(x, 0, 0), 1e-6), name


# ---------------------------------------------------------------------------
# a run of drags gathers into a Merge
# ---------------------------------------------------------------------------

@pytest.fixture
def gathering(scene, monkeypatch):
    """scene, with a Merge to gather into and a chunk at the head of the chain."""
    stage, registry, module = scene
    monkeypatch.setitem(registry.registered_modules, "merge", _StubModule(merge_mod, "merge"))
    head = chunk.Chunk("Head")
    attach_stub_module(head)
    registry.scene.add_chunk(head)
    return stage, registry, module, chunk_stage.get_chunk_prim_path(head)


def _sources(stage, path):
    return [str(c.GetPrimPath())
            for c in chunk_stage.exec_input_connections(stage.GetPrimAtPath(str(path)))]


def test_drags_in_a_row_gather_into_one_merge(gathering) -> None:
    """Chained, a dozen drags make a dozen nodes in a row; they move different prims, so
    side by side in one Merge is the same result without the line growing."""
    stage, _registry, _module, head = gathering

    paths = [str(chunk_gizmo.ensure_transform_chunk(_prim(stage, n)).chunk_prim) for n in "ABC"]

    merges = [p for p in stage.Traverse() if chunk_stage.is_merge_prim(p)]
    assert len(merges) == 1
    # The newest strongest, then the older Transforms, then what came before, weakest:
    # each outranks what came before it, as it would chained after it.
    assert _sources(stage, merges[0].GetPath()) == [*reversed(paths), head]
    for path in paths:
        assert _sources(stage, path) == [], "a gathered Transform needs no input of its own"


def test_a_merge_gathered_with_the_upstream_strongest_is_put_right(gathering) -> None:
    """Merges gathered before the upstream moved to the weakest slot have it in slot 0."""
    stage, registry, _module, head = gathering
    a, b = (str(chunk_gizmo.ensure_transform_chunk(_prim(stage, n)).chunk_prim) for n in "AB")
    (merge,) = [p for p in stage.Traverse() if chunk_stage.is_merge_prim(p)]
    for slot, source in enumerate([head, a, b]):  # the old layout
        chunk_stage.connect_exec(stage, registry.scene, source, str(merge.GetPath()),
                                 input_name=chunk_naming.exec_input_name(slot))

    c = str(chunk_gizmo.ensure_transform_chunk(_prim(stage, "C")).chunk_prim)

    assert _sources(stage, merge.GetPath()) == [c, a, b, head]


def test_the_transforms_stack_downward_like_the_slots(gathering) -> None:
    stage, _registry, _module, _head = gathering

    paths = [str(chunk_gizmo.ensure_transform_chunk(_prim(stage, n)).chunk_prim) for n in "ABC"]

    xs, ys = zip(*(chunk_gizmo._node_pos(stage.GetPrimAtPath(p)) for p in paths))
    assert len(set(xs)) == 1
    assert ys[0] > ys[1] > ys[2], "each one below the last, newest at the bottom (USD is Y-up)"


def test_a_dragged_transform_starts_collapsed(scene) -> None:
    """Like a node made from the menu: a run of drags stacks them, and headers stack short."""
    stage, _registry, _module = scene

    desc = chunk_gizmo.ensure_transform_chunk(_prim(stage, "A"))

    assert chunk_gizmo.chunk_at_prim_path(desc.chunk_prim).has_state(ChunkFlags.State.COLLAPSED)


def test_the_merge_takes_the_output(gathering) -> None:
    stage, registry, _module, _head = gathering
    out = chunk.Chunk("Output")
    out._config = ChunkConfig(module="output", terminal=True)
    registry.scene.add_chunk(out)
    first = chunk_gizmo.ensure_transform_chunk(_prim(stage, "A")).chunk_prim
    out_prim = stage.GetPrimAtPath(chunk_stage.get_chunk_prim_path(out))
    out_prim.CreateAttribute(chunk_stage._EXEC_INPUT, Sdf.ValueTypeNames.Token).SetConnections(
        [Sdf.Path(str(first)).AppendProperty(chunk_stage._EXEC_OUTPUT)])

    chunk_gizmo.ensure_transform_chunk(_prim(stage, "B"))

    (merge,) = [p for p in stage.Traverse() if chunk_stage.is_merge_prim(p)]
    assert _sources(stage, out_prim.GetPath()) == [str(merge.GetPath())]


def test_gathered_transforms_keep_prims_where_they_were(gathering) -> None:
    stage, _registry, _module, _head = gathering
    for name, x in (("A", 1.0), ("B", 5.0), ("C", 9.0)):
        with Usd.EditContext(stage, Sdf.Layer.Find(stage.GetRootLayer().subLayerPaths[-1])):
            UsdGeom.Xformable(_prim(stage, name)).AddTransformOp().Set(
                Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(x, 0, 0)))

    for name in "ABC":
        desc = chunk_gizmo.ensure_transform_chunk(_prim(stage, name))
        chunk_gizmo.chunk_at_prim_path(desc.chunk_prim).execute()

    for name, x in (("A", 1.0), ("B", 5.0), ("C", 9.0)):
        local = UsdGeom.Xformable(_prim(stage, name)).GetLocalTransformation(Usd.TimeCode.Default())
        local = local[0] if isinstance(local, tuple) else local
        assert Gf.IsClose(local.ExtractTranslation(), Gf.Vec3d(x, 0, 0), 1e-6), name


def test_without_a_merge_module_the_chain_grows(gathering, monkeypatch) -> None:
    stage, registry, _module, head = gathering
    monkeypatch.delitem(registry.registered_modules, "merge")

    a, b = (str(chunk_gizmo.ensure_transform_chunk(_prim(stage, n)).chunk_prim) for n in "AB")

    assert _sources(stage, a) == [head]
    assert _sources(stage, b) == [a]
    assert not any(chunk_stage.is_merge_prim(p) for p in stage.Traverse())


# ---------------------------------------------------------------------------
# deleting the chunk that was created
# ---------------------------------------------------------------------------

def test_deleting_the_chunk_releases_the_prim(scene) -> None:
    stage, registry, module = scene
    desc = chunk_gizmo.ensure_transform_chunk(_prim(stage, "A"))
    created = chunk_gizmo.chunk_at_prim_path(desc.chunk_prim)

    created.request_delete()

    assert chunk_gizmo.placement_gizmos(_prim(stage, "A")) == [], (
        "the prim still reports as owned by a chunk that no longer exists"
    )


def test_the_prim_goes_back_where_it_was(scene) -> None:
    """Deleting the chunk takes its layer with it, so the override it authored goes too."""
    stage, _registry, _module = scene
    desc = chunk_gizmo.ensure_transform_chunk(_prim(stage, "A"))
    created = chunk_gizmo.chunk_at_prim_path(desc.chunk_prim)
    chunk_gizmo.apply_bindings(
        desc.chunk_prim, desc.name, Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(7.0, 0, 0))
    )
    created.execute()

    created.request_delete()

    local = UsdGeom.Xformable(_prim(stage, "A")).GetLocalTransformation(Usd.TimeCode.Default())
    local = local[0] if isinstance(local, tuple) else local
    assert Gf.IsClose(local.ExtractTranslation(), Gf.Vec3d(0, 0, 0), 1e-6)


def test_dragging_again_after_a_delete_makes_a_fresh_chunk(scene) -> None:
    stage, _registry, module = scene
    first = chunk_gizmo.ensure_transform_chunk(_prim(stage, "A"))
    chunk_gizmo.chunk_at_prim_path(first.chunk_prim).request_delete()

    second = chunk_gizmo.ensure_transform_chunk(_prim(stage, "A"))

    assert module.creates == 2
    assert second is not None
    assert chunk_gizmo.chunk_at_prim_path(second.chunk_prim) is not None
