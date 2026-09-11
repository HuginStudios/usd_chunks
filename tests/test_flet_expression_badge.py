"""The expression badge on a Flet wire.

The engine already carried `EdgeRef.expression` -- Qt drew a labelled dot for it, Flet
only swapped the wire colour. These tests pin the badge itself, because a colour change
is invisible to anyone who does not already know which wire to look at.

The whole file runs without Qt, like the rest of the Flet and engine tests.
"""
from __future__ import annotations

import pytest

# flet is an optional extra (pyproject: [flet]), so this file steps aside rather than
# erroring the whole collection when it is not installed -- which is what a module-level
# `import flet` did. Every import below is deliberately after it.
pytest.importorskip("flet", reason="flet is an optional extra")


import dataclasses  # noqa: E402

import flet.canvas as cv  # noqa: E402

from graph_fixtures import make_network  # noqa: E402
from usd_chunks.plugins import chunk_flet_nodegraph as fg  # noqa: E402



@pytest.fixture
def view(tmp_path):
    path, context = make_network(tmp_path / "expr.usda")
    return fg.GraphView(path, context)


def _badges(view):
    """Every 'Exp' glyph the canvas would draw."""
    return [s for s in view.shapes()
            if isinstance(s, cv.Text) and s.value == "Exp"]


def _drawn(view):
    """The edges shapes() will actually draw.

    Not every EdgeRef becomes a wire: a connection whose endpoint is not an exposed port
    is skipped. Counting badges against all edges made this test fail for a reason that
    had nothing to do with badges.
    """
    out = []
    for edge in view.edges:
        if (edge.src_node_path not in view.positions
                or edge.dst_node_path not in view.positions):
            continue
        src = next((p for p in view.ports[edge.src_node_path]
                    if p.attr_path == edge.src_attr_path), None)
        dst = next((p for p in view.ports[edge.dst_node_path]
                    if p.attr_path == edge.dst_attr_path), None)
        if src is not None and dst is not None:
            out.append(edge)
    return out


def _circles_at(view, x, y, radius):
    return [s for s in view.shapes()
            if isinstance(s, cv.Circle)
            and abs(s.x - x) < 0.01 and abs(s.y - y) < 0.01
            and abs(s.radius - radius) < 0.01]


class TestBadge:
    def test_a_plain_wire_draws_no_badge(self, view):
        """The control: without an expression there must be nothing to see.

        If this ever passes for the wrong reason -- shapes() returning nothing at all --
        the wire count below catches it.
        """
        assert view.edges, "no edges built; the fixture is wrong"
        view.edges = [dataclasses.replace(e, expression=None) for e in view.edges]
        assert _badges(view) == []

    def test_an_expression_wire_draws_a_labelled_dot(self, view):
        view.edges = [dataclasses.replace(e, expression="@Source.tx@")
                      for e in view.edges]
        drawn = _drawn(view)
        assert drawn, "no wire is drawable; the fixture is wrong"
        assert len(_badges(view)) == len(drawn)

    def test_the_dot_sits_on_the_wire_not_beside_it(self, view):
        """The badge goes at the cubic's own midpoint.

        Both control points are offset horizontally by the same reach, so the curve at
        t=0.5 reduces to the straight midpoint -- this asserts that reduction rather than
        trusting it, by checking a circle of badge radius exists exactly there.
        """
        target = _drawn(view)[0]
        view.edges = [dataclasses.replace(e, expression="@Source.tx@" if e is target
                                          else None)
                      for e in view.edges]
        edge = next(e for e in view.edges if e.expression)

        src = next(p for p in view.ports[edge.src_node_path]
                   if p.attr_path == edge.src_attr_path)
        dst = next(p for p in view.ports[edge.dst_node_path]
                   if p.attr_path == edge.dst_attr_path)
        x1, y1 = view.port_xy(src)
        x2, y2 = view.port_xy(dst)

        assert _circles_at(view, (x1 + x2) / 2, (y1 + y2) / 2, fg.EXP_R), (
            "no badge-sized circle at the wire midpoint"
        )

    def test_the_badge_matches_qt_radius(self):
        """Drift here is the failure mode: two frontends that disagree on a glyph."""
        from usd_chunks.plugins import chunk_qt_nodegraph as qg
        assert fg.EXP_R == qg.ChunkEdgeItem._EXP_RADIUS
