"""Tests for `usdchunks pin` (usd_chunks/store/pin.py).

The resolve/materialize/lock logic is pure (no pxr) and tested with in-process
fake stores + real temp files. The end-to-end pin() over a composed stage runs
only when pxr is importable.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from usd_chunks.store import pin as pinmod
from usd_chunks.store.pin import (
    PinEntry,
    materialize,
    read_lock,
    resolve_pins,
    write_lock,
)


class FakeStore:
    """Structural StoreProvider backed by on-disk fixture files."""

    def __init__(self, name: str, catalog: dict[str, dict[str, Path]]):
        self.name = name
        self.catalog = catalog  # chunk_type -> {version: path}

    def select(self, chunk_type, spec):
        vers = self.catalog.get(chunk_type)
        if not vers:
            return None
        if spec is None or spec == "*":
            return sorted(vers)[-1]
        return spec if spec in vers else None

    def provide(self, chunk_type, version):
        return self.catalog.get(chunk_type, {}).get(version)


def _impl(tmp: Path, name: str, version: str) -> Path:
    d = tmp / "store" / name / version
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{name}.py"
    f.write_text(f"# {name} {version}\n", encoding="utf-8")
    return f


# --- resolve_pins -------------------------------------------------------------

def test_resolve_fresh_picks_latest_and_provides(tmp_path):
    store = FakeStore("galder", {"prim-chunk": {
        "1.0.0": _impl(tmp_path, "prim-chunk", "1.0.0"),
        "1.2.0": _impl(tmp_path, "prim-chunk", "1.2.0"),
    }})
    entries, problems = resolve_pins({"prim-chunk": None}, {}, stores=[store])
    assert problems == []
    assert len(entries) == 1
    e = entries[0]
    assert (e.chunk_type, e.store, e.version) == ("prim-chunk", "galder", "1.2.0")
    assert e.source is not None and e.source.exists()


def test_resolve_restore_reuses_recorded_lock(tmp_path):
    # Two versions available, but the lock pins 1.0.0 -> restore must reuse it,
    # not re-resolve to the latest.
    store = FakeStore("galder", {"prim-chunk": {
        "1.0.0": _impl(tmp_path, "prim-chunk", "1.0.0"),
        "9.9.9": _impl(tmp_path, "prim-chunk", "9.9.9"),
    }})
    entries, problems = resolve_pins(
        {"prim-chunk": None}, {"prim-chunk": "galder:1.0.0"}, stores=[store]
    )
    assert problems == []
    assert (entries[0].store, entries[0].version) == ("galder", "1.0.0")


def test_resolve_first_store_wins(tmp_path):
    a = FakeStore("a", {"prim-chunk": {"1.0.0": _impl(tmp_path, "prim-chunk", "1.0.0")}})
    b = FakeStore("b", {"prim-chunk": {"2.0.0": _impl(tmp_path, "prim-chunk", "2.0.0")}})
    entries, _ = resolve_pins({"prim-chunk": None}, {}, stores=[a, b])
    assert (entries[0].store, entries[0].version) == ("a", "1.0.0")


def test_resolve_no_store_is_a_problem(tmp_path):
    a = FakeStore("a", {"other": {"1.0.0": _impl(tmp_path, "other", "1.0.0")}})
    entries, problems = resolve_pins({"prim-chunk": "1.2.*"}, {}, stores=[a])
    assert entries == []
    assert problems and "prim-chunk" in problems[0]


def test_resolve_restore_missing_store_yields_sourceless_entry(tmp_path):
    # The store named in the lock isn't installed (private handoff). The entry
    # still exists with source=None; materialize decides if it's bundled.
    entries, problems = resolve_pins(
        {"prim-chunk": None}, {"prim-chunk": "private:1.0.0"}, stores=[]
    )
    assert problems == []
    assert entries[0].store == "private" and entries[0].source is None


# --- materialize --------------------------------------------------------------

def test_materialize_places_impls(tmp_path):
    src = _impl(tmp_path, "prim-chunk", "1.2.0")
    entries = [PinEntry("prim-chunk", "galder", "1.2.0", src)]
    out, problems = materialize(entries, tmp_path / "pin", copy=True)
    assert problems == []
    assert len(out) == 1
    m = out[0]
    assert m.path == tmp_path / "pin" / "prim-chunk.py"
    assert m.path.read_text(encoding="utf-8") == src.read_text(encoding="utf-8")
    assert m.mode == "copy"


def test_materialize_bundled_fallback_when_no_source(tmp_path):
    pin_dir = tmp_path / "pin"
    pin_dir.mkdir()
    (pin_dir / "prim-chunk.py").write_text("# bundled\n", encoding="utf-8")
    entries = [PinEntry("prim-chunk", "private", "1.0.0", None)]
    out, problems = materialize(entries, pin_dir, copy=True)
    assert problems == []
    assert out[0].mode == "bundled"


def test_materialize_missing_and_unbundled_is_a_problem(tmp_path):
    entries = [PinEntry("prim-chunk", "private", "1.0.0", None)]
    out, problems = materialize(entries, tmp_path / "pin", copy=True)
    assert out == []
    assert problems and "not bundled" in problems[0]


# --- lock round-trip (fake layer) --------------------------------------------

class FakeLayer:
    """Enough of Sdf.Layer for read_lock/write_lock: a customLayerData dict."""

    def __init__(self, data=None):
        self.customLayerData = dict(data or {})


def test_lock_round_trip():
    layer = FakeLayer()
    assert read_lock(layer) == {}
    write_lock(layer, {"prim-chunk": "galder:1.2.0", "var-chunk": "fab:3.1.0"})
    assert read_lock(layer) == {"prim-chunk": "galder:1.2.0", "var-chunk": "fab:3.1.0"}


def test_write_lock_preserves_other_custom_data():
    layer = FakeLayer({"comment": "hi"})
    write_lock(layer, {"prim-chunk": "galder:1.0.0"})
    assert layer.customLayerData["comment"] == "hi"
    assert pinmod.PINS_KEY in layer.customLayerData


# --- end-to-end (requires pxr) -----------------------------------------------

pxr = pytest.importorskip("pxr")


def _write_scene(path: Path) -> None:
    from pxr import Usd

    stage = Usd.Stage.CreateNew(str(path))
    for name, ct in [("Prim1", "prim-chunk"), ("Grp", "chunkgroup")]:
        prim = stage.DefinePrim(f"/{name}", "Chunk")
        prim.CreateAttribute("info:chunkType", pxr.Sdf.ValueTypeNames.Token).Set(ct)
    stage.GetRootLayer().Save()


def test_pin_end_to_end_writes_folder_and_lock(tmp_path):
    scene = tmp_path / "shot.usda"
    _write_scene(scene)
    store = FakeStore("galder", {"prim-chunk": {"1.2.0": _impl(tmp_path, "prim-chunk", "1.2.0")}})

    result = pinmod.pin(scene, pin_dir=tmp_path / "pin", copy=True, stores=[store])

    assert result.ok, result.problems
    # the chunkgroup sentinel prim is skipped; only prim-chunk is pinned
    assert [m.chunk_type for m in result.materialized] == ["prim-chunk"]
    assert (tmp_path / "pin" / "prim-chunk.py").exists()
    assert result.lock_mode == "in-place"

    # lock landed in the entry layer's customLayerData and reads back
    from pxr import Sdf

    layer = Sdf.Layer.FindOrOpen(str(scene))
    assert read_lock(layer) == {"prim-chunk": "galder:1.2.0"}


def test_read_scene_lock_reports_recorded_pins(tmp_path):
    scene = tmp_path / "shot.usda"
    _write_scene(scene)
    store = FakeStore("galder", {"prim-chunk": {"1.2.0": _impl(tmp_path, "prim-chunk", "1.2.0")}})
    # Nothing recorded yet.
    assert pinmod.read_scene_lock(scene) == {}
    pinmod.pin(scene, pin_dir=tmp_path / "pin", copy=True, stores=[store])
    # After pinning, --list reads it back without resolving anything.
    assert pinmod.read_scene_lock(scene) == {"prim-chunk": "galder:1.2.0"}


def test_pin_is_idempotent_restore(tmp_path):
    scene = tmp_path / "shot.usda"
    _write_scene(scene)
    store = FakeStore("galder", {"prim-chunk": {
        "1.2.0": _impl(tmp_path, "prim-chunk", "1.2.0"),
        "2.0.0": _impl(tmp_path, "prim-chunk", "2.0.0"),
    }})
    # First pin resolves to latest (2.0.0)...
    first = pinmod.pin(scene, pin_dir=tmp_path / "pin", copy=True, stores=[store])
    assert first.lock() == {"prim-chunk": "galder:2.0.0"}
    # ...second pin sees the recorded lock and restores the same version.
    second = pinmod.pin(scene, pin_dir=tmp_path / "pin2", copy=True, stores=[store])
    assert second.lock() == {"prim-chunk": "galder:2.0.0"}


class TestPackageLock:
    """The impl pins say which chunk code produced a scene; these say what it stood on.

    A pin without them reproduces the recipe and not the result -- mujoco is the case that
    matters, where a solver change moves the physics with every chunk byte-identical.
    """

    def test_versions_round_trip_through_custom_layer_data(self):
        from pxr import Sdf

        layer = Sdf.Layer.CreateAnonymous()
        pinmod.write_packages(layer, {"numpy": "2.4.2", "mujoco": "3.12.0"})
        assert pinmod.read_packages(layer) == {"numpy": "2.4.2", "mujoco": "3.12.0"}

    def test_they_sit_beside_the_impl_pins_rather_than_inside_them(self):
        """read_lock's contract is a flat {chunkType: 'store:version'}; widening it would
        break every reader to save one key."""
        from pxr import Sdf

        layer = Sdf.Layer.CreateAnonymous()
        pinmod.write_lock(layer, {"transform": "store:1.0.0"})
        pinmod.write_packages(layer, {"numpy": "2.4.2"})

        assert pinmod.read_lock(layer) == {"transform": "store:1.0.0"}
        assert pinmod.read_packages(layer) == {"numpy": "2.4.2"}

    def test_nothing_recorded_reads_as_empty(self):
        from pxr import Sdf

        assert pinmod.read_packages(Sdf.Layer.CreateAnonymous()) == {}

    def test_an_empty_lock_writes_no_key(self):
        """A scene whose chunks need nothing should not carry an empty dict implying it
        was checked and found bare."""
        from pxr import Sdf

        layer = Sdf.Layer.CreateAnonymous()
        pinmod.write_packages(layer, {})
        assert pinmod.PACKAGES_KEY not in (layer.customLayerData or {})

    def test_it_records_the_installed_version_not_the_declared_range(self):
        """rbd_solver declares mujoco>=3.1,<4. What belongs in a lock is what ran."""
        from usd_chunks import chunk_registry
        from usd_chunks.chunk_core import SceneSender

        class _NoScene(SceneSender):
            def get_stage(self):
                return None

            def get_selected_prims(self):
                return []

            def should_install_package(self, package):
                return False

        registry = chunk_registry.ChunkRegistry()
        registry.initialize(_NoScene(), "pytest")
        packages = pinmod.collect_packages(["rbd_solver"])
        if "numpy" not in packages:
            pytest.skip("rbd_solver not registered, or numpy absent")
        assert not any(c in packages["numpy"] for c in "<>=,"), packages
        assert packages["numpy"] == chunk_registry.installed_version("numpy")

    def test_an_unregistered_chunk_type_contributes_nothing(self):
        assert pinmod.collect_packages(["definitely-not-a-chunk"]) == {}


class TestProvisionVenv:
    """The npm-ci half: a recorded lock is only worth something if something applies it."""

    def test_an_empty_lock_creates_nothing(self, tmp_path):
        """A scene whose chunks need no packages needs no environment."""
        target = tmp_path / "env"
        python, installed, problems = pinmod.provision_venv({}, target)
        assert (python, installed, problems) == (None, [], [])
        assert not target.exists()

    def test_the_interpreter_path_is_platform_correct(self, tmp_path):
        import os

        expected = "Scripts" if os.name == "nt" else "bin"
        assert pinmod.venv_python(tmp_path).parent.name == expected

    @pytest.mark.slow
    def test_it_installs_the_exact_locked_versions(self, tmp_path):
        """Exact, not the declared range -- resolving ranges again would let versions drift,
        which is the thing the lock exists to stop."""
        import subprocess

        target = tmp_path / "env"
        python, installed, problems = pinmod.provision_venv({"packaging": "24.0"}, target)
        assert not problems, problems
        assert installed == ["packaging==24.0"]

        out = subprocess.run(
            [str(python), "-c", "import packaging; print(packaging.__version__)"],
            capture_output=True, text=True,
        )
        assert out.stdout.strip() == "24.0", out.stdout + out.stderr

    @pytest.mark.slow
    def test_the_host_stack_stays_visible(self, tmp_path):
        """system_site_packages is not a convenience: pxr comes from a bundle pip cannot
        install, so a fully isolated venv is one where every chunk fails to import USD."""
        import subprocess

        target = tmp_path / "env"
        python, _, problems = pinmod.provision_venv({"packaging": "24.0"}, target)
        assert not problems, problems

        out = subprocess.run(
            [str(python), "-c", "import pxr; print('ok')"], capture_output=True, text=True
        )
        assert out.stdout.strip() == "ok", out.stdout + out.stderr


class TestIntegrityHashes:
    """A version says which impl; a hash says whether it is still those bytes.

    The gap a version alone leaves: edit a pinned chunk by hand and it is still 1.2.0,
    producing different results with an identical lock.
    """

    def test_a_hash_round_trips(self):
        from pxr import Sdf

        layer = Sdf.Layer.CreateAnonymous()
        pinmod.write_hashes(layer, {"transform": "sha256:abc"})
        assert pinmod.read_hashes(layer) == {"transform": "sha256:abc"}

    def test_it_sits_beside_the_pins_and_packages(self):
        from pxr import Sdf

        layer = Sdf.Layer.CreateAnonymous()
        pinmod.write_lock(layer, {"transform": "store:1.0.0"})
        pinmod.write_packages(layer, {"numpy": "2.4.2"})
        pinmod.write_hashes(layer, {"transform": "sha256:abc"})

        assert pinmod.read_lock(layer) == {"transform": "store:1.0.0"}
        assert pinmod.read_packages(layer) == {"numpy": "2.4.2"}
        assert pinmod.read_hashes(layer) == {"transform": "sha256:abc"}

    def test_hashing_reads_whole_bytes(self, tmp_path):
        """Not the whitespace-stripped main() digest ChunkModule caches: that one ignores
        formatting on purpose, and integrity has to notice everything."""
        a = tmp_path / "a.py"
        b = tmp_path / "b.py"
        a.write_text("def main():\n    return 1\n", encoding="utf-8")
        b.write_text("def main():\n        return 1\n", encoding="utf-8")
        assert pinmod.hash_file(a) != pinmod.hash_file(b)

    def test_an_unreadable_file_hashes_to_none(self, tmp_path):
        assert pinmod.hash_file(tmp_path / "nope.py") is None

    def test_an_edited_impl_is_caught_though_the_version_did_not_move(self, tmp_path):
        """The whole point. Same chunk type, same version, different bytes."""
        impl = tmp_path / "transform.py"
        impl.write_text("def main():\n    return 1\n", encoding="utf-8")
        entry = pinmod.MaterializedPin("transform", "store", "1.2.0", impl, "copy")
        recorded = pinmod.collect_hashes([entry])

        impl.write_text("def main():\n    return 2\n", encoding="utf-8")

        problems = pinmod.verify_hashes([entry], recorded)
        assert len(problems) == 1
        assert "transform==1.2.0" in problems[0]
        assert "content changed since it was pinned" in problems[0]

    def test_an_untouched_impl_verifies(self, tmp_path):
        impl = tmp_path / "transform.py"
        impl.write_text("def main():\n    return 1\n", encoding="utf-8")
        entry = pinmod.MaterializedPin("transform", "store", "1.2.0", impl, "copy")
        assert pinmod.verify_hashes([entry], pinmod.collect_hashes([entry])) == []

    def test_a_lock_predating_hashes_still_restores(self, tmp_path):
        """Refusing to restore an older lock would be worse than the gap it closes."""
        impl = tmp_path / "transform.py"
        impl.write_text("def main():\n    return 1\n", encoding="utf-8")
        entry = pinmod.MaterializedPin("transform", "store", "1.2.0", impl, "copy")
        assert pinmod.verify_hashes([entry], {}) == []
