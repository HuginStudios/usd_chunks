"""Batch planning -- turn one chunk network plus a glob into N independent runs.

The model is Houdini PDG/TOPs, not Solaris For-Each: each matched file becomes its own work
item that cooks the whole network on its own, rather than everything being composed into a
single stage.

A batch is planned from the patterns already stored on the network's Chunk prims. Every
string ``inputs:`` attribute holding a glob is a pattern site. Exactly one of those must
resolve to at least one existing file: that one is the *driver* and it decides how many runs
happen, because an input pattern describes files that exist while an output pattern
describes files that don't yet. Every other pattern is a *sink*, filled from the driver's
captures (see ``chunk_glob``).

Planning only reads the stage -- no chunk is loaded or cooked. Each run is then an ordinary
``usdchunks run`` in its own process (see ``cli.cmd_batch``), handed the driver's file and
every sink's value as ``CHUNK.param=value`` overrides, so a batch cooks exactly what ``run``
would for that one file. A process per run, as ``sweep`` does per variant: the registry and
stage-sync singletons hold the stage a network was loaded into, so a second load in one
process would not cook the network again.

Patterns are anchored to the network .usda's own directory -- standard USD asset-path
semantics -- so neither the network nor the files it writes depend on the shell's current
directory.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

from pxr import Sdf, Usd, Vt

from usd_chunks import chunk_glob, chunk_naming, chunk_utils

_INPUTS_PREFIX = chunk_naming.INPUTS_PREFIX

# A chunkgroup's documentation is prose, not a path -- a `*` in it is a bullet point, and
# treating it as a sink would fail the whole batch on a wildcard-count mismatch.
_NOT_PATTERNS = {f"{_INPUTS_PREFIX}doc"}


class BatchError(Exception):
    """A batch that cannot be planned -- no driver, an ambiguous one, or one matching nothing."""


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
        """The param as ``run`` finds it: a grouped param's attribute is
        ``inputs:Output:output_dir``, and ``run`` reads that group path as ``Output.output_dir``."""
        return _param_path(self.attr_name)

    @property
    def label(self) -> str:
        """The ``CHUNK.param`` form the user types after ``--driver``, and ``run`` reads."""
        name = f"{self.chunk_name}.{self.param_name}"
        return f"{name}[{self.index}]" if self.index is not None else name


@dataclass(frozen=True)
class BatchRun:
    """One work item: the driver file it cooks, and every concrete value that follows from it."""

    source: str
    captures: Tuple[str, ...]
    sink_values: Tuple[Tuple[PatternSite, str], ...] = ()
    output: str | None = None

    def values(self, driver: PatternSite, anchor: Path | None = None) -> List[Tuple[PatternSite, str]]:
        """This run's concrete values: the driver's file, then every sink.

        Given the anchor, a relative value is handed over absolute. A chunk authors a path
        into its own anonymous layer, which has no folder for a relative path to resolve
        against -- a Reference to ``./assets/a.usd`` would look for it beside wherever the
        result is later exported. The plan itself stays relative, as the network wrote it.
        """

        def value_of(value: str) -> str:
            if anchor is None or Path(value).is_absolute():
                return value
            return (Path(anchor) / value).resolve().as_posix()

        return [(driver, value_of(self.source))] + [(site, value_of(value)) for site, value in self.sink_values]


def write_session(stage: Usd.Stage, values: List[Tuple[PatternSite, str]], path: str) -> None:
    """Write a run's values as a layer of opinions on the network's chunk prims.

    ``run --session`` opens the network with it as the session layer, the strongest opinion
    there is, so the chunks load holding this run's values and the network cooks once, with
    them. Handed over as ``CHUNK.param=value`` instead they would arrive after load had
    already cooked it with the saved values -- for a proxy network, the saved asset baked
    and its textures written, once per run, before the run's own asset was even read.

    Args:
        stage (Usd.Stage): The planning stage, for each attribute's type
        values: (pattern site, value) pairs, from BatchRun.values
        path (str): Where to write the layer
    """
    layer = Sdf.Layer.CreateNew(path)
    for site, value in values:
        attr = stage.GetPrimAtPath(site.prim_path).GetAttribute(site.attr_name)
        type_name = attr.GetTypeName() if attr else Sdf.ValueTypeNames.String
        prim_spec = Sdf.CreatePrimInLayer(layer, site.prim_path)
        spec = prim_spec.attributes.get(site.attr_name) or Sdf.AttributeSpec(prim_spec, site.attr_name, type_name)
        spec.default = value
    layer.Save()


@dataclass
class BatchPlan:
    anchor: Path
    driver: PatternSite
    sinks: List[PatternSite] = field(default_factory=list)
    runs: List[BatchRun] = field(default_factory=list)
    output_pattern: str | None = None


def _is_chunk_prim(prim: Usd.Prim) -> bool:
    return prim.GetTypeName() == chunk_naming.CHUNK_PRIM_TYPENAME


def _param_path(attr_name: str) -> str:
    """``inputs:Output:output_dir`` -> ``Output.output_dir``, the group path ``run`` reads."""
    return attr_name[len(_INPUTS_PREFIX):].replace(":", ".")


def _names_param(attr_name: str, wanted: str) -> bool:
    """Whether an override's param (bare, or a dotted group path) names this attribute:
    ``output_dir`` and ``Output.output_dir`` both name ``inputs:Output:output_dir``,
    as both reach that param in ``run``."""
    path = _param_path(attr_name)
    return path == wanted or ("." not in wanted and path.endswith(f".{wanted}"))


def _is_pattern_value(value: str) -> bool:
    """A glob, and not an expression -- an expression's value is computed from another
    chunk's param, so overwriting it with a batch path would break the wiring it stands for.
    """
    return chunk_glob.is_pattern(value) and not chunk_utils.has_expression(value)


def _input_attrs(prim: Usd.Prim):
    """A Chunk prim's param attributes, by name: no exec ports, no prose."""
    for attr in sorted(prim.GetAttributes(), key=lambda a: a.GetName()):
        name = attr.GetName()
        if name.startswith(_INPUTS_PREFIX) and not chunk_naming.is_exec_input_name(name) and name not in _NOT_PATTERNS:
            yield attr


def find_pattern_sites(stage: Usd.Stage) -> List[PatternSite]:
    """Every glob held by an ``inputs:`` attribute on a Chunk prim, in traversal order.

    Entries of a list param (``string[] inputs:imports:sublayer_path``) are found too, so
    that build_plan can refuse them by name rather than leave them as literal globs.
    """
    sites: List[PatternSite] = []
    for prim in stage.Traverse():
        if not _is_chunk_prim(prim):
            continue
        prim_path = str(prim.GetPath())
        for attr in _input_attrs(prim):
            value = attr.Get()
            if isinstance(value, str):
                if _is_pattern_value(value):
                    sites.append(PatternSite(prim_path, attr.GetName(), value))
            elif isinstance(value, (Vt.StringArray, Vt.TokenArray)):
                for i, entry in enumerate(value):
                    if _is_pattern_value(entry):
                        sites.append(PatternSite(prim_path, attr.GetName(), entry, index=i))
    return sites


_OVERRIDE_RE = re.compile(r"\A(?:(?P<chunk>[^.=\[\]]+)\.)?(?P<param>[^=\[\]]+)\Z")


def split_overrides(overrides: List[str]) -> Tuple[List[str], List[str]]:
    """Separate overrides that supply a glob from the rest.

    A glob has to be on the stage *before* planning, because it may be the pattern that
    drives the whole batch. Everything else is passed through to every run, where ``run``
    applies it to the loaded params with their real types.

    Args:
        overrides (List[str]): Raw ``CHUNK.param=value`` strings

    Returns:
        Tuple[List[str], List[str]]: (pattern overrides, plain overrides)
    """
    patterns, plain = [], []
    for override in overrides:
        _, _, value = override.partition("=")
        (patterns if chunk_glob.is_pattern(value) else plain).append(override)
    return patterns, plain


def apply_overrides(stage: Usd.Stage, overrides: List[str]) -> List[str]:
    """Author glob-valued ``CHUNK.param=value`` overrides into the session layer for planning.

    A network is worth reusing across asset folders without editing it, and the planner
    reads patterns off the stage, so a glob from the command line goes there first.

    The attribute is created when the network never authored one: a param left at its
    default has no ``inputs:`` attr at all, so requiring one would refuse exactly the case
    this exists for -- a Reference chunk whose file path was never filled in, pointed at a
    folder from the command line. Only glob values reach here, and a glob is a path pattern,
    so String is not a guess. Creating one needs a chunk name; a bare ``param=value`` applies
    to every chunk that already authored a param of that name.

    Args:
        stage (Usd.Stage): The stage to author onto
        overrides (List[str]): Glob-valued overrides, from split_overrides

    Returns:
        List[str]: One human-readable line per applied override, for the plan header

    Raises:
        BatchError: A malformed override, or one naming no chunk
    """
    applied: List[str] = []
    chunks = [p for p in stage.Traverse() if _is_chunk_prim(p)]
    known = ", ".join(sorted(p.GetName() for p in chunks)) or "none"
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        for override in overrides:
            label, sep, value = override.partition("=")
            m = _OVERRIDE_RE.match(label.strip()) if sep else None
            if not m:
                raise BatchError(f"malformed override {override!r} -- expected CHUNK.param=value")
            want_chunk, want_param = m.group("chunk"), m.group("param")
            # Only planning reads this attribute -- each run hands the concrete value to
            # `run` by name -- so one the network never authored is made in the colon form
            # of what was typed.
            attr_name = f"{_INPUTS_PREFIX}{want_param.replace('.', ':')}"

            hits = []
            for prim in chunks:
                if want_chunk is not None and prim.GetName() != want_chunk:
                    continue
                named = [a.GetName() for a in _input_attrs(prim) if _names_param(a.GetName(), want_param)]
                hits += [(prim, name) for name in named]
            if not hits:
                if want_chunk is None:
                    raise BatchError(
                        f"override {label!r} matches no param the network has authored, and without a "
                        f"chunk name there is nothing to create it on -- qualify it, e.g. "
                        f"CHUNK.{want_param}=... (chunks: {known})"
                    )
                hits = [(p, attr_name) for p in chunks if p.GetName() == want_chunk]
                if not hits:
                    raise BatchError(f"override {label!r} names no chunk {want_chunk!r} (chunks: {known})")
                for prim, name in hits:
                    prim.CreateAttribute(name, Sdf.ValueTypeNames.String)

            for prim, name in hits:
                prim.GetAttribute(name).Set(value)
                applied.append(f"{prim.GetName()}.{_param_path(name)} = {value!r}")
    return applied


def _find_site(sites: List[PatternSite], label: str) -> PatternSite:
    for site in sites:
        if site.label == label:
            return site
    known = ", ".join(s.label for s in sites) or "none"
    raise BatchError(f"--driver {label!r} is not a pattern in this network (patterns: {known})")


def select_driver(
    sites: List[PatternSite], anchor: Path, driver: str | None = None
) -> Tuple[PatternSite, List[Tuple[str, Tuple[str, ...]]]]:
    """Pick the one pattern that drives the batch, and return it with its expansion.

    Without ``--driver`` this is decided by the filesystem: whichever pattern actually
    resolves to files is the input. Two of them resolving is genuinely ambiguous and is a
    hard error rather than a guess.
    """
    if not sites:
        raise BatchError("no glob patterns found on any Chunk prim -- nothing to batch over")

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
            f"no pattern matched any existing file under {anchor} -- one input pattern must "
            f"resolve to at least one file to drive the batch (tried: {listed})"
        )
    if len(candidates) > 1:
        listed = ", ".join(f"{s.label} ({s.pattern!r}, {len(m)} file(s))" for s, m in candidates)
        raise BatchError(
            f"ambiguous driver: {len(candidates)} patterns match existing files -- {listed}. "
            f"Pick one with --driver CHUNK.param"
        )
    return candidates[0]


def _check_addressable(stage: Usd.Stage, sites: List[PatternSite]) -> None:
    """Every pattern must be one ``run`` can set by ``CHUNK.param=value``.

    A list entry cannot be (``run`` sets whole params), and a chunk name shared by two chunk
    prims -- one in each of two groups, say -- would send the value to whichever ``run``
    finds first. Either way some run would cook with the glob, or the wrong chunk's value,
    still in place, so both are refused before anything runs.
    """
    lists = [s.label for s in sites if s.index is not None]
    if lists:
        raise BatchError(
            f"batch cannot fill a pattern inside a list param yet: {', '.join(lists)} -- "
            f"move it to a plain path param"
        )
    names = Counter(p.GetName() for p in stage.Traverse() if _is_chunk_prim(p))
    shared = sorted({s.chunk_name for s in sites if names[s.chunk_name] > 1})
    if shared:
        raise BatchError(
            f"more than one chunk is named {', '.join(shared)}, so a run cannot say which one "
            f"its value is for -- rename one"
        )


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
    _check_addressable(stage, [driver_site] + sinks)

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


def committing_chunk_names(stage: Usd.Stage, registry) -> List[str]:
    """Names of the Chunk prims whose module defines a ``commit`` hook.

    These write to disk once a run's solve settles -- the ones a batch without ``-o`` is run
    for. Read from the registry by each prim's ``info:chunkType`` (the module's registered
    name), so nothing is loaded or cooked to find out.

    Args:
        stage (Usd.Stage): The network
        registry: The ChunkRegistry to look modules up in

    Returns:
        List[str]: The committing chunks' names, in traversal order
    """
    names = []
    for prim in stage.Traverse():
        if not _is_chunk_prim(prim):
            continue
        chunk_type = prim.GetAttribute(chunk_naming.CHUNK_TYPE_ATTR).Get()
        module = getattr(registry.get_chunk_module(str(chunk_type)), "module", None) if chunk_type else None
        if callable(getattr(module, "commit", None)):
            names.append(prim.GetName())
    return names


def check_commit_targets(committing: List[str], plan: BatchPlan) -> None:
    """Verify every chunk that writes on each run writes somewhere *different* each run.

    A committing chunk holding a fixed output path is the one way a batch quietly does the
    wrong thing: all N runs cook correctly, every one of them writes, and the folder is left
    holding a single file -- the last asset -- with nothing reporting a problem.

    Args:
        committing (List[str]): Names of the chunks that write, from committing_chunk_names
        plan (BatchPlan): The plan whose sinks say which params vary per run

    Raises:
        BatchError: Nothing writes at all, or a committing chunk has no per-run output
    """
    if not committing:
        raise BatchError(
            "nothing in this network writes to disk -- add an output pattern with -o, or a "
            "chunk that exports (its output path needs a wildcard so each run writes its own "
            "file)"
        )
    sink_chunks = {site.chunk_name for site in plan.sinks}
    stuck = sorted(name for name in committing if name not in sink_chunks)
    if stuck:
        raise BatchError(
            f"{', '.join(stuck)} writes on every run but its output path never changes -- all "
            f"{len(plan.runs)} runs would overwrite the same file. Put a wildcard in it "
            f"(e.g. 'out/*.usda') so each run writes its own, or redirect with -o"
        )


def format_plan(plan: BatchPlan, usda: str) -> str:
    """The header printed before any run starts -- the anchor is in it deliberately, since
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
