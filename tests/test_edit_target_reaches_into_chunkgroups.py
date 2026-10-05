"""Chunks inside a chunkgroup author into the Edit Target the group sits behind.

The first chunk in a group has no exec input of its own, so the walk upstream for the
nearest Edit Target stopped at the group's edge: everything in the group landed in the
session layer, and an export of the Edit Target's layer left the group's work out.
"""

from __future__ import annotations

from pxr import Sdf

from usd_chunks import Scene, chunk_utils
from usd_chunks.chunk_stage import exec_input_connections, get_chunk_prim_path


def _target_layer(stage, name):
    return next(layer for layer in stage.GetLayerStack(includeSessionLayers=True) if layer.GetDisplayName() == name)


def test_a_group_behind_an_edit_target_authors_into_its_layer() -> None:
    with Scene() as scene:
        layer = scene.create.layer(layer_name="proxy")
        with scene.create.edit_target(layer, edit_target="proxy"):
            mesh = scene.create.cube(prim_path="/ball/proxy", name="proxy_mesh")
            group = scene.create.chunkgroup(mesh, name="make_proxy")
            with group:
                first = scene.create.set_display_color(prim_path="/ball/proxy", color_r=0.8, name="color")
                second = scene.create.set_metadata(first, prim_path="/ball/proxy", metadata_key="purpose",
                                                   metadata_value="proxy", name="purpose")
        stage = scene.get_stage()
        proxy_layer = _target_layer(stage, "proxy")
        for inner in (first, second):
            assert chunk_utils.get_edit_layer(stage, inner) == proxy_layer


def test_a_group_with_no_edit_target_still_uses_the_session_layer() -> None:
    with Scene() as scene:
        group = scene.create.chunkgroup(name="props")
        with group:
            crate = scene.create.cube(prim_path="/props/crate", name="crate_mesh")
        stage = scene.get_stage()
        assert chunk_utils.get_edit_layer(stage, crate) == stage.GetSessionLayer()
        assert isinstance(stage.GetSessionLayer(), Sdf.Layer)


def test_a_scripted_group_gets_its_exec_input() -> None:
    """scene.create.chunkgroup(upstream) wires upstream into the group, as for any chunk."""
    with Scene() as scene:
        mesh = scene.create.cube(prim_path="/ball/proxy", name="proxy_mesh")
        group = scene.create.chunkgroup(mesh, name="make_proxy")
        prim = scene.get_stage().GetPrimAtPath(get_chunk_prim_path(group))
        assert [str(c.GetPrimPath()) for c in exec_input_connections(prim)] == [get_chunk_prim_path(mesh)]
