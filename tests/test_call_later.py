"""SceneSender.call_later: the host-neutral way back to the main thread."""
from __future__ import annotations

import threading
from types import SimpleNamespace

from chunks.dynamics import rbd_solver
from usd_chunks import chunk_scene
from usd_chunks.chunk_scene import SceneSender


def test_with_no_event_loop_it_runs_now():
    """`usdchunks run` and scripts have no loop to wait in, so the call cannot be later."""
    ran = []
    cancel = SceneSender.call_later(None, lambda: ran.append(1), delay=5.0)
    assert ran == [1]
    cancel()  # nothing left to cancel; must not raise


def test_from_another_thread_it_waits_for_the_main_thread():
    """A download finishing on a pool thread must not run its hand-off there: setting a
    param from it cooked USD off the main thread and crashed the process."""
    ran = []
    worker = threading.Thread(
        target=lambda: SceneSender.call_later(None, lambda: ran.append(threading.current_thread())))
    worker.start()
    worker.join()
    assert ran == [], "it ran on the worker thread"
    chunk_scene.run_pending()
    assert ran == [threading.main_thread()]


def test_a_queued_call_can_be_cancelled():
    ran = []
    cancels = []
    worker = threading.Thread(
        target=lambda: cancels.append(SceneSender.call_later(None, lambda: ran.append(1))))
    worker.start()
    worker.join()
    cancels[0]()
    chunk_scene.run_pending()
    assert ran == []


def test_the_next_main_thread_call_runs_what_was_queued_first():
    ran = []
    worker = threading.Thread(target=lambda: SceneSender.call_later(None, lambda: ran.append("queued")))
    worker.start()
    worker.join()
    SceneSender.call_later(None, lambda: ran.append("now"))
    assert ran == ["queued", "now"]


def test_rbd_auto_run_cancels_the_wait_the_last_change_started(monkeypatch):
    """A debounce: each change cancels the previous wait, so one bake follows a drag."""
    pending = []

    class _Scene:
        def call_later(self, fn, delay=0.0):
            entry = {"fn": fn, "delay": delay, "cancelled": False}
            pending.append(entry)
            return lambda: entry.update(cancelled=True)

    chunk = SimpleNamespace(get_scene=_Scene)
    monkeypatch.setattr(rbd_solver.sim_bake, "is_unattended", lambda: False)
    for _ in range(3):
        rbd_solver._schedule_auto_run(chunk)
    assert [e["cancelled"] for e in pending] == [True, True, False]
    assert pending[-1]["delay"] == rbd_solver._AUTO_RUN_DELAY_MS / 1000.0
