"""Auto quick save in Qt: the controller's timer tick, the recovery prompt, Save Scene, the toggle.

The host's order is: open the stage, offer its quick saves, load its chunks, then watch it
for edits (frontends.qt.autosave.start_after_recovery). `_launch` does the same with a
script Scene, so every test here runs the way usdview opens a file. The timer is never
waited on: each test calls `tick()`, the timer's slot.
"""
from __future__ import annotations

import logging
import os
import time

import pytest
from pxr import Sdf, Usd

pytest.importorskip("PySide6")

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from usd_chunks import Scene, autosave  # noqa: E402
from usd_chunks.frontends.qt import autosave as qt_autosave  # noqa: E402
from usd_chunks.frontends.qt import window_state  # noqa: E402

ENV = "USD_CHUNKS_AUTOSAVE_MINUTES"
DIR_ENV = "USD_CHUNKS_AUTOSAVE_DIR"
LOGGER = "usd_chunks.frontends.qt.autosave"
TX = "inputs:translate_grp:tx"
TRANSFORM = "/chunks/ChunkGroup/Transform"


@pytest.fixture(autouse=True)
def scratch_settings(monkeypatch, tmp_path):
    """QSettings in a throwaway ini file and quick saves in a folder of the test's own: a
    test never touches the user's settings or quick saves. Quick saves are turned on here,
    as a user turns them on; test_quick_saves_are_off_until_turned_on checks the default."""
    path = str(tmp_path / "settings.ini")
    monkeypatch.setattr(window_state, "_settings", lambda: QtCore.QSettings(path, QtCore.QSettings.Format.IniFormat))
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.setenv(DIR_ENV, str(tmp_path / "quick_saves"))
    qt_autosave.set_user_enabled(True)


@pytest.fixture
def net(tmp_path):
    path = tmp_path / "shot.usda"
    with Scene.new(path=str(path)) as scene:
        box = scene.create.cube(prim_path="/geo/box")
        lift = scene.create.transform(box)
        scene.set_output(lift)
        scene.save()
    old = time.time() - 100
    os.utime(path, (old, old))
    return path


@pytest.fixture
def controller(qapp):
    controller = qt_autosave.AutosaveController()
    yield controller
    controller.close()


def _refuse(paths):
    raise AssertionError(f"no prompt expected, got {paths}")


class Session:
    """One "launch" of the network: its stage and Scene, opened the way the host opens it."""

    def __init__(self, path, controller, chooser=_refuse):
        self.path = path
        self.controller = controller
        self.stage = Usd.Stage.Open(str(path))
        self.scene = None

        def start():
            self.scene = Scene(self.stage)
            self.scene._owns_stage = True
            controller.attach(self.scene)

        qt_autosave.start_after_recovery(controller, self.stage, start, lambda fn: fn(), chooser)

    def chunk(self, name):
        return next(c for c in self.scene.get_chunks() if c.name == name)

    def edit(self, **values):
        self.scene.set(self.chunk("Transform"), **values)

    def value(self, name="tx"):
        """The Transform chunk's translate param on the stage; unauthored reads 0."""
        return self.stage.GetPrimAtPath(TRANSFORM).GetAttribute(f"inputs:translate_grp:{name}").Get() or 0.0

    def tx(self):
        return self.value("tx")

    def close(self):
        """Quit without saving: the next launch reads the file as it is on disk."""
        self.controller.close()
        self.scene.close()
        self.scene = self.stage = None
        layer = Sdf.Layer.Find(str(self.path))
        if layer is not None:
            layer.Reload(force=True)


@pytest.fixture
def session(net, controller):
    session = Session(net, controller)
    yield session
    if session.scene is not None:
        session.close()


def _quick_saves(net):
    """The network's quick saves on disk, newest first."""
    return autosave.quick_save_paths(net)


def _crashed_with_a_quick_save(net, controller, tx=4.0):
    """A session that edited the network, wrote a quick save, and never saved. Returns it."""
    first = Session(net, controller)
    first.edit(tx=tx)
    first.controller.tick()
    first.close()
    (main,) = _quick_saves(net)
    stamp = time.time() - 50  # after the network was saved, before anything this test writes
    os.utime(main, (stamp, stamp))
    return main


def _tx_in(path):
    layer = Sdf.Layer.OpenAsAnonymous(path)  # held: a spec of a freed layer cannot be read
    return layer.GetAttributeAtPath(f"{TRANSFORM}.{TX}").default


# -- writing ------------------------------------------------------------------------------------


def test_quick_saves_are_off_until_turned_on(net, controller):
    window_state.settings().remove(qt_autosave.SETTINGS_KEY)  # a user who never chose
    assert not qt_autosave.user_enabled()
    session = Session(net, controller)
    try:
        assert not controller.timer.isActive()
        session.edit(tx=4.0)
        controller.tick()
        assert _quick_saves(net) == []
    finally:
        session.close()


def test_tick_writes_after_an_edit_into_the_quick_save_folder(session, net, tmp_path):
    session.controller.tick()
    assert _quick_saves(net) == []  # nothing changed: nothing written
    session.edit(tx=4.0)
    session.controller.tick()
    (written,) = _quick_saves(net)
    assert os.path.dirname(written) == str(autosave.network_folder(net))
    assert sorted(p.name for p in net.parent.iterdir()) == ["quick_saves", "settings.ini", "shot.usda"]
    assert _tx_in(written) == pytest.approx(4.0)
    # Written: clean again until the next edit.
    mtime = os.path.getmtime(written)
    os.utime(written, (mtime - 10, mtime - 10))
    session.controller.tick()
    assert os.path.getmtime(written) == pytest.approx(mtime - 10)
    # The next edit goes to the same file: one file per session.
    session.edit(tx=5.0)
    session.controller.tick()
    assert _quick_saves(net) == [written]
    assert _tx_in(written) == pytest.approx(5.0)


def test_each_session_writes_its_own_file(net, controller):
    first = _crashed_with_a_quick_save(net, controller)
    before = open(first, "rb").read()
    session = Session(net, controller, lambda paths: None)  # asked, and closed unanswered
    try:
        session.edit(tx=9.0)
        controller.tick()
        newest, older = _quick_saves(net)
        assert older == first and newest != first
        assert open(first, "rb").read() == before
    finally:
        session.close()


def test_a_new_session_keeps_only_the_newest_quick_saves(net, controller, monkeypatch):
    monkeypatch.setattr(autosave, "KEEP_PER_NETWORK", 2)
    folder = autosave.network_folder(net)
    folder.mkdir(parents=True)
    stamp = time.time() - 300  # older than the network file: not offered, only kept
    for name in ("shot_1.usda", "shot_2.usda", "shot_3.usda"):
        (folder / name).write_text("#usda 1.0\n")
        stamp -= 10
        os.utime(folder / name, (stamp, stamp))
    session = Session(net, controller)
    try:
        session.edit(tx=4.0)
        controller.tick()
        names = [os.path.basename(p) for p in _quick_saves(net)]
        assert len(names) == 2 and names[1] == "shot_1.usda"
    finally:
        session.close()


def test_the_timer_runs_at_the_set_interval(session, monkeypatch):
    assert session.controller.timer.isActive()
    assert session.controller.timer.interval() == 2 * 60_000
    monkeypatch.setenv(ENV, "0.5")
    session.controller.apply_settings()
    assert session.controller.timer.interval() == 30_000
    session.controller.close()
    assert not session.controller.timer.isActive()


def test_a_failed_write_warns_once_retries_and_leaves_no_temp_file(session, net, tmp_path, monkeypatch, caplog):
    real_replace = os.replace

    def broken(*_args):
        raise PermissionError("the share is read-only")

    monkeypatch.setattr(os, "replace", broken)
    session.edit(tx=4.0)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        session.controller.tick()  # does not raise
        session.controller.tick()
        session.edit(tx=5.0)
        session.controller.tick()
    warnings = [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "read-only" in warnings[0].getMessage()
    assert _quick_saves(net) == []

    assert list(autosave.network_folder(net).iterdir()) == []  # no .tmp left behind

    monkeypatch.setattr(os, "replace", real_replace)
    session.controller.tick()  # still changed: this one writes
    (written,) = _quick_saves(net)
    assert [p.name for p in autosave.network_folder(net).iterdir()] == [os.path.basename(written)]
    assert _tx_in(written) == pytest.approx(5.0)

    # Written once, a later failure warns again.
    monkeypatch.setattr(os, "replace", broken)
    caplog.clear()
    session.edit(tx=6.0)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        session.controller.tick()
    assert len([r for r in caplog.records if r.name == LOGGER]) == 1


def test_env_zero_writes_nothing_even_with_the_toggle_on(session, net, monkeypatch):
    qt_autosave.set_user_enabled(True)
    monkeypatch.setenv(ENV, "0")
    session.controller.apply_settings()
    assert not session.controller.timer.isActive()
    session.edit(tx=4.0)
    session.controller.tick()
    assert _quick_saves(net) == []


def test_the_user_toggle(session, net):
    session.controller.set_enabled(False)
    assert window_state.settings().value(qt_autosave.SETTINGS_KEY) not in (True, "true")
    assert not session.controller.timer.isActive()
    session.edit(tx=4.0)
    session.controller.tick()
    assert _quick_saves(net) == []

    session.controller.set_enabled(True)
    assert session.controller.timer.isActive()
    session.controller.tick()
    assert len(_quick_saves(net)) == 1


# -- saving -------------------------------------------------------------------------------------


def test_save_scene_to_the_network_deletes_its_quick_saves(net, controller):
    _crashed_with_a_quick_save(net, controller)
    session = Session(net, controller, lambda paths: paths[0])
    try:
        session.edit(tx=7.0)
        controller.tick()
        assert len(_quick_saves(net)) == 2

        other = net.with_name("other.usda")
        session.scene.save(str(other))
        qt_autosave.notify_saved(str(other))
        assert len(_quick_saves(net)) == 2

        session.scene.save(str(net))
        qt_autosave.notify_saved(os.path.join(str(net.parent), ".", net.name))  # any spelling
        assert _quick_saves(net) == []
        assert not autosave.network_folder(net).exists()
        # Edited after the save: a new quick save again.
        session.edit(tx=8.0)
        controller.tick()
        assert len(_quick_saves(net)) == 1
    finally:
        session.close()


def test_save_scene_dialog_tells_the_controllers(monkeypatch, tmp_path):
    from usd_chunks.frontends.qt import factory

    told = []
    saved = []
    monkeypatch.setattr(factory, "file_dial", lambda *a, **kw: str(tmp_path / "shot"))
    monkeypatch.setattr(factory.chunk_stage, "save_scene", lambda scene, path: saved.append(path))
    monkeypatch.setattr(qt_autosave, "notify_saved", told.append)
    factory.save_scene_dialog(scene=object())
    assert saved == told == [str(tmp_path / "shot.usda")]


# -- recovering ---------------------------------------------------------------------------------


def test_discard_deletes_every_quick_save(net, controller):
    main = _crashed_with_a_quick_save(net, controller)
    seen = []
    session = Session(net, controller, lambda paths: seen.append(paths) or qt_autosave.DISCARD)
    try:
        assert seen == [[main]]
        assert _quick_saves(net) == []
        assert not autosave.network_folder(net).exists()
        assert session.tx() == pytest.approx(0.0)  # the network as saved
    finally:
        session.close()


def test_a_prompt_closed_unanswered_keeps_every_file(net, controller):
    main = _crashed_with_a_quick_save(net, controller)
    offered = []
    session = Session(net, controller, lambda paths: offered.append(paths))
    try:
        assert offered == [[main]]
        assert session.tx() == pytest.approx(0.0)  # nothing recovered
    finally:
        session.close()
    assert _quick_saves(net) == [main]


def test_recover_never_writes_over_the_recovered_file(net, controller):
    main = _crashed_with_a_quick_save(net, controller)
    before = open(main, "rb").read()
    session = Session(net, controller, lambda paths: paths[0])
    try:
        assert session.tx() == pytest.approx(4.0)
        assert session.stage.GetRootLayer().dirty  # recovered, not saved
        controller.tick()
        assert _quick_saves(net) == [main]  # nothing edited since: nothing written
        session.edit(tx=5.0)
        controller.tick()
        assert len(_quick_saves(net)) == 2
        assert open(main, "rb").read() == before
    finally:
        session.close()


def test_reload_after_recover_writes_nothing(net, controller):
    main = _crashed_with_a_quick_save(net, controller)
    session = Session(net, controller, lambda paths: paths[0])
    try:
        session.stage.Reload()
        controller.tick()
        assert _quick_saves(net) == [main]
    finally:
        session.close()


def test_recover_reload_edit_relaunch_recover_again(net, controller):
    main = _crashed_with_a_quick_save(net, controller)
    first_recovered = open(main, "rb").read()

    # 1) Recover it; 2) reload; 3) edit: the tick writes this session's own file.
    session = Session(net, controller, lambda paths: paths[0])
    session.stage.Reload()
    assert session.tx() == pytest.approx(0.0)
    # ty: after a reload, writing a param whose spec the reload took away does not reach
    # the stage (not autosave's doing), so the edit is to one the network never had.
    session.edit(ty=11.0)
    controller.tick()
    second = next(p for p in _quick_saves(net) if p != main)
    later = time.time()
    os.utime(second, (later, later))
    session.close()

    # 4) Relaunch; 5) both are offered, newest first; 6) recover the latest.
    offered = []
    relaunch = Session(net, controller, lambda paths: offered.append(paths) or paths[0])
    try:
        assert offered == [[second, main]]
        assert relaunch.value("ty") == pytest.approx(11.0)
        # 7) The first recovered network is still there, byte for byte.
        assert open(main, "rb").read() == first_recovered
        # 8) One more edit goes to a third file.
        relaunch.edit(ty=12.0)
        controller.tick()
        assert len(_quick_saves(net)) == 3
        assert open(main, "rb").read() == first_recovered
    finally:
        relaunch.close()


def test_a_quick_save_opened_directly_is_never_offered_or_written(net, controller):
    main = _crashed_with_a_quick_save(net, controller)
    session = Session(main, controller)  # _refuse: any prompt fails the test
    try:
        assert controller.is_quick_save()
        session.edit(tx=13.0)
        controller.tick()
        assert [str(p) for p in autosave.autosave_root().rglob("*.usda")] == [main]
        controller.on_saved(main)  # saving over it leaves the quick saves alone
        assert os.path.exists(main)
    finally:
        session.close()


# -- start-up order -----------------------------------------------------------------------------


def test_start_without_quick_saves_is_not_deferred(net, controller):
    calls = []
    stage = Usd.Stage.Open(str(net))
    deferred = qt_autosave.start_after_recovery(
        controller, stage, lambda: calls.append("start"), lambda fn: pytest.fail("deferred"), _refuse)
    assert deferred is False
    assert calls == ["start"]


@pytest.mark.parametrize("answer", ["recover", "discard"])
def test_start_waits_for_the_choice(net, controller, answer):
    main = _crashed_with_a_quick_save(net, controller)
    stage = Usd.Stage.Open(str(net))
    attr = f"{TRANSFORM}.{TX}"
    calls, queued = [], []

    def chooser(paths):
        calls.append(("choose", list(paths)))
        return paths[0] if answer == "recover" else qt_autosave.DISCARD

    def start():
        value = stage.GetRootLayer().GetAttributeAtPath(attr)
        calls.append(("start", value.default if value else None, _quick_saves(net)))

    try:
        assert qt_autosave.start_after_recovery(controller, stage, start, queued.append, chooser) is True
        assert calls == []  # nothing loads until the deferred call runs
        queued.pop()()
        if answer == "recover":
            assert calls == [("choose", [main]), ("start", pytest.approx(4.0), [main])]
        else:
            assert calls[0] == ("choose", [main])
            assert calls[1][0] == "start" and calls[1][2] == []
            assert calls[1][1] in (None, 0.0)
    finally:
        stage.GetRootLayer().Reload(force=True)


def test_a_failed_recovery_still_starts(net, controller, monkeypatch):
    main = _crashed_with_a_quick_save(net, controller)
    stage = Usd.Stage.Open(str(net))
    calls, queued = [], []

    def broken(*_args):
        raise OSError("unreadable")

    monkeypatch.setattr(autosave, "recover_into", broken)
    qt_autosave.start_after_recovery(controller, stage, lambda: calls.append("start"), queued.append,
                                     lambda paths: paths[0])
    queued.pop()()
    assert calls == ["start"]
    assert os.path.exists(main)


# -- the prompt ---------------------------------------------------------------------------------


def _files_written_at(tmp_path, *stamps):
    files = []
    for i, stamp in enumerate(stamps):
        path = str(tmp_path / f"shot_{i}.usda")
        with open(path, "w", encoding="utf-8") as f:
            f.write("#usda 1.0\n")
        when = time.mktime(stamp + (0, 0, -1))
        os.utime(path, (when, when))
        files.append(path)
    return files


def test_recovery_buttons_say_when_each_was_written(tmp_path):
    files = _files_written_at(tmp_path, (2026, 11, 18, 14, 30, 0), (2026, 11, 18, 14, 29, 0),
                              (2026, 11, 17, 9, 5, 0))
    assert qt_autosave.recovery_buttons(files[2:]) == [("Recover", files[2]), ("Discard", qt_autosave.DISCARD)]
    assert qt_autosave.recovery_buttons(files) == [
        ("Recover latest (2026-11-18 14:30)", files[0]),
        ("Recover earlier (2026-11-18 14:29)", files[1]),
        ("Recover earlier (2026-11-17 09:05)", files[2]),
        ("Discard all", qt_autosave.DISCARD),
    ]


def test_recovery_buttons_in_the_same_minute_show_seconds(tmp_path):
    files = _files_written_at(tmp_path, (2026, 11, 18, 14, 30, 40), (2026, 11, 18, 14, 30, 5))
    assert [label for label, _ in qt_autosave.recovery_buttons(files)] == [
        "Recover latest (2026-11-18 14:30:40)", "Recover earlier (2026-11-18 14:30:05)", "Discard all"]


def _answer_box(qapp, answer: str, stuck: list, seen: list):
    """Once the prompt is up, answer it: "esc" presses Esc, "close" closes the window (its
    close button), anything else clicks the button with that label.

    A prompt still up a moment later is noted in stuck and then closed by force, so a box
    that ignores the answer fails the test instead of hanging it.
    """

    def find():
        return next((w for w in qapp.topLevelWidgets()
                     if isinstance(w, qt_autosave.RecoveryBox) and w.isVisible()), None)

    def act():
        box = find()
        if box is None:
            QtCore.QTimer.singleShot(10, act)
            return
        # The close button works only with an escape button (Windows greys it out otherwise).
        seen.append(box.escapeButton() is box.cancel_button and not box.cancel_button.isVisible())
        if answer == "esc":
            QTest.keyClick(box, QtCore.Qt.Key.Key_Escape)
        elif answer == "close":
            box.close()
        else:
            next(b for b in box.buttons() if b.text() == answer).click()
        QtCore.QTimer.singleShot(200, lambda: check(box))

    def check(box):
        # This box only: the next test's prompt may be up by now.
        if box.isVisible():
            stuck.append(answer)
            QtWidgets.QDialog.done(box, 0)

    QtCore.QTimer.singleShot(0, act)


@pytest.mark.parametrize("answer", ["recover", "discard", "esc", "close"])
def test_the_prompt_returns_the_choice(qapp, tmp_path, answer):
    files = _files_written_at(tmp_path, (2026, 11, 18, 14, 30, 0), (2026, 11, 18, 14, 29, 0))
    labels = [label for label, _ in qt_autosave.recovery_buttons(files)]
    press, expected = {
        "recover": (labels[1], files[1]),
        "discard": ("Discard all", qt_autosave.DISCARD),
        "esc": ("esc", None),  # closed unanswered: keep every file
        "close": ("close", None),
    }[answer]
    stuck, seen = [], []
    _answer_box(qapp, press, stuck, seen)
    choice = qt_autosave.choose_recovery(files, str(tmp_path / "shot.usda"))
    assert stuck == [], f"the prompt stayed up after {answer!r}"
    assert seen == [True], "the escape button is the hidden Cancel"
    assert choice == expected


# -- the menu toggle ----------------------------------------------------------------------------


def test_toggle_action_follows_and_writes_the_setting(qapp):
    qt_autosave.set_user_enabled(False)
    menu = QtWidgets.QMenu()
    action = menu.addAction(qt_autosave.ACTION_TEXT)
    seen = []
    qt_autosave.configure_toggle_action(action, seen.append)
    assert action.isCheckable() and not action.isChecked() and action.isEnabled()
    assert seen == []  # setting the initial state is not a toggle
    action.toggle()
    assert qt_autosave.user_enabled() and seen == [True]
    action.toggle()
    assert not qt_autosave.user_enabled() and seen == [True, False]


def test_toggle_action_is_greyed_out_when_the_env_turns_it_off(qapp, monkeypatch):
    monkeypatch.setenv(ENV, "0")
    action = QtGui.QAction(qt_autosave.ACTION_TEXT)
    qt_autosave.configure_toggle_action(action)
    assert action.isCheckable() and action.isChecked()  # the user's own choice is still on
    assert not action.isEnabled()
    assert action.toolTip() == "Turned off by USD_CHUNKS_AUTOSAVE_MINUTES=0"


def test_the_menu_shows_the_toggle_tooltip(qapp, monkeypatch):
    monkeypatch.setenv(ENV, "0")
    menu = QtWidgets.QMenu()
    assert not menu.toolTipsVisible()  # Qt's default: the tooltip would never show
    action = menu.addAction(qt_autosave.ACTION_TEXT)
    qt_autosave.configure_toggle_action(action)
    assert menu.toolTipsVisible()


def test_find_action_in_the_menu_bar(qapp):
    bar = QtWidgets.QMenuBar()
    bar.addMenu("File").addAction("Open")
    chunks = bar.addMenu("Chunks")
    chunks.addAction("Save Scene")
    wanted = chunks.addAction(qt_autosave.ACTION_TEXT)
    assert qt_autosave.find_action(bar) is wanted
    assert qt_autosave.find_action(bar, "Nothing") is None
