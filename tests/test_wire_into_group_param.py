"""A wire into a group's own param reads the chunk beside the group, not one inside it."""
from __future__ import annotations

import contextlib

from pxr import Usd

from usd_chunks import chunk, chunk_registry, chunk_stage
from usd_chunks.chunk_flags import ChunkFlags

from chunk_stubs import attach_stub_module
from tests.test_expression_arithmetic import _mk, scene  # noqa: F401 -- scene is a fixture
from tests.test_nested_chunkgroup_roundtrip import _scene_for
from tests.test_unloft import _loft, _prim, env  # noqa: F401 -- env is a fixture
from usd_chunks.chunk_stage import get_chunk_prim_path, save_scene
from usd_chunks.graph import engine as chunk_graph


def _group(name: str) -> chunk.Chunk:
    g = chunk.Chunk(name, flags=ChunkFlags.GroupFlags.CONTEXT)
    attach_stub_module(g)
    return g


def _mk_in(scene, key: str, name: str, context=None):  # noqa: F811
    """A chunk of module key, named name, made in context (the tab's group by default)."""
    mod = chunk_registry.ChunkRegistry().get_chunk_module(key)
    made = mod.create() if mod.loaded else mod.load()
    made[0].name = name
    with scene.creating_in(context) if context is not None else contextlib.nullcontext():
        for c in made:
            scene.add_chunk(c)
    return made[0]


def _cube(scene):  # noqa: F811
    cube = _mk("cube")
    cube.add_param(chunk.LineEdit("primpath_location"))
    scene.add_chunk(cube)
    cube.get_param("primpath_location").value = "/cube"
    return cube


def test_a_groups_param_reads_a_wire_from_beside_it(scene):  # noqa: F811
    _cube(scene)
    group = _group("library")
    target = chunk.LineEdit("target_primpath", flags=ChunkFlags.ParamFlags.PRIM_SELECTION)
    group.add_param(target)
    scene.add_chunk(group)

    # What dragging a wire from the cube onto the field writes.
    target.value = "@cube.primpath_location@"

    assert target.value == "/cube"


def test_a_groups_param_follows_the_chunk_beside_it(scene):  # noqa: F811
    cube = _cube(scene)
    group = _group("library")
    target = chunk.LineEdit("target_primpath")
    group.add_param(target)
    scene.add_chunk(group)
    target.value = "@cube.primpath_location@"

    cube.get_param("primpath_location").value = "/world/cube"

    assert target.value == "/world/cube"


def test_a_chunk_inside_a_group_still_reads_its_siblings(scene):  # noqa: F811
    group = _group("library")
    scene.add_chunk(group)
    inner_a = _mk("inner_a")
    inner_a.add_param(chunk.LineEdit("primpath_location"))
    group.add_param(inner_a)
    inner_a.get_param("primpath_location").value = "/inner"
    inner_b = _mk("inner_b")
    path = chunk.LineEdit("path")
    inner_b.add_param(path)
    group.add_param(inner_b)

    path.value = "@inner_a.primpath_location@"

    assert path.value == "/inner"


def test_a_wire_into_a_groups_lofted_param_reaches_the_chunk_inside(env):  # noqa: F811
    stage = env["stage"]
    outside = env["mk"]("transform", name="Outside")
    group = env["mk"]("chunkgroup", context=outside.get_context(), name="Group")
    inner = env["mk"]("transform", context=group)
    env["settle"]()
    lofted = _loft(env, inner.get_param("tx"), group)
    src = next(p for p in chunk_graph.iter_ports(_prim(stage, outside), outside)
               if p.name == "tx" and not p.is_input)
    dst = next(p for p in chunk_graph.iter_ports(_prim(stage, group), group)
               if p.name == lofted.name and p.is_input)
    chunks = {get_chunk_prim_path(group): group, get_chunk_prim_path(outside): outside}
    assert chunk_graph.connect(stage, src, dst, chunks) is not None

    outside.get_param("tx").value = 4.0
    env["settle"]()

    assert lofted.value == 4.0
    assert inner.get_param("tx").value == 4.0


def test_a_wire_into_a_groups_lofted_param_survives_a_reopen(qapp, tmp_path):
    first = _scene_for(Usd.Stage.CreateInMemory())
    outside = _mk_in(first, "transform", "Outside")
    group = _mk_in(first, "chunkgroup", "Group")
    inner = _mk_in(first, "transform", "Inner", context=group)
    chunk_stage.loft_param_to_context(inner.get_param("tx"), group)
    [lofted] = [p for p in group.get_my_params() if getattr(p, "_lofted", False)]
    lofted.value = "@Outside.tx@"  # what a wire from Outside's tx writes
    outside.get_param("tx").value = 4.0
    qapp.processEvents()
    out = tmp_path / "wired_group.usda"
    save_scene(first, out)

    scene2 = _scene_for(Usd.Stage.Open(str(out)))
    qapp.processEvents()
    by_name = {c.name: c for c in scene2.get_chunks()}
    by_name["Outside"].get_param("tx").value = 7.0
    qapp.processEvents()

    [lofted2] = [p for p in by_name["Group"].get_my_params() if getattr(p, "_lofted", False)]
    assert lofted2.value == 7.0
    assert by_name["Inner"].get_param("tx").value == 7.0
