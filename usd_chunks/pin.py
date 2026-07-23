"""``usdchunks pin`` -- freeze a chunk network to exact store versions.

Mirrors Pixar's ``mdl pin``: resolve every Chunk in a composed stage to a
concrete version from a registered store, materialize those exact impls into a
flat ``pin/`` folder (which you prepend to ``USD_CHUNK_DIRS`` so the shot runs
against exactly them), and record the lock in the entry layer's
``customLayerData``. Read-only on source *prims* -- a shot composes thousands of
layers, often immutable -- so nothing is stamped per prim; the lock is a single
layer-level write (or a thin wrapper layer if the entry layer is read-only).

Idempotent, like ``npm ci``: re-pinning a scene that already carries a
``usdchunks:pins`` lock reuses exactly those versions instead of re-resolving.
That is the vendor-handoff / restore path -- hand someone a pinned ``.usda`` and
``usdchunks pin`` on their side re-provides the same versions from whatever
store carries them (or from a ``--copy`` bundle that travelled with the file).

The store-facing work goes through the plugin seam (usd_chunks.stores); this
module never imports any marketplace. pxr is imported lazily so the pure
resolve/materialize logic stays importable (and testable) without USD.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from usd_chunks.stores import StoreProvider, discovered_stores, resolve_across_stores

#: Layer-level key holding the flat lock: {chunkType: "store:version"}.
PINS_KEY = "usdchunks:pins"

#: A prim with info:chunkType == this is a chunkgroup *container*, not a chunk
#: that resolves to a store impl -- skip it (matches usdchunks_common).
CHUNKGROUP_SENTINEL = "chunkgroup"


class PinError(RuntimeError):
    pass


@dataclass
class PinEntry:
    """A chunk resolved to a store + version, before materialization."""

    chunk_type: str
    store: str
    version: str
    source: Optional[Path]  # store impl path; None if the store couldn't provide


@dataclass
class MaterializedPin:
    """A pinned chunk present in the pin folder."""

    chunk_type: str
    store: str
    version: str
    path: Path
    mode: str  # "symlink" | "copy" | "bundled"


@dataclass
class PinResult:
    scene: Path
    pin_dir: Path
    materialized: List[MaterializedPin] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    lock_location: Optional[Path] = None
    lock_mode: Optional[str] = None  # "in-place" | "wrapper"

    @property
    def ok(self) -> bool:
        return not self.problems

    def lock(self) -> Dict[str, str]:
        return {m.chunk_type: f"{m.store}:{m.version}" for m in self.materialized}


# --- resolution (pure: no pxr, no filesystem writes) -------------------------

def resolve_pins(
    specs: Dict[str, Optional[str]],
    existing: Dict[str, str],
    stores: Optional[Iterable[StoreProvider]] = None,
) -> Tuple[List[PinEntry], List[str]]:
    """Turn {chunkType: spec} into resolved (store, version, source) entries.

    A chunkType already in ``existing`` (the recorded lock) is *reused* -- its
    exact store:version is re-provided, not re-resolved (restore semantics).
    Anything else is resolved across the registered stores by spec, first hit
    wins. Returns (entries, problems); a chunk no store carries is a problem.
    """
    stores = list(discovered_stores() if stores is None else stores)
    by_name = {s.name: s for s in stores}
    entries: List[PinEntry] = []
    problems: List[str] = []

    for chunk_type in sorted(specs):
        spec = specs[chunk_type]
        if chunk_type in existing:
            store_name, _, version = existing[chunk_type].partition(":")
            if not version:
                problems.append(f"{chunk_type}: malformed lock entry {existing[chunk_type]!r}")
                continue
            store = by_name.get(store_name)
            source = store.provide(chunk_type, version) if store is not None else None
            entries.append(PinEntry(chunk_type, store_name, version, source))
        else:
            hit = resolve_across_stores(chunk_type, spec, stores=stores)
            if hit is None:
                problems.append(
                    f"{chunk_type}: no registered store carries it (spec {spec or '*'})"
                )
                continue
            store, version = hit
            source = store.provide(chunk_type, version)
            if source is None:
                problems.append(
                    f"{chunk_type}=={version}: store {store.name!r} selected it but could not provide it"
                )
                continue
            entries.append(PinEntry(chunk_type, store.name, version, source))

    return entries, problems


# --- materialization ----------------------------------------------------------

def _find_in_dir(pin_dir: Path, chunk_type: str) -> Optional[Path]:
    for p in pin_dir.glob(f"{chunk_type}.*"):
        if p.is_file():
            return p
    return None


def _link_or_copy(src: Path, dst: Path, copy: bool) -> str:
    """Place src at dst as a symlink (default) or a copy. Falls back to copy if
    the platform refuses symlinks (Windows without developer mode/admin)."""
    if dst.is_symlink() or dst.exists():
        dst.unlink()
    if not copy:
        try:
            os.symlink(src, dst)
            return "symlink"
        except (OSError, NotImplementedError):
            pass  # e.g. Windows without symlink privilege -> copy
    shutil.copyfile(src, dst)
    return "copy"


def materialize(
    entries: List[PinEntry], pin_dir: Path, copy: bool = False
) -> Tuple[List[MaterializedPin], List[str]]:
    """Lay resolved entries into the flat pin folder as ``<chunkType><ext>``.

    An entry whose store couldn't provide a source is only OK if the impl is
    already present in the pin folder (a ``--copy`` bundle that travelled with
    the scene); otherwise it is a problem.
    """
    pin_dir.mkdir(parents=True, exist_ok=True)
    out: List[MaterializedPin] = []
    problems: List[str] = []
    for e in entries:
        if e.source is not None:
            ext = e.source.suffix or ".py"
            dst = pin_dir / f"{e.chunk_type}{ext}"
            mode = _link_or_copy(e.source, dst, copy)
            out.append(MaterializedPin(e.chunk_type, e.store, e.version, dst, mode))
        else:
            bundled = _find_in_dir(pin_dir, e.chunk_type)
            if bundled is None:
                problems.append(
                    f"{e.chunk_type}=={e.version}: store {e.store!r} unavailable "
                    f"and not bundled in {pin_dir}"
                )
                continue
            out.append(MaterializedPin(e.chunk_type, e.store, e.version, bundled, "bundled"))
    return out, problems


# --- USD I/O (lazy pxr) -------------------------------------------------------

def collect_specs(stage) -> Dict[str, Optional[str]]:
    """{chunkType: info:chunkVersion spec} for every Chunk prim in the composed
    stage (the container 'chunkgroup' sentinel excluded). Read-only.

    Multiple prims of the same chunkType must agree on a spec; a genuine
    conflict (two different non-None specs) raises PinError.
    """
    specs: Dict[str, Optional[str]] = {}
    conflicts: Dict[str, set] = {}
    for prim in stage.Traverse():
        if prim.GetTypeName() != "Chunk":
            continue
        ct_attr = prim.GetAttribute("info:chunkType")
        ct = ct_attr.Get() if ct_attr else None
        if not ct or str(ct) == CHUNKGROUP_SENTINEL:
            continue
        ct = str(ct)
        v_attr = prim.GetAttribute("info:chunkVersion")
        raw = v_attr.Get() if (v_attr and v_attr.HasValue()) else None
        spec = str(raw) if raw else None
        prev = specs.get(ct, "___unset___")
        if prev == "___unset___":
            specs[ct] = spec
        elif spec is not None and prev is not None and spec != prev:
            conflicts.setdefault(ct, {prev}).add(spec)
        elif specs[ct] is None:
            specs[ct] = spec
    if conflicts:
        raise PinError(
            "conflicting version specs -- "
            + "; ".join(f"{k}: {sorted(v)}" for k, v in sorted(conflicts.items()))
        )
    return specs


def read_lock(layer) -> Dict[str, str]:
    """The recorded {chunkType: 'store:version'} lock on a layer, or {}."""
    cld = layer.customLayerData
    pins = cld.get(PINS_KEY) if cld else None
    if not pins:
        return {}
    return {str(k): str(v) for k, v in dict(pins).items()}


def read_scene_lock(scene_path) -> Dict[str, str]:
    """The recorded {chunkType: 'store:version'} lock on a scene's root layer.

    Read-only and cheap: opens just the layer (no stage composition), so `pin
    --list` can report what a shot is frozen to without touching any store.
    """
    from pxr import Sdf

    scene_path = Path(scene_path)
    layer = Sdf.Layer.FindOrOpen(str(scene_path))
    if layer is None:
        raise PinError(f"failed to open {scene_path}")
    return read_lock(layer)


def write_lock(layer, lock: Dict[str, str]) -> None:
    """Set the flat pin lock on a layer's customLayerData (one write, layer
    level, ignored by composition). Caller Saves."""
    cld = dict(layer.customLayerData) if layer.customLayerData else {}
    cld[PINS_KEY] = dict(lock)
    layer.customLayerData = cld


def _write_wrapper(scene_path: Path, lock: Dict[str, str], pin_dir: Path):
    """A thin layer that sublayers the (immutable) scene and carries the lock,
    written into the pin folder so the source is never touched. Render this."""
    from pxr import Sdf

    pin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = pin_dir / f"{scene_path.stem}.pinned.usda"
    wl = Sdf.Layer.CreateNew(str(wrapper))
    wl.subLayerPaths.append(str(scene_path.resolve()))
    write_lock(wl, lock)
    wl.Save()
    return wrapper


def _record_lock(scene_path: Path, entry_layer, lock: Dict[str, str], pin_dir: Path, wrapper: bool):
    """Write the lock onto the entry layer in place, or into a wrapper layer if
    the entry layer is read-only (or --wrapper was forced). Returns (path, mode)."""
    if not wrapper:
        try:
            write_lock(entry_layer, lock)
            entry_layer.Save()
            return Path(scene_path), "in-place"
        except Exception:
            pass  # read-only entry layer -> fall through to a wrapper
    return _write_wrapper(scene_path, lock, pin_dir), "wrapper"


# --- orchestration ------------------------------------------------------------

def pin(
    scene_path,
    pin_dir=None,
    copy: bool = False,
    wrapper: bool = False,
    stores: Optional[Iterable[StoreProvider]] = None,
) -> PinResult:
    """Pin (or restore) a chunk network. See module docstring.

    Opens the stage read-only, resolves each chunk (reusing any recorded lock),
    materializes the exact impls into ``pin_dir`` (default ``./pin``), and -- only
    if everything resolved -- records the lock. A partial resolve writes nothing
    and surfaces the problems, so a pin is all-or-nothing (npm-ci strict).
    """
    from pxr import Usd

    scene_path = Path(scene_path)
    pin_dir = Path(pin_dir) if pin_dir else Path.cwd() / "pin"

    stage = Usd.Stage.Open(str(scene_path))
    if not stage:
        raise PinError(f"failed to open {scene_path}")
    entry_layer = stage.GetRootLayer()

    existing = read_lock(entry_layer)
    specs = collect_specs(stage)

    entries, resolve_problems = resolve_pins(specs, existing, stores=stores)
    materialized, mat_problems = materialize(entries, pin_dir, copy=copy)

    result = PinResult(
        scene=scene_path,
        pin_dir=pin_dir,
        materialized=materialized,
        problems=resolve_problems + mat_problems,
    )
    if result.ok and materialized:
        result.lock_location, result.lock_mode = _record_lock(
            scene_path, entry_layer, result.lock(), pin_dir, wrapper
        )
    return result
