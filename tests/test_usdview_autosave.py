"""usdview's side of auto quick save: when the chunks load, the deferral, the menu toggle.

The prompt comes before a network's chunks load (File > Open, and launch, which usdview
also reports as a stage replaced), from usdview's own event loop. Without quick saves the
chunks load at once, as they always did. What the controller does with a choice is tested
in test_autosave_qt; this is the order the host does things in.

usdview is not importable here, so the scene is built without its constructor, as in
test_usdview_deps_prompt_after_load, and the timer, the prompt and the chunk load are
stand-ins.
"""
from __future__ import annotations

import logging
import os
import time
import types

import pytest

pytest.importorskip("pxr.Usdviewq", reason="usdview is not part of the usd-core wheel")

from pxr import Sdf, Usd  # noqa: E402
from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

from usd_chunks import Scene, autosave  # noqa: E402
from usd_chunks.frontends.qt import autosave as qt_autosave  # noqa: E402
from usd_chunks.frontends.qt import window_state  # noqa: E402
from usd_chunks.hosts import usdview  # noqa: E402

ENV = "USD_CHUNKS_AUTOSAVE_MINUTES"
TX = "inputs:translate_grp:tx"
TRANSFORM = "/chunks/ChunkGroup/Transform"


@pytest.fixture(autouse=True)
def scratch_settings(monkeypatch, tmp_path):
    """QSettings in a throwaway ini file: a test never touches the user's own settings."""
    path = str(tmp_path / "settings.ini")
    monkeypatch.setattr(window_state, "_settings", lambda: QtCore.QSettings(path, QtCore.QSettings.Format.IniFormat))
    monkeypatch.delenv(ENV, raising=False)


@pytest.fixture
def net(tmp_path):
    path = tmp_path / "shot.usda"
    with Scene.new(path=str(path)) as scene:
        scene.set_output(scene.create.transform(scene.create.cube(prim_path="/geo/box")))
        scene.save()
    old = time.time() - 100
    os.utime(path, (old, old))
    return path


def _quick_save(net, tx=4.0):
    """shot.autosave.usda, newer than shot.usda: what a crash leaves behind."""
    with Scene.open(str(net)) as scene:
        scene.set(next(c for c in scene.get_chunks() if c.name == "Transform"), tx=tx)
        main = autosave.next_target(net)
        autosave.write_quick_save(scene, main)
    Sdf.Layer.FindOrOpen(str(net)).Reload(force=True)  # the edit was never saved
    return main


class Host:
    """A UsdviewScene with stand-ins for usdview's stage, the chunk load and the timer."""

    def __init__(self, monkeypatch, stage):
        self.stage = stage
        self.calls = []
        self.queued = []
        scene = object.__new__(usdview.UsdviewScene)
        scene.autosave = qt_autosave.AutosaveController()
        scene.get_stage = lambda: self.stage
        scene.notify_stage_replaced = self._notified
        scene.autosave.attach = lambda s: self.calls.append(("attach", s))
        monkeypatch.setattr(usdview, "_after_startup", self.queued.append)
        self.scene = scene

    def _notified(self, stage):
        tx = None
        if stage is not None:
            attr = stage.GetRootLayer().GetAttributeAtPath(f"{TRANSFORM}.{TX}")
            tx = attr.default if attr else None
        self.calls.append(("notify", stage, tx))

    def choose(self, monkeypatch, answer):
        def chooser(paths, network_path=None):
            self.calls.append(("choose", [os.path.basename(p) for p in paths]))
            return paths[0] if answer == "recover" else answer

        monkeypatch.setattr(qt_autosave, "choose_recovery", chooser)


@pytest.fixture
def stage(net):
    stage = Usd.Stage.Open(str(net))
    yield stage
    stage.GetRootLayer().Reload(force=True)


# -- _on_stage_replaced -------------------------------------------------------------------------


def test_without_quick_saves_the_chunks_load_at_once(qapp, monkeypatch, stage):
    host = Host(monkeypatch, stage)
    host.choose(monkeypatch, "never asked")
    host.scene._on_stage_replaced()
    assert host.queued == []
    assert host.calls == [("notify", stage, None), ("attach", host.scene)]


@pytest.mark.parametrize("answer", ["recover", qt_autosave.DISCARD])
def test_with_quick_saves_the_choice_comes_before_the_load(qapp, monkeypatch, net, answer):
    _quick_save(net)
    stage = Usd.Stage.Open(str(net))
    try:
        host = Host(monkeypatch, stage)
        host.choose(monkeypatch, answer)
        host.scene._on_stage_replaced()
        assert host.calls == []  # nothing loads, nothing is asked, until usdview is idle
        assert len(host.queued) == 1
        host.queued.pop()()
        recovered = pytest.approx(4.0) if answer == "recover" else None
        assert host.calls == [
            ("choose", ["shot.autosave.usda"]),
            ("notify", stage, recovered),
            ("attach", host.scene),
        ]
        expected = ["shot.autosave.usda"] if answer == "recover" else []
        assert [os.path.basename(p) for p in autosave.quick_save_paths(net)] == expected
    finally:
        stage.GetRootLayer().Reload(force=True)


def test_a_stage_replaced_while_the_prompt_was_up_is_not_loaded(qapp, monkeypatch, net, tmp_path):
    _quick_save(net)
    stage = Usd.Stage.Open(str(net))
    try:
        host = Host(monkeypatch, stage)
        host.choose(monkeypatch, qt_autosave.DISCARD)
        host.scene._on_stage_replaced()
        host.stage = Usd.Stage.CreateInMemory()  # another file opened meanwhile
        host.queued.pop()()
        assert host.calls == [("choose", ["shot.autosave.usda"])]  # its own signal loads it
    finally:
        stage.GetRootLayer().Reload(force=True)


def test_no_stage_tears_down_at_once(qapp, monkeypatch, stage):
    host = Host(monkeypatch, stage)
    host.stage = None  # usdview closes the old stage before it opens the next file
    host.scene.autosave.timer.start(60_000)
    host.scene._on_stage_replaced()
    assert host.calls == [("notify", None, None)]
    assert host.queued == []
    assert not host.scene.autosave.timer.isActive()


# -- _after_startup ------------------------------------------------------------------------------


@pytest.fixture
def event_loop(monkeypatch):
    """Stand-ins for QTimer.singleShot and the thread's loopLevel()."""
    state = types.SimpleNamespace(level=0, queued=[])
    thread = types.SimpleNamespace(loopLevel=lambda: state.level)
    monkeypatch.setattr(usdview.QtCore.QTimer, "singleShot", lambda ms, ctx, fn: state.queued.append((ms, fn)))
    monkeypatch.setattr(usdview.QtCore.QThread, "currentThread", staticmethod(lambda: thread))
    return state


def test_after_startup_waits_for_the_event_loop(qapp, event_loop):
    ran = []
    usdview._after_startup(lambda: ran.append(True))
    assert ran == [] and len(event_loop.queued) == 1

    # Fired from a processEvents inside usdview's start-up: no loop yet, so again later.
    _ms, fn = event_loop.queued.pop()
    fn()
    assert ran == [] and len(event_loop.queued) == 1

    event_loop.level = 1  # app.exec() is running
    _ms, fn = event_loop.queued.pop()
    fn()
    assert ran == [True] and event_loop.queued == []


def test_after_startup_logs_what_fn_raises(qapp, event_loop, caplog):
    event_loop.level = 1

    def broken():
        raise RuntimeError("the prompt broke")

    usdview._after_startup(broken)
    with caplog.at_level(logging.ERROR, logger="usd_chunks.hosts.usdview"):
        event_loop.queued.pop()[1]()  # does not raise: from a timer slot it would end usdview
    assert any("the prompt broke" in r.getMessage() or (r.exc_info and "the prompt broke" in str(r.exc_info[1]))
               for r in caplog.records)


# -- the menu toggle ----------------------------------------------------------------------------


def test_a_real_action_becomes_the_checkbox(qapp):
    container = usdview.ChunksPluginContainer()
    menu = QtWidgets.QMenu()
    action = menu.addAction(qt_autosave.ACTION_TEXT)
    container._setup_autosave_action(action)
    assert container._autosaveAction is action
    assert action.isCheckable() and action.isChecked()


def test_without_the_action_it_is_found_in_the_menu_bar(qapp):
    window = QtWidgets.QMainWindow()
    wanted = window.menuBar().addMenu("Chunks").addAction(qt_autosave.ACTION_TEXT)
    container = usdview.ChunksPluginContainer()
    container._usdviewApi = types.SimpleNamespace(qMainWindow=window)
    container._setup_autosave_action(object())  # what an older usdview's addItem might return
    assert container._autosaveAction is wanted
    assert wanted.isCheckable()
    # Looking did not take the menu away (QAction.menu() wrappers can, in PySide6 6.9).
    assert [m.title() for m in window.menuBar().findChildren(QtWidgets.QMenu)] == ["Chunks"]


def test_toggle_command_returns_early_when_the_action_does_the_work(qapp):
    container = usdview.ChunksPluginContainer()
    container._autosaveAction = QtGui.QAction(qt_autosave.ACTION_TEXT)
    container.toggleAutosave(None)
    assert qt_autosave.user_enabled()


def test_toggle_command_flips_the_setting_without_an_action(qapp):
    container = usdview.ChunksPluginContainer()
    container._autosaveAction = None
    container.toggleAutosave(None)
    assert not qt_autosave.user_enabled()
    container.toggleAutosave(None)
    assert qt_autosave.user_enabled()


def test_toggle_command_only_logs_when_the_env_turns_it_off(qapp, monkeypatch, caplog):
    monkeypatch.setenv(ENV, "0")
    container = usdview.ChunksPluginContainer()
    container._autosaveAction = None
    with caplog.at_level(logging.WARNING, logger="usd_chunks.hosts.usdview"):
        container.toggleAutosave(None)
    assert qt_autosave.user_enabled()
    assert any("USD_CHUNKS_AUTOSAVE_MINUTES=0" in r.getMessage() for r in caplog.records)
