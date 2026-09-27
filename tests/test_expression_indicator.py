"""A param whose value comes from somewhere else says so.

A field holding `@Node.param@` shows the resolved value, because that is what the value IS
-- so on screen it is indistinguishable from a number somebody typed in, and the text
behind it is nowhere. An asset path holding `${SHOT}` is the same story with USD doing the
resolving. Either way the row's label is marked and carries the text as its tooltip.
"""

from __future__ import annotations

from pxr import Usd

from usd_chunks import chunk, chunk_registry, chunk_stage, chunk_utils
from usd_chunks.chunk_core import SceneSender
from usd_chunks.frontends.qt.widgets import ChunkWidget


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
    from usd_chunks.frontends.qt.widgets import ChunkParamWidgetBase, LabeledWidget

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


def test_a_param_wire_marks_its_label_and_shows_the_text(qapp) -> None:
    _, scene = _scene()
    src, src_param = _chunk_with_slider(scene, 12.5)
    src.name = "src"
    src_param.name = "intensity"
    dst, slider = _chunk_with_slider(scene, "@src.intensity@")

    label = _label_for(ChunkWidget(dst), slider)

    assert label.property("chunkExpression"), "the label must say the value is driven"
    assert label.toolTip() == "@src.intensity@"


def test_an_asset_path_expression_marks_its_label(qapp) -> None:
    """USD resolves ${ASSET} in an asset path during composition, so the field shows one
    thing and holds another -- the one place a stage expression legitimately lives."""
    stage, scene = _scene()
    chunk_utils.set_stage_expression_variables(stage, {"ASSET": "hero"})
    c = chunk.Chunk("Reference")
    path = chunk.FileBrowser("file_path", "File", default_value="",
                             flags=chunk.ChunkFlags.ParamFlags.LABELED)
    c.add_param(path)
    scene.add_chunk(c)
    path.value = '`"./${ASSET}.usda"`'

    label = _label_for(ChunkWidget(c), path)

    assert label.property("chunkExpression")
    assert label.toolTip() == '`"./${ASSET}.usda"`'
