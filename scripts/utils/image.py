"""Image -- put a picture on the graph.

A reference frame, a concept sketch, a plate, a note with an arrow drawn on it: the things
that get pinned next to a shot in every other application, and that until now lived in a
window somebody had to alt-tab to.

The node IS the image. Browse to a file and the node draws it; drag the slider under it and
the **node** grows, with the picture keeping its aspect. Nothing is authored onto the stage
-- this chunk has no prim and no output. It is a note in the graph, saved with the graph
like any other chunk's params.

**Why the width slider is inside the widget** rather than being a Slider param of its own:
the node reads its width from the `node_width` param (ChunkNodeItem.WIDTH_PARAM), so the
value has to live there to be saved -- but a chunk callback on that param did not reliably
fire once the chunk was also shown in the params panel, and a width control that only
sometimes does anything is worse than none. A slider this widget owns is a plain Qt signal:
it rescales the picture on the spot and writes the param, and the node picks the new width
up when the body's height changes under it.

Requires: usd_chunks.
"""

from __future__ import annotations

import logging
import os
import weakref

from PySide6 import QtCore, QtGui, QtWidgets

from usd_chunks import chunk, chunk_widget_api
from usd_chunks.chunk_flags import ChunkFlags

logger = logging.getLogger(__name__)

FILE_PARAM = "image_path"

#: The node graph reads this one by name to size the node (ChunkNodeItem.WIDTH_PARAM).
WIDTH_PARAM = "node_width"

VIEW_PARAM = "preview"

#: The standard node is 220 wide, which is the floor the graph clamps to anyway.
MIN_WIDTH = 220
MAX_WIDTH = 1600
DEFAULT_WIDTH = 420

#: What Qt reads without a plugin, plus the ones a pipeline actually hands you.
EXTENSIONS = [".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp"]

#: Height of the empty state, so a fresh node is a visible drop target rather than a
#: zero-height sliver with a file field above it.
EMPTY_HEIGHT = 110

#: Every live ImageWidget. A chunk shown in the params panel AND as a node in the graph has
#: TWO widgets: chunk_core builds a fresh instance for the second panel rather than
#: reparenting the one the param holds (a widget has one parent). So param.custom_widget is
#: whichever came first, and updating only that one left the node showing an empty frame
#: while the panel showed the picture. Weak, so a closed panel's widget leaves on its own.
_LIVE: "weakref.WeakSet" = weakref.WeakSet()


def _owning_chunk(obj):
    """The chunk holding `obj`, or None.

    Starts at obj and climbs: a param is not a container (a Custom has no get_param at all),
    and a chunk's params can sit inside groups, so neither end can be assumed.
    """
    seen = 0
    while obj is not None and seen < 32:
        get_param = getattr(obj, "get_param", None)
        if callable(get_param) and get_param(FILE_PARAM) is not None:
            return obj
        get_owner = getattr(obj, "get_owner", None)
        obj = get_owner() if callable(get_owner) else None
        seen += 1
    return None


class ImageWidget(chunk_widget_api.ChunkParamWidgetBase, QtWidgets.QWidget):
    """The picture, and the slider that scales the node it sits in."""

    def __init__(self, param: chunk.ChunkParamBase):
        super().__init__(param)
        self._pixmap: QtGui.QPixmap | None = None
        self._source = ""
        self._display_width = DEFAULT_WIDTH

        self._label = QtWidgets.QLabel(self)
        self._label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self._label.setMinimumHeight(EMPTY_HEIGHT)

        self._slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal, self)
        self._slider.setRange(MIN_WIDTH, MAX_WIDTH)
        self._slider.setValue(DEFAULT_WIDTH)
        self._slider.setToolTip("How wide this node is drawn. The image scales with it.")
        self._slider.valueChanged.connect(self._on_slider)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self._label)
        layout.addWidget(self._slider)

        _LIVE.add(self)
        self.setup_chunk_widget()
        # Pull the chunk's current values immediately: a second widget is built the moment
        # the chunk is shown in a second place, and it missed every change that came before
        # it existed. Deferred as well, because the FIRST widget is constructed before its
        # param is added to the chunk, so at that point there is no chunk to read.
        self._sync_from_chunk()
        QtCore.QTimer.singleShot(0, self._sync_from_chunk)

    # -- the chunk side ----------------------------------------------------------------

    def set_label(self, text: str):
        """No label: the picture is the point, and a caption just eats height."""

    def get_value(self):
        return self._source

    def set_value(self, value):
        self.load(value)

    def _sync_from_chunk(self) -> None:
        owner = _owning_chunk(self.param) if self.param is not None else None
        if owner is not None:
            apply_to_widgets(owner)

    # -- content -----------------------------------------------------------------------

    def load(self, path: str) -> None:
        path = (path or "").strip().strip('"')
        if path == self._source and self._pixmap is not None:
            self._rescale()
            return
        self._source = path
        if not path or not os.path.isfile(path):
            self._pixmap = None
            # Say which of the two it is: "no image" on a path the user just typed reads as
            # "this chunk is broken" when the real answer is a typo.
            self._label.setText("Drop an image, or browse above" if not path
                                else f"Not a file:\n{os.path.basename(path)}")
        else:
            pixmap = QtGui.QPixmap(path)
            if pixmap.isNull():
                self._pixmap = None
                self._label.setText(f"Qt cannot read\n{os.path.basename(path)}")
                logger.warning("Image: %s is not a format Qt can read", path)
            else:
                self._pixmap = pixmap
                self._label.setText("")
        self._rescale()

    # -- sizing ------------------------------------------------------------------------

    def set_display_width(self, width: int) -> None:
        """Draw at this width, and re-scale to it now.

        setFixedWidth does not change width() until a layout pass, so rescaling off width()
        right after setting it scales to the OLD size -- the picture stayed put while the
        node grew around it.
        """
        width = int(max(MIN_WIDTH, min(float(width), MAX_WIDTH)))
        self._display_width = width
        self.setFixedWidth(width)
        if self._slider.value() != width:
            was = self._slider.blockSignals(True)
            self._slider.setValue(width)
            self._slider.blockSignals(was)
        self._rescale()

    def _rescale(self) -> None:
        """Repaint at the current width, and report the height that makes."""
        width = max(1, self._display_width)
        chrome = self._slider.sizeHint().height() + 4
        if self._pixmap is None or self._pixmap.isNull():
            self._label.setFixedHeight(EMPTY_HEIGHT)
            self.setFixedHeight(EMPTY_HEIGHT + chrome)
            return
        # scaled() returns a QImage on some PySide6 builds, so go through fromImage rather
        # than assuming a QPixmap comes back.
        scaled = self._pixmap.scaledToWidth(
            width, QtCore.Qt.TransformationMode.SmoothTransformation)
        if isinstance(scaled, QtGui.QImage):
            scaled = QtGui.QPixmap.fromImage(scaled)
        self._label.setFixedHeight(scaled.height())
        self._label.setPixmap(scaled)
        self.setFixedHeight(scaled.height() + chrome)

    def _on_slider(self, value: int) -> None:
        """Rescale now, and record the width so it is saved and the node can read it."""
        self.set_display_width(value)
        owner = _owning_chunk(self.param) if self.param is not None else None
        if owner is None:
            return
        param = owner.get_param(WIDTH_PARAM)
        if param is not None and param.value != value:
            param.value = value
        # Keep the chunk's other widget in step, if the params panel is showing it too.
        for widget in _LIVE:
            if widget is not self and getattr(widget, "param", None) is self.param:
                widget.set_display_width(value)


def apply_to_widgets(owner: chunk.Chunk) -> None:
    """Put the chunk's params onto every widget showing it -- see _LIVE."""
    view = owner.get_param(VIEW_PARAM, chunk.is_inst(chunk.Custom))
    if view is None:
        return
    path = owner.get_param(FILE_PARAM).value
    width = owner.get_param(WIDTH_PARAM).value
    widgets = [w for w in _LIVE if getattr(w, "param", None) is view]
    held = getattr(view, "custom_widget", None)
    if held is not None and held not in widgets:
        widgets.append(held)
    for widget in widgets:
        if isinstance(width, (int, float)):
            widget.set_display_width(int(width))
        widget.load(path)


def execute(node):
    """Authors nothing: an Image is a note in the graph, not a prim on the stage.

    Still worth existing -- it is the reserved hook load_chunks calls once per chunk after
    restoring saved values, and it is what puts the picture back on a reopened scene.
    """
    apply_to_widgets(node)


def _on_changed(owner, notice, sender) -> None:
    if getattr(getattr(notice, "data", None), "name", None) not in ("value", "size"):
        return
    chunk_obj = _owning_chunk(owner)
    if chunk_obj is not None:
        apply_to_widgets(chunk_obj)


def main():
    chunk_api = chunk.Chunk("Image")

    browser = chunk.FileBrowser(FILE_PARAM, "File", extensions=EXTENSIONS)
    browser.set_flag(ChunkFlags.ParamFlags.FILE_EXISTING)
    browser.set_flag(ChunkFlags.ParamFlags.LABELED)
    chunk_api.add_param(browser)

    # Hidden: the control for it is the slider inside the widget (see the module docstring).
    # This is where the value lives so it is saved, and where the node graph reads it.
    width = chunk.Slider(
        WIDTH_PARAM, "Node Width",
        default_value=DEFAULT_WIDTH, min_value=MIN_WIDTH, max_value=MAX_WIDTH,
        is_int=True,
    )
    width.set_state(ChunkFlags.State.HIDDEN)
    chunk_api.add_param(width)

    view = chunk.Custom(VIEW_PARAM)
    view.custom_widget = ImageWidget(view)
    chunk_api.add_param(view)

    # Still worth subscribing: browsing to a file through the params panel arrives here,
    # and it costs one idempotent re-read.
    chunk_api.add_callback(_on_changed, notice_alias=chunk.ChunkNoticeAlias.ATTRIBUTE_CHANGED)
    return chunk_api


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.UTILS,
        description=(
            "Shows an image on the graph -- a reference frame, a concept sketch, a plate, a "
            "note with an arrow on it.\n"
            "The node is the image: browse to a file, then drag the slider under it to scale "
            "the node, with the picture keeping its aspect.\n"
            "Authors nothing on the stage. It is a note in the graph, saved with it like any "
            "other chunk."
        ),
    )
