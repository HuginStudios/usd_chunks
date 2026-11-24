"""Auto quick save in a Qt host: the timer, the recovery prompt and the menu toggle.

usd_chunks.autosave decides where quick saves go, writes and recovers them, and tells an
edit from a cook. This module only adds what needs Qt:

- `AutosaveController`: one per open network. A timer on the main thread writes a quick
  save while the network has unsaved changes, one file per editing session in the user's
  quick-save folder; `check_recovery` offers the quick saves left behind (a crash, a quit
  without saving) before the network's chunks load; `on_saved` deletes them once the user
  saves the network to its own file.
- `start_after_recovery`: the "check, ask, then load the chunks" order a host starts up in,
  so the network in the file never cooks before the user has chosen.
- `choose_recovery`: the default prompt.
- `configure_toggle_action`: the Auto Quick Save menu entry.
- `notify_saved`: what Save Scene calls after it wrote a file.

Quick saves are off until a user turns them on with the toggle (QSettings, per user).
USD_CHUNKS_AUTOSAVE_MINUTES=0 keeps them off for everyone (a site or a farm sets it). Neither
stops the recovery prompt: quick saves already on disk are the user's work, so they are
always offered.
"""
from __future__ import annotations

import logging
import os
import time
import weakref
from collections.abc import Callable, Sequence

from pxr import Sdf, Usd
from PySide6 import QtCore, QtGui, QtWidgets

from usd_chunks import autosave, chunk_registry
from usd_chunks.frontends.qt import window_state

logger = logging.getLogger(__name__)

#: The QSettings key of the per-user toggle. False (off) when unset.
SETTINGS_KEY = "autosave/enabled"

#: What a recovery chooser returns to delete every quick save. A path recovers that file;
#: None (the prompt closed without a choice) keeps them all and recovers nothing.
DISCARD = "discard"

#: The menu entry's label, and its tooltip when the environment has turned quick saves off.
ACTION_TEXT = "Auto Quick Save"
ENV_OFF_TOOLTIP = f"Turned off by {chunk_registry.USD_CHUNKS_AUTOSAVE_MINUTES.key}=0"

Chooser = Callable[[Sequence[str]], "str | None"]

_controllers: weakref.WeakSet[AutosaveController] = weakref.WeakSet()


# -- settings --------------------------------------------------------------------------------


def env_off() -> bool:
    """Whether USD_CHUNKS_AUTOSAVE_MINUTES=0 has turned quick saves off for everyone."""
    return autosave.autosave_minutes() <= 0


def user_enabled() -> bool:
    """The per-user toggle, from QSettings. Off unless the user turned it on."""
    value = window_state.settings().value(SETTINGS_KEY, False)
    if isinstance(value, str):  # an ini file gives back text
        return value.strip().lower() not in ("false", "0", "no", "off")
    return bool(value)


def set_user_enabled(on: bool) -> None:
    """Store the per-user toggle, and start or stop every controller's timer to match."""
    settings = window_state.settings()
    settings.setValue(SETTINGS_KEY, bool(on))
    settings.sync()
    for controller in list(_controllers):
        controller.apply_settings()


def enabled() -> bool:
    """Whether quick saves are written: the environment allows them and the user wants them."""
    return not env_off() and user_enabled()


def _same_file_key(path: str | os.PathLike) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


_swept = False


def _sweep_once() -> None:
    """Delete month-old quick saves of any network, once per session (autosave.sweep)."""
    global _swept
    if not _swept:
        _swept = True
        autosave.sweep()


# -- the controller --------------------------------------------------------------------------


class AutosaveController(QtCore.QObject):
    """Quick saves of one network at a time, on a timer, and its recovery.

    Use, per network the host opens:

    1. `set_network(stage.GetRootLayer())`, before the network's chunks load.
    2. `check_recovery()` if `pending()` lists anything -- still before they load.
    3. `attach(scene)` once they have loaded: from here on an edit of the network is
       written to a quick save on the next tick.
    4. `close()` when the host drops the stage (File > Open), then back to 1.

    Every slot here catches what it raises: an exception from a Qt timer slot takes the
    host application down.
    """

    def __init__(self, parent: QtCore.QObject | None = None):
        super().__init__(parent)
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.tick)
        self._root_layer: Sdf.Layer | None = None
        self._path: str | None = None
        self._scene = None
        self._tracker: autosave.ChangeTracker | None = None
        #: This session's quick-save file, named when it is first written. A quick save of
        #: another session (one offered at recovery) is never written over.
        self._target: str | None = None
        self._last_text: str | None = None
        self._warned = False
        _controllers.add(self)

    # -- the network ---------------------------------------------------------------------

    @property
    def network_path(self) -> str | None:
        """The network's file, as recorded by set_network; None for an unsaved stage."""
        return self._path

    def is_quick_save(self) -> bool:
        """Whether the network open is itself a quick save: then nothing is offered or written."""
        return self._path is not None and autosave.is_quick_save(self._path)

    def set_network(self, root_layer: Sdf.Layer | None) -> None:
        """Record the network about to load: its root layer and that layer's file.

        Forgets what belonged to the previous network: its session file, the last text
        written, the warning already given.
        """
        self.close()
        self._root_layer = root_layer
        real = root_layer.realPath if root_layer is not None and not root_layer.anonymous else ""
        self._path = os.path.realpath(real) if real else None
        self._target = None
        self._last_text = None
        self._warned = False

    def pending(self) -> list[str]:
        """The quick saves to offer for the recorded network, newest first."""
        if self._path is None or self.is_quick_save():
            return []
        return autosave.pending_recovery(self._path)

    def check_recovery(self, chooser: Chooser | None = None) -> str | None:
        """Offer the network's quick saves, and do what the user chooses.

        Before the network's chunks load. chooser gets the files newest first and returns
        one to recover, DISCARD, or None; choose_recovery (a dialog) when omitted.

        - A file: its network goes into the root layer, which is left unsaved. The file is
          kept until the network is saved: this session's quick saves go to a new one.
        - DISCARD: every quick save is deleted.
        - None (the dialog closed with Esc or its close button): nothing is recovered and
          nothing is deleted, so they are all still there to recover next time.

        Returns:
            The file recovered, or None.
        """
        paths = self.pending()
        if not paths:
            return None
        if chooser is None:
            def chooser(found):
                return choose_recovery(found, self._path)
        choice = chooser(list(paths))
        if choice == DISCARD:
            autosave.discard(self._path)
            return None
        if choice is None:
            return None
        if _same_file_key(choice) not in {_same_file_key(p) for p in paths}:
            logger.warning("Not a quick save of %s: %s. Nothing recovered.", self._path, choice)
            return None
        autosave.recover_into(self._root_layer, choice)
        logger.info("Recovered %s into %s. Save Scene to keep it.", choice, self._path)
        return choice

    # -- writing -------------------------------------------------------------------------

    def attach(self, scene) -> None:
        """Start watching the scene's stage for edits, now that its chunks have loaded."""
        if self._tracker is not None:
            self._tracker.close()
        self._scene = scene
        stage: Usd.Stage | None = scene.get_stage() if scene is not None else None
        self._tracker = autosave.ChangeTracker(stage) if stage is not None else None
        self.apply_settings()

    @property
    def enabled(self) -> bool:
        return enabled()

    def set_enabled(self, on: bool) -> None:
        """Turn quick saves on or off for this user (QSettings), and start or stop the timer."""
        set_user_enabled(on)
        self.apply_settings()

    def apply_settings(self) -> None:
        """Run the timer while quick saves are on and a stage is watched; stop it otherwise."""
        if self.enabled and self._tracker is not None:
            interval = max(1, round(autosave.autosave_minutes() * 60_000))
            if not self._timer.isActive() or self._timer.interval() != interval:
                self._timer.start(interval)
        else:
            self._timer.stop()

    @property
    def timer(self) -> QtCore.QTimer:
        return self._timer

    def tick(self) -> None:
        """Write a quick save if the network changed since the last one. Never raises."""
        try:
            self._tick()
        except Exception:  # noqa: BLE001 - a timer slot must not raise
            logger.exception("Quick save failed")

    def _tick(self) -> None:
        tracker = self._tracker
        if (tracker is None or self._scene is None or self._path is None or self.is_quick_save()
                or not self.enabled or not tracker.changed):
            return
        if self._target is None:
            # This session's first quick save: a new file, so no other session's is touched.
            self._target = autosave.new_target(self._path)
            autosave.prune(self._path, autosave.KEEP_PER_NETWORK - 1)
            _sweep_once()
        target = self._target
        try:
            text = autosave.write_quick_save(self._scene, target, self._path, self._last_text)
        except Exception as exc:  # noqa: BLE001 - any failure: warn once, retry next tick
            if not self._warned:
                self._warned = True
                logger.warning("Could not write a quick save of %s to %s: %s. Trying again every few "
                               "minutes; save the network to keep your changes.", self._path, target, exc)
            return
        if text is not None:
            self._last_text = text
        tracker.mark_clean()
        self._warned = False

    def on_saved(self, path: str | os.PathLike) -> None:
        """Save Scene wrote path. Saved to the network's own file, its quick saves go."""
        try:
            if self._path is None or self.is_quick_save():
                return
            if _same_file_key(path) != _same_file_key(self._path):
                return  # saved elsewhere: the network's own file still lacks the changes
            autosave.discard(self._path)
            self._target = None
            self._last_text = None
            if self._tracker is not None:
                self._tracker.mark_clean()
        except Exception:  # noqa: BLE001 - the save itself worked; never fail it here
            logger.exception("Could not delete the quick saves of %s", self._path)

    def stop(self) -> None:
        """Stop the timer."""
        self._timer.stop()

    def close(self) -> None:
        """Stop the timer and stop watching the stage. Safe to call more than once.

        The root layer is let go too, so a stage the host closed does not stay in memory,
        with its unsaved edits, for the next open of the same file to find.
        """
        self.stop()
        if self._tracker is not None:
            self._tracker.close()
        self._tracker = None
        self._scene = None
        self._root_layer = None


def notify_saved(path: str | os.PathLike) -> None:
    """Tell every controller that Save Scene wrote path."""
    for controller in list(_controllers):
        controller.on_saved(path)


# -- start-up ---------------------------------------------------------------------------------


def start_after_recovery(
    controller: AutosaveController,
    stage: Usd.Stage,
    start: Callable[[], None],
    defer: Callable[[Callable[[], None]], None],
    chooser: Chooser | None = None,
) -> bool:
    """Load a network's chunks, after offering its quick saves if it has any.

    Without quick saves to offer, start() runs now, exactly as it would without autosave.
    With some, the prompt and start() are handed to defer (the host runs them once its
    window is up and no load is under way: a dialog shown mid-load crashes usdview), and
    start() runs only after the choice is applied, so the network as saved never cooks
    first. start() runs even if the recovery fails.

    Args:
        controller: The host's controller; set_network is called here.
        stage: The stage just opened, before its chunks load.
        start: Loads the chunks (and attaches the controller).
        defer: Runs a function later, on the main thread.
        chooser: Passed to check_recovery.

    Returns:
        True when start() was deferred.
    """
    controller.set_network(stage.GetRootLayer())
    if not controller.pending():
        start()
        return False

    def recover_then_start():
        try:
            controller.check_recovery(chooser)
        except Exception:  # noqa: BLE001 - the network still has to load
            logger.exception("Could not recover a quick save of %s; opening it as saved", controller.network_path)
        start()

    defer(recover_then_start)
    return True


# -- the prompt -------------------------------------------------------------------------------


def _when(path: str, seconds: bool = False) -> str:
    """When path was last written, as date and time: `2026-11-24 14:30` (`14:30:05` with seconds)."""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M",
                             time.localtime(os.path.getmtime(path)))
    except OSError:
        return "?"


def recovery_buttons(paths: Sequence[str]) -> list[tuple[str, str]]:
    """(label, choice) for each button of the prompt, in order: the recover buttons, newest
    first, then the discard button. Labels say when each was written, to the second where two
    would read the same to the minute."""
    if len(paths) == 1:
        return [("Recover", paths[0]), ("Discard", DISCARD)]
    times = [_when(p) for p in paths]
    if len(set(times)) < len(times):
        times = [_when(p, seconds=True) for p in paths]
    buttons = [(f"Recover {'latest' if i == 0 else 'earlier'} ({t})", p)
               for i, (t, p) in enumerate(zip(times, paths))]
    return [*buttons, ("Discard all", DISCARD)]


class RecoveryBox(QtWidgets.QMessageBox):
    """The recovery prompt: a QMessageBox that Esc and the window's close button close.

    A QMessageBox with no Cancel-like button has no escape button: it ignores Esc, refuses
    to close, and on Windows greys out the title bar's close button. So it has a Cancel
    button, hidden, as its escape button. Qt then closes it on Esc or the close button as
    if that were clicked, which choose_recovery takes as no choice: nothing is recovered
    and nothing is deleted.
    """

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        #: The hidden escape button: clicked by Esc and by closing the window.
        self.cancel_button = self.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        self.cancel_button.hide()
        self.setEscapeButton(self.cancel_button)

    def dismissed(self) -> bool:
        """Whether the box closed with no choice made (Esc or the close button)."""
        clicked = self.clickedButton()
        return clicked is None or clicked is self.cancel_button


def choose_recovery(paths: Sequence[str], network_path: str | None = None,
                    parent: QtWidgets.QWidget | None = None) -> str | None:
    """Ask which quick save to recover: a message box with a button per file, newest first.

    No Cancel button: the choice is Recover or Discard. A box closed without one (Esc, the
    close button) returns None, which keeps every file for next time.

    Returns:
        A path, DISCARD, or None.
    """
    name = os.path.basename(network_path) if network_path else "The network"
    box = RecoveryBox(parent or QtWidgets.QApplication.activeWindow())
    box.setIcon(QtWidgets.QMessageBox.Icon.Question)
    box.setWindowTitle("Recover quick save")
    if len(paths) == 1:
        box.setText(f"{name} has a quick save with changes that were never saved ({_when(paths[0])}).")
    else:
        box.setText(f"{name} has {len(paths)} quick saves with changes that were never saved.")
    box.setInformativeText(
        "Recover puts the quick save's network in place of the saved one, unsaved: Save Scene "
        "to keep it. Discard deletes the quick saves. Closing this keeps them for next time.")
    box.setDetailedText("\n".join(paths))
    choices = {}
    for i, (label, choice) in enumerate(recovery_buttons(paths)):
        role = (QtWidgets.QMessageBox.ButtonRole.DestructiveRole if choice == DISCARD
                else QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        button = box.addButton(label, role)
        choices[button] = choice
        if i == 0:
            box.setDefaultButton(button)
    box.exec()
    if box.dismissed():
        return None
    return choices.get(box.clickedButton())


# -- the menu toggle --------------------------------------------------------------------------


def configure_toggle_action(action: QtGui.QAction, on_toggled: Callable[[bool], None] | None = None) -> None:
    """Make action the Auto Quick Save toggle: checkable, checked from the user's setting.

    Greyed out, with a tooltip saying why, when USD_CHUNKS_AUTOSAVE_MINUTES=0. Toggling it
    stores the setting (set_user_enabled) and then calls on_toggled.
    """
    action.setCheckable(True)
    with QtCore.QSignalBlocker(action):
        action.setChecked(user_enabled())
    if env_off():
        action.setEnabled(False)
        action.setToolTip(ENV_OFF_TOOLTIP)
    else:
        action.setToolTip("Keep a quick save of the network every few minutes while it has unsaved "
                          "changes, in your own user folder. Opening the network after a crash "
                          "offers it back")
    # A QMenu hides its actions' tooltips unless told otherwise.
    for menu in _menus_of(action):
        menu.setToolTipsVisible(True)

    def toggled(on: bool) -> None:
        try:
            set_user_enabled(on)
            if on_toggled is not None:
                on_toggled(on)
        except Exception:  # noqa: BLE001 - a menu slot must not take the host down
            logger.exception("Could not change the Auto Quick Save setting")

    action.toggled.connect(toggled)


def _menus_of(action: QtGui.QAction) -> list[QtWidgets.QMenu]:
    """The menus action is in."""
    owners = action.associatedObjects() if hasattr(action, "associatedObjects") else action.associatedWidgets()
    return [owner for owner in owners if isinstance(owner, QtWidgets.QMenu)]


def find_action(menu_bar: QtWidgets.QMenuBar, text: str = ACTION_TEXT) -> QtGui.QAction | None:
    """The action labelled text in any menu of menu_bar, or None.

    Walks the menu bar's QMenu children rather than QAction.menu(): in PySide6 6.9 the
    wrapper that returns owns the menu, and letting it go deletes the host's menu.
    """
    for menu in menu_bar.findChildren(QtWidgets.QMenu):
        for action in menu.actions():
            if action.text().replace("&", "") == text:
                return action
    return None
