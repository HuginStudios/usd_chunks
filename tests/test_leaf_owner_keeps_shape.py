"""A node with no edges joining its owner does not change the graph's shape.

A param makes an attribute sender the first time one of its attributes changes -- a
Transform setting its sliders' range in its first cook -- and that sender joins the param
as its owner. Counted as a change of shape, it dropped every chunk's cached relations
(skip_cook's _related, _beside, _chunk_prims), and the next cook worked them all out again
for every chunk in the network.
"""

from __future__ import annotations

from chunk_stubs import attach_stub_module

from usd_chunks import Scene, chunk, chunk_scheduler


def test_a_first_range_change_keeps_the_shape():
    with Scene() as scene:
        c = chunk.Chunk("Holder")
        attach_stub_module(c)
        c.add_param(chunk.Slider("size", default_value=1.0, min_value=0.0, max_value=10.0))
        scene.add_chunk(c)
        param = c.get_param("size")
        before = chunk_scheduler.shape_version()

        param.max_value = 20.0  # its first change: a new attribute sender joins the param

        assert chunk_scheduler.shape_version() == before


def test_a_wire_still_changes_the_shape():
    with Scene() as scene:
        with chunk.change_block():
            box = scene.create.cube(prim_path="/World/box")
            moved = scene.create.transform(primpath_location="/World/box", tx=1.0)
        before = chunk_scheduler.shape_version()

        scene.connect(box, moved)

        assert chunk_scheduler.shape_version() > before
