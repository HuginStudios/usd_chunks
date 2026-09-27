"""Copy, cut, paste, duplicate in the Flet graph.

These exercise the operations through the same clipboard the Qt graph uses -- the format
lives in chunk_graph now, and both frontends read the same two module-level lists.
"""
from __future__ import annotations

import pytest

# flet is an optional extra (pyproject: [flet]), so this file steps aside rather than
# erroring the whole collection when it is not installed -- which is what a module-level
# `import flet` did. Every import below is deliberately after it.
pytest.importorskip("flet", reason="flet is an optional extra")


import flet as ft  # noqa: E402

from graph_fixtures import content_nodes, make_network  # noqa: E402
from usd_chunks.frontends.flet import nodegraph as fg  # noqa: E402
from usd_chunks import chunk_graph  # noqa: E402


@pytest.fixture
def view(tmp_path):
    path, context = make_network(tmp_path / "clip.usda")
    v = fg.GraphView(path, context)
    v.live = False
    chunk_graph.node_clipboard.clear()
    chunk_graph.edge_clipboard.clear()
    return v


@pytest.fixture
def first(view):
    return content_nodes(view)[0]


def key(name, ctrl=False):
    return ft.KeyboardEvent(name="keyboard", control=None, key=name,
                            shift=False, ctrl=ctrl, alt=False, meta=False)


class TestCopyPaste:
    def test_copy_fills_the_shared_clipboard(self, view, first):
        view.set_selection([first])
        view.copy_selection()
        assert len(chunk_graph.node_clipboard) == 1
        assert chunk_graph.node_clipboard[0].module_name == "transform"

    def test_copy_with_nothing_selected_is_a_no_op(self, view):
        view.copy_selection()
        assert chunk_graph.node_clipboard == []
        assert "nothing" in view.status

    def test_paste_creates_a_new_node(self, view, first):
        view.set_selection([first])
        view.copy_selection()
        before = set(view.prims)
        view.paste_at(600.0, 300.0)
        added = set(view.prims) - before
        assert len(added) == 1

    def test_paste_lands_where_asked(self, view, first):
        view.set_selection([first])
        view.copy_selection()
        before = set(view.prims)
        view.paste_at(600.0, 300.0)
        path = (set(view.prims) - before).pop()
        assert view.positions[path] == pytest.approx((600.0, 300.0), abs=1.0)

    def test_paste_carries_param_values(self, view, first):
        """The point of a copy: the new chunk is configured like the old one, not blank."""
        chunk = view.chunks[first]
        chunk.get_param("tx").value = 7.5
        view.set_selection([first])
        view.copy_selection()
        before = set(view.prims)
        view.paste_at(600.0, 300.0)
        path = (set(view.prims) - before).pop()
        assert view.chunks[path].get_param("tx").value == pytest.approx(7.5)

    def test_paste_selects_what_it_pasted(self, view, first):
        view.set_selection([first])
        view.copy_selection()
        before = set(view.prims)
        view.paste_at(600.0, 300.0)
        assert view.selected_nodes == set(view.prims) - before

    def test_an_empty_clipboard_pastes_nothing(self, view):
        before = set(view.prims)
        view.paste_at(600.0, 300.0)
        assert set(view.prims) == before


class TestWiring:
    def test_a_wire_between_two_copied_nodes_survives(self, view):
        """Both ends copied, so the wire is recreated between the two NEW nodes."""
        view.set_selection(content_nodes(view))
        view.copy_selection()
        assert len(chunk_graph.edge_clipboard) >= 1, "fixture has no wire to copy"
        before = set(view.prims)
        view.paste_at(200.0, 600.0)
        added = set(view.prims) - before
        assert len(added) == 2
        pasted_edges = [e for e in view.edges
                        if e.src_node_path in added and e.dst_node_path in added]
        assert pasted_edges, "the copied wire was not recreated"

    def test_a_wire_to_an_uncopied_node_is_dropped(self, view):
        """Only one end selected: the wire is not recreated against the original."""
        downstream = content_nodes(view)[1]
        view.set_selection([downstream])
        view.copy_selection()
        assert chunk_graph.edge_clipboard == []


class TestCutAndDuplicate:
    def test_cut_removes_the_node_and_keeps_it_on_the_clipboard(self, view, first):
        view.set_selection([first])
        view.cut_selection()
        assert first not in view.prims
        assert len(chunk_graph.node_clipboard) == 1

    def test_duplicate_adds_a_node_at_an_offset(self, view, first):
        origin = view.positions[first]
        view.set_selection([first])
        before = set(view.prims)
        view.duplicate_selection()
        path = (set(view.prims) - before).pop()
        assert view.positions[path] == pytest.approx((origin[0] + 40, origin[1] + 40), abs=1.0)

    def test_duplicate_leaves_the_clipboard_alone(self, view, first):
        """Duplicating must not clobber whatever was last explicitly copied."""
        second = content_nodes(view)[1]
        view.set_selection([second])
        view.copy_selection()
        kept = list(chunk_graph.node_clipboard)
        view.set_selection([first])
        view.duplicate_selection()
        assert chunk_graph.node_clipboard == kept


class TestShortcuts:
    def test_ctrl_c_then_ctrl_v(self, view, first):
        view.set_selection([first])
        view.on_key(key("C", ctrl=True))
        assert len(chunk_graph.node_clipboard) == 1
        before = set(view.prims)
        view.on_key(key("V", ctrl=True))
        assert set(view.prims) - before

    def test_ctrl_d_duplicates_and_bare_d_disables(self, view, first):
        """The two share a letter; the modifier has to decide which one runs."""
        view.set_selection([first])
        before = set(view.prims)
        view.on_key(key("D", ctrl=True))
        assert set(view.prims) - before, "Ctrl+D did not duplicate"
        assert not chunk_graph.is_disabled(view.prims[first]), "Ctrl+D also disabled"

        view.set_selection([first])
        count = len(view.prims)
        view.on_key(key("D"))
        assert chunk_graph.is_disabled(view.prims[first]), "bare D did not disable"
        assert len(view.prims) == count, "bare D duplicated"

    def test_ctrl_x_cuts(self, view, first):
        view.set_selection([first])
        view.on_key(key("X", ctrl=True))
        assert first not in view.prims


class TestGrouping:
    def test_grouping_replaces_the_selection_with_one_node(self, view):
        view.set_selection(content_nodes(view))
        view.group_selection()
        assert len(content_nodes(view)) == 1
        only = view.prims[content_nodes(view)[0]]
        assert (only.GetAttribute("info:chunkType").Get() or "") == "chunkgroup"

    def test_the_grouped_chunks_live_inside_the_group(self, view):
        view.set_selection(content_nodes(view))
        view.group_selection()
        group_path = content_nodes(view)[0]
        inner = [str(p.GetPath()) for p in view.stage.TraverseAll()
                 if p.GetTypeName() == "Chunk"
                 and str(p.GetPath()).startswith(group_path + "/")]
        assert len(inner) == 2, f"expected 2 chunks inside the group, got {inner}"

    def test_the_group_takes_the_selections_place_in_the_chain(self, view, first):
        """Group only the downstream node: the wire that fed it must now feed the group,
        or grouping leaves a hole in the exec chain."""
        downstream = content_nodes(view)[1]
        view.set_selection([downstream])
        view.group_selection()
        group_path = next(p for p, prim in view.prims.items()
                          if (prim.GetAttribute("info:chunkType").Get() or "") == "chunkgroup")
        wired = [e for e in view.edges
                 if e.is_exec and e.src_node_path == first and e.dst_node_path == group_path]
        assert wired, "the upstream wire was not reconnected to the group"

    def test_grouping_nothing_is_a_no_op(self, view):
        before = set(view.prims)
        view.group_selection()
        assert set(view.prims) == before
        assert "nothing" in view.status

    def test_ctrl_g_groups(self, view):
        view.set_selection(content_nodes(view))
        view.on_key(key("G", ctrl=True))
        assert len(content_nodes(view)) == 1
