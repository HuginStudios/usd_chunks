"""upstream_chunks is built from the chunks one step above, kept while the graph keeps its shape.

It walked every param above a chunk, once for each chunk asking: on the first edit after
opening a 300-Transform chain in usdview, 26 s of 36. The answer must stay the walk's own,
for every chunk, whatever the network: a chain, branches into a Merge, a param wire, a
chunkgroup, a param lofted into one (where the steps loop and the walk answers).
"""

from __future__ import annotations

from usd_chunks import Scene, chunk, chunk_stage, skip_cook


def _same_as_the_walk(scene) -> None:
    for c in scene.get_chunks():
        built = {id(x) for x in skip_cook.upstream_chunks(c)}
        walked = {id(x) for x in skip_cook._walk_upstream_chunks(c)}
        assert built == walked, f"{c.name}: {sorted(x.name for x in skip_cook.upstream_chunks(c))}"


def test_a_chain_and_branches_into_a_merge():
    with Scene() as scene:
        with chunk.change_block():
            box = scene.create.cube(name="Box", prim_path="/World/box")
            last = box
            for i in range(5):
                last = scene.create.transform(last, name=f"T{i}", primpath_location="/World/box", ty=0.1)
            ball = scene.create.sphere(name="Ball", prim_path="/World/ball")
            moved = scene.create.transform(ball, name="Move", primpath_location="/World/ball", tx=1.0)
            merge = scene.create.merge(last, moved, name="Merge")
            scene.set_output(merge)
        _same_as_the_walk(scene)


def test_a_param_wire():
    with Scene() as scene:
        with chunk.change_block():
            scene.create.transform(name="A", primpath_location="/World/a", ty=2.0)
            b = scene.create.transform(name="B", primpath_location="/World/b", ty="@A.ty@")
            scene.set_output(b)
        _same_as_the_walk(scene)


def test_a_chunkgroup_and_a_lofted_param():
    with Scene() as scene:
        box = scene.create.cube(prim_path="/box", name="Box")
        red = scene.create.set_display_color(box, prim_path="/box", color_r=1.0, name="Red")
        group = scene.create.chunkgroup(red, name="Recolor")
        with group:
            blue = scene.create.set_display_color(prim_path="/box", color_b=1.0, name="Blue")
            moved = scene.create.transform(blue, primpath_location="/box", tx=1.0, name="Move")
        _same_as_the_walk(scene)

        chunk_stage.loft_param_to_context(moved.get_param("tx"), group)
        _same_as_the_walk(scene)


def test_asking_every_chunk_visits_each_node_about_once(monkeypatch):
    """Every chunk of a chain asked in turn, as the first edit after opening does: a few visits
    per node (1 800 for 32 chunks), where a walk per chunk visited every node above it each
    time, the square of the chain (30 010)."""
    with Scene() as scene:
        with chunk.change_block():
            last = scene.create.cube(name="Box", prim_path="/World/box")
            for i in range(30):
                last = scene.create.transform(last, name=f"T{i}", primpath_location="/World/box", ty=0.1)
            scene.set_output(last)
        visits = []
        real = skip_cook._chunk_of
        monkeypatch.setattr(skip_cook, "_chunk_of", lambda node: visits.append(1) or real(node))
        skip_cook._upstream_memo = (-1, {})

        for c in scene.get_chunks():
            skip_cook.upstream_chunks(c)

        chunks = len(scene.get_chunks())
        assert len(visits) < 100 * chunks, f"{len(visits)} node visits for {chunks} chunks"

