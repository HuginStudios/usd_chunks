"""Tests for apply_chunk_stylesheet, which decides who carries CHUNK_STYLESHEET.

setStyleSheet re-polishes the whole subtree beneath the widget, so where and *when* the
sheet is applied is a performance decision, not a cosmetic one: applied late, to a finished
chunk tree, one call was the most expensive thing in building a node.
"""

from PySide6 import QtWidgets

from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins.chunk_qt_widgets import CHUNK_STYLESHEET, ChunkWidget, apply_chunk_stylesheet


class _Scene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def test_applies_to_an_unstyled_widget(qapp):
    w = QtWidgets.QWidget()

    assert apply_chunk_stylesheet(w) is True
    assert w.styleSheet() == CHUNK_STYLESHEET


def test_second_call_is_a_no_op(qapp):
    w = QtWidgets.QWidget()
    apply_chunk_stylesheet(w)

    assert apply_chunk_stylesheet(w) is False


def test_descendant_of_a_styled_widget_is_left_alone(qapp):
    # Qt merges an ancestor's sheet into every descendant, so a child re-applying it
    # repolishes its whole subtree to arrive at the same appearance.
    parent = QtWidgets.QWidget()
    apply_chunk_stylesheet(parent)
    child = QtWidgets.QWidget(parent)
    grandchild = QtWidgets.QWidget(child)

    assert apply_chunk_stylesheet(child) is False
    assert apply_chunk_stylesheet(grandchild) is False
    assert child.styleSheet() == ""
    assert grandchild.styleSheet() == ""


def test_a_self_styling_ancestor_covers_before_it_has_applied(qapp):
    # A nested ChunkWidget asks this question while the outer one is still inside its own
    # __init__ and has not applied the sheet yet -- children are built before
    # update_from_state runs. Answering from the flag alone left every nested ChunkWidget
    # applying its own copy: ten sheets in a nine-page shader instead of one, 175ms against
    # 117ms to build the same tree.
    class _SelfStyling(QtWidgets.QWidget):
        _chunk_styles_self = True

    outer = _SelfStyling()
    inner = QtWidgets.QWidget(outer)

    assert outer.styleSheet() == ""  # not applied yet, and still covering
    assert apply_chunk_stylesheet(inner) is False
    assert inner.styleSheet() == ""


def test_a_built_chunk_tree_carries_exactly_one_sheet(qapp):
    """The invariant that matters: one sheet per tree, however deeply groups nest."""
    scene = _Scene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")

    c = chunk.Chunk("nine_pages")
    for page in range(3):
        g = chunk.Group(f"page{page}", flags=chunk.ChunkFlags.GroupFlags.COLLAPSABLE)
        for i in range(3):
            g.add_param(chunk.Slider(f"s{page}_{i}", default_value=0.0))
        c.add_param(g)
    scene.add_chunk(c)

    w = ChunkWidget(c)
    styled = [x for x in [w] + w.findChildren(QtWidgets.QWidget) if x.styleSheet() == CHUNK_STYLESHEET]

    assert len(styled) == 1
    assert styled[0] is w


def test_a_detached_widget_styles_itself(qapp):
    # The node graph builds a chunk widget under a sink and then reparents it to None for
    # the proxy. A widget that skipped because of an ancestor must not stay unstyled once
    # that ancestor is gone.
    parent = QtWidgets.QWidget()
    apply_chunk_stylesheet(parent)
    child = QtWidgets.QWidget(parent)
    assert apply_chunk_stylesheet(child) is False

    child.setParent(None)

    assert apply_chunk_stylesheet(child) is True
    assert child.styleSheet() == CHUNK_STYLESHEET
