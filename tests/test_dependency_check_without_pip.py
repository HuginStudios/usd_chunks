"""A Python without pip still finds the packages it can import.

usdview's bundled Python ships without pip. The dependency check listed installed packages
with `pip list` only, so there it found nothing: every chunk requirement read as missing, the
user was asked to install mujoco and numpy that were already on PYTHONPATH, and pip then
failed with "No module named pip".
"""

from __future__ import annotations

import subprocess
import types
from collections import defaultdict

import pytest

from usd_chunks import chunk_registry


@pytest.fixture()
def pipless(monkeypatch):
    """The dependency manager, with every `pip` subprocess finding nothing, as without pip."""
    mgr = chunk_registry.ChunkDependencyManager()
    had = {k: mgr.__dict__[k] for k in ("initialized", "package_dir", "_cache") if k in mgr.__dict__}
    mgr.initialized = True
    mgr.package_dir = "/tmp/chunk-deps"
    mgr._cache = defaultdict(set)

    def no_pip(args, **kwargs):
        assert "pip" in args, f"only pip is expected to run, not {args}"
        return types.SimpleNamespace(stdout=b"", stderr=b"No module named pip", returncode=1)

    monkeypatch.setattr(subprocess, "run", no_pip)
    yield mgr

    for key in ("initialized", "package_dir", "_cache"):
        mgr.__dict__.pop(key, None)
    mgr.__dict__.update(had)


def test_importable_packages_count_as_installed_without_pip(pipless) -> None:
    pipless.update_cache()

    assert pipless.package_in_cache("pytest>=1"), "pytest is importable, so it is installed"
    assert not pipless.package_in_cache("pytest<1"), "the version still has to match"
    assert not pipless.package_in_cache("surely-not-a-real-package-name")
