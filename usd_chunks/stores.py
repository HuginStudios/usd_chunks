"""Store-provider plugin seam.

usd_chunks stays independent of any particular marketplace. A *store* is
anything that can turn a chunkType plus a version specifier into a concrete file
on disk. A marketplace (galder, or anyone else) ships a provider and advertises
it under the ``usd_chunks.stores`` entry-point group; usd_chunks discovers it
without ever importing the marketplace.

Providers are duck-typed against :class:`StoreProvider` -- a runtime-checkable
Protocol, so no shared base class crosses the repo boundary. A provider deals
only in name / version / file: it knows nothing about USD, prims, or the pin
lock. That knowledge lives in the consumer (``usdchunks pin``), which uses
:func:`resolve_across_stores` to pick a concrete version for each chunk and
:meth:`StoreProvider.provide` to fetch it.
"""

from __future__ import annotations

from pathlib import Path
from collections.abc import Iterable
from typing import Protocol, runtime_checkable

try:  # py3.8+ stdlib; importlib_metadata is the backport
    from importlib.metadata import EntryPoint, entry_points
except ImportError:  # pragma: no cover
    from importlib_metadata import EntryPoint, entry_points  # type: ignore

#: Entry-point group a marketplace registers its provider under.
STORE_ENTRY_POINT_GROUP = "usd_chunks.stores"


@runtime_checkable
class StoreProvider(Protocol):
    """What a marketplace exposes to the runtime.

    A provider is structural -- anything with a ``name`` and matching
    ``select``/``provide`` satisfies it; there is no base class to inherit.
    ``versions`` is optional (used for status/listing) and callers must probe
    for it with ``hasattr``.
    """

    name: str

    def select(self, chunk_type: str, spec: str | None) -> str | None:
        """Concrete version satisfying ``spec`` (this becomes the lock), or
        ``None`` if this store does not carry ``chunk_type``.

        The store owns its own version-matching semantics, so no shared semver
        code is required anywhere. ``spec`` is the author's specifier from
        ``info:chunkVersion`` (exact, trailing wildcard, or ``None`` for latest).
        """
        ...

    def provide(self, chunk_type: str, version: str) -> Path | None:
        """Ensure that exact ``version`` is present locally (fetch/download if
        needed) and return the file to link, or ``None`` if it cannot be
        provided (unknown chunk/version, or the store is unreachable).
        """
        ...


def _iter_entry_points(group: str) -> Iterable[EntryPoint]:
    """Entry points in ``group``, across importlib.metadata API versions."""
    eps = entry_points()
    if hasattr(eps, "select"):  # py3.10+
        return eps.select(group=group)
    return eps.get(group, [])  # type: ignore[union-attr]  # py3.9 dict-like


def discovered_stores() -> list[StoreProvider]:
    """Every registered store provider, in entry-point order.

    Each entry point loads to a zero-argument factory (typically the provider
    class itself) that returns a provider. A store that fails to load or
    construct is skipped, never fatal -- a broken or unconfigured marketplace
    must not break the runtime.
    """
    stores: list[StoreProvider] = []
    for ep in _iter_entry_points(STORE_ENTRY_POINT_GROUP):
        try:
            factory = ep.load()
            provider = factory()
        except Exception:
            continue
        stores.append(provider)
    return stores


def resolve_across_stores(
    chunk_type: str,
    spec: str | None,
    stores: Iterable[StoreProvider] | None = None,
) -> tuple[StoreProvider, str] | None:
    """Ask each store in order for a version satisfying ``spec``; first hit wins.

    Returns ``(store, version)`` -- the store to fetch from and the concrete
    version that becomes the lock -- or ``None`` if no store carries the chunk.
    This is the store-agnostic resolution ``usdchunks pin`` runs; anything
    marketplace-specific stays inside the provider. Pass ``stores`` to resolve
    against a fixed set (e.g. in tests); otherwise the registered ones are used.
    """
    for store in (discovered_stores() if stores is None else stores):
        version = store.select(chunk_type, spec)
        if version is not None:
            return store, version
    return None
