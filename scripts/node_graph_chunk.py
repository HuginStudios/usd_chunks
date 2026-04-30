"""
Node Graph chunk — visual flowchart editor for USD operations.
Press Tab on the canvas to search/add nodes, or use the + Add Node button.
Hit ▶ Execute to run the graph top-to-bottom.
"""
import sys
from usd_chunks import chunk, chunk_core, chunk_registry
from PySide6 import QtWidgets, QtCore

# Force reload node_graph submodules on chunk reload
for mod_name in list(sys.modules):
    if mod_name.startswith("node_graph"):
        del sys.modules[mod_name]

from node_graph.graph_widget import NodeGraphWidget

_active_graph_widget = None

def get_graph_widget():
    return _active_graph_widget


class _FocusGuardWindow(QtWidgets.QWidget):
    """Floating window that disables usdview's AppEventFilter while active."""
    def __init__(self):
        super().__init__(None, QtCore.Qt.Window)
        self._filter = None
        self._graph_widget = None

    def _get_usdview_filter(self):
        if self._filter is None:
            try:
                scene = chunk_registry.ChunkRegistry().scene
                ctrl = scene.usdviewAPI._UsdviewApi__appController
                self._filter = ctrl._filterObj
            except Exception:
                self._filter = False
        return self._filter if self._filter else None

    def showEvent(self, event):
        f = self._get_usdview_filter()
        if f:
            QtWidgets.QApplication.instance().removeEventFilter(f)
        super().showEvent(event)

    def hideEvent(self, event):
        f = self._get_usdview_filter()
        if f:
            QtWidgets.QApplication.instance().installEventFilter(f)
        super().hideEvent(event)

    def event(self, event):
        # Clear stuck keys when window regains focus
        if event.type() == QtCore.QEvent.WindowActivate:
            if self._graph_widget:
                try:
                    self._graph_widget._graph.viewer().clear_key_state()
                except Exception:
                    pass
        return super().event(event)
        super().hideEvent(event)


class NodeGraphChunkWidget(chunk_core.ChunkParamWidgetBase, QtWidgets.QWidget):
    def __init__(self, param):
        super().__init__(param)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self._open_btn = QtWidgets.QPushButton("Open Node Graph")
        self._open_btn.clicked.connect(self._open_window)
        layout.addWidget(self._open_btn)

        self._window = None
        self._graph_widget = None
        self.setup_chunk_widget()

        from usd_chunks import chunk_notice
        self._reload_conn = chunk_notice.NoticeConnection(
            self._on_reload, chunk_notice.ChunkNoticeAlias.CHUNK_LOADED
        )

    def _on_reload(self, notice, sender):
        # Only close if the node_graph_chunk itself is being reloaded
        if sender and hasattr(sender, 'name') and 'node_graph' not in str(getattr(sender, 'name', '')).lower():
            return
        if self._window:
            self._window.close()
            self._window = None

    @staticmethod
    def _qt_alive(widget):
        """True if the Qt C++ object behind ``widget`` hasn't been destroyed."""
        if widget is None:
            return False
        try:
            widget.objectName()
            return True
        except RuntimeError:
            return False

    def _open_window(self):
        # Window still alive (just hidden on previous close) — show and return.
        # Qt's default closeEvent just hides top-level widgets, so the window,
        # the graph widget, and every node inside survive untouched. We only
        # have to not throw away our reference to it.
        if self._qt_alive(self._window):
            self._window.show()
            self._window.raise_()
            self._window.activateWindow()
            # Re-publish the global ref in case it was cleared.
            if self._qt_alive(self._graph_widget):
                import sys
                sys.modules['_node_graph_ref'] = type(sys)('_node_graph_ref')
                sys.modules['_node_graph_ref'].widget = self._graph_widget
            return

        # Window was destroyed (e.g. by chunk reload) — start fresh.
        self._window = _FocusGuardWindow()
        self._window.setWindowTitle("Node Graph")
        self._window.resize(1000, 600)
        self._window.setStyleSheet("background: #08090c;")

        win_layout = QtWidgets.QVBoxLayout(self._window)
        win_layout.setContentsMargins(0, 0, 0, 0)

        # If the graph widget somehow outlived the previous window, reparent
        # it into the new one so any nodes already placed are preserved.
        if self._qt_alive(self._graph_widget):
            self._graph_widget.setParent(self._window)
        else:
            try:
                self._graph_widget = NodeGraphWidget(self._window)
            except RuntimeError as e:
                self._window.close()
                self._window = None
                QtWidgets.QMessageBox.information(None, "Node Graph", str(e))
                return
        self._window._graph_widget = self._graph_widget
        # Store globally so MCP server can access it
        import sys
        sys.modules['_node_graph_ref'] = type(sys)('_node_graph_ref')
        sys.modules['_node_graph_ref'].widget = self._graph_widget
        win_layout.addWidget(self._graph_widget)

        try:
            scene = chunk_registry.ChunkRegistry().scene
            if scene:
                self._graph_widget.set_stage(scene.get_stage())
        except Exception:
            pass

        # Enable drag from usdview's prim tree
        try:
            scene = chunk_registry.ChunkRegistry().scene
            ctrl = scene.usdviewAPI._UsdviewApi__appController
            tree = ctrl._ui.primView
            tree.setDragEnabled(True)
            tree.setDragDropMode(QtWidgets.QAbstractItemView.DragOnly)
        except Exception:
            pass

        self._window.show()

    def set_label(self, text):
        pass


def main():
    custom = chunk.Custom("Node Graph")
    widget = NodeGraphChunkWidget(custom)
    custom.custom_widget = widget

    c = chunk.Chunk("Node Graph")
    c.add_param(custom)
    return c


def config():
    return chunk.DefaulConfig(category=chunk.StandardCategory.USER)
