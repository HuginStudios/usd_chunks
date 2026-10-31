"""Wiring a chunk in cooks it and what is below it: not the chunk above, nor that chunk's other branches.

The chunk above used to be marked dirty by the new wire, so the next change anywhere connected
-- a script's next param -- cooked it and every branch below it again. Building a network by
script cooked it over and over that way.
"""
from __future__ import annotations

from usd_chunks import Scene, chunk_cook


def _count_cooks(monkeypatch) -> list[str]:
    cooked: list[str] = []
    cook = chunk_cook.cook

    def counting(chunk, *args, **kwargs):
        cooked.append(chunk.name)
        return cook(chunk, *args, **kwargs)

    monkeypatch.setattr(chunk_cook, "cook", counting)
    return cooked


def test_wiring_a_chunk_below_cooks_neither_the_chunk_above_nor_its_other_branch(monkeypatch) -> None:
    with Scene() as scene:
        ball = scene.create.prim(primpath_location="/World/ball", prim_type="Sphere", name="ball")
        scene.create.property_editor(ball, prim="/World/ball", edits="double radius = 2", name="other")
        cooked = _count_cooks(monkeypatch)

        new = scene.create.property_editor(ball, prim="/World/ball", edits="double radius = 3", name="new")
        new.get_param("edits").value = "double radius = 4"

        assert "new" in cooked
        assert "ball" not in cooked and "other" not in cooked, cooked


def test_a_transform_still_cooks_when_one_is_wired_below_it(monkeypatch) -> None:
    """Its op order names every Transform below it on the prim (cooks_when_wired_below)."""
    with Scene() as scene:
        scene.create.prim(primpath_location="/World/ball", prim_type="Sphere", name="ball")
        placed = scene.create.transform(primpath_location="/World/ball", ty=5.0, name="placed")
        cooked = _count_cooks(monkeypatch)

        nudge = scene.create.transform(placed, primpath_location="/World/ball", ty=2.0, name="nudge")
        nudge.get_param("ty").value = 3.0

        assert "placed" in cooked and "nudge" in cooked, cooked
