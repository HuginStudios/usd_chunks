"""Core lifecycle and guard rails no other test reached.

- the scene: removing a chunk, clearing, the stage-replaced hook,
- the dependency graph's downstream queries (get_dependent / get_dependents),
- list rows built from a grouped template following the template as it changes,
- the errors a chunk author gets for a bad name, a bad index or a failed predicate.
"""
from __future__ import annotations

import pytest
from pxr import Usd

from usd_chunks import chunk, chunk_registry, chunk_stage
from usd_chunks.chunk_core import ChunkParamBase
from usd_chunks.chunk_scene import SceneSender

from chunk_stubs import attach_stub_module


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture()
def scene():
    stage = Usd.Stage.CreateInMemory()
    scene = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_stage.ChunkStageSync().initialize(stage, scene)
    return scene


def _add(scene, name):
    c = chunk.Chunk(name)
    attach_stub_module(c)
    c.add_param(chunk.Slider("p", default_value=0.0))
    scene.add_chunk(c)
    return c


# -- the scene --------------------------------------------------------------------------------

def test_remove_chunk_by_name_deletes_it_and_marks_the_scene_modified(scene):
    c = _add(scene, "gone")
    scene._modified = False

    scene.remove_chunk("gone")

    assert scene.get_chunk("gone") is None
    assert c not in scene.get_chunks()
    assert scene.is_modified()


def test_removing_an_unknown_chunk_changes_nothing(scene):
    _add(scene, "kept")
    scene._modified = False

    scene.remove_chunk("nobody")

    assert scene.get_chunk("kept") is not None
    assert not scene.is_modified()


@pytest.mark.xfail(strict=True, reason=(
    "SceneSender.clear() clears only the ChainMap's first map: get_chunks() empties but "
    "get_chunk(name) still finds the old chunk and its prim stays, so reload_modules "
    "recreates every chunk under a new name (X -> X1). Fix pending a decision on what "
    "Reload Chunks should keep."))
def test_clear_empties_the_scene(scene):
    _add(scene, "a")
    _add(scene, "b")

    scene.clear()

    assert scene.get_chunk("a") is None and scene.get_chunk("b") is None


def test_stage_replaced_listeners_hear_the_new_stage(scene):
    heard = []
    scene.add_stage_replaced_listener(heard.append)
    new_stage = Usd.Stage.CreateInMemory()

    scene.notify_stage_replaced(new_stage)

    assert heard == [new_stage]


# -- downstream queries -----------------------------------------------------------------------

def test_get_dependent_and_dependents_walk_downstream():
    a, b = chunk.Slider("a"), chunk.Slider("b")
    b.add_dependency(a)

    assert a.get_dependent(0) is b
    assert [p for _, p in a.get_dependents()] == [b]
    assert b.get_dependency(0) is a


def test_a_missing_dependent_is_none():
    assert chunk.Slider("lonely").get_dependent(3) is None
    assert chunk.Slider("lonely").get_dependency(3) is None


def test_get_dependents_filters_by_predicate():
    a, b, c = chunk.Slider("a"), chunk.Slider("b"), chunk.LineEdit("c")
    b.add_dependency(a)
    c.add_dependency(a)

    lines = a.get_dependents(lambda p: isinstance(p, chunk.LineEdit))

    assert [p for _, p in lines] == [c]


def test_a_dependent_that_fails_the_predicate_raises():
    a, b = chunk.Slider("a"), chunk.Slider("b")
    b.add_dependency(a)

    with pytest.raises(ValueError):
        a.get_dependent(0, lambda p: isinstance(p, chunk.LineEdit))


# -- list rows of a grouped template ----------------------------------------------------------

@pytest.fixture()
def grouped_list(scene):
    c = chunk.Chunk("lister")
    attach_stub_module(c)
    rows = chunk.ListGroup("rows")
    template = chunk.Group("entry")
    template.add_param(chunk.Slider("x", default_value=1.0))
    rows.add_param(template)
    c.add_param(rows)
    scene.add_chunk(c)
    rows.size = 2
    return rows, template


def _row_names(rows, i):
    row = rows.get_indexed_param("entry", i)
    return sorted(p.name for p in row.get_my_params())


def test_each_row_of_a_grouped_template_has_its_params(grouped_list):
    rows, _ = grouped_list

    assert _row_names(rows, 0) == ["x"]
    assert _row_names(rows, 1) == ["x"]


def test_a_param_added_to_the_template_appears_in_every_row(grouped_list):
    rows, template = grouped_list

    template.add_param(chunk.Slider("y", default_value=2.0))

    assert _row_names(rows, 0) == ["x", "y"]
    assert _row_names(rows, 1) == ["x", "y"]


def test_a_param_removed_from_the_template_leaves_every_row(grouped_list):
    rows, template = grouped_list
    y = chunk.Slider("y", default_value=2.0)
    template.add_param(y)

    y.request_delete()

    assert _row_names(rows, 0) == ["x"]
    assert _row_names(rows, 1) == ["x"]


def test_rows_of_a_grouped_template_hold_their_own_values(grouped_list):
    rows, _ = grouped_list
    first = rows.get_indexed_param("entry", 0).get_param("x")
    second = rows.get_indexed_param("entry", 1).get_param("x")

    first.value = 5.0

    assert first.value == pytest.approx(5.0)
    assert second.value == pytest.approx(1.0)


# -- guard rails -------------------------------------------------------------------------------

@pytest.mark.parametrize("given, kept", [("has space", "hasspace"), ("dash-ed", "dashed")])
def test_a_name_is_cleaned_to_what_usd_accepts(given, kept):
    assert chunk.Slider(given).name == kept
    renamed = chunk.Slider("ok")
    renamed.name = given
    assert renamed.name == kept


def test_a_name_that_cannot_be_cleaned_is_refused():
    with pytest.raises(ValueError):
        chunk.Slider("ümlaut")


def test_get_param_with_a_failing_predicate_raises():
    c = chunk.Chunk("c")
    c.add_param(chunk.Slider("p"))

    with pytest.raises(ValueError):
        c.get_param("p", lambda p: isinstance(p, chunk.LineEdit))


def test_get_param_with_a_passing_predicate_returns_it():
    c = chunk.Chunk("c")
    p = c.add_param(chunk.Slider("p"))

    assert c.get_param("p", lambda x: isinstance(x, ChunkParamBase)) is p


def test_an_indexed_param_past_the_end_raises(grouped_list):
    rows, _ = grouped_list

    with pytest.raises(IndexError):
        rows.get_indexed_param("entry", 5)


def test_removing_a_row_past_the_end_is_ignored(grouped_list):
    rows, _ = grouped_list

    rows.remove_index(9)

    assert rows.size == 2


def test_check_hierarchy_accepts_a_sound_chunk():
    c = chunk.Chunk("c")
    g = c.add_param(chunk.Group("g"))
    g.add_param(chunk.Slider("p"))
    c.add_param(chunk.Slider("p2"))

    c.check_hierarchy()
