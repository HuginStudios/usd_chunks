"""Which chunks made or changed a prim, and which prims a chunk made or changed.

Each chunk writes into a layer of its own, so a prim's spec stack already records every
chunk that touched it: nothing to index, nothing to keep in sync, and nothing done while no
one asks. A lookup reads one prim's stack (and its ancestors' when no chunk wrote the prim
itself), so it is cheap enough to run on every selection change, however big the scene.

Toolkit-free: frontends decide how to show the answer.
"""

from __future__ import annotations

from collections.abc import Iterable

import attrs
from pxr import Sdf, Usd

from usd_chunks import chunk_core as core
from usd_chunks.chunk_registry import ChunkLayerManager, ChunkRegistry


@attrs.frozen
class PrimTrace:
    """What one prim owes to which chunks.

    Attributes:
        path: The prim.
        made_by: The chunk that defined it (where it was introduced), if a chunk did.
        changed_by: Chunks that authored something on it, weakest first: in the order their
            changes apply, the last one winning a value they both set.
        via: For a prim no chunk wrote itself -- a mesh inside an asset a Reference chunk
            brought in -- the nearest ancestor a chunk wrote, and those chunks. None otherwise.
    """

    path: Sdf.Path
    made_by: core.ChunkBase | None = None
    changed_by: tuple[core.ChunkBase, ...] = ()
    via: tuple[Sdf.Path, tuple[core.ChunkBase, ...]] | None = None

    @property
    def stack(self) -> tuple[core.ChunkBase, ...]:
        """The chunks strongest first, as Usd.Prim.GetPrimStack orders specs: the last changer,
        down to the maker, then the chunks that brought it in."""
        found = list(reversed(self.changed_by)) + ([self.made_by] if self.made_by is not None else [])
        if self.via is not None:
            found += [c for c in self.via[1] if not any(c is f for f in found)]
        return tuple(found)

    @property
    def chunks(self) -> tuple[core.ChunkBase, ...]:
        """Every chunk the prim owes something to: maker first, then changers, then via."""
        found = ([self.made_by] if self.made_by is not None else []) + list(self.changed_by)
        if self.via is not None:
            found += [c for c in self.via[1] if not any(c is f for f in found)]
        return tuple(found)


def chunks_by_layer(scene=None) -> dict[str, list[core.ChunkBase]]:
    """Each chunk layer's identifier and the chunks that write into it.

    Several chunks can share one layer: a CONTEXT group hands its layer to its children.

    Args:
        scene: The scene; the registry's current one by default.

    Returns:
        Layer identifier -> chunks, empty when nothing is loaded.
    """
    manager = ChunkLayerManager()
    scene = scene or getattr(ChunkRegistry(), "scene", None)
    if scene is None or not getattr(manager, "initialized", False) or not manager.chunk_layers:
        return {}
    found: dict[str, list[core.ChunkBase]] = {}
    for chunk in scene.get_chunks():
        layer = manager.chunk_layers.get(chunk.get_hash())
        if layer is not None:
            found.setdefault(layer.identifier, []).append(chunk)
    return found


def is_meaningful_spec(spec: Sdf.PrimSpec) -> bool:
    """Whether this spec is the chunk touching the prim, or only scaffolding.

    Authoring on a prim makes empty `over` specs for every ancestor, as a path to hang off.
    Those claim nothing. A property, metadata (an `active` flag from a prune), or a `def`
    does.

    Args:
        spec: A prim spec.

    Returns:
        True when the spec authors something on its prim.
    """
    if spec.properties:
        return True
    if set(spec.ListInfoKeys()) - {"specifier"}:
        return True
    return spec.specifier != Sdf.SpecifierOver


def _owners(prim: Usd.Prim, by_layer: dict[str, list[core.ChunkBase]]):
    """(maker, changers weakest first) from prim's own spec stack."""
    maker = None
    changers: list[core.ChunkBase] = []
    defs = []
    for spec in prim.GetPrimStack():  # strongest first
        chunks = by_layer.get(spec.layer.identifier)
        if not chunks or not is_meaningful_spec(spec):
            continue
        if spec.specifier == Sdf.SpecifierDef:
            defs.append((spec, chunks))
        for chunk in chunks:
            if not any(chunk is c for c in changers):
                changers.append(chunk)
    if defs:
        # Introduced by the weakest def, as USD composes it; a stronger def that authors
        # nothing else is a chunk making sure the parent of its own prim exists.
        maker = defs[-1][1][0]
        for spec, chunks in defs[:-1]:
            if not spec.properties and not (set(spec.ListInfoKeys()) - {"specifier", "typeName"}):
                changers = [c for c in changers if not any(c is d for d in chunks)]
    changers = [c for c in changers if c is not maker]
    changers.reverse()
    return maker, tuple(changers)


def trace_prim(prim: Usd.Prim, by_layer: dict[str, list[core.ChunkBase]] | None = None) -> PrimTrace:
    """Which chunks made and changed prim.

    Args:
        prim: A scene prim (an instance proxy works too: its instance's chunks answer).
        by_layer: chunks_by_layer(), when tracing many prims at once.

    Returns:
        The trace. Empty when no chunk touched the prim or any of its ancestors.
    """
    by_layer = chunks_by_layer() if by_layer is None else by_layer
    path = prim.GetPath()
    if not by_layer or not prim.IsValid():
        return PrimTrace(path)
    maker, changers = _owners(prim, by_layer)
    if maker is not None or changers:
        return PrimTrace(path, maker, changers)
    parent = prim.GetParent()
    while parent and not parent.IsPseudoRoot():
        up_maker, up_changers = _owners(parent, by_layer)
        if up_maker is not None or up_changers:
            chunks = ((up_maker,) if up_maker is not None else ()) + up_changers
            return PrimTrace(path, via=(parent.GetPath(), chunks))
        parent = parent.GetParent()
    return PrimTrace(path)


def chunk_prims(chunk: core.ChunkBase, stage: Usd.Stage, limit: int = 2000) -> tuple[list[Sdf.Path], bool]:
    """The prims a chunk made or changed, read from its own layer, not the whole stage.

    What it made is given by the outermost prim (a House's /World/House, not every board);
    what it changed, by each prim it authored something on.

    Args:
        chunk: The chunk.
        stage: The scene's stage, to tell a prim the chunk made from a parent it only made
            sure exists.
        limit: The most prims to return.

    Returns:
        (paths, more): the prims, and whether there were more than limit.
    """
    layer = ChunkLayerManager().chunk_layers.get(chunk.get_hash())
    if layer is None:
        return [], False
    found: list[Sdf.Path] = []

    def visit(specs: Iterable[Sdf.PrimSpec]) -> bool:
        for spec in specs:
            if len(found) >= limit:
                return True
            if spec.path.HasPrefix(_CHUNKS_ROOT):
                continue
            if spec.specifier == Sdf.SpecifierDef and _made_here(spec, stage) and not _container(spec):
                found.append(spec.path)
                continue
            if spec.specifier != Sdf.SpecifierDef and is_meaningful_spec(spec):
                found.append(spec.path)
            if visit(spec.nameChildren):
                return True
        return False

    more = visit(layer.rootPrims)
    return found, more


_CHUNKS_ROOT = Sdf.Path("/chunks")


def _container(spec: Sdf.PrimSpec) -> bool:
    """A prim made only to hold others (a /World every chunk makes sure exists): selecting it
    would select far more than the chunk made, so its children answer instead."""
    return bool(spec.nameChildren) and not spec.properties and not (
        set(spec.ListInfoKeys()) - {"specifier", "typeName"})


def _made_here(spec: Sdf.PrimSpec, stage: Usd.Stage) -> bool:
    """Whether spec's layer is where its prim was introduced (the weakest def), and not a
    chunk making sure the parent of its own prim exists."""
    prim = stage.GetPrimAtPath(spec.path)
    if not prim:
        return False
    weakest = None
    for other in prim.GetPrimStack():
        if other.path == spec.path and other.specifier == Sdf.SpecifierDef:
            weakest = other.layer
    return weakest == spec.layer
