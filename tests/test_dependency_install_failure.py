"""A pip install that fails must not be cached as if it succeeded.

install_packages appended the package to its cache the moment the user approved the
dialog, then ran pip and ignored the return code. When pip failed -- routinely, since an
embedded interpreter such as usdview's often ships without pip at all -- the cache still
recorded the package as present. package_in_cache then short-circuited every later run,
so the dependency was never installed and the user was never asked again: the chunk just
errored on creation forever.

The on-disk fingerprint of the bug was a cache holding exactly one package at version
None (the optimistic add), next to a package dir that plainly did not contain it --
whereas a cache built by update_cache() lists every package in the environment.

Seen with scripts/examples/AMDMaterialXLibrary.py, which declares requirements=["requests"].
"""
from __future__ import annotations

import subprocess

import pytest

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender


class _ApprovingScene(SceneSender):
    """A host that says yes to every install prompt."""

    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []

    def should_install_package(self, package_name: str) -> bool:  # noqa: ARG002
        return True

    def request_save(self):
        return False


PACKAGE = "usd-chunks-package-that-cannot-install"


@pytest.fixture
def manager(monkeypatch):
    """The dependency manager, with its cache restored afterwards.

    The cache is a process-wide singleton that atexit writes to the real dependency dir,
    so a test that left entries behind would poison the developer's own environment in
    exactly the way this bug describes.
    """
    # Every test here is about what install_packages does once it is allowed to install.
    # That is opt-in now (USD_CHUNK_AUTO_INSTALL), and without this the manager returns
    # before pip is ever reached -- which several of these tests would misread as a pass,
    # since "nothing was cached" is trivially true when nothing was attempted.
    monkeypatch.setenv("USD_CHUNK_AUTO_INSTALL", "1")
    chunk_registry.ChunkRegistry().initialize(_ApprovingScene(), "pytest")
    mgr = chunk_registry.ChunkDependencyManager()
    mgr.initialize()
    if not mgr.initialized:
        pytest.skip("no dependency dir configured in this environment")

    snapshot = {name: set(versions) for name, versions in mgr._cache.items()}
    # update_cache shells out to `pip list` twice; the cache contents are what this test
    # is about, so keep it off the network and off the clock.
    monkeypatch.setattr(chunk_registry.ChunkDependencyManager, "update_cache", lambda self: None)
    yield mgr
    mgr._cache.clear()
    mgr._cache.update(snapshot)


def _run_returning(code: int):
    def fake_run(args, **kwargs):  # noqa: ARG001
        return subprocess.CompletedProcess(args, code, stdout=b"", stderr=b"boom")

    return fake_run


def test_failed_install_is_not_recorded_in_the_cache(manager, monkeypatch):
    monkeypatch.setattr(chunk_registry.subprocess, "run", _run_returning(1))

    manager.install_packages([PACKAGE])

    assert not manager.package_in_cache(PACKAGE), (
        "pip failed, so the package must stay uncached -- otherwise the install is never "
        "retried and the user is never prompted again"
    )


def test_failed_install_is_retried_on_the_next_run(manager, monkeypatch):
    monkeypatch.setattr(chunk_registry.subprocess, "run", _run_returning(1))
    manager.install_packages([PACKAGE])

    attempts = []

    def recording_run(args, **kwargs):  # noqa: ARG001
        attempts.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(chunk_registry.subprocess, "run", recording_run)
    manager.install_packages([PACKAGE])

    assert attempts, "a previously failed install must be attempted again"
    assert PACKAGE in attempts[0]


def test_successful_install_is_recorded(manager, monkeypatch):
    monkeypatch.setattr(chunk_registry.subprocess, "run", _run_returning(0))

    manager.install_packages([PACKAGE])

    assert manager.package_in_cache(PACKAGE), "a successful install should not be repeated"
