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
