"""Scrolling over a slider does not change its value.

The number field beside a slider, and the pair as a whole, already let the wheel go: it
scrolls the panel, or zooms the node graph. The slider itself took it, so scrolling the graph
with the pointer over a node's Keep % or Triangle Budget changed the value, and the chunk
cooked again.
"""

from __future__ import annotations

from chunk_stubs import register_stub_module
from PySide6 import QtCore, QtGui, QtWidgets

from usd_chunks import Scene, chunk
from usd_chunks.frontends.qt.widgets import ChunkSliderWidget


def _wheel(widget, steps=3) -> QtGui.QWheelEvent:
    at = QtCore.QPointF(widget.rect().center())
    event = QtGui.QWheelEvent(
        at, QtCore.QPointF(widget.mapToGlobal(at.toPoint())), QtCore.QPoint(0, 0), QtCore.QPoint(0, 120 * steps),
        QtCore.Qt.MouseButton.NoButton, QtCore.Qt.KeyboardModifier.NoModifier, QtCore.Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QtWidgets.QApplication.sendEvent(widget, event)
    return event


def test_scrolling_over_a_slider_leaves_its_value_and_passes_the_wheel_on(qapp):
    def main():
        c = chunk.Chunk("Wheel")
        c.add_param(chunk.Slider("keep", min_value=0.0, max_value=100.0, default_value=50.0))
        return c

    with Scene() as scene:
        (c,) = register_stub_module(main=main).load()
        scene.add_chunk(c)
        widget = ChunkSliderWidget(c.get_param("keep"))
        widget.resize(300, 24)
        widget.show()

        event = _wheel(widget.slider)

        assert c.get_param("keep").value == 50.0, "the wheel changed the value"
        assert not event.isAccepted(), "the slider kept the wheel, so the graph could not zoom"
        widget.deleteLater()
