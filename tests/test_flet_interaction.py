"""Flet graph interaction, driven through real Flet event objects.

Every test here builds an actual `ft.TapEvent` / `ft.DragUpdateEvent` / `ft.ScrollEvent`
and hands it to the handler the GestureDetector would call. That is the point: the
handlers previously read `event.local_x`, which exists on no Flet 0.80 event class, so
every one of them raised AttributeError on first touch. The source looked implemented and
a hand-rolled fake event with the attributes the code wanted would have agreed with it.
Only the real class disagrees.
"""
from __future__ import annotations

import flet as ft
import pytest

from usd_chunks.plugins import chunk_flet_nodegraph as fg

NETWORK = """#usda 1.0

def Scope "chunks"
{
    def Chunk "Source"
    {
        uniform token info:chunkType = "transform"
        string inputs:primpath_location = "/World/thing"
        uniform token outputs:chunk
        uniform float2 ui:nodegraph:node:pos = (100, 50)
    }

    def Chunk "Driven"
    {
        uniform token info:chunkType = "transform"
        token inputs:chunk.connect = </chunks/Source.outputs:chunk>
        string inputs:primpath_location = "/World/thing"
        uniform token outputs:chunk
        uniform float2 ui:nodegraph:node:pos = (400, 50)
    }
}
"""


@pytest.fixture
def view(tmp_path):
    path = tmp_path / "net.usda"
    path.write_text(NETWORK, encoding="utf-8")
    v = fg.GraphView(str(path))
    v.live = False  # no page; refresh() must stay a no-op
    return v


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


def scroll(x, y, dy):
    return ft.ScrollEvent(name="scroll", control=None,
                          local_position=ft.Offset(x, y), global_position=ft.Offset(x, y),
                          scroll_delta=ft.Offset(0, dy))


class TestEventsReachTheHandlers:
    def test_a_tap_on_a_port_selects_it(self, view):
        """The regression: this raised AttributeError, so nothing was selectable."""
        port = view.ports["/chunks/Source"][0]
        x, y = view.port_xy(port)
        view.on_tap(tap(x, y))
        assert view.selected is not None
        assert view.selected.attr_path == port.attr_path

    def test_a_tap_on_empty_space_clears_the_selection(self, view):
        view.selected = view.ports["/chunks/Source"][0]
        view.on_tap(tap(5.0, 5.0))
        assert view.selected is None

    def test_dragging_a_node_moves_it(self, view):
        before = view.positions["/chunks/Source"]
        view.on_pan_start(start(*view.positions["/chunks/Source"]))
        assert view.dragging == "/chunks/Source"
        view.on_pan_update(drag(0, 0, 30, 20))
        after = view.positions["/chunks/Source"]
        assert (after[0] - before[0], after[1] - before[1]) == (30, 20)


class TestZoomAndPan:
    def test_empty_space_drags_the_view_not_a_node(self, view):
        view.on_pan_start(start(2.0, 2.0))
        assert view.panning and view.dragging is None
        view.on_pan_update(drag(2, 2, 15, -25))
        assert view.pan == (15, -25)
        view.on_pan_end(None)
        assert not view.panning

    def test_a_node_drag_is_not_swallowed_by_panning(self, view):
        """Panning must not steal a gesture that started on a node."""
        view.on_pan_start(start(*view.positions["/chunks/Driven"]))
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

    def test_a_drag_tracks_the_cursor_when_zoomed(self, view):
        """A 30px drag at 0.5x has to move the node 60 layout units, or it slides out
        from under the pointer."""
        view.zoom = 0.5
        before = view.positions["/chunks/Source"]
        view.on_pan_start(start(*view._to_screen(*before)))
        assert view.dragging == "/chunks/Source"
        view.on_pan_update(drag(0, 0, 30, 30))
        after = view.positions["/chunks/Source"]
        assert (after[0] - before[0], after[1] - before[1]) == (60, 60)

    def test_reset_view(self, view):
        view.zoom, view.pan = 2.5, (40.0, 40.0)
        view.reset_view()
        assert (view.zoom, view.pan) == (1.0, (0.0, 0.0))
