"""A module's `hosts` config: which applications it loads in, by name and version."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from usd_chunks.chunk_core import ChunkConfig
from usd_chunks.chunk_registry import ChunkModule
from usd_chunks.chunk_scene import SceneSender


def _loads(hosts, name, version):
    module = SimpleNamespace(config=ChunkConfig(hosts=hosts), module_name="m")
    return ChunkModule.should_load_for_host(module, name, version)


@pytest.mark.parametrize("hosts", [[], None])
def test_no_hosts_loads_everywhere(hosts):
    assert _loads(hosts, "blender", "5.1.0")
    assert _loads(hosts, "", "")


@pytest.mark.parametrize("name, version, expected", [
    ("blender", "5.1.0", True),
    ("blender", "5.2.1", True),
    ("blender", "5.0.3", False),
    ("houdini", "21.5.400", True),
    ("houdini", "21.0.1", False),
    ("Blender", "5.1.0", True),     # names are not case-sensitive
    ("usdview", "0.26.5", False),   # a host the module does not name
    ("", "", False),                # no particular host
])
def test_a_host_must_be_named_and_new_enough(name, version, expected):
    assert _loads(["houdini>=21.5", "blender>=5.1"], name, version) is expected


def test_a_bare_name_takes_any_version():
    assert _loads(["blender"], "blender", "")
    assert _loads(["blender"], "blender", "2.79")


def test_a_versioned_entry_needs_a_version_to_compare():
    assert not _loads(["blender>=5.1"], "blender", "")


def test_an_unreadable_version_does_not_load(caplog):
    assert not _loads(["blender>=5.1"], "blender", "five")
    assert "cannot compare" in caplog.text


def test_a_scene_names_no_host_by_default():
    assert SceneSender.get_host(None) == ("", "")
