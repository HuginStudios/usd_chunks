"""Render settings discovery must not load a render delegate inside a host.

usdview owns a render engine. Building a second one in the same process and walking it
through every installed plugin takes the host down: loading hdCycles that way segfaults
(0xC0000005), so every scene holding a Render Settings chunk failed to open at all --
the chunk reads the delegates as it is built, before anything is drawn.

Schema- and Sdr-declared renderers are unaffected, and that is where V-Ray's settings come
from. A renderer that describes itself solely through its delegate (hdCycles, hdAurora) is
read by a child process instead, where loading it costs the host nothing.
"""

from __future__ import annotations

import pytest

from usd_chunks import render_defs

ENV = "USD_CHUNKS_PROBE_DELEGATES"


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)


def test_a_gui_host_is_recognised(qapp) -> None:
    """The qapp fixture is the stand-in for usdview: a live QApplication."""
    assert render_defs._host_owns_a_viewport() is True


def _no_engine(monkeypatch) -> None:
    def _fail(*_args, **_kwargs):
        raise AssertionError("a delegate must not be loaded inside a host")

    monkeypatch.setattr(render_defs, "UsdImagingGL", type("_GL", (), {"Engine": _fail}))


def test_no_delegate_is_loaded_in_a_host(qapp, monkeypatch) -> None:
    """The guard has to come before the engine is built, not after: a host asks a child
    process instead."""
    _no_engine(monkeypatch)
    probed = []
    monkeypatch.setattr(render_defs, "_can_probe_out_of_process", lambda: True)
    monkeypatch.setattr(
        render_defs, "_defs_from_delegates_out_of_process", lambda existing: probed.append(1) or []
    )

    assert render_defs._defs_from_delegates([]) == []
    assert probed == [1]


def test_a_refused_probe_asks_no_child_either(qapp, monkeypatch) -> None:
    """=0 means no delegate is asked at all, in or out of process."""
    _no_engine(monkeypatch)
    monkeypatch.setenv(ENV, "0")
    monkeypatch.setattr(render_defs, "_can_probe_out_of_process", lambda: True)
    monkeypatch.setattr(
        render_defs, "_defs_from_delegates_out_of_process",
        lambda existing: pytest.fail("no child process may be started"),
    )

    assert render_defs._defs_from_delegates([]) == []


def test_a_host_without_a_python_executable_asks_no_child(qapp, monkeypatch) -> None:
    """Under Blender or Houdini sys.executable is the application, not an interpreter."""
    _no_engine(monkeypatch)
    monkeypatch.setattr(render_defs.sys, "executable", r"C:\Program Files\Blender\blender.exe")
    monkeypatch.setattr(
        render_defs, "_defs_from_delegates_out_of_process",
        lambda existing: pytest.fail("no child process may be started"),
    )

    assert render_defs._defs_from_delegates([]) == []


def test_the_override_forces_the_probe(qapp, monkeypatch) -> None:
    """For anyone who wants the delegate-declared settings badly enough to risk it."""
    monkeypatch.setenv(ENV, "1")

    assert render_defs._host_owns_a_viewport() is False


def test_the_override_can_refuse_it_too(monkeypatch) -> None:
    """And for a headless process that must not load delegates either -- a render farm
    blade with no GL, say."""
    monkeypatch.setenv(ENV, "0")

    assert render_defs._host_owns_a_viewport() is True
