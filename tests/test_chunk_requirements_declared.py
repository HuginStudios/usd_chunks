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

import ast
import functools
import importlib.metadata
import re
import subprocess
import sys
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

# What you import is not always what you install. The registry hands requirements to pip,
# so a chunk declares the distribution name; the import statement names the module.
IMPORT_TO_DISTRIBUTION = {
    "cv2": "opencv-python",
    "PIL": "pillow",
    "yaml": "pyyaml",
    "sklearn": "scikit-learn",
    "skimage": "scikit-image",
}


@functools.lru_cache(maxsize=1)
def _packages_distributions() -> dict:
    return importlib.metadata.packages_distributions()


def candidate_distributions(imported: str) -> set[str]:
    """Every pip name that could plausibly supply this import.

    Several distributions can provide one module -- opencv-python and
    opencv-python-headless both give you cv2 -- so there is no single right answer to
    check against. Asking the local environment on its own would be worse than useless
    here: it would pass or fail a chunk on which variant this particular machine happens
    to have installed.
    """
    candidates = {imported, IMPORT_TO_DISTRIBUTION.get(imported, imported)}
    candidates.update(_packages_distributions().get(imported, ()))
    return {normalise(name) for name in candidates}


def normalise(name: str) -> str:
    """PEP 503 name, with any version specifier or extra dropped."""
    for sep in ("[", "<", ">", "=", "!", "~", ";", " "):
        name = name.split(sep)[0]
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def declared_requirements(path: Path) -> list[str]:
    """What the framework reads off the source, without importing it."""
    module = chunk_registry.ChunkModule(module_name=path.stem, module_file=path)
    chunk_registry._pre_load_module(path.read_text(encoding="utf-8"), {}, module)
    return list(getattr(module.config, "requirements", None) or [])


def unguarded_third_party_imports(tree: ast.Module) -> set[str]:
    """Top-level imports that are not wrapped in a try/except.

    A guarded import is a deliberate optional -- the module keeps working without it. A
    bare one at module scope takes the whole chunk down with it, which is the case the
    registry needs a package name for.
    """
    roots = set()
    for node in tree.body:  # module scope only; a try/except body is not reached
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return {root for root in roots if root not in sys.stdlib_module_names and root not in NOT_FROM_PIP}


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
        ("scripts/rbd_solver.py", "mujoco"),
        ("scripts/modelling/add_mesh.py", "numpy"),
        ("scripts/modelling/add_curves.py", "numpy"),
    ],
)
def test_requirement_is_legible_without_importing(relpath, package):
    """The package can be named while it is still missing -- which is the whole point."""
    assert package in {normalise(r) for r in declared_requirements(REPO / relpath)}


def test_rbd_solver_does_not_claim_its_optional_dependency():
    """scipy only smooths heightfield colliders and is imported where it is used.

    Listing it would make an optional look mandatory, and would ask the user to install a
    large package for a fallback they already have.
    """
    assert "scipy" not in declared_requirements(REPO / "scripts/rbd_solver.py")


def test_no_tracked_chunk_imports_an_undeclared_package():
    undeclared = {}
    for path in tracked_chunk_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        declared = {normalise(req) for req in declared_requirements(path)}
        missing = {
            imported
            for imported in unguarded_third_party_imports(tree)
            if not candidate_distributions(imported) & declared
        }
        if missing:
            undeclared[path.relative_to(REPO).as_posix()] = sorted(missing)

    assert not undeclared, (
        "these chunks import a package they never declare, so the registry has no name to "
        f"offer when it is absent -- add requirements=[...] to config(): {undeclared}"
    )


def test_a_declared_requirement_reaches_the_installer(monkeypatch):
    """The declaration is not decoration: loading the chunk hands the name to pip.

    This is the whole path the RBD Solver was missing. What happens next -- a warning
    naming the package, or an actual install -- is USD_CHUNK_AUTO_INSTALL's business and
    is covered in test_dependency_install_optin; all that matters here is that mujoco
    arrives somewhere the user can be told about it.
    """
    path = REPO / "scripts/rbd_solver.py"
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

    # By package name, not by the literal string: the requirement is pinned
    # ("mujoco==3.12.0") and repinning it must not break this, which is about the path
    # existing rather than about which version is on it.
    names = {chunk_registry.ChunkDependencyManager.package_info(r)[0] for r in asked_for}
    assert "mujoco" in names, asked_for
