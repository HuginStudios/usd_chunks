"""A reopened network of chunks that loft their params on load cooks each of them once more.

A chunk that lofts its params in CHUNK_LOADED (a MaterialX Shader, from its nodedef) has
cooked once without them by then, so the load cooks it again. That second pass ran one
chunk at a time, and each ran everything below it: in a tree of shaders, the Surface at the
bottom cooked once per op above it (50 times in a 100-node network): an op reached from the
leaf above was skipped (nothing it reads changed), stayed stale, and then ran alone. Now the pass is one solve,
and every chunk in it cooks once, upstream first, with its restored values.
"""

from __future__ import annotations

import collections

from chunk_stubs import register_stub_module
from pxr import Sdf

from usd_chunks import Scene, chunk
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_notice import ChunkNoticeAlias

LEAVES = 8
COOKS: collections.Counter = collections.Counter()


def _loft(owner):
    if owner.get_param("level") is None and owner.get_param("kind").value:
        owner.add_param(chunk.Slider("level", "Level", default_value=0.0, min_value=0.0, max_value=1000.0))


def _kind_changed(owner, _notice, _sender):
    _loft(owner)


def _loaded(owner, _notice, _sender):
    _loft(owner)


def _main():
    c = chunk.Chunk("Lofter")
    kind = chunk.LineEdit("kind", "Kind")
    reads = chunk.LineEdit("reads", "Reads", flags=ChunkFlags.ParamFlags.PRIM_PATHS)
    c.add_params([kind, reads])
    c.add_callback(_kind_changed, sender=kind)
    c.add_callback(_loaded, notice_alias=ChunkNoticeAlias.CHUNK_LOADED)
    return c


def _execute(node):
    """Write level plus the values of the prims it reads, at /data/<name>."""
    COOKS[node.name] += 1
    stage = node.get_stage()
    level = node.get_param("level")
    total = level.value if level is not None else -1000.0
    for path in filter(None, (node.get_param("reads").value or "").split(",")):
        attr = stage.GetAttributeAtPath(f"{path.strip()}.v")
        total += (attr.Get() or 0.0) if attr else 0.0
    if node.get_param("kind").value == "sink":
        # Reads the whole stage, as a Surface binding by path does: never skipped.
        total += 0.0 * sum(1 for _ in stage.Traverse())
    prim = stage.DefinePrim(f"/data/{node.name}")
    prim.CreateAttribute("v", Sdf.ValueTypeNames.Double).Set(float(total))


def test_a_reopened_tree_cooks_its_sink_once_with_every_value(tmp_path):
    """Leaves into pairing nodes into one sink, the shape of a shader tree into a Surface."""
    path = str(tmp_path / "tree.usda")
    with Scene() as scene:
        module = register_stub_module(main=_main, execute=_execute)

        def add(*inputs, **params):
            (new,) = module.load()
            return scene.place(new, inputs, **params)

        with chunk.change_block():
            level = [add(name=f"Leaf{i}", kind="leaf", level=float(i + 1)) for i in range(LEAVES)]
            depth = 0
            while len(level) > 1:
                depth += 1
                # An op reads nothing of the leaves (a shader input is a USD connection,
                # not a value it copies), so a cook above it lets it be skipped.
                level = [add(*level[j:j + 2], name=f"Op{depth}_{j // 2}", kind="op")
                         for j in range(0, len(level), 2)]
            sink = add(level[0], name="Sink", kind="sink", level=100.0,
                       reads=",".join(f"/data/Leaf{i}" for i in range(LEAVES)))
            scene.set_output(sink)
        expected = 100.0 + sum(range(1, LEAVES + 1))
        assert scene.get_stage().GetAttributeAtPath("/data/Sink.v").Get() == expected
        scene.save(path)

    COOKS.clear()
    with Scene.open(path) as scene:
        assert scene.get_stage().GetAttributeAtPath("/data/Sink.v").Get() == expected
    # A few times as the network opens, the same as any chunk; not once more per op above it
    # (10 times here before: each op, skipped while stale, then ran alone and cooked it).
    assert COOKS["Sink"] <= 3, dict(COOKS)
    assert all(n <= 2 for name, n in COOKS.items() if name != "Sink"), dict(COOKS)
