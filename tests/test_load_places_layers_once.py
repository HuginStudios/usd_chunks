"""Opening a network puts each chunk's layer on the stage once, in its final graph layer.

A load restores a chunk's inputs before it restores the wiring between chunks, and
restoring an input asks for the chunk's layer. Placed there and then, every chunk looked
like a graph of its own: it got a graph layer of its own on the stage, and the wiring
moved it into the shared one a moment later and threw that graph layer away. Each of
those sublayer edits recomposes the stage -- a fifth of opening a 70-chunk network.
"""

from __future__ import annotations

from pxr import Usd

from usd_chunks import Scene, chunk, chunk_registry


def _save_chain(path):
    with Scene() as scene:
        wedge = scene.create.wedge(seed=3, count=4, ranges="x 1 9")
        upstream = wedge
        for i in range(4):
            upstream = scene.create.transform(upstream, primpath_location=f"/World/box{i}",
                                              tx="@Wedge.x@")
        scene.save(str(path))


def test_open_makes_no_throwaway_graph_layers(tmp_path, monkeypatch):
    path = tmp_path / "chain.usda"
    _save_chain(path)

    manager = chunk_registry.ChunkLayerManager
    seen = set()
    real = manager.get_or_create_layer

    def spying(self, c):
        layer = real(self, c)
        seen.update(g.identifier for g in self.graph_layers.values())
        return layer

    monkeypatch.setattr(manager, "get_or_create_layer", spying)

    with Scene.open(str(path)) as scene:
        lm = manager()
        kept = {g.identifier for g in lm.graph_layers.values()}
        assert seen <= kept, f"{len(seen - kept)} graph layers made and thrown away"

        placed = {sub for g in lm.graph_layers.values() for sub in g.subLayerPaths}
        for c in scene.get_chunks():
            if layer := lm.get_layer(c):
                assert layer.identifier in placed, f"{c.name}'s layer is not on the stage"


def test_a_held_layer_can_be_authored_into():
    """A held layer behaves as a placed one: anything that authors into it mid-load lands
    in it. Kept off the stage, as the first cut of this did, Usd.EditContext raises for it
    ("not in the local LayerStack"), and a chunk whose layer is not in the stack is not
    cooked at all -- which quietly changed what a load produced."""
    with Scene() as scene:
        stage = scene.get_stage()
        lm = chunk_registry.ChunkLayerManager()
        held = chunk.Chunk("Held")  # no layer yet, like a chunk mid-load

        assert lm.defer_placement()
        try:
            layer = lm.get_or_create_layer(held)
            with Usd.EditContext(stage, layer):
                stage.OverridePrim("/World/held")
            assert layer.GetPrimAtPath("/World/held"), "authored somewhere other than its layer"
        finally:
            lm.place_deferred_layers()

        placed = {sub for g in lm.graph_layers.values() for sub in g.subLayerPaths}
        assert layer.identifier in placed
        assert not any("loading" in sub for sub in stage.GetSessionLayer().subLayerPaths)
