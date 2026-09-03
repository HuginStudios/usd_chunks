"""Installing a chunk's pip dependencies is opt-in.

A chunk module is ordinary Python that anyone can drop on USD_CHUNK_DIRS, so opening a
network must not be able to put packages into the user's environment as a side effect. The
per-scene should_install_package prompt is not enough on its own: chunk_cli's asks with
input(), which nobody is there to answer under `usdchunks run` in CI.

USD_CHUNK_AUTO_INSTALL decides whether asking is allowed at all; the prompt still decides
the answer when it is.
"""
from __future__ import annotations

import logging

import pytest

from usd_chunks import chunk_registry


@pytest.fixture(scope="module", autouse=True)
def _singleton_left_clean():
    """This file must not leave anything on the dependency-manager singleton.

    Every leak found here took the same shape: a patch applied to the instance, undone by
    writing the found value back onto the instance, where it outlives the test. The value
    written back is whatever was current at teardown -- which, once a fixture patches the
    class, is the fixture's own stub. It then answers for the rest of the session, and the
    file that trips over it is some other file that looks entirely unrelated.
    """
    mgr = chunk_registry.ChunkDependencyManager()
    before = set(mgr.__dict__)
    yield
    leaked = set(mgr.__dict__) - before
    assert not leaked, (
        f"left on the ChunkDependencyManager singleton: {sorted(leaked)}. Patch the class, "
        "not the instance -- monkeypatch's undo cannot remove what it did not add."
    )


@pytest.fixture()
def deps(monkeypatch):
    """A dependency manager that believes nothing is installed and can never shell out.

    Patched on the class, not on the instance. ChunkDependencyManager is a process-wide
    singleton, and monkeypatch undoes an instance patch by writing the value it found back
    with setattr -- which lands on the *instance* even when it was only ever a class
    attribute. The singleton then carries a real, unpatchable bound method for the rest of
    the session, shadowing any later class-level patch. test_dependency_install_failure
    patches this same update_cache on the class to keep pip off the network; run it after
    this file and its no-op was silently overridden, so `pip list` ran for real and turned
    up as the first recorded subprocess call instead of the install being asserted on.
    """
    mgr = chunk_registry.ChunkDependencyManager()
    # Restored below rather than monkeypatched: package_dir does not exist at all until
    # initialize() runs, so there is no original value for monkeypatch to put back.
    had = {k: mgr.__dict__[k] for k in ("initialized", "package_dir") if k in mgr.__dict__}
    mgr.initialized = True
    mgr.package_dir = "/tmp/chunk-deps"

    cls = chunk_registry.ChunkDependencyManager
    monkeypatch.setattr(cls, "package_in_cache", lambda self, package: False)
    monkeypatch.setattr(cls, "update_cache", lambda self: None)

    ran = []
    monkeypatch.setattr(
        chunk_registry.subprocess, "run",
        lambda *a, **k: ran.append(a[0]) or pytest.fail("pip was invoked"),
    )

    asked = []
    monkeypatch.setattr(
        cls, "should_install_package", lambda self, p: asked.append(p) or False
    )
    yield mgr, asked

    for key in ("initialized", "package_dir"):
        mgr.__dict__.pop(key, None)
    mgr.__dict__.update(had)


def test_off_by_default(deps, monkeypatch, caplog):
    mgr, asked = deps
    monkeypatch.delenv("USD_CHUNK_AUTO_INSTALL", raising=False)

    with caplog.at_level(logging.WARNING, logger=chunk_registry.logger.name):
        mgr.install_packages(["requests==2.31.0"])

    assert asked == [], "the prompt must not even be reached while installing is off"
    assert "requests==2.31.0" in caplog.text, "the user has to be told what is missing"
    assert "USD_CHUNK_AUTO_INSTALL" in caplog.text, "and how to turn installing on"


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_falsey_values_are_off(deps, monkeypatch, value):
    mgr, asked = deps
    monkeypatch.setenv("USD_CHUNK_AUTO_INSTALL", value)

    mgr.install_packages(["requests"])

    assert asked == []


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_enabling_reaches_the_prompt(deps, monkeypatch, value):
    """On, the switch only permits the question -- should_install_package still answers it,
    and answering no still installs nothing (the stub returns False, and the subprocess.run
    patch would fail the test if pip ran)."""
    mgr, asked = deps
    monkeypatch.setenv("USD_CHUNK_AUTO_INSTALL", value)

    mgr.install_packages(["requests"])

    assert asked == ["requests"]


def test_nothing_missing_asks_nothing(deps, monkeypatch):
    mgr, asked = deps
    monkeypatch.setenv("USD_CHUNK_AUTO_INSTALL", "1")
    # On the class, for the same reason the fixture does: an instance patch here is undone
    # by writing back whatever it found -- the fixture's own lambda -- onto the singleton,
    # where it outlives this file and answers False to every later cache check.
    monkeypatch.setattr(
        chunk_registry.ChunkDependencyManager, "package_in_cache", lambda self, package: True
    )

    mgr.install_packages(["requests"])

    assert asked == []
