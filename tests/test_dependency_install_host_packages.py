"""Installing a chunk's packages leaves out what the host Python already has.

`pip install -t <dir>` ignores the packages of the running Python, so installing scipy for
Scatter Points in Blender also put a second numpy into the dependency dir, next to the one
Blender ships. The manager now asks pip first what it would install with those counted (a
dry run with a report) and installs just that, pinned, with --no-deps.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest

from usd_chunks import chunk_registry


@pytest.fixture
def manager(monkeypatch):
    """The dependency manager with a dependency dir of its own and no scene to show progress."""
    mgr = chunk_registry.ChunkDependencyManager()
    keys = ("initialized", "package_dir", "_cache")
    had = {k: mgr.__dict__[k] for k in keys if k in mgr.__dict__}
    mgr.initialized = True
    mgr.package_dir = "/tmp/chunk-deps"
    mgr._cache = defaultdict(set)
    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "scene", None, raising=False)
    monkeypatch.setattr(chunk_registry, "installed_version", lambda name: "1.0")
    monkeypatch.setattr(chunk_registry.ChunkDependencyManager, "_declined", set())
    yield mgr
    for k in keys:
        mgr.__dict__.pop(k, None)
    mgr.__dict__.update(had)


def _pip(calls, resolved, dry_run_code=0):
    """A fake _run_pip: the dry run reports `resolved` [(name, version)], the install succeeds."""

    def run(args, on_line=None):  # noqa: ARG001
        calls.append(list(args))
        if "--dry-run" in args:
            if dry_run_code:
                return dry_run_code, "no such option: --report"
            items = [{"metadata": {"name": n, "version": v}} for n, v in resolved]
            Path(args[args.index("--report") + 1]).write_text(json.dumps({"install": items}))
        return 0, ""

    return run


def _after_target(args):
    """What follows `-t <dir>` in a pip command line."""
    return args[args.index("-t") + 2:]


def test_only_what_the_host_lacks_is_installed(manager, monkeypatch):
    calls = []
    # numpy is not in the report: the host has a numpy that satisfies scipy.
    monkeypatch.setattr(chunk_registry, "_run_pip", _pip(calls, [("scipy", "1.18.1")]))

    assert manager.install(["scipy>=1.11,<2"], "scatter_points")

    assert len(calls) == 2
    assert "--dry-run" in calls[0] and "-t" not in calls[0], "resolved against the host"
    assert "scipy>=1.11,<2" in calls[0]
    assert _after_target(calls[1]) == ["--no-deps", "scipy==1.18.1"]


def test_nothing_is_installed_when_the_host_has_everything(manager, monkeypatch):
    calls = []
    monkeypatch.setattr(chunk_registry, "_run_pip", _pip(calls, []))

    assert manager.install(["numpy>=2,<3"], "noise_field")

    assert len(calls) == 1, "only the dry run"


def test_an_old_pip_installs_the_plain_way(manager, monkeypatch):
    """pip before 22.2 has no --report; the install still has to happen."""
    calls = []
    monkeypatch.setattr(chunk_registry, "_run_pip", _pip(calls, [], dry_run_code=2))

    assert manager.install(["scipy>=1.11,<2"], "scatter_points")

    assert _after_target(calls[-1]) == ["scipy>=1.11,<2"]
