"""A wire a script removes with remove_dependency stays out of the saved file.

`sun.remove_dependency(merge)` dropped the wire in the session, but the saved file still
had it: only adding a dependency authored the USD connection.
"""
from __future__ import annotations

import pytest
from pxr import Usd

from usd_chunks import Scene
from usd_chunks.chunk_naming import iter_exec_inputs


def _sources(path, name):
    """Names of the chunks wired into `name`'s exec inputs in the saved file, slot order."""
    stage = Usd.Stage.Open(str(path))
    prim = next(p for p in stage.Traverse() if p.GetName() == name and p.HasAttribute("inputs:chunk"))
    return [c.GetPrimPath().name for a in iter_exec_inputs(prim) for c in a.GetConnections()]


def _get(scene, name):
    return next(c for c in scene.get_chunks() if c.name == name)


@pytest.mark.parametrize("lazy", [False, True])
def test_a_removed_wire_is_not_saved(tmp_path, lazy):
    first, second = tmp_path / "first.usda", tmp_path / "second.usda"
    with Scene(host_name="pytest") as scene:
        a = scene.create.transform(name="A")
        scene.create.transform(a, name="B")
        scene.save(str(first))
    assert _sources(first, "B") == ["A"]

    with Scene.open(str(first), lazy=lazy) as scene:
        _get(scene, "B").remove_dependency(_get(scene, "A"))
        scene.save(str(second))
    assert _sources(second, "B") == []


def test_removing_a_merge_branch_keeps_the_others_in_order(tmp_path):
    first, second = tmp_path / "first.usda", tmp_path / "second.usda"
    with Scene(host_name="pytest") as scene:
        a, b, c = (scene.create.transform(name=n) for n in "abc")
        scene.create.merge(a, b, c, name="merge")
        scene.save(str(first))
    assert _sources(first, "merge") == ["a", "b", "c"]

    with Scene.open(str(first)) as scene:
        _get(scene, "merge").remove_dependency(_get(scene, "b"))
        scene.save(str(second))
    assert _sources(second, "merge") == ["a", "c"]
