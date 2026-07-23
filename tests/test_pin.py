"""Tests for `usdchunks pin` (usd_chunks/pin.py).

The resolve/materialize/lock logic is pure (no pxr) and tested with in-process
fake stores + real temp files. The end-to-end pin() over a composed stage runs
only when pxr is importable.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from usd_chunks import pin as pinmod
from usd_chunks.pin import (
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
