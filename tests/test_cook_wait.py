"""A cook that waits on a long job holds the whole graph until it is done.

sim_bake.wait() runs a long part of a chunk's cook on a worker and returns its result to
the cook. Nothing else cooks meanwhile: a recook a timer starts waits, the window takes no
input but the progress window's, and the chunks below cook once, after it. Cancel, or a
failure, marks the chunk FAILED and the chunks below do not cook.
"""

from __future__ import annotations

import time

import pytest
from chunk_stubs import register_stub_module
from PySide6 import QtCore, QtTest, QtWidgets

from usd_chunks import Scene, chunk, chunk_cook, chunk_stage, sim_bake, skip_cook
from usd_chunks.chunk_flags import ChunkFlags

FAILED = ChunkFlags.State.FAILED


@pytest.fixture(autouse=True)
def _every_cook_runs(monkeypatch):
    # Down reads Slow's result through a dict, which skip_cook cannot see.
    monkeypatch.setattr(skip_cook, "should_skip", lambda *args, **kwargs: False)


@pytest.fixture
def threaded(qapp, monkeypatch):
    monkeypatch.setattr(sim_bake, "in_background", lambda: True)
    return qapp


def _add(scene, module):
    (c,) = module.load()
    scene.add_chunk(c)
    return c


def _slow(log: list, result: dict, job: dict):
    """Slow: its cook waits on a job of job["seconds"] that returns size * 10."""

    def main():
        c = chunk.Chunk("Slow")
        c.add_param(chunk.Slider("size", default_value=1.0))
        return c

    def execute(node):
        size = node.get_param("size").value

        def bake(ctx):
            ctx.status("working")
            yield sim_bake.WORKER
            end = time.perf_counter() + job.get("seconds", 0.0)
            while time.perf_counter() < end:
                ctx.check()
                time.sleep(0.01)
            if job.get("fail"):
                raise ValueError("no surface")
            return size * 10

        result["slow"] = sim_bake.wait(node, bake, report=lambda text: None)
        log.append(("Slow", result["slow"]))

    return register_stub_module(main=main, execute=execute)


def _reader(name: str, log: list, result: dict):
    def main():
        c = chunk.Chunk(name)
        c.add_param(chunk.Slider("gain", default_value=1.0))
        return c

    def execute(node):
        log.append((name, result.get("slow")))

    return register_stub_module(main=main, execute=execute)


def _chain(scene, **job):
    log: list = []
    result: dict = {}
    slow = _add(scene, _slow(log, result, job))
    down = _add(scene, _reader("Down", log, result))
    down.add_dependency(slow)
    slow.get_param("size").value = 2.0
    log.clear()
    return slow, down, log, job


def _later(ms: int, fn) -> None:
    QtCore.QTimer.singleShot(ms, QtWidgets.QApplication.instance(), fn)


def _progress_window():
    return next((w for w in QtWidgets.QApplication.topLevelWidgets()
                 if isinstance(w, QtWidgets.QProgressDialog) and w.isVisible()), None)


def test_headless_it_runs_straight_through_and_returns_the_result():
    with Scene() as scene:
        slow, down, log, _ = _chain(scene)
        slow.get_param("size").value = 3.0
        assert log == [("Slow", 30.0), ("Down", 30.0)]


def test_the_chunks_below_cook_once_with_the_result(threaded):
    with Scene() as scene:
        slow, down, log, job = _chain(scene)
        job["seconds"] = 0.3
        slow.get_param("size").value = 3.0
        threaded.processEvents()
        assert log == [("Slow", 30.0), ("Down", 30.0)]


def test_a_recook_a_timer_starts_meanwhile_runs_after_it(threaded, monkeypatch):
    real = chunk_stage._recook_pass

    def recording(*args, **kwargs):
        log.append("recook")
        return real(*args, **kwargs)

    monkeypatch.setattr(chunk_stage, "_recook_pass", recording)
    with Scene() as scene:
        slow, down, log, job = _chain(scene)
        job["seconds"] = 0.4
        stage = scene.get_stage()
        _later(100, lambda: chunk_stage.recook_for_view(stage, scene, None, None))

        slow.get_param("size").value = 3.0
        assert log == [("Slow", 30.0), ("Down", 30.0)], "a recook ran while Slow waited"

        threaded.processEvents()
        assert log[-1] == "recook", "the held recook never ran"


def test_the_window_takes_no_input_while_it_waits(threaded):
    clicks = []
    button = QtWidgets.QPushButton("edit")
    button.clicked.connect(lambda: clicks.append(1))
    button.show()
    try:
        with Scene() as scene:
            slow, down, log, job = _chain(scene)
            job["seconds"] = 0.4
            _later(100, lambda: QtTest.QTest.mouseClick(button, QtCore.Qt.LeftButton))
            slow.get_param("size").value = 3.0
        assert not clicks, "a click got through while a cook waited"
        QtTest.QTest.mouseClick(button, QtCore.Qt.LeftButton)
        assert clicks == [1], "input is still blocked after the wait"
    finally:
        button.deleteLater()


def test_the_progress_window_shows_the_chunk_and_cancel_stops_it(threaded):
    seen = {}

    def press_cancel():
        window = _progress_window()
        seen["label"] = window.labelText() if window else None
        if window is not None:
            cancel = window.findChild(QtWidgets.QPushButton)
            QtTest.QTest.mouseClick(cancel, QtCore.Qt.LeftButton)

    with Scene() as scene:
        slow, down, log, job = _chain(scene)
        job["seconds"] = 5.0
        _later(500, press_cancel)
        started = time.perf_counter()

        slow.get_param("size").value = 3.0  # cancelled: nothing raised

        assert time.perf_counter() - started < 3.0, f"Cancel did not stop the wait: {seen}"
        assert seen["label"] == "Slow\nworking"
        assert slow.has_state(FAILED), "a cancelled cook is not marked failed"
        assert log == [], "a chunk below cooked after Cancel"
        assert chunk_cook.is_cook_stale(down)
        threaded.processEvents()
        assert _progress_window() is None

        job["seconds"] = 0.0
        slow.get_param("size").value = 4.0
        assert not slow.has_state(FAILED), "a cook that ran left it failed"
        assert log == [("Slow", 40.0), ("Down", 40.0)]


def test_a_failed_job_fails_the_cook_and_nothing_below_cooks(threaded):
    with Scene() as scene:
        slow, down, log, job = _chain(scene)
        job.update(seconds=0.1, fail=True)
        with pytest.raises(ValueError, match="no surface"):
            slow.get_param("size").value = 3.0
        assert slow.has_state(FAILED)
        assert log == []
        assert chunk_cook.is_cook_stale(down)


def _breaks(log: list):
    def main():
        c = chunk.Chunk("Breaks")
        c.add_param(chunk.Slider("size", default_value=1.0))
        return c

    def execute(node):
        if node.get_param("size").value > 2.0:
            raise RuntimeError("bad size")
        log.append(("Breaks", None))

    return register_stub_module(main=main, execute=execute)


def test_a_cook_that_raises_holds_back_only_what_is_below_it():
    with Scene() as scene:
        log: list = []
        breaks = _add(scene, _breaks(log))
        below = _add(scene, _reader("Below", log, {}))
        below.add_dependency(breaks)
        other = _add(scene, _reader("Other", log, {}))
        log.clear()

        with pytest.raises(RuntimeError, match="bad size"):
            with chunk_cook.batched_solve():
                breaks.get_param("size").value = 3.0
                other.get_param("gain").value = 2.0

        assert breaks.has_state(FAILED)
        assert ("Below", None) not in log, "the chunk below a failed cook cooked"
        assert ("Other", None) in log, "a chunk beside the failed one did not cook"
        assert chunk_cook.is_cook_stale(below)

        breaks.get_param("size").value = 1.0
        assert not breaks.has_state(FAILED)
        assert ("Below", None) in log
