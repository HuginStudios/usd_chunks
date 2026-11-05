"""A chunk's params keep their names when another chunk in the scene shares one of them.

A Remesh Proxy's group is called "Output". In a scene that also held a chunk named Output,
adding the Remesh Proxy renamed its group to "Output1". The values were saved as
inputs:Output1:*, and reopening did not read them back: Set Up as Proxy and Bake Colour
came back at their defaults.
"""

from __future__ import annotations

from test_execute_written_params_are_saved import _add, _new_stage, _reopen, _scene_for


def test_a_group_named_like_another_chunk_keeps_its_name() -> None:
    stage = _new_stage()
    scene = _scene_for(stage)
    assert _add(scene, "output").name == "Output"
    remesh = _add(scene, "remesh_proxy")
    remesh.get_param("as_proxy").value = False

    prim = stage.GetPrimAtPath("/chunks/ChunkGroup/RemeshProxy")
    assert prim.GetAttribute("inputs:Output:as_proxy").Get() is False
    assert not prim.GetAttribute("inputs:Output1:as_proxy")


def test_its_values_survive_a_save_and_reopen(tmp_path) -> None:
    scene = _scene_for(_new_stage())
    _add(scene, "output")
    remesh = _add(scene, "remesh_proxy")
    remesh.get_param("as_proxy").value = False
    remesh.get_param("bake_colour").value = False

    _stage, chunks = _reopen(scene, "remesh_proxy", tmp_path)

    assert chunks[0].get_param("as_proxy").value is False
    assert chunks[0].get_param("bake_colour").value is False
