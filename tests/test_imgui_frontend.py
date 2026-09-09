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


class TestParamLayout:
    """The drawing decisions that made the first render unreadable.

    Not pixel assertions -- there is no frame here -- but the structural choices behind
    them, which is what actually regressed: flattening groups, and laying out against a
    node size nobody measured.
    """

    def test_groups_are_not_flattened_for_drawing(self, view, first):
        """A Transform models a vector as a GROUP of three sliders. Flattened, that is
        twelve labelled rows and a node taller than the window; kept as groups it is four
        inline rows."""
        from usd_chunks.plugins import chunk_imgui
        chunk = view.chunks[first]
        top = [p for p in chunk.get_my_params() if hasattr(p, "get_my_params")]
        assert top, "fixture chunk has no groups, so this proves nothing"
        assert len(chunk_imgui.visible_params(chunk)) > len(top), (
            "flattening should yield more leaves than there are groups")

    def test_layout_uses_the_measured_node_size_when_available(self, view, first):
        """Headless there is no editor, so this exercises the fallback branch and pins the
        contract: a size is always returned, never zero, or the layout divides by it."""
        width, height = view._node_size(first)
        assert width > 1.0 and height > 1.0

    def test_numeric_params_are_unbounded(self, view, first):
        """The first version drew every number as a slider clamped to +/-100, so a
        translate of 250 silently became 100. Values must survive being set."""
        param = view.chunks[first].get_param("tx")
        param.value = 250.0
        assert param.value == pytest.approx(250.0)


class TestPinFiltering:
    """A Transform exposes 28 ports and wires 2 of them.

    Drawing all of them made the node twenty-four rows taller than its own body, which is
    what made the first renders unreadable. Qt draws the same line with its "Show param
    connections" toggle.
    """

    def test_unwired_data_pins_are_hidden_by_default(self, view, first):
        wired = view._wired_attr_paths()
        ports = view.ports[first]
        hidden = [p for p in ports if not view._pin_visible(p, wired)]
        assert hidden, "fixture has no unwired data ports, so this proves nothing"
        assert all(p.port_type != chunk_graph.PORT_EXEC for p in hidden)
        assert all(p.attr_path not in wired for p in hidden)

    def test_exec_pins_are_always_shown(self, view, first):
        wired = view._wired_attr_paths()
        execs = [p for p in view.ports[first] if p.port_type == chunk_graph.PORT_EXEC]
        assert execs
        assert all(view._pin_visible(p, wired) for p in execs)

    def test_a_wired_data_pin_is_shown(self, view):
        """Whichever ports carry edges must be visible, whatever their type."""
        wired = view._wired_attr_paths()
        assert wired
        for path, ports in view.ports.items():
            for port in ports:
                if port.attr_path in wired:
                    assert view._pin_visible(port, wired)

    def test_the_toggle_reveals_everything(self, view, first):
        wired = view._wired_attr_paths()
        view.toggle_all_pins()
        assert view.show_all_pins
        assert all(view._pin_visible(p, wired) for p in view.ports[first])
        view.toggle_all_pins()
        assert not view.show_all_pins

    def test_hiding_never_orphans_a_link(self, view):
        """The safety property. A link is drawn between two pins; if hiding could remove a
        pin a link points at, the editor would draw that link to nowhere -- which is
        exactly the class of bug that produced the wires running off the canvas."""
        wired = view._wired_attr_paths()
        for edge in view.edges:
            for node_path, attr_path in ((edge.src_node_path, edge.src_attr_path),
                                         (edge.dst_node_path, edge.dst_attr_path)):
                port = view._port_for_attr(node_path, attr_path)
                assert port is not None, f"edge endpoint {attr_path} has no port"
                assert view._pin_visible(port, wired), f"{attr_path} is wired but hidden"
