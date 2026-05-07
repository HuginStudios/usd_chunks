"""Side-panel builder functions for the node graph.

Each function takes a node, a QFormLayout target, and an `on_changed` callable
that is invoked after any value commit.  No reference to the executor or widget
is held — callers inject the execution coupling via `on_changed`.

Interface:
    build_group_promo_panel(group_node, form, on_changed)
    build_chunk_panel(node, form, on_changed, active_graph_fn=None, ref_menu_fn=None)
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6 import QtWidgets, QtCore
from NodeGraphQt.base.graph import SubGraph

from .chunk_adapter import ChunkAdapter
from .nodes._base import _get_promotions, _get_promoted_outputs, _save_promotions

# ── Shared combo-box styles (injected into ChunkWidget children) ─────────────

_COMBO_POPUP_STYLE = (
    "QAbstractItemView { background: #0e1016; color: #c8d0e8;"
    " border: 1px solid #1e2438;"
    " selection-background-color: #1e2a40; selection-color: #c8d0e8;"
    " outline: none; }"
)
_COMBO_ITEM_STYLE = (
    "QComboBox { background: #111520; color: #c8d0e8;"
    " border: 1px solid #1e2438; border-radius: 3px;"
    " padding: 2px 6px; font: 10px 'Consolas'; }"
    "QComboBox::drop-down { border: none; }"
    "QComboBox::down-arrow { image: none; width: 10px; }"
)


def build_group_promo_panel(
    group_node, form: QtWidgets.QFormLayout, on_changed: Callable
) -> None:
    """Populate *form* with editable fields for each promoted param on *group_node*.

    on_changed(group_node) is called after a value is committed so callers can
    schedule re-execution without this module knowing the executor exists.
    """
    promotions = _get_promotions(group_node)
    promo_outs = _get_promoted_outputs(group_node)

    if not promotions and not promo_outs:
        lbl = QtWidgets.QLabel(
            "No promoted params.\nDouble-click to enter group,\nthen right-click a param → Promote to Group."
        )
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color: #666; font: 10px 'Consolas'; padding: 8px 4px;")
        form.addRow(lbl)
        return

    for port_name, info in promotions.items():
        default_val = info.get("default_value")
        edit = QtWidgets.QLineEdit(str(default_val) if default_val is not None else "")
        edit.setPlaceholderText("Enter value or tab away")

        def _make_commit(pname, grp, e=edit) -> None:
            def _on_commit(pn=pname, gn=grp, ed=e) -> None:
                promos = _get_promotions(gn)
                if pn in promos:
                    promos[pn]["default_value"] = ed.text()
                    _save_promotions(gn)
                    QtCore.QTimer.singleShot(0, lambda: on_changed(gn))

            return _on_commit

        edit.editingFinished.connect(_make_commit(port_name, group_node))
        form.addRow(port_name, edit)

    if promo_outs:
        sep = QtWidgets.QLabel("Promoted Outputs")
        sep.setStyleSheet(
            "color: #666; font: italic 9px 'Consolas'; padding: 6px 0 2px 0;"
        )
        form.addRow(sep)
        for port_name, info in promo_outs.items():
            lbl = QtWidgets.QLabel(f"→ {info.get('inner_port', '?')}")
            lbl.setStyleSheet("color: #888; font: 9px 'Consolas';")
            form.addRow(port_name, lbl)


def build_chunk_panel(
    node,
    form: QtWidgets.QFormLayout,
    on_changed: Callable,
    active_graph_fn: Callable | None = None,
    ref_menu_fn: Callable | None = None,
) -> None:
    """Populate *form* with the real ChunkWidget for a chunk node.

    active_graph_fn() — returns the currently active graph (main or SubGraph).
    ref_menu_fn(widget, node, param) — installs the @-reference right-click menu.
    on_changed(node) — called on any value change.

    Both extra callables default to no-ops so the panel is usable in isolation.
    """
    try:
        from plugins.chunk_qt import ChunkWidget  # optional: not available in tests

        adapter = getattr(node, "_cached_adapter", None)
        if not adapter or not adapter.chunk:
            my_chunk = None
            if hasattr(node, "_ensure_chunk"):
                try:
                    my_chunk = node._ensure_chunk()
                except Exception:
                    my_chunk = None
            adapter = ChunkAdapter(
                node._chunk_display_name,
                module_name=node._chunk_module,
                auto_create=(my_chunk is None),
                chunk=my_chunk,
            )
            node._cached_adapter = adapter
        if not adapter.chunk:
            form.addRow(QtWidgets.QLabel("Chunk not loaded"))
            return
        if hasattr(node, "_subscribe_to_chunk_notices"):
            node._subscribe_to_chunk_notices(adapter.chunk)
        chunk_widget = ChunkWidget(adapter.chunk)
        for _cb in chunk_widget.findChildren(QtWidgets.QComboBox):
            _cb.view().setStyleSheet(_COMBO_POPUP_STYLE)
            _cb.setStyleSheet(_COMBO_ITEM_STYLE)
        if ref_menu_fn:
            for _pw in chunk_widget.findChildren(QtWidgets.QWidget):
                _p = getattr(_pw, "param", None)
                if _p is not None and hasattr(_p, "value_reference"):
                    ref_menu_fn(_pw, node, _p)
        form.addRow(chunk_widget)

        # Lock params that are wired via port or promoted to a group
        connected_params: set[str] = set()
        for port in node.input_ports():
            if port.name().startswith("_") and port.connected_ports():
                connected_params.add(port.name()[1:])
        if active_graph_fn:
            try:
                g = active_graph_fn()
                if isinstance(g, SubGraph):
                    for pname, info in _get_promotions(g.node).items():
                        if info.get("inner_node_id") == node.id:
                            connected_params.add(info.get("inner_param", ""))
            except Exception:
                pass
        if connected_params:
            for child in chunk_widget.findChildren(QtWidgets.QWidget):
                if hasattr(child, "param") and hasattr(child.param, "name"):
                    if child.param.name in connected_params:
                        child.setEnabled(False)
                        child.setToolTip(
                            "Promoted to group — change value on the group node"
                        )
    except Exception as e:
        print(f"[NodeGraph] chunk panel err: {e}")
        form.addRow(QtWidgets.QLabel(f"Error: {e}"))
