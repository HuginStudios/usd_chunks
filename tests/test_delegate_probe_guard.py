"""Render settings discovery must not load a render delegate inside a host.

usdview owns a render engine. Building a second one in the same process and walking it
through every installed plugin takes the host down: loading hdCycles that way segfaults
(0xC0000005), so every scene holding a Render Settings chunk failed to open at all --
the chunk reads the delegates as it is built, before anything is drawn.

Schema- and Sdr-declared renderers are unaffected, and that is where V-Ray's settings come
from; only a renderer that describes itself solely through its delegate loses its group
in a host.
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


def test_no_delegate_is_loaded_in_a_host(qapp, monkeypatch) -> None:
    """The guard has to come before the engine is built, not after."""
    def _fail(*_args, **_kwargs):
        raise AssertionError("a delegate must not be loaded inside a host")

    monkeypatch.setattr(render_defs, "UsdImagingGL", type("_GL", (), {"Engine": _fail}))

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
