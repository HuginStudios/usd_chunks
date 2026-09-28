"""View slots in the Flet graph.

A context has up to three view slots; the active one drives execution. The slot lives on
the context prim as chunk:activeView -- the same attribute the Qt graph reads and writes,
so a file saved with slot 2 active opens on slot 2 in either frontend.
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
    path, context = make_network(tmp_path / "views.usda")
    v = fg.GraphView(path, context)
    v.live = False
    return v


def key(name, ctrl=False):
    return ft.KeyboardEvent(name="keyboard", control=None, key=name,
                            shift=False, ctrl=ctrl, alt=False, meta=False)


class TestCreatingViews:
    def test_a_network_opens_with_a_view(self, view):
        """Every frontend creates one on open, as Qt always has.

        Not decoration: recook_for_view cooks the active view's upstream tree and blocks
        everything else, so a context WITHOUT a view cooks nothing at all -- a network
        would look right and do nothing.
        """
        assert sorted(view.view_chunks()) == [1]
        assert view.active_slot() == 1

    def test_v_on_a_selected_node_adds_a_view_for_it(self, view):
        node = content_nodes(view)[0]
        view.set_selection([node])
        before = set(view.prims)
        view.on_key(key("V"))
        made = set(view.prims) - before
        assert made, "no view node was created"
        assert sorted(view.view_chunks()) == [1, 2]

    def test_a_second_view_on_the_same_node_reuses_its_slot(self, view):
        """create_view_for returns the EXISTING slot rather than duplicating, so a caller
        can activate whatever comes back either way."""
        node = content_nodes(view)[0]
        view.set_selection([node])
        view.create_view()
        slots = sorted(view.view_chunks())
        view.set_selection([node])
        view.create_view()
        assert sorted(view.view_chunks()) == slots

    def test_a_fourth_view_is_refused(self, view):
        for node in content_nodes(view):
            view.set_selection([node])
            view.create_view()
        count = len(view.prims)
        # Slots are 1-3 and the open already took one, so this asks for a fourth.
        view.set_selection(content_nodes(view))
        view.create_view()
        assert len(view.prims) == count


class TestActiveSlot:
    def test_creating_a_view_activates_it(self, view):
        node = content_nodes(view)[0]
        view.set_selection([node])
        view.create_view()
        assert view.active_slot() == 2

    def test_switching_slots(self, view):
        view.set_selection([content_nodes(view)[0]])
        view.create_view()
        view.on_key(key("1"))
        assert view.active_slot() == 1
        view.on_key(key("2"))
        assert view.active_slot() == 2

    def test_the_slot_is_stored_on_the_context_prim(self, view):
        """Where Qt looks for it. A slot kept only in the view object would not survive a
        save, and would not be seen by the other frontend at all."""
        view.set_selection([content_nodes(view)[0]])
        view.create_view()
        view.set_active_slot(2)
        prim = view.stage.GetPrimAtPath(view.context)
        assert prim.GetAttribute("chunk:activeView").Get() == 2

    def test_slot_zero_clears_rather_than_writing_a_zero(self, view):
        """An authored 0 and an absent attribute would be two spellings of one state."""
        view.set_selection([content_nodes(view)[0]])
        view.create_view()
        view.set_active_slot(0)
        prim = view.stage.GetPrimAtPath(view.context)
        attr = prim.GetAttribute("chunk:activeView")
        assert not (attr and attr.IsValid() and attr.HasAuthoredValue())
        assert view.active_slot() == 0

    def test_switching_to_an_empty_slot_is_refused(self, view):
        view.set_selection([content_nodes(view)[0]])
        view.create_view()
        view.set_active_slot(3)
        assert view.active_slot() == 2, "an empty slot should not become active"
        assert "no view in slot 3" in view.status

    def test_the_slot_survives_a_reload(self, view):
        view.set_selection([content_nodes(view)[0]])
        view.create_view()
        view.set_active_slot(2)
        reopened = fg.GraphView(view.network, view.context)
        assert reopened.active_slot() == 2


class TestHeaderColour:
    def test_the_active_view_gets_the_reserved_accent(self, view):
        view.set_selection([content_nodes(view)[0]])
        view.create_view()
        path = view.view_chunks()[view.active_slot()]
        assert view._header_colour(path, view.prims[path]) == fg.COL_VIEW_HEADER

    def test_an_inactive_view_is_dimmed(self, view):
        view.set_selection([content_nodes(view)[0]])
        view.create_view()
        view.set_active_slot(2)
        inactive = view.view_chunks()[1]
        colour = view._header_colour(inactive, view.prims[inactive])
        assert colour != fg.COL_VIEW_HEADER
        assert colour != fg.COL_NODE_HEADER, "an inactive view should not look like a plain node"

    def test_an_ordinary_node_keeps_the_plain_header(self, view):
        path = sorted(p for p in view.prims if p not in view.view_chunks().values())[0]
        assert view._header_colour(path, view.prims[path]) == fg.COL_NODE_HEADER
