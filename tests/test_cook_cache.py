"""The cook cache: a cache_cook module's cooks are read back from disk when nothing changed.

Also the load's root loop, which runs a root only if no earlier root's solve cooked it.
"""

from __future__ import annotations

import os
import sys
import time
import types

import pytest
from chunk_stubs import register_stub_module
from pxr import Sdf

from usd_chunks import Scene, chunk, chunk_registry, cook_cache


@pytest.fixture(autouse=True)
def _cache_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv(cook_cache.ENV_DIR, str(tmp_path / "cook_cache"))
    monkeypatch.delenv(cook_cache.ENV, raising=False)
    # Every stub cook is quick; the floor is for real modules.
    monkeypatch.setattr(cook_cache, "_MIN_COOK_S", 0.0)


def _cached_module(cooks: list, *, cache_key=None, extra=None):
    """A cache_cook stub: authors /Out with `size` as an attribute and reports in `readout`."""

    def main():
        c = chunk.Chunk("Cached")
        c.add_param(chunk.Slider("size", default_value=1.0))
        c.add_param(chunk.LineEdit("source", default_value=""))
        c.add_param(chunk.Label("readout", "not cooked", default_value="not cooked"))
        return c

    def execute(node):
        cooks.append(node.get_param("size").value)
        prim = node.get_stage().DefinePrim("/Out", "Xform")
        prim.CreateAttribute("size", Sdf.ValueTypeNames.Double).Set(node.get_param("size").value)
        node.get_param("readout").value = f"size {node.get_param('size').value}"
        if extra:
            extra(node)

    hooks = {"main": main, "execute": execute}
    if cache_key is not None:
        hooks["cache_key"] = cache_key
    module = register_stub_module(**hooks)
    module.config.cache_cook = True
    return module


def _add(scene, module):
    (c,) = module.load()
    scene.add_chunk(c)
    return c


def _entries():
    folder = cook_cache.cache_dir()
    return sorted(p.name for p in folder.glob("*.usdc")) if folder.is_dir() else []


def _out_size(scene):
    return scene.get_stage().GetPrimAtPath("/Out").GetAttribute("size").Get()


def test_a_cache_cook_module_gets_the_cache_result_toggle():
    with Scene():
        (c,) = _cached_module([]).load()
        (p,) = register_stub_module().load()

        assert c.get_param(cook_cache.TOGGLE).value is True
        assert p.get_param(cook_cache.TOGGLE) is None


def test_the_same_inputs_are_read_back_instead_of_cooked():
    cooks: list = []
    with Scene() as scene:
        c = _add(scene, _cached_module(cooks))
        assert len(cooks) == 1
        assert len(_entries()) == 1
        c.get_param("readout").value = "stale"

        assert c._cook() is True

        assert len(cooks) == 1
        assert _out_size(scene) == 1.0
        # What the cook wrote comes back with the layer.
        assert c.get_param("readout").value == "size 1.0"


def test_a_result_stored_outside_a_cook_is_read_back_by_the_next_one():
    """A bake from a button: stored under the entry taken when it started, and a later
    change to the params does not relabel it."""
    cooks: list = []
    with Scene() as scene:
        c = _add(scene, _cached_module(cooks, cache_key=lambda node: "baked"))
        c.get_param("size").value = 5.0
        entry = cook_cache.lookup(c)
        c.get_param("size").value = 6.0  # changed while the "bake" ran

        baked = Sdf.Layer.CreateAnonymous(".usda")
        prim = Sdf.CreatePrimInLayer(baked, "/Out")
        prim.specifier = Sdf.SpecifierDef
        Sdf.AttributeSpec(prim, "size", Sdf.ValueTypeNames.Double).default = 50.0
        c.get_param("readout").value = "baked"
        entry.store(c, baked)
        count = len(cooks)

        c.get_param("size").value = 5.0

        assert len(cooks) == count, "read back, not cooked"
        assert _out_size(scene) == 50.0
        assert c.get_param("readout").value == "baked"


def test_a_new_version_of_a_required_package_cooks_again(monkeypatch):
    """A new mujoco moves the physics with every param and every line of the chunk the same."""
    versions = {"numpy": "2.0.0"}
    monkeypatch.setattr(chunk_registry, "installed_version", versions.get)
    cooks: list = []
    with Scene() as scene:
        module = _cached_module(cooks)
        module.config.requirements = ["numpy>=2"]
        c = _add(scene, module)
        assert c._cook() is True
        assert len(cooks) == 1, "the same version reads back"

        versions["numpy"] = "2.1.0"
        cook_cache._PACKAGES.clear()  # a restart, as after a real upgrade
        c._cook()

        assert len(cooks) == 2


def test_a_package_with_no_install_record_counts_by_its_imported_version(monkeypatch):
    """The usdview bundles unpack mujoco into a plain folder: no metadata, only the module."""
    monkeypatch.setattr(chunk_registry, "installed_version", lambda name: None)
    fake = types.ModuleType("fake_physics")
    fake.__version__ = "3.8.1"
    monkeypatch.setitem(sys.modules, "fake_physics", fake)
    cooks: list = []
    with Scene() as scene:
        module = _cached_module(cooks)
        module.config.requirements = ["fake-physics>=3"]
        assert module.installed_packages() == {"fake-physics": "3.8.1"}
        c = _add(scene, module)
        assert len(cooks) == 1

        fake.__version__ = "3.10.0"
        cook_cache._PACKAGES.clear()  # a restart, as after a real upgrade
        c._cook()

        assert len(cooks) == 2


def test_a_package_added_to_the_requirements_mid_session_counts(monkeypatch):
    """Dan's review on #333: the same module object with new requirements is a new key."""
    monkeypatch.setattr(chunk_registry, "installed_version", {"numpy": "2.0.0", "scipy": "1.0"}.get)
    cooks: list = []
    with Scene() as scene:
        module = _cached_module(cooks)
        module.config.requirements = ["numpy>=2"]
        c = _add(scene, module)
        assert c._cook() is True
        assert len(cooks) == 1

        module.config.requirements = ["numpy>=2", "scipy"]
        c._cook()

        assert len(cooks) == 2


def test_a_changed_param_cooks_again():
    cooks: list = []
    with Scene() as scene:
        c = _add(scene, _cached_module(cooks))

        c.get_param("size").value = 2.0

        assert cooks[-1] == 2.0
        assert _out_size(scene) == 2.0


def test_going_back_to_earlier_inputs_reads_them_back():
    cooks: list = []
    with Scene() as scene:
        c = _add(scene, _cached_module(cooks))
        c.get_param("size").value = 2.0
        count = len(cooks)

        c.get_param("size").value = 1.0

        assert len(cooks) == count
        assert _out_size(scene) == 1.0


def test_unticking_cache_result_always_cooks():
    cooks: list = []
    with Scene() as scene:
        c = _add(scene, _cached_module(cooks))
        c.get_param(cook_cache.TOGGLE).value = False
        count = len(cooks)

        c._cook()
        c._cook()

        assert len(cooks) == count + 2


def test_the_environment_switches_it_off(monkeypatch):
    monkeypatch.setenv(cook_cache.ENV, "0")
    cooks: list = []
    with Scene() as scene:
        c = _add(scene, _cached_module(cooks))
        c._cook()

        assert len(cooks) == 2
        assert _entries() == []


def _always_module(cooks: list):
    module = _cached_module(cooks)
    module.config.cache_cook = False
    module.config.cache_always = True  # implies cache_cook
    return module


def test_cache_always_has_no_toggle_and_still_reads_back():
    cooks: list = []
    with Scene() as scene:
        c = _add(scene, _always_module(cooks))
        assert c.get_param(cook_cache.TOGGLE) is None

        c._cook()

        assert len(cooks) == 1
        assert len(_entries()) == 1


def test_cache_always_ignores_the_environment_switch(monkeypatch):
    monkeypatch.setenv(cook_cache.ENV, "0")
    cooks: list = []
    with Scene() as scene:
        c = _add(scene, _always_module(cooks))
        c._cook()

        assert len(cooks) == 1
        assert len(_entries()) == 1


def test_cache_always_stores_a_fast_cook(monkeypatch):
    """The floor that keeps quick cooks out is for a shortcut; here the entry is the result."""
    monkeypatch.setattr(cook_cache, "_MIN_COOK_S", 3600.0)
    cooks: list = []
    with Scene() as scene:
        _add(scene, _always_module(cooks))
        assert len(_entries()) == 1


def test_refresh_cooks_and_rewrites(monkeypatch):
    cooks: list = []
    with Scene() as scene:
        c = _add(scene, _cached_module(cooks))
        monkeypatch.setenv(cook_cache.ENV, "refresh")

        c._cook()

        assert len(cooks) == 2
        assert len(_entries()) == 1


def test_a_hook_input_that_changes_cooks_again():
    cooks: list = []
    token = ["a"]
    with Scene() as scene:
        c = _add(scene, _cached_module(cooks, cache_key=lambda node: token[0]))
        c._cook()
        assert len(cooks) == 1

        token[0] = "b"
        c._cook()

        assert len(cooks) == 2


def test_a_hook_that_cannot_say_cooks_and_stores_nothing():
    cooks: list = []
    with Scene() as scene:
        c = _add(scene, _cached_module(cooks, cache_key=lambda node: None))
        c._cook()

        assert len(cooks) == 2
        assert _entries() == []


def test_a_named_file_that_changes_cooks_again(tmp_path):
    source = tmp_path / "input.txt"
    source.write_text("one", encoding="utf-8")
    cooks: list = []
    with Scene() as scene:
        c = _add(scene, _cached_module(cooks))
        c.get_param("source").value = str(source)
        count = len(cooks)
        c._cook()
        assert len(cooks) == count

        source.write_text("two!", encoding="utf-8")
        c._cook()

        assert len(cooks) == count + 1


def test_a_cook_that_sets_its_own_input_is_not_stored():
    def bump(node):
        node.get_param("source").value = "set by the cook"

    with Scene() as scene:
        _add(scene, _cached_module([], extra=bump))

        assert _entries() == []


def test_hidden_state_a_cook_sets_comes_back_with_it():
    def hide(node):
        node.get_param("source").set_state(chunk.ChunkFlags.State.HIDDEN)

    cooks: list = []
    with Scene() as scene:
        c = _add(scene, _cached_module(cooks, extra=hide))
        c.get_param("source").unset_state(chunk.ChunkFlags.State.HIDDEN)

        c._cook()

        assert len(cooks) == 1
        assert c.get_param("source").has_state(chunk.ChunkFlags.State.HIDDEN)


def test_an_unreadable_entry_is_a_miss_and_is_dropped():
    cooks: list = []
    with Scene() as scene:
        c = _add(scene, _cached_module(cooks))
        (entry,) = cook_cache.cache_dir().glob("*.params")
        entry.write_bytes(b"not a pickle")

        c._cook()

        assert len(cooks) == 2


def test_the_size_cap_drops_the_least_recently_read(monkeypatch):
    cooks: list = []
    with Scene() as scene:
        c = _add(scene, _cached_module(cooks))
        (first,) = _entries()
        old = time.time() - 3600
        for path in cook_cache.cache_dir().glob(first.split(".")[0] + ".*"):
            os.utime(path, (old, old))
        # Room for one entry: the next store evicts the older.
        size = sum(p.stat().st_size for p in cook_cache.cache_dir().iterdir())
        monkeypatch.setenv(cook_cache.ENV_MAX_GB, str(size * 1.5 / 1024**3))

        c.get_param("size").value = 2.0

        assert first not in _entries()
        assert len(_entries()) == 1


def test_a_reopen_reads_back_what_the_first_open_cooked(tmp_path):
    cooks: list = []
    module = _cached_module(cooks)
    path = tmp_path / "net.usda"
    with Scene() as scene:
        _add(scene, module)
        scene.save(str(path))
    count = len(cooks)

    with Scene.open(str(path)) as scene:
        assert len(cooks) == count
        assert _out_size(scene) == 1.0


def test_a_load_cooks_a_chunk_fed_by_several_roots_once(tmp_path):
    cooks: dict = {}

    def counting(name):
        def main():
            return chunk.Chunk(name)

        def execute(node):
            cooks[node.name] = cooks.get(node.name, 0) + 1

        return register_stub_module(main=main, execute=execute)

    root_module, below_module = counting("Root"), counting("Below")
    path = tmp_path / "net.usda"
    with Scene() as scene:
        roots = [_add(scene, root_module) for _ in range(3)]
        below = _add(scene, below_module)
        for root in roots:
            below.add_dependency(root)
        scene.save(str(path))
    cooks.clear()

    with Scene.open(str(path)):
        pass

    assert cooks.get("Below") == 1
