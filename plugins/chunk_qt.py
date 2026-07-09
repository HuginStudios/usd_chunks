import weakref

from PySide6 import QtWidgets, QtCore, QtGui
from pxr import Tf

from usd_chunks import chunk, chunk_notice, chunk_registry, chunk_serial
from usd_chunks import chunk_core
from usd_chunks.chunk_flags import ChunkFlags

from plugins import chunk_usd

from plugins.chunk_qt_widgets import (
    SyntaxHighlighter,
    ChunkSliderWidget,
    ChunkFloatSpinboxWidget,
    ChunkIntSpinboxWidget,
    ChunkLabelWidget,
    ChunkLineEditWidget,
    ChunkTextEditWidget,
    ChunkFileWidget,
    ChunkComboBoxWidget,
    ChunkCheckBoxWidget,
    ChunkPushButtonWidget,
    ChunkSeparatorWidget,
    ChunkQTWidgetFactory,
    LabeledWidget,
    ChunkArea,
    ContainerWidget,
    ChunkWidget,
    confirm_dial,
    source_code_dial,
    FilterableChunkMenu,
    file_dial,
    CHUNK_STYLESHEET,
    ViewSlotState,
    _clean_description,
)
from plugins.chunk_qt_nodegraph import ChunkGraphView


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


def _guarded_paste():
    focused = QtWidgets.QApplication.focusWidget()
    if focused and isinstance(focused, _TEXT_INPUT_TYPES):
        return
    chunk_usd.paste_chunks()


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
            self.setParent(None)
            self.deleteLater()
        except RuntimeError:
            pass


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
        mod = FilterableChunkMenu().select_module(event.globalPos())
        if not mod:
            return

        if mod.loaded:
            chunks = mod.create()
        else:
            chunks = mod.load()

        for c in chunks:
            self.add_chunk_to_tab(c)

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
            self._rebuild_graph()
        if chunk_usd.is_view_chunk(sender):
            self._reposition_view_chunk(sender)
        elif not chunk_notice._loading_stage:
            chunk_registry.ChunkRegistry().scene.set_selected_chunks([sender])

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

    def _rebuild_graph(self):
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
        graph_view.build(stage, registry.scene, context)
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
        self._view_bar.currentChanged.connect(self._on_view_mode_changed)
        self.tab_widget.currentChanged.connect(self._on_tab_changed)

        registry = chunk_registry.ChunkRegistry()
        self.setStyleSheet(CHUNK_STYLESHEET)

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
