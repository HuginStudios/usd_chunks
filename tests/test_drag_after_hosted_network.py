"""Dragging a prim a network-hosting Reference placed wires the new Transform after it.

A Reference pointed at a saved network turns CONTEXT in place and has no _parent_context,
so the tail lookup skipped it: the chunk it consumes looked like the end of the chain, and
the auto-created Transform was wired in above the Reference.
"""

from __future__ import annotations

from graph_fixtures import make_network
from usd_chunks import Scene
from usd_chunks.chunk_stage import exec_input_connections, get_chunk_prim_path
from usd_chunks.gizmo import runtime


def test_auto_transform_follows_the_hosting_reference(tmp_path) -> None:
    network, _ = make_network(tmp_path / "seq.usda")
    with Scene() as scene:
        up = scene.create.prim(primpath_location="/stuff", prim_type="Xform")
        ref = scene.create.reference(up, file_path=network, primpath_location="/hosted")
        scene.commit()
        stage = ref.get_stage()

        assert runtime.ensure_transform_chunk(stage.GetPrimAtPath("/hosted")) is not None
        scene.commit()

        group = stage.GetPrimAtPath(get_chunk_prim_path(ref)).GetParent()
        wired = {p.GetName(): [str(c.GetPrimPath()) for c in exec_input_connections(p)]
                 for p in group.GetChildren()}
        new = [name for name in wired if name not in (up.name, ref.name) and not name.startswith("View")]
        assert len(new) == 1, wired
        assert wired[new[0]] == [get_chunk_prim_path(ref)], wired
