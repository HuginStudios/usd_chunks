"""A cached "installed" is checked against what this Python can actually see.

The package cache is written to disk at exit and trusted on the next start. A package
recorded there and then gone -- removed by hand, or seen only because a session had another
folder on sys.path -- read as installed for good: the install prompt never came, and a chunk
that needs the package fell back or failed on import. Blender's Scatter Points ran its
pure-Python spacing path for minutes that way, with scipy cached but not there.
"""
from __future__ import annotations

from collections import defaultdict

import pytest

from usd_chunks import chunk_registry

GONE = "usdchunks-test-package-that-is-not-installed"


@pytest.fixture()
def deps(monkeypatch):
    """The manager with a cache of our own, no pip, and a scene that records questions."""
    mgr = chunk_registry.ChunkDependencyManager()
    had = {k: mgr.__dict__[k] for k in ("initialized", "package_dir", "_cache") if k in mgr.__dict__}
    mgr.initialized = True
    mgr.package_dir = "/tmp/chunk-deps"
    mgr._cache = defaultdict(set)

    cls = chunk_registry.ChunkDependencyManager
    monkeypatch.setattr(cls, "update_cache", lambda self: None)
    monkeypatch.setattr(chunk_registry, "_run_pip", lambda *a, **k: pytest.fail("pip was invoked"))
    monkeypatch.setattr(cls, "_declined", set())
    monkeypatch.setattr(cls, "_listed", False)
    asked = []
    monkeypatch.setattr(
        cls, "should_install_packages",
        lambda self, packages, chunk_name="": asked.append((list(packages), chunk_name)) or False,
    )
    yield mgr, asked
    for key in ("initialized", "package_dir", "_cache"):
        mgr.__dict__.pop(key, None)
    mgr.__dict__.update(had)


def test_a_cached_package_that_is_gone_is_asked_for(deps):
    mgr, asked = deps
    mgr._cache[GONE].add("1.0")

    missing = mgr.install_packages([f"{GONE}>=1,<2"], "scatter_points")

    assert asked == [([f"{GONE}>=1,<2"], "scatter_points")]
    assert missing == [f"{GONE}>=1,<2"]
    assert not mgr.package_in_cache(f"{GONE}>=1,<2"), "the stale entry stays in the cache"


def test_a_cached_package_that_is_there_is_not_asked_for(deps):
    mgr, asked = deps
    mgr._cache["pytest"].add(pytest.__version__)

    assert mgr.install_packages(["pytest>=1"], "scatter_points") == []
    assert asked == []
