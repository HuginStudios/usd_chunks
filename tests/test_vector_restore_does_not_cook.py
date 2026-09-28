"""A vector restored during a load announces nothing, like every other param.

value_changed sends nothing while a load is restoring values, so a chunk cooks once, after
all of its inputs are back. A vector set all its children and then announced its value
itself, straight past that rule, so it cooked its chunk in the middle of the restore: a
camera in the car configurator restored its clipping range, cooked with the default
target, and wrote its lens onto /cameras/camera1 -- a prim none of the cameras targeted --
and gave its transform op a new id that no longer matched the saved one.
"""

from __future__ import annotations

import pytest

from usd_chunks import Scene, chunk, chunk_notice


@pytest.fixture
def vector():
    with Scene():
        c = chunk.Chunk("Lens")
        param = chunk.Vector("clippingRange", "Clipping Range", param=chunk.Slider(), length=2,
                             default_value=(1.0, 1000000.0))
        c.add_param(param)
        yield param


@pytest.fixture
def solves(monkeypatch):
    """Every solve started, by the name of what started it."""
    started = []
    real = chunk_notice.ChunkNoticePropagator._solve_downstream

    def counting(self):
        started.append(getattr(self, "name", None))
        return real(self)

    monkeypatch.setattr(chunk_notice.ChunkNoticePropagator, "_solve_downstream", counting)
    return started


def test_a_vector_set_while_loading_solves_nothing(vector, solves, monkeypatch):
    monkeypatch.setattr(chunk_notice, "_loading_stage", object())

    vector.value = (0.5, 5000.0)

    assert solves == []
    assert tuple(vector.value) == (0.5, 5000.0), "the value itself is still restored"


def test_a_vector_set_outside_a_load_still_solves(vector, solves):
    vector.value = (0.5, 5000.0)

    assert solves, "a live edit must still reach whatever depends on the vector"
