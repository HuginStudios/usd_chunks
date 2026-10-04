"""Installing a chunk's pip dependencies needs the user's yes, asked once per chunk.

A chunk module is ordinary Python that anyone can drop on USD_CHUNK_DIRS, so opening a
network must not be able to put packages into the user's environment as a side effect. So
the scene asks -- usdview with a Yes/No dialog, the CLI on an interactive terminal -- naming
the chunk and every package it is missing, and a host with nobody to ask says no.

USD_CHUNK_AUTO_INSTALL changes that: truthy installs without asking (the user said yes ahead
of time), falsey never installs and only names the missing packages in a warning.
"""
from __future__ import annotations

import io
import logging
import sys

import pytest

from usd_chunks import chunk_registry, chunk_script


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

    monkeypatch.setattr(
        chunk_registry, "_run_pip", lambda *a, **k: pytest.fail("pip was invoked"),
    )

    # A "no" is remembered per process; start every test without one. _listed likewise:
    # install_packages sets it on the class, and left True with update_cache stubbed, the
    # rest of this worker never lists what is installed -- numpy reads as missing.
    monkeypatch.setattr(cls, "_declined", set())
    monkeypatch.setattr(cls, "_listed", False)

    asked = []
    monkeypatch.setattr(
        cls, "should_install_packages",
        lambda self, packages, chunk_name="": asked.append((list(packages), chunk_name)) or False,
    )
    yield mgr, asked

    for key in ("initialized", "package_dir"):
        mgr.__dict__.pop(key, None)
    mgr.__dict__.update(had)


def test_asks_by_default(deps, monkeypatch, caplog):
    """Unset, the scene is asked -- once, for every missing package, naming the chunk."""
    mgr, asked = deps
    monkeypatch.delenv("USD_CHUNK_AUTO_INSTALL", raising=False)

    with caplog.at_level(logging.WARNING, logger=chunk_registry.logger.name):
        missing = mgr.install_packages(["requests>=2,<3", "numpy>=2,<3"], "AMDMaterialXLibrary")

    assert asked == [(["requests>=2,<3", "numpy>=2,<3"], "AMDMaterialXLibrary")]
    assert missing == ["requests>=2,<3", "numpy>=2,<3"], "declined, so still missing"
    assert "AMDMaterialXLibrary" in caplog.text and "requests>=2,<3" in caplog.text, (
        "a no must still say what is missing and for which chunk"
    )


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_falsey_values_turn_asking_off(deps, monkeypatch, caplog, value):
    mgr, asked = deps
    monkeypatch.setenv("USD_CHUNK_AUTO_INSTALL", value)

    with caplog.at_level(logging.WARNING, logger=chunk_registry.logger.name):
        missing = mgr.install_packages(["requests==2.31.0"], "chunky")

    assert asked == [], "the prompt must not even be reached while asking is off"
    assert missing == ["requests==2.31.0"]
    assert "requests==2.31.0" in caplog.text, "the user has to be told what is missing"
    assert "pip install" in caplog.text, "and how to install it"


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_truthy_values_install_without_asking(deps, monkeypatch, value):
    mgr, asked = deps
    monkeypatch.setenv("USD_CHUNK_AUTO_INSTALL", value)
    installed = []
    monkeypatch.setattr(
        chunk_registry.ChunkDependencyManager, "install",
        lambda self, packages, chunk_name="": installed.extend(packages) or True,
    )

    assert mgr.install_packages(["requests"], "chunky") == []

    assert asked == [], "the user already said yes by setting it"
    assert installed == ["requests"]


def test_a_no_is_not_asked_again(deps, monkeypatch):
    """Ten nodes of one chunk in a network load it ten times; one dialog, not ten."""
    mgr, asked = deps
    monkeypatch.delenv("USD_CHUNK_AUTO_INSTALL", raising=False)

    mgr.install_packages(["requests"], "chunky")
    assert mgr.install_packages(["requests"], "chunky") == ["requests"]

    assert len(asked) == 1


def test_a_yes_installs_and_reports_nothing_missing(deps, monkeypatch):
    mgr, _ = deps
    monkeypatch.delenv("USD_CHUNK_AUTO_INSTALL", raising=False)
    cls = chunk_registry.ChunkDependencyManager
    monkeypatch.setattr(cls, "should_install_packages", lambda self, packages, chunk_name="": True)
    installed = []
    monkeypatch.setattr(cls, "install", lambda self, packages, chunk_name="": installed.extend(packages) or True)

    assert mgr.install_packages(["requests>=2,<3"], "chunky") == []
    assert installed == ["requests>=2,<3"]


def test_the_scene_runs_pip_and_sees_its_output(deps, monkeypatch):
    """install() hands pip to scene.run_install, so a host can show progress; the lines pip
    prints reach it as they arrive."""
    mgr, _ = deps
    seen = []

    class _Scene:
        def run_install(self, packages, chunk_name, run):
            seen.append((list(packages), chunk_name))
            return run(seen.append)

    def fake_pip(args, on_line=None):
        on_line("Collecting requests")
        on_line("Successfully installed requests-2.32.3")
        return 0, ""

    monkeypatch.setattr(chunk_registry, "_run_pip", fake_pip)
    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "scene", _Scene(), raising=False)
    monkeypatch.setattr(chunk_registry, "installed_version", lambda name: "2.32.3")
    cache = mgr.__dict__.get("_cache")
    mgr._cache = chunk_registry.defaultdict(set)
    try:
        assert mgr.install(["requests>=2,<3"], "AMDMaterialXLibrary") is True
    finally:
        mgr.__dict__.pop("_cache", None)
        if cache is not None:
            mgr._cache = cache

    assert seen == [
        (["requests>=2,<3"], "AMDMaterialXLibrary"),
        "Collecting requests",
        "Successfully installed requests-2.32.3",
    ]


def test_nothing_missing_asks_nothing(deps, monkeypatch):
    mgr, asked = deps
    monkeypatch.delenv("USD_CHUNK_AUTO_INSTALL", raising=False)
    # On the class, for the same reason the fixture does: an instance patch here is undone
    # by writing back whatever it found -- the fixture's own lambda -- onto the singleton,
    # where it outlives this file and answers False to every later cache check.
    monkeypatch.setattr(
        chunk_registry.ChunkDependencyManager, "package_in_cache", lambda self, package: True
    )

    assert mgr.install_packages(["requests"], "chunky") == []
    assert asked == []


def test_a_missing_requirement_is_named_instead_of_a_traceback(monkeypatch, caplog):
    """A declined install that breaks the module's import says what is missing, rather than
    "module has no attribute 'main'", and leaves the module to be read afresh next time."""

    class _Broken:
        def __getattr__(self, name):
            raise ModuleNotFoundError("No module named 'requests'")

    module = chunk_registry.ChunkModule.__new__(chunk_registry.ChunkModule)
    module.module_name = "AMDMaterialXLibrary"
    module.loaded = False
    module._needs_read = False
    module.module = _Broken()
    monkeypatch.setattr(chunk_registry.ChunkModule, "_install_deps", lambda self: ["requests>=2,<3"])

    with caplog.at_level(logging.ERROR, logger=chunk_registry.logger.name):
        assert module._pre_load() is False

    assert "AMDMaterialXLibrary" in caplog.text and "requests>=2,<3" in caplog.text
    assert "Traceback" not in caplog.text
    assert module.loaded is False
    assert module._needs_read is True and module.module is None, "next load reads the file again"


def test_an_optional_requirement_still_loads(monkeypatch):
    """A chunk that guards its import loads without the package -- cad_import's readout."""

    class _Fine:
        def main(self):
            return []

    module = chunk_registry.ChunkModule.__new__(chunk_registry.ChunkModule)
    module.module_name = "cad_import"
    module.loaded = False
    module._needs_read = False
    module.module = _Fine()
    monkeypatch.setattr(chunk_registry.ChunkModule, "_install_deps", lambda self: ["cadquery-ocp>=7,<8"])

    assert module._pre_load() is True


class _Tty(io.StringIO):
    def isatty(self):
        return True


def _cli_scene():
    """A chunk_script.Scene without its constructor, which would start the registry."""
    return chunk_script.Scene.__new__(chunk_script.Scene)


@pytest.mark.parametrize("answer, expected", [("y\n", True), ("yes\n", True), ("\n", False), ("n\n", False)])
def test_cli_asks_on_a_terminal(monkeypatch, capsys, answer, expected):
    monkeypatch.setattr(chunk_script.sys, "stdin", _Tty(answer))

    assert _cli_scene().should_install_packages(["requests>=2,<3"], "AMDMaterialXLibrary") is expected
    prompt = capsys.readouterr().out
    assert "requests>=2,<3" in prompt and "AMDMaterialXLibrary" in prompt


def test_cli_echoes_pip_progress(capsys):
    def run(on_line):
        on_line("Collecting requests")
        return 0, ""

    assert _cli_scene().run_install(["requests"], "chunky", run) == (0, "")
    err = capsys.readouterr().err
    assert "requests for chunk chunky" in err and "  Collecting requests" in err


def test_cli_without_a_terminal_says_no(monkeypatch):
    """CI, a pipe, a cron job: nobody to answer, and input() would read EOF or block."""
    monkeypatch.setattr(chunk_script.sys, "stdin", io.StringIO("y\n"))

    assert _cli_scene().should_install_packages(["requests"], "chunky") is False


def test_run_pip_streams_each_line():
    """The real _run_pip, on a command that is not pip: lines arrive one by one, stripped."""
    lines = []
    code, output = chunk_registry._run_pip(
        [sys.executable, "-c", "print('Collecting a'); print(); print('Installing b')"], lines.append
    )
    assert code == 0
    assert lines == ["Collecting a", "Installing b"]
    assert "Installing b" in output
