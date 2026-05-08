"""PropertyPanelFactory — builds the side-panel property rows for any node.

Public interface:
    build_node_panel(node, form, on_changed, active_graph_fn, ref_menu_fn, get_prim_attrs_fn)

Dispatches on node type:
  - GroupNode → build_group_promo_panel
  - Chunk node → build_chunk_panel
  - Otherwise → property-type rows (code, file_path, bool, combo, line edit)
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6 import QtWidgets, QtCore, QtGui
from NodeGraphQt import GroupNode
from NodeGraphQt.base.graph import SubGraph

from ._panel_builder import build_group_promo_panel, build_chunk_panel
from ._graph_ui import _CodeEditor, _PythonHighlighter, add_field_completer
from .nodes._base import _get_promotions, _TriggerSignal


def build_node_panel(
    node,
    form: QtWidgets.QFormLayout,
    on_changed: Callable,
    active_graph_fn: Callable | None = None,
    ref_menu_fn: Callable | None = None,
    get_prim_attrs_fn: Callable | None = None,
) -> dict[str, QtWidgets.QWidget]:
    """Build property rows for *node* into *form*. Returns editor dict.

    Parameters
    ----------
    on_changed : called with (node) after any value commit.
    active_graph_fn : returns the currently active graph (main or SubGraph).
    ref_menu_fn : installs @-reference right-click menu on a QLineEdit.
    get_prim_attrs_fn : returns list of attribute names for a node's prim.
    """
    if isinstance(node, GroupNode):
        build_group_promo_panel(node, form, on_changed)
        return {}

    if not hasattr(node, "get_input"):
        return {}

    if hasattr(node, "_chunk_module"):
        build_chunk_panel(
            node,
            form,
            on_changed,
            active_graph_fn=active_graph_fn,
            ref_menu_fn=ref_menu_fn,
        )
        return {}

    return _build_property_rows(
        node, form, on_changed, active_graph_fn, ref_menu_fn, get_prim_attrs_fn
    )


def _build_property_rows(
    node,
    form: QtWidgets.QFormLayout,
    on_changed: Callable,
    active_graph_fn: Callable | None,
    ref_menu_fn: Callable | None,
    get_prim_attrs_fn: Callable | None,
) -> dict[str, QtWidgets.QWidget]:
    """Create one widget row per custom property on *node*."""
    from . import nodes  # local to avoid circular import

    skip = {
        "id",
        "name",
        "color",
        "disabled",
        "selected",
        "width",
        "height",
        "icon",
        "pos",
        "layout_direction",
        "type_",
        "promoted_params",
        "promoted_outputs",
    }

    promoted_params: set[str] = set()
    if active_graph_fn:
        try:
            g = active_graph_fn()
            if isinstance(g, SubGraph):
                for _pname, _info in _get_promotions(g.node).items():
                    if _info.get("inner_node_id") == node.id:
                        promoted_params.add(_info.get("inner_param", ""))
        except Exception:
            pass

    editors: dict[str, QtWidgets.QWidget] = {}

    for name, val in node.model.custom_properties.items():
        if name in skip:
            continue

        if name == "code":
            _add_code_row(node, name, val, form, on_changed, editors)
        elif isinstance(val, bool):
            _add_bool_row(node, name, val, form, on_changed, promoted_params, editors)
        elif name == "file_path" and isinstance(node, nodes.TextureFileNode):
            _add_file_path_row(node, name, val, form, on_changed, ref_menu_fn, editors)
        else:
            _add_generic_row(
                node,
                name,
                val,
                form,
                on_changed,
                promoted_params,
                ref_menu_fn,
                get_prim_attrs_fn,
                editors,
            )

    return editors


# ── Row builders ──────────────────────────────────────────────────────────────


def _add_code_row(node, name, val, form, on_changed, editors):
    code_widget = QtWidgets.QWidget()
    code_layout = QtWidgets.QVBoxLayout(code_widget)
    code_layout.setContentsMargins(0, 0, 0, 0)

    edit = _CodeEditor()
    edit._node_ref = node
    edit.setPlainText(str(val) if val else "")
    edit.setMinimumHeight(120)
    edit.setFont(QtGui.QFont("Consolas", 9))
    edit.setStyleSheet(
        "QPlainTextEdit { background: #282c34; color: #abb2bf;"
        " border: 1px solid #3e4451; border-radius: 4px; padding: 4px; }"
    )
    _PythonHighlighter(edit.document())
    edit.setProperty("prop_name", name)
    edit.textChanged.connect(lambda e=edit, n=node: _on_code_changed(e, n, on_changed))
    code_layout.addWidget(edit)

    btn_row = QtWidgets.QHBoxLayout()
    save_snip = QtWidgets.QPushButton("💾 Save Snippet")
    load_snip = QtWidgets.QPushButton("📂 Load Snippet")

    def _save_snippet(e=edit) -> None:
        p, _ = QtWidgets.QFileDialog.getSaveFileName(
            None, "Save Snippet", "", "Python (*.py)"
        )
        if p:
            with open(p, "w", encoding="utf-8") as f:
                f.write(e.toPlainText())

    def _load_snippet(e=edit) -> None:
        p, _ = QtWidgets.QFileDialog.getOpenFileName(
            None, "Load Snippet", "", "Python (*.py)"
        )
        if p:
            with open(p, encoding="utf-8") as f:
                e.setPlainText(f.read())

    save_snip.clicked.connect(_save_snippet)
    load_snip.clicked.connect(_load_snippet)
    btn_row.addWidget(save_snip)
    btn_row.addWidget(load_snip)
    code_layout.addLayout(btn_row)
    form.addRow(name, code_widget)
    editors[name] = edit


def _add_bool_row(node, name, val, form, on_changed, promoted_params, editors):
    cb = QtWidgets.QCheckBox()
    cb.setChecked(val)
    cb.setProperty("prop_name", name)
    cb.toggled.connect(
        lambda state, n=name, nd=node: _on_bool_changed(nd, n, state, on_changed)
    )
    if name in promoted_params:
        cb.setEnabled(False)
        cb.setToolTip("Promoted to group — change value on the group node")
    form.addRow(name, cb)
    editors[name] = cb


def _add_file_path_row(node, name, val, form, on_changed, ref_menu_fn, editors):
    row = QtWidgets.QWidget()
    row_layout = QtWidgets.QVBoxLayout(row)
    row_layout.setContentsMargins(0, 0, 0, 0)

    file_row = QtWidgets.QHBoxLayout()
    edit = QtWidgets.QLineEdit(str(val) if val else "")
    edit.setProperty("prop_name", name)
    edit.editingFinished.connect(
        lambda nd=node, e=edit: _on_prop_committed(nd, e, on_changed)
    )
    if ref_menu_fn:
        ref_menu_fn(edit, node, name)

    browse = QtWidgets.QPushButton("📁")
    browse.setFixedWidth(30)
    thumb = QtWidgets.QLabel()
    thumb.setFixedSize(200, 200)
    thumb.setStyleSheet("background: #1a1a1a; border: 1px solid #333;")
    thumb.setAlignment(QtCore.Qt.AlignCenter)

    def _make_browse(ed, th, nd) -> None:
        def _fn() -> None:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                None,
                "Select Texture",
                "",
                "Images (*.png *.jpg *.jpeg *.exr *.tga *.bmp)",
            )
            if path:
                ed.setText(path)
                ed.editingFinished.emit()
                _show_thumbnail(th, path)
                if hasattr(nd, "_thumb"):
                    nd._thumb.set_value(path)

        return _fn

    browse.clicked.connect(_make_browse(edit, thumb, node))
    file_row.addWidget(edit)
    file_row.addWidget(browse)
    row_layout.addLayout(file_row)
    row_layout.addWidget(thumb)
    if val:
        _show_thumbnail(thumb, str(val))
    form.addRow(name, row)
    editors[name] = edit


def _add_generic_row(
    node,
    name,
    val,
    form,
    on_changed,
    promoted_params,
    ref_menu_fn,
    get_prim_attrs_fn,
    editors,
):
    # Check if this property is a combo menu on the node
    node_widget = (
        node.view._widgets.get(name)
        if hasattr(node, "view") and hasattr(node.view, "_widgets")
        else None
    )
    if node_widget and getattr(node_widget, "type_", "") == "ComboNodeWidget":
        inner = node_widget.get_custom_widget()
        combo = QtWidgets.QComboBox()
        for i in range(inner.count()):
            combo.addItem(inner.itemText(i))
        if val:
            combo.setCurrentText(str(val))
        combo.setProperty("prop_name", name)
        combo.currentTextChanged.connect(
            lambda text, n=name, nd=node: _on_combo_changed(nd, n, text, on_changed)
        )
        form.addRow(name, combo)
        editors[name] = combo
        return

    edit = QtWidgets.QLineEdit(str(val) if val else "")
    edit.setProperty("prop_name", name)
    edit.editingFinished.connect(
        lambda nd=node, e=edit: _on_prop_committed(nd, e, on_changed)
    )
    if ref_menu_fn:
        ref_menu_fn(edit, node, name)

    if name in promoted_params:
        edit.setEnabled(False)
        edit.setToolTip("Promoted to group — change value on the group node")

    # Check if driven by auto-port
    display_name = name
    auto_port = node.get_input(f"_{name}")
    if auto_port and auto_port.connected_ports():
        src_port = auto_port.connected_ports()[0]
        src_node = src_port.node()
        driven_val = (
            src_node._output_cache.get(src_port.name())
            if hasattr(src_node, "_output_cache")
            else None
        )
        if driven_val is not None and not isinstance(driven_val, _TriggerSignal):
            edit.setText(str(driven_val))
            edit.setReadOnly(True)
            edit.setStyleSheet(
                "QLineEdit { background: rgba(50,210,220,0.08); color: #32d2dc;"
                " border: 1px solid rgba(50,210,220,0.3); border-radius: 4px;"
                " padding: 3px 6px; font: 10px 'Consolas'; }"
            )
            display_name = f"⭐ {name}"

    if name == "attr_name":
        combo = QtWidgets.QComboBox()
        combo.setEditable(True)
        attrs = ["name", "path", "type"]
        if get_prim_attrs_fn:
            attrs += get_prim_attrs_fn(node)
        combo.addItems(attrs)
        if val:
            combo.setCurrentText(str(val))
        combo.setProperty("prop_name", name)
        combo.currentTextChanged.connect(
            lambda text, n=name, nd=node: _on_combo_changed(nd, n, text, on_changed)
        )
        form.addRow(name, combo)
        editors[name] = combo
        return

    if name in (
        "prim_path",
        "set_name",
        "type_filter",
        "prim_type",
        "purpose",
        "material_path",
    ):
        add_field_completer(edit, name, node)
    edit.setPlaceholderText("Type to search...")

    form.addRow(display_name, edit)
    editors[name] = edit


# ── Value-commit helpers ──────────────────────────────────────────────────────


def _on_code_changed(edit, node, on_changed):
    name = edit.property("prop_name")
    node.set_property(name, edit.toPlainText())
    on_changed(node)


def _on_bool_changed(node, name, state, on_changed):
    node.set_property(name, bool(state))
    on_changed(node)


def _on_combo_changed(node, name, text, on_changed):
    node.set_property(name, text)
    on_changed(node)


def _on_prop_committed(node, edit, on_changed):
    name = edit.property("prop_name")
    text = edit.text()
    current = node.model.custom_properties.get(name)
    if isinstance(current, bool):
        node.set_property(name, text.strip().lower() in ("true", "1", "yes"))
    else:
        node.set_property(name, text)
    on_changed(node)


def _show_thumbnail(label, path):
    pixmap = QtGui.QPixmap(path)
    if not pixmap.isNull():
        label.setPixmap(
            pixmap.scaled(
                200, 200, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation
            )
        )
    else:
        label.setText("No preview")
