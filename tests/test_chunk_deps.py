"""A chunk has to declare and pin what it imports, or a recipe's manifest lies.

Not really about installing. A recipe claims that regenerating it reproduces the dataset.
A dependency that is never declared never reaches the manifest, and one that is declared
without a version can move underneath an unchanged recipe -- so two runs disagree while
every recorded fact matches. That is worse than recording nothing, because it reads as
authoritative.

The import scan is static, which is complete here only because chunks import at module top
level and never inside a function body.
"""

from __future__ import annotations

import sys

import pytest

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import ChunkConfig, SceneSender


class _NoScene(SceneSender):
    """Enough of a scene to let the registry discover modules, and nothing more."""

    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []

    def should_install_package(self, package):
        return False


class _Module:
    """The slice of a ChunkModule the two checks touch."""

    def __init__(self, path, requirements=(), required_modules=()):
        self.module_file = path
        self.module_name = "fake"
        self.config = ChunkConfig(
            requirements=list(requirements), required_modules=list(required_modules)
        )


def _module(tmp_path, source, **kwargs):
    path = tmp_path / "fake_chunk.py"
    path.write_text(source, encoding="utf-8")
    return _Module(path, **kwargs)


class TestTopLevelImports:
    def test_it_finds_both_import_forms(self, tmp_path):
        mod = _module(tmp_path, "import numpy\nfrom mujoco import MjSpec\n")
        assert chunk_registry.top_level_imports(mod.module_file) == {"numpy", "mujoco"}

    def test_it_takes_the_root_of_a_dotted_import(self, tmp_path):
        mod = _module(tmp_path, "import os.path\nfrom pxr.UsdGeom import Cube\n")
        assert chunk_registry.top_level_imports(mod.module_file) == {"os", "pxr"}

    def test_a_relative_import_is_not_a_dependency(self, tmp_path):
        mod = _module(tmp_path, "from . import sibling\n")
        assert chunk_registry.top_level_imports(mod.module_file) == set()

    def test_an_import_inside_a_function_is_not_seen(self, tmp_path):
        """The known limit, and the reason the house rule against it matters: this scan is
        only complete while chunks import at module top level."""
        mod = _module(tmp_path, "def f():\n    import numpy\n    return numpy\n")
        assert chunk_registry.top_level_imports(mod.module_file) == set()

    def test_an_unparseable_file_is_empty_rather_than_fatal(self, tmp_path):
        mod = _module(tmp_path, "def broken(:\n")
        assert chunk_registry.top_level_imports(mod.module_file) == set()


class TestUndeclaredImports:
    def test_an_undeclared_package_is_reported(self, tmp_path):
        mod = _module(tmp_path, "import numpy\n")
        assert chunk_registry.undeclared_imports(mod) == ["numpy"]

    def test_declaring_it_clears_it(self, tmp_path):
        mod = _module(tmp_path, "import numpy\n", requirements=["numpy"])
        assert chunk_registry.undeclared_imports(mod) == []

    def test_a_pinned_declaration_still_matches(self, tmp_path):
        mod = _module(tmp_path, "import numpy\n", requirements=["numpy==2.1.0"])
        assert chunk_registry.undeclared_imports(mod) == []

    def test_the_stdlib_needs_no_declaring(self, tmp_path):
        mod = _module(tmp_path, "import os\nimport json\nimport hashlib\n")
        assert chunk_registry.undeclared_imports(mod) == []

    def test_the_host_provides_pxr_qt_and_the_framework(self, tmp_path):
        """A chunk cannot bring its own USD build or its own Qt -- it draws into the host's."""
        mod = _module(tmp_path, "from pxr import Usd\nfrom PySide6 import QtWidgets\n"
                                "from usd_chunks import chunk\n")
        assert chunk_registry.undeclared_imports(mod) == []

    def test_a_sibling_chunk_is_not_a_dependency(self, tmp_path):
        mod = _module(tmp_path, "import shader_defs\n")
        assert chunk_registry.undeclared_imports(mod, {"shader_defs"}) == []

    def test_the_import_name_is_mapped_to_the_package_name(self, tmp_path):
        """`import cv2` is satisfied by declaring opencv-python, and nothing in the import
        statement says so."""
        mod = _module(tmp_path, "import cv2\n", requirements=["opencv-python"])
        assert chunk_registry.undeclared_imports(mod) == []

    def test_declaring_the_wrong_package_does_not_clear_it(self, tmp_path):
        mod = _module(tmp_path, "import cv2\n", requirements=["numpy"])
        assert chunk_registry.undeclared_imports(mod) == ["cv2"]


class TestUnpinnedRequirements:
    def test_a_bare_requirement_is_reported(self, tmp_path):
        mod = _module(tmp_path, "", requirements=["mujoco", "numpy"])
        assert chunk_registry.unpinned_requirements(mod) == ["mujoco", "numpy"]

    def test_a_pinned_one_is_not(self, tmp_path):
        mod = _module(tmp_path, "", requirements=["mujoco==3.2.0"])
        assert chunk_registry.unpinned_requirements(mod) == []

    def test_a_chunk_with_no_requirements_is_clean(self, tmp_path):
        assert chunk_registry.unpinned_requirements(_module(tmp_path, "")) == []


@pytest.mark.skipif(sys.version_info < (3, 10), reason="needs sys.stdlib_module_names")
def test_the_real_chunk_library_has_no_undeclared_imports(tmp_path):
    """Regression guard on scripts/ itself: every chunk declares what it imports.

    The one offender this check found was rigs.turntabler, doing `from scripts.modelling
    import add_mesh` -- which resolves only while the repo root happens to be importable as
    a `scripts` package, and does not for anyone whose chunks live elsewhere. That module
    was outdated and has been removed, so the expected set is now empty and any new entry
    is a regression.
    """
    registry = chunk_registry.ChunkRegistry()
    registry.initialize(_NoScene(), "pytest")
    modules = registry.get_chunk_modules()
    if not modules:
        pytest.skip("no chunk modules registered -- USD_CHUNK_DIRS must point at scripts/")
    siblings = {m.module_name.split(".")[-1] for m in modules}

    offenders = {
        m.module_name for m in modules
        if chunk_registry.undeclared_imports(m, siblings)
    }
    assert not offenders, f"undeclared imports: {sorted(offenders)}"


def test_every_chunk_pins_its_requirements():
    """A declared dependency without a version pins nothing.

    Two runs a month apart can then use different solvers from an identical recipe with an
    identical manifest, and the diff has no explanation in it. `mujoco` was the case that
    mattered -- a solver change moves physics silently, and nothing recorded would say so.

    Enforced rather than reported: `usdchunks deps --strict` gates CI, and this gates the
    library itself.
    """
    registry = chunk_registry.ChunkRegistry()
    registry.initialize(_NoScene(), "pytest")
    modules = registry.get_chunk_modules()
    if not modules:
        pytest.skip("no chunk modules registered -- USD_CHUNK_DIRS must point at scripts/")

    unpinned = {
        m.module_name: reqs for m in modules
        if (reqs := chunk_registry.unpinned_requirements(m))
    }
    assert not unpinned, f"requirements declared without a version: {unpinned}"
