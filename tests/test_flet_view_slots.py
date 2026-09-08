"""View slots in the Flet graph.

A context has up to three view slots; the active one drives execution. The slot lives on
the context prim as chunk:activeView -- the same attribute the Qt graph reads and writes,
so a file saved with slot 2 active opens on slot 2 in either frontend.
"""
from __future__ import annotations

import flet as ft
import pytest

from graph_fixtures import make_network
from usd_chunks.plugins import chunk_flet_nodegraph as fg


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
    def test_v_adds_a_view_node_in_slot_one(self, view):
        before = set(view.prims)
        view.on_key(key("V"))
        assert set(view.prims) - before, "no view node was created"
        assert view.view_chunks() == {1: (set(view.prims) - before).pop()}

    def test_a_second_view_takes_the_next_free_slot(self, view):
        view.create_view(100.0, 500.0)
        view.create_view(100.0, 700.0)
        assert sorted(view.view_chunks()) == [1, 2]

    def test_a_fourth_view_is_refused(self, view):
        for i in range(3):
            view.create_view(100.0, 400.0 + i * 100)
        count = len(view.prims)
        view.create_view(100.0, 800.0)
        assert len(view.prims) == count
        assert "slots are taken" in view.status


class TestActiveSlot:
    def test_creating_a_view_activates_it(self, view):
        view.create_view(100.0, 500.0)
        assert view.active_slot() == 1

    def test_switching_slots(self, view):
        view.create_view(100.0, 500.0)
        view.create_view(100.0, 700.0)
        view.on_key(key("1"))
        assert view.active_slot() == 1
        view.on_key(key("2"))
        assert view.active_slot() == 2

    def test_the_slot_is_stored_on_the_context_prim(self, view):
        """Where Qt looks for it. A slot kept only in the view object would not survive a
        save, and would not be seen by the other frontend at all."""
        view.create_view(100.0, 500.0)
        view.create_view(100.0, 700.0)
        view.set_active_slot(2)
        prim = view.stage.GetPrimAtPath(view.context)
        assert prim.GetAttribute("chunk:activeView").Get() == 2

    def test_slot_zero_clears_rather_than_writing_a_zero(self, view):
        """An authored 0 and an absent attribute would be two spellings of one state."""
        view.create_view(100.0, 500.0)
        view.set_active_slot(0)
        prim = view.stage.GetPrimAtPath(view.context)
        attr = prim.GetAttribute("chunk:activeView")
        assert not (attr and attr.IsValid() and attr.HasAuthoredValue())
        assert view.active_slot() == 0

    def test_switching_to_an_empty_slot_is_refused(self, view):
        view.create_view(100.0, 500.0)
        view.set_active_slot(3)
        assert view.active_slot() == 1, "an empty slot should not become active"
        assert "no view in slot 3" in view.status

    def test_the_slot_survives_a_reload(self, view):
        view.create_view(100.0, 500.0)
        view.create_view(100.0, 700.0)
        view.set_active_slot(2)
        reopened = fg.GraphView(view.network, view.context)
        assert reopened.active_slot() == 2


class TestHeaderColour:
    def test_the_active_view_gets_the_reserved_accent(self, view):
        view.create_view(100.0, 500.0)
        path = view.view_chunks()[1]
        assert view._header_colour(path, view.prims[path]) == fg.COL_VIEW_HEADER

    def test_an_inactive_view_is_dimmed(self, view):
        view.create_view(100.0, 500.0)
        view.create_view(100.0, 700.0)
        view.set_active_slot(2)
        inactive = view.view_chunks()[1]
        colour = view._header_colour(inactive, view.prims[inactive])
        assert colour != fg.COL_VIEW_HEADER
        assert colour != fg.COL_NODE_HEADER, "an inactive view should not look like a plain node"

    def test_an_ordinary_node_keeps_the_plain_header(self, view):
        path = sorted(p for p in view.prims if p not in view.view_chunks().values())[0]
        assert view._header_colour(path, view.prims[path]) == fg.COL_NODE_HEADER
