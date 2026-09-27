"""Flet graph interaction, driven through real Flet event objects.

Every test here builds an actual `ft.TapEvent` / `ft.DragUpdateEvent` / `ft.ScrollEvent`
and hands it to the handler the GestureDetector would call. That is the point: the
handlers previously read `event.local_x`, which exists on no Flet 0.80 event class, so
every one of them raised AttributeError on first touch. The source looked implemented and
a hand-rolled fake event with the attributes the code wanted would have agreed with it.
Only the real class disagrees.
"""
from __future__ import annotations

import pytest

# flet is an optional extra (pyproject: [flet]), so this file steps aside rather than
# erroring the whole collection when it is not installed -- which is what a module-level
# `import flet` did. Every import below is deliberately after it.
pytest.importorskip("flet", reason="flet is an optional extra")


import flet as ft  # noqa: E402

from graph_fixtures import content_nodes, make_network  # noqa: E402
from usd_chunks.chunk_flags import ChunkFlags  # noqa: E402
from usd_chunks.plugins import chunk_flet_nodegraph as fg  # noqa: E402
from usd_chunks import chunk_graph  # noqa: E402



@pytest.fixture
def view(tmp_path):
    path, context = make_network(tmp_path / "net.usda")
    v = fg.GraphView(path, context)
    v.live = False  # no page; refresh() must stay a no-op
    assert len(content_nodes(v)) == 2, f"fixture built {len(content_nodes(v))} content nodes"
    return v


@pytest.fixture
def first(view):
    """The upstream node's path. Named rather than hard-coded: add_chunk decides the
    prim name, and asserting on 'Transform1' would break the day it becomes 'Transform2'."""
    return content_nodes(view)[0]


@pytest.fixture
def second(view):
    return content_nodes(view)[1]


def tap(x, y):
    return ft.TapEvent(name="tap_down", control=None, kind=None,
                       local_position=ft.Offset(x, y), global_position=ft.Offset(x, y))


def drag(x, y, dx, dy):
    return ft.DragUpdateEvent(
        name="pan_update", control=None,
        local_position=ft.Offset(x, y), global_position=ft.Offset(x, y),
        local_delta=ft.Offset(dx, dy), global_delta=ft.Offset(dx, dy),
        primary_delta=None, timestamp=0)


def start(x, y):
    return ft.DragStartEvent(name="pan_start", control=None, kind=None, timestamp=0,
                             local_position=ft.Offset(x, y), global_position=ft.Offset(x, y))


def key(name, ctrl=False, shift=False):
    return ft.KeyboardEvent(name="keyboard", control=None, key=name,
                            shift=shift, ctrl=ctrl, alt=False, meta=False)


def scroll(x, y, dy):
    return ft.ScrollEvent(name="scroll", control=None,
                          local_position=ft.Offset(x, y), global_position=ft.Offset(x, y),
                          scroll_delta=ft.Offset(0, dy))


class TestEventsReachTheHandlers:
    def test_a_tap_on_a_port_selects_it(self, view, first):
        """The regression: this raised AttributeError, so nothing was selectable."""
        port = view.ports[first][0]
        x, y = view.port_xy(port)
        view.on_tap(tap(x, y))
        assert view.selected is not None
        assert view.selected.attr_path == port.attr_path

    def test_a_tap_on_empty_space_clears_the_selection(self, view, first):
        view.selected = view.ports[first][0]
        view.on_tap(tap(5.0, 5.0))
        assert view.selected is None

    def test_dragging_a_node_moves_it(self, view, first):
        before = view.positions[first]
        view.on_pan_start(start(*view.positions[first]))
        assert view.dragging == first
        view.on_pan_update(drag(0, 0, 30, 20))
        after = view.positions[first]
        assert (after[0] - before[0], after[1] - before[1]) == (30, 20)


class TestZoomAndPan:
    def test_empty_space_drags_the_view_not_a_node(self, view):
        view.on_pan_start(start(2.0, 2.0))
        assert view.panning and view.dragging is None
        view.on_pan_update(drag(2, 2, 15, -25))
        assert view.pan == (15, -25)
        view.on_pan_end(None)
        assert not view.panning

    def test_a_node_drag_is_not_swallowed_by_panning(self, view, second):
        """Panning must not steal a gesture that started on a node."""
        view.on_pan_start(start(*view.positions[second]))
        assert not view.panning

    def test_zoom_keeps_the_point_under_the_cursor_still(self, view):
        """The anchoring property, stated as the invariant rather than as a pan formula."""
        cursor = (300.0, 220.0)
        before = view._xy(scroll(*cursor, -1))
        view.on_scroll(scroll(*cursor, -1))
        after = view._xy(scroll(*cursor, -1))
        assert view.zoom > 1.0
        assert before == pytest.approx(after, abs=1e-9)

    def test_zoom_clamps(self, view):
        for _ in range(200):
            view.on_scroll(scroll(100, 100, -1))
        assert view.zoom == pytest.approx(fg.ZOOM_MAX)
        for _ in range(400):
            view.on_scroll(scroll(100, 100, 1))
        assert view.zoom == pytest.approx(fg.ZOOM_MIN)

    def test_a_drag_tracks_the_cursor_when_zoomed(self, view, first):
        """A 30px drag at 0.5x has to move the node 60 layout units, or it slides out
        from under the pointer."""
        view.zoom = 0.5
        before = view.positions[first]
        view.on_pan_start(start(*view._to_screen(*before)))
        assert view.dragging == first
        view.on_pan_update(drag(0, 0, 30, 30))
        after = view.positions[first]
        assert (after[0] - before[0], after[1] - before[1]) == (60, 60)

    def test_reset_view(self, view):
        view.zoom, view.pan = 2.5, (40.0, 40.0)
        view.reset_view()
        assert (view.zoom, view.pan) == (1.0, (0.0, 0.0))


class TestNodeCreation:
    def test_the_menu_offers_the_registry_entries(self, view):
        entries = view.menu_entries()
        assert entries, "no chunk menu entries"
        assert any("transform" in e.label.lower() for e in entries)

    def test_creating_a_chunk_adds_a_node(self, view):
        before = set(view.prims)
        entry = next(e for e in view.menu_entries() if e.label.lower() == "transform")
        view.create_chunk(entry, 250.0, 400.0)
        added = set(view.prims) - before
        assert len(added) == 1, f"expected one new node, got {added}"

    def test_the_new_node_lands_where_it_was_asked_to(self, view):
        """Round trip through the USD write: the node comes back at the layout point the
        menu was opened at, which is what makes right-click-here feel placed rather than
        random."""
        before = set(view.prims)
        entry = next(e for e in view.menu_entries() if e.label.lower() == "transform")
        view.create_chunk(entry, 250.0, 400.0)
        path = (set(view.prims) - before).pop()
        assert view.positions[path] == pytest.approx((250.0, 400.0), abs=1.0)

    def test_right_click_on_empty_space_does_not_raise_without_a_page(self, view):
        """No page in tests, so the menu cannot open -- it must degrade, not throw."""
        view.on_secondary_tap(tap(5.0, 5.0))


class TestYConvention:
    def test_usd_y_is_flipped_the_way_qt_flips_it(self, view, first):
        """USD is Y-up and this canvas is Y-down.

        Qt writes _create_node_pos as (x, -y). Flet did not flip at all, so the same file
        rendered mirrored between the two frontends and a node dragged in one jumped to
        the other side when opened in the other.
        """
        usd_y = chunk_graph.node_position(view.prims[first])[1]
        layout_y = view.positions[first][1]
        assert view._layout_to_usd(*view.positions[first])[1] == pytest.approx(usd_y)
        assert (layout_y + view.origin[1]) == pytest.approx(-usd_y)

    def test_a_move_round_trips_through_usd(self, view, first):
        before = chunk_graph.node_position(view.prims[first])
        view.on_pan_start(start(*view.positions[first]))
        view.on_pan_update(drag(0, 0, 25, 35))
        view.on_pan_end(None)
        view.reload()
        after = chunk_graph.node_position(view.prims[first])
        # Asserted in USD space, not layout space: layout is measured from an origin that
        # is itself the leftmost/topmost node, so moving that node moves the origin and
        # its layout coordinate legitimately stays put. The first version of this test
        # asserted the layout value and failed for that reason alone.
        assert (after[0] - before[0]) == pytest.approx(25, abs=0.5)
        assert (after[1] - before[1]) == pytest.approx(-35, abs=0.5)  # Y-down -> Y-up


class TestSelectionAndKeys:
    def test_a_tap_on_a_node_body_selects_it(self, view, first):
        x, y = view.positions[first]
        view.on_tap(tap(x + fg.NODE_W / 2, y + 6))
        assert view.selected_nodes == {first}

    def test_selection_is_mirrored_onto_the_chunk(self, view, first):
        """Qt's operations read the SELECTED state bit, not a view attribute. If Flet kept
        selection to itself, a shared operation would see nothing selected."""
        view.set_selection([first])
        chunk = view.chunks[first]
        assert chunk.has_state(ChunkFlags.State.SELECTED)
        view.set_selection(())
        assert not chunk.has_state(ChunkFlags.State.SELECTED)

    def test_escape_clears_the_selection(self, view, first):
        view.set_selection([first])
        view.on_key(key("Escape"))
        assert view.selected_nodes == set()

    def test_delete_removes_the_selected_node(self, view, first):
        view.set_selection([first])
        view.on_key(key("Delete"))
        assert first not in view.prims

    def test_d_toggles_disable(self, view, first):
        view.set_selection([first])
        assert not chunk_graph.is_disabled(view.prims[first])
        view.on_key(key("D"))
        assert chunk_graph.is_disabled(view.prims[first])
        view.set_selection([first])
        view.on_key(key("D"))
        assert not chunk_graph.is_disabled(view.prims[first])

    def test_ctrl_a_selects_everything(self, view):
        view.on_key(key("A", ctrl=True))
        assert view.selected_nodes == set(view.prims)

    def test_shortcuts_are_ignored_while_typing(self, view, first):
        """The focus rule. A page-level handler sees keystrokes meant for a TextField
        inside a node; without this, typing a prim path deletes the node.

        Driven through the tracker a widget actually calls, rather than by patching the
        query -- the first version patched is_editing, which only proved the branch reads
        it, not that a focused widget ever gets it there.
        """
        view.set_selection([first])
        view.focus.enter()
        view.on_key(key("Delete"))
        assert first in view.prims, "Delete fired while a param was being edited"
        assert view.selected_nodes == {first}

    def test_f_frames_the_selection(self, view, first):
        view.zoom, view.pan = 3.0, (999.0, 999.0)
        view.set_selection([first])
        view.on_key(key("F"))
        assert view.zoom != 3.0 and view.pan != (999.0, 999.0)
        # The framed node's centre should land near the middle of the nominal viewport.
        cx = view.positions[first][0] + fg.NODE_W / 2
        assert view._to_screen(cx, view.positions[first][1])[0] == pytest.approx(600, abs=60)
