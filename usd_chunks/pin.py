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

Two things are locked, not one. ``usdchunks:pins`` records which chunk impl
produced the scene; ``usdchunks:packages`` records the pip package versions that
code was standing on. Pinning only the first reproduces the recipe and not the
result -- mujoco is the case that matters, where a solver change moves the
physics with every chunk byte-identical. ``provision_venv`` is the npm-ci half:
it installs those exact versions into a venv, with system site packages on, so
the host's OpenUSD and Qt stay visible while pip's share is pinned.

The store-facing work goes through the plugin seam (usd_chunks.stores); this
module never imports any marketplace. pxr is imported lazily so the pure
resolve/materialize logic stays importable (and testable) without USD.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Iterable

from usd_chunks.stores import StoreProvider, discovered_stores, resolve_across_stores
from usd_chunks import chunk_naming

#: Layer-level key holding the flat lock: {chunkType: "store:version"}.
PINS_KEY = "usdchunks:pins"
PACKAGES_KEY = "usdchunks:packages"
HASHES_KEY = "usdchunks:hashes"

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
    source: Path | None  # store impl path; None if the store couldn't provide


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
    materialized: list[MaterializedPin] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    lock_location: Path | None = None
    lock_mode: str | None = None  # "in-place" | "wrapper"
    #: {package: installed version} recorded beside the impl pins.
    packages: dict[str, str] = field(default_factory=dict)
    #: {chunkType: "sha256:..."} of the materialized impls -- integrity, where the
    #: version is provenance.
    hashes: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problems

    def lock(self) -> dict[str, str]:
        return {m.chunk_type: f"{m.store}:{m.version}" for m in self.materialized}


# --- resolution (pure: no pxr, no filesystem writes) -------------------------

def resolve_pins(
    specs: dict[str, str | None],
    existing: dict[str, str],
    stores: Iterable[StoreProvider] | None = None,
) -> tuple[list[PinEntry], list[str]]:
    """Turn {chunkType: spec} into resolved (store, version, source) entries.

    A chunkType already in ``existing`` (the recorded lock) is *reused* -- its
    exact store:version is re-provided, not re-resolved (restore semantics).
    Anything else is resolved across the registered stores by spec, first hit
    wins. Returns (entries, problems); a chunk no store carries is a problem.
    """
    stores = list(discovered_stores() if stores is None else stores)
    by_name = {s.name: s for s in stores}
    entries: list[PinEntry] = []
    problems: list[str] = []

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

def _find_in_dir(pin_dir: Path, chunk_type: str) -> Path | None:
    for p in pin_dir.glob(f"{chunk_type}.*"):
        if p.is_file():
            return p
    return None


def _link_or_copy(src: Path, dst: Path, copy: bool) -> str:
    """Place src at dst as a symlink (default) or a copy. Falls back to copy if
    the platform refuses symlinks (Windows without developer mode/admin).
    """
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
    entries: list[PinEntry], pin_dir: Path, copy: bool = False
) -> tuple[list[MaterializedPin], list[str]]:
    """Lay resolved entries into the flat pin folder as ``<chunkType><ext>``.

    An entry whose store couldn't provide a source is only OK if the impl is
    already present in the pin folder (a ``--copy`` bundle that travelled with
    the scene); otherwise it is a problem.
    """
    pin_dir.mkdir(parents=True, exist_ok=True)
    out: list[MaterializedPin] = []
    problems: list[str] = []
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

def collect_specs(stage) -> dict[str, str | None]:
    """{chunkType: info:chunkVersion spec} for every Chunk prim in the composed
    stage (the container 'chunkgroup' sentinel excluded). Read-only.

    Multiple prims of the same chunkType must agree on a spec; a genuine
    conflict (two different non-None specs) raises PinError.
    """
    specs: dict[str, str | None] = {}
    conflicts: dict[str, set] = {}
    for prim in stage.Traverse():
        if prim.GetTypeName() != chunk_naming.CHUNK_PRIM_TYPENAME:
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


def read_lock(layer) -> dict[str, str]:
    """The recorded {chunkType: 'store:version'} lock on a layer, or {}."""
    cld = layer.customLayerData
    pins = cld.get(PINS_KEY) if cld else None
    if not pins:
        return {}
    return {str(k): str(v) for k, v in dict(pins).items()}


def read_scene_lock(scene_path) -> dict[str, str]:
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


def collect_packages(chunk_types: Iterable[str]) -> dict[str, str]:
    """{package: installed version} for everything the scene's chunks require.

    The versions actually importable now, not the ranges the chunks declared. A range says
    what a chunk tolerates; this says what it ran against, and only the second one lets a
    later difference be explained rather than merely detected.

    Recorded beside the impl pins because they answer the same question at two levels: the
    pins say which chunk code produced this, and these say what that code was standing on.
    A pin without them reproduces the recipe and not the result -- mujoco is the case that
    matters, where a solver change moves physics with every chunk byte-identical.
    """
    from usd_chunks import chunk_registry

    registry = chunk_registry.ChunkRegistry()
    modules = {m.module_name: m for m in registry.get_chunk_modules()} if registry.initialized else {}

    packages: dict[str, str] = {}
    for chunk_type in chunk_types:
        module = modules.get(str(chunk_type))
        if module is None:
            continue
        for requirement in module.config.requirements or []:
            name, _ = chunk_registry.split_requirement(requirement)
            if name in packages:
                continue
            if (installed := chunk_registry.installed_version(name)) is not None:
                packages[name] = installed
    return packages


def hash_file(path) -> str | None:
    """sha256 of a file's bytes, as "sha256:<hex>", or None if it cannot be read.

    Whole bytes, not the whitespace-stripped main() digest ChunkModule keeps for its cache:
    that one deliberately ignores formatting, and integrity has to notice everything.
    """
    import hashlib

    try:
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None
    return f"sha256:{digest}"


def collect_hashes(materialized: Iterable[MaterializedPin]) -> dict[str, str]:
    """{chunkType: "sha256:..."} for the impls actually laid into the pin folder.

    A version and a hash answer different questions and a lock needs both. "rbd_solver
    1.2.0 from store X" is *resolvable* -- it is how the impl is fetched again, and no store
    can serve you a hash. But a version cannot notice that the file changed underneath it:
    edit a pinned chunk by hand and it is still 1.2.0, producing different results with an
    identical lock. Fetch by version, verify by hash -- npm records both, so does Cargo, and
    for this reason.
    """
    hashes: dict[str, str] = {}
    for pin_entry in materialized:
        if (digest := hash_file(pin_entry.path)) is not None:
            hashes[pin_entry.chunk_type] = digest
    return hashes


def read_hashes(layer) -> dict[str, str]:
    """The recorded {chunkType: "sha256:..."} on a layer, or {}."""
    cld = layer.customLayerData
    recorded = cld.get(HASHES_KEY) if cld else None
    if not recorded:
        return {}
    return {str(k): str(v) for k, v in dict(recorded).items()}


def write_hashes(layer, hashes: dict[str, str]) -> None:
    """Set the integrity hashes on a layer's customLayerData. Caller Saves."""
    if not hashes:
        return
    cld = dict(layer.customLayerData) if layer.customLayerData else {}
    cld[HASHES_KEY] = dict(hashes)
    layer.customLayerData = cld


def verify_hashes(materialized: Iterable[MaterializedPin], recorded: dict[str, str]) -> list[str]:
    """Impls whose bytes no longer match what the lock recorded.

    The case a version alone cannot see: same store, same version number, edited file. Also
    catches a store republishing different content under one version, and a --copy bundle
    tampered with in transit.

    A chunk with no recorded hash is not a failure -- it was pinned before hashes existed,
    and refusing to restore an older lock would be worse than the gap it closes.
    """
    problems = []
    for pin_entry in materialized:
        expected = recorded.get(pin_entry.chunk_type)
        if not expected:
            continue
        actual = hash_file(pin_entry.path)
        if actual is None:
            problems.append(f"{pin_entry.chunk_type}: cannot read {pin_entry.path} to verify")
        elif actual != expected:
            problems.append(
                f"{pin_entry.chunk_type}=={pin_entry.version}: content changed since it was "
                f"pinned ({expected[:19]}... -> {actual[:19]}...)"
            )
    return problems


def read_packages(layer) -> dict[str, str]:
    """The recorded {package: version} lock on a layer, or {}."""
    cld = layer.customLayerData
    recorded = cld.get(PACKAGES_KEY) if cld else None
    if not recorded:
        return {}
    return {str(k): str(v) for k, v in dict(recorded).items()}


def write_packages(layer, packages: dict[str, str]) -> None:
    """Set the package lock on a layer's customLayerData. Caller Saves.

    A separate key from the impl pins rather than nested inside them: read_lock's contract
    is a flat {chunkType: 'store:version'}, and widening it would break every reader for the
    sake of avoiding one more key.
    """
    if not packages:
        return
    cld = dict(layer.customLayerData) if layer.customLayerData else {}
    cld[PACKAGES_KEY] = dict(packages)
    layer.customLayerData = cld


def write_lock(layer, lock: dict[str, str]) -> None:
    """Set the flat pin lock on a layer's customLayerData (one write, layer
    level, ignored by composition). Caller Saves.
    """
    cld = dict(layer.customLayerData) if layer.customLayerData else {}
    cld[PINS_KEY] = dict(lock)
    layer.customLayerData = cld


def _write_wrapper(scene_path: Path, lock: dict[str, str], pin_dir: Path,
                   packages: dict[str, str] | None = None,
                   hashes: dict[str, str] | None = None):
    """A thin layer that sublayers the (immutable) scene and carries the lock,
    written into the pin folder so the source is never touched. Render this.
    """
    from pxr import Sdf

    pin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = pin_dir / f"{scene_path.stem}.pinned.usda"
    wl = Sdf.Layer.CreateNew(str(wrapper))
    wl.subLayerPaths.append(str(scene_path.resolve()))
    write_lock(wl, lock)
    write_packages(wl, packages or {})
    write_hashes(wl, hashes or {})
    wl.Save()
    return wrapper


def _record_lock(scene_path: Path, entry_layer, lock: dict[str, str], pin_dir: Path, wrapper: bool,
                 packages: dict[str, str] | None = None,
                 hashes: dict[str, str] | None = None):
    """Write the lock onto the entry layer in place, or into a wrapper layer if
    the entry layer is read-only (or --wrapper was forced). Returns (path, mode).
    """
    if not wrapper:
        try:
            write_lock(entry_layer, lock)
            write_packages(entry_layer, packages or {})
            write_hashes(entry_layer, hashes or {})
            entry_layer.Save()
            return Path(scene_path), "in-place"
        except Exception:
            pass  # read-only entry layer -> fall through to a wrapper
    return _write_wrapper(scene_path, lock, pin_dir, packages, hashes), "wrapper"


# --- provisioning -------------------------------------------------------------

def venv_python(venv_dir: Path) -> Path:
    """The interpreter inside a venv, on either platform."""
    windows = venv_dir / "Scripts" / "python.exe"
    return windows if os.name == "nt" else venv_dir / "bin" / "python"


def provision_venv(packages: dict[str, str], venv_dir, system_site_packages: bool = True):
    """Create a venv and install the locked package versions into it.

    **--system-site-packages by default, and it is not a convenience.** A chunk runs against
    the host's OpenUSD and the host's Qt -- `pxr` and `PySide6` come from a bundle that pip
    cannot install and a venv must not hide. So the venv pins what pip owns (mujoco, numpy)
    and inherits what the host owns. Isolating fully would produce an environment where
    every chunk fails to import USD.

    Exact versions, not the declared ranges: this is the npm-ci half of the lock. Resolving
    the ranges again would let the versions drift, which is the thing the lock exists to
    stop.

    Returns (python, installed, problems). Nothing is installed and no venv is made when the
    lock is empty -- a scene whose chunks need no packages needs no environment.
    """
    import subprocess
    import venv as venv_module

    venv_dir = Path(venv_dir)
    if not packages:
        return None, [], []

    builder = venv_module.EnvBuilder(
        with_pip=True, system_site_packages=system_site_packages, clear=False
    )
    builder.create(str(venv_dir))
    python = venv_python(venv_dir)
    if not python.exists():
        return None, [], [f"venv created but no interpreter at {python}"]

    requirements = [f"{name}=={version}" for name, version in sorted(packages.items())]
    proc = subprocess.run(
        [str(python), "-m", "pip", "install", "--disable-pip-version-check", *requirements],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout or b"").decode(errors="replace").strip().splitlines()[-10:])
        return python, [], [f"pip install failed ({proc.returncode}):\n{tail}"]
    return python, requirements, []


# --- orchestration ------------------------------------------------------------

def pin(
    scene_path,
    pin_dir=None,
    copy: bool = False,
    wrapper: bool = False,
    stores: Iterable[StoreProvider] | None = None,
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

    # Restore path: an impl fetched by version has to match the bytes that version had when
    # it was pinned. This is the only check that notices a chunk edited in place -- the
    # version string is identical either way.
    mat_problems = list(mat_problems) + verify_hashes(materialized, read_hashes(entry_layer))

    result = PinResult(
        scene=scene_path,
        pin_dir=pin_dir,
        materialized=materialized,
        problems=resolve_problems + mat_problems,
    )
    if result.ok and materialized:
        # The package versions alongside the impl pins: pinning which chunk code produced a
        # scene reproduces the recipe, not the result. mujoco is the case that matters --
        # a solver change moves the physics with every chunk byte-identical.
        result.packages = collect_packages(specs.keys())
        result.hashes = collect_hashes(materialized)
        result.lock_location, result.lock_mode = _record_lock(
            scene_path, entry_layer, result.lock(), pin_dir, wrapper, result.packages,
            result.hashes,
        )
    return result
