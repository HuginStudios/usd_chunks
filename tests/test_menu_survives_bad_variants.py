"""One module's variant list must not take out the whole chunk menu.

Live-reported on a USD 26.05 build: right-clicking in the node graph of a new scene raised
out of Sdr, through the MaterialX chunk's lazily-declared menu variants, and no menu
appeared at all:

    pxr.Tf.ErrorException: Error in 'SdrVersion' ... 'Invalid version 0.0: both components
    must be non-negative and at least one non-zero'

The registry there is unusable for reasons that have nothing to do with usd_chunks -- some
installed plugin advertises a shader with a bad version -- but every chunk in the menu paid
for it. Variants are an optional extra on one entry; failing to build them should cost that
entry its sub-menu and nothing more.
"""
from __future__ import annotations

from usd_chunks.chunk_core import ChunkConfig


def test_a_raising_variants_callable_yields_no_variants():
    def _boom():
        raise RuntimeError("Sdr is unusable in this build")

    config = ChunkConfig(variants=_boom)

    assert config.get_variants() == []


def test_it_does_not_swallow_the_variants_that_do_work():
    config = ChunkConfig(variants=lambda: [("Metal", {"preset": 1})])

    assert config.get_variants() == [("Metal", {"preset": 1})]


def test_a_failure_is_retried_rather_than_cached_as_empty():
    """A callable can fail for a reason that goes away.

    Discovery of some shaders needs its renderer initialised, so a list that is empty now
    may be real later. Memoising the failure would make the first right-click of a session
    decide what the menu holds for the rest of it.
    """
    calls = []

    def _twice():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("not ready yet")
        return [("Later", {})]

    config = ChunkConfig(variants=_twice)

    assert config.get_variants() == []
    assert config.get_variants() == [("Later", {})]


def test_the_registry_being_unusable_is_not_an_exception(monkeypatch):
    """shader_defs is where the reported failure actually came from."""
    from pxr import Sdr

    from usd_chunks import shader_defs

    def _boom(*_a, **_k):
        raise RuntimeError("Invalid version 0.0")

    monkeypatch.setattr(Sdr, "Registry", _boom)

    assert list(shader_defs.iter_shader_defs()) == []


def test_the_registry_warning_is_said_once_per_process(monkeypatch, caplog):
    """Retried every menu draw, but not re-announced every menu draw.

    The retry is deliberate -- a registry that needs its renderer initialised can start
    working later -- but on a build where it never will, saying so on every right-click
    buries everything else in the log.
    """
    import logging

    from pxr import Sdr

    from usd_chunks import shader_defs

    monkeypatch.setattr(shader_defs, "_registry_warned", False)
    monkeypatch.setattr(Sdr, "Registry", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("nope")))

    with caplog.at_level(logging.WARNING, logger="usd_chunks.shader_defs"):
        list(shader_defs.iter_shader_defs())
        list(shader_defs.iter_shader_defs())
        list(shader_defs.iter_shader_defs())

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, f"expected one warning for three attempts, got {len(warnings)}"
