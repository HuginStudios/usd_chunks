"""A Payload or Reference does not listen to every change of the stage for its Layer menu.

Its menu lists the arc layers, and the layer manager lists them again in every such menu
when they change. Heard on every stage change instead, a set of 300 References spent about
a third of moving one prop calling their menus; tests/test_arc_layers.py checks the menus
still follow the layers.
"""

from __future__ import annotations

from usd_chunks import Scene, chunk_notice
from usd_chunks.chunk_notice import ChunkNoticeAlias


def _stage_listeners() -> int:
    dispatch = chunk_notice._global_dispatches.get(ChunkNoticeAlias.STAGE_CONTENTS_CHANGED)
    return len(dispatch.subscribers) if dispatch else 0


def test_more_references_and_payloads_add_no_stage_listener() -> None:
    with Scene() as scene:
        scene.create.reference(primpath_location="/World/a")
        scene.create.payload(primpath_location="/World/b")
        before = _stage_listeners()
        for i in range(5):
            scene.create.reference(primpath_location=f"/World/r{i}")
            scene.create.payload(primpath_location=f"/World/p{i}")
        assert _stage_listeners() == before
