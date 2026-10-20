"""A chunk whose cook waits on a long job takes a slider's value when the drag ends.

Each move of a drag set the value, and each one started a whole remesh behind the progress
window: one drag of Remesh Proxy's Thickness started eleven. A module that declares
cook_on_release gets sliders that set their value once, on release, with the number beside
the slider following the drag meanwhile. Any other chunk's sliders still set it live.
"""

from __future__ import annotations

import attrs
from chunk_stubs import register_stub_module
from PySide6 import QtCore, QtGui, QtWidgets

from usd_chunks import Scene, chunk
from usd_chunks.frontends.qt.widgets import ChunkSliderWidget


def _chunk(scene, on_release: bool):
    def main():
        c = chunk.Chunk("Heavy" if on_release else "Light")
        c.add_param(chunk.Slider("size", min_value=0.0, max_value=10.0, default_value=1.0))
        return c

    module = register_stub_module(main=main)
    module.config = attrs.evolve(module.config, cook_on_release=on_release)
    (c,) = module.load()
    scene.add_chunk(c)
    return c


def _drag(widget, steps=8, ctrl=False):
    """Press on the slider's handle, move right in steps, release; the values set meanwhile."""
    slider = widget.slider
    option = QtWidgets.QStyleOptionSlider()
    slider.initStyleOption(option)
    at = slider.style().subControlRect(
        QtWidgets.QStyle.ComplexControl.CC_Slider, option, QtWidgets.QStyle.SubControl.SC_SliderHandle, slider
    ).center()
    left = QtCore.Qt.MouseButton.LeftButton
    mods = QtCore.Qt.KeyboardModifier.ControlModifier if ctrl else QtCore.Qt.KeyboardModifier.NoModifier
    seen = []

    def send(kind, pos, button, buttons):
        p = QtCore.QPointF(pos)
        QtWidgets.QApplication.sendEvent(slider, QtGui.QMouseEvent(kind, p, p, button, buttons, mods))

    send(QtCore.QEvent.Type.MouseButtonPress, at, left, left)
    texts = []
    for i in range(1, steps + 1):
        send(QtCore.QEvent.Type.MouseMove, at + QtCore.QPoint(6 * i, 0), QtCore.Qt.MouseButton.NoButton, left)
        seen.append(widget.param.value)
        texts.append(widget.spin_box.text())
    send(QtCore.QEvent.Type.MouseButtonRelease, at + QtCore.QPoint(6 * steps, 0), left, QtCore.Qt.MouseButton.NoButton)
    return seen, texts


def _widget(c):
    widget = ChunkSliderWidget(c.get_param("size"))
    widget.resize(300, 24)
    widget.show()
    return widget


def test_a_drag_on_a_cook_on_release_chunk_sets_the_value_once_when_it_ends(qapp):
    with Scene() as scene:
        c = _chunk(scene, on_release=True)
        widget = _widget(c)
        before = c.get_param("size").value
        sets = []
        c.get_param("size").add_callback(lambda *a: sets.append(c.get_param("size").value))

        during, texts = _drag(widget)

        assert during == [before] * len(during), "the value was set during the drag"
        assert len(set(texts)) > 1, "the number did not follow the drag"
        assert c.get_param("size").value > before
        assert c.get_param("size").value == float(texts[-1])
        assert len(sets) == 1, f"set {len(sets)} times"
        widget.deleteLater()


def test_a_ctrl_drag_also_sets_it_once(qapp):
    with Scene() as scene:
        c = _chunk(scene, on_release=True)
        widget = _widget(c)
        before = c.get_param("size").value

        during, _texts = _drag(widget, ctrl=True)

        assert during == [before] * len(during)
        assert c.get_param("size").value > before
        widget.deleteLater()


def test_any_other_chunk_still_follows_the_drag(qapp):
    with Scene() as scene:
        c = _chunk(scene, on_release=False)
        widget = _widget(c)

        during, _texts = _drag(widget)

        assert len(set(during)) > 1, "a light chunk stopped updating while dragging"
        widget.deleteLater()
