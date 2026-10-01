"""A cook that sets its own params finishes before anything below it cooks, and ends clean.

A module that writes its own params while it runs -- a Light showing its prim's attributes
as params, a Wedge writing a variant's ranges -- solves each write there and then. That
solve reached everything downstream of the chunk while the chunk was half way through its
cook, so those chunks cooked against a layer it had not finished, and cooked again when the
change that started it got to them. The params it created start out dirty, which left the
chunk dirty too, and the next edit anywhere below it walked up and cooked it again.
"""

from __future__ import annotations

import pytest
from chunk_stubs import register_stub_module

from usd_chunks import Scene, chunk, skip_cook


@pytest.fixture(autouse=True)
def _every_cook_runs(monkeypatch):
    # What is held and when it cooks, not whether: Down reads nothing Up writes, and with
    # skipping on it would rightly not cook at all.
    monkeypatch.delenv(skip_cook.ENV, raising=False)


def _add(scene, module):
    (c,) = module.load()
    scene.add_chunk(c)
    return c


def _writes_its_own_params(cooks: list, done: dict):
    """Up: sets a param of its own and creates one, the way Light's show_attribute does."""

    def main():
        c = chunk.Chunk("Up")
        c.add_param(chunk.Slider("size", default_value=1.0))
        c.add_param(chunk.Slider("count", default_value=0, is_int=True))
        return c

    def execute(node):
        cooks.append(node.name)
        done["up"] = False
        node.get_param("count").value = int(node.get_param("size").value * 10)
        shown = f"shown_{int(node.get_param('size').value)}"
        if node.get_param(shown) is None:
            node.add_param(chunk.Slider(shown, default_value=0.0))
        done["up"] = True

    return register_stub_module(main=main, execute=execute)


def _records(cooks: list, done: dict, seen: list):
    """Down: notes whether Up had finished when it cooked."""

    def main():
        c = chunk.Chunk("Down")
        c.add_param(chunk.Slider("gain", default_value=1.0))
        return c

    def execute(node):
        cooks.append(node.name)
        seen.append(done.get("up"))

    return register_stub_module(main=main, execute=execute)


def _chain(scene):
    cooks: list = []
    done: dict = {}
    seen: list = []
    up = _add(scene, _writes_its_own_params(cooks, done))
    down = _add(scene, _records(cooks, done, seen))
    down.add_dependency(up)
    return up, down, cooks, seen


def test_downstream_cooks_once_after_the_cook_that_changed_it():
    with Scene() as scene:
        up, down, cooks, seen = _chain(scene)
        up.get_param("size").value = 2.0  # creates a param, like the first cook after a load
        cooks.clear()
        seen.clear()

        up.get_param("size").value = 3.0

        assert cooks.count("Up") == 1
        assert cooks.count("Down") == 1, f"Down cooked {cooks.count('Down')} times"
        assert seen == [True], "Down cooked while Up was still cooking"


def test_a_chunk_that_just_cooked_is_not_cooked_by_an_edit_below_it():
    with Scene() as scene:
        up, down, cooks, _ = _chain(scene)
        up.get_param("size").value = 2.0
        assert not up.get_dag_node().dirty, "Up left dirty by its own cook"
        cooks.clear()

        down.get_param("gain").value = 2.0

        assert cooks == ["Down"]
