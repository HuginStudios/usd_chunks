"""The ImGui frontend does what the contract says.

A third toolkit, chosen because it is a different paradigm from both existing ones:
immediate mode, no widget objects, no retained scene. If the engine were quietly shaped
around Qt and Flet, this is where that would show.

Every verb is exercised headless -- no window, no GL context. That works because the
contract verbs are all engine calls plus USD writes; only drawing and navigation need the
editor, and those are not in the contract.
"""
from __future__ import annotations

import pytest

pytest.importorskip("imgui_bundle", reason="imgui-bundle is an optional extra")

from graph_fixtures import make_network  # noqa: E402
from usd_chunks.chunk_flags import ChunkFlags  # noqa: E402
from usd_chunks.plugins import chunk_graph  # noqa: E402
from usd_chunks.plugins.chunk_imgui_nodegraph import ImGuiGraphView  # noqa: E402


@pytest.fixture
def view(tmp_path):
    path, context = make_network(tmp_path / "imgui.usda")
    v = ImGuiGraphView(path, context)
    chunk_graph.node_clipboard.clear()
    chunk_graph.edge_clipboard.clear()
    assert len(v.prims) == 2, f"fixture built {len(v.prims)} nodes"
    return v


@pytest.fixture
def first(view):
    return sorted(view.prims)[0]


class TestReading:
    def test_it_reads_the_network(self, view):
        assert len(view.prims) == 2
        assert len(view.edges) >= 1

    def test_positions_come_from_usd_before_the_editor_has_seen_them(self, view, first):
        """No editor session headless, so positions fall back to what USD stores --
        converted to canvas space by the engine, like every other frontend."""
        assert view.positions[first] == chunk_graph.graph_position(view.prims[first])


class TestSelection:
    def test_selection_mirrors_onto_the_chunk(self, view, first):
        view.set_selection([first])
        assert view.chunks[first].has_state(ChunkFlags.State.SELECTED)
        view.set_selection(())
        assert not view.chunks[first].has_state(ChunkFlags.State.SELECTED)

    def test_add_extends(self, view):
        a, b = sorted(view.prims)
        view.set_selection([a])
        view.set_selection([b], add=True)
        assert view.selected_nodes == {a, b}


class TestEditing:
    def test_delete(self, view, first):
        view.set_selection([first])
        view.delete_selected()
        assert first not in view.prims

    def test_toggle_disable(self, view, first):
        view.set_selection([first])
        view.toggle_disable_selected()
        assert chunk_graph.is_disabled(view.prims[first])

    def test_copy_uses_the_shared_clipboard(self, view, first):
        """The property that makes three frontends one system: copy here, paste anywhere."""
        view.set_selection([first])
        view.copy_selection()
        assert len(chunk_graph.node_clipboard) == 1
        assert isinstance(chunk_graph.node_clipboard[0], chunk_graph.ClipboardNode)

    def test_paste(self, view, first):
        view.set_selection([first])
        view.copy_selection()
        before = set(view.prims)
        view.paste_at(400.0, 400.0)
        assert len(set(view.prims) - before) == 1

    def test_paste_carries_values(self, view, first):
        view.chunks[first].get_param("tx").value = 3.25
        view.set_selection([first])
        view.copy_selection()
        before = set(view.prims)
        view.paste_at(400.0, 400.0)
        new = (set(view.prims) - before).pop()
        assert view.chunks[new].get_param("tx").value == pytest.approx(3.25)

    def test_duplicate(self, view, first):
        view.set_selection([first])
        before = set(view.prims)
        view.duplicate_selection()
        assert len(set(view.prims) - before) == 1

    def test_cut(self, view, first):
        view.set_selection([first])
        view.cut_selection()
        assert first not in view.prims
        assert len(chunk_graph.node_clipboard) == 1

    def test_create(self, view):
        entry = next(e for e in view.menu_entries() if e.label.lower() == "transform")
        before = set(view.prims)
        view.create_chunk(entry, 600.0, 200.0)
        assert len(set(view.prims) - before) == 1


class TestGroupingAndDiving:
    def test_group_then_dive(self, view):
        view.set_selection(list(view.prims))
        view.group_selection()
        assert len(view.prims) == 1
        group = next(iter(view.prims))
        assert view.can_dive(group)
        view.dive_into(group)
        assert view.context == group
        assert len(view.prims) == 2

    def test_dive_out(self, view):
        outer = view.context
        view.set_selection(list(view.prims))
        view.group_selection()
        group = next(iter(view.prims))
        view.dive_into(group)
        view.dive_out()
        assert view.context == outer

    def test_an_ordinary_node_is_not_divable(self, view, first):
        assert not view.can_dive(first)

    def test_at_the_top_it_says_so(self, view):
        while view.context != chunk_graph.CHUNKS_ROOT:
            before = view.context
            view.dive_out()
            if view.context == before:
                break
        view.dive_out()
        assert "already at the top" in view.status


class TestViewSlots:
    def test_no_slot_by_default(self, view):
        assert view.active_slot() == 0

    def test_switching_to_an_empty_slot_is_refused(self, view):
        view.set_active_slot(2)
        assert view.active_slot() == 0
        assert "no view in slot 2" in view.status


class TestImmediateModeWidgets:
    """The paradigm difference, asserted rather than described.

    Qt and Flet build a widget object per param and keep it alive so a notice can update
    it. This frontend has no objects: draw_param reads param.value every frame. So the
    thing to test is the write-back, not a widget.
    """

    def test_visible_params_flattens_groups_and_drops_hidden(self, view, first):
        from usd_chunks.plugins import chunk_imgui
        params = chunk_imgui.visible_params(view.chunks[first])
        assert params, "no params found"
        assert not any(p.has_state(ChunkFlags.State.HIDDEN) for p in params)
        assert all(not hasattr(p, "get_my_params") for p in params), "a group leaked through"

    def test_there_is_no_widget_factory_to_keep_alive(self):
        """Stated as a test so the absence reads as a decision, not an omission: an
        immediate-mode frontend has nothing to subscribe, so ChunkWidgetFactoryBase does
        not apply to it."""
        from usd_chunks.plugins import chunk_imgui
        assert not hasattr(chunk_imgui, "ChunkImGuiWidgetFactory")
        assert callable(chunk_imgui.draw_param)
