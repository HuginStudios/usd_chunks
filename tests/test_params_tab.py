"""The Params tab: its header sits on top, and rows come and go in every panel showing them."""
from __future__ import annotations

from tests.test_clear_param import _right_click, _row
from tests.test_unloft import env  # noqa: F401 -- a fixture
from usd_chunks import chunk_stage
from usd_chunks.frontends.qt.widgets import ChunkWidget, LabeledWidget, ViewSlotState


def _tab_widget(env, context) -> ChunkWidget:  # noqa: F811
    """A Params page for context, on screen."""
    page = ChunkWidget(context, is_tab_context=True)
    page.set_view_state(ViewSlotState())
    page.resize(500, 800)
    page.show()
    env["settle"]()
    return page


def _lofted(context):
    return [p for p in context.get_my_params() if getattr(p, "_lofted", False)]


def test_the_header_sits_above_the_chunks(env):  # noqa: F811
    mesh = env["mk"]("mesh")
    env["settle"]()
    page = _tab_widget(env, mesh.get_context())

    # The group's box (whose title is the header) first, then the search box.
    assert page._group_box.isAncestorOf(page.minimize_button)
    assert page._group_box.geometry().bottom() < page._search_panel.geometry().top()


def _list_container(widget: ChunkWidget, group) -> ChunkWidget:
    return next(c for c in widget.findChildren(ChunkWidget) if c.group is group)


def _row_count(widget: ChunkWidget, group) -> int:
    layout = _list_container(widget, group).content_layout
    return sum(isinstance(layout.itemAt(i).widget(), LabeledWidget) for i in range(layout.count()))


def test_a_list_row_deleted_in_one_panel_goes_from_the_other(env):  # noqa: F811
    node = env["mk"]("example")
    panel, body = ChunkWidget(node), ChunkWidget(node, for_proxy=True)
    for widget in (panel, body):
        widget.show()
    rows = node.get_param("listGroup")
    rows.size = 3
    env["settle"]()
    assert _row_count(panel, rows) == _row_count(body, rows) == 3

    # The row's "-" button, in the node.
    first = _list_container(body, rows).content_layout.itemAt(0).widget()
    first.vlayout.itemAt(0).widget().click()
    env["settle"]()

    assert rows.size == 2
    assert _row_count(panel, rows) == _row_count(body, rows) == 2


def _header_rows(page: ChunkWidget) -> list:
    """The params shown in the page's header, besides the prim path."""
    layout = page._lofted_layout
    return [layout.itemAt(i).widget()._model for i in range(layout.count())]


def test_unloft_takes_the_row_off_the_header(env):  # noqa: F811
    fence = env["mk"]("fence")
    env["settle"]()
    context = fence.get_context()
    chunk_stage.loft_param_to_context(fence.get_param("length"), context)
    env["settle"]()
    page = _tab_widget(env, context)
    [lofted] = _lofted(context)
    assert lofted in _header_rows(page)

    chunk_stage.unloft_param(lofted)
    env["settle"]()

    assert lofted not in _header_rows(page)


def test_a_param_is_lofted_once(env, monkeypatch):  # noqa: F811
    fence = env["mk"]("fence")
    env["settle"]()
    context = fence.get_context()
    length = fence.get_param("length")
    chunk_stage.loft_param_to_context(length, context)
    env["settle"]()

    # The menu offers to take it back down, not to loft a second copy.
    panel = ChunkWidget(fence)
    panel.show()
    env["settle"]()
    offered = _right_click(_row(panel, length), monkeypatch)
    assert "Loft up" not in offered
    assert offered["Unloft"]

    chunk_stage.loft_param_to_context(length, context)
    env["settle"]()
    assert len(_lofted(context)) == 1

    _right_click(_row(panel, length), monkeypatch, pick="Unloft")
    env["settle"]()
    assert _lofted(context) == []


def test_a_param_can_be_lofted_again_after_unloft(env):  # noqa: F811
    node = env["mk"]("example")
    env["settle"]()
    context = node.get_context()
    slider = node.get_param("sliderA")  # at its default: its attr only holds the loft
    chunk_stage.loft_param_to_context(slider, context)
    env["settle"]()
    chunk_stage.unloft_param(_lofted(context)[0])
    env["settle"]()

    chunk_stage.loft_param_to_context(slider, context)
    env["settle"]()

    [lofted] = _lofted(context)
    lofted.value = 3.0
    env["settle"]()
    assert slider.value == 3.0


def test_a_float_slider_at_a_whole_number_keeps_its_loft(env):  # noqa: F811
    node = env["mk"]("example")
    env["settle"]()
    context = node.get_context()
    slider = node.get_param("sliderA")  # a float slider, at 0: a whole number
    chunk_stage.loft_param_to_context(slider, context)
    env["settle"]()

    [lofted] = _lofted(context)
    inner = env["stage"].GetAttributeAtPath(slider._param_attr_path)
    assert inner, "the attr Loft up made was deleted"
    assert [str(c) for c in inner.GetConnections()] == [lofted._param_attr_path]
