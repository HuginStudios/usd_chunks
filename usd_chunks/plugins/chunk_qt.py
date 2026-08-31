import os
import weakref

from PySide6 import QtWidgets, QtCore, QtGui
from pxr import Tf

from usd_chunks import chunk, chunk_notice, chunk_registry, chunk_serial
from usd_chunks import chunk_core
from usd_chunks.chunk_flags import ChunkFlags

from usd_chunks.plugins import chunk_usd

from usd_chunks.plugins.chunk_qt_widgets import (
    ChunkArea,
    ChunkWidget,
    source_code_dial,
    FilterableChunkMenu,
    file_dial,
    apply_chunk_stylesheet,
    ViewSlotState,
    _clean_description,
)

# Re-exported, not used here: chunk_qt is the Qt entry point chunks and DCC plugins
# import against, so the widget API stays reachable as chunk_qt.<Name> even though it
# now lives in chunk_qt_widgets.
from usd_chunks.plugins.chunk_qt_widgets import (
    CHUNK_STYLESHEET,  # noqa: F401
    SyntaxHighlighter,  # noqa: F401
    ChunkSliderWidget,  # noqa: F401
    ChunkFloatSpinboxWidget,  # noqa: F401
    ChunkIntSpinboxWidget,  # noqa: F401
    ChunkLabelWidget,  # noqa: F401
    ChunkLineEditWidget,  # noqa: F401
    ChunkTextEditWidget,  # noqa: F401
    ChunkFileWidget,  # noqa: F401
    ChunkComboBoxWidget,  # noqa: F401
    ChunkCheckBoxWidget,  # noqa: F401
    ChunkPushButtonWidget,  # noqa: F401
    ChunkSeparatorWidget,  # noqa: F401
    ChunkQTWidgetFactory,  # noqa: F401
    LabeledWidget,  # noqa: F401
    ContainerWidget,  # noqa: F401
    confirm_dial,  # noqa: F401
)
from usd_chunks.plugins.chunk_qt_nodegraph import ChunkGraphView


def save_chunkgroup_dialog(scene=None) -> None:
    """Export the current chunk graph as one clean, portable USD layer — every
    chunk's own bookkeeping freshly authored into a brand-new layer, with no
    subLayerPaths at all. Same mechanism as the Export ChunkGroup chunk.
    Shared by the DCC plugins' Save ChunkGroup menu entries."""
    scene = scene or chunk_registry.ChunkRegistry().scene

    flt = "*.usda *.usd *.usdc *.usdz"
    if not (file := file_dial(flt, create_new=True)):
        return
    if not os.path.splitext(file)[1]:
        file += ".usda"
    chunk_usd.export_chunks(scene, file)


def save_scene_dialog(scene=None) -> None:
    """The "nice save flow" (Dan's request): export_chunks plus the stage metadata
    (up-axis, units) it doesn't carry on its own -- see chunk_usd.save_scene. Unlike
    Save ChunkGroup, the result reopens with the same up-axis/units as the live scene."""
    scene = scene or chunk_registry.ChunkRegistry().scene

    flt = "*.usda *.usd *.usdc *.usdz"
    if not (file := file_dial(flt, create_new=True)):
        return
    if not os.path.splitext(file)[1]:
        file += ".usda"
    chunk_usd.save_scene(scene, file)


class EditableTabBar(QtWidgets.QTabBar):
    request_delete = QtCore.Signal(int)
    request_toggle_disable = QtCore.Signal(int)
    tab_name_changed = QtCore.Signal(str, int)

    def __init__(self, parent=None):
        super().__init__(parent)

        self._editor = QtWidgets.QLineEdit(self)
        self._editor.setWindowFlags(QtCore.Qt.WindowType.Popup)
        self._editor.setFocusProxy(self)
        self._editor.editingFinished.connect(self.handleEditingFinished)
        self._editor.installEventFilter(self)

    def mousePressEvent(self, event):
        if self.tabAt(event.pos()) == 0:
            return
        super().mousePressEvent(event)

    def contextMenuEvent(self, event: QtGui.QContextMenuEvent) -> None:
        idx = self.tabAt(event.pos())
        if idx == 0:
            return

        menu = QtWidgets.QMenu()

        remove_action = QtGui.QAction("remove")
        disable_action = QtGui.QAction("toggle enabled")

        menu.addActions([remove_action, disable_action])
        res = menu.exec_(event.globalPos())

        if not res:
            return

        if res == remove_action:
            self.request_delete.emit(idx)
        elif res == disable_action:
            self.request_toggle_disable.emit(idx)

    def eventFilter(self, widget, event):
        if (
            event.type() == QtCore.QEvent.Type.MouseButtonPress
            and not self._editor.geometry().contains(event.globalPos())
        ) or (event.type() == QtCore.QEvent.Type.KeyPress and event.key() == QtCore.Qt.Key.Key_Escape):
            self._editor.hide()
            return True

        return super().eventFilter(widget, event)

    def mouseDoubleClickEvent(self, event):
        index = self.tabAt(event.pos())
        if index > 0:
            self.editTab(index)

    def editTab(self, index):
        if self.shape() in [
            QtWidgets.QTabBar.Shape.RoundedEast,
            QtWidgets.QTabBar.Shape.RoundedWest,
            QtWidgets.QTabBar.Shape.TriangularEast,
            QtWidgets.QTabBar.Shape.TriangularWest,
        ]:
            rect = self.tabRect(index)
            h = rect.height()
            rect.setHeight(rect.width())
            rect.setWidth(h)

        else:
            rect = self.tabRect(index)

        self._editor.move(self.mapToGlobal(rect.topLeft()))
        self._editor.setText(self.tabText(index))

        self._editor.setFixedSize(rect.size())
        if not self._editor.isVisible():
            self._editor.show()

    def handleEditingFinished(self):
        index = self.currentIndex()
        if index >= 0:
            self._editor.hide()
            self.setTabText(index, self._editor.text())
            self.tab_name_changed.emit(self._editor.text(), index)


_TEXT_INPUT_TYPES = (
    QtWidgets.QLineEdit,
    QtWidgets.QTextEdit,
    QtWidgets.QAbstractSpinBox,
    QtWidgets.QPlainTextEdit,
)


def _guarded_copy():
    focused = QtWidgets.QApplication.focusWidget()
    if focused and isinstance(focused, _TEXT_INPUT_TYPES):
        return
    chunk_usd.copy_selected_chunks()


def _focused_view_is_graph(focused: QtWidgets.QWidget | None) -> bool:
    """True if focused sits inside a ChunkGraphView (the Graph page), for either page's
    copy/paste shortcut — both pages of a tab share one QShortcut (WidgetWithChildrenShortcut),
    so this is how paste tells which surface it was actually triggered from."""
    w = focused
    while w is not None:
        if isinstance(w, ChunkGraphView):
            return True
        w = w.parentWidget()
    return False


def _guarded_paste():
    focused = QtWidgets.QApplication.focusWidget()
    if focused and isinstance(focused, _TEXT_INPUT_TYPES):
        return
    # Pasted chunks follow the active surface, same as a fresh create: pinned if pasted while
    # looking at Params, not pinned if pasted while looking at the Graph.
    chunk_usd._pin_new_chunks = not _focused_view_is_graph(focused)
    try:
        chunk_usd.paste_chunks()
    finally:
        chunk_usd._pin_new_chunks = None


class ChunkTabContextStack(QtWidgets.QStackedWidget):
    def __init__(self, ctx: chunk_core.ChunkBase, idx: int, parent=None):
        super().__init__(parent)

        params_widget = ChunkWidget(ctx, self, is_tab_context=True)
        graph_view = ChunkGraphView(self)

        vs = ViewSlotState(self)
        params_widget.set_view_state(vs)
        graph_view.set_view_state(vs)

        self.addWidget(params_widget)  # page 0: params
        self.addWidget(graph_view)  # page 1: graph

        self.setCurrentIndex(idx)

        copy_sc = QtGui.QShortcut(QtGui.QKeySequence.StandardKey.Copy, self)
        copy_sc.setContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
        copy_sc.activated.connect(_guarded_copy)

        paste_sc = QtGui.QShortcut(QtGui.QKeySequence.StandardKey.Paste, self)
        paste_sc.setContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
        paste_sc.activated.connect(_guarded_paste)

        self.ctx = weakref.ref(ctx, self._ctx_died)

    def _ctx_died(self, ref):
        # Weakref finalizer — can fire during app shutdown after Qt has already destroyed
        # this stack's C++ object, so setParent/deleteLater would raise. Ignore that.
        try:
            # Visible widget reparented to no parent becomes a real top-level window that
            # Qt shows immediately — suppress that before detaching.
            self.setAttribute(QtCore.Qt.WA_DontShowOnScreen)
            self.setParent(None)
            self.deleteLater()
        except RuntimeError:
            pass


class _EmptyPanelTabRouter(QtCore.QObject):
    """Application-level event filter: bootstraps the first context tab when Tab is
    pressed while hovering the chunk panel before any tab exists.

    ChunkGraphView installs its own Tab-to-create router (_ProxyKeyRouter in
    chunk_qt_nodegraph) once it's built, but that requires a context tab to already
    exist. Before the first tab is created, the panel only holds the "+" placeholder
    (ChunkArea), which has no graph view and so nothing listening for Tab. This filter
    covers exactly that gap.
    """

    def __init__(self, tab_widget: "ChunkTabWidget"):
        super().__init__(tab_widget)
        # Weak, deliberately -- see _ProxyKeyRouter for why (installing on the
        # QApplication would otherwise pin the whole panel through this filter).
        self._tab_widget = weakref.ref(tab_widget)

    def uninstall(self) -> None:
        if (app := QtWidgets.QApplication.instance()) is not None:
            app.removeEventFilter(self)

    def eventFilter(self, _obj, event) -> bool:
        if (tab_widget := self._tab_widget()) is None:
            self.uninstall()
            return False
        etype = event.type()
        if etype not in (QtCore.QEvent.Type.KeyPress, QtCore.QEvent.Type.ShortcutOverride):
            return False
        if event.key() != QtCore.Qt.Key.Key_Tab:
            return False
        if tab_widget.get_current_graph_view() is not None:
            return False  # a real context tab exists -- its own ChunkGraphView router handles Tab
        tab_global = QtCore.QRect(tab_widget.mapToGlobal(QtCore.QPoint(0, 0)), tab_widget.size())
        if not tab_global.contains(QtGui.QCursor.pos()):
            return False
        if etype == QtCore.QEvent.Type.ShortcutOverride:
            event.accept()
            return True
        tab_widget.add_new_tab()
        if (gv := tab_widget.get_current_graph_view()) is not None:
            gv._open_tab_menu()
        event.accept()
        return True


class ChunkTabWidget(QtWidgets.QTabWidget):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.tab_bar = EditableTabBar(self)
        self.setTabBar(self.tab_bar)

        self.tab_bar.request_delete.connect(self.delete_tab)
        self.tab_bar.request_toggle_disable.connect(self.toggle_disabled_tab)
        self.tab_bar.tab_name_changed.connect(self.update_context_name)

        self._view_mode: int = 0  # 0 = params, 1 = graph

        self._btn = QtWidgets.QPushButton("+", self)
        self._btn.clicked.connect(self.add_new_tab)

        self._tabPosition = QtWidgets.QTabWidget.TabPosition.North
        self.setMinimumWidth(200)

        self.add_tab_button()

        self._empty_panel_router = _EmptyPanelTabRouter(self)
        QtWidgets.QApplication.instance().installEventFilter(self._empty_panel_router)
        # Pair the install with a removal, same as ChunkGraphView's _ProxyKeyRouter --
        # otherwise the filter keeps receiving app-wide key events for the rest of the
        # session after this panel is gone.
        self.destroyed.connect(lambda *_, _router=self._empty_panel_router: _router.uninstall())
        self.update_stylesheet()

    def update_stylesheet(self):
        code = ""

        if self.count() > 1:
            code = f"""
                QTabBar::tab {{
                    border-color: #4A4A4A;
                    border: 1px solid transparent;
                }}
                QTabBar::tab:right {{
                    padding: 12 0px;
                    border-top-right-radius: 8px;
                    border-bottom-right-radius: 8px;
                }}
                QTabBar::tab:top {{
                    padding: 0 12px;
                    border-top-left-radius: 8px;
                    border-top-right-radius: 8px;
                }}

                QTabBar::tab:hover:!selected {{
                    background: #868686;
                }}

                QTabBar::tab:selected:!first {{
                    border-color: {ChunkWidget.SELECTED_COLOR};
                }}

                QTabBar::tab:first {{
                    background: transparent;
                    border: none;
                    padding: 0 0px;
                }}
                QTabBar::tab:first:hover {{
                    background: transparent;
                }}
                """
        else:
            code = """
                QTabBar::tab {
                    background: transparent;
                    border: none;
                    padding: 0 0px;
                }
                """
        self.setStyleSheet(code)

    def toggle_disabled_tab(self, idx):
        if t := self.get_chunk_context(idx):
            if t.group.has_state(ChunkFlags.State.DISABLED):
                t.group.enable()
            else:
                t.group.disable()

    def delete_tab(self, idx):
        if t := self.get_chunk_context(idx):
            t.group.request_delete()
        self.update_stylesheet()

    def update_context_name(self, name, idx):
        if tab := self.get_chunk_context(idx):
            tab.group.name = name

    def add_new_tab(self):
        # Create a new context chunk which will be displayed as the current tab
        name = f"Tab {self.tabBar().count()}"

        if scene := chunk.get_scene():
            scene.add_context(name)
        else:
            c = chunk.Chunk(name, flags=ChunkFlags.GroupFlags.CONTEXT)
            self.add_chunk_context(c)

    def add_tab_button(self):
        w = ChunkArea(self)
        idx = self.addTab(w, "")

        self.tabBar().setTabButton(idx, QtWidgets.QTabBar.ButtonPosition.LeftSide, self._btn)
        return idx

    def update_tab_position(self):
        if self.height() > self.width():
            self._tabPosition = QtWidgets.QTabWidget.TabPosition.East
        else:
            self._tabPosition = QtWidgets.QTabWidget.TabPosition.North
        self.setTabPosition(self._tabPosition)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        self.update_tab_position()
        return super().resizeEvent(event)

    def tabPosition(self) -> QtWidgets.QTabWidget.TabPosition:
        return self._tabPosition

    def contextMenuEvent(self, event):
        entry = FilterableChunkMenu().select_module(event.globalPos())
        if not entry:
            return

        chunks = entry.create()

        # Flag the Params-tab origin so ChunkStageSync inserts the new chunk before the active
        # view (view-follows-tail) and auto-pins it — node-graph creates don't set either flag,
        # there wiring is manual and pin is opt-in via the node's right-click menu.
        chunk_usd._params_tab_create = True
        chunk_usd._pin_new_chunks = True
        try:
            for c in chunks:
                self.add_chunk_to_tab(c)
        finally:
            chunk_usd._params_tab_create = False
            chunk_usd._pin_new_chunks = None

    def get_chunk_context(self, idx) -> ChunkWidget | None:
        w = self.widget(idx)
        if isinstance(w, ChunkTabContextStack):
            w = w.widget(0)
        # Not every tab holds a context: index 0 is the "+" ChunkArea, and during
        # async teardown (a ChunkTabContextStack removing itself on GC) the current
        # widget can momentarily be something else. Return None rather than assert.
        return w if isinstance(w, ChunkWidget) else None

    def get_chunk_context_index(self, ctx: chunk_core.ChunkBase) -> int | None:
        for i in range(self.count()):
            w = self.widget(i)
            if isinstance(w, ChunkTabContextStack):
                # Match on the stack's own context weakref. During delete the inner params
                # ChunkWidget is torn down first, which shifts page 0 to the graph view and
                # breaks a widget(0).group lookup — the stack's ctx stays valid until GC.
                if w.ctx() is ctx:
                    return i
                w = w.widget(0)
            if isinstance(w, ChunkWidget):
                try:
                    if w.group == ctx:
                        return i
                except ReferenceError:
                    continue
        return None

    def add_chunk_context(self, c: chunk.ChunkBase):
        stack = ChunkTabContextStack(c, self._view_mode, self)
        idx = self.addTab(stack, c.name)
        cfg = c.get_config() if hasattr(c, "get_config") else None
        if cfg and (desc := _clean_description(cfg.description)):
            self.setTabToolTip(idx, desc)
        self.setCurrentIndex(idx)
        self.update_stylesheet()
        # The tab text above is only ever set once, here. A later rename -- e.g. via the
        # node graph's right-click "Rename..." on this same chunk/chunkgroup, which just
        # does `grp.name = new` -- has nothing else re-syncing it, leaving the tab stuck on
        # its creation-time name. Stored on the stack so the connection's lifetime matches
        # the tab (weakrefs everywhere here, per NoticeConnection -- no manual teardown
        # needed on tab removal).
        stack._rename_conn = chunk_notice.NoticeConnection(
            self._on_context_renamed,
            chunk_notice.ChunkNoticeAlias.ATTRIBUTE_CHANGED,
            c.get_attribute_sender("name"),
        )

    def _on_context_renamed(self, notice, sender):
        ctx = sender.get_owner()
        if ctx is None:
            return
        idx = self.get_chunk_context_index(ctx)
        if idx is not None:
            self.setTabText(idx, ctx.name)

    def set_view_mode(self, mode: int):
        self._view_mode = mode
        for i in range(self.count()):
            w = self.widget(i)
            if isinstance(w, ChunkTabContextStack):
                w.setCurrentIndex(mode)

    def get_current_graph_view(self):
        w = self.widget(self.currentIndex())
        if isinstance(w, ChunkTabContextStack):
            gv = w.widget(1)
            if isinstance(gv, ChunkGraphView):
                return gv
        return None

    def add_chunk_to_tab(self, param: chunk_core.ChunkBase, tabidx=None):
        if tabidx is None:
            tabidx = self.tabBar().currentIndex()
        # No context tab to receive the chunk: index 0 is the "+" button, and -1 means
        # every tab was deleted. Either way, spin up a fresh tab and target it (add_new_tab
        # selects the new tab, so read the current index back).
        if tabidx <= 0 or self.get_chunk_context(tabidx) is None:
            self.add_new_tab()
            tabidx = self.tabBar().currentIndex()

        if t := self.get_chunk_context(tabidx):
            if param in t.group.get_my_params():
                return

            cfg = getattr(param, "get_config", lambda: None)()
            if cfg and cfg.module and param.has_flag(ChunkFlags.GroupFlags.CONTEXT):
                param._parent_context = weakref.ref(t.group)

            t.group.add_param(param)


class ChunkWidgetDocker(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)

        # Chunks whose CHUNK_LOADED arrived during a file open, waiting on one shared
        # graph rebuild -- see _loaded_module_callback.
        self._pending_graph_rebuild: set = set()
        self._graph_rebuild_scheduled = False

        self._chunk_loaded_listener = chunk_notice.NoticeConnection(
            self._loaded_module_callback, chunk_notice.ChunkNoticeAlias.CHUNK_LOADED, globally=True
        )
        self._chunk_deleted_listener = chunk_notice.NoticeConnection(
            self._deleted_chunk_callback, chunk_notice.ChunkNoticeAlias.CHUNK_DELETED, globally=True
        )
        self.build()

    def _loaded_module_callback(self, notice: Tf.Notice, sender: chunk.Chunk):
        """Called whenever CHUNK_LOADED notice was recieved - will in turn add a new chunk widget

        Args:
            notice (chunk_notice.ChunkLoadedNotice): The notice sent from the registry
            sender (chunk.Chunk): The new chunk that was added
        """
        self.create_chunk_widget(sender)
        self._invalidate_graph_views()
        if self._view_bar.currentIndex() == 1:
            if chunk_notice._loading_stage:
                # One CHUNK_LOADED fires per chunk, and every rebuild reconciles the whole
                # graph against every chunk prim -- so a file open costs one full rebuild
                # per chunk in it (60 rebuilds for 57 chunks, measured). Only the last one
                # describes the finished graph; collapse them into a single deferred pass.
                # Deferring is safe here specifically because a rebuild already defers its
                # own expensive half (proxy bodies are built in batches off a timer), so
                # nothing downstream can have been assuming a fully built graph on return.
                self._pending_graph_rebuild.add(sender)
                if not self._graph_rebuild_scheduled:
                    self._graph_rebuild_scheduled = True
                    QtCore.QTimer.singleShot(0, self._flush_graph_rebuild)
            else:
                self._rebuild_graph(changed_chunks={sender})
        if chunk_usd.is_view_chunk(sender):
            self._reposition_view_chunk(sender)
        elif not chunk_notice._loading_stage:
            chunk_registry.ChunkRegistry().scene.set_selected_chunks([sender])

    def _flush_graph_rebuild(self) -> None:
        """Run the single rebuild that a finished load needs, for every chunk it brought in."""
        self._graph_rebuild_scheduled = False
        changed = self._pending_graph_rebuild
        self._pending_graph_rebuild = set()
        if not changed:
            return
        try:
            self._rebuild_graph(changed_chunks=changed)
        except RuntimeError:
            return  # docker torn down between the load finishing and this firing

    def _reposition_view_chunk(self, view_chunk: chunk.ChunkBase) -> None:
        registry = chunk_registry.ChunkRegistry()
        if not registry.initialized:
            return
        stage = registry.scene.get_stage()
        if not stage:
            return
        sources = chunk_usd.get_view_exec_sources(view_chunk, stage, registry.scene)
        if not sources:
            return
        ctx = view_chunk.get_context()
        if not ctx:
            return
        idx = self.tab_widget.get_chunk_context_index(ctx)
        tab_cw = self.tab_widget.get_chunk_context(idx)
        if tab_cw is not None:
            tab_cw._reorder_chunk_widget_after(view_chunk, sources[0])

    def _deleted_chunk_callback(self, notice: Tf.Notice, sender: chunk.Chunk):
        if not sender.has_flag(ChunkFlags.GroupFlags.CONTEXT):
            return
        if (idx := self.tab_widget.get_chunk_context_index(sender)) is not None:
            self.tab_widget.removeTab(idx)
        self._invalidate_graph_views()

    def create_chunk_widget(self, loaded_chunk: chunk.ChunkBase):
        if loaded_chunk.has_flag(ChunkFlags.GroupFlags.CONTEXT):
            cfg = loaded_chunk.get_config()
            if cfg and cfg.module:
                # User-created ChunkGroup: add a standalone ChunkWidget directly to
                # the parent tab's content layout — NOT via add_param, which would
                # propagate sub-chunk PARAM_ADDED notices up to the parent context.
                parent_ctx = getattr(loaded_chunk, "_parent_context", None)
                parent_ctx = parent_ctx() if parent_ctx else None
                if parent_ctx is not None:
                    idx = self.tab_widget.get_chunk_context_index(parent_ctx)
                    # A nested group's parent may itself be a just-created group nobody has
                    # dived into yet (no tab exists for it) -- e.g. recursively
                    # pasting/duplicating a group that contains a nested sub-group. No tab
                    # to attach a widget to in that case; the sub-group's contents still
                    # exist on the stage and become visible once the parent group is dived
                    # into for the first time (ChunkTabWidget.dive_into builds its tab fresh
                    # from the stage at that point).
                    if idx is not None:
                        tab_cw = self.tab_widget.get_chunk_context(idx)
                        if tab_cw is not None:
                            tab_cw._add_group_widget(loaded_chunk)
                    return
            # Plain tab context: create its tab immediately.
            self.tab_widget.add_chunk_context(loaded_chunk)
        elif ctx := loaded_chunk.get_context():
            idx = self.tab_widget.get_chunk_context_index(ctx)
            if idx is not None:
                self.tab_widget.add_chunk_to_tab(loaded_chunk, idx)

    def _current_context(self):
        idx = self.tab_widget.currentIndex()
        if idx == 0:
            return None
        w = self.tab_widget.get_chunk_context(idx)
        return w.group if w else None

    def _invalidate_graph_views(self):
        for i in range(self.tab_widget.count()):
            w = self.tab_widget.widget(i)
            if isinstance(w, QtWidgets.QStackedWidget):
                gv = w.widget(1)
                if isinstance(gv, ChunkGraphView):
                    gv.scene().invalidate()

    def _rebuild_graph(self, changed_chunks=None):
        """changed_chunks: passed through to ChunkGraphView.build()'s post-build recook when the
        caller knows exactly which chunk(s) triggered this rebuild (e.g. one chunk was just
        created) -- see build()'s docstring for why this matters. Omit for a genuine view/tab
        switch, which always needs a full recook."""
        registry = chunk_registry.ChunkRegistry()
        if not registry.initialized:
            return
        stage = registry.scene.get_stage()
        if not stage:
            return
        graph_view = self.tab_widget.get_current_graph_view()
        if not graph_view:
            return
        context = self._current_context()
        scene = graph_view.scene()
        if not getattr(scene, "_dirty", True) and scene._built_context == context:
            return  # already built for this context and not invalidated — keep the cache
        graph_view.build(stage, registry.scene, context, changed_chunks=changed_chunks)
        graph_view.dive_into.connect(self._on_dive_into, QtCore.Qt.UniqueConnection)

    def _on_dive_into(self, context_chunk):
        idx = self.tab_widget.get_chunk_context_index(context_chunk)
        if idx is None:
            # Ensure the group isn't collapsed when we build its tab — the scroll
            # area is sized via setFixedHeight inside update_from_state, so a
            # lingering COLLAPSED state would make it 0px tall on first show.
            context_chunk.unset_state(ChunkFlags.State.COLLAPSED)
            self.tab_widget.add_chunk_context(context_chunk)
            idx = self.tab_widget.get_chunk_context_index(context_chunk)
        if idx is not None:
            self.tab_widget.setCurrentIndex(idx)

    def _on_tab_changed(self, _):
        registry = chunk_registry.ChunkRegistry()
        if registry.initialized:
            ctx = self._current_context()
            registry.scene._current_context = weakref.ref(ctx) if ctx is not None else None
        self._rebuild_graph()
        # Re-establish the viewport for the tab we switched to: its active view must drive the
        # (global) stage, muting a view left active in another context. _rebuild_graph skips
        # build() when the graph is cached, so recook here too — covers the cached-tab switch.
        gv = self.tab_widget.get_current_graph_view()
        if gv is not None:
            QtCore.QTimer.singleShot(0, gv._recook_active_view)

    def _on_view_mode_changed(self, mode: int):
        self.tab_widget.set_view_mode(mode)
        if mode == 1:
            self._rebuild_graph()

    def build(self):
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.MinimumExpanding, QtWidgets.QSizePolicy.Policy.MinimumExpanding)
        self.tab_widget: ChunkTabWidget = ChunkTabWidget(self)

        self._view_bar = QtWidgets.QTabBar(self)
        self._view_bar.addTab("Params")
        self._view_bar.addTab("Graph")
        # Start on Graph, not Params — an empty graph rather than an empty Params tab.
        # Set directly (not via setCurrentIndex) since no slot is connected yet and any
        # tabs created below must pick up view mode 1 from the start.
        self.tab_widget.set_view_mode(1)
        self._view_bar.setCurrentIndex(1)
        self._view_bar.currentChanged.connect(self._on_view_mode_changed)
        self.tab_widget.currentChanged.connect(self._on_tab_changed)

        registry = chunk_registry.ChunkRegistry()
        # Applied before any chunk widget exists below it, so every one of them inherits it
        # by cascade instead of re-applying the whole sheet to its own finished tree.
        apply_chunk_stylesheet(self)

        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)

        if registry.initialized:
            # Context chunks are excluded from get_chunks() — process them first
            # so tabs exist before non-context chunks are added to them.
            for ctx in registry.scene.chunks.get_my_params():
                if isinstance(ctx, chunk_core.ChunkBase) and ctx.has_flag(ChunkFlags.GroupFlags.CONTEXT):
                    self.tab_widget.add_chunk_context(ctx)
            for c in registry.scene.get_chunks():
                self.create_chunk_widget(c)

        self.main_layout = QtWidgets.QVBoxLayout()
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)
        self.main_layout.addWidget(self._view_bar)
        self.main_layout.addWidget(self.tab_widget)

        self.setLayout(self.main_layout)
        self.show()


class ChunkModuleLoader(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.content = QtWidgets.QTableWidget(rowCount=0, columnCount=3, parent=self)

        self.content.setSelectionMode(QtWidgets.QTableWidget.SelectionMode.ExtendedSelection)
        self.content.setSelectionBehavior(QtWidgets.QTableWidget.SelectionBehavior.SelectRows)
        self.content.setEditTriggers(QtWidgets.QTableWidget.EditTrigger.NoEditTriggers)

        self.main_layout = QtWidgets.QVBoxLayout()
        self.main_layout.addWidget(self.content)

        self.setLayout(self.main_layout)
        self.load_chunks()

        self.content.model().dataChanged.connect(self.handle_data_change)

    def contextMenuEvent(self, event: QtGui.QContextMenuEvent) -> None:
        if not (item := self.content.currentItem()):
            return

        reg = chunk_registry.ChunkRegistry()
        if not (mod := reg.get_chunk_module(item.text())):
            return

        menu = QtWidgets.QMenu()

        create_action = QtGui.QAction("create")
        export_action = QtGui.QAction("export")
        inspect_action = QtGui.QAction("inspect")

        menu.addActions([create_action, export_action, inspect_action])

        res = menu.exec_(event.globalPos())

        if not res:
            return

        if res == export_action:
            exts = (chunk_serial.FileExtensions.CHUNK_MODULE, chunk_serial.FileExtensions.PY_MODULE)
            flt = " ".join([f"*{x}" for x in exts])
            if file := file_dial(flt, True):
                mod.export(file)
        elif res == inspect_action and mod.module:
            if isinstance(mod.module, chunk_serial.RuntimeModule):
                src = mod.module._code
            else:
                src = chunk_serial.serialize_source(mod.module)
            source_code_dial(src, chunk_core.PySyntaxHighlightMapping())
        elif res == create_action:
            if not mod.loaded:
                cs = mod.load()
            else:
                cs = mod.create()
            if cs:
                chunk_registry.ChunkRegistry().scene.add_chunks(cs)

        return super().contextMenuEvent(event)

    def get_check_state(self, loaded):
        return QtCore.Qt.CheckState.Checked if loaded else QtCore.Qt.CheckState.Unchecked

    def new_row(self, row, mod: chunk_registry.ChunkModule):
        self.content.insertRow(row)

        c1 = QtWidgets.QTableWidgetItem(mod.module_name)
        c1.setToolTip(_clean_description(mod.config.description))

        c2 = QtWidgets.QTableWidgetItem(mod.module_file.as_posix() if mod.module_file.exists() else "Not Found")

        c3 = QtWidgets.QTableWidgetItem(mod.config.category)

        self.content.setItem(row, 0, c1)
        self.content.setItem(row, 1, c2)
        self.content.setItem(row, 2, c3)

    def load_chunks(self):
        reg = chunk_registry.ChunkRegistry()
        for row, c in enumerate(reg.get_chunk_modules()):
            self.new_row(row, c)

    def load_unload(self, mod: chunk_registry.ChunkModule, check):
        if check and not mod.loaded:
            mod.load()
        elif not check and mod.loaded:
            mod.unload()

    def handle_data_change(self, first: QtCore.QModelIndex, last: QtCore.QModelIndex, roles):
        if QtCore.Qt.ItemDataRole.CheckStateRole not in roles:
            return

        reg = chunk_registry.ChunkRegistry()
        check = None

        for r in range(first.row(), last.row() + 1):
            check_idx = self.content.model().index(r, 0)
            mod_idx = self.content.model().index(r, 1)

            mod = reg.get_chunk_module(mod_idx.data())
            if not mod:
                raise ModuleNotFoundError(f"Could not find module {mod_idx.data()}")

            check = check_idx.data(QtCore.Qt.ItemDataRole.CheckStateRole)
            self.load_unload(mod, check)

        if check is None:
            return

        for srange in self.content.selectedRanges():
            for row in range(srange.topRow(), srange.bottomRow() + 1):
                check_idx = self.content.model().index(row, 0)
                mod_idx = self.content.model().index(row, 1)

                if row >= first.row() and row < last.row() + 1:
                    continue

                mod = reg.get_chunk_module(mod_idx.data())

                if not mod:
                    raise ModuleNotFoundError(f"Could not find module {mod_idx.data()}")
                check_item = self.content.item(check_idx.row(), 0)

                if check_item:
                    check_item.setCheckState(self.get_check_state(check))
                    self.load_unload(mod, check)


def build_chunk_docker(parent_window: QtWidgets.QMainWindow):
    # Create an empty dock widget to hold the modules
    module_dock = parent_window.findChild(QtWidgets.QDockWidget, "Chunks")
    if module_dock:
        return

    module_dock = QtWidgets.QDockWidget("Chunks", parent_window)

    module_dock.setWidget(ChunkWidgetDocker(parent=module_dock))
    parent_window.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, module_dock)
    parent_window.resizeDocks([module_dock], [400], QtCore.Qt.Orientation.Horizontal)
