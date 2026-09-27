"""Tests for the store-provider plugin seam (usd_chunks/store/providers.py).

These exercise the runtime side of the seam with in-process fake providers --
no marketplace, no entry points, no network. The point of the seam is that
usd_chunks depends only on the StoreProvider *shape*, never on a concrete store.
"""
from __future__ import annotations

from pathlib import Path

from usd_chunks.store import providers as stores
from usd_chunks.store.providers import StoreProvider, resolve_across_stores


class FakeStore:
    """A structural StoreProvider: no base class, just the right methods."""

    def __init__(self, name: str, catalog: dict[str, list[str]]):
        self.name = name
        self.catalog = catalog  # chunk_type -> available versions
        self.provided: list[tuple[str, str]] = []

    def select(self, chunk_type, spec):
        versions = self.catalog.get(chunk_type)
        if not versions:
            return None
        if spec is None or spec == "*":
            return versions[-1]
        return spec if spec in versions else None

    def provide(self, chunk_type, version):
        self.provided.append((chunk_type, version))
        return Path(f"/store/{chunk_type}/{version}/{chunk_type}.py")


def test_fake_store_is_structural_provider():
    # Duck typing: a plain class satisfies the runtime-checkable Protocol.
    assert isinstance(FakeStore("x", {}), StoreProvider)


def test_resolve_first_store_wins():
    a = FakeStore("a", {"prim-chunk": ["1.0.0", "1.2.0"]})
    b = FakeStore("b", {"prim-chunk": ["9.9.9"]})
    hit = resolve_across_stores("prim-chunk", None, stores=[a, b])
    assert hit is not None
    store, version = hit
    assert store is a and version == "1.2.0"  # a is asked first and has it


def test_resolve_falls_through_to_next_store():
    a = FakeStore("a", {"other": ["1.0.0"]})
    b = FakeStore("b", {"prim-chunk": ["3.1.0"]})
    hit = resolve_across_stores("prim-chunk", None, stores=[a, b])
    assert hit is not None
    store, version = hit
    assert store is b and version == "3.1.0"


def test_resolve_spec_is_honored():
    a = FakeStore("a", {"prim-chunk": ["1.0.0", "1.2.0", "2.0.0"]})
    hit = resolve_across_stores("prim-chunk", "1.2.0", stores=[a])
    assert hit == (a, "1.2.0")
    # A spec no store satisfies resolves to nothing.
    assert resolve_across_stores("prim-chunk", "5.0.0", stores=[a]) is None


def test_resolve_no_store_has_it():
    a = FakeStore("a", {"other": ["1.0.0"]})
    assert resolve_across_stores("prim-chunk", None, stores=[a]) is None


def test_discovery_skips_broken_providers(monkeypatch):
    class Boom:
        def load(self):
            raise RuntimeError("misconfigured store")

    good_ep = _StubEntryPoint(lambda: FakeStore("good", {}))
    monkeypatch.setattr(stores, "_iter_entry_points", lambda group: [Boom(), good_ep])
    found = stores.discovered_stores()
    assert [s.name for s in found] == ["good"]  # broken one skipped, not fatal


class _StubEntryPoint:
    def __init__(self, factory):
        self._factory = factory

    def load(self):
        return self._factory
