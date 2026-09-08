"""Every Flet param widget can actually be built.

Written after a Dropdown crashed the app on startup: Flet 0.80 has no `on_change` on
Dropdown at all -- it is `on_select` -- and passing the keyword raises. Nothing caught it
because the graph tests all use the `transform` chunk, which has sliders and a text field
and no Menu, so that branch of the factory was never executed.

The lesson is the shape of this file rather than the one bug: construct EVERY widget the
factory can produce, from a real param, and drive its change handler. A factory row that
is never exercised is a crash waiting for the first chunk that uses it.
"""
from __future__ import annotations

import flet as ft
import pytest

from usd_chunks import chunk
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.plugins import chunk_flet


def params():
    """One real param per widget class the factory can return."""
    menu = chunk.Menu("menu")
    menu.menu_items = ["alpha", "beta"]
    return [
        ("slider", chunk.Slider("slider")),
        ("int slider", chunk.Slider("islider", is_int=True)),
        ("lineedit", chunk.LineEdit("line")),
        ("textedit", chunk.TextEdit("text")),
        ("menu", menu),
        ("checkbox", chunk.Checkbox("check")),
        ("button", chunk.Button("press")),
        ("separator", chunk.Separator("sep")),
        ("colorpicker", chunk.ColorPicker("colour")),
        ("label", chunk.Label("label")),
        ("filebrowser", chunk.FileBrowser("file")),
    ]


PARAMS = params()
IDS = [name for name, _ in PARAMS]


@pytest.fixture
def factory():
    return chunk_flet.ChunkFletWidgetFactory()


@pytest.mark.parametrize("name,param", PARAMS, ids=IDS)
class TestEveryWidgetBuilds:
    def test_the_factory_returns_a_widget(self, factory, name, param):
        assert factory.create_widget(param) is not None, (
            f"no Flet widget for {name} ({param.evaluate_type()!r})")

    def test_building_it_does_not_raise(self, factory, name, param):
        """The regression: Dropdown raised inside build() on an unexpected keyword."""
        widget = factory.create_widget(param)
        assert widget.control is not None or isinstance(widget, chunk_flet.FletSeparatorWidget)


class TestTheFactoryIsFullyCovered:
    def test_every_widget_class_in_the_map_is_exercised(self, factory):
        """If someone adds a row to widget_map, this fails until a param covers it.

        Rows are checked by widget CLASS rather than by ParamType: the two spinbox types
        both map to FletTextWidget, which lineedit already covers, and authoring a Spinbox
        param is against house rules.
        """
        built = {type(factory.create_widget(p)) for _, p in PARAMS}
        declared = set(chunk_flet.ChunkFletWidgetFactory.widget_map.values())
        assert declared - built == set(), (
            f"widget classes never built by any test: {declared - built}")


class TestChangeHandlers:
    def test_a_slider_writes_back_to_its_param(self, factory):
        param = chunk.Slider("s")
        widget = factory.create_widget(param)
        widget.control.value = 3.5
        widget.on_changed(type("E", (), {"control": widget.control})())
        assert param.value == pytest.approx(3.5)

    def test_a_menu_writes_back_to_its_param(self, factory):
        """Driven through on_changed, which is what on_select is wired to."""
        param = chunk.Menu("m")
        param.menu_items = ["alpha", "beta"]
        widget = factory.create_widget(param)
        widget.control.value = "beta"
        widget.on_changed(type("E", (), {"control": widget.control})())
        assert param.value == "beta"

    def test_a_checkbox_writes_back_to_its_param(self, factory):
        param = chunk.Checkbox("c")
        widget = factory.create_widget(param)
        widget.control.value = True
        widget.on_changed(type("E", (), {"control": widget.control})())
        assert bool(param.value) is True


class TestFocusGuard:
    def test_typed_controls_report_focus(self, factory):
        """The graph's shortcuts are page-level, so anything that swallows keystrokes has
        to say when it has focus -- a dropdown included, or picking an option with the
        keyboard would also fire graph shortcuts."""
        for name in ("line", "text", "menu"):
            param = {"line": chunk.LineEdit, "text": chunk.TextEdit}.get(name)
            if param is None:
                p = chunk.Menu("m")
                p.menu_items = ["a"]
            else:
                p = param(name)
            widget = factory.create_widget(p)
            control = getattr(widget, "field", None) or widget.control
            assert control.on_focus is not None, f"{name} does not report focus"

    def test_focus_counting_is_balanced(self, factory):
        tracker = chunk_flet.FocusTracker()
        widget = factory.create_widget(chunk.LineEdit("line"))
        widget.focus = tracker
        assert not tracker.is_editing()
        widget.on_focus(None)
        assert tracker.is_editing()
        widget.on_blur(None)
        assert not tracker.is_editing()

    def test_blur_without_focus_does_not_go_negative(self, factory):
        """A stray blur must not drive the counter below zero -- it would then take two
        focuses to register one, and the guard would silently stop working."""
        tracker = chunk_flet.FocusTracker()
        widget = factory.create_widget(chunk.LineEdit("line"))
        widget.focus = tracker
        widget.on_blur(None)
        widget.on_blur(None)
        widget.on_focus(None)
        assert tracker.is_editing()
        widget.on_blur(None)
        assert not tracker.is_editing()

    def test_two_trackers_are_independent(self, factory):
        """The reason this is an object: two graph windows in one process shared a single
        module-level counter, so typing in one suppressed the other's shortcuts."""
        a, b = chunk_flet.FocusTracker(), chunk_flet.FocusTracker()
        widget = factory.create_widget(chunk.LineEdit("line"))
        widget.focus = a
        widget.on_focus(None)
        assert a.is_editing() and not b.is_editing()
