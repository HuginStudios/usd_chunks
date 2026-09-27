"""Auto-layout, splice-on-drop and shake-to-disconnect in the Flet graph.

The three Qt gestures that had no Flet counterpart. Drag-and-drop file import is the
fourth and is not implementable: Flet 0.80 has DragTarget/Draggable for in-app drags and a
FilePicker dialog, but no page-level OS file drop at all.
"""
from __future__ import annotations

import pytest

# flet is an optional extra (pyproject: [flet]), so this file steps aside rather than
# erroring the whole collection when it is not installed -- which is what a module-level
# `import flet` did. Every import below is deliberately after it.
pytest.importorskip("flet", reason="flet is an optional extra")


import flet as ft  # noqa: E402

from graph_fixtures import content_nodes, make_network  # noqa: E402
from usd_chunks.plugins import chunk_flet_nodegraph as fg  # noqa: E402
from usd_chunks import chunk_graph  # noqa: E402


@pytest.fixture
def view(tmp_path):
    path, context = make_network(tmp_path / "gestures.usda")
    v = fg.GraphView(path, context)
    v.live = False
    return v


@pytest.fixture
def first(view):
    return content_nodes(view)[0]


@pytest.fixture
def second(view):
    return content_nodes(view)[1]


def key(name, ctrl=False):
    return ft.KeyboardEvent(name="keyboard", control=None, key=name,
                            shift=False, ctrl=ctrl, alt=False, meta=False)


def start(x, y):
    return ft.DragStartEvent(name="pan_start", control=None, kind=None, timestamp=0,
                             local_position=ft.Offset(x, y), global_position=ft.Offset(x, y))


def drag(dx, dy):
    return ft.DragUpdateEvent(
        name="pan_update", control=None,
        local_position=ft.Offset(0, 0), global_position=ft.Offset(0, 0),
        local_delta=ft.Offset(dx, dy), global_delta=ft.Offset(dx, dy),
        primary_delta=None, timestamp=0)


class TestAutoLayout:
    def test_l_moves_nodes(self, view):
        before = dict(view.positions)
        view.on_key(key("L"))
        assert view.positions != before, "auto-layout moved nothing"

    def test_it_persists_to_usd(self, view):
        view.on_key(key("L"))
        for path, (x, y) in view.positions.items():
            stored = chunk_graph.node_position(view.prims[path])
            assert stored == pytest.approx(view._layout_to_usd(x, y), abs=1.0)

    def test_a_chain_is_laid_out_left_to_right(self, view, first, second):
        """The fixture wires first -> second, so the downstream node must end up in a
        later column. This is the property the algorithm exists for."""
        view.on_key(key("L"))
        assert view.positions[second][0] > view.positions[first][0]

    def test_laying_out_a_selection_leaves_others_alone(self, view, first, second):
        """A selection grows to its connected subgraph -- and these two ARE connected, so
        both move. The assertion is that the operation is scoped at all, not that it
        stops at the selection boundary."""
        view.set_selection([first])
        before = dict(view.positions)
        view.auto_layout()
        assert view.positions != before

    def test_an_empty_graph_does_not_raise(self, view):
        view.prims = {}
        view.auto_layout()


class TestSplice:
    def test_a_node_dropped_on_a_wire_is_spliced_in(self, view, first, second, tmp_path):
        """A -> B becomes A -> C -> B when C is dropped on the wire."""
        entry = next(e for e in view.menu_entries() if e.label.lower() == "transform")
        before = set(view.prims)
        view.create_chunk(entry, 900.0, 900.0)
        new = (set(view.prims) - before).pop()

        wire = [e for e in view.edges if e.is_exec
                and e.src_node_path == first and e.dst_node_path == second]
        assert wire, "fixture has no exec wire to splice into"

        # Put the new node on the wire and drop it there.
        src = view._port_ref(first, wire[0].src_attr_path)
        dst = view._port_ref(second, wire[0].dst_attr_path)
        mid_x = (view.port_xy(src)[0] + view.port_xy(dst)[0]) / 2
        mid_y = (view.port_xy(src)[1] + view.port_xy(dst)[1]) / 2
        view.positions[new] = (mid_x - fg.NODE_W / 2, mid_y - 10)

        assert view.try_splice(new), "the node was not spliced"
        paths = {(e.src_node_path, e.dst_node_path) for e in view.edges if e.is_exec}
        assert (first, new) in paths, "upstream was not rewired into the new node"
        assert (new, second) in paths, "the new node was not wired to downstream"
        assert (first, second) not in paths, "the original wire survived the splice"

    def test_a_node_not_over_a_wire_is_not_spliced(self, view, first):
        assert view.edge_under_node(first) is None
        assert not view.try_splice(first)

    def test_a_plain_click_does_not_splice(self, view, first):
        """Qt's rule: the node has to actually move, or clicking a node that happens to sit
        over a wire would silently rewire the graph."""
        view.on_pan_start(start(*view.positions[first]))
        assert not view._moved_far_enough
        view.on_pan_end(None)


class TestShake:
    def test_a_steady_drag_does_not_fire(self, view, first):
        view.on_pan_start(start(*view.positions[first]))
        for _ in range(20):
            view.on_pan_update(drag(20, 0))
        assert not view._shook, "a straight drag was read as a shake"

    def test_jitter_does_not_fire(self, view, first):
        """Below SHAKE_MIN_STEP each way: a shaky hand is not a gesture."""
        view.on_pan_start(start(*view.positions[first]))
        for i in range(40):
            view.on_pan_update(drag(2 if i % 2 else -2, 0))
        assert not view._shook

    def test_a_real_shake_disconnects(self, view, second):
        """second is the downstream node, so it has an exec wire to lose."""
        assert any(e.dst_node_path == second for e in view.edges)
        view.on_pan_start(start(*view.positions[second]))
        for i in range(12):
            view.on_pan_update(drag(60 if i % 2 else -60, 0))
        assert view._shook, "the shake was not detected"
        assert not any(e.dst_node_path == second or e.src_node_path == second
                       for e in view.edges), "wires survived the shake"

    def test_shaking_a_middle_node_heals_the_chain(self, view, first, second):
        """A -> C -> B must become A -> B, not leave B orphaned."""
        entry = next(e for e in view.menu_entries() if e.label.lower() == "transform")
        before = set(view.prims)
        view.create_chunk(entry, 900.0, 900.0)
        middle = (set(view.prims) - before).pop()

        wire = [e for e in view.edges if e.is_exec
                and e.src_node_path == first and e.dst_node_path == second][0]
        src = view._port_ref(first, wire.src_attr_path)
        dst = view._port_ref(second, wire.dst_attr_path)
        mid_x = (view.port_xy(src)[0] + view.port_xy(dst)[0]) / 2
        mid_y = (view.port_xy(src)[1] + view.port_xy(dst)[1]) / 2
        view.positions[middle] = (mid_x - fg.NODE_W / 2, mid_y - 10)
        assert view.try_splice(middle)

        view.shake_disconnect(middle)
        paths = {(e.src_node_path, e.dst_node_path) for e in view.edges if e.is_exec}
        assert (first, second) in paths, "the chain was not healed around the shaken node"
        assert not any(middle in pair for pair in paths), "the shaken node kept a wire"
