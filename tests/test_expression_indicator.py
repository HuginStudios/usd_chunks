"""A param driven by an expression says so, and follows what it reads.

A field holding `@Node.param@` or `${SHOT}` shows the resolved value, because that is what
the value IS -- so on screen it is indistinguishable from a number somebody typed in, and
the text behind it is nowhere. Its label is marked instead.

The second half is the one that bit in the kitchen scene: changing a Stage Expression
variable changes no param text, so nothing fired, and a slider kept showing the value from
the shot it was authored in while the viewport had already moved on.
"""

from __future__ import annotations

from pxr import Usd

from usd_chunks import chunk, chunk_registry, chunk_stage, chunk_utils
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins.chunk_qt_widgets import ChunkWidget


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


def _scene():
    stage = Usd.Stage.CreateInMemory()
    scene = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_stage.initialize(stage, scene)
    return stage, scene


def _chunk_with_slider(scene, value):
    c = chunk.Chunk("Light")
    slider = chunk.Slider("intensity", "Intensity", default_value=1.0,
                          flags=chunk.ChunkFlags.ParamFlags.LABELED)
    c.add_param(slider)
    scene.add_chunk(c)
    slider.value = value
    return c, slider


def _label_for(widget, param):
    """The LabeledWidget row's label for param, found in the built chunk widget."""
    from usd_chunks.plugins.chunk_qt_widgets import ChunkParamWidgetBase, LabeledWidget

    for row in widget.findChildren(LabeledWidget):
        inner = row.widget
        if isinstance(inner, ChunkParamWidgetBase) and inner.param is param:
            return row.label
    raise AssertionError("no row found for the param")


def test_a_plain_value_is_not_marked(qapp) -> None:
    _, scene = _scene()
    c, slider = _chunk_with_slider(scene, 20.0)

    label = _label_for(ChunkWidget(c), slider)

    assert not label.property("chunkExpression")
    assert label.toolTip() == ""


def test_a_stage_expression_marks_its_label_and_shows_the_text(qapp) -> None:
    stage, scene = _scene()
    chunk_utils.set_stage_expression_variables(stage, {"SHOT": "sh010_day"})
    c, slider = _chunk_with_slider(scene, "${SHOT}")

    label = _label_for(ChunkWidget(c), slider)

    assert label.property("chunkExpression"), "the label must say the value is driven"
    assert label.toolTip() == "${SHOT}", "and hovering it must show the text behind the value"


def test_the_widget_follows_a_variable_change(qapp) -> None:
    """Nothing fires on its own: the param's own text does not change when SHOT does."""
    stage, scene = _scene()
    chunk_utils.set_stage_expression_variables(stage, {"LEVEL": "20"})
    c, slider = _chunk_with_slider(scene, "${LEVEL}")
    widget = ChunkWidget(c)
    qapp.processEvents()
    param_widget = _label_for(widget, slider).parent().widget
    assert param_widget.get_value() == 20.0

    chunk_utils.set_stage_expression_variables(stage, {"LEVEL": "40"})
    chunk_stage.refresh_stage_expression_params(stage, scene)
    qapp.processEvents()

    assert slider.value == 40.0
    assert param_widget.get_value() == 40.0, "the slider still showed the old value"
