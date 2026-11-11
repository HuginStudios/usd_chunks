"""Tests for .github/scripts/release.py, which the release workflow runs on a tag."""

from __future__ import annotations

import importlib.util
import re
import tomllib
from pathlib import Path

import pytest
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("release", ROOT / ".github" / "scripts" / "release.py")
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)

CHANGELOG = """# Changelog

## [Unreleased]

### Added

- Not released yet.

## [1.0.0-rc] - 2026-11-11

### Added

- A thing.

## [0.9.0] - 2026-01-01

- Older.
"""


def _pyproject(tmp_path: Path, version: str) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(f'[project]\nname = "x"\nversion = "{version}"\n', encoding="utf-8")
    return path


def test_rc_tag_matches_python_spelling(tmp_path):
    release.check("v1.0.0-rc", _pyproject(tmp_path, "1.0.0rc0"))
    release.check("v1.0.0-rc.2", _pyproject(tmp_path, "1.0.0rc2"))


def test_mismatched_tag_fails(tmp_path):
    with pytest.raises(ValueError, match="does not match"):
        release.check("v1.0.1", _pyproject(tmp_path, "1.0.0"))


def test_tag_without_v_fails(tmp_path):
    with pytest.raises(ValueError, match="start with 'v'"):
        release.check("1.0.0", _pyproject(tmp_path, "1.0.0"))


def test_notes_are_the_tag_section_only(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(CHANGELOG, encoding="utf-8")
    assert release.notes("v1.0.0-rc", changelog) == "### Added\n\n- A thing.\n"
    assert release.notes("v0.9.0", changelog) == "- Older.\n"


def test_missing_section_fails(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(CHANGELOG, encoding="utf-8")
    with pytest.raises(ValueError, match="no '## \\[2.0.0\\]' section"):
        release.notes("v2.0.0", changelog)


def test_pyproject_version_has_a_changelog_section():
    """Tagging the version in pyproject.toml would pass the workflow's checks."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = Version(pyproject["project"]["version"])
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    sections = re.findall(r"^## \[([^\]]+)\]", changelog, re.MULTILINE)
    tags = [f"v{s}" for s in sections if s != "Unreleased" and Version(s) == version]
    assert tags, f"CHANGELOG.md has no section for version {version}"
    release.check(tags[0])
    assert release.notes(tags[0])
