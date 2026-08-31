"""Pinning a viewport selection into a ListGroup row.

Reported live on Scatter Points: with a prim selected in the hierarchy, the pin icon on a
prototype row wrote nothing. Typing the same path into the same field worked, which is what
made it puzzling.

The cause is one level below the chunk. A row's param is a `ProxyParam`: assigning `.value`
is proxied to that row, but a *method* looked up on the proxy comes back bound to the shared
template, so `set_prims()` -- which does `self.value = ...` internally -- updated the
template's default and left every row untouched.

These tests pin the way the widget does and assert the row moved and the template did not.
Driving the Qt widget itself would need a QApplication; the defect is entirely in which object
the write lands on, so it is reproduced at that level.
"""
from __future__ import annotations

from pxr import Usd, UsdGeom

from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_core import SceneSender


def _scene_with(*paths, selected=()):
    stage = Usd.Stage.CreateInMemory()
    for path in paths:
        UsdGeom.Cube.Define(stage, path)
    chosen = [stage.GetPrimAtPath(p) for p in selected]

    class _Scene(SceneSender):
        def get_stage(self):
            return stage

        def get_selected_prims(self):
            return chosen

    chunk_registry.ChunkRegistry().initialize(_Scene(), "pytest")
    return stage, _Scene()


def _rows_of(size=2):
    """A two-row list of prim-selection fields, standing in for Scatter's Prototypes."""
    rows = chunk.ListGroup("rows", "Rows")
    rows.add_param(
        chunk.LineEdit(
            "path",
            "Prim",
            flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        )
    )
    holder = chunk.Chunk("Holder")
    holder.add_param(rows)
    rows.size = size
    template = rows.get_param("path")
    return rows, template, [rows.get_indexed_param(template, i) for i in range(size)]


def _pin(param, scene):
    """Exactly what ChunkLineEditWidget.pin_prim does, minus Qt."""
    prims = scene.get_selected_prims()
    if not prims:
        return
    param.value = ",".join(str(p.GetPath()) for p in prims)


# ---------------------------------------------------------------------------

def test_pinning_a_row_writes_that_row_and_not_the_template() -> None:
    """The reported bug: the pin landed on the shared template, so no row ever changed."""
    _stage, scene = _scene_with("/Cup", "/Plate", selected=["/Cup"])
    _rows, template, (row0, row1) = _rows_of()
    for param in (template, row0, row1):
        param.value = "/Plate"

    _pin(row1, scene)

    assert row1.value == "/Cup", "the pinned row did not take the selection"
    assert template.value == "/Plate", "the pin leaked into the shared template"
    assert row0.value == "/Plate", "the pin changed a row that was not clicked"


def test_pinning_nothing_leaves_the_path_alone() -> None:
    """Pinning with an empty selection used to blank the field.

    A bad trade -- the path already there was worth more than the empty selection, and it made
    the button feel unreliable: the same click either filled the row or wiped it, depending on
    something happening in another panel.
    """
    _stage, scene = _scene_with("/Cup", selected=[])
    _rows, _template, (row0, _row1) = _rows_of()
    row0.value = "/Cup"

    _pin(row0, scene)

    assert row0.value == "/Cup"


def test_each_row_keeps_its_own_pinned_path() -> None:
    """Two rows, two selections -- the case the prototype list exists for."""
    stage, _scene = _scene_with("/Cup", "/Plate")
    _rows, template, (row0, row1) = _rows_of()
    template.value = ""

    class _Sel(SceneSender):
        def __init__(self, path):
            self._prims = [stage.GetPrimAtPath(path)]

        def get_stage(self):
            return stage

        def get_selected_prims(self):
            return self._prims

    _pin(row0, _Sel("/Cup"))
    _pin(row1, _Sel("/Plate"))

    assert (row0.value, row1.value) == ("/Cup", "/Plate")
    assert template.value == ""


def test_set_prims_on_a_row_still_writes_the_template() -> None:
    """Characterisation, not approval.

    `pin_prim` assigns `.value` instead of calling `set_prims()` purely because of this. If
    ProxyParam ever rebinds methods onto the row, this test fails -- and that is the signal to
    go and simplify `pin_prim` back to the obvious one-liner, not to update this assertion.
    """
    _stage, scene = _scene_with("/Cup", "/Plate", selected=["/Cup"])
    _rows, template, (row0, _row1) = _rows_of()
    template.value = "/Plate"
    row0.value = "/Plate"

    row0.set_prims(scene.get_selected_prims())

    assert template.value == "/Cup", "ProxyParam now rebinds methods -- see the docstring"
    assert row0.value == "/Plate"
