"""A FileBrowser param's settings reach the file dialog, in every frontend.

Qt honoured `pattern`, `extensions`, DIRECTORY and FILE_EXISTING; the Flet and ImGui
frontends rendered a FileBrowser as a bare text field with no browse button at all. The
answers are toolkit-free, so they are computed once in chunk_widget_api and all three read
them -- these tests pin the rules so a frontend cannot quietly disagree.
"""
from __future__ import annotations

import pytest

from usd_chunks import chunk, chunk_widget_api
from usd_chunks.chunk_flags import ChunkFlags


def browser(name="file_path", **kwargs):
    param = chunk.FileBrowser(name)
    for key, value in kwargs.items():
        setattr(param, key, value)
    return param


class TestPatterns:
    def test_no_extensions_means_everything(self):
        spec = chunk_widget_api.file_dialog_spec(browser())
        assert spec.patterns == ("*",)

    def test_extensions_are_appended_to_the_pattern(self):
        """`pattern` "*" with [".usda", ".usdc"] means ("*.usda", "*.usdc") -- the shape
        Qt has always built, written down so the others cannot drift from it."""
        spec = chunk_widget_api.file_dialog_spec(
            browser(extensions=[".usda", ".usdc"]))
        assert spec.patterns == ("*.usda", "*.usdc")

    def test_a_custom_pattern_is_kept(self):
        spec = chunk_widget_api.file_dialog_spec(browser(pattern="scene_*"))
        assert spec.patterns == ("scene_*",)

    def test_a_custom_pattern_combines_with_extensions(self):
        spec = chunk_widget_api.file_dialog_spec(
            browser(pattern="scene_*", extensions=[".usd"]))
        assert spec.patterns == ("scene_*.usd",)

    def test_an_empty_pattern_falls_back_to_star(self):
        spec = chunk_widget_api.file_dialog_spec(browser(pattern=""))
        assert spec.patterns == ("*",)


class TestFlags:
    def test_a_plain_browser_picks_a_file_that_need_not_exist(self):
        """FILE_EXISTING unset is the default and means a DESTINATION -- an output file
        that has not been written yet, like Export ChunkGroup's Output File."""
        spec = chunk_widget_api.file_dialog_spec(browser())
        assert not spec.directory
        assert not spec.must_exist

    def test_file_existing_requires_an_existing_file(self):
        param = browser()
        param.flags |= ChunkFlags.ParamFlags.FILE_EXISTING
        assert chunk_widget_api.file_dialog_spec(param).must_exist

    def test_directory_picks_a_folder(self):
        param = browser()
        param.flags |= ChunkFlags.ParamFlags.DIRECTORY
        assert chunk_widget_api.file_dialog_spec(param).directory


class TestTitle:
    def test_the_title_comes_from_the_label(self):
        param = browser("output_file")
        param.label = "Output File"
        assert chunk_widget_api.file_dialog_spec(param).title == "Output File"

    def test_it_falls_back_to_the_name(self):
        assert chunk_widget_api.file_dialog_spec(browser("output_file")).title == "output_file"


class TestEveryFrontendOffersBrowsing:
    """The gap this closes: two of the three had no browse affordance at all."""

    def test_flet_builds_a_file_widget_not_a_text_field(self):
        from usd_chunks.plugins import chunk_flet
        factory = chunk_flet.ChunkFletWidgetFactory()
        param = browser()
        widget = factory.create_widget(param)
        assert isinstance(widget, chunk_flet.FletFileWidget), (
            "a FileBrowser must not fall back to a plain text field")
        assert widget.picker is not None

    def test_flet_hands_its_pickers_back_for_the_page_overlay(self):
        """A FilePicker cannot open until it is in page.overlay, and only the view can put
        it there -- so the widgets have to surrender them."""
        from usd_chunks.plugins import chunk_flet
        factory = chunk_flet.ChunkFletWidgetFactory()
        param = browser()
        widget = factory.create_widget(param)
        assert chunk_flet.file_pickers([widget]) == [widget.picker]

    def test_flet_applies_what_the_picker_returns(self):
        """pick_files hands back a LIST, save_file and get_directory_path hand back a
        path -- one write-back has to cope with both shapes.

        `param` is held in a local on purpose: a widget keeps only a weakref to its param
        (see the NoticeConnection lifetime rule), so letting it fall out of scope makes
        widget.param raise "reference to param is dead" rather than fail an assertion.
        """
        from usd_chunks.plugins import chunk_flet
        factory = chunk_flet.ChunkFletWidgetFactory()
        param = browser()
        widget = factory.create_widget(param)
        widget.apply_picked("C:/tmp/one.usda")
        assert param.value == "C:/tmp/one.usda"

        class _File:
            path = "C:/tmp/two.usda"

        widget.apply_picked([_File()])
        assert param.value == "C:/tmp/two.usda"
        widget.apply_picked(None)
        assert param.value == "C:/tmp/two.usda", "a cancelled dialog must not clear it"

    def test_imgui_has_a_browse_path(self):
        pytest.importorskip("imgui_bundle")
        from usd_chunks.plugins import chunk_imgui
        assert callable(chunk_imgui._browse)
        assert callable(chunk_imgui.poll_dialogs)

    def test_imgui_polling_is_safe_with_nothing_pending(self):
        pytest.importorskip("imgui_bundle")
        from usd_chunks.plugins import chunk_imgui
        chunk_imgui._pending_dialogs.clear()
        assert chunk_imgui.poll_dialogs() is False
