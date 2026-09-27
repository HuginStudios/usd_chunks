"""The frontends/hosts split: toolkit frontends under usd_chunks.frontends, host entry points
under usd_chunks.hosts, the CLI at usd_chunks.cli.

The old ``usd_chunks.plugins.*`` names keep working for code written before the move, and
nothing in this repository uses them any more.
"""
from __future__ import annotations

import importlib
import pathlib
import re
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent

#: Old name under usd_chunks.plugins -> the module it now aliases. Only the modules that
#: import without a toolkit are checked for identity here; the rest are covered by the
#: static check below.
_HEADLESS_SHIMS = {
    "usdchunks_cli": "usd_chunks.cli",
}


@pytest.mark.parametrize("old, new", sorted(_HEADLESS_SHIMS.items()))
def test_the_old_import_path_is_the_same_module(old, new):
    assert importlib.import_module(f"usd_chunks.plugins.{old}") is importlib.import_module(new)


def test_every_shim_names_a_module_that_exists():
    for shim in (REPO / "usd_chunks" / "plugins").glob("*.py"):
        if shim.name in ("__init__.py", "chunk_usd.py"):
            continue
        target = re.search(r'import_module\("([\w.]+)"\)', shim.read_text(encoding="utf-8"))
        assert target, f"{shim.name} is not an alias shim"
        path = REPO / (target[1].replace(".", "/") + ".py")
        assert path.is_file(), f"{shim.name} aliases {target[1]}, which does not exist"


def test_nothing_here_imports_the_old_path():
    offenders = []
    for folder in ("usd_chunks", "scripts", "tests"):
        for path in (REPO / folder).rglob("*.py"):
            rel = path.relative_to(REPO).as_posix()
            if rel.startswith("usd_chunks/plugins/") or rel in (
                    "tests/test_package_layout.py", "tests/test_chunk_stage_module.py"):
                continue
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            if re.search(r"usd_chunks\.plugins\b", text):
                offenders.append(rel)
    assert not offenders, f"import from usd_chunks.frontends / .hosts / .cli instead in: {offenders}"


def test_the_old_cli_path_still_runs_as_main():
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
