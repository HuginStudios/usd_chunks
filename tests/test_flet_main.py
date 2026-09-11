"""main() builds a page without raising.

The entry point was the one thing not covered, and it is exactly where the Dropdown crash
landed: a chunk with a Menu param blew up inside node_controls during startup, before the
first frame, while every other Flet test passed.

A real ft.Page needs a running Flet session, so this drives main() against a stand-in that
records what was added. That is enough to exercise the whole build path -- GraphView
construction, node_controls, every param widget, shapes(), the Stack and the
GestureDetector wiring -- which is where startup crashes actually live.
"""
from __future__ import annotations

import pytest

# flet is an optional extra (pyproject: [flet]), so this file steps aside rather than
# erroring the whole collection when it is not installed -- which is what a module-level
# `import flet` did. Every import below is deliberately after it.
pytest.importorskip("flet", reason="flet is an optional extra")



from graph_fixtures import make_network  # noqa: E402
from usd_chunks.plugins import chunk_flet_nodegraph as fg  # noqa: E402


class FakePage:
    """The slice of ft.Page main() touches.

    Deliberately not a Mock: a Mock accepts any attribute, so it would keep passing if
    main() started using a page API that does not exist -- which is the class of bug this
    file is here to catch.
    """

    def __init__(self):
        self.controls = []
        self.title = ""
        self.bgcolor = None
        self.padding = None
        self.width = 1200
        self.height = 800
        self.on_keyboard_event = None
        self.opened = []
        #: Flet requires a FilePicker to be here before it can open one, so main() puts
        #: the widgets' pickers in. Adding it to the stand-in is the point of the
        #: stand-in: a Mock would have accepted page.overlay silently.
        self.overlay = []

    def add(self, *controls):
        self.controls.extend(controls)

    def open(self, dialog):
        self.opened.append(dialog)

    def close(self, dialog):
        if dialog in self.opened:
            self.opened.remove(dialog)

    def update(self):
        pass


@pytest.fixture
def page():
    return FakePage()


@pytest.fixture
def network(tmp_path):
    return make_network(tmp_path / "main.usda")


class TestMainBuilds:
    def test_it_does_not_raise(self, page, network):
        path, context = network
        fg.main(page, path, context)

    def test_it_adds_content_to_the_page(self, page, network):
        path, context = network
        fg.main(page, path, context)
        assert page.controls, "main() added nothing to the page"

    def test_file_pickers_reach_the_page_overlay(self, page, tmp_path):
        """A FileBrowser param's picker cannot open until it is in page.overlay, and only
        the page can put it there -- the `reference` chunk has a file_path param."""
        path, context = make_network(tmp_path / "picker.usda",
                                     modules=("reference", "reference"))
        fg.main(page, path, context)
        assert page.overlay, "no file picker reached the overlay"

    def test_it_wires_the_keyboard(self, page, network):
        path, context = network
        fg.main(page, path, context)
        assert callable(page.on_keyboard_event)

    def test_it_titles_the_window_after_the_file(self, page, network):
        path, context = network
        fg.main(page, path, context)
        assert "main.usda" in page.title

    def test_it_works_without_an_explicit_context(self, page, network):
        """The command line takes the context as an optional second argument."""
        path, _context = network
        fg.main(page, path)
        assert page.controls


class TestEveryChunkTypeStarts:
    """The Dropdown regression, at the level it actually happened.

    A Menu param crashed startup. Building the widget in isolation (test_flet_widgets)
    catches that now, but this catches it through the real entry point, which is how a
    user meets it.
    """

    # edit_target and create_layer carry Menu params -- the widget the Dropdown crash was
    # in. Verified they do: with on_select reverted to on_change, these fail and the
    # transform-only cases still pass, which is why the list is not just "transform".
    @pytest.mark.parametrize("module", ["transform", "reference", "sublayer",
                                        "edit_target", "create_layer"])
    def test_a_network_of_that_chunk_starts(self, page, tmp_path, module):
        path, context = make_network(tmp_path / f"{module}.usda",
                                     modules=(module, module))
        fg.main(page, path, context)
        assert page.controls


class TestTheViewIsUsableAfterMain:
    def test_widgets_are_marked_live(self, page, network):
        """Before page.add a widget must stay silent; after it, it has to be allowed to
        talk to the client or nothing ever updates."""
        path, context = network
        fg.main(page, path, context)
        # main() holds the view only through the closures it wired; reach it from the
        # keyboard handler, which is bound to the instance.
        view = page.on_keyboard_event.__self__
        assert view.live
        assert all(w.live for w in view.param_widgets)

    def test_the_focus_tracker_is_per_view(self, page, tmp_path):
        a, b = FakePage(), FakePage()
        path1, ctx1 = make_network(tmp_path / "a.usda")
        path2, ctx2 = make_network(tmp_path / "b.usda")
        fg.main(a, path1, ctx1)
        fg.main(b, path2, ctx2)
        view_a = a.on_keyboard_event.__self__
        view_b = b.on_keyboard_event.__self__
        view_a.focus.enter()
        assert view_a.focus.is_editing()
        assert not view_b.focus.is_editing(), "two views share one focus tracker"
