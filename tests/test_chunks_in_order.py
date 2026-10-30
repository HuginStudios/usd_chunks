"""scene.chunks_in_order(): the chunks in the order a script made them."""
from __future__ import annotations

from usd_chunks import Scene


def test_chunks_come_back_in_the_order_they_were_made() -> None:
    """Live-reported: a script laying nodes out from scene.get_chunks() got its row backwards,
    since that lists the newest chunk first."""
    with Scene() as scene:
        a = scene.create.prim(primpath_location="/a", name="a")
        b = scene.create.transform(a, primpath_location="/a", name="b")
        c = scene.create.transform(b, primpath_location="/a", name="c")
        assert [x.name for x in scene.chunks_in_order()] == [a.name, b.name, c.name]
        assert set(map(id, scene.chunks_in_order())) == set(map(id, scene.get_chunks()))


def test_a_group_comes_before_what_is_inside_it() -> None:
    with Scene() as scene:
        first = scene.create.prim(primpath_location="/first", name="first")
        with scene.create.chunkgroup(first, name="group") as group:
            inner = scene.create.prim(primpath_location="/inner", name="inner")
        names = [x.name for x in scene.chunks_in_order()]
        assert names.index(first.name) < names.index(group.name) < names.index(inner.name)
