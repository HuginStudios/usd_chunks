"""The installed-package cache is plain JSON, and an unreadable one is thrown away.

Nothing depends on this cache being right -- update_cache rebuilds it from `pip list` on a
miss -- so the correct answer to a file we cannot read is to drop it.
"""

from __future__ import annotations

import json

import pytest

from usd_chunks import chunk_cache


def test_versions_survive_the_round_trip(tmp_path):
    path = tmp_path / "cache.json"
    chunk_cache.write_package_cache(path, {"numpy": {"2.4.3", "2.4.2"}, "requests": {"2.31.0"}})

    assert chunk_cache.read_package_cache(path) == {
        "numpy": {"2.4.3", "2.4.2"},
        "requests": {"2.31.0"},
    }


def test_a_version_that_could_not_be_determined_round_trips_as_none(tmp_path):
    """package_info yields None when a package has no `==version`, and that is a real entry."""
    path = tmp_path / "cache.json"
    chunk_cache.write_package_cache(path, {"mystery": {None}})

    assert chunk_cache.read_package_cache(path) == {"mystery": {None}}


def test_the_file_is_plain_readable_json(tmp_path):
    """Someone opening this file should see package names and versions."""
    path = tmp_path / "cache.json"
    chunk_cache.write_package_cache(path, {"numpy": {"2.4.3"}})

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw == {"numpy": ["2.4.3"]}


def test_an_entry_that_is_not_a_version_list_drops_the_cache(tmp_path):
    """Each package must map to a list; any other shape is not parsed at all."""
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"absl-py": {"version": "2.4.0"}}), encoding="utf-8")

    assert chunk_cache.read_package_cache(path) == {}


@pytest.mark.parametrize(
    "content",
    ["{ not json", "[]", '"a string"', "null", ""],
    ids=["corrupt", "list", "string", "null", "empty"],
)
def test_anything_unreadable_reads_as_an_empty_cache(tmp_path, content):
    path = tmp_path / "cache.json"
    path.write_text(content, encoding="utf-8")

    assert chunk_cache.read_package_cache(path) == {}


def test_a_missing_file_is_not_an_error(tmp_path):
    assert chunk_cache.read_package_cache(tmp_path / "nothing-here.json") == {}


def test_the_directory_is_created_on_write(tmp_path):
    path = tmp_path / "made" / "up" / "cache.json"
    chunk_cache.write_package_cache(path, {"numpy": {"2.4.3"}})

    assert path.exists()


def test_the_checksum_separates_two_interpreters():
    """The cache file is keyed by interpreter: two Pythons must not share one."""
    assert chunk_cache.compute_checksum("C:/py311/python.exe") != chunk_cache.compute_checksum("C:/py313/python.exe")
    assert chunk_cache.compute_checksum("same") == chunk_cache.compute_checksum("same")
