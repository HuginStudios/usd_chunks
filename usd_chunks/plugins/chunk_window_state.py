"""Remembers the host window's size and dock layout between sessions.

usdview does not save its own window geometry. Its state file (``~/.usdview/*state.json``)
keeps splitter sizes, column visibility and view settings, and nothing about how big the
window was or where it sat -- so every launch opens at whatever size Qt picks, with the
Chunks dock at the fixed width :func:`chunk_qt.build_chunk_docker` asks for. On a large
monitor that is a small window with a narrow node graph in it, resized by hand before any
work can start, every single session. Reported as exactly that.

Kept in ``QSettings`` rather than added to usdview's own state file: that file is keyed by
usdview's ``SETTINGS_VERSION`` and rewritten wholesale by its ``ConfigManager``, so an entry
we put in it would vanish the day usdview bumps that version -- and it is written only on a
clean exit through usdview's own close path, which a crash or a kill skips.

``QMainWindow.saveState()`` is what carries the dock layout, and it identifies widgets by
``objectName``. A dock without one is dropped from the saved state with a warning, which is
why :func:`chunk_qt.build_chunk_docker` names its dock.
"""

from __future__ import annotations

import logging

from PySide6 import QtCore, QtWidgets

logger = logging.getLogger(__name__)

_ORG = "HuginStudios"
_APP = "usd_chunks"

_GEOMETRY_KEY = "window/geometry"
_STATE_KEY = "window/state"

#: Bumped when the dock layout changes shape in a way that makes an older saved state
#: restore something misleading rather than nothing. `restoreState` takes this as its
#: version argument and refuses a mismatch, which is the whole point: a stale layout is
#: silently ignored instead of half-applied.
_STATE_VERSION = 1

#: How much of the window has to land on a screen for the saved geometry to be worth using.
#: A window remembered on a monitor that is no longer plugged in restores off-canvas, where
#: it cannot be dragged back -- worse than opening small, which is the thing being fixed.
_MIN_VISIBLE_FRACTION = 0.25


def _settings() -> QtCore.QSettings:
    return QtCore.QSettings(_ORG, _APP)


def _is_reachable(window: QtWidgets.QMainWindow) -> bool:
    """True if enough of the restored frame sits on some connected screen to grab."""
    frame = window.frameGeometry()
    area = frame.width() * frame.height()
    if area <= 0:
        return False
    visible = 0
    for screen in QtWidgets.QApplication.screens():
        overlap = frame.intersected(screen.availableGeometry())
        visible += overlap.width() * overlap.height()
    return visible >= area * _MIN_VISIBLE_FRACTION


class _SaveOnClose(QtCore.QObject):
    """Writes the layout out when the host window closes.

    An event filter rather than a `closeEvent` override, because the window belongs to
    usdview and subclassing it is not ours to do. Close is the last moment the geometry
    still means anything -- by `aboutToQuit` the window may already be gone -- so that is
    the trigger, with `aboutToQuit` kept as a backstop for the paths that never deliver a
    close event.
    """

    def __init__(self, window: QtWidgets.QMainWindow):
        super().__init__(window)
        self._window = window
        self._saved = False
        window.installEventFilter(self)
        if (app := QtWidgets.QApplication.instance()) is not None:
            app.aboutToQuit.connect(self._save)

    def eventFilter(self, obj, event) -> bool:
        # getattr, not self._window: Qt keeps calling this during teardown, after Python has
        # already emptied the instance's __dict__, and an AttributeError raised from inside
        # an event filter is printed by PySide and swallowed -- noise on every exit.
        window = getattr(self, "_window", None)
        if window is not None and obj is window and event.type() == QtCore.QEvent.Type.Close:
            self._save()
        return False

    def _save(self) -> None:
        # Once. Close fires, then aboutToQuit fires behind it, and by the second the C++
        # window is often already destroyed -- asking a dead QWidget for its geometry
        # raises, and doing it during shutdown is not a place to raise from.
        if self._saved:
            return
        try:
            settings = _settings()
            settings.setValue(_GEOMETRY_KEY, self._window.saveGeometry())
            settings.setValue(_STATE_KEY, self._window.saveState(_STATE_VERSION))
            settings.sync()
        except RuntimeError:  # the window went away first
            pass
        except Exception:  # noqa: BLE001 - a remembered size is never worth a crash on exit
            logger.debug("Could not save window layout", exc_info=True)
        finally:
            self._saved = True


def install(window: QtWidgets.QMainWindow) -> None:
    """Restore the saved layout onto `window`, and arrange to save it again on close.

    Called after the Chunks dock exists: `restoreState` can only place docks that are
    already there, so restoring first would drop ours and leave it at the default width.

    Nothing saved yet is the ordinary first-run case and does nothing, leaving whatever
    usdview and `build_chunk_docker` decided.
    """
    if window is None:
        return
    try:
        settings = _settings()
        geometry = settings.value(_GEOMETRY_KEY)
        state = settings.value(_STATE_KEY)

        if geometry:
            # Snapshotted before the attempt, so an unusable rectangle can be put back
            # exactly. showNormal() only leaves the window unmaximised, at whatever size the
            # bad restore just gave it, which is not the same thing as never having tried.
            fallback = window.saveGeometry()
            window.restoreGeometry(geometry)
            if not _is_reachable(window):
                logger.debug("Saved window geometry is off-screen; keeping the default")
                # Put back rather than leave the window somewhere it cannot be dragged back
                # from, and drop the entry so the same bad rectangle is not reapplied on the
                # next launch either.
                window.restoreGeometry(fallback)
                settings.remove(_GEOMETRY_KEY)

        # Independent of the above: where the window sat and how the docks were arranged
        # inside it are separate questions, and a monitor that went away only invalidates
        # the first one.
        if state:
            window.restoreState(state, _STATE_VERSION)
    except Exception:  # noqa: BLE001 - a remembered size is never worth failing a launch over
        logger.debug("Could not restore window layout", exc_info=True)

    _SaveOnClose(window)
