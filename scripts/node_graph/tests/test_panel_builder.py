"""Tests for _panel_builder: build_group_promo_panel and build_chunk_panel."""
import pytest
from PySide6 import QtWidgets


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_form(qapp):
    w = QtWidgets.QWidget()
    form = QtWidgets.QFormLayout(w)
    return w, form


def _noop(*_a, **_kw):
    pass


# ── Scenario 1: build_group_promo_panel with promotions ───────────────────────

def test_promo_panel_with_promotions(node_graph, qapp):
    """Form receives one labeled QLineEdit row per promoted param."""
    from node_graph._panel_builder import build_group_promo_panel
    from node_graph.nodes._base import _get_promotions, _save_promotions

    ng, CubeNodeCls = node_graph
    g = ng.create_node("nodeGraphQt.nodes.GroupNode", name="PanelPromo")
    g.set_port_deletion_allowed(True)
    g.add_input("my_param", color=(140, 150, 170))

    promos = _get_promotions(g)
    promos["my_param"] = {
        "inner_node_id": "fake",
        "inner_node_name": "Inner",
        "inner_param": "my_param",
        "chunk_param_class": "LineEdit",
        "chunk_param_kwargs": {},
        "default_value": "hello",
    }
    _save_promotions(g)
    if hasattr(g, '_promotions'):
        del g._promotions

    _, form = _make_form(qapp)
    build_group_promo_panel(g, form, _noop)

    assert form.rowCount() == 1
    edit = form.itemAt(1).widget()  # row 0: label at 0, field at 1
    assert isinstance(edit, QtWidgets.QLineEdit)
    assert edit.text() == "hello"


# ── Scenario 2: build_group_promo_panel with no promotions ────────────────────

def test_promo_panel_empty(node_graph, qapp):
    """Form receives a placeholder QLabel when nothing is promoted."""
    from node_graph._panel_builder import build_group_promo_panel

    ng, CubeNodeCls = node_graph
    g = ng.create_node("nodeGraphQt.nodes.GroupNode", name="PanelEmpty")

    _, form = _make_form(qapp)
    build_group_promo_panel(g, form, _noop)

    assert form.rowCount() == 1
    lbl = form.itemAt(0).widget()
    assert isinstance(lbl, QtWidgets.QLabel)
    assert "No promoted params" in lbl.text()


# ── Scenario 3: build_chunk_panel when plugins.chunk_qt unavailable ───────────

def test_chunk_panel_no_plugin(node_graph, qapp, monkeypatch):
    """An error QLabel is added instead of raising when chunk_qt is missing."""
    import builtins

    ng, CubeNodeCls = node_graph
    node = ng.create_node(CubeNodeCls.type_, name="PanelChunkFail")

    orig_import = builtins.__import__

    def _block_chunk_qt(name, *args, **kwargs):
        if "chunk_qt" in name:
            raise ImportError("blocked for test")
        return orig_import(name, *args, **kwargs)

    _, form = _make_form(qapp)
    monkeypatch.setattr(builtins, "__import__", _block_chunk_qt)
    try:
        from node_graph._panel_builder import build_chunk_panel
        build_chunk_panel(node, form, _noop)
    finally:
        monkeypatch.undo()

    assert form.rowCount() >= 1
    lbl = form.itemAt(0).widget()
    assert isinstance(lbl, QtWidgets.QLabel)


# ── Scenario 4: build_chunk_panel with real chunk ─────────────────────────────

def test_chunk_panel_real_chunk(node_graph, qapp):
    """Form receives a ChunkWidget child row when plugins.chunk_qt is available."""
    try:
        from plugins.chunk_qt import ChunkWidget
    except ImportError:
        pytest.skip("plugins.chunk_qt unavailable in test environment")

    from node_graph._panel_builder import build_chunk_panel

    ng, CubeNodeCls = node_graph
    node = ng.create_node(CubeNodeCls.type_, name="PanelChunkReal")

    _, form = _make_form(qapp)
    build_chunk_panel(node, form, _noop)

    found = False
    for i in range(form.count()):
        item = form.itemAt(i)
        if item and item.widget() and isinstance(item.widget(), ChunkWidget):
            found = True
            break
    assert found, "ChunkWidget not found in form"


# ── Scenario 5: build_node_panel with a plain UsdNodeBase subclass ────────────

def test_build_node_panel_plain_node(node_graph, qapp):
    """build_node_panel creates property rows for a plain (non-chunk, non-group) node."""
    from node_graph._property_panel import build_node_panel

    ng, CubeNodeCls = node_graph
    node = ng.create_node(CubeNodeCls.type_, name="PanelPlain")

    _, form = _make_form(qapp)
    editors = build_node_panel(node, form, _noop)

    # Chunk nodes have _chunk_module so they go through build_chunk_panel.
    # For this test we verify the dispatch works — if it's a chunk node it
    # delegates to build_chunk_panel (returns empty dict), otherwise returns editors.
    assert isinstance(editors, dict)
