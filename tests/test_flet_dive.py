"""Diving into a chunkgroup, and back out.

A chunkgroup is both a node in its parent's graph and a graph of its own. Diving is how
you get at the second one, so it is not a convenience -- without it the inside of a group
is unreachable and grouping is a one-way operation.
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


@pytest.fixture
def view(tmp_path):
    path, context = make_network(tmp_path / "dive.usda")
    v = fg.GraphView(path, context)
    v.live = False
    return v


@pytest.fixture
def grouped(view):
    """A view holding one chunkgroup with the two original nodes inside it."""
    view.set_selection(content_nodes(view))
    view.group_selection()
    assert len(content_nodes(view)) == 1, "fixture did not produce a single group"
    return view


def tap(x, y):
    return ft.TapEvent(name="double_tap_down", control=None, kind=None,
                       local_position=ft.Offset(x, y), global_position=ft.Offset(x, y))


def key(name):
    return ft.KeyboardEvent(name="keyboard", control=None, key=name,
                            shift=False, ctrl=False, alt=False, meta=False)


class TestDivingIn:
    def test_double_clicking_a_group_shows_its_contents(self, grouped):
        outer = grouped.context
        group = content_nodes(grouped)[0]
        x, y = grouped.positions[group]
        grouped.on_double_tap(tap(x + fg.NODE_W / 2, y + 6))
        assert grouped.context == group
        assert grouped.context != outer
        assert len(grouped.prims) == 2, "the group's two chunks should be the graph now"

    def test_the_nodes_inside_are_the_grouped_ones(self, grouped):
        group = content_nodes(grouped)[0]
        grouped.dive_into(group)
        assert all(p.startswith(group + "/") for p in grouped.prims)

    def test_an_ordinary_node_is_not_divable(self, view):
        node = content_nodes(view)[0]
        assert not view.can_dive(node)
        before = view.context
        view.dive_into(node)
        assert view.context == before

    def test_double_clicking_an_ordinary_node_says_so(self, view):
        node = content_nodes(view)[0]
        x, y = view.positions[node]
        view.on_double_tap(tap(x + fg.NODE_W / 2, y + 6))
        assert "not a subnet" in view.status

    def test_a_group_header_is_marked_as_divable(self, grouped):
        group = content_nodes(grouped)[0]
        assert grouped._header_colour(group, grouped.prims[group]) == fg.COL_GROUP_HEADER


class TestComingBackOut:
    def test_backspace_goes_up(self, grouped):
        outer = grouped.context
        group = content_nodes(grouped)[0]
        grouped.dive_into(group)
        grouped.on_key(key("Backspace"))
        assert grouped.context == outer

    def test_coming_out_selects_the_group_you_were_in(self, grouped):
        group = content_nodes(grouped)[0]
        grouped.dive_into(group)
        grouped.dive_out()
        assert grouped.selected_nodes == {group}

    def test_double_clicking_empty_space_goes_up(self, grouped):
        outer = grouped.context
        group = content_nodes(grouped)[0]
        grouped.dive_into(group)
        grouped.on_double_tap(tap(2.0, 2.0))
        assert grouped.context == outer

    def test_at_the_top_it_says_so(self, view):
        while view.parent_context() is not None:
            view.dive_out()
        view.dive_out()
        assert "already at the top" in view.status

    def test_the_breadcrumb_shows_depth(self, grouped):
        group = content_nodes(grouped)[0]
        grouped.dive_into(group)
        assert grouped.breadcrumb().count("/") >= 1


class TestCreatingWhileInside:
    def test_a_new_chunk_nests_into_the_group(self, grouped):
        """add_chunk parents to the scene's current context, which a dive does not set. So
        without _nest, creating a node while inside a group silently adds it OUTSIDE."""
        group = content_nodes(grouped)[0]
        grouped.dive_into(group)
        entry = next(e for e in grouped.menu_entries() if e.label.lower() == "transform")
        before = set(grouped.prims)
        grouped.create_chunk(entry, 300.0, 300.0)
        added = set(grouped.prims) - before
        assert len(added) == 1, "the new chunk did not appear inside the group"
        assert added.pop().startswith(group + "/")

    def test_the_outer_graph_did_not_gain_a_node(self, grouped):
        group = content_nodes(grouped)[0]
        grouped.dive_into(group)
        entry = next(e for e in grouped.menu_entries() if e.label.lower() == "transform")
        grouped.create_chunk(entry, 300.0, 300.0)
        grouped.dive_out()
        assert len(content_nodes(grouped)) == 1, "a chunk leaked into the outer graph"

    def test_a_paste_nests_too(self, grouped):
        group = content_nodes(grouped)[0]
        grouped.dive_into(group)
        inner = content_nodes(grouped)[0]
        grouped.set_selection([inner])
        grouped.copy_selection()
        before = set(grouped.prims)
        grouped.paste_at(400.0, 400.0)
        added = set(grouped.prims) - before
        assert len(added) == 1
        assert added.pop().startswith(group + "/")


class TestContextList:
    def test_grouping_registers_a_new_context(self, view):
        before = list(view.available)
        view.set_selection(content_nodes(view))
        view.group_selection()
        assert len(view.available) > len(before), "the group did not become a context"
