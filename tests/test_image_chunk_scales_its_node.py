"""An Image chunk draws a picture, and scaling it scales the node it sits in.

A node has been a fixed 220 wide since the graph existed, which is right for a column of
params and useless for a reference frame. So a chunk can now ask to be wider by carrying a
`node_width` param (ChunkNodeItem.WIDTH_PARAM), and the Image chunk is the first to.

Opt-in on purpose: deriving width from every chunk's sizeHint would re-flow every node in
every existing graph, and most of them are the right size already. The last test here pins
that an ordinary chunk is untouched.
"""

from __future__ import annotations

import importlib
import pathlib
import sys

import pytest
from pxr import Usd

from usd_chunks import chunk_registry, chunk_stage
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins.chunk_qt import ChunkWidgetDocker
from usd_chunks.plugins.chunk_qt_nodegraph import _NODE_WIDTH

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
for _dir in (_SCRIPTS / "utils",):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))
image_mod = importlib.import_module("image")


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture
def an_image(tmp_path):
    """A real PNG on disk, written here so the test owns it."""
    from PySide6 import QtGui

    path = tmp_path / "ref.png"
    # 3:2, so a scaled height is obviously derived rather than coincidental.
    pixmap = QtGui.QPixmap(300, 200)
    pixmap.fill(QtGui.QColor("steelblue"))
    assert pixmap.save(str(path))
    return str(path)


@pytest.fixture
def graph(qapp):
    stage = Usd.Stage.CreateInMemory()
    scene = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_stage.initialize(stage, scene)

    docker = ChunkWidgetDocker()
    docker.resize(1000, 800)
    docker.show()
    qapp.processEvents()
    try:
        yield qapp, scene, docker
    finally:
        docker.close()


def _make(scene, module_name):
    mod = chunk_registry.ChunkRegistry().get_chunk_module(module_name)
    chunks = mod.create() if mod.loaded else mod.load()
    for c in chunks:
        scene.add_chunk(c)
    return chunks[0]


def _node_for(docker, chunk_obj):
    view = docker.tab_widget.get_current_graph_view()
    node = view.scene()._nodes[chunk_stage.get_chunk_prim_path(chunk_obj)]
    node.create_proxy()
    return node


def _settle(node, qapp, rounds=4):
    """Let the node re-measure. In the app a timer does this when the body's geometry
    changes; here it is driven directly so the test does not race it."""
    for _ in range(rounds):
        node._place_param_ports(node._chunk_widget)
        qapp.processEvents()


def _widgets(chunk_obj):
    view = chunk_obj.get_param(image_mod.VIEW_PARAM)
    return [w for w in image_mod._LIVE if getattr(w, "param", None) is view]


def test_the_node_grows_with_the_slider(graph, an_image):
    qapp, scene, docker = graph
    img = _make(scene, "image")
    img.get_param(image_mod.FILE_PARAM).value = an_image
    image_mod.apply_to_widgets(img)
    qapp.processEvents()
    node = _node_for(docker, img)
    _settle(node, qapp)

    widths = []
    for target in (420, 900, 300):
        _widgets(img)[0]._slider.setValue(target)
        qapp.processEvents()
        _settle(node, qapp)
        widths.append(round(node.boundingRect().width()))

    assert widths[0] < widths[1], "the node must widen with the slider"
    assert widths[2] < widths[0], "and narrow again"


def test_the_picture_keeps_its_aspect(graph, an_image):
    """A 3:2 image at 600 wide is 400 tall, whatever the node does around it."""
    qapp, scene, docker = graph
    img = _make(scene, "image")
    img.get_param(image_mod.FILE_PARAM).value = an_image
    image_mod.apply_to_widgets(img)
    qapp.processEvents()

    widget = _widgets(img)[0]
    widget.set_display_width(600)
    qapp.processEvents()

    assert widget._label.pixmap().width() == 600
    assert widget._label.pixmap().height() == pytest.approx(400, abs=2)


def test_the_width_is_recorded_on_the_chunk_so_it_is_saved(graph, an_image):
    """The slider is a Qt control, but the value belongs to the chunk -- that is what makes
    it survive a save, and what the node reads."""
    qapp, scene, docker = graph
    img = _make(scene, "image")
    image_mod.apply_to_widgets(img)
    qapp.processEvents()

    _widgets(img)[0]._slider.setValue(777)
    qapp.processEvents()

    assert img.get_param(image_mod.WIDTH_PARAM).value == 777


def test_a_reopened_chunk_shows_its_picture_again(graph, an_image):
    """execute() is the reserved hook load_chunks calls after restoring saved values; it is
    what puts the picture back, since the widget is rebuilt empty."""
    qapp, scene, docker = graph
    img = _make(scene, "image")
    img.get_param(image_mod.FILE_PARAM).value = an_image
    img.get_param(image_mod.WIDTH_PARAM).value = 640

    image_mod.execute(img)
    qapp.processEvents()

    widget = _widgets(img)[0]
    assert widget._pixmap is not None, "the saved path was not loaded"
    assert widget._display_width == 640


def test_both_of_a_chunks_widgets_are_updated(graph, an_image):
    """Shown in the params panel AND as a node, a chunk has two widgets -- chunk_core builds
    a second rather than reparenting the first. Updating only param.custom_widget left the
    node showing an empty frame while the panel showed the picture."""
    qapp, scene, docker = graph
    img = _make(scene, "image")
    img.get_param(image_mod.FILE_PARAM).value = an_image
    _node_for(docker, img)  # the second widget is built here
    qapp.processEvents()
    image_mod.apply_to_widgets(img)
    qapp.processEvents()

    widgets = _widgets(img)
    assert len(widgets) >= 2, "expected a widget per panel showing the chunk"
    assert all(w._pixmap is not None for w in widgets)


def test_an_ordinary_chunk_is_left_at_the_standard_width(graph):
    """The opt-in half of the feature: no node_width param, no change."""
    qapp, scene, docker = graph
    other = _make(scene, "transform")
    qapp.processEvents()
    node = _node_for(docker, other)
    _settle(node, qapp)

    assert node._width == _NODE_WIDTH
