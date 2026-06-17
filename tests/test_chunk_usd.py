from __future__ import annotations

import types as _types
from pathlib import Path

import pytest
from pxr import Gf, Sdf, Usd

from plugins.chunk_usd import (
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
    load_chunks,
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

    c = chunk.Chunk("testChunk")
    c._config = ChunkConfig(module="prim")
    prim_type = chunk.Menu("prim_type", menu_items=["Xform", "Scope", "Class"])
    prim_type.value = "Scope"
    c.add_param(prim_type)
    scene_src.add_chunk(c)

    out = tmp_path / "chunks.usda"
    export_chunks(scene_src, out)
    assert out.exists()

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

    # Author a sub-chunk prim at the old location
    stage.DefinePrim(Sdf.Path("/old_ctx/chunks"), _CHUNK_PRIM_TYPENAME)
    stage.DefinePrim(Sdf.Path("/old_ctx/chunks/c1"), _CHUNK_PRIM_TYPENAME)
    assert stage.GetPrimAtPath("/old_ctx/chunks/c1").IsValid()

    # Context chunk that owns c1 as a sub-chunk
    ctx = _chunk_with_module("ctx")
    ctx.prim_path = "/old_ctx"
    c1 = _chunk_with_module("c1")
    ctx.add_param(c1)

    rename_context_prim_path(ctx, "/new_ctx")

    assert ctx.prim_path == "/new_ctx"
    assert stage.GetPrimAtPath("/new_ctx/chunks/c1").IsValid()
    assert not stage.GetPrimAtPath("/old_ctx/chunks/c1").IsValid()


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
    assert sub[0].name == "Prim"


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
