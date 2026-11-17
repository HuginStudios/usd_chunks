"""A param disabled before its panel is built shows greyed out.

A chunk that greys out sliders while they change nothing sets the state in its cook, often
before anyone opens its params. The panel built later showed those sliders live: no state
notice comes for a change made before the widget existed.
"""

from __future__ import annotations

import pytest
from pxr import Usd

from usd_chunks import chunk, chunk_registry, chunk_stage
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.frontends.qt.widgets import ChunkParamWidgetBase, ChunkWidget, LabeledWidget


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
    return scene


def _widget_for(built, param):
    for row in built.findChildren(LabeledWidget):
        if isinstance(row.widget, ChunkParamWidgetBase) and row.widget.param is param:
            return row.widget
    raise AssertionError("no row found for the param")


@pytest.mark.parametrize("make", [
    lambda: chunk.Slider("amount", default_value=1.0),
    lambda: chunk.Slider("count", default_value=1, is_int=True),
    lambda: chunk.Checkbox("on", default_value=True),
    lambda: chunk.Menu("kind", menu_items=["a", "b"], default_value="a"),
    lambda: chunk.LineEdit("where", default_value="/geo"),
])
def test_a_param_disabled_before_its_widget_is_built_shows_disabled(scene, make):
    node = chunk.Chunk("dst")
    param = make()
    node.add_param(param)
    scene.add_chunk(node)
    param.set_state(chunk.ChunkFlags.State.DISABLED)
    widget = _widget_for(ChunkWidget(node), param)
    assert not widget.isEnabled()
    # And it still follows the state once built.
    param.unset_state(chunk.ChunkFlags.State.DISABLED)
    assert widget.isEnabled()
