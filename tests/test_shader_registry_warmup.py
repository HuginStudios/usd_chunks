"""The shader registry is built ahead of the first chunk menu, on a worker thread.

Building it is what made the first right-click in usdview take seconds: every installed
shader plugin is discovered the first time anything asks. Starting it at launch moves that
cost off the click, and doing it on a thread keeps usdview responsive meanwhile.
"""
from __future__ import annotations

from usd_chunks import shader_defs


def test_it_starts_once_and_finishes(monkeypatch):
    monkeypatch.setattr(shader_defs, "_warm_thread", None)
    thread = shader_defs.warm_registry_in_background()
    assert thread is not None and thread.daemon, "a daemon thread, so it never holds up exit"
    assert shader_defs.warm_registry_in_background() is None, "a second call starts nothing"
    thread.join(timeout=60)
    assert not thread.is_alive()


def test_definitions_are_still_listed_after_warming(monkeypatch):
    monkeypatch.setattr(shader_defs, "_warm_thread", None)
    shader_defs.warm_registry_in_background().join(timeout=60)
    warmed = [d.identifier for d in shader_defs.iter_shader_defs("mtlx")]
    assert warmed == [d.identifier for d in shader_defs.iter_shader_defs("mtlx")]
