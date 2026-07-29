from __future__ import annotations

import time
import types as _types
from pathlib import Path

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom

from usd_chunks.plugins.chunk_usd import (
    _CHUNK_PRIM_TYPENAME,
    _CHUNK_TYPE_ATTR,
    _CHUNKS_ROOT,
    _EXEC_INPUT,
    _EXEC_OUTPUT,
    _INPUTS_PREFIX,
    _author_prim,
    _load_wire_inputs,
    ChunkStageSync,
    export_chunks,
    initialize as _chunk_usd_init,
    load_chunks,
    loft_param_to_context,
    rename_context_prim_path,
)
from usd_chunks.chunk_utils import get_unique_path
from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_core import ChunkBase, ChunkConfig, ChunkFlags, SceneSender


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


chunk_registry.ChunkRegistry().initialize(_DummyScene(), "pytest")


def _chunk_with_module(name: str, module: str = "prim") -> chunk.Chunk:
    c = chunk.Chunk(name)
    c._config = ChunkConfig(module=module)
    return c


# ---------------------------------------------------------------------------
# basic structure
# ---------------------------------------------------------------------------

def test_export_chunk_creates_prim() -> None:
    """Authoring a Chunk prim produces a typed Chunk prim under /chunks/<name>."""
    c = _chunk_with_module("myChunk")

    stage = Usd.Stage.CreateInMemory()
    _author_prim(stage, c)

    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/{c.name}")
    assert prim.IsValid()
    assert prim.GetTypeName() == "Chunk"
    assert prim.GetAttribute(_CHUNK_TYPE_ATTR).Get() == "prim"
    assert prim.GetAttribute(_EXEC_OUTPUT).IsValid()


def test_export_prev_chunk_wires_exec_connection() -> None:
    """prev_chunk creates an inputs:chunk connection on the downstream prim."""
    chunk_a = _chunk_with_module("nodeA")
    chunk_b = _chunk_with_module("nodeB")

    stage = Usd.Stage.CreateInMemory()
    _author_prim(stage, chunk_a, index=0)
    _author_prim(stage, chunk_b, prev_chunk=chunk_a, index=1)

    prim_b = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/nodeB")
    exec_in = prim_b.GetAttribute(_EXEC_INPUT)
    assert exec_in.IsValid()
    assert any("nodeA" in str(conn) for conn in exec_in.GetConnections())


# ---------------------------------------------------------------------------
# scalar roundtrips
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("make_param,set_value,expected", [
    pytest.param(
        lambda: chunk.Checkbox("p", default_value=False), True, True,
        id="bool",
    ),
    pytest.param(
        lambda: chunk.Slider("p", is_int=True), 5, 5,
        id="int",
    ),
    pytest.param(
        lambda: chunk.Slider("p"), 0.75, pytest.approx(0.75),
        id="float",
    ),
    pytest.param(
        lambda: chunk.LineEdit("p"), "hello", "hello",
        id="str",
    ),
])
def test_scalar_roundtrip(make_param, set_value, expected) -> None:
    """Each scalar param type survives an export → _load_wire_inputs roundtrip."""
    src_param = make_param()
    src_param.value = set_value

    src = _chunk_with_module("n")
    src.add_param(src_param)

    stage = Usd.Stage.CreateInMemory()
    _author_prim(stage, src)

    dst = chunk.Chunk("n")
    dst_param = make_param()
    dst.add_param(dst_param)

    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/n")
    _load_wire_inputs(prim, dst)
    assert dst_param.value == expected


# ---------------------------------------------------------------------------
# vector roundtrips
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("length,is_int,val,expected_gf", [
    pytest.param(2, False, (1.0, 2.0),            Gf.Vec2f(1.0, 2.0),            id="Vec2f"),
    pytest.param(3, False, (1.0, 0.5, 0.25),      Gf.Vec3f(1.0, 0.5, 0.25),      id="Vec3f"),
    pytest.param(4, False, (1.0, 0.5, 0.25, 0.1), Gf.Vec4f(1.0, 0.5, 0.25, 0.1), id="Vec4f"),
    pytest.param(2, True,  (3, 7),                Gf.Vec2i(3, 7),                id="Vec2i"),
    pytest.param(3, True,  (1, 2, 3),             Gf.Vec3i(1, 2, 3),             id="Vec3i"),
    pytest.param(4, True,  (1, 2, 3, 4),          Gf.Vec4i(1, 2, 3, 4),          id="Vec4i"),
])
def test_vector_roundtrip(length, is_int, val, expected_gf) -> None:
    """Vector params of every supported length and element type survive the roundtrip."""
    def make_vec():
        return chunk.Vector("v", param=chunk.Slider(is_int=is_int), length=length)

    src_vec = make_vec()
    src_vec.value = val

    src = _chunk_with_module("n")
    src.add_param(src_vec)

    stage = Usd.Stage.CreateInMemory()
    _author_prim(stage, src)

    dst = chunk.Chunk("n")
    dst_vec = make_vec()
    dst.add_param(dst_vec)

    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/n")
    _load_wire_inputs(prim, dst)
    assert tuple(dst_vec.value) == pytest.approx(tuple(expected_gf))


# ---------------------------------------------------------------------------
# nested param roundtrip
# ---------------------------------------------------------------------------

def test_nested_param_roundtrip() -> None:
    src = _chunk_with_module("n")
    grp = chunk.Group("grp")
    slider = chunk.Slider("s")
    slider.value = 4.2
    grp.add_param(slider)
    src.add_param(grp)

    stage = Usd.Stage.CreateInMemory()
    _author_prim(stage, src)
    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/n")

    attr = prim.GetAttribute(f"{_INPUTS_PREFIX}grp:s")
    assert attr.IsValid()
    assert attr.Get() == pytest.approx(4.2)

    dst = chunk.Chunk("n")
    dst_grp = chunk.Group("grp")
    dst_slider = chunk.Slider("s")
    dst_grp.add_param(dst_slider)
    dst.add_param(dst_grp)

    _load_wire_inputs(prim, dst)
    assert dst_slider.value == pytest.approx(4.2)


# ---------------------------------------------------------------------------
# listgroup roundtrip
# ---------------------------------------------------------------------------

def test_listgroup_roundtrip() -> None:
    src = _chunk_with_module("n")
    lg = chunk.ListGroup("items")
    lg.add_param(chunk.Slider("val"))
    src.add_param(lg)
    lg.size = 3
    for i, v in enumerate([1.0, 2.0, 3.0]):
        lg.get_indexed_param("val", i).value = v

    stage = Usd.Stage.CreateInMemory()
    _author_prim(stage, src)
    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/n")

    dst = chunk.Chunk("n")
    dst_lg = chunk.ListGroup("items")
    dst_lg.add_param(chunk.Slider("val"))
    dst.add_param(dst_lg)
    dst_lg.size = 1

    _load_wire_inputs(prim, dst)
    assert dst_lg.size == 3
    assert dst_lg.get_indexed_param("val", 0).value == pytest.approx(1.0)
    assert dst_lg.get_indexed_param("val", 1).value == pytest.approx(2.0)
    assert dst_lg.get_indexed_param("val", 2).value == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# authoring guards: defaults, None, all-None vector
# ---------------------------------------------------------------------------

def test_default_value_not_authored() -> None:
    src = _chunk_with_module("n")
    slider = chunk.Slider("p", default_value=2.5)
    slider.value = 2.5
    src.add_param(slider)
    stage = Usd.Stage.CreateInMemory()
    _author_prim(stage, src)
    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/n")
    assert not prim.GetAttribute(f"{_INPUTS_PREFIX}p").IsValid()


def test_none_param_not_authored() -> None:
    src = _chunk_with_module("n")
    src.add_param(chunk.Slider("p"))  # value stays None
    stage = Usd.Stage.CreateInMemory()
    _author_prim(stage, src)
    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/n")
    assert not prim.GetAttribute(f"{_INPUTS_PREFIX}p").IsValid()


def test_all_none_vector_not_authored() -> None:
    src = _chunk_with_module("n")
    src.add_param(chunk.Vector("v", param=chunk.Slider(), length=3))  # no values set
    stage = Usd.Stage.CreateInMemory()
    _author_prim(stage, src)
    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/n")
    assert not prim.GetAttribute(f"{_INPUTS_PREFIX}v").IsValid()


# ---------------------------------------------------------------------------
# type coverage: Menu
# ---------------------------------------------------------------------------

def test_menu_roundtrip() -> None:
    def make_menu():
        return chunk.Menu("m", menu_items=["optA", "optB", "optC"])

    src_menu = make_menu()
    src_menu.value = "optB"
    src = _chunk_with_module("n")
    src.add_param(src_menu)

    stage = Usd.Stage.CreateInMemory()
    _author_prim(stage, src)

    dst = chunk.Chunk("n")
    dst_menu = make_menu()
    dst.add_param(dst_menu)

    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/n")
    _load_wire_inputs(prim, dst)
    assert dst_menu.value == "optB"


# ---------------------------------------------------------------------------
# type coverage: ListGroup with multiple template params
# ---------------------------------------------------------------------------

def test_listgroup_multiple_template_params_roundtrip() -> None:
    src = _chunk_with_module("n")
    lg = chunk.ListGroup("pts")
    lg.add_param(chunk.Slider("x"))
    lg.add_param(chunk.Slider("y"))
    src.add_param(lg)
    lg.size = 2
    lg.get_indexed_param("x", 0).value = 1.0
    lg.get_indexed_param("x", 1).value = 2.0
    lg.get_indexed_param("y", 0).value = 10.0
    lg.get_indexed_param("y", 1).value = 20.0

    stage = Usd.Stage.CreateInMemory()
    _author_prim(stage, src)
    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/n")

    dst = chunk.Chunk("n")
    dst_lg = chunk.ListGroup("pts")
    dst_lg.add_param(chunk.Slider("x"))
    dst_lg.add_param(chunk.Slider("y"))
    dst.add_param(dst_lg)
    dst_lg.size = 1

    _load_wire_inputs(prim, dst)
    assert dst_lg.size == 2
    assert dst_lg.get_indexed_param("x", 0).value == pytest.approx(1.0)
    assert dst_lg.get_indexed_param("x", 1).value == pytest.approx(2.0)
    assert dst_lg.get_indexed_param("y", 0).value == pytest.approx(10.0)
    assert dst_lg.get_indexed_param("y", 1).value == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# type coverage: empty ListGroup writes nothing
# ---------------------------------------------------------------------------

def test_listgroup_empty_not_authored() -> None:
    src = _chunk_with_module("n")
    lg = chunk.ListGroup("items")
    lg.add_param(chunk.Slider("val"))
    src.add_param(lg)  # size stays 0

    stage = Usd.Stage.CreateInMemory()
    _author_prim(stage, src)
    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/n")
    assert not prim.GetAttribute(f"{_INPUTS_PREFIX}items:val").IsValid()


# ---------------------------------------------------------------------------
# ChunkStageSync live sync
# ---------------------------------------------------------------------------

def test_chunk_stage_sync_live_update() -> None:
    stage = Usd.Stage.CreateInMemory()

    class _SyncScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _SyncScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    ChunkStageSync().initialize(stage, scene)

    c = chunk.Chunk("live")
    c._config = ChunkConfig(module="prim")
    slider = chunk.Slider("p", default_value=0.0)
    c.add_param(slider)
    scene.add_chunk(c)

    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/live")
    assert prim.IsValid()
    assert not prim.GetAttribute(f"{_INPUTS_PREFIX}p").IsValid()  # default not yet authored

    slider.value = 3.7  # _on_attr_changed → _sync_param_input

    attr = prim.GetAttribute(f"{_INPUTS_PREFIX}p")
    assert attr.IsValid()
    assert attr.Get() == pytest.approx(3.7)


# ---------------------------------------------------------------------------
# High-level API: export_chunks + load_chunks full roundtrip
# ---------------------------------------------------------------------------

def test_export_load_roundtrip(tmp_path) -> None:
    stage_src = Usd.Stage.CreateInMemory()

    class _Src(_DummyScene):
        def get_stage(self):
            return stage_src

    scene_src = _Src()
    chunk_registry.ChunkRegistry().initialize(scene_src, "pytest")
    # export_chunks now copies each chunk's already-composed prim spec straight from
    # the live stage (rather than re-deriving it from Python state via _author_prim),
    # so the stage must actually be wired up to author those prims in the first place.
    _chunk_usd_init(stage_src, scene_src)

    c = chunk.Chunk("testChunk")
    c._config = ChunkConfig(module="prim")
    prim_type = chunk.Menu("prim_type", menu_items=["Xform", "Scope", "Class"])
    prim_type.value = "Scope"
    c.add_param(prim_type)
    scene_src.add_chunk(c)

    out = tmp_path / "chunks.usda"
    export_chunks(scene_src, out)
    assert out.exists()

    # ChunkLayerManager is a singleton — reset it before switching to the destination
    # stage, or its state from stage_src (graph_layers, etc.) leaks in and load_chunks'
    # execute() cascade tries to use layers that aren't part of stage_dst's stack.
    chunk_registry.ChunkLayerManager().reset()

    stage_dst = Usd.Stage.Open(str(out))

    class _Dst(_DummyScene):
        def get_stage(self):
            return stage_dst

    scene_dst = _Dst()
    chunk_registry.ChunkRegistry().initialize(scene_dst, "pytest")
    load_chunks(stage_dst, scene_dst)

    loaded = scene_dst.get_chunks()
    assert len(loaded) == 1
    assert loaded[0].get_param("prim_type").value == "Scope"


def test_reload_for_stage_resets_on_same_path_fresh_stage(tmp_path) -> None:
    """Re-exporting to the same path and reloading it (a common iteration workflow)
    gives a genuinely new Usd.Stage whose root layer nonetheless shares the same
    identifier (Sdf's layer registry caches by identifier/path) as the previously
    loaded one. reload_for_stage must still reset in that case — comparing stage
    *object identity*, not the identifier string — or the previous file's chunk
    objects stay registered, orphaned, alongside the freshly loaded content.
    """
    out = tmp_path / "roundtrip.usda"
    Sdf.Layer.CreateNew(str(out)).Save()

    stage1 = Usd.Stage.Open(str(out))

    class _Scene(_DummyScene):
        def __init__(self):
            super().__init__()
            self._stage = stage1

        def get_stage(self):
            return self._stage

    scene = _Scene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    sync = ChunkStageSync()
    _chunk_usd_init(stage1, scene)

    a = chunk.Chunk("first")
    a._config = ChunkConfig(module="prim")
    scene.add_chunk(a)
    assert any(c.name == "first" for c in scene.get_chunks())

    stage2 = Usd.Stage.Open(str(out))
    assert stage2 is not stage1
    assert stage2.GetRootLayer().identifier == stage1.GetRootLayer().identifier

    scene._stage = stage2
    sync.reload_for_stage(stage2)

    assert sync._loaded_stage is stage2
    assert all(c.name != "first" for c in scene.get_chunks())


def test_load_chunks_preserves_named_tab_context_on_reload(tmp_path) -> None:
    """A named top-level Tab context (e.g. "Tab1"), saved via export_chunks and
    reloaded into a fresh session/stage, must come back as that same context — not
    get orphaned into a brand-new tab named after whatever file happens to be open.

    load_chunks only ever recognized two cases: a chunk with a chunkType (a real
    module-backed chunk) or an untyped root-level orphan (wrapped in a tab named
    after the current file, stem-derived from GetRootLayer()). A Tab/ChunkGroup
    context prim (Chunk-typed, no chunkType) fell into neither: get_chunk_module()
    was never even consulted for it, so its children's source layer resolved to
    the file's own root layer (_find_prim_layer excludes the root layer entirely),
    landing them in the untyped-orphan bucket and wrapping them in a *new*,
    incorrectly-named tab instead of recognizing "Tab1" as their real home.
    """
    stage = Usd.Stage.CreateInMemory()

    class _Src(_DummyScene):
        def get_stage(self):
            return stage

    scene = _Src()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    _chunk_usd_init(stage, scene)

    tab = scene.add_context("Tab1")
    a = _chunk_with_module("a")
    tab.add_param(a)
    b = _chunk_with_module("b")
    tab.add_param(b)

    out = tmp_path / "chunks.usda"
    export_chunks(scene, out)

    out_stage = Usd.Stage.Open(str(out))

    class _Dst(_DummyScene):
        def get_stage(self):
            return out_stage

    scene2 = _Dst()
    chunk_registry.ChunkRegistry().initialize(scene2, "pytest")
    load_chunks(out_stage, scene2)

    loaded = scene2.get_chunks()
    assert len(loaded) == 2
    for c in loaded:
        ctx = c.get_context()
        assert ctx is not None and ctx.name == "Tab1", (
            f"{c.name} should be homed under Tab1, got context {ctx.name if ctx else None!r}"
        )


def test_export_chunks_preserves_live_exec_order_not_scene_order(tmp_path) -> None:
    """export_chunks must follow each chunk's LIVE inputs:chunk connection (the real
    wiring, as authored by the node-graph UI's drag-to-connect) rather than falling
    back to scene.get_chunks() iteration order — which is what happened before this
    fix, since chunk.get_dependencies() (the Python DAG) stays empty for a graph
    wired live: drag-to-connect authors the USD-level connection directly and never
    calls add_dependency(), that reconstruction only happens on load_chunks().
    """
    stage = Usd.Stage.CreateInMemory()

    class _Src(_DummyScene):
        def get_stage(self):
            return stage

    scene = _Src()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    _chunk_usd_init(stage, scene)

    # Registered in scrambled order (b, c, a) relative to the intended exec chain
    # (a -> b -> c) — the old scene.get_chunks()-order fallback would export b -> c -> a.
    b = _chunk_with_module("b")
    scene.add_chunk(b)
    c = _chunk_with_module("c")
    scene.add_chunk(c)
    a = _chunk_with_module("a")
    scene.add_chunk(a)

    # Wire a -> b -> c directly on the USD prims, exactly like the node-graph UI's
    # drag-to-connect — never calling add_dependency().
    b_prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/b")
    c_prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/c")
    b_prim.CreateAttribute(_EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False).SetConnections(
        [Sdf.Path(f"{_CHUNKS_ROOT}/a.{_EXEC_OUTPUT}")]
    )
    c_prim.CreateAttribute(_EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False).SetConnections(
        [Sdf.Path(f"{_CHUNKS_ROOT}/b.{_EXEC_OUTPUT}")]
    )
    assert a.get_dependencies() == [] and b.get_dependencies() == [] and c.get_dependencies() == []

    out = tmp_path / "chunks.usda"
    export_chunks(scene, out)

    out_stage = Usd.Stage.Open(str(out))
    b_conns = out_stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/b").GetAttribute(_EXEC_INPUT).GetConnections()
    c_conns = out_stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/c").GetAttribute(_EXEC_INPUT).GetConnections()
    a_attr = out_stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/a").GetAttribute(_EXEC_INPUT)

    assert any("a" in str(p) for p in b_conns), f"expected b -> a, got {b_conns}"
    assert any("b" in str(p) for p in c_conns), f"expected c -> b, got {c_conns}"
    assert not a_attr.IsValid() or not a_attr.GetConnections()


# ---------------------------------------------------------------------------
# High-level API: second mutation propagates via update_usd_prop
# ---------------------------------------------------------------------------

def test_second_mutation_propagates() -> None:
    stage = Usd.Stage.CreateInMemory()

    class _SyncScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _SyncScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    ChunkStageSync().initialize(stage, scene)

    c = _chunk_with_module("n")
    slider = chunk.Slider("p", default_value=0.0)
    c.add_param(slider)
    scene.add_chunk(c)

    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/n")

    slider.value = 3.7  # first write — _sync_param_input creates attr + wires _connect_param_attr
    assert prim.GetAttribute(f"{_INPUTS_PREFIX}p").Get() == pytest.approx(3.7)

    slider.value = 9.1  # second write — update_usd_prop writes directly to existing attr
    assert prim.GetAttribute(f"{_INPUTS_PREFIX}p").Get() == pytest.approx(9.1)


# ---------------------------------------------------------------------------
# ChunkStageSync lifecycle: chunk rename
# ---------------------------------------------------------------------------

def test_chunk_stage_sync_rename_chunk() -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _DummyScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    ChunkStageSync().initialize(stage, scene)

    c = _chunk_with_module("foo")
    scene.add_chunk(c)
    assert stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/foo").IsValid()

    c.name = "bar"

    assert not stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/foo").IsValid()
    assert stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/bar").IsValid()


def test_chunk_stage_sync_rename_no_duplicate() -> None:
    """Rename must not spawn a duplicate chunk. Applying the namespace edit fires
    StageContentsChanged synchronously; if that re-enters load_chunks before
    _loaded_prim_paths is updated, the renamed prim looks 'new' and a stale copy is
    created (the renamed node then vanishes in the graph rebuild). Uses a stage-backed
    scene so _on_contents_changed actually runs load_chunks."""

    stage = Usd.Stage.CreateInMemory()

    class _StageScene(SceneSender):
        def get_stage(self):
            return stage

        def get_selected_prims(self):
            return []

    scene = _StageScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    ChunkStageSync().initialize(stage, scene)

    c = _chunk_with_module("foo")
    scene.add_chunk(c)
    c.name = "bar"

    assert sum(1 for _ in scene.get_chunks()) == 1  # no duplicate chunk
    assert stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/bar").IsValid()
    assert not stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/foo").IsValid()


def test_chunk_stage_sync_rename_anonymous_edit_target() -> None:
    """Rename must work with no Edit Target chunk in play — bookkeeping then resolves to the
    session layer (nearest_upstream_edit_layer's default). If the rename bails when the prim
    can't be pre-resolved instead of falling back to the current edit target, the chunk is
    renamed but its prim isn't — the paths diverge and the node vanishes from the graph."""

    stage = Usd.Stage.CreateInMemory()

    class _StageScene(SceneSender):
        def get_stage(self):
            return stage

        def get_selected_prims(self):
            return []

    scene = _StageScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    sync = ChunkStageSync()
    sync.initialize(stage, scene)

    c = _chunk_with_module("foo")
    scene.add_chunk(c)

    c.name = "bar"

    assert stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/bar").IsValid()
    assert not stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/foo").IsValid()
    assert sum(1 for _ in scene.get_chunks()) == 1


def test_chunk_stage_sync_rename_preserves_connections() -> None:
    """Renaming a chunk must repoint connections that target it (e.g. a view's
    inputs:chunk -> foo.outputs:chunk), otherwise the wire dangles and drops."""

    stage = Usd.Stage.CreateInMemory()

    class _StageScene(SceneSender):
        def get_stage(self):
            return stage

        def get_selected_prims(self):
            return []

    scene = _StageScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    ChunkStageSync().initialize(stage, scene)

    c = _chunk_with_module("foo")
    scene.add_chunk(c)

    # A downstream Chunk prim wired to foo's exec output.
    view = stage.DefinePrim(f"{_CHUNKS_ROOT}/View1", _CHUNK_PRIM_TYPENAME)
    vin = view.CreateAttribute(_EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False)
    vin.SetConnections([Sdf.Path(f"{_CHUNKS_ROOT}/foo.{_EXEC_OUTPUT}")])

    c.name = "bar"

    conns = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/View1").GetAttribute(_EXEC_INPUT).GetConnections()
    assert list(conns) == [Sdf.Path(f"{_CHUNKS_ROOT}/bar.{_EXEC_OUTPUT}")]


# ---------------------------------------------------------------------------
# ChunkStageSync lifecycle: param rename
# ---------------------------------------------------------------------------

def test_chunk_stage_sync_rename_param() -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _DummyScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    ChunkStageSync().initialize(stage, scene)

    c = _chunk_with_module("n")
    slider = chunk.Slider("old", default_value=0.0)
    c.add_param(slider)
    scene.add_chunk(c)

    slider.value = 5.0  # causes _sync_param_input to create inputs:old attr
    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/n")
    assert prim.GetAttribute(f"{_INPUTS_PREFIX}old").IsValid()

    slider.name = "new"  # _handle_param_rename renames the attr

    assert not prim.GetAttribute(f"{_INPUTS_PREFIX}old").IsValid()
    assert prim.GetAttribute(f"{_INPUTS_PREFIX}new").IsValid()


# ---------------------------------------------------------------------------
# ChunkStageSync lifecycle: chunk delete
# ---------------------------------------------------------------------------

def test_chunk_stage_sync_delete_chunk() -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _DummyScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    ChunkStageSync().initialize(stage, scene)

    c = _chunk_with_module("del")
    scene.add_chunk(c)
    assert stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/del").IsValid()

    c.request_delete()

    assert not stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/del").IsValid()


def test_delete_chunk_removes_anonymous_layer() -> None:
    """Releasing a deleted chunk removes its anonymous layer from the stage sublayer stack.

    ChunkLayerManager registers a weakref.finalize on each chunk so that GC triggers
    removal of the sublayer.  This test verifies that after request_delete() + del the
    anonymous layer is no longer reachable via stage.GetLayerStack().
    """
    import gc

    stage = Usd.Stage.CreateInMemory()
    scene = _DummyScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    _chunk_usd_init(stage, scene)  # initialises both ChunkLayerManager and ChunkStageSync

    c = _chunk_with_module("anon_del")
    scene.add_chunk(c)

    lm = chunk_registry.ChunkLayerManager()
    layer = lm.get_layer(c.get_hash())
    assert layer is not None, "chunk should have an anonymous layer after add"
    layer_id = layer.identifier
    assert layer_id in {lay.identifier for lay in stage.GetLayerStack(includeSessionLayers=True)}

    c.request_delete()
    del c  # release test reference so weakref.finalize can fire
    gc.collect()

    assert layer_id not in {lay.identifier for lay in stage.GetLayerStack(includeSessionLayers=True)}


def test_delete_loaded_chunk_removes_prim() -> None:
    """Deleting a chunk that was loaded from a USDA file removes its prim from the stage.

    Regression guard: _on_chunk_deleted must find and remove the prim from the session
    layer even when the chunk was populated via load_chunks rather than add_chunk.
    The prim path must match the loaded chunk name (load_chunks uses the module's
    default name, e.g. "prim" module → chunk "Prim" → prim at /chunks/Prim).
    """
    stage = Usd.Stage.CreateInMemory()
    scene = _DummyScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")

    prim_path = Sdf.Path(f"{_CHUNKS_ROOT}/Prim")
    prim = stage.DefinePrim(prim_path, _CHUNK_PRIM_TYPENAME)
    prim.CreateAttribute(_CHUNK_TYPE_ATTR, Sdf.ValueTypeNames.Token).Set("prim")

    _chunk_usd_init(stage, scene)

    loaded = next((c for c in scene.get_chunks() if c.name == "Prim"), None)
    assert loaded is not None, "chunk not found after load_chunks"
    assert stage.GetPrimAtPath(prim_path).IsValid()

    loaded.request_delete()

    assert not stage.GetPrimAtPath(prim_path).IsValid()


def test_delete_last_chunk_removes_chunks_root() -> None:
    """Deleting the last chunk under /chunks removes the /chunks root prim too."""
    stage = Usd.Stage.CreateInMemory()
    scene = _DummyScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    _chunk_usd_init(stage, scene)

    c = _chunk_with_module("Solo")
    scene.add_chunk(c)
    assert stage.GetPrimAtPath(_CHUNKS_ROOT).IsValid()

    c.request_delete()

    assert not stage.GetPrimAtPath(_CHUNKS_ROOT).IsValid()


def test_delete_one_of_two_chunks_keeps_chunks_root() -> None:
    """Deleting one chunk when another still exists must not remove /chunks."""
    stage = Usd.Stage.CreateInMemory()
    scene = _DummyScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    _chunk_usd_init(stage, scene)

    c1 = _chunk_with_module("A")
    c2 = _chunk_with_module("B")
    scene.add_chunk(c1)
    scene.add_chunk(c2)

    c1.request_delete()

    assert stage.GetPrimAtPath(_CHUNKS_ROOT).IsValid()
    assert stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/B").IsValid()


def test_delete_then_readd_same_type_recreates_prim() -> None:
    """Re-adding a chunk of the same type after deletion must recreate the chunk prim.

    Regression: _on_param_added adds every new prim path to _loaded_prim_paths so that
    duplicate add_chunk calls from load_chunks are ignored.  _on_chunk_deleted was not
    discarding the path, so the second add_chunk hit the early-return and skipped
    _define_prim entirely.
    """
    stage = Usd.Stage.CreateInMemory()
    scene = _DummyScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    _chunk_usd_init(stage, scene)

    c1 = _chunk_with_module("Prim")
    scene.add_chunk(c1)
    assert stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/Prim").IsValid()

    c1.request_delete()
    assert not stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/Prim").IsValid()

    c2 = _chunk_with_module("Prim")
    scene.add_chunk(c2)
    assert stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/Prim").IsValid()


def test_delete_context_with_children_does_not_reenter_load_chunks(monkeypatch) -> None:
    """Deleting a context with several children must not re-trigger load_chunks per child.

    Regression: _on_chunk_deleted's nameChildren removal and ChunkLayerManager._chunk_deleted's
    subLayerPaths removal both authored raw Sdf edits without the _usd_write_active guard used
    everywhere else. Each edit fired StageContentsChanged, re-entering
    ChunkStageSync._on_contents_changed -> load_chunks mid-cascade, while the context still had
    unprocessed children. load_chunks unconditionally calls scene.add_context() for any root-layer
    chunk it (spuriously) treats as unregistered, which doesn't dedupe against an existing tab of
    the same name -- minting duplicate tabs ("MyTab1", "MyTab2", ...) with lost connections, as
    seen live in usdview.
    """
    stage = Usd.Stage.CreateInMemory()

    class _SyncScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _SyncScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    _chunk_usd_init(stage, scene)

    ctx = scene.add_context("MyTab")
    children = [_chunk_with_module(f"c{i}") for i in range(5)]
    for c in children:
        scene.add_chunk(c)

    import usd_chunks.plugins.chunk_usd as chunk_usd_mod

    calls: list = []
    orig_load_chunks = chunk_usd_mod.load_chunks

    def _traced(stage_, scene_, loaded_prim_paths=None):
        calls.append(1)
        return orig_load_chunks(stage_, scene_, loaded_prim_paths)

    monkeypatch.setattr(chunk_usd_mod, "load_chunks", _traced)

    ctx.request_delete()

    assert not calls, f"load_chunks re-entered {len(calls)} time(s) during the delete cascade"

    remaining_names = {c.name for c in scene.chunks.get_my_params()}
    assert not any(n.startswith("MyTab") and n != "MyTab" for n in remaining_names), (
        f"deletion must not spawn duplicate contexts, got: {remaining_names}"
    )


# ---------------------------------------------------------------------------
# ChunkStageSync: external USD active change syncs to chunk disabled state
# ---------------------------------------------------------------------------

def test_external_prim_deactivate_disables_chunk() -> None:
    """Setting prim.SetActive(False) externally (e.g. from Python or usdview) must
    propagate back to the chunk's DISABLED state via _on_objects_changed.

    The scene must return the same stage so the root-layer identity check passes.
    """
    stage = Usd.Stage.CreateInMemory()

    class _SyncScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _SyncScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    _chunk_usd_init(stage, scene)

    c = _chunk_with_module("ext")
    scene.add_chunk(c)
    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/ext")
    assert prim.IsActive()
    assert not c.has_state(chunk.ChunkFlags.State.DISABLED)

    prim.SetActive(False)

    assert c.has_state(chunk.ChunkFlags.State.DISABLED)

    prim.SetActive(True)

    assert not c.has_state(chunk.ChunkFlags.State.DISABLED)


# ---------------------------------------------------------------------------
# ChunkStageSync lifecycle: chunk disable / enable toggles prim active state
# ---------------------------------------------------------------------------

def test_chunk_stage_sync_disable_enable() -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _DummyScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    ChunkStageSync().initialize(stage, scene)

    c = _chunk_with_module("dis")
    scene.add_chunk(c)
    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/dis")
    assert prim.IsActive()

    c.set_state(chunk.ChunkFlags.State.DISABLED)
    assert not stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/dis").IsActive()

    c.unset_state(chunk.ChunkFlags.State.DISABLED)
    assert stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/dis").IsActive()


def test_blocked_chunk_does_not_execute_on_creation() -> None:
    """A chunk blocked before it's added (view-driven: created out of the active view's tree)
    must not run its module on creation — not via param-resolve and not via execute-on-creation.
    Unblocking and cooking it (as recook does when it joins the view's tree) runs it."""
    stage = Usd.Stage.CreateInMemory()
    scene = _DummyScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    ChunkStageSync().initialize(stage, scene)

    calls = []

    class _Mod:
        def execute(self, prop):
            calls.append(1)

    c = _chunk_with_module("blk")
    c._module = _Mod()
    c.set_blocked(True)
    scene.add_chunk(c)
    assert calls == []  # blocked → no execute on creation

    c.execute()
    assert calls == []  # blocked → even a direct execute() is suppressed (module guard)

    c.set_blocked(False)
    c.execute()
    assert len(calls) >= 1  # unblocked + cooked → runs


def test_recook_no_active_view_blocks_all() -> None:
    """With no active view (view_chunk=None), recook must block every chunk so nothing cooks —
    not run the whole graph (which used to unblock + execute all nodes, so toggling the active
    view off ran everything, including unconnected nodes)."""
    from usd_chunks.plugins.chunk_usd import recook_for_view

    stage = Usd.Stage.CreateInMemory()
    scene = _DummyScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    _chunk_usd_init(stage, scene)

    a = _chunk_with_module("a")
    b = _chunk_with_module("b")
    scene.add_chunk(a)
    scene.add_chunk(b)

    recook_for_view(stage, scene, None, None)

    assert a.is_blocked() and b.is_blocked()


def test_state_change_keeps_view_blocked_layer_muted() -> None:
    """A view-blocked chunk's layer is muted (hidden). A state change (e.g. selecting the node)
    must NOT un-mute it — _flags_changed used to un-mute any non-disabled muted layer on every
    state change, so selecting an out-of-view node showed its output."""
    stage = Usd.Stage.CreateInMemory()

    class _StageScene(SceneSender):
        def get_stage(self):
            return stage

        def get_selected_prims(self):
            return []

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    _chunk_usd_init(stage, scene)
    lm = chunk_registry.ChunkLayerManager()

    primmod = reg.get_chunk_module("prim")
    chunks = primmod.create() if primmod.loaded else primmod.load()
    for ch in chunks:
        scene.add_chunk(ch)  # execute-on-creation authors output + creates the chunk layer
    c = chunks[0]
    layer = lm.get_layer(c)
    assert layer is not None

    # Simulate recook blocking + muting this out-of-active-view chunk.
    c.set_blocked(True)
    stage.MuteLayer(layer.identifier)
    assert stage.IsLayerMuted(layer.identifier)

    c.set_state(chunk.ChunkFlags.State.SELECTED)
    assert stage.IsLayerMuted(layer.identifier)  # selection must not un-mute a block-muted layer


# ---------------------------------------------------------------------------
# ChunkStageSync: param added after chunk already registered
# ---------------------------------------------------------------------------

def test_param_added_after_registration() -> None:
    """Adding a param to an already-registered chunk triggers _sync_chunk_inputs."""
    stage = Usd.Stage.CreateInMemory()
    scene = _DummyScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    ChunkStageSync().initialize(stage, scene)

    c = _chunk_with_module("n")
    scene.add_chunk(c)  # registers chunk prim; no params yet

    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/n")
    assert prim.IsValid()
    assert not prim.GetAttribute(f"{_INPUTS_PREFIX}val").IsValid()

    slider = chunk.Slider("val", default_value=0.0)
    slider.value = 7.5  # non-default so _sync_chunk_inputs authors the attr
    c.add_param(slider)  # PARAM_ADDED → _on_param_added → _sync_chunk_inputs

    attr = prim.GetAttribute(f"{_INPUTS_PREFIX}val")
    assert attr.IsValid()
    assert attr.Get() == pytest.approx(7.5)


# ---------------------------------------------------------------------------
# _load_wire_inputs: live channel wired so subsequent mutations propagate
# ---------------------------------------------------------------------------

def test_load_wire_inputs_live_channel() -> None:
    """After _load_wire_inputs wires a param, subsequent mutations update the USD attr."""
    stage = Usd.Stage.CreateInMemory()

    class _SyncScene(_DummyScene):
        def get_stage(self):
            return stage

    chunk_registry.ChunkRegistry().initialize(_SyncScene(), "pytest")

    c = _chunk_with_module("n")
    slider = chunk.Slider("p", default_value=0.0)
    slider.value = 3.0
    c.add_param(slider)
    _author_prim(stage, c)

    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/n")
    _load_wire_inputs(prim, c)

    slider.value = 7.5  # data_changed uses _param_attr_path set by _connect_param_attr

    assert prim.GetAttribute(f"{_INPUTS_PREFIX}p").Get() == pytest.approx(7.5)


# ---------------------------------------------------------------------------
# ChunkStageSync lifecycle: ListGroup proxy mutation updates array attr
# ---------------------------------------------------------------------------

def test_chunk_stage_sync_listgroup_live_sync() -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _DummyScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    ChunkStageSync().initialize(stage, scene)

    c = _chunk_with_module("n")
    lg = chunk.ListGroup("items")
    lg.add_param(chunk.Slider("val"))
    c.add_param(lg)
    scene.add_chunk(c)
    lg.size = 2

    lg.get_indexed_param("val", 0).value = 1.0
    lg.get_indexed_param("val", 1).value = 2.0

    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/n")
    attr = prim.GetAttribute(f"{_INPUTS_PREFIX}items:val")
    assert attr.IsValid()
    assert list(attr.Get()) == pytest.approx([1.0, 2.0])


# ---------------------------------------------------------------------------
# Expression authoring: value + USD connections
# ---------------------------------------------------------------------------

def test_expression_authoring() -> None:
    """Expression string is stored as attr value; USD connections point to the referenced inputs:."""
    stage = Usd.Stage.CreateInMemory()

    src = _chunk_with_module("src")
    expr_param = chunk.LineEdit("p")
    expr_param.value = "@dst.val@"
    src.add_param(expr_param)
    _author_prim(stage, src)

    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/src")
    attr = prim.GetAttribute(f"{_INPUTS_PREFIX}p")
    assert attr.IsValid()
    assert attr.Get() == "@dst.val@"
    conns = attr.GetConnections()
    assert len(conns) == 1
    assert str(conns[0]) == f"{_CHUNKS_ROOT}/dst.inputs:val"


# ---------------------------------------------------------------------------
# Expression restore: _load_wire_inputs wires param and preserves USD connections
# ---------------------------------------------------------------------------

def test_expression_load_wires_param() -> None:
    """_load_wire_inputs calls _connect_param_attr on expression attrs: param is wired and connections intact."""
    stage = Usd.Stage.CreateInMemory()

    src = _chunk_with_module("src")
    expr_param = chunk.LineEdit("p")
    expr_param.value = "@dst.val@"
    src.add_param(expr_param)
    _author_prim(stage, src)

    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/src")
    _load_wire_inputs(prim, src)

    assert expr_param._param_attr_path == f"{_CHUNKS_ROOT}/src.{_INPUTS_PREFIX}p"
    conns = prim.GetAttribute(f"{_INPUTS_PREFIX}p").GetConnections()
    assert len(conns) == 1
    assert str(conns[0]) == f"{_CHUNKS_ROOT}/dst.inputs:val"


def test_float_expression_load_reconstructs_raw_value() -> None:
    """Float attrs store only the connection (no string value); reload must
    reconstruct the @node.param@ expression from that connection so it survives
    a save/reopen, instead of silently falling back to a plain number."""
    stage = Usd.Stage.CreateInMemory()

    class _SyncScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _SyncScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    ChunkStageSync().initialize(stage, scene)

    dst = _chunk_with_module("dst")
    dst.add_param(chunk.Slider("val", default_value=1.0))
    scene.add_chunk(dst)
    dst.get_param("val").value = 5.0  # non-default so its storage attr exists

    src = _chunk_with_module("src")
    src.add_param(chunk.Slider("p", default_value=1.0))
    scene.add_chunk(src)
    src.get_param("p").value = "@dst.val@"  # live-authored, same as a UI-dragged connection

    attr = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/src").GetAttribute(f"{_INPUTS_PREFIX}p")
    assert attr.GetTypeName() == Sdf.ValueTypeNames.Float
    assert attr.Get() is None, "Float expression attrs must not store a string value"
    assert len(attr.GetConnections()) == 1

    reloaded = chunk.Chunk("src")
    reloaded_param = chunk.Slider("p", default_value=1.0)
    reloaded.add_param(reloaded_param)

    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/src")
    _load_wire_inputs(prim, reloaded)

    assert reloaded_param.raw_value == "@dst.val@"


def test_expression_value_updates_driven_attr() -> None:
    """A param wired to a real prim attr via connect_usd_attr (e.g. camera focalLength
    driven by a Slider) must push its *resolved* value to that driven attr even when
    raw_value is an expression, not just when it's a plain number."""
    stage = Usd.Stage.CreateInMemory()

    class _SyncScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _SyncScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    ChunkStageSync().initialize(stage, scene)

    src = _chunk_with_module("src")
    src.add_param(chunk.Slider("val", default_value=0.0))
    scene.add_chunk(src)
    src.get_param("val").value = 12.5

    dst = _chunk_with_module("dst")
    slider = chunk.Slider("p", default_value=0.0)
    dst.add_param(slider)
    scene.add_chunk(dst)

    driven_prim = stage.DefinePrim("/geo/sphere", "Sphere")
    driven_attr = driven_prim.CreateAttribute("radius", Sdf.ValueTypeNames.Float, custom=False)
    slider.connect_usd_attr(driven_attr)

    slider.value = "@src.val@"

    assert driven_attr.Get() == pytest.approx(12.5)


def test_expression_listener_reconnects_after_reload() -> None:
    """Reproduces a real user report: load a saved graph with MathOps.result driving
    MathOps1.a via an expression, then change MathOps.a. The resolved value updates
    fine on a fresh read, but the live-update listener (_expression_connections),
    rebuilt during _load_wire_inputs's _connect_param_attr, failed to attach because
    the *referenced* chunk (MathOps) wasn't registered with the scene yet at that
    point in load_chunks's loop — so no notice ever reached MathOps1's widget.
    load_chunks must retry the listener setup once every chunk in the batch is
    registered, and the retry must actually restore live propagation."""
    import importlib
    mo = importlib.import_module("scripts.math_ops")

    stage = Usd.Stage.CreateInMemory()
    stage.GetRootLayer().ImportFromString(
        """#usda 1.0

def Chunk "chunks"
{
    def Chunk "MathOps" (
        prepend apiSchemas = ["NodeGraphNodeAPI"]
    )
    {
        uniform token info:chunkType = "math_ops"
        float inputs:a = 54.077
        uniform token outputs:chunk
        uniform float2 ui:nodegraph:node:pos = (0, 0)
    }

    def Chunk "MathOps1" (
        prepend apiSchemas = ["NodeGraphNodeAPI"]
    )
    {
        uniform token info:chunkType = "math_ops"
        float inputs:a.connect = </chunks/MathOps.inputs:result>
        token inputs:chunk.connect = </chunks/MathOps.outputs:chunk>
        uniform token outputs:chunk
        uniform float2 ui:nodegraph:node:pos = (260, 0)
    }
}
"""
    )

    class _SyncScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _SyncScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    ChunkStageSync().initialize(stage, scene)
    load_chunks(stage, scene)

    loaded = {c.name: c for c in scene.get_chunks()}
    m0, m1 = loaded["MathOps"], loaded["MathOps1"]
    a_param = m1.get_param("a")

    assert a_param.raw_value == "@MathOps.result@"
    assert a_param.value == pytest.approx(54.077, abs=1e-3)
    assert a_param._expression_connections, "listener must be attached after load_chunks"

    # A GUI slider bound to a real driven attr (e.g. camera focalLength) must also
    # refresh on reload/reconnect, and keep tracking live edits afterward.
    driven_prim = stage.DefinePrim("/geo/xform", "Xform")
    driven_attr = driven_prim.CreateAttribute("myFloat", Sdf.ValueTypeNames.Float, custom=True)
    a_param.connect_usd_attr(driven_attr)
    assert driven_attr.Get() == pytest.approx(54.077, abs=1e-3)

    m0.get_param("a").value = 10.0
    mo.execute(m0)

    assert a_param.value == pytest.approx(10.0)
    assert driven_attr.Get() == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# Expression authoring: multiple @..@ tokens produce multiple connections
# ---------------------------------------------------------------------------

def test_expression_multi_reference_authoring() -> None:
    """Two @node.param@ tokens in one expression produce two USD connections."""
    stage = Usd.Stage.CreateInMemory()

    src = _chunk_with_module("src")
    expr_param = chunk.LineEdit("p")
    expr_param.value = "@a.x@ + @b.y@"
    src.add_param(expr_param)
    _author_prim(stage, src)

    attr = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/src").GetAttribute(f"{_INPUTS_PREFIX}p")
    assert attr.Get() == "@a.x@ + @b.y@"
    conns = attr.GetConnections()
    assert len(conns) == 2
    assert str(conns[0]) == f"{_CHUNKS_ROOT}/a.inputs:x"
    assert str(conns[1]) == f"{_CHUNKS_ROOT}/b.inputs:y"


# ---------------------------------------------------------------------------
# Expression authoring: numeric param with multiplication expression
# ---------------------------------------------------------------------------

def test_expression_numeric_param_authoring() -> None:
    """Expression on a Slider is stored as a String USD attr (raw_value is the expr string)."""
    stage = Usd.Stage.CreateInMemory()

    src = _chunk_with_module("src")
    slider = chunk.Slider("p", default_value=0.0)
    slider.value = "@a.x@ * @b.y@"  # bypasses float type-check via convert_attr expression path
    src.add_param(slider)
    _author_prim(stage, src)

    attr = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/src").GetAttribute(f"{_INPUTS_PREFIX}p")
    assert attr.IsValid()
    assert attr.Get() == "@a.x@ * @b.y@"
    conns = attr.GetConnections()
    assert len(conns) == 2
    assert str(conns[0]) == f"{_CHUNKS_ROOT}/a.inputs:x"
    assert str(conns[1]) == f"{_CHUNKS_ROOT}/b.inputs:y"


# ---------------------------------------------------------------------------
# Float expression via ChunkStageSync (mult-style workflow)
# ---------------------------------------------------------------------------

def test_float_expression_via_chunk_stage_sync() -> None:
    """Float param expression via ChunkStageSync: Float attr with connections only, resolves Python-side."""
    stage = Usd.Stage.CreateInMemory()
    scene = _DummyScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    ChunkStageSync().initialize(stage, scene)

    # Source chunk with an OUTPUT result param (simulates compute chunk)
    src = _chunk_with_module("Mult1")
    result = chunk.Spinbox("result", default_value=0.0, flags=ChunkFlags.ParamFlags.OUTPUT)
    result.value = 15.0  # non-default so _sync_chunk_inputs creates outputs:result attr
    src.add_param(result)
    scene.add_chunk(src)

    # Downstream chunk with a float input driven by expression
    ref = _chunk_with_module("Mult2")
    a_param = chunk.Slider("a", default_value=1.0)
    a_param.value = "@Mult1.result@"
    ref.add_param(a_param)
    scene.add_chunk(ref)

    mult2_prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/Mult2")
    attr = mult2_prim.GetAttribute(f"{_INPUTS_PREFIX}a")

    assert attr.IsValid(), "inputs:a must exist on Mult2 prim"
    assert attr.GetTypeName() == Sdf.ValueTypeNames.Float, "float expression must use Float attr, not String"
    conns = attr.GetConnections()
    assert len(conns) == 1, "expression must create exactly one connection"
    assert str(conns[0]).endswith("Mult1.outputs:result"), "expression routes to outputs: for OUTPUT params"
    assert attr.Get() is None, "float expression attrs must not store the expression string as a value"
    assert a_param.raw_value == "@Mult1.result@", "raw_value must remain the expression string"
    assert a_param.value == pytest.approx(15.0), "param.value must resolve the expression to Mult1.result"


# ---------------------------------------------------------------------------
# _patch_expressions: chunk rename rewrites expression strings across the stage
# ---------------------------------------------------------------------------

def test_patch_expressions_on_chunk_rename() -> None:
    """Renaming a chunk rewrites @oldname.param@ expressions in all attrs via _patch_expressions."""
    stage = Usd.Stage.CreateInMemory()
    scene = _DummyScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    ChunkStageSync().initialize(stage, scene)

    src = _chunk_with_module("src")
    scene.add_chunk(src)

    ref = _chunk_with_module("ref")
    expr_param = chunk.LineEdit("p")
    expr_param.value = "@src.val@"
    ref.add_param(expr_param)
    scene.add_chunk(ref)  # _wire_inputs → _sync_chunk_inputs creates inputs:p = "@src.val@"

    ref_prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/ref")
    assert ref_prim.GetAttribute(f"{_INPUTS_PREFIX}p").Get() == "@src.val@"

    src.name = "src2"  # _handle_rename → _patch_expressions(stage, "@src.", "@src2.")

    assert ref_prim.GetAttribute(f"{_INPUTS_PREFIX}p").Get() == "@src2.val@"


# ---------------------------------------------------------------------------
# _patch_expressions: param rename rewrites .oldname@ expression substrings
# ---------------------------------------------------------------------------

def test_patch_expressions_on_param_rename() -> None:
    """Renaming a param rewrites .oldname@ in expression strings via _patch_expressions."""
    stage = Usd.Stage.CreateInMemory()
    scene = _DummyScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    ChunkStageSync().initialize(stage, scene)

    owner = _chunk_with_module("owner")
    foo = chunk.Slider("foo", default_value=0.0)
    owner.add_param(foo)
    scene.add_chunk(owner)

    ref = _chunk_with_module("ref")
    expr_param = chunk.LineEdit("p")
    expr_param.value = "@owner.foo@"
    ref.add_param(expr_param)
    scene.add_chunk(ref)  # _sync_chunk_inputs creates inputs:p = "@owner.foo@"

    ref_prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/ref")
    assert ref_prim.GetAttribute(f"{_INPUTS_PREFIX}p").Get() == "@owner.foo@"

    foo.name = "bar"  # _handle_param_rename → _patch_expressions(stage, ".foo@", ".bar@")

    assert ref_prim.GetAttribute(f"{_INPUTS_PREFIX}p").Get() == "@owner.bar@"


# ---------------------------------------------------------------------------
# ChunkStageSync: vector pre-wired to a real USD attr gets inputs: attr created
# ---------------------------------------------------------------------------

def test_vector_sync_input_creates_chunk_prim_attr() -> None:
    """_sync_vector_input copies a real attr's value to inputs: on the chunk prim."""
    stage = Usd.Stage.CreateInMemory()
    scene = _DummyScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    ChunkStageSync().initialize(stage, scene)

    # Real USD prim with an authored Float3 attr (simulates e.g. a camera translate)
    real_prim = stage.DefinePrim("/scene/obj", "Xform")
    real_attr = real_prim.CreateAttribute("translate", Sdf.ValueTypeNames.Float3, custom=True)
    real_attr.Set(Gf.Vec3f(1.0, 2.0, 3.0))

    # Vector param pre-wired to the real attr (as if a prior sync already linked them)
    vec = chunk.Vector("pos", param=chunk.Slider(), length=3, suffixes="xyz")
    vec._param_attr_path = str(real_attr.GetPath())

    c = _chunk_with_module("n")
    c.add_param(vec)
    scene.add_chunk(c)  # → _define_prim → _sync_chunk_inputs → _sync_vector_input

    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/n")
    attr = prim.GetAttribute(f"{_INPUTS_PREFIX}pos")
    assert attr.IsValid()
    assert attr.Get() == Gf.Vec3f(1.0, 2.0, 3.0)


# ---------------------------------------------------------------------------
# _author_prim: three-chunk exec chain wires a→b→c via prev_chunk
# ---------------------------------------------------------------------------

def test_exec_chain_three_chunks() -> None:
    """A three-node chain produces inputs:chunk connections b→a and c→b."""
    stage = Usd.Stage.CreateInMemory()
    a = _chunk_with_module("a")
    b = _chunk_with_module("b")
    c = _chunk_with_module("c")
    _author_prim(stage, a, index=0)
    _author_prim(stage, b, prev_chunk=a, index=1)
    _author_prim(stage, c, prev_chunk=b, index=2)

    conns_b = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/b").GetAttribute(_EXEC_INPUT).GetConnections()
    conns_c = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/c").GetAttribute(_EXEC_INPUT).GetConnections()

    assert any("a" in str(p) for p in conns_b)
    assert any("b" in str(p) for p in conns_c)


# ---------------------------------------------------------------------------
# rename_context_prim_path: copies prim specs to new path and removes old
# ---------------------------------------------------------------------------

def test_rename_context_prim_path() -> None:
    """rename_context_prim_path moves sub-chunk prim to the new root and clears the old one."""
    stage = Usd.Stage.CreateInMemory()
    scene = _DummyScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    ChunkStageSync().initialize(stage, scene)

    # Author a sub-chunk prim at the old location (no /chunks intermediate)
    stage.DefinePrim(Sdf.Path("/old_ctx/c1"), _CHUNK_PRIM_TYPENAME)
    assert stage.GetPrimAtPath("/old_ctx/c1").IsValid()

    # Context chunk that owns c1 as a sub-chunk
    ctx = _chunk_with_module("ctx")
    ctx.prim_path = "/old_ctx"
    c1 = _chunk_with_module("c1")
    ctx.add_param(c1)

    rename_context_prim_path(ctx, "/new_ctx")

    assert ctx.prim_path == "/new_ctx"
    assert stage.GetPrimAtPath("/new_ctx/c1").IsValid()
    assert not stage.GetPrimAtPath("/old_ctx/c1").IsValid()


def test_rename_context_prim_path_updates_chunk_prim_paths() -> None:
    """rename_context_prim_path must update ChunkStageSync._chunk_prim_paths.

    Regression: after rename the tracking dict still pointed to the old path so
    state sync and active-state detection silently stopped working — the prim
    appeared to 'disappear' from the sync's perspective even though it moved.
    """
    stage = Usd.Stage.CreateInMemory()
    scene = _DummyScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    _chunk_usd_init(stage, scene)

    # A named tab context gets its own prim root ("/MyTab") — sub-chunks nest under it.
    ctx = scene.add_context("MyTab")
    c1 = _chunk_with_module("c1")
    scene.add_chunk(c1)  # → ctx.add_param(c1) → prim at /MyTab/c1

    sync = ChunkStageSync()
    assert stage.GetPrimAtPath("/MyTab/c1").IsValid(), "prim must exist before rename"
    assert sync._chunk_prim_paths.get(id(c1)) == "/MyTab/c1"

    rename_context_prim_path(ctx, "/mychunks")

    assert stage.GetPrimAtPath("/mychunks/c1").IsValid(), "prim must be at new path"
    assert not stage.GetPrimAtPath("/MyTab/c1").IsValid(), "old prim must be removed"
    assert sync._chunk_prim_paths.get(id(c1)) == "/mychunks/c1", \
        "_chunk_prim_paths must be updated so sync still finds the chunk"


# ---------------------------------------------------------------------------
# load_chunks: sub-chunk nested under a CONTEXT+module prim joins the group
# ---------------------------------------------------------------------------

_GROUP_MODULE_NAME = "_test_context_group"


def _ensure_test_group_module() -> None:
    """Insert a minimal CONTEXT-producing ChunkModule into the registry (once)."""
    reg = chunk_registry.ChunkRegistry()
    if _GROUP_MODULE_NAME in reg.registered_modules:
        return
    grp_mod = object.__new__(chunk_registry.ChunkModule)
    grp_mod.module = _types.ModuleType(_GROUP_MODULE_NAME)
    grp_mod.module_name = _GROUP_MODULE_NAME
    grp_mod.module_file = Path(_GROUP_MODULE_NAME + ".py")
    grp_mod.loaded = True
    grp_mod._has_main = True
    grp_mod.config = ChunkConfig(module=_GROUP_MODULE_NAME)
    grp_mod._checksum = -1
    grp_mod.func = lambda: ChunkBase("grp", flags=ChunkFlags.GroupFlags.CONTEXT)
    reg.registered_modules[_GROUP_MODULE_NAME] = grp_mod


def test_load_chunks_subchunk_in_group() -> None:
    """load_chunks adds sub-chunks as params of their nearest ancestor CONTEXT+module chunk."""
    _ensure_test_group_module()

    stage = Usd.Stage.CreateInMemory()
    scene = _DummyScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")

    # Author group prim with its type attr
    grp_prim = stage.DefinePrim(Sdf.Path(f"{_CHUNKS_ROOT}/grp"), _CHUNK_PRIM_TYPENAME)
    grp_attr = grp_prim.CreateAttribute(_CHUNK_TYPE_ATTR, Sdf.ValueTypeNames.Token, custom=False)
    grp_attr.SetVariability(Sdf.VariabilityUniform)
    grp_attr.Set(_GROUP_MODULE_NAME)

    # Author sub-chunk prim nested under the group prim
    sub_prim = stage.DefinePrim(Sdf.Path(f"{_CHUNKS_ROOT}/grp/c1"), _CHUNK_PRIM_TYPENAME)
    sub_attr = sub_prim.CreateAttribute(_CHUNK_TYPE_ATTR, Sdf.ValueTypeNames.Token, custom=False)
    sub_attr.SetVariability(Sdf.VariabilityUniform)
    sub_attr.Set("prim")

    load_chunks(stage, scene)

    all_chunks = scene.get_chunks()
    ctx = next((c for c in all_chunks if c.has_flag(ChunkFlags.GroupFlags.CONTEXT)), None)
    assert ctx is not None, "No CONTEXT group chunk found after load_chunks"
    sub = [p for p in ctx.get_my_params() if isinstance(p, ChunkBase)]
    assert len(sub) == 1
    # The chunk's name is restored from its prim (prims are authored at
    # <root>/<chunk.name>), not reset to the module default — otherwise two
    # chunks of the same type can swap identities on load.
    assert sub[0].name == "c1"


# ---------------------------------------------------------------------------
# get_unique_path: expression path clears stale anonymous layer prim
# ---------------------------------------------------------------------------

class _LayerChunk:
    """Minimal chunk stub whose get_edit_context returns a controlled anonymous layer."""
    def __init__(self, layer: Sdf.Layer) -> None:
        self._layer = layer

    def get_edit_context(self) -> Sdf.Layer:
        return self._layer


class _ExprParam:
    """Minimal param stub with a pre-resolved expression value and a stale anonymous layer."""
    def __init__(self, resolved_path: str, layer: Sdf.Layer) -> None:
        self.raw_value = "@A.primpath_location@/geo"
        self._resolved = resolved_path
        self._chunk = _LayerChunk(layer)

    @property
    def value(self) -> str:
        return self._resolved

    def get_chunk(self) -> _LayerChunk:
        return self._chunk


def test_get_unique_path_clears_layer_on_expression() -> None:
    """get_unique_path clears the chunk's anonymous layer even when raw_value is an expression.

    Bug: previously the layer.Clear() call was guarded by the non-expression branch, so a stale
    prim authored during a previous execution (e.g. /geo) would persist after the primpath_location
    was changed to an expression like @A.primpath_location@/geo.
    """
    stage = Usd.Stage.CreateInMemory()

    # Stale anonymous layer simulating a prim left over from a previous execution
    old_layer = Sdf.Layer.CreateAnonymous()
    Sdf.CreatePrimInLayer(old_layer, Sdf.Path("/geo"))
    assert old_layer.GetPrimAtPath(Sdf.Path("/geo")) is not None

    param = _ExprParam("/prim/geo", old_layer)

    result = get_unique_path(stage, param)

    assert result == "/prim/geo"
    assert old_layer.GetPrimAtPath(Sdf.Path("/geo")) is None  # stale prim was cleared


# ---------------------------------------------------------------------------
# load_chunks: dynamic attr_grp params restored from saved chunk-prim attrs
# ---------------------------------------------------------------------------

def test_load_chunks_restores_light_dynamic_params() -> None:
    """_restore_dynamic_params re-populates attr_grp params created by show_attribute.

    Regression: before the fix, loading a saved light chunk left attr_grp params at
    USD schema defaults because show_attribute wired them to the freshly-created light
    prim (schema defaults), and the saved chunk-prim storage attrs were never consulted.
    """
    stage = Usd.Stage.CreateInMemory()
    scene = _DummyScene()

    # Author a chunk prim as it would appear in a saved .usda file.
    prim = stage.DefinePrim(Sdf.Path(f"{_CHUNKS_ROOT}/Light"), _CHUNK_PRIM_TYPENAME)
    prim.CreateAttribute(_CHUNK_TYPE_ATTR, Sdf.ValueTypeNames.Token).Set("light")
    prim.CreateAttribute("inputs:primpath_location", Sdf.ValueTypeNames.String).Set("/lights/light1")
    prim.CreateAttribute(
        "inputs:attr_grp:inputs:color", Sdf.ValueTypeNames.Color3f
    ).Set(Gf.Vec3f(-1.339, 1.0, 1.0))

    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    _chunk_usd_init(stage, scene)

    light_chunks = [c for c in scene.get_chunks() if c.name == "Light"]
    assert light_chunks, "Light chunk was not loaded"
    color = light_chunks[0].get_param("attr_grp.inputs.color")
    assert color is not None, "attr_grp.inputs.color param not found after load"
    assert pytest.approx(tuple(color.value), abs=1e-3) == (-1.339, 1.0, 1.0)


def test_loft_default_valued_param_creates_attr() -> None:
    """Loft up must work right after creating a chunk, when the param is still at its default
    and has no USD attribute yet — it force-creates the storage attr and promotes it."""
    import weakref

    stage = Usd.Stage.CreateInMemory()

    class _StageScene(SceneSender):
        def get_stage(self):
            return stage

        def get_selected_prims(self):
            return []

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    _chunk_usd_init(stage, scene)

    grpmod = reg.get_chunk_module("chunkgroup")
    primmod = reg.get_chunk_module("prim")

    def mk(mod):
        chunks = mod.create() if mod.loaded else mod.load()
        for c in chunks:
            scene.add_chunk(c)
        return chunks[0]

    grp = mk(grpmod)
    scene._current_context = weakref.ref(grp)
    prim_chunk = mk(primmod)

    p = prim_chunk.get_param("primpath_location")
    assert p._param_attr_path is None  # default value → no attr yet (the bug)

    loft_param_to_context(p, prim_chunk.get_context())

    assert p._param_attr_path is not None  # attr force-created
    grp_prim = stage.GetPrimAtPath(Sdf.Path(grp.prim_path))
    assert grp_prim.GetAttribute(f"{_INPUTS_PREFIX}{p.name}").IsValid()
    assert p.name in [x.name for x in grp.get_my_params()]  # promoted onto the group


def test_loft_output_param_creates_outputs_attr() -> None:
    """Lofting an OUTPUT-flagged param exposes it as a data OUTPUT on the group (outputs:name),
    so a chunkgroup can surface computed results — not just inputs."""
    import weakref

    stage = Usd.Stage.CreateInMemory()

    class _StageScene(SceneSender):
        def get_stage(self):
            return stage

        def get_selected_prims(self):
            return []

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    _chunk_usd_init(stage, scene)

    grpmod = reg.get_chunk_module("chunkgroup")
    primmod = reg.get_chunk_module("prim")

    def _mk(mod):
        cs = mod.create() if mod.loaded else mod.load()
        for c in cs:
            scene.add_chunk(c)
        return cs[0]

    grp = _mk(grpmod)
    scene._current_context = weakref.ref(grp)
    leaf = _mk(primmod)

    out_param = chunk.LineEdit(
        "myout", "My Out", default_value="hi", flags=chunk.ChunkFlags.ParamFlags.OUTPUT
    )
    leaf.add_param(out_param)
    loft_param_to_context(out_param, leaf.get_context())

    grp_prim = stage.GetPrimAtPath(Sdf.Path(grp.prim_path))
    assert grp_prim.GetAttribute("outputs:myout").IsValid()
    assert not grp_prim.GetAttribute(f"{_INPUTS_PREFIX}myout").IsValid()


def test_loft_group_param_up_to_parent_group() -> None:
    """A lofted param must be able to loft AGAIN up to the parent group (nested groups) instead
    of duplicating onto the same group. clone() carried over the source param's cached
    _chunk_parent (the inner chunk), so the lofted param's get_chunk() still pointed at the inner
    chunk — the Loft-up resolver ('param sits on a group → target its parent') couldn't fire and
    re-lofted onto the same group. loft_param_to_context must clear the cache so get_chunk()
    re-resolves to the group the param now lives on."""
    import weakref
    from usd_chunks.plugins.chunk_usd import get_chunk_prim_path

    stage = Usd.Stage.CreateInMemory()

    class _StageScene(SceneSender):
        def get_stage(self):
            return stage

        def get_selected_prims(self):
            return []

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    _chunk_usd_init(stage, scene)

    grpmod = reg.get_chunk_module("chunkgroup")
    primmod = reg.get_chunk_module("prim")

    def _mk(mod):
        cs = mod.create() if mod.loaded else mod.load()
        for c in cs:
            scene.add_chunk(c)
        return cs[0]

    # nested groups: grp1 -> grp2 -> inner
    grp1 = _mk(grpmod)
    scene._current_context = weakref.ref(grp1)
    grp2 = _mk(grpmod)
    scene._current_context = weakref.ref(grp2)
    inner = _mk(primmod)
    scene._current_context = None

    p = inner.get_param("primpath_location")
    loft_param_to_context(p, grp2)  # first loft: inner -> grp2

    lofted = next(x for x in grp2.get_my_params() if x.name == "primpath_location")
    # The lofted param must report the group it now lives on, not the original inner chunk.
    assert lofted.get_chunk() is grp2

    # Replicate the widget's Loft-up target resolution: a param on a group lofts to the parent.
    chunk_obj = lofted.get_chunk()
    context = chunk_obj.get_context()
    assert context is chunk_obj  # get_context() returns the group itself
    context = chunk_obj._parent_context()  # so resolve up to the parent group
    assert context is grp1

    loft_param_to_context(lofted, context)  # second loft: grp2 -> grp1
    grp1_prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(grp1)))
    assert grp1_prim.GetAttribute(f"{_INPUTS_PREFIX}primpath_location").IsValid()


def test_recook_recurses_into_group() -> None:
    """A group has no execute() of its own — cooking a group (in the parent view's tree) must
    cook its inner active view's tree; a hidden group mutes its whole inner tree."""
    import weakref
    from usd_chunks.plugins.chunk_usd import recook_for_view, get_chunk_prim_path

    stage = Usd.Stage.CreateInMemory()

    class _StageScene(SceneSender):
        def get_stage(self):
            return stage

        def get_selected_prims(self):
            return []

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    _chunk_usd_init(stage, scene)

    grpmod = reg.get_chunk_module("chunkgroup")
    primmod = reg.get_chunk_module("prim")
    viewmod = reg.get_chunk_module("view")

    def _mk(mod):
        cs = mod.create() if mod.loaded else mod.load()
        for c in cs:
            scene.add_chunk(c)
        return cs[0]

    def _wire(dst, src):
        p = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(dst)))
        a = p.GetAttribute(_EXEC_INPUT) or p.CreateAttribute(_EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False)
        a.SetConnections([Sdf.Path(f"{get_chunk_prim_path(src)}.{_EXEC_OUTPUT}")])

    grp = _mk(grpmod)
    scene._current_context = weakref.ref(grp)
    inner_node = _mk(primmod)
    inner_view = _mk(viewmod)
    _wire(inner_view, inner_node)
    scene._current_context = None
    tab_view = _mk(viewmod)
    _wire(tab_view, grp)

    class _Root:
        prim_path = _CHUNKS_ROOT

    recook_for_view(stage, scene, _Root(), tab_view)
    assert not inner_node.is_blocked()  # group cooked → inner tree cooks

    recook_for_view(stage, scene, _Root(), None)
    assert inner_node.is_blocked()  # group hidden → inner tree muted


def test_recook_for_view_reveals_upstream_incrementally(qapp) -> None:
    """Regression reported live in usdview: Transform1 -> Scatter -> Transform2 (both
    transforms targeting the same prim), with a real, properly-activated View wired to
    Transform2. Merely creating an unrelated, unconnected new node elsewhere in the graph
    re-triggers a recook of the active view (ChunkGraphView.build() unconditionally
    schedules one) -- and that recook used to unmute recook_for_view's ENTIRE upstream tree
    before executing any of it. Scatter (reactive, reads the target prim's composed
    transform during its own execute()) would then see Transform2's stale, previously-
    authored edit even though Scatter legitimately executes before Transform2 in cook_list
    order, because that edit's layer was already unmuted and outranked Transform1's per the
    "closer to view wins" ranking. Layers must instead be revealed one at a time, immediately
    before each chunk's own turn.

    Needs the real Qt graph widget: a hand-built recook_for_view() call in isolation doesn't
    reproduce this (the live bug depends on state accumulated by the real view-activation
    path -- ChunkGraphView._activate_menu_view's view:slot attribute + _view_state.set_slot
    -- not just wiring the exec chain by hand)."""
    from usd_chunks.plugins.chunk_usd import get_chunk_prim_path, get_exec_upstream_chunks
    from usd_chunks.plugins.chunk_qt import ChunkWidgetDocker

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
    prim_mod = reg.get_chunk_module("prim")  # any node type -- not specific to one chunk kind

    def _mk(mod):
        cs = mod.create() if mod.loaded else mod.load()
        for c in cs:
            scene.add_chunk(c)
        return cs[0]

    def _wire(dst, src):
        # Wire through the dependency API alone and let ChunkStageSync's listener author
        # inputs:chunk. It writes into the chunk-prim bookkeeping (session) layer, which is
        # where the live graph's own wiring lands. Hand-authoring the connection first (the
        # SetConnections other recook tests in this file use) goes to the stage's AMBIENT edit
        # target -- the root layer -- and is doubly wrong here: the listener then sees the
        # connection already present, skips its own session-layer write, and the root-layer
        # opinion is later dropped, leaving inputs:chunk with no connections at all. The exec
        # chain then reads as three unwired nodes, get_exec_upstream_chunks returns [], and the
        # whole cook set collapses to "nothing cooks" -- 0.0 instead of 99.0 at the
        # precondition below, before the behaviour under test is ever reached.
        dst.add_dependency(src)

    def _pump(seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            qapp.processEvents()

    # A real mesh, not a bare Xform: Scatter's no-triangles-found fallback is a jittered grid
    # in prim-local coordinates that never reads the prim's transform at all, which would make
    # this test structurally incapable of ever exercising the bug regardless of which code is
    # running (found the hard way -- an earlier draft of this test used a bare Xform target).
    target = UsdGeom.Mesh.Define(stage, "/target")
    target.GetPointsAttr().Set([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
    target.GetFaceVertexCountsAttr().Set([3])
    target.GetFaceVertexIndicesAttr().Set([0, 1, 2])
    target = target.GetPrim()

    t1 = _mk(transform_mod)
    scatter = _mk(scatter_mod)
    t2 = _mk(transform_mod)
    qapp.processEvents()

    t1.get_param("primpath_location").value = "/target"
    scatter.get_param("surface_prim").value = "/target"
    scatter.get_param("max_count").value = 1
    t2.get_param("primpath_location").value = "/target"
    _wire(scatter, t1)
    _wire(t2, scatter)
    # Settle before touching views. Creating a node arms ChunkGraphView._ensure_default_view on
    # a 60ms timer, which makes its own view on the exec tail and claims a slot. Pumping past it
    # here pins down which view exists first instead of letting chunk-module load times decide,
    # so the slot our view gets below (and therefore which view is active) is deterministic.
    _pump(0.2)

    # Create + properly activate a view wired to t2 (the real tail), the same way the actual
    # "Set View" / chunk-menu flow does: view:slot attribute + _view_state.set_slot -- not
    # just wiring the exec chain by hand, which (confirmed while investigating this bug)
    # leaves _view_state.active_slot at 0 and doesn't reproduce the live behaviour.
    gv = docker.tab_widget.get_current_graph_view()
    context = gv._graph_context()
    view_mod = reg.get_chunk_module("view")
    view = _mk(view_mod)
    _wire(view, t2)
    qapp.processEvents()
    gv._activate_menu_view(view, context, stage)
    _pump(0.1)  # _activate_menu_view defers set_slot through a 0ms timer
    view_slot = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(view))).GetAttribute("view:slot").Get()
    assert gv._view_state.active_slot == view_slot, (
        "our view must be the active one for this repro to be faithful"
    )
    assert [c.name for c in get_exec_upstream_chunks(view, stage, scene)] == [
        t1.name, scatter.name, t2.name
    ], "the exec chain must be wired roots-first before the behaviour under test can be reached"

    xform_cache = UsdGeom.XformCache()

    def _target_tx() -> float:
        xform_cache.Clear()
        return xform_cache.GetLocalToWorldTransform(target).ExtractTranslation()[0]

    def _scatter_tx() -> float:
        xform_cache.Clear()
        pos = stage.GetPrimAtPath("/ScatterInstancer").GetAttribute("positions").Get()
        return pos[0][0]

    t1.get_param("tx").value = 1.0
    t2.get_param("tx").value = 99.0
    _pump(0.5)
    assert _target_tx() == pytest.approx(99.0), "precondition: the settled prim must show t2's result"

    # This is the actual live trigger: creating any unrelated, unconnected new node
    # re-triggers a recook of the still-active view (see ChunkGraphView.build()).
    _mk(prim_mod)
    _pump(1.0)

    # Mesh points are (0,0,0),(1,0,0),(0,1,0) -- any barycentric sample has local X in [0, 1],
    # so a fixed range check sidesteps needing an exact expected value (which depends on the
    # sampler's internal RNG draws): t1's offset (tx=1) puts the correct answer in [1, 2];
    # t2's offset (tx=99) -- the bug -- would put it in [99, 100].
    final_x = _scatter_tx()
    assert 1.0 <= final_x <= 2.0, (
        f"Scatter must read only its real upstream (t1, tx=1), never t2 (tx=99, downstream of "
        f"it) -- creating an unrelated node must not shift the scattered point to t2's offset. "
        f"Got x={final_x}"
    )
    assert _target_tx() == pytest.approx(99.0), (
        "the settled prim itself must still show t2's result -- the view's own 'closest wins' "
        "ranking is unaffected by the incremental reveal during execution"
    )


def test_recook_focus_group_mutes_outside_subtree() -> None:
    """Focusing a group (the tab you're looking at) must mute everything OUTSIDE the group so
    only its inner active view drives the shared stage. recook_for_view alone only manages the
    focused context's own children, so a parent-tab chunk stayed unmuted and bled through when
    switching into a group tab ('switch tabs, still see the previous view')."""
    import weakref
    from usd_chunks.plugins.chunk_usd import recook_focus, get_chunk_prim_path

    stage = Usd.Stage.CreateInMemory()

    class _StageScene(SceneSender):
        def get_stage(self):
            return stage

        def get_selected_prims(self):
            return []

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    _chunk_usd_init(stage, scene)

    grpmod = reg.get_chunk_module("chunkgroup")
    primmod = reg.get_chunk_module("prim")
    viewmod = reg.get_chunk_module("view")

    def _mk(mod):
        cs = mod.create() if mod.loaded else mod.load()
        for c in cs:
            scene.add_chunk(c)
        return cs[0]

    def _wire(dst, src):
        p = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(dst)))
        a = p.GetAttribute(_EXEC_INPUT) or p.CreateAttribute(_EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False)
        a.SetConnections([Sdf.Path(f"{get_chunk_prim_path(src)}.{_EXEC_OUTPUT}")])

    # A tab-level chunk driven by a tab view, plus a group with its own inner node + view.
    tab_node = _mk(primmod)
    tab_view = _mk(viewmod)
    _wire(tab_view, tab_node)
    grp = _mk(grpmod)
    scene._current_context = weakref.ref(grp)
    inner_node = _mk(primmod)
    inner_view = _mk(viewmod)
    _wire(inner_view, inner_node)
    scene._current_context = None

    # Focus the group: only its inner tree drives the stage.
    recook_focus(stage, scene, grp, inner_view)
    assert not inner_node.is_blocked()   # inner active view's tree cooks
    assert tab_node.is_blocked()          # everything outside the group is muted


def test_recook_focus_does_not_block_ancestor_container() -> None:
    """Focusing a nested group must NOT block its ancestor container. is_blocked() climbs the
    owner chain, so blocking an ancestor propagates down and blocks the focused inner tree —
    'nothing in the chunkgroups gets cooked'. Ancestors (all context chunks) must be left alone."""
    import weakref
    from usd_chunks.plugins.chunk_usd import recook_focus, get_chunk_prim_path

    stage = Usd.Stage.CreateInMemory()

    class _StageScene(SceneSender):
        def get_stage(self):
            return stage

        def get_selected_prims(self):
            return []

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    _chunk_usd_init(stage, scene)

    grpmod = reg.get_chunk_module("chunkgroup")
    primmod = reg.get_chunk_module("prim")
    viewmod = reg.get_chunk_module("view")

    def _mk(mod):
        cs = mod.create() if mod.loaded else mod.load()
        for c in cs:
            scene.add_chunk(c)
        return cs[0]

    def _wire(dst, src):
        p = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(dst)))
        a = p.GetAttribute(_EXEC_INPUT) or p.CreateAttribute(_EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False)
        a.SetConnections([Sdf.Path(f"{get_chunk_prim_path(src)}.{_EXEC_OUTPUT}")])

    # An OUTER group (the ancestor container) holding an INNER group with its own node + view.
    outer = _mk(grpmod)
    scene._current_context = weakref.ref(outer)
    inner_grp = _mk(grpmod)
    scene._current_context = weakref.ref(inner_grp)
    inner_node = _mk(primmod)
    inner_view = _mk(viewmod)
    _wire(inner_view, inner_node)
    scene._current_context = None

    # Focus the inner group — the outer container must not be blocked, so the inner tree cooks.
    recook_focus(stage, scene, inner_grp, inner_view)
    assert not outer.is_blocked()        # ancestor container left alone
    assert not inner_node.is_blocked()   # inner active view's tree cooks


def test_delete_middle_node_heals_exec_chain() -> None:
    """Deleting a node in the middle of an exec chain (A -> X -> B) must leave A -> B, not a
    broken chain. Regression: removing a chunk wired between a chunkgroup and a view left the
    view with a dangling connection and the chunkgroup unconsumed."""
    from usd_chunks.plugins.chunk_usd import get_chunk_prim_path

    stage = Usd.Stage.CreateInMemory()

    class _StageScene(SceneSender):
        def get_stage(self):
            return stage

        def get_selected_prims(self):
            return []

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    _chunk_usd_init(stage, scene)

    grpmod = reg.get_chunk_module("chunkgroup")
    primmod = reg.get_chunk_module("prim")
    viewmod = reg.get_chunk_module("view")

    def _mk(mod):
        cs = mod.create() if mod.loaded else mod.load()
        for c in cs:
            scene.add_chunk(c)
        return cs[0]

    def _wire(dst, src):
        p = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(dst)))
        a = p.GetAttribute(_EXEC_INPUT) or p.CreateAttribute(_EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False)
        a.SetConnections([Sdf.Path(f"{get_chunk_prim_path(src)}.{_EXEC_OUTPUT}")])

    a = _mk(grpmod)   # A: chunkgroup
    x = _mk(primmod)  # X: middle chunk
    b = _mk(viewmod)  # B: view
    _wire(x, a)  # A -> X
    _wire(b, x)  # X -> B

    a_out = f"{get_chunk_prim_path(a)}.{_EXEC_OUTPUT}"
    x_out = f"{get_chunk_prim_path(x)}.{_EXEC_OUTPUT}"
    b_in = stage.GetAttributeAtPath(Sdf.Path(f"{get_chunk_prim_path(b)}.{_EXEC_INPUT}"))
    assert [str(c) for c in b_in.GetConnections()] == [x_out]  # B <- X before delete

    x.request_delete()  # fires CHUNK_DELETED -> heal runs before the prim is removed

    b_in = stage.GetAttributeAtPath(Sdf.Path(f"{get_chunk_prim_path(b)}.{_EXEC_INPUT}"))
    assert [str(c) for c in b_in.GetConnections()] == [a_out]  # B <- A after delete (healed)


def test_rename_group_is_sibling_and_rebases_children() -> None:
    """Renaming a chunkgroup must rename it in place (sibling), not nest it inside itself
    (/G -> /G/new — the get_context()-returns-self bug), and must move + rebase its children."""
    import weakref
    from usd_chunks.plugins.chunk_usd import get_chunk_prim_path

    stage = Usd.Stage.CreateInMemory()

    class _StageScene(SceneSender):
        def get_stage(self):
            return stage

        def get_selected_prims(self):
            return []

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    _chunk_usd_init(stage, scene) or ChunkStageSync()

    grpmod = reg.get_chunk_module("chunkgroup")
    primmod = reg.get_chunk_module("prim")

    def _mk(mod):
        cs = mod.create() if mod.loaded else mod.load()
        for c in cs:
            scene.add_chunk(c)
        return cs[0]

    grp = _mk(grpmod)
    scene._current_context = weakref.ref(grp)
    child = _mk(primmod)
    old_root = get_chunk_prim_path(grp)

    grp.name = "hahaha"

    new_root = get_chunk_prim_path(grp)
    assert new_root == str(Sdf.Path(old_root).GetParentPath().AppendChild("hahaha"))
    assert new_root != old_root
    assert not stage.GetPrimAtPath(Sdf.Path(old_root)).IsValid()
    assert stage.GetPrimAtPath(Sdf.Path(new_root)).IsValid()
    # child moved with the subtree and its tracked path was rebased
    assert ChunkStageSync()._chunk_prim_paths[id(child)] == f"{new_root}/{child.name}"
    assert stage.GetPrimAtPath(Sdf.Path(f"{new_root}/{child.name}")).IsValid()


def test_can_create_chunk_inside_blocked_group() -> None:
    """Editing inside a group must work even when the group is view-driven blocked (hidden). A
    blocked group used to swallow its children's structural notices (PARAM_ADDED), so a child's
    prim was never created — you couldn't add nodes inside a group not wired to an active view."""
    import weakref
    from usd_chunks.plugins.chunk_usd import get_chunk_prim_path

    stage = Usd.Stage.CreateInMemory()

    class _StageScene(SceneSender):
        def get_stage(self):
            return stage

        def get_selected_prims(self):
            return []

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    _chunk_usd_init(stage, scene)

    grpmod = reg.get_chunk_module("chunkgroup")
    primmod = reg.get_chunk_module("prim")

    grp = (grpmod.create() if grpmod.loaded else grpmod.load())[0]
    scene.add_chunk(grp)
    grp.set_blocked(True)  # simulate: group hidden / out of the active view's tree

    scene._current_context = weakref.ref(grp)
    child = (primmod.create() if primmod.loaded else primmod.load())[0]
    scene.add_chunk(child)

    assert child.is_blocked()  # inherits the group's block (won't cook)
    assert stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(child))).IsValid()  # but the node exists


def test_inner_view_cooks_when_group_hidden_at_parent() -> None:
    """A group is its own cook domain: diving into it and activating its inner view must cook
    the inner tree even when the group itself is hidden at the parent (not wired to a parent
    view). Previously the group's block propagated to its children via the owner chain, so the
    inner view was inert unless the group had a parent view."""
    import weakref
    from usd_chunks.plugins.chunk_usd import recook_for_view, get_chunk_prim_path

    stage = Usd.Stage.CreateInMemory()

    class _StageScene(SceneSender):
        def get_stage(self):
            return stage

        def get_selected_prims(self):
            return []

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    _chunk_usd_init(stage, scene)

    grpmod = reg.get_chunk_module("chunkgroup")
    primmod = reg.get_chunk_module("prim")
    viewmod = reg.get_chunk_module("view")

    def _mk(mod):
        cs = mod.create() if mod.loaded else mod.load()
        for c in cs:
            scene.add_chunk(c)
        return cs[0]

    def _wire(dst, src):
        p = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(dst)))
        a = p.GetAttribute(_EXEC_INPUT) or p.CreateAttribute(_EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False)
        a.SetConnections([Sdf.Path(f"{get_chunk_prim_path(src)}.{_EXEC_OUTPUT}")])

    # tab: a prim P with an active view V; a group G that is NOT wired to V (hidden).
    P = _mk(primmod)
    V = _mk(viewmod)
    _wire(V, P)
    grp = _mk(grpmod)
    scene._current_context = weakref.ref(grp)
    inner = _mk(primmod)
    inner_view = _mk(viewmod)
    _wire(inner_view, inner)
    scene._current_context = None

    class _Root:
        prim_path = _CHUNKS_ROOT

    recook_for_view(stage, scene, _Root(), V)  # tab active view = V; group hidden
    assert inner.is_blocked()  # group hidden at parent -> inner tree muted

    recook_for_view(stage, scene, grp, inner_view)  # dive in, inner view active
    assert not inner.is_blocked()  # inner view cooks despite the group being hidden at parent


# ---------------------------------------------------------------------------
# Chunkgroup lifecycle
# ---------------------------------------------------------------------------

def _group_scene():
    """A stage-backed scene with the sync initialised, plus the chunkgroup/prim modules."""
    import weakref

    stage = Usd.Stage.CreateInMemory()

    class _StageScene(SceneSender):
        def get_stage(self):
            return stage

        def get_selected_prims(self):
            return []

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    _chunk_usd_init(stage, scene)
    return stage, scene, reg, weakref


def test_two_groups_get_unique_prim_paths() -> None:
    """Creating two chunkgroups yields two distinct prims (no collision/skip)."""
    stage, scene, reg, weakref = _group_scene()
    grpmod = reg.get_chunk_module("chunkgroup")

    def _mkgrp():
        cs = grpmod.create() if grpmod.loaded else grpmod.load()
        for c in cs:
            scene.add_chunk(c)
        return cs[0]

    g1, g2 = _mkgrp(), _mkgrp()
    from usd_chunks.plugins.chunk_usd import get_chunk_prim_path

    assert get_chunk_prim_path(g1) != get_chunk_prim_path(g2)
    assert stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(g1))).IsValid()
    assert stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(g2))).IsValid()


def test_nested_group_prim_paths() -> None:
    """A group nested in a group gets a prim under the parent group; a leaf in it nests deeper."""
    from usd_chunks.plugins.chunk_usd import get_chunk_prim_path

    stage, scene, reg, weakref = _group_scene()
    grpmod = reg.get_chunk_module("chunkgroup")
    primmod = reg.get_chunk_module("prim")

    def _mk(mod):
        cs = mod.create() if mod.loaded else mod.load()
        for c in cs:
            scene.add_chunk(c)
        return cs[0]

    outer = _mk(grpmod)
    scene._current_context = weakref.ref(outer)
    inner = _mk(grpmod)
    scene._current_context = weakref.ref(inner)
    leaf = _mk(primmod)

    outer_p = get_chunk_prim_path(outer)
    assert get_chunk_prim_path(inner) == f"{outer_p}/{inner.name}"
    assert get_chunk_prim_path(leaf) == f"{outer_p}/{inner.name}/{leaf.name}"
    assert stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(leaf))).IsValid()


def test_delete_group_removes_its_subtree() -> None:
    """Deleting a chunkgroup removes the group prim and everything under it."""
    from usd_chunks.plugins.chunk_usd import get_chunk_prim_path

    stage, scene, reg, weakref = _group_scene()
    grpmod = reg.get_chunk_module("chunkgroup")
    primmod = reg.get_chunk_module("prim")

    def _mk(mod):
        cs = mod.create() if mod.loaded else mod.load()
        for c in cs:
            scene.add_chunk(c)
        return cs[0]

    grp = _mk(grpmod)
    scene._current_context = weakref.ref(grp)
    child = _mk(primmod)
    scene._current_context = None
    grp_path = get_chunk_prim_path(grp)
    child_path = get_chunk_prim_path(child)
    assert stage.GetPrimAtPath(Sdf.Path(child_path)).IsValid()

    grp.request_delete()

    assert not stage.GetPrimAtPath(Sdf.Path(grp_path)).IsValid()
    assert not stage.GetPrimAtPath(Sdf.Path(child_path)).IsValid()


def test_get_upstream_stage_preserves_real_relative_order_among_included_chunks() -> None:
    """Regression: get_upstream_stage's first draft built its scratch layer order from
    scene.get_chunks()'s own iteration order -- reported live as a bowl (created by one chunk,
    then rotated by a Transform wired after it) reading with the wrong orientation once RBD's
    collision-prim read went through get_upstream_stage: both the creating chunk and the
    rotating Transform are "not downstream" of RBD, so both get included, but which one *wins*
    for their shared prim was being decided by creation order instead of the real, already-
    correct order (the same sort-key/view-driven ranking recook_for_view relies on elsewhere).
    Included layers must keep their real relative strength order -- filtered, not rebuilt."""
    from usd_chunks.plugins.chunk_usd import get_chunk_prim_path, get_upstream_stage

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
    prim_mod = reg.get_chunk_module("prim")

    def _mk(mod):
        cs = mod.create() if mod.loaded else mod.load()
        for c in cs:
            scene.add_chunk(c)
        return cs[0]

    def _wire(dst, src):
        # add_dependency alone -- see the _wire in
        # test_recook_for_view_reveals_upstream_incrementally for why hand-authoring the
        # connection alongside it silently leaves inputs:chunk unconnected.
        dst.add_dependency(src)

    stage.DefinePrim("/bowl", "Xform")

    # t1 "creates" the bowl (positions it); t2 (wired after t1) rotates it -- both authoring
    # xformOp:transform on the same prim, exactly like a mesh-creation chunk followed by a
    # Transform. A third, unrelated chunk (the "reference" -- standing in for RBD) reads
    # through get_upstream_stage: neither t1 nor t2 is downstream of it, so both are included.
    #
    # t2 is created FIRST, t1 SECOND (still wired t1 -> t2, t1 upstream): scene.get_chunks()
    # orders more-recently-created chunks first, so this creation order makes a naive
    # "order by scene.get_chunks()" implementation put t1 ahead of t2 -- the opposite of the
    # real, forced-below precondition -- so this setup actually exercises the bug rather than
    # coincidentally agreeing with the fix regardless of which implementation is used.
    t2 = _mk(transform_mod)
    reference = _mk(prim_mod)
    t1 = _mk(transform_mod)
    _wire(t2, t1)

    t1.get_param("primpath_location").value = "/bowl"
    t1.get_param("tx").value = 1.0
    t2.get_param("primpath_location").value = "/bowl"
    t2.get_param("tx").value = 2.0

    # Force the precondition this bug needs: t2 currently wins on the real stage. Plain
    # creation-time ordering favours whichever chunk was created first (t1) by default in a
    # short-lived test -- t2 only wins once something has established "downstream wins" (any
    # view activation/recook, confirmed repeatedly earlier while investigating the Scatter/
    # RBD bugs). This reproduces that real, ordinary state directly instead of depending on
    # Qt/view machinery just to set it up.
    lm = chunk_registry.ChunkLayerManager()
    t1_layer = lm.get_layer(t1)
    t2_layer = lm.get_layer(t2)
    graph_layer = next(g for g in lm.graph_layers.values() if t1_layer.identifier in g.subLayerPaths)
    graph_layer.subLayerPaths = [t2_layer.identifier] + [
        p for p in graph_layer.subLayerPaths if p != t2_layer.identifier
    ]

    xform_cache = UsdGeom.XformCache()

    def _real_tx() -> float:
        xform_cache.Clear()
        return xform_cache.GetLocalToWorldTransform(stage.GetPrimAtPath("/bowl")).ExtractTranslation()[0]

    def _scratch_tx() -> float:
        scratch_stage = get_upstream_stage(stage, scene, reference)
        xform_cache.Clear()
        return xform_cache.GetLocalToWorldTransform(scratch_stage.GetPrimAtPath("/bowl")).ExtractTranslation()[0]

    # t2 must currently win on the real, live stage -- confirming the forced precondition,
    # not the fix itself.
    assert _real_tx() == pytest.approx(2.0)

    # The scratch stage must agree: t2 must still win, not t1, even though neither is
    # downstream of `reference` and both get included.
    assert _scratch_tx() == pytest.approx(2.0), (
        "get_upstream_stage must preserve the real relative order between two included "
        "chunks that touch the same prim, not silently pick a different winner"
    )
