"""Packages installed before the per-Python-version split are migrated, not reinstalled.

Dependencies used to go flat into one folder that every interpreter -- usdview's 3.11,
Blender's 3.13 -- `pip install -t`'d into. Each now gets its own folder; on its first run
the packages it can import are copied over from the old one. Only a distribution whose
wheel tags fit the running interpreter and whose files still hash as installed is copied:
two interpreters installing one package into the shared folder left it half of each.
"""
from __future__ import annotations

import base64
import hashlib
import os

from usd_chunks import chunk_registry


def _hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def _install(folder, name: str, version: str, tag: str, files: dict[str, bytes]):
    """Lay `files` down in `folder` the way `pip install -t` would, RECORD and all."""
    dist_info = folder / f"{name}-{version}.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "WHEEL").write_text(f"Wheel-Version: 1.0\nTag: {tag}\n")
    record = []
    for path, data in files.items():
        full = folder / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)
        record.append(f"{path},{_hash(data)},{len(data)}")
    record.append(f"{dist_info.name}/RECORD,,")
    (dist_info / "RECORD").write_text("\n".join(record) + "\n")


def test_compatible_intact_packages_are_copied(tmp_path):
    legacy, target = tmp_path / "legacy", tmp_path / "py3x"
    _install(legacy, "purepkg", "1.0", "py3-none-any", {"purepkg/__init__.py": b"x = 1\n"})
    _install(legacy, "oldabi", "1.0", "cp27-cp27m-win_amd64", {"oldabi/__init__.py": b""})

    copied = chunk_registry.migrate_legacy_dependencies([str(legacy)], str(target))

    assert copied == ["purepkg-1.0"]
    assert (target / "purepkg" / "__init__.py").read_bytes() == b"x = 1\n"
    assert (target / "purepkg-1.0.dist-info" / "RECORD").is_file()
    assert not (target / "oldabi").exists()
    # Copied, not moved: another interpreter may still migrate from the same folder.
    assert (legacy / "purepkg" / "__init__.py").is_file()


def test_overwritten_distribution_falls_back_to_an_intact_one(tmp_path):
    legacy, target = tmp_path / "legacy", tmp_path / "py3x"
    _install(legacy, "pkg", "1.0", "py3-none-any", {"pkg/a.py": b"old\n"})
    _install(legacy, "pkg", "2.0", "py3-none-any", {"pkg/b.py": b"new\n"})
    # 2.0's file changed after install -- what a second interpreter's install leaves.
    (legacy / "pkg" / "b.py").write_bytes(b"tampered\n")

    copied = chunk_registry.migrate_legacy_dependencies([str(legacy)], str(target))

    assert copied == ["pkg-1.0"]
    assert (target / "pkg" / "a.py").is_file()
    assert not (target / "pkg" / "b.py").exists()


def test_runs_once_per_folder(tmp_path):
    legacy, target = tmp_path / "legacy", tmp_path / "py3x"
    _install(legacy, "purepkg", "1.0", "py3-none-any", {"purepkg/__init__.py": b""})

    assert chunk_registry.migrate_legacy_dependencies([str(legacy)], str(target))
    _install(legacy, "later", "1.0", "py3-none-any", {"later/__init__.py": b""})
    assert chunk_registry.migrate_legacy_dependencies([str(legacy)], str(target)) == []


def test_nothing_to_migrate_still_marks_the_folder(tmp_path):
    legacy, target = tmp_path / "legacy", tmp_path / "py3x"
    legacy.mkdir()

    assert chunk_registry.migrate_legacy_dependencies([str(legacy)], str(target)) == []
    assert os.listdir(target) == [chunk_registry._MIGRATED_MARKER]


def test_folder_holding_only_its_package_cache_is_migrated(tmp_path):
    legacy, target = tmp_path / "legacy", tmp_path / "py3x"
    _install(legacy, "purepkg", "1.0", "py3-none-any", {"purepkg/__init__.py": b""})
    target.mkdir()
    (target / "123_chunk_package_cache.json").write_text("{}")

    assert chunk_registry.migrate_legacy_dependencies([str(legacy)], str(target)) == ["purepkg-1.0"]


def test_folder_already_in_use_is_left_alone(tmp_path):
    legacy, target = tmp_path / "legacy", tmp_path / "py3x"
    _install(legacy, "purepkg", "1.0", "py3-none-any", {"purepkg/__init__.py": b""})
    _install(target, "installed", "1.0", "py3-none-any", {"installed/__init__.py": b""})

    assert chunk_registry.migrate_legacy_dependencies([str(legacy)], str(target)) == []
    assert not (target / "purepkg").exists()
