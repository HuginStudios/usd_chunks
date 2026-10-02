"""A Label's value wins over its placeholder label, whenever the widget is built.

Reported on the Physics Drive chunk: its readout said "no target yet" while the drive was
plainly working. The chunk cooks during scene load, before its widget exists; the widget's
setup then wrote the value and, straight after, the label over it -- and a Label shows one
string, so the placeholder won and nothing repainted it later.
"""

from __future__ import annotations

from pxr import Usd

from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import initialize as _chunk_usd_init
from usd_chunks.frontends.qt.widgets import ChunkLabelWidget
from PySide6 import QtWidgets


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


def _label(qapp, value):
    stage = Usd.Stage.CreateInMemory()
    scene = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    _chunk_usd_init(stage, scene)
    c = chunk.Chunk("Readout")
    readout = chunk.Label("readout", "no target yet", default_value="no target yet")
    c.add_param(readout)
    scene.add_chunk(c)
    if value is not None:
        readout.value = value
    return ChunkLabelWidget(readout), readout


def test_a_value_set_before_the_widget_existed_is_what_it_shows(qapp):
    widget, _ = _label(qapp, "Mantle: spins 90 rpm about Y")
    assert widget.text() == "Mantle: spins 90 rpm about Y"


def test_an_untouched_label_still_shows_its_placeholder(qapp):
    widget, _ = _label(qapp, None)
    assert widget.text() == "no target yet"


def test_a_later_value_still_updates_it(qapp):
    widget, readout = _label(qapp, None)
    readout.value = "cooked"
    qapp.processEvents()
    assert widget.text() == "cooked"


def test_a_long_readout_wraps_instead_of_being_cut_off(qapp):
    """A solver's error named a file, and only "simulation failed: C:" and the first folder or
    two showed; the rest was cut off at the panel edge."""
    message = r"simulation failed: C:\Users\artist\Desktop\usdbundle_26.05\Python\python.exe: No module named x"
    widget, _ = _label(qapp, message)
    assert widget.wordWrap()
    line = widget.fontMetrics().height()
    assert widget.heightForWidth(160) >= 3 * line, "the path breaks across lines, not just at spaces"
    assert widget.get_value() == message, "what the widget reports is the exact text"


def test_the_panel_keeps_asking_a_wrapped_label_its_height(qapp):
    """The panel sets every param widget's size policy fresh; a wrapped label's height for its
    width has to survive that, or the layout never makes the row taller."""
    widget, _ = _label(qapp, "a long readout " * 20)
    widget.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Minimum)
    assert widget.sizePolicy().hasHeightForWidth()
