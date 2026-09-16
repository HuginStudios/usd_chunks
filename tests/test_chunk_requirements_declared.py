"""A chunk's third-party dependencies have to be declared, not just mentioned.

The RBD Solver described its need for mujoco in its prose description and nowhere else.
When mujoco was absent the chunk simply did nothing and said so to a console nobody was
reading -- the host had no package name to offer, because from its side there was nothing
to offer. Prose is for the person browsing the chunk list; ``requirements`` is the half
the framework can act on.

Declaring costs nothing at import time: the config is harvested from the source by
``_ConfigTransformer`` before the module body ever runs, so the requirement is legible
precisely in the case that matters -- the package is missing and the import would fail.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from usd_chunks import chunk_registry

REPO = Path(__file__).resolve().parent.parent

# Shipped with the host or part of this repo -- never installed from an index.
NOT_FROM_PIP = {
    "pxr",
    "usd_chunks",
    "scripts",
    "PySide2",
    "PySide6",
    "PyQt5",
    "PyQt6",
    "shiboken6",
}

def declared_requirements(path: Path) -> list[str]:
    """What the framework reads off the source, without importing it."""
    module = chunk_registry.ChunkModule(module_name=path.stem, module_file=path)
    chunk_registry._pre_load_module(path.read_text(encoding="utf-8"), {}, module)
    return list(getattr(module.config, "requirements", None) or [])


def tracked_chunk_sources() -> list[Path]:
    """Chunk modules that are part of the repo.

    Deliberately git-tracked only: scripts/ is also where a developer's own unfinished
    chunks sit, and this suite has no business failing over those.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO), "ls-files", "scripts/*.py", "scripts/**/*.py"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - no git present
        pytest.skip("git is not available to enumerate tracked chunks")

    paths = []
    for line in out.splitlines():
        path = REPO / line.strip()
        if not path.suffix == ".py" or not path.is_file():
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if "def config(" in source or "def main(" in source:
            paths.append(path)
    return paths


@pytest.mark.parametrize(
    "relpath, package",
    [
        ("scripts/dynamics/rbd_solver.py", "mujoco"),
        ("scripts/modelling/add_mesh.py", "numpy"),
        ("scripts/modelling/add_curves.py", "numpy"),
    ],
)
def test_requirement_is_legible_without_importing(relpath, package):
    """The package can be named while it is still missing -- which is the whole point."""
    declared = {chunk_registry._normalise_package(r) for r in declared_requirements(REPO / relpath)}
    assert package in declared


def test_rbd_solver_does_not_claim_its_optional_dependency():
    """scipy only smooths heightfield colliders and is imported where it is used.

    Listing it would make an optional look mandatory, and would ask the user to install a
    large package for a fallback they already have.
    """
    assert "scipy" not in declared_requirements(REPO / "scripts/dynamics/rbd_solver.py")


def test_no_tracked_chunk_imports_an_undeclared_package():
    """One rule, not two.

    This used to carry its own import-to-distribution map, its own stdlib check and its own
    top-level walk, duplicating chunk_registry's. Two copies of one rule drift, and the
    copy in the test is the thing meant to protect the manifest -- once it disagrees with
    the scanner that writes the manifest it protects nothing. They shared a bug, too: both
    read `tree.body` literally and so never saw a try-guarded import, which is how
    rbd_solver's mujoco stayed invisible.

    Guarded imports now count. The earlier version skipped them on the grounds that a
    guarded import is a deliberate optional -- true for whether the chunk still *runs*, and
    beside the point for whether its version reaches the manifest. mujoco is optional to
    importing rbd_solver and decisive for what it computes. Declaring costs nothing: the
    registry only installs when USD_CHUNK_AUTO_INSTALL says so.

    Still skipped: imports inside a function body, which is the actual house rule and where
    rbd_solver's scipy fallback lives.
    """
    siblings = {p.stem for p in tracked_chunk_sources()}
    undeclared = {}
    for path in tracked_chunk_sources():
        module = chunk_registry.ChunkModule(module_name=path.stem, module_file=path)
        chunk_registry._pre_load_module(path.read_text(encoding="utf-8"), {}, module)
        if missing := chunk_registry.undeclared_imports(module, siblings):
            undeclared[path.relative_to(REPO).as_posix()] = sorted(missing)

    assert not undeclared, (
        "these chunks import a package they never declare, so its version never reaches a "
        f"recipe's manifest -- add requirements=[...] to config(): {undeclared}"
    )


def test_the_scanner_sees_a_try_guarded_import():
    """The gap that let rbd_solver's mujoco through.

    A guarded import obeys the house rule lexically -- it is at module scope, not inside a
    function -- it simply is not an ast.Import node in tree.body. Reading the top level
    literally missed it, and nothing failed, which is the worst shape for a blind spot in
    the thing that decides what a manifest records.
    """
    source = (
        "import os\n"
        "try:\n"
        "    import mujoco\n"
        "except ImportError:\n"
        "    mujoco = None\n"
    )
    path = REPO / "scripts/dynamics/rbd_solver.py"
    assert "mujoco" in chunk_registry.top_level_imports(path), (
        "the real case: rbd_solver guards mujoco and numpy in a try/except"
    )

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        guarded = Path(tmp) / "guarded.py"
        guarded.write_text(source, encoding="utf-8")
        assert chunk_registry.top_level_imports(guarded) == {"os", "mujoco"}


def test_the_scanner_still_ignores_a_function_body_import():
    """rbd_solver imports scipy where it is used, as an optional fallback. That is the
    house rule the scan actually depends on, and it must stay skipped -- listing scipy
    would make an optional look mandatory."""
    assert "scipy" not in chunk_registry.top_level_imports(REPO / "scripts/dynamics/rbd_solver.py")


def test_a_declared_requirement_reaches_the_installer(monkeypatch):
    """The declaration is not decoration: loading the chunk hands the name to pip.

    This is the whole path the RBD Solver was missing. What happens next -- a warning
    naming the package, or an actual install -- is USD_CHUNK_AUTO_INSTALL's business and
    is covered in test_dependency_install_optin; all that matters here is that mujoco
    arrives somewhere the user can be told about it.
    """
    path = REPO / "scripts/dynamics/rbd_solver.py"
    module = chunk_registry.ChunkModule(module_name=path.stem, module_file=path)
    chunk_registry._pre_load_module(path.read_text(encoding="utf-8"), {}, module)

    asked_for = []
    monkeypatch.setattr(chunk_registry.ChunkDependencyManager, "initialize", lambda self: None)
    monkeypatch.setattr(
        chunk_registry.ChunkDependencyManager,
        "install_packages",
        lambda self, requirements: asked_for.extend(requirements),
    )
    chunk_registry.ChunkDependencyManager().initialized = True

    module._install_deps()

    # By package name, not by the literal string: the requirement carries a range
    # ("mujoco>=3.1,<4") and changing that range must not break this test, which is about
    # the path existing rather than which versions are on it. split_requirement rather than
    # package_info, because the latter only understands "==".
    names = {chunk_registry.split_requirement(r)[0] for r in asked_for}
    assert "mujoco" in names, asked_for
