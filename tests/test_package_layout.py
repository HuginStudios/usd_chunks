"""Where modules live, and the old names that still import.

Toolkit frontends are under usd_chunks.frontends, host entry points under usd_chunks.hosts,
the CLI at usd_chunks.cli; the graph engine, gizmos and store seam are subpackages of the
core. Every old name in usd_chunks._moved.MOVED keeps importing as an alias, and nothing in
this repository uses one any more.
"""
from __future__ import annotations

import importlib
import pathlib
import re
import subprocess
import sys

import pytest

from usd_chunks import _moved

REPO = pathlib.Path(__file__).resolve().parent.parent

#: Old names whose new module imports without a UI toolkit, so identity can be checked here.
_HEADLESS = sorted(
    (old, new) for old, new in _moved.MOVED.items()
    if not new.startswith("usd_chunks.frontends."))


@pytest.mark.parametrize("new", sorted(set(_moved.MOVED.values())))
def test_every_alias_names_a_module_that_exists(new):
    path = REPO / (new.replace(".", "/") + ".py")
    assert path.is_file(), f"{new} is an alias target but has no file"


@pytest.mark.parametrize("old, new", _HEADLESS)
def test_the_old_name_is_the_same_module(old, new):
    moved = importlib.import_module(new)
    assert importlib.import_module(old) is moved
    assert moved.__spec__.name == new


def test_from_import_of_an_old_name():
    from usd_chunks.plugins import usdchunks_cli
    from usd_chunks import chunk_graph

    assert usdchunks_cli is importlib.import_module("usd_chunks.cli")
    assert chunk_graph is importlib.import_module("usd_chunks.graph.engine")


def test_no_old_name_is_still_a_file():
    for old in _moved.MOVED:
        path = REPO / (old.replace(".", "/") + ".py")
        assert not path.exists(), f"{path} shadows the alias for {old}"


def test_nothing_here_imports_an_old_name():
    old_names = "|".join(re.escape(old) for old in _moved.MOVED)
    leaves = "|".join(old.rsplit(".", 1)[1] for old in _moved.MOVED if old.count(".") == 1)
    dotted = re.compile(rf"\b({old_names})\b(?![\"'.\w])|\b({old_names})\.")
    from_import = re.compile(rf"^\s*from usd_chunks import [^#\n]*\b({leaves})\b", re.M)
    offenders = []
    for folder in ("usd_chunks", "chunks", "tests"):
        for path in (REPO / folder).rglob("*.py"):
            rel = path.relative_to(REPO).as_posix()
            if rel in ("usd_chunks/_moved.py", "tests/test_package_layout.py",
                       "tests/test_chunk_stage_module.py"):
                continue
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            code = "\n".join(line for line in text.splitlines()
                             if "import" in line or "import_module" in line or "setattr" in line)
            if dotted.search(code) or from_import.search(text):
                offenders.append(rel)
    assert not offenders, f"import the new module name instead in: {offenders}"


def test_the_old_cli_name_still_runs_as_main():
    proc = subprocess.run([sys.executable, "-m", "usd_chunks.plugins.usdchunks_cli", "--help"],
                          cwd=str(REPO), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "usdchunks" in proc.stdout


@pytest.mark.parametrize("host", ["usdview", "kraken", "houdini"])
def test_plugin_path_resolves_every_host(host):
    proc = subprocess.run([sys.executable, "-m", "usd_chunks.cli", "plugin-path", "--host", host],
                          cwd=str(REPO), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert pathlib.Path(proc.stdout.strip()).is_dir()
