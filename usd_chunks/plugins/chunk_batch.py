"""Batch planning — turn one chunk network plus a glob into N independent runs.

The model is Houdini PDG/TOPs, not Solaris For-Each: each matched file becomes its own work
item that cooks the whole network on its own, one after the other, rather than everything
being composed into a single stage.

A run is planned from the patterns already stored on the network's Chunk prims. Every
``inputs:`` attribute holding a glob (scalar ``string`` or ``string[]`` alike) is a pattern
site. Exactly one of those must resolve to at least one existing file: that one is the
*driver* and it decides how many runs happen, because an input pattern describes files that
exist while an output pattern describes files that don't yet. Every other pattern is a *sink*,
filled from the driver's captures (see ``chunk_glob``).

Patterns are anchored to the network .usda's own directory — standard USD asset-path
semantics — so neither the network nor the files it writes carry the shell's current
directory around with them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

from pxr import Sdf, Usd, Vt

from usd_chunks import chunk_glob, chunk_utils
from usd_chunks.plugins.chunk_usd import (
    _CHUNK_PRIM_TYPENAME,
    _CHUNK_TYPE_ATTR,
    _INPUTS_PREFIX,
    is_exec_input_name,
)

# A chunkgroup's documentation is prose, not a path — a `*` in it is a bullet point, and
# treating it as a sink would fail the whole batch on a wildcard-count mismatch.
_NOT_PATTERNS = {f"{_INPUTS_PREFIX}doc"}


class BatchError(Exception):
    """A batch that cannot be planned — no driver, an ambiguous one, or one matching nothing."""


@dataclass(frozen=True)
class PatternSite:
    """One glob found on a chunk prim: which attribute holds it, and where inside it."""

    prim_path: str
    attr_name: str
    pattern: str
    index: int | None = None

    @property
    def chunk_name(self) -> str:
        return Sdf.Path(self.prim_path).name

    @property
    def param_name(self) -> str:
        return self.attr_name[len(_INPUTS_PREFIX):]

    @property
    def label(self) -> str:
        """The ``CHUNK.param`` form the user types after ``--driver``."""
        name = f"{self.chunk_name}.{self.param_name}"
        return f"{name}[{self.index}]" if self.index is not None else name


@dataclass(frozen=True)
class BatchRun:
    """One work item: the driver file it cooks, and every concrete value that follows from it."""

    source: str
    captures: Tuple[str, ...]
    sink_values: Tuple[Tuple[PatternSite, str], ...] = ()
    output: str | None = None


@dataclass
class BatchPlan:
    anchor: Path
    driver: PatternSite
    sinks: List[PatternSite] = field(default_factory=list)
    runs: List[BatchRun] = field(default_factory=list)
    output_pattern: str | None = None


def _is_pattern_value(value: str) -> bool:
    """A glob, and not an expression — an expression's value is computed from another
    chunk's param, so overwriting it with a batch path would break the wiring it stands for.
    """
    return chunk_glob.is_pattern(value) and not chunk_utils.has_expression(value)


def find_pattern_sites(stage: Usd.Stage) -> List[PatternSite]:
    """Every glob held by an ``inputs:`` attribute on a Chunk prim, in traversal order.

    Scalar and array params are both in scope: a list param is stored as
    ``string[] inputs:imports:sublayer_path``, and one of its entries is as much a batch
    input as a plain ``string inputs:file_path`` is.
    """
    sites: List[PatternSite] = []
    for prim in stage.Traverse():
        if prim.GetTypeName() != _CHUNK_PRIM_TYPENAME:
            continue
        prim_path = str(prim.GetPath())
        for attr in sorted(prim.GetAttributes(), key=lambda a: a.GetName()):
            name = attr.GetName()
            if not name.startswith(_INPUTS_PREFIX) or is_exec_input_name(name) or name in _NOT_PATTERNS:
                continue
            value = attr.Get()
            if isinstance(value, str):
                if _is_pattern_value(value):
                    sites.append(PatternSite(prim_path, name, value))
            elif isinstance(value, (Vt.StringArray, Vt.TokenArray)):
                for i, entry in enumerate(value):
                    if _is_pattern_value(entry):
                        sites.append(PatternSite(prim_path, name, entry, index=i))
    return sites


_OVERRIDE_RE = re.compile(
    r"\A(?:(?P<chunk>[^.=\[\]]+)\.)?(?P<param>[^=\[\]]+)(?:\[(?P<index>\d+)\])?\Z"
)


def _iter_input_attrs(stage: Usd.Stage):
    """Yield (prim, attr_name, param_name) for every ``inputs:`` attribute on a Chunk prim."""
    for prim in stage.Traverse():
        if prim.GetTypeName() != _CHUNK_PRIM_TYPENAME:
            continue
        for attr in sorted(prim.GetAttributes(), key=lambda a: a.GetName()):
            name = attr.GetName()
            if not name.startswith(_INPUTS_PREFIX) or is_exec_input_name(name):
                continue
            yield prim, name, name[len(_INPUTS_PREFIX):]


def apply_overrides(stage: Usd.Stage, overrides: List[str]) -> List[str]:
    """Author ``CHUNK.param=value`` command-line overrides into the session layer.

    The same override form ``run`` takes, applied here *before* planning rather than after
    loading, because an override may well be the glob that drives the whole batch — a
    network is worth reusing across asset folders without editing it. Authoring at the USD
    level (rather than onto loaded params, as ``run`` does) is what makes that possible, and
    it costs nothing: ``load_chunks`` reads these back as the params' restored values.

    A bare ``param=value`` applies to every chunk holding a param of that name, matching how
    ``run`` resolves the same form.

    Args:
        stage (Usd.Stage): The stage to author onto
        overrides (List[str]): ``CHUNK.param=value``, ``param=value`` or ``CHUNK.param[i]=value``

    Returns:
        List[str]: One human-readable line per applied override, for the plan header

    Raises:
        BatchError: A malformed override, or one naming no attribute on any Chunk prim
    """
    applied: List[str] = []
    if not overrides:
        return applied

    with Usd.EditContext(stage, stage.GetSessionLayer()):
        for override in overrides:
            label, _, value = override.partition("=")
            if not _:
                raise BatchError(f"malformed override {override!r} — expected CHUNK.param=value")

            m = _OVERRIDE_RE.match(label.strip())
            if not m:
                raise BatchError(f"malformed override target {label!r} — expected CHUNK.param=value")
            want_chunk, want_param = m.group("chunk"), m.group("param")
            index = int(m.group("index")) if m.group("index") else None

            hits = [
                (prim, attr_name)
                for prim, attr_name, param_name in _iter_input_attrs(stage)
                if param_name == want_param and (want_chunk is None or prim.GetName() == want_chunk)
            ]
            if not hits:
                raise BatchError(
                    f"override {label!r} names no param on any Chunk prim — it must already be "
                    f"authored in the network, so its type is known"
                )

            for prim, attr_name in hits:
                attr = prim.GetAttribute(attr_name)
                current = attr.Get()
                if isinstance(current, (Vt.StringArray, Vt.TokenArray)):
                    if index is None:
                        raise BatchError(
                            f"override {label!r} targets a list param — say which entry, "
                            f"e.g. {label}[0]={value}"
                        )
                    entries = list(current)
                    if index >= len(entries):
                        raise BatchError(
                            f"override {label!r} index {index} is out of range "
                            f"({len(entries)} entr(y/ies))"
                        )
                    entries[index] = value
                    attr.Set(Vt.StringArray(entries))
                else:
                    attr.Set(value)
                slot = f"[{index}]" if index is not None else ""
                applied.append(f"{prim.GetName()}.{want_param}{slot} = {value!r}")

    return applied


def _find_site(sites: List[PatternSite], label: str) -> PatternSite:
    matches = [s for s in sites if s.label == label]
    if not matches:
        known = ", ".join(s.label for s in sites) or "none"
        raise BatchError(f"--driver {label!r} is not a pattern in this network (patterns: {known})")
    return matches[0]


def select_driver(
    sites: List[PatternSite], anchor: Path, driver: str | None = None
) -> Tuple[PatternSite, List[Tuple[str, Tuple[str, ...]]]]:
    """Pick the one pattern that drives the batch, and return it with its expansion.

    Without ``--driver`` this is decided by the filesystem: whichever pattern actually
    resolves to files is the input. Two of them resolving is genuinely ambiguous and is a
    hard error rather than a guess.
    """
    if not sites:
        raise BatchError("no glob patterns found on any Chunk prim — nothing to batch over")

    root = str(anchor)
    if driver:
        site = _find_site(sites, driver)
        matches = chunk_glob.expand(site.pattern, root)
        if not matches:
            raise BatchError(f"driver {site.label} ({site.pattern!r}) matched no files under {anchor}")
        return site, matches

    resolved = [(site, chunk_glob.expand(site.pattern, root)) for site in sites]
    candidates = [(site, matches) for site, matches in resolved if matches]

    if not candidates:
        listed = ", ".join(f"{s.label} ({s.pattern!r})" for s in sites)
        raise BatchError(
            f"no pattern matched any existing file under {anchor} — one input pattern must "
            f"resolve to at least one file to drive the batch (tried: {listed})"
        )
    if len(candidates) > 1:
        listed = ", ".join(f"{s.label} ({s.pattern!r}, {len(m)} file(s))" for s, m in candidates)
        raise BatchError(
            f"ambiguous driver: {len(candidates)} patterns match existing files — {listed}. "
            f"Pick one with --driver CHUNK.param"
        )
    return candidates[0]


def build_plan(
    stage: Usd.Stage,
    anchor: Path,
    output_pattern: str | None = None,
    driver: str | None = None,
) -> BatchPlan:
    """Resolve the network's patterns into a concrete list of runs."""
    anchor = Path(anchor)
    sites = find_pattern_sites(stage)
    driver_site, matches = select_driver(sites, anchor, driver)
    sinks = [s for s in sites if s != driver_site]

    plan = BatchPlan(anchor=anchor, driver=driver_site, sinks=sinks, output_pattern=output_pattern)
    for source, captures in matches:
        sink_values = []
        for sink in sinks:
            try:
                sink_values.append((sink, chunk_glob.substitute(sink.pattern, captures)))
            except chunk_glob.GlobError as exc:
                raise BatchError(f"sink {sink.label}: {exc}") from exc
        output = None
        if output_pattern:
            try:
                output = chunk_glob.substitute(output_pattern, captures)
            except chunk_glob.GlobError as exc:
                raise BatchError(f"output pattern: {exc}") from exc
        plan.runs.append(BatchRun(source, captures, tuple(sink_values), output))
    return plan


def committing_chunks(chunks) -> List:
    """Every loaded chunk whose module defines a ``commit`` hook.

    These are the chunks that write to disk once the solve settles — the ones a batch is
    ultimately run for, and the only ones whose output path has to differ run to run.

    Read off the loaded chunk objects rather than resolved from the stage, because a chunk
    module is loaded lazily: at plan time the registry holds a wrapper that has not imported
    the module yet, so the hook is not visible on it. This is the same place
    chunk_scheduler.commit_all looks, so the two can never disagree about who commits.

    Args:
        chunks (Iterable[ChunkBase]): The loaded chunks to inspect

    Returns:
        List: The chunks whose module defines commit
    """
    return [c for c in chunks if callable(getattr(getattr(c, "_module", None), "commit", None))]


def check_commit_targets(chunks, plan: BatchPlan) -> List:
    """Verify every chunk that writes on each run writes somewhere *different* each run.

    A committing chunk holding a fixed output path is the one way this feature quietly does
    the wrong thing: all N runs cook correctly, every one of them writes, and the folder is
    left holding a single file — the last asset — with nothing anywhere reporting a problem.
    Checked on the first run, before a single file has been written.

    Args:
        chunks (Iterable[ChunkBase]): The loaded chunks of the first run
        plan (BatchPlan): The plan whose sinks say which params vary per run

    Returns:
        List: The committing chunks, all of which are correctly targeted

    Raises:
        BatchError: Nothing writes at all, or a committing chunk has no per-run output
    """
    committing = committing_chunks(chunks)
    if not committing:
        raise BatchError(
            "nothing in this network writes to disk — add an output pattern with -o, or a "
            "chunk that exports (its output path needs a wildcard so each run writes its own "
            "file)"
        )

    sink_chunks = {site.chunk_name for site in plan.sinks}
    stuck = sorted(c.name for c in committing if c.name not in sink_chunks)
    if stuck:
        listed = ", ".join(stuck)
        raise BatchError(
            f"{listed} writes on every run but its output path never changes — all "
            f"{len(plan.runs)} runs would overwrite the same file. Put a wildcard in it "
            f"(e.g. 'out/*.usda') so each run writes its own, or redirect with -o"
        )
    return committing


def apply_run(stage: Usd.Stage, plan: BatchPlan, run: BatchRun) -> None:
    """Author this run's concrete paths over their ``inputs:`` attributes in the session layer.

    Session is the strongest opinion in the stack, so ``load_chunks`` reads these back as the
    params' restored values with no param-name resolution and no difference in handling
    between a scalar and one entry of an array. Must run before the chunks are loaded.
    """
    values: Dict[Tuple[str, str], Dict[int | None, str]] = {}
    for site, value in ((plan.driver, run.source),) + run.sink_values:
        values.setdefault((site.prim_path, site.attr_name), {})[site.index] = value

    with Usd.EditContext(stage, stage.GetSessionLayer()):
        for (prim_path, attr_name), by_index in values.items():
            prim = stage.GetPrimAtPath(prim_path)
            attr = prim.GetAttribute(attr_name) if prim else None
            if not attr:
                continue
            if None in by_index:
                attr.Set(by_index[None])
                continue
            entries = list(attr.Get() or [])
            for index, value in by_index.items():
                if index < len(entries):
                    entries[index] = value
            attr.Set(Vt.StringArray(entries))


def format_plan(plan: BatchPlan, usda: str) -> str:
    """The header printed before any run starts — the anchor is in it deliberately, since
    patterns resolving relative to the .usda rather than to the shell's cwd surprises people
    exactly once."""
    lines = [
        f"Batch: {usda}",
        f"Anchor: {plan.anchor}",
        f"Driver: {plan.driver.label} = {plan.driver.pattern!r}",
    ]
    for sink in plan.sinks:
        lines.append(f"Sink:   {sink.label} = {sink.pattern!r}")
    if plan.output_pattern:
        lines.append(f"Output: {plan.output_pattern!r}")
    lines.append(f"Runs:   {len(plan.runs)}")
    for i, run in enumerate(plan.runs, 1):
        target = f" -> {run.output}" if run.output else ""
        lines.append(f"  [{i}/{len(plan.runs)}] {run.source}{target}")
    return "\n".join(lines)
