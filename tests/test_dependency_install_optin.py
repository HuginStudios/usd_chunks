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


@pytest.fixture()
def deps(monkeypatch):
    """A dependency manager that believes nothing is installed and can never shell out."""
    mgr = chunk_registry.ChunkDependencyManager()
    mgr.initialized = True
    mgr.package_dir = "/tmp/chunk-deps"

    monkeypatch.setattr(mgr, "package_in_cache", lambda package: False)
    monkeypatch.setattr(mgr, "update_cache", lambda: None)

    ran = []
    monkeypatch.setattr(
        chunk_registry.subprocess, "run",
        lambda *a, **k: ran.append(a[0]) or pytest.fail("pip was invoked"),
    )

    asked = []
    monkeypatch.setattr(mgr, "should_install_package", lambda p: asked.append(p) or False)
    return mgr, asked


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
    monkeypatch.setattr(mgr, "package_in_cache", lambda package: True)

    mgr.install_packages(["requests"])

    assert asked == []
