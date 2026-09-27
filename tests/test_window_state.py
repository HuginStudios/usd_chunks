"""The host window's size and dock layout survive a restart.

usdview saves neither, so every launch opened small with a narrow Chunks dock that had to be
dragged out by hand before any work could start.

Sizes here stay well inside Qt's offscreen 800x800 virtual screen: anything larger is clamped
to it, and a test asserting on a clamped size is asserting on the platform plugin.
"""
from __future__ import annotations

import pytest
from PySide6 import QtCore, QtWidgets

from usd_chunks.frontends.qt import window_state as chunk_window_state


@pytest.fixture
def scratch_settings(monkeypatch, tmp_path):
    """A QSettings backed by a throwaway ini file, so a test never touches the real one."""
    path = str(tmp_path / "window.ini")
    monkeypatch.setattr(
        chunk_window_state,
        "_settings",
        lambda: QtCore.QSettings(path, QtCore.QSettings.Format.IniFormat),
    )
    return path


def _window(qapp, width, height):
    """A stand-in for usdview's main window: one named dock, shown, laid out.

    Shown deliberately. `restoreState` moves docks by driving the main window's layout, and a
    window that was never shown has not run one -- the dock keeps its old width and the test
    reads a pass or a fail that says nothing about the code.
    """
    win = QtWidgets.QMainWindow()
    # A central widget, so the dock is a strip beside something rather than the whole
    # window -- without one Qt gives the dock the full width and there is no width to
    # restore.
    win.setCentralWidget(QtWidgets.QWidget())
    dock = QtWidgets.QDockWidget("Chunks", win)
    dock.setObjectName("Chunks")
    dock.setWidget(QtWidgets.QWidget())
    win.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, dock)
    win.resize(width, height)
    win.show()
    qapp.processEvents()
    return win, dock


def test_size_comes_back_on_the_next_launch(qapp, scratch_settings):
    first, _ = _window(qapp, 640, 480)
    chunk_window_state.install(first)
    first.resize(560, 420)
    qapp.processEvents()
    first.close()

    second, _ = _window(qapp, 300, 200)
    chunk_window_state.install(second)

    assert second.size() == QtCore.QSize(560, 420)


def test_dock_width_comes_back_too(qapp, scratch_settings):
    """The narrow node graph is the half of this that actually gets in the way."""
    first, first_dock = _window(qapp, 640, 480)
    chunk_window_state.install(first)
    first.resizeDocks([first_dock], [300], QtCore.Qt.Orientation.Horizontal)
    qapp.processEvents()
    saved_width = first_dock.width()
    first.close()

    second, second_dock = _window(qapp, 640, 480)
    assert second_dock.width() != saved_width  # or the test proves nothing
    chunk_window_state.install(second)
    qapp.processEvents()

    assert second_dock.width() == saved_width


def test_first_run_leaves_the_defaults_alone(qapp, scratch_settings):
    win, _ = _window(qapp, 512, 384)
    chunk_window_state.install(win)
    assert win.size() == QtCore.QSize(512, 384)


def test_offscreen_geometry_is_discarded(qapp, scratch_settings, monkeypatch):
    """A window remembered on a monitor that has since been unplugged must not restore
    off-canvas -- unreachable is worse than small, which is the thing being fixed."""
    win, _ = _window(qapp, 640, 480)
    chunk_window_state.install(win)
    win.close()

    monkeypatch.setattr(chunk_window_state, "_is_reachable", lambda _w: False)
    reopened, _ = _window(qapp, 300, 200)
    chunk_window_state.install(reopened)

    assert reopened.size() == QtCore.QSize(300, 200)
    # ...and the bad rectangle is gone, rather than waiting to be reapplied next launch.
    settings = chunk_window_state._settings()
    assert not settings.value(chunk_window_state._GEOMETRY_KEY)


def test_saving_twice_does_not_raise_on_a_dead_window(qapp, scratch_settings):
    """Close fires, then aboutToQuit fires behind it -- by which point the C++ window may
    already be gone, and shutdown is not a place to raise from."""
    win, _ = _window(qapp, 400, 300)
    chunk_window_state.install(win)
    win.close()

    class _Destroyed:
        """What a QWidget whose C++ side is gone behaves like from Python."""

        def saveGeometry(self):
            raise RuntimeError("Internal C++ object already deleted.")

    saver = win.findChild(chunk_window_state._SaveOnClose)
    saver._window = _Destroyed()
    saver._saved = False
    saver._save()  # must not raise
