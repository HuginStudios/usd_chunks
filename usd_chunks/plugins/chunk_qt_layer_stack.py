"""Layer Stack Inspector -- a usdview window (Chunks ▸ Layer Stack).

Why this exists: usdview's built-in "Layer Stack" tab is *prim-scoped* -- it only
lists the layers that have an opinion on the currently selected prim, so a freshly
opened scene (or an unauthored prim) shows just the root layer. This window shows
the whole picture instead:

  * every layer composing the stage in strength order (top = strongest = wins),
    including the anonymous chunk edit layers nested under the session layer,
  * mute state and the active edit target (where your edits land),
  * for the selected prim: its composition arcs (root / reference / payload /
    inherit / variant / specialize) in strength order and the winning layer for
    each authored attribute.

Strength rule: session beats root, and within any layer an earlier sublayer beats
a later one -- so reading top-to-bottom is reading strongest-to-weakest.
"""
from __future__ import annotations

from typing_extensions import override

from PySide6 import QtWidgets, QtCore, QtGui
from pxr import Usd, Sdf, Pcp, Tf


_ARC_LABEL = {
    Pcp.ArcTypeRoot: "root",
    Pcp.ArcTypeReference: "reference",
    Pcp.ArcTypePayload: "payload",
    Pcp.ArcTypeInherit: "inherit",
    Pcp.ArcTypeVariant: "variant",
    Pcp.ArcTypeSpecialize: "specialize",
}

_COL_EDIT = "#cab46a"     # gold -- the active edit target
_COL_SESSION = "#7fa8d0"  # blue -- session layer
_COL_ANON = "#9a8fb5"     # purple -- anonymous (chunk / in-memory) layers
_COL_MUTED = "#6a6a6a"    # grey -- muted (excluded from composition)
_COL_DIM = "#8a8a8a"
_COL_WIN = "#79b07a"      # green -- winning opinion

_STYLE = """
QWidget { font-size: 11px; }
QTreeWidget { background:#1e1e1e; border:1px solid #333; border-radius:4px;
              alternate-background-color:#242424; }
QTreeWidget::item { padding:2px 0; }
QLabel#status { color:#888; font-size:10px; }
QPushButton { background:#2a2a2a; color:#ccc; border:1px solid #3a3a3a;
              border-radius:4px; padding:3px 10px; }
QPushButton:hover { background:#3a3a3a; }
QPushButton:checked { background:#33402a; color:#cab46a; border-color:#5a5a3a; }
"""


def _display_name(layer):
    if layer is None:
        return "<missing>"
    return layer.GetDisplayName() or layer.identifier


def _short_id(layer):
    """A compact, human-readable id: filename for files, short tag for anon."""
    if layer is None:
        return ""
    if layer.anonymous:
        tail = layer.identifier.rsplit(":", 1)[-1]
        return f"anon:…{tail}" if tail else layer.identifier
    return layer.identifier


class _UsdHighlighter(QtGui.QSyntaxHighlighter):
    """Lightweight USD-text colouring for the inspect dialog: keywords, types, strings, comments.

    Intentionally rough -- this is a read-only inspector.
    """

    def __init__(self, doc):
        super().__init__(doc)
        self._rules = []

        kw = QtGui.QTextCharFormat()
        kw.setForeground(QtGui.QColor("#7fa8d0"))
        for w in ("def", "over", "class", "variantSet", "variants", "references",
                  "payload", "inherits", "specializes", "add", "delete", "prepend",
                  "append", "rel", "custom", "uniform", "config", "kind"):
            self._rules.append((QtCore.QRegularExpression(rf"\b{w}\b"), kw))

        typ = QtGui.QTextCharFormat()
        typ.setForeground(QtGui.QColor("#9a8fb5"))
        self._rules.append((QtCore.QRegularExpression(
            r"\b(token|float|double|int|bool|string|asset|matrix4d|normal3f|"
            r"point3f|color3f|vector3f|texCoord2f|quatf|half)[\w\[\]]*\b"), typ))

        st = QtGui.QTextCharFormat()
        st.setForeground(QtGui.QColor("#c39b6a"))
        self._rules.append((QtCore.QRegularExpression(r'"[^"]*"'), st))

        cm = QtGui.QTextCharFormat()
        cm.setForeground(QtGui.QColor("#6a6a6a"))
        self._rules.append((QtCore.QRegularExpression(r"#[^\n]*"), cm))

    @override
    def highlightBlock(self, text):
        for rx, fmt in self._rules:
            it = rx.globalMatch(text)
            while it.hasNext():
                m = it.next()
                self.setFormat(m.capturedStart(), m.capturedLength(), fmt)


class _LayerTextDialog(QtWidgets.QDialog):
    """Non-modal window showing a layer's exported USD text."""

    def __init__(self, title, subtitle, text, parent=None):
        super().__init__(parent)
        self.setWindowFlag(QtCore.Qt.Window, True)
        self.setWindowTitle(f"Layer — {title}")
        self.resize(760, 640)
        self.setStyleSheet(_STYLE)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)

        head = QtWidgets.QLabel(subtitle or title)
        head.setObjectName("status")
        head.setWordWrap(True)
        head.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        lay.addWidget(head)

        edit = QtWidgets.QPlainTextEdit()
        edit.setReadOnly(True)
        edit.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        font.setPointSize(10)
        edit.setFont(font)
        edit.setStyleSheet(
            "QPlainTextEdit { background:#1a1a1a; color:#ddd; border:1px solid #333; }")
        edit.setPlainText(text)
        self._hl = _UsdHighlighter(edit.document())
        lay.addWidget(edit, 1)


class LayerStackPanel(QtWidgets.QWidget):
    """Top-level window: layer hierarchy on top, selected-prim opinions below.

    `api` is the usdview API object (usdviewApi) -- used only to read `stage`, the
    focus prim, and the selection-changed signal.
    """

    def __init__(self, api, parent=None):
        super().__init__(parent)
        self._api = api
        self._follow_selection = True
        self._listeners = []

        self.setWindowFlag(QtCore.Qt.Window, True)
        self.setWindowTitle("Layer Stack")
        self.resize(560, 640)
        self.setStyleSheet(_STYLE)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        bar = QtWidgets.QHBoxLayout()
        self.refresh_btn = QtWidgets.QPushButton("⟳ Refresh")
        self.follow_btn = QtWidgets.QPushButton("Follow selection")
        self.follow_btn.setCheckable(True)
        self.follow_btn.setChecked(True)
        self.expand_btn = QtWidgets.QPushButton("Expand all")
        bar.addWidget(self.refresh_btn)
        bar.addWidget(self.follow_btn)
        bar.addWidget(self.expand_btn)
        bar.addStretch()
        root.addLayout(bar)

        self.status = QtWidgets.QLabel("")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        split = QtWidgets.QSplitter(QtCore.Qt.Vertical)

        self.layer_tree = QtWidgets.QTreeWidget()
        self.layer_tree.setColumnCount(2)
        self.layer_tree.setHeaderLabels(["Layer  (strongest on top)", "Status"])
        self.layer_tree.setAlternatingRowColors(True)
        self.layer_tree.header().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        self.layer_tree.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        split.addWidget(self.layer_tree)

        self.prim_tree = QtWidgets.QTreeWidget()
        self.prim_tree.setColumnCount(2)
        self.prim_tree.setHeaderLabels(["Selected prim — who wins", "Layer"])
        self.prim_tree.setAlternatingRowColors(True)
        self.prim_tree.header().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        self.prim_tree.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        split.addWidget(self.prim_tree)
        split.setSizes([360, 240])
        root.addWidget(split, 1)

        self.layer_tree.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.layer_tree.customContextMenuRequested.connect(self._on_layer_menu)

        self.refresh_btn.clicked.connect(self.refresh)
        self.follow_btn.toggled.connect(self._on_follow_toggled)
        self.expand_btn.clicked.connect(lambda: (self.layer_tree.expandAll(),
                                                 self.prim_tree.expandAll()))

        self._debounce = QtCore.QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(150)
        self._debounce.timeout.connect(self.refresh)

        self._connect_signals()
        QtCore.QTimer.singleShot(0, self.refresh)

    # ------------------------------------------------------------ live refresh
    def _connect_signals(self):
        # Recompose/layer edits -- refresh (debounced so a burst collapses to one).
        for notice in (Usd.Notice.StageContentsChanged, Usd.Notice.ObjectsChanged):
            try:
                self._listeners.append(Tf.Notice.RegisterGlobally(notice, self._on_usd_notice))
            except Exception:
                pass
        # Selection changes from usdview.
        try:
            self._api.dataModel.selection.signalPrimSelectionChanged.connect(
                self._on_selection_changed)
        except Exception:
            pass

    def _on_usd_notice(self, notice, sender):
        self._debounce.start()

    def _on_selection_changed(self, *args):
        if self._follow_selection:
            self._debounce.start()

    def _on_follow_toggled(self, on):
        self._follow_selection = on
        self.refresh()

    @override
    def closeEvent(self, event):
        # Drop the Tf listeners so we don't fire into a dead widget.
        for lst in self._listeners:
            try:
                lst.Revoke()
            except Exception:
                pass
        self._listeners = []
        super().closeEvent(event)

    # ------------------------------------------------------------------- data
    def _get_stage(self):
        try:
            return self._api.stage
        except Exception:
            return None

    def _get_prim(self):
        try:
            p = self._api.prim
            if p and p.IsValid():
                return p
        except Exception:
            pass
        try:
            return self._api.dataModel.selection.getFocusPrim()
        except Exception:
            return None

    # -------------------------------------------------------------- rendering
    def refresh(self):
        """Rebuild both trees from the current stage."""
        stage = self._get_stage()
        self.layer_tree.clear()
        self.prim_tree.clear()
        if stage is None:
            self.status.setText("No stage.")
            return
        try:
            self._build_layer_tree(stage)
            self._build_prim_tree(stage)
        except Exception as e:
            self.status.setText(f"Error: {e}")

    def _build_layer_tree(self, stage):
        edit_layer = stage.GetEditTarget().GetLayer()
        try:
            ordered = stage.GetLayerStack(includeSessionLayers=True)
            rank = {lyr.identifier: i for i, lyr in enumerate(ordered)}
        except Exception:
            rank = {}

        n_muted = len(stage.GetMutedLayers())
        self.status.setText(
            f"{len(stage.GetUsedLayers())} layers used · {n_muted} muted · "
            f"edit target: {_display_name(edit_layer)}"
        )

        session = stage.GetSessionLayer()
        seen = set()
        if session is not None:
            self._add_layer_item(self.layer_tree.invisibleRootItem(), session,
                                 stage, edit_layer, rank, seen, tag="session")
        self._add_layer_item(self.layer_tree.invisibleRootItem(), stage.GetRootLayer(),
                             stage, edit_layer, rank, seen, tag="root")
        self.layer_tree.expandAll()

    def _add_layer_item(self, parent, layer, stage, edit_layer, rank, seen, tag=""):
        item = QtWidgets.QTreeWidgetItem(parent)
        muted = self._is_muted(stage, layer)
        is_edit = layer is edit_layer

        badges = []
        r = rank.get(layer.identifier)
        if r is not None:
            badges.append(f"#{r}")
        if tag:
            badges.append(tag)
        if layer.anonymous:
            badges.append("anon")
        if is_edit:
            badges.append("✎ EDIT TARGET")
        if muted:
            badges.append("MUTED")
        if not layer.anonymous and layer.dirty:
            badges.append("unsaved*")

        item.setText(0, _display_name(layer))
        item.setText(1, "  ".join(badges))
        item.setToolTip(0, _short_id(layer))
        item.setData(0, QtCore.Qt.ItemDataRole.UserRole, layer.identifier)

        color = None
        if muted:
            color = QtGui.QColor(_COL_MUTED)
            f = item.font(0)
            f.setStrikeOut(True)
            item.setFont(0, f)
        elif is_edit:
            color = QtGui.QColor(_COL_EDIT)
            f = item.font(0)
            f.setBold(True)
            item.setFont(0, f)
        elif tag == "session":
            color = QtGui.QColor(_COL_SESSION)
        elif layer.anonymous:
            color = QtGui.QColor(_COL_ANON)
        if color is not None:
            item.setForeground(0, color)
            item.setForeground(1, color)

        # recurse into sublayers (subLayerPaths[0] is strongest → keep order)
        if layer.identifier not in seen:
            seen.add(layer.identifier)
            for sub_path in layer.subLayerPaths:
                sub = Sdf.Layer.FindRelativeToLayer(layer, sub_path)
                if sub is None:
                    try:
                        sub = Sdf.Layer.FindOrOpen(layer.ComputeAbsolutePath(sub_path))
                    except Exception:
                        sub = None
                if sub is not None:
                    self._add_layer_item(item, sub, stage, edit_layer, rank, seen)
                else:
                    miss = QtWidgets.QTreeWidgetItem(item)
                    miss.setText(0, sub_path)
                    miss.setText(1, "not found")
                    miss.setForeground(0, QtGui.QColor(_COL_DIM))
        return item

    @staticmethod
    def _is_muted(stage, layer):
        try:
            if stage.IsLayerMuted(layer.identifier):
                return True
        except Exception:
            pass
        try:
            return layer.IsMuted()
        except Exception:
            return False

    def _build_prim_tree(self, stage):
        prim = self._get_prim() if self._follow_selection else None
        if prim is None or not prim.IsValid():
            hint = "Select a prim to see its winning opinions."
            if not self._follow_selection:
                hint = "Follow selection is off."
            head = QtWidgets.QTreeWidgetItem(self.prim_tree)
            head.setText(0, hint)
            head.setForeground(0, QtGui.QColor(_COL_DIM))
            self.prim_tree.setHeaderLabels(["Selected prim — who wins", "Layer"])
            return

        self.prim_tree.setHeaderLabels([f"{prim.GetPath()} — who wins", "Layer"])

        arcs_root = QtWidgets.QTreeWidgetItem(self.prim_tree)
        arcs_root.setText(0, "Composition arcs (strongest first)")
        arcs_root.setForeground(0, QtGui.QColor(_COL_DIM))
        try:
            query = Usd.PrimCompositionQuery(prim)
            for arc in query.GetCompositionArcs():
                it = QtWidgets.QTreeWidgetItem(arcs_root)
                label = _ARC_LABEL.get(arc.GetArcType(), str(arc.GetArcType()))
                it.setText(0, f"{label}  →  {arc.GetTargetPrimPath()}")
                lyr = arc.GetTargetLayer()
                it.setText(1, _display_name(lyr))
                it.setToolTip(1, _short_id(lyr))
                if not arc.HasSpecs():
                    it.setForeground(0, QtGui.QColor(_COL_DIM))
        except Exception as e:
            QtWidgets.QTreeWidgetItem(arcs_root).setText(0, f"(composition query failed: {e})")

        props_root = QtWidgets.QTreeWidgetItem(self.prim_tree)
        props_root.setText(0, "Authored attributes (winner → layer)")
        props_root.setForeground(0, QtGui.QColor(_COL_DIM))
        for attr in prim.GetAuthoredAttributes():
            try:
                stack = attr.GetPropertyStack(Usd.TimeCode.Default())
            except Exception:
                stack = []
            if not stack:
                continue
            it = QtWidgets.QTreeWidgetItem(props_root)
            it.setText(0, attr.GetName())
            it.setText(1, _display_name(stack[0].layer))
            it.setToolTip(1, _short_id(stack[0].layer))
            it.setForeground(1, QtGui.QColor(_COL_WIN))
            if len(stack) > 1:
                for i, spec in enumerate(stack):
                    sub = QtWidgets.QTreeWidgetItem(it)
                    sub.setText(0, "  wins ✓" if i == 0 else "  overridden")
                    sub.setText(1, _display_name(spec.layer))
                    sub.setToolTip(1, _short_id(spec.layer))
                    c = _COL_WIN if i == 0 else _COL_DIM
                    sub.setForeground(0, QtGui.QColor(c))
                    sub.setForeground(1, QtGui.QColor(c))
        self.prim_tree.expandItem(arcs_root)
        self.prim_tree.expandItem(props_root)

    # --------------------------------------------------------- inspect layer
    def _on_layer_menu(self, pos):
        item = self.layer_tree.itemAt(pos)
        if item is None:
            return
        ident = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        menu = QtWidgets.QMenu(self)
        act_inspect = menu.addAction("Inspect layer (USD text)")
        act_copy = menu.addAction("Copy identifier")
        if not ident:
            act_inspect.setEnabled(False)
            act_copy.setEnabled(False)
        res = menu.exec(self.layer_tree.viewport().mapToGlobal(pos))
        if res is None:
            return
        if res == act_inspect:
            self._inspect_layer(ident)
        elif res == act_copy:
            QtWidgets.QApplication.clipboard().setText(ident)

    def _inspect_layer(self, identifier):
        if not identifier:
            return
        layer = Sdf.Layer.Find(identifier)
        if layer is None:
            try:
                layer = Sdf.Layer.FindOrOpen(identifier)
            except Exception:
                layer = None
        if layer is None:
            self.status.setText(f"Cannot open layer: {identifier}")
            return
        try:
            text = layer.ExportToString() or "(empty layer)"
        except Exception as e:
            text = f"(failed to export layer: {e})"
        dlg = _LayerTextDialog(_display_name(layer), _short_id(layer), text, self)
        dlg.show()
        dlg.raise_()


def open_window(api):
    """Create and raise the Layer Stack window.

    Args:
        api: The usdview API object.

    Returns:
        The window, so the caller can keep a reference alive.
    """
    parent = getattr(api, "qMainWindow", None)
    win = LayerStackPanel(api, parent=parent)
    win.show()
    win.raise_()
    win.activateWindow()
    return win
