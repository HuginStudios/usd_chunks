"""A wired param's widget is locked; only a text field may be typed over its wire.

Dragging a slider that a wire drives replaced the wire with the dragged number, so any
hand that landed on it won. A text field is different: its expression is what it shows and
edits, which is how a path is built on a wire (@Box.primpath_location@/lid).
"""

from __future__ import annotations

import pytest
from pxr import Usd

from usd_chunks import chunk, chunk_registry, chunk_stage, chunk_utils
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.frontends.qt.widgets import ChunkWidget, ChunkParamWidgetBase, LabeledWidget


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture()
def scene(qapp):
    stage = Usd.Stage.CreateInMemory()
    scene = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_stage.initialize(stage, scene)
    src = chunk.Chunk("src")
    src.add_param(chunk.Slider("level", default_value=2.0))
    src.add_param(chunk.LineEdit("where", default_value="/geo"))
    src.add_param(chunk.Checkbox("on", default_value=True))
    scene.add_chunk(src)
    return scene


def _dst(scene, *params):
    c = chunk.Chunk("dst")
    c.add_params(list(params))
    scene.add_chunk(c)
    return c, ChunkWidget(c)


def _widget_for(built, param):
    for row in built.findChildren(LabeledWidget):
        if isinstance(row.widget, ChunkParamWidgetBase) and row.widget.param is param:
            return row.widget
    raise AssertionError("no row found for the param")


def test_a_wired_slider_is_locked(scene):
    slider = chunk.Slider("amount", default_value=0.0)
    _, built = _dst(scene, slider)
    widget = _widget_for(built, slider)
    assert widget.isEnabled()

    slider.value = "@src.level@"
    assert not widget.isEnabled(), "a hand on the slider would replace the wire"

    slider.value = slider.default_value
    assert widget.isEnabled(), "pulling the wire must free the slider"


def test_a_slider_built_already_wired_starts_locked(scene):
    slider = chunk.Slider("amount", default_value=0.0)
    slider.value = "@src.level@"
    _, built = _dst(scene, slider)
    assert not _widget_for(built, slider).isEnabled()


def test_a_wired_checkbox_is_locked(scene):
    box = chunk.Checkbox("enabled", default_value=False)
    _, built = _dst(scene, box)
    box.value = "@src.on@"
    assert not _widget_for(built, box).isEnabled()


def test_a_wired_line_edit_stays_editable(scene):
    line = chunk.LineEdit("target", default_value="")
    _, built = _dst(scene, line)
    line.value = "@src.where@/lid"
    assert _widget_for(built, line).isEnabled()
    assert line.value == "/geo/lid"


def test_moving_a_locked_widget_does_not_replace_the_wire(scene):
    slider = chunk.Slider("amount", default_value=0.0)
    _, built = _dst(scene, slider)
    slider.value = "@src.level@"
    # A frontend that left the widget live would still hand its value over.
    _widget_for(built, slider).value_changed(9.0)
    assert slider.raw_value == "@src.level@"
    assert slider.value == 2.0


def test_disabled_state_and_the_wire_lock_combine(scene):
    slider = chunk.Slider("amount", default_value=0.0)
    _, built = _dst(scene, slider)
    widget = _widget_for(built, slider)
    slider.value = "@src.level@"
    # Re-applying state (here: enabling the param) must not unlock a wired field.
    slider.unset_state(chunk.ChunkFlags.State.DISABLED)
    widget.update_from_state()
    assert not widget.isEnabled()


@pytest.mark.parametrize("make, raw, locked", [
    (lambda: chunk.Slider("s", default_value=0.0), "@a.b@", True),
    (lambda: chunk.Slider("s", default_value=0.0), 3.0, False),
    (lambda: chunk.Checkbox("c", default_value=False), "@a.b@", True),
    (lambda: chunk.LineEdit("l", default_value=""), "@a.b@/x", False),
    (lambda: chunk.FileBrowser("f", default_value=""), "@a.b@", False),
])
def test_is_wire_locked(make, raw, locked):
    param = make()
    param.__dict__["value"] = raw
    assert chunk_utils.is_wire_locked(param) is locked
