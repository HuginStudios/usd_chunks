"""usdchunks CLI -- headless execution of chunk networks serialized as .usda files.

Usage:
    usdchunks run my_setup.usda [ChunkName.param=value ...]
    usdchunks batch my_setup.usda [-o 'out/*.usda']
    usdchunks info my_setup.usda
"""
from __future__ import annotations

import filecmp
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from argparse import SUPPRESS, ArgumentParser, RawDescriptionHelpFormatter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from importlib.resources import files
from pathlib import Path

from pxr import Sdf, Usd
from usd_chunks import chunk_batch, chunk_naming, configure_logging
from usd_chunks.chunk_process import python_executable
from usd_chunks import chunk, chunk_registry, chunk_core, chunk_scene, chunk_scheduler, chunk_utils
from usd_chunks import chunk_catalog, cook_timings, live
from usd_chunks import sweep as sweepmod
from usd_chunks.store import pin as pinmod
from usd_chunks.chunk_script import Scene
from usd_chunks.chunk_stage import flatten_without_chunks
from usd_chunks.chunk_stage import (
    commit_order_from_stage,
    find_group_active_view,
    find_output_chunk,
    get_chunk_prim_path,
    is_view_chunk,
    recook_focus,
)


# ANSI colors for CLI output
RED = "\033[31m"
GREEN = "\033[32m"
RESET = "\033[0m"


def _all_chunks(chunks):
    """Yield every chunk recursively, including sub-chunks inside ChunkGroups."""
    for c in chunks:
        yield c
        sub = [p for p in c.get_my_params() if isinstance(p, chunk_core.ChunkBase)]
        yield from _all_chunks(sub)


def _coerce(param, raw):
    """Read a command-line string as something the param can hold.

    Returns (value, None) or (None, reason). Every path reports a reason rather than
    passing quietly: an override that lands on nothing looks exactly like one that
    worked, and the network cooks with its saved values either way.

    ParamTypeToTypes reports tuple/list for a VECTOR, and calling that constructor on
    the string is the trap -- tuple("0.1,0.6,0.9") is a tuple of eleven characters, not
    a colour. Vectors are split into components first. bool is the other one: bool() of
    any non-empty string is True, so "false" would switch a Checkbox on.
    """
    types = chunk.ChunkFlags.ParamTypeToTypes(param.evaluate_type())
    if not types:
        return None, f"{param.name!r} has no settable type"

    first = types[0]
    if first in (tuple, list):
        parts = [p for p in raw.strip().strip("()[]").split(",") if p.strip()]
        try:
            return tuple(float(p) for p in parts), None
        except ValueError:
            return None, f"expected comma-separated numbers, got {raw!r}"

    if first is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on"), None

    try:
        return first(raw), None
    except (TypeError, ValueError):
        return None, f"cannot read {raw!r} as {getattr(first, '__name__', first)}"


def _collapsed_groups(owner):
    """The collapsed groups under owner, outermost first, not looking inside them."""
    for child in getattr(owner, "get_my_params", lambda: [])():
        if not hasattr(child, "get_my_params"):
            continue
        if child.has_state(chunk.ChunkFlags.State.COLLAPSED):
            yield child
        else:
            yield from _collapsed_groups(child)


def _expanded(group):
    """Open group the way clicking its header does, then close it again.

    A chunk may build a group's params only once it is first opened -- Render Settings
    does, since V-Ray alone declares hundreds -- and a param that has not been built
    cannot be found, let alone set. Closing it again afterwards keeps the saved network
    as it was: the params stay built, only the header state goes back.
    """
    group.unset_state(chunk.ChunkFlags.State.COLLAPSED)
    group.set_state(chunk.ChunkFlags.State.COLLAPSED)


def _find_param(owner, name):
    """owner's param at name, opening collapsed groups until it has been built.

    The groups a dotted name passes through are opened first, since that is where the
    param has to be. Failing that -- a bare name, or a path that skips a level -- the
    collapsed groups are opened one at a time, outermost first, until the param exists,
    so only as much is built as it takes to find it.
    """
    param = owner.get_param(name)
    if param is not None:
        return param

    parts = name.split(".")
    for depth in range(1, len(parts)):
        group = owner.get_param(".".join(parts[:depth]))
        if group is None:
            break
        if group.has_state(chunk.ChunkFlags.State.COLLAPSED):
            _expanded(group)
    if (param := owner.get_param(name)) is not None:
        return param

    pending = list(_collapsed_groups(owner))
    while pending:
        group = pending.pop(0)
        _expanded(group)
        if (param := owner.get_param(name)) is not None:
            return param
        pending[:0] = _collapsed_groups(group)
    return None


def _parse_param_overrides(scene, overrides):
    # Contexts first, in both lookups. get_chunks() omits plain tab contexts because they
    # are not executable, but a tab is exactly where a network's lofted interface lives --
    # so without them `Tab1.length=40` reports "chunk not found", and a bare `length=40`
    # silently reaches past the promoted param into whichever implementation param happens
    # to share its name. Interface before implementation is also the right precedence: a
    # lofted param drives what it is connected to, so setting it does everything setting
    # the inner param would, and means what the tool's author said it means.
    searchable = list(scene.get_contexts()) + list(_all_chunks(scene.get_chunks()))
    chunk_map = {c.name: c for c in reversed(searchable)}
    for override in overrides:
        try:
            name, value = override.split("=", 1)
        except ValueError:
            print(f"Invalid argument format: {override!r} — expected param=value or ChunkName.param=value")
            continue

        # Bare param name (e.g. variant_selection=A): search all chunks for a matching param.
        # This is the natural form for lofted group interface params.
        if "." not in name:
            param = None
            for c in searchable:
                p = c.get_param(name, chunk.is_param)
                if p:
                    param = p
                    break
            else:
                # Not built yet in any chunk: look inside the collapsed groups too, once
                # every chunk's already-built params have had their chance.
                for c in searchable:
                    if (p := _find_param(c, name)) is not None and chunk.is_param(p):
                        param = p
                        break
            if not param:
                print(f"Param not found: {name!r}")
                continue
        else:
            chunk_name, param_path = name.split(".", 1)
            c = chunk_map.get(chunk_name)
            if not c:
                print(f"Chunk not found: {chunk_name!r}")
                continue
            param = _find_param(c, param_path)
            if param is not None and not chunk.is_param(param):
                param = None
            if not param:
                print(f"Param not found: {param_path!r} on chunk {chunk_name!r}")
                continue

        coerced, why = _coerce(param, value)
        if why:
            print(f"Cannot set {name!r}: {why}")
            continue
        param.value = coerced


def cmd_info(args):
    stage = Usd.Stage.Open(args.usda)
    if not stage:
        print(f"Failed to open: {args.usda}")
        sys.exit(1)

    _INPUTS = "inputs:"

    for prim in stage.Traverse():
        if prim.GetTypeName() != chunk_naming.CHUNK_PRIM_TYPENAME:
            continue
        if prim.GetAttribute("info:chunkType").Get() != "chunkgroup":
            continue

        doc = prim.GetAttribute(f"{_INPUTS}doc").Get()
        print("="*120)
        print(f"File: {stage.GetRootLayer().identifier}")
        print(f"Prim path: {prim.GetPath()}")
        print()

        print("Info:")
        if doc:
            for line in doc.strip().splitlines():
                print(f"  {line}")
        else:
            print("  (none)")
        print()

        params = [
            (attr.GetName()[len(_INPUTS):], attr.Get())
            for attr in prim.GetAttributes()
            if attr.GetName().startswith(_INPUTS)
            and attr.GetName() != f"{_INPUTS}doc"
            and attr.Get() is not None
        ]

        node_count = sum(1 for child in prim.GetChildren() if child.GetAttribute("info:chunkType").Get())
        print(f"Nodes: {node_count}")
        print()
        print("Params:")
        if params:
            for name, val in params:
                print(f"  {name} = {val!r}")
        else:
            print("  None (no chunkgroup params exists)")
        print("="*120)

def _catalog_registry():
    """The chunk registry, from USD_CHUNK_DIRS, without a stage worth the name."""
    return Scene(Usd.Stage.CreateInMemory(), "cli", sync=False).registry


#: Without a query, a module with more menu entries than this lists as one line (the
#: MaterialX shader chunk has hundreds, one per node).
_MANY_ENTRIES = 12


def _utf8_stdout() -> None:
    """Descriptions carry dashes and units (m²); a Windows pipe defaults to the ANSI page."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def cmd_list(args):
    _utf8_stdout()
    rows = chunk_catalog.entries(_catalog_registry(), args.query or "")
    if args.json:
        print(json.dumps([vars(r) for r in rows], indent=1, default=str))
        return
    if not rows:
        print(f"No chunk matches {args.query!r} -- is USD_CHUNK_DIRS set?" if args.query
              else "No chunks found -- is USD_CHUNK_DIRS set?")
        sys.exit(1)
    counts: dict[str, int] = {}
    for r in rows:
        if r.presets is not None:
            counts[r.module] = counts.get(r.module, 0) + 1
    folded = set() if args.query else {m for m, n in counts.items() if n > _MANY_ENTRIES}
    for r in rows:
        if r.module in folded and r.presets is not None:
            continue
        kind = f"-> {r.module}" if r.presets is not None else r.category
        summary = r.summary or "(no description)"
        if r.module in folded:
            summary = f"[{counts[r.module]} menu entries: usdchunks list <word>] {summary}"
        print(f"{r.name:<28} {kind:<20} {summary[:90]}")
    print("\n`usdchunks show <name>` for one in full; in a script, scene.create.<name>(input, param=value).")
    # The first thing an agent runs, so it points at the one page a script needs: agents
    # went from here straight to the docs and the framework's source, and never read it.
    print(f"Building a network in a script: {_build_network_guide()}")


def _build_network_guide() -> str:
    """The build-network skill's file in a checkout, else the scripting guide's name."""
    skill = Path(__file__).resolve().parent.parent / "skills" / "usd-chunks-build-network" / "SKILL.md"
    return str(skill) if skill.is_file() else "docs/scripting.md in the usd_chunks repository"


def _param_rows(params, depth=0):
    """(indent, script name, type, default, range or choices, label: tooltip) per param."""
    for p in params:
        pad = "  " * depth
        if p.kind in ("group", "list"):
            note = (
                f"list of rows: {p.name}=[{{param: value, ...}}, ...], one dict per row; "
                f"one row's param alone as {p.name}.<i>.<param>"
                if p.kind == "list" else "group"
            )
            yield pad, f"[{p.name}]", "", "", "", f"{p.label} ({note})"
            yield from _param_rows(p.children, depth + 1)
            continue
        if p.items:
            joined = "|".join(p.items)
            span = joined if len(joined) <= 40 else "|".join(p.items[:4]) + "|..."
        elif p.min_value is not None or p.max_value is not None:
            lo = "" if p.min_value is None else f"{p.min_value:g}"
            hi = "" if p.max_value is None else f"{p.max_value:g}"
            span = f"{lo}..{hi}"
        else:
            span = ""
        tip = p.tooltip.splitlines()[0] if p.tooltip else ""
        text = f"{p.label}: {tip}" if tip else p.label
        yield pad, p.name, p.kind, repr(p.default), span, text[:100]


def cmd_show(args):
    _utf8_stdout()
    registry = _catalog_registry()
    found = chunk_catalog.describe(registry, args.name)
    if found is None:
        near = [e.name for e in chunk_catalog.entries(registry, args.name)][:8]
        print(f"No chunk called {args.name!r}." + (f" Matching: {', '.join(near)}" if near else ""))
        sys.exit(1)
    entry, description, variants, params = found
    if args.json:
        print(json.dumps({"entry": vars(entry), "description": description,
                          "menu_entries": [vars(v) for v in variants],
                          "params": [asdict(p) for p in params]}, indent=1, default=str))
        return
    if entry.presets is not None:
        sets = ", ".join(f"{k}={v!r}" for k, v in entry.presets.items())
        print(f"{entry.name} -- a menu entry of {entry.module} ({entry.category}), with {sets}")
    else:
        print(f"{entry.name} ({entry.category})")
    print(f"  scene.create.{entry.name}(input, param=value, ...)")
    print()
    print("\n".join(f"  {line}" for line in (description or "(no description)").splitlines()))
    if variants:
        print("\nMenu entries (the same chunk with params preset; create them by name):")
        for v in variants[:_MANY_ENTRIES]:
            print(f"  {v.name:<20} " + ", ".join(f"{k}={val!r}" for k, val in (v.presets or {}).items()))
        if len(variants) > _MANY_ENTRIES:
            print(f"  ... and {len(variants) - _MANY_ENTRIES} more: usdchunks list <word>")
    print("\nParams (script name, type, default, range or choices, label):")
    rows = list(_param_rows(params))
    width = max((len(pad + name) for pad, name, *_ in rows), default=10)
    for pad, name, kind, default, span, text in rows:
        if not kind:
            print(f"  {pad}{name} {text}")
            continue
        print(f"  {(pad + name):<{width}}  {kind:<9} {default:<16} {span:<16} {text}".rstrip())


def cmd_validate(args):
    stage = Usd.Stage.Open(args.usda)
    if not stage:
        print(f"Failed to open: {args.usda}")
        sys.exit(1)

    # Registry only: validating reads the chunk modules, it does not load or cook the network.
    scene = Scene(stage, "cli", sync=False)
    registry = scene.registry

    print(f"Validating: {stage.GetRootLayer().identifier}")
    print()

    missing = set()
    passed = 0
    total = 0
    for prim in stage.Traverse():
        if prim.GetTypeName() != chunk_naming.CHUNK_PRIM_TYPENAME:
            continue
        chunk_type = prim.GetAttribute("info:chunkType").Get()
        if not chunk_type:
            continue
        total += 1
        mod = registry.get_chunk_module(str(chunk_type))
        if mod:
            passed += 1
            if args.verbose:
                print(f"  {GREEN}ok{RESET}  {prim.GetName()} ({chunk_type})")
        else:
            missing.add(str(chunk_type))
            if args.verbose:
                print(f"  {RED}!!{RESET}  {prim.GetName()} ({chunk_type})")
            # TODO: offer to install missing chunk type

    if args.verbose:
        print()

    failed = total - passed
    if missing:
        print(f"{RED}Network will not work as expected due to missing chunk types.")
        print(f"Please make sure the following chunk types are installed before using this network:{RESET}")
        print()
        for t in sorted(missing):
            print(f"  {RED}{t}{RESET}")
        print()
        print(f"{RED}{failed} chunk{'s' if failed != 1 else ''} failed{RESET}, {passed} chunk{'s' if passed != 1 else ''} passed.")
        sys.exit(1)
    else:
        print(f"{GREEN}All {passed} chunk{'s' if passed != 1 else ''} passed. Network is working nicely!{RESET}")


def cmd_pin(args):
    if args.list:
        try:
            pins = pinmod.read_scene_lock(args.usda)
        except pinmod.PinError as exc:
            print(f"pin failed: {exc}")
            sys.exit(1)
        if not pins:
            print("No pins recorded (run `usdchunks pin` first).")
            return
        for chunk_type in sorted(pins):
            store, _, version = pins[chunk_type].partition(":")
            print(f"  {chunk_type:<24} {store}:{version}")
        print(f"\n{len(pins)} pinned chunk(s).")
        return

    try:
        result = pinmod.pin(
            args.usda, pin_dir=args.pin_dir, copy=args.copy, wrapper=args.wrapper
        )
    except pinmod.PinError as exc:
        print(f"pin failed: {exc}")
        sys.exit(1)

    for m in result.materialized:
        print(f"  {m.chunk_type:<24} {m.store}:{m.version:<10} {m.mode:<8} -> {m.path}")

    if result.problems:
        print()
        for p in result.problems:
            print(f"  {RED}!!{RESET} {p}")
        print()
        print(f"{RED}{len(result.problems)} unresolved -- lock not written.{RESET}")
        sys.exit(1)

    if not result.materialized:
        print("No Chunk prims found -- nothing to pin.")
        return

    print()
    print(f"{GREEN}Pinned {len(result.materialized)} chunk(s){RESET} -> {result.pin_dir}")
    print(f"Lock recorded in {result.lock_location} ({result.lock_mode})")
    if result.hashes:
        print(f"Content hashes recorded for {len(result.hashes)} chunk(s) -- a re-pin "
              f"verifies the bytes, which a version alone cannot")
    if result.packages:
        print(f"Package versions recorded: "
              f"{', '.join(f'{n}=={v}' for n, v in sorted(result.packages.items()))}")

    if args.venv:
        python, installed, problems = pinmod.provision_venv(result.packages, args.venv)
        for problem in problems:
            print(f"{RED}{problem}{RESET}", file=sys.stderr)
        if problems:
            sys.exit(1)
        if python is None:
            print("No packages to install -- no venv created.")
        else:
            print()
            print(f"{GREEN}Environment ready{RESET} -> {python}")
            print(f"Installed: {', '.join(installed)}")
            print("Run against it with:")
            print(f"  {python} -m usd_chunks.cli run {args.usda}")
    print(f"Prepend to USD_CHUNK_DIRS:  {result.pin_dir}")


def _active_view(stage, scene):
    """The chunk that drives the stage: the file's Output, else a view, else None.

    The Output first: it is what the file delivers, and a view is only where someone last
    looked. A file saved before Output nodes falls back to its active view.

    A network is only worth cooking if something looks at it. The UI has always worked this
    way -- recook_focus establishes the active view and mutes everything the view cannot
    reach -- while headless ran every root and composed the lot, so `run` produced a
    superset of what the graph showed. A file with two experiments in it exported both.

    Views are found by walking every chunk, not by asking the top-level ones for theirs: a
    view can sit beside its branch at the top level rather than inside a group, and
    find_group_active_view only looks *within* the context it is given.

    A context that names an active view wins, since that is the slot the user last drove the
    graph from. Otherwise the first view found is as good an answer as any -- with one view
    in the file there is no choice to make.

    None means the file predates views or never set one, and the caller falls back to
    cooking everything, because silently producing nothing for every existing recipe would
    be worse than cooking too much.
    """
    if (output := find_output_chunk(stage, scene, None)) is not None:
        return output
    # Outermost first: a Reference or Payload hosting a saved network is a context holding
    # that network's own view, and walked first it drove the whole file -- muting every
    # layer outside the network, the host's own downstream chunks included.
    everything = sorted(_all_chunks(scene.get_chunks()),
                        key=lambda c: get_chunk_prim_path(c).count("/"))
    views = [c for c in everything if is_view_chunk(c)]
    if not views:
        return None

    for c in everything:
        if is_view_chunk(c):
            continue
        if not c.has_flag(chunk.ChunkFlags.GroupFlags.CONTEXT):
            continue
        if get_chunk_prim_path(views[0]).count("/") <= get_chunk_prim_path(c).count("/"):
            break  # a view sits further out than this context's: that one drives the file
        if (named := find_group_active_view(c, stage, scene)) is not None:
            return named
    return views[0]


_TRUE_WORDS = ("1", "true", "yes", "on")
_FALSE_WORDS = ("0", "false", "no", "off")


def _value_for(attr, raw):
    """A command-line string as a value of attr's own type, or None if it does not read as one.

    Covers what a param stores -- bool, numbers, strings, and float/int vectors written as
    comma-separated components. Anything else (arrays, matrices) is left to the loaded param.
    """
    type_name = attr.GetTypeName()
    scalar = str(type_name.scalarType)
    if type_name.isArray:
        return None
    text = raw.strip()
    try:
        if scalar == "bool":
            lowered = text.lower()
            return True if lowered in _TRUE_WORDS else False if lowered in _FALSE_WORDS else None
        if scalar in ("int", "int64", "uint", "uint64", "uchar"):
            return int(text)
        if scalar in ("float", "double", "half"):
            return float(text)
        if scalar in ("string", "token", "asset"):
            return raw
        python_class = getattr(type_name.type, "pythonClass", None)
        if python_class is not None and hasattr(python_class, "dimension"):
            parts = [float(p) for p in text.strip("()[]").split(",") if p.strip()]
            return python_class(*parts) if len(parts) == python_class.dimension else None
    except (TypeError, ValueError):
        return None
    return None


def _overrides_into_session(stage, overrides):
    """Author every override that names one saved param into the session layer, before load.

    So the network loads holding the new value and cooks once. Applied after load instead,
    as _parse_param_overrides does, it lands on a network load has already cooked with the
    saved values, and everything it reaches cooks a second time.

    Only an override this can be sure of goes in: ``CHUNK.param`` naming one chunk, whose
    param is saved in the file (a param at its default has no attribute) and not wired to
    another param (the wire would set it back at load), with a value that reads as the
    attribute's type. The rest are returned for _parse_param_overrides to apply after load:
    a bare name, which prefers a group's lofted interface over the param it drives, and
    params built only on demand -- in a collapsed group, during a chunk's own execute.

    Args:
        stage (Usd.Stage): The network, opened but not yet loaded
        overrides (list[str]): ``CHUNK.param=value`` and ``param=value`` strings

    Returns:
        list[str]: The overrides still to apply after load
    """
    chunks = [p for p in stage.Traverse() if p.GetTypeName() == chunk_naming.CHUNK_PRIM_TYPENAME]
    names = [p.GetName() for p in chunks]
    remaining = []
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        for override in overrides:
            label, sep, raw = override.partition("=")
            chunk_name, _, param = label.partition(".")
            candidates = [p for p in chunks if p.GetName() == chunk_name] if param and names.count(chunk_name) == 1 else []
            attrs = [
                a for p in candidates for a in p.GetAttributes()
                if a.GetName().startswith(chunk_naming.INPUTS_PREFIX)
                and not chunk_naming.is_exec_input_name(a.GetName())
                and a.HasAuthoredValue() and not a.HasAuthoredConnections()
                and chunk_batch.names_param(a.GetName(), param)
            ]
            value = _value_for(attrs[0], raw) if sep and len(attrs) == 1 else None
            if value is None:
                remaining.append(override)
                continue
            attrs[0].Set(value)
    return remaining


def cmd_run(args):
    # Overrides go into the session layer before load wherever they can (see
    # _overrides_into_session), and a batch run's values come as one (cmd_batch): read at
    # load, the network cooks once with them.
    if getattr(args, "timings", False):
        cook_timings.enabled = True
    session = getattr(args, "session", None)
    with cook_timings.phase("open stage"):
        root = Sdf.Layer.FindOrOpen(args.usda)
        stage = Usd.Stage.Open(root, Sdf.Layer.FindOrOpen(session)) if root and session else Usd.Stage.Open(args.usda)
    if not stage:
        print(f"Failed to open: {args.usda}")
        sys.exit(1)
    if args.params:
        args.params = _overrides_into_session(stage, args.params)
    # Before the load, so what an earlier session left in the overrides file is in the
    # first cook rather than an edit after it.
    session_live = live.LiveSession(stage, args.usda) if getattr(args, "live", False) else None

    # The scene's stage sync replaces a bare load_chunks: initialize loads the network
    # itself and, unlike a bare call, leaves the sync listening while it does.
    #
    # Params a chunk creates during its own execute -- a Light chunk's attribute group,
    # anything else built by show_attribute -- reach their real prim only through the
    # driven-attr connection that _sync_param_input authors off this sync. Without it
    # those params load carrying the right values and drive nothing: the light keeps its
    # schema defaults (intensity 1, a 1x1 rectangle) while the chunk says otherwise, and
    # the frame comes out lit by whatever fallback the delegate supplies.
    #
    # It also restores Edit Target selections, which a bare load_chunks skips -- so a
    # network whose chunks write into named layers now lands its edits where the file
    # says, rather than wherever the default target happened to be.
    with cook_timings.phase("load network"):
        scene = Scene(stage, "cli")

    if args.params:
        # Each override solves what it reaches, as the same edit in the UI does -- no
        # re-cook of every root afterwards. That re-cook only ever papered over the first
        # change after a load reading its param's saved value back over the new one
        # (fixed in ChunkStageSync._sync_param_input), and cooked the network twice over.
        with cook_timings.phase("param overrides"):
            _parse_param_overrides(scene, args.params)

    # Establish the active view, exactly as the UI does on focus. load_chunks has already
    # executed every dependency-free chunk by this point -- in the UI the view recook then
    # mutes whatever the view cannot reach, and headless simply never ran that step. Two
    # execution models for one file is the actual bug; this is the one line that ends it.
    #
    # After the overrides, so a sweep's `Wedge.current_index=i` is in effect before the cook
    # set runs.
    view = None if getattr(args, "all", False) else _active_view(stage, scene)
    if view is not None:
        with cook_timings.phase("cook for the view"):
            recook_focus(stage, scene, None, view)

    # Whatever a worker thread handed back during the cook (a download finishing) runs
    # here, on the main thread, before anything is written.
    chunk_scene.run_pending()

    # The solve has settled, so anything the network declared as an output on disk (an
    # Export USD chunk, ...) gets written now -- before --export/--printout report on the
    # composed stage.
    # Ordered from the stage's own connections, not DagNode depth: a param port wired in
    # the node graph authors an expression rather than an add_dependency edge, so depth
    # cannot tell that an exporter feeds the chunk that reads what it wrote.
    with cook_timings.phase("commit outputs"):
        chunk_scheduler.commit_all(
            scene.get_chunks(),
            order=lambda cs: commit_order_from_stage(cs, stage),
        )

    if getattr(args, "bake", False):
        for path in chunk_registry.ChunkLayerManager().save_cook_layers():
            print(f"Baked: {path}")

    if args.export:
        with cook_timings.phase("export"):
            if args.procedural:
                stage.Export(args.export)
            else:
                flatten_without_chunks(stage).Export(args.export)
            if getattr(args, "relative", False):
                chunk_utils.relative_asset_paths(args.export)
        print(f"Exported: {args.export}")

    if args.printout:
        print(flatten_without_chunks(stage).ExportToString())

    # On stderr, so --printout's stdout stays a clean layer.
    if cook_timings.enabled:
        print(cook_timings.report(), file=sys.stderr)

    if session_live is not None:
        _serve_live(stage, scene, session_live, args.usda)


def _serve_live(stage, scene, session, usda):
    """Keep the network up, cooking and committing again whenever a client sends values.

    The reload is all it takes to cook: the values arrive as stage edits, which the sync
    adopts and the solve recooks as far as they reach. What is left is what `run` does
    after its cook -- hand-offs from worker threads, then the outputs (a render) -- and
    the manifest, which also tells a waiting client its request is done.
    """
    session.publish(scene)
    print(f"Live: change params with `usdchunks send {usda} Chunk.param=value`. Ctrl+C ends.", flush=True)
    try:
        while True:
            time.sleep(live.POLL_SECONDS)
            error = None
            # The reload cooks: the sync recooks as the reloaded layer's edits arrive, so a
            # chunk failing on a value fails inside poll(), not only in the commit after it.
            try:
                if not session.poll():
                    continue
                chunk_scene.run_pending()
                chunk_scheduler.commit_all(
                    scene.get_chunks(),
                    order=lambda cs: commit_order_from_stage(cs, stage),
                )
            except Exception as exc:  # noqa: BLE001 - one bad edit must not end the session
                traceback.print_exc()
                error = str(exc)
            session.publish(scene, error=error)
            print(f"Live: request {session.request()} done.", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        session.close(scene)


def cmd_send(args):
    values = {}
    for item in args.params:
        name, sep, value = item.partition("=")
        if not sep:
            print(f"Invalid argument format: {item!r} — expected Chunk.param=value")
            sys.exit(1)
        values[name] = value
    try:
        if args.list:
            for name, entry in sorted(live.read_manifest(args.usda)["params"].items()):
                print(f"{name}  ({entry['type']})")
            return
        request = live.send(args.usda, values, reset=args.reset, wait=args.wait, timeout=args.timeout)
    except live.LiveError as exc:
        print(exc)
        sys.exit(1)
    print(f"Sent request {request}{' (done)' if args.wait else ''}.")


WEDGE_CHUNK_TYPE = "wedge"


def _find_wedge(stage):
    """The network's wedge prim, read straight off the stage.

    Deliberately not a chunk load: the parent only needs the wedge's name and count to plan
    the sweep, and loading the network here would pay the whole startup cost twice -- once
    to plan, once in the child that actually cooks.
    """
    for prim in stage.TraverseAll():
        if prim.GetTypeName() != chunk_naming.CHUNK_PRIM_TYPENAME:
            continue
        attr = prim.GetAttribute("info:chunkType")
        if attr and attr.Get() == WEDGE_CHUNK_TYPE:
            return prim
    return None


def _variant_dir(out, index):
    return os.path.join(out, f"variant_{index:05d}")


def _run_variant(usda, wedge_name, index, out, procedural, cook_all=False, params=()):
    """One variant, in its own process.

    A process per variant rather than a loop: the registry and stage-sync singletons hold
    the stage a network was loaded into, so a second load in one process returns the
    chunkgroup and none of its children -- variant 2 would silently re-export variant 1.
    It also stops one bad variant taking the batch down.

    params are `run` overrides applied to every variant alike -- a render device, a
    resolution -- ahead of the wedge's own index, so an override can never replace it.
    """
    directory = _variant_dir(out, index)
    os.makedirs(directory, exist_ok=True)
    cmd = [
        python_executable(), "-m", "usd_chunks.cli", "run", usda,
        *params,
        f"{wedge_name}.current_index={index}",
        "--export", os.path.join(directory, "variant.usda"),
    ]
    if procedural:
        cmd.append("--procedural")
    if cook_all:
        cmd.append("--all")
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return index, proc.returncode, proc.stdout.decode(errors="replace")


def cmd_sweep(args):
    stage = Usd.Stage.Open(args.usda)
    if not stage:
        print(f"Failed to open: {args.usda}")
        sys.exit(1)

    wedge = _find_wedge(stage)
    if wedge is None:
        print(f"{RED}No wedge chunk in {args.usda}{RESET}", file=sys.stderr)
        print("A sweep needs a Wedge chunk -- it is what says which params vary and how.")
        sys.exit(1)

    name = wedge.GetName()
    count_attr = wedge.GetAttribute("inputs:count")
    count = args.count or (int(count_attr.Get()) if count_attr and count_attr.Get() else 0)

    if args.only:
        indices = [int(i) for i in args.only.split(",") if i.strip() != ""]
    else:
        if count <= 0:
            print(f"{RED}Nothing to sweep: no --count and no count on {name}{RESET}", file=sys.stderr)
            sys.exit(1)
        indices = list(range(count))

    if args.verify:
        return _verify(args, name, indices)

    os.makedirs(args.out, exist_ok=True)

    if args.prepare_only:
        return _prepare_only(args, name, indices)

    if not (args.per_variant or args.procedural):
        reason = _sweep_batched(args, name, indices)
        if reason is None:
            return
        print(f"Not batching: {reason}. One process per variant instead.")

    print(f"Sweeping {len(indices)} variant(s) of {name} -> {args.out}")

    def one(i):
        return _run_variant(args.usda, name, i, args.out, args.procedural, args.all, args.params)

    failed = []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for index, code, output in pool.map(one, indices):
            if code == 0:
                print(f"  variant_{index:05d}")
            else:
                failed.append(index)
                print(f"{RED}  variant_{index:05d} FAILED{RESET}")
                print("\n".join(output.strip().splitlines()[-8:]))

    if failed:
        print(f"\n{RED}{len(failed)} of {len(indices)} failed{RESET}: {failed[:10]}")
        sys.exit(1)
    print(f"\n{GREEN}{len(indices)} variant(s){RESET} -> {args.out}")


def _absolute_chunk_dirs():
    """os.environ with relative USD_CHUNK_DIRS entries made absolute.

    A batch's runs work in the network's own directory (see cmd_batch), and a relative entry
    means "relative to where the command was typed", so it is pinned down before the
    directory changes under it.
    """
    env = dict(os.environ)
    raw = env.get("USD_CHUNK_DIRS")
    if raw:
        env["USD_CHUNK_DIRS"] = os.pathsep.join(
            str(Path(entry).resolve()) for entry in raw.split(os.pathsep) if entry
        )
    return env


def _run_batch_item(cmd, anchor, env):
    """One batch run, in its own process: (return code, combined output)."""
    proc = subprocess.run(cmd, cwd=anchor, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return proc.returncode, proc.stdout.decode(errors="replace")


def cmd_batch(args):
    """Cook a network once per file a glob in it matches: plan here, cook in children.

    Planning reads the patterns off the stage and loads nothing (chunk_batch). Each run is a
    `run` of the same file in its own process, opened with a session layer holding the
    driver's file and every sink's value -- so it cooks what `run` would for that one file,
    Output, commit hooks and all, and cooks it once.
    """
    usda = Path(args.usda).resolve()
    anchor = usda.parent
    root_layer = Sdf.Layer.FindOrOpen(str(usda))
    if not root_layer:
        print(f"Failed to open: {args.usda}")
        sys.exit(1)

    # A glob from the command line can be the driver, so it goes onto the stage before
    # planning; every other override is handed to each run as typed.
    pattern_params, plain_params = chunk_batch.split_overrides(args.params)
    stage = Usd.Stage.Open(root_layer, Sdf.Layer.CreateAnonymous("batch-plan.usda"))
    try:
        applied = chunk_batch.apply_overrides(stage, pattern_params)
        plan = chunk_batch.build_plan(stage, anchor, output_pattern=args.export, driver=args.driver)
        committing = []
        if not args.export:
            # The network writes its own output, so something in it has to, somewhere
            # different every run -- checked before the first file is written.
            registry = Scene(stage, "cli", sync=False).registry
            committing = chunk_batch.committing_chunk_names(stage, registry)
            chunk_batch.check_commit_targets(committing, plan)
    except chunk_batch.BatchError as exc:
        print(f"{RED}batch: {exc}{RESET}")
        sys.exit(1)

    for line in applied:
        print(f"Set:    {line}")
    print(chunk_batch.format_plan(plan, args.usda))
    print()
    if args.dry_run:
        # The plan has been through every check a real batch makes; nothing is written.
        print("Dry run: nothing cooked or written.")
        return

    env = _absolute_chunk_dirs()
    total = len(plan.runs)
    sessions = tempfile.TemporaryDirectory(prefix="usdchunks-batch-")

    def command(i, run):
        # This run's values, read at load (see cmd_run): one cook, with them.
        session = os.path.join(sessions.name, f"run_{i:05d}.usda")
        chunk_batch.write_session(stage, run.values(plan.driver, anchor), session)
        cmd = [python_executable(), "-m", "usd_chunks.cli", "run", str(usda), *plain_params, "--session", session]
        if run.output:
            out = anchor / run.output
            # USD does not make folders on the way out, and a pattern names one that need
            # not exist yet. Likewise for the sinks of the chunks that write.
            out.parent.mkdir(parents=True, exist_ok=True)
            cmd += ["--export", str(out)]
        for site, value in run.sink_values:
            if site.chunk_name in committing:
                (anchor / value).parent.mkdir(parents=True, exist_ok=True)
        if args.procedural:
            cmd.append("--procedural")
        if args.relative:
            cmd.append("--relative")
        if args.all:
            cmd.append("--all")
        return cmd

    # Written up front, on this thread: the planning stage is not for sharing between threads.
    commands = [(i, run, command(i, run)) for i, run in enumerate(plan.runs, 1)]

    def one(item):
        i, run, cmd = item
        return i, run, *_run_batch_item(cmd, anchor, env)

    failed = []
    with sessions, ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for i, run, code, output in pool.map(one, commands):
            target = f" -> {run.output}" if run.output else ""
            if code == 0:
                print(f"  [{i}/{total}] {run.source}{target}")
            else:
                failed.append(run.source)
                print(f"{RED}  [{i}/{total}] {run.source} FAILED{RESET}")
                print("\n".join(output.strip().splitlines()[-8:]))

    if failed:
        print(f"\n{RED}{len(failed)} of {total} failed{RESET}: {failed[:10]}")
        sys.exit(1)
    print(f"\n{GREEN}Batch complete: {total} run{'s' if total != 1 else ''}.{RESET}")


def _open_for_sweep(args):
    """The network loaded as `run` loads it, overrides applied, and its active view."""
    scene = Scene.open(args.usda, "cli")
    if args.params:
        _parse_param_overrides(scene, args.params)
    stage = scene.get_stage()
    view = None if args.all else _active_view(stage, scene)
    return scene, view


def _prepare_only(args, name, indices):
    """A worker of a batched sweep: prepare these variants, commit nothing."""
    scene, view = _open_for_sweep(args)
    try:
        failed = sweepmod.prepare(scene, sweepmod.find_wedge(scene, name), indices, args.out, view)
    finally:
        scene.close()
    if failed:
        sys.exit(1)


def _sweep_batched(args, name, indices):
    """Prepare every variant in one load, stitch them into frames, commit once.

    See usd_chunks.sweep. --jobs splits the preparing between this process and workers;
    the commit is always one.

    Returns:
        None when the sweep is done, or why this network cannot be batched -- the caller
        then sweeps one process per variant.
    """
    started = time.perf_counter()
    scene, view = _open_for_sweep(args)
    try:
        consumers, reason = sweepmod.frame_consumers(_all_chunks(scene.get_chunks()), name)
        if reason:
            return reason
        wedge = sweepmod.find_wedge(scene, name)

        slices = [indices[i::max(1, args.jobs)] for i in range(max(1, args.jobs))]
        mine, others = slices[0], [s for s in slices[1:] if s]
        print(f"Sweeping {len(indices)} variant(s) of {name} -> {args.out}, batched")
        workers = [
            subprocess.Popen([
                python_executable(), "-m", "usd_chunks.cli", "sweep", args.usda, *args.params,
                "--out", args.out, "--only", ",".join(str(i) for i in part), "--prepare-only",
                *(["--all"] if args.all else []),
            ])
            for part in others
        ]
        failed = sweepmod.prepare(scene, wedge, mine, args.out, view)
        for worker, part in zip(workers, others):
            if worker.wait() != 0:
                failed.extend(i for i in part if not os.path.exists(sweepmod.variant_path(args.out, i)))
        prepared = time.perf_counter()

        done = [i for i in indices if i not in failed]
        if not done:
            print(f"{RED}Every variant failed{RESET}")
            sys.exit(1)
        stitched = os.path.join(args.out, sweepmod.STITCHED_NAME)
        problems = sweepmod.stitch({i: sweepmod.variant_path(args.out, i) for i in done}, stitched)
        if problems:
            for problem in problems[:5]:
                print(f"  {problem}")
            return f"the variants cannot be stitched into frames ({len(problems)} difference(s) above)"

        committed = sweepmod.commit_frames(scene, consumers, stitched, done)
        finished = time.perf_counter()
    finally:
        scene.close()

    print(f"\nPrepared {len(done)} variant(s) in {prepared - started:.1f}s, then "
          f"{', '.join(c.name for c in committed) or 'nothing'} over all of them as frames "
          f"in {finished - prepared:.1f}s")
    if failed:
        print(f"{RED}{len(failed)} of {len(indices)} failed{RESET}: {sorted(failed)[:10]}")
        sys.exit(1)
    print(f"{GREEN}{len(done)} variant(s){RESET} -> {args.out}")
    return None


def _verify(args, name, indices):
    """Regenerate variants in a scratch directory and diff them against what is on disk.

    Not a test -- the artifact. "Ship the recipe, not the dataset" is a claim a customer
    will check, and this is the report that answers it.

    Each variant regenerates in a *fresh process*, which is the only version worth running:
    re-cooking in one interpreter reuses the first run's state, and a value seeded off the
    built-in hash would agree with itself all day because PYTHONHASHSEED is fixed for the
    life of a process.
    """
    missing = [
        i for i in indices
        if not os.path.exists(os.path.join(_variant_dir(args.out, i), "variant.usda"))
    ]
    if missing:
        print(f"{RED}Not swept yet{RESET}: {missing[:10]}")
        sys.exit(1)

    print(f"Verifying {len(indices)} variant(s) of {name} against {args.out}")
    with tempfile.TemporaryDirectory(prefix="usdchunks-verify-") as scratch:
        def one(i):
            return _run_variant(args.usda, name, i, scratch, args.procedural, args.all, args.params)

        identical, differing, errored = [], [], []
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            for index, code, output in pool.map(one, indices):
                if code != 0:
                    errored.append(index)
                    print(f"{RED}  variant_{index:05d} ERRORED{RESET}")
                    print("\n".join(output.strip().splitlines()[-8:]))
                    continue
                a = os.path.join(_variant_dir(args.out, index), "variant.usda")
                b = os.path.join(_variant_dir(scratch, index), "variant.usda")
                # shallow=False: matching size and mtime is exactly what two runs of a
                # deterministic pipeline produce, so a shallow compare would pass having
                # read no content at all.
                if filecmp.cmp(a, b, shallow=False):
                    identical.append(index)
                else:
                    differing.append(index)
                    print(f"{RED}  variant_{index:05d} DIFFERS{RESET}")

    print(f"\nidentical : {len(identical)}/{len(indices)}")
    if errored:
        print(f"errored   : {len(errored)} {errored[:10]}")
    if differing or errored:
        print(f"\n{RED}RESULT: not reproducible{RESET}")
        sys.exit(1)
    print(f"\n{GREEN}RESULT: every variant regenerated byte-identical in a fresh process.{RESET}")


def cmd_deps(args):
    """Report chunks whose dependencies are undeclared or unpinned.

    Both matter for the same reason, and it is not really about installing. A recipe claims
    that regenerating it reproduces the dataset; a dependency that is undeclared never
    reaches the run's manifest, and one that is unpinned can move underneath an unchanged
    recipe. Either way two runs can disagree while every recorded fact matches -- which is
    worse than recording nothing, because it reads as authoritative.
    """
    registry = Scene(host_name="cli", sync=False).registry
    modules = registry.get_chunk_modules()
    siblings = {m.module_name.split(".")[-1] for m in modules}

    undeclared = [(m.module_name, u) for m in modules
                  if (u := chunk_registry.undeclared_imports(m, siblings))]
    unconstrained = [(m.module_name, r) for m in modules
                     if (r := chunk_registry.unconstrained_requirements(m))]
    conflicts = chunk_registry.conflicting_requirements(modules)

    print(f"Checked {len(modules)} chunk module(s)")
    print()
    if undeclared:
        print(f"{RED}Undeclared third-party imports{RESET} -- imported but not in config().requirements:")
        for name, imports in sorted(undeclared):
            print(f"  {name:42s} {', '.join(imports)}")
        print()
    if unconstrained:
        print(f"{RED}Requirements with no version constraint{RESET} -- a recipe cannot record "
              f"what a chunk left open:")
        for name, reqs in sorted(unconstrained):
            print(f"  {name:42s} {', '.join(reqs)}")
        print()
    if conflicts:
        print(f"{RED}Installed version does not satisfy what a chunk asked for{RESET} -- "
              f"there is only one of each package in a process:")
        for package, claims in sorted(conflicts.items()):
            for module_name, specifier, installed in sorted(claims):
                print(f"  {module_name:42s} wants {package}{specifier}, installed {installed}")
        print()
    if not undeclared and not unconstrained and not conflicts:
        print(f"{GREEN}Every chunk declares a version range, and the installed versions "
              f"satisfy them.{RESET}")
        return
    if args.strict:
        sys.exit(1)


def cmd_plugin_path(args):
    """Print the directory of a host integration: the one to put on PXR_PLUGINPATH_NAME for
    usdview, the Houdini package's hpath for houdini (hosts/houdini/install.py writes it).

    Resolved from the installed package rather than the source tree, so the printed path
    is correct whether usd_chunks was pip-installed or is being run from a checkout.
    """
    path = Path(files("usd_chunks.hosts")) / args.host
    if not path.is_dir():
        print(f"{RED}plugin directory not found: {path}{RESET}", file=sys.stderr)
        sys.exit(1)
    print(path)


def main():
    configure_logging()
    parser = ArgumentParser(
        prog="usdchunks",
        description="Execute and inspect USD chunk networks serialized as .usda files.",
        epilog=(
            "Examples:\n"
            "  usdchunks run setup.usda\n"
            "  usdchunks run setup.usda -o result.usda\n"
            "  usdchunks run setup.usda -p\n"
            "  usdchunks run setup.usda MyChunk.primpath_location=/geo\n"
            "  usdchunks run setup.usda --live\n"
            "  usdchunks send setup.usda MyChunk.size=2 --wait\n"
            "  usdchunks batch setup.usda -o 'out/*.usda'\n"
            "  usdchunks list\n"
            "  usdchunks list scatter\n"
            "  usdchunks show mesh\n"
            "  usdchunks info setup.usda\n"
            "  usdchunks validate setup.usda -v\n"
            "  usdchunks plugin-path\n"
        ),
        formatter_class=RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser(
        "run",
        help="Execute a chunk network from a .usda file",
        description=(
            "Load and execute a USD chunk network. Each chunk's execute() is called in\n"
            "topological order (DAG solve). Param overrides are applied after load.\n"
            "Use --export or --printout to inspect the composed result.\n\n"
            "Only the network the file's active view reaches is cooked -- the same rule the\n"
            "UI applies on focus, so `run` produces what the graph shows rather than a\n"
            "superset of it. A file with no view cooks everything, and --all forces that."
        ),
        epilog=(
            "Examples:\n"
            "  usdchunks run setup.usda\n"
            "  usdchunks run setup.usda -o result.usda\n"
            "  usdchunks run setup.usda -p\n"
            "  usdchunks run setup.usda MyChunk.primpath_location=/geo -o result.usda\n"
        ),
        formatter_class=RawDescriptionHelpFormatter,
    )
    run_parser.add_argument("usda", help="Path to the .usda chunk network file")
    run_parser.add_argument("params", nargs="*", help="Param overrides: ChunkName.param=value")
    run_parser.add_argument(
        "--export",
        "-o",
        metavar="FILE",
        help="Flatten and export the result to FILE, without the Chunk prims that built it",
    )
    run_parser.add_argument(
        "--procedural",
        action="store_true",
        help="Keep the Chunk prims in --export, so the network reloads and re-cooks",
    )
    run_parser.add_argument(
        "--printout", "-p", action="store_true", help="Print the flattened stage to stdout, Chunk prims stripped"
    )
    run_parser.add_argument(
        "--bake",
        action="store_true",
        help="Write each cook layer's result to its file (layout.cook.usda beside the "
             "layout.usda that lists it), so the bake is there without usd_chunks",
    )
    run_parser.add_argument(
        "--all",
        action="store_true",
        help="Cook every network in the file, not just the one the active view reaches, "
             "for a file whose side branches are meant to run",
    )
    # A layer of param values to open the network with, as its session layer: how a batch
    # hands each run its file (cmd_batch).
    run_parser.add_argument("--session", metavar="LAYER", help=SUPPRESS)
    run_parser.add_argument(
        "--relative", action="store_true",
        help="Write --export's asset paths relative to the exported file, so it moves with what "
             "it references (paths on another drive stay absolute)",
    )
    run_parser.add_argument(
        "--timings", action="store_true",
        help="Print how long each phase and each chunk's cook took, slowest first, on stderr "
             "(the same as USD_CHUNKS_TIMINGS=1)",
    )
    run_parser.add_argument(
        "--live", action="store_true",
        help="Stay up after the run and cook (and render) again whenever `usdchunks send` "
             "changes a param",
    )

    send_parser = sub.add_parser(
        "send",
        help="Change params on a network running with `run --live`",
        description=(
            "Write param values into the live session's overrides file (NET.live.usda beside\n"
            "the network). The session reloads it and cooks what the values reach, as an\n"
            "edit in the UI would. Values stay in the file until --reset."
        ),
        epilog=(
            "Examples:\n"
            "  usdchunks send setup.usda Fill.intensity=8\n"
            "  usdchunks send setup.usda Fill.intensity=8 Sky.intensity=2 --wait\n"
            "  usdchunks send setup.usda --list\n"
        ),
        formatter_class=RawDescriptionHelpFormatter,
    )
    send_parser.add_argument("usda", help="Path to the .usda chunk network the session runs")
    send_parser.add_argument("params", nargs="*", help="Values: ChunkName.param=value")
    send_parser.add_argument("--reset", action="store_true", help="Clear earlier values first")
    send_parser.add_argument("--wait", action="store_true", help="Return once the session has cooked them")
    send_parser.add_argument("--timeout", type=float, default=None, help="Seconds --wait waits at most")
    send_parser.add_argument("--list", action="store_true", help="List the params the session can set")

    list_parser = sub.add_parser(
        "list",
        help="List the installed chunks (USD_CHUNK_DIRS), or those matching a word",
        description="One line per chunk and menu entry: its script name, category and what it does.",
    )
    list_parser.add_argument("query", nargs="?", default="", help="Only chunks whose name or description has this")
    list_parser.add_argument("--json", action="store_true", help="Print JSON instead")

    show_parser = sub.add_parser(
        "show",
        help="Show one chunk in full: description, menu entries and every param",
        description="What a chunk does and every param a script can set: name, type, default, range.",
    )
    show_parser.add_argument("name", help="The chunk's script name, as `usdchunks list` prints it")
    show_parser.add_argument("--json", action="store_true", help="Print JSON instead")

    info_parser = sub.add_parser(
        "info",
        help="Show info and exposed params for ChunkGroups in a .usda file",
        description="Print metadata and exposed params for each ChunkGroup in the network.",
    )
    info_parser.add_argument("usda", help="Path to the .usda chunk network file")

    validate_parser = sub.add_parser(
        "validate",
        help="Check all chunk types resolve before running",
        description=(
            "Verify that every chunk type referenced in the network is registered\n"
            "and loadable. Exits with code 1 if any chunk type is missing."
        ),
        formatter_class=RawDescriptionHelpFormatter,
    )
    validate_parser.add_argument("usda", help="Path to the .usda chunk network file")
    validate_parser.add_argument("-v", "--verbose", action="store_true", help="Show result for every chunk prim")

    pin_parser = sub.add_parser(
        "pin",
        help="Freeze a network to exact store versions (mdl-pin style)",
        description=(
            "Resolve every chunk in the network to a concrete version from a\n"
            "registered store, materialize those exact impls into a flat pin/\n"
            "folder, and record the lock in the entry layer's customLayerData.\n"
            "Read-only on source prims. Re-pinning a scene that already carries a\n"
            "lock restores exactly those versions (vendor handoff)."
        ),
        epilog=(
            "Examples:\n"
            "  usdchunks pin myshot.usda\n"
            "  usdchunks pin myshot.usda --list       # show recorded pins, resolve nothing\n"
            "  usdchunks pin myshot.usda --pin-dir /farm/tmp/pin\n"
            "  usdchunks pin myshot.usda --copy       # portable/sealed bundle\n"
            "\n"
            "Then run against the pinned versions:\n"
            "  USD_CHUNK_DIRS=./pin:$USD_CHUNK_DIRS usdchunks run myshot.usda\n"
        ),
        formatter_class=RawDescriptionHelpFormatter,
    )
    pin_parser.add_argument("usda", help="Path to the .usda chunk network file")
    pin_parser.add_argument("--list", action="store_true", help="List the recorded pins (read-only) and exit")
    pin_parser.add_argument("--pin-dir", metavar="DIR", default=None, help="Where to materialize impls (default ./pin)")
    pin_parser.add_argument("--copy", action="store_true", help="Copy impls instead of symlinking (portable/sealed)")
    pin_parser.add_argument("--wrapper", action="store_true", help="Always write the lock to a wrapper layer, never in place")
    pin_parser.add_argument(
        "--venv", metavar="DIR", default=None,
        help="Create a venv at DIR and install the locked package versions into it. "
             "System site packages stay visible, so the host's OpenUSD and Qt still "
             "import -- only pip's share is pinned",
    )

    sweep_parser = sub.add_parser(
        "sweep",
        help="Run every variant of a network's wedge",
        description=(
            "Cook a network once per wedge index and export each result.\n\n"
            "Every value a wedge produces is a pure function of (seed, index, range name),\n"
            "so variants are independent: --only regenerates one without producing the ones\n"
            "before it, --jobs runs them concurrently, and --verify proves a rerun matches.\n\n"
            "Batched by default: the network is loaded once and every variant cooked and\n"
            "exported in turn, then the variants are stitched into one stage, variant i at\n"
            "frame i, and the commit hooks (Render, KITTI Writer) run once over all of them.\n"
            "A network that cannot be batched -- a commit that is not per frame, such as\n"
            "Export USD, or variants that differ in more than attribute values -- falls back\n"
            "to one `run` per variant and says why. --per-variant forces that."
        ),
        epilog=(
            "Examples:\n"
            "  usdchunks sweep drop.usda --out ./data\n"
            "  usdchunks sweep drop.usda --out ./data --count 500 --jobs 4\n"
            "  usdchunks sweep drop.usda --out ./data --only 42,1337\n"
            "  usdchunks sweep drop.usda --out ./data --verify --only 0,7,19\n"
            "  usdchunks sweep drop.usda --out ./data vray_render_device=7\n"
        ),
        formatter_class=RawDescriptionHelpFormatter,
    )
    sweep_parser.add_argument("usda", help="Path to the .usda chunk network file")
    sweep_parser.add_argument(
        "params", nargs="*",
        help="Param overrides for every variant, as for run: ChunkName.param=value",
    )
    sweep_parser.add_argument(
        "--out", required=True, metavar="DIR", help="Where variant_NNNNN/ directories are written"
    )
    sweep_parser.add_argument(
        "--per-variant",
        action="store_true",
        help="One `run` process per variant, instead of preparing them all in one load and "
             "committing (rendering, writing) once over all of them as frames",
    )
    sweep_parser.add_argument("--prepare-only", action="store_true", help=SUPPRESS)
    sweep_parser.add_argument(
        "--count", type=int, default=0, help="How many variants (default: the wedge's own count)"
    )
    sweep_parser.add_argument("--only", metavar="I,J,K", help="Sweep just these indices")
    sweep_parser.add_argument(
        "--jobs", type=int, default=1,
        help="Variants to run concurrently. Independent by construction, but the ceiling is "
             "usually the renderer rather than the cores -- measure before raising it",
    )
    sweep_parser.add_argument(
        "--procedural", action="store_true", help="Keep Chunk prims in each export"
    )
    sweep_parser.add_argument(
        "--all", action="store_true",
        help="Cook every network per variant, not just what the active view reaches",
    )
    sweep_parser.add_argument(
        "--verify", action="store_true",
        help="Regenerate the variants in fresh processes and diff them against --out",
    )

    batch_parser = sub.add_parser(
        "batch",
        help="Run a chunk network once per file matched by a glob",
        description=(
            "Cook one network once per file a glob in it matches, one `run` per file.\n\n"
            "Every string param holding a glob (*, ?, [...]) is a pattern. The one that\n"
            "matches existing files drives the batch; every other pattern (and -o) is filled\n"
            "from the driver's wildcards, left to right, so subfolders carry over:\n"
            "  Reference.file_path = assets/**/*.usdc    ExportUSD.output_file = out/**/*.usda\n"
            "  assets/props/chair.usdc  ->  out/props/chair.usda\n\n"
            "Patterns are relative to the network's own folder, not the shell's. A glob given\n"
            "as an override counts as one, so a network can be pointed at another folder\n"
            "without editing it. Without -o something in the network has to write (Export\n"
            "USD, Render) to a path with a wildcard in it, or every run would overwrite the\n"
            "same file -- that is checked before the first run."
        ),
        epilog=(
            "Examples:\n"
            "  usdchunks batch convert.usda\n"
            "  usdchunks batch convert.usda -o 'out/*.usda'\n"
            "  usdchunks batch convert.usda Reference.file_path='assets/**/*.usdc' -o 'out/**/*.usda'\n"
            "  usdchunks batch convert.usda --driver Reference.file_path --jobs 4\n"
        ),
        formatter_class=RawDescriptionHelpFormatter,
    )
    batch_parser.add_argument("usda", help="Path to the .usda chunk network file")
    batch_parser.add_argument(
        "params", nargs="*", help="Param overrides for every run, as for run: ChunkName.param=value"
    )
    batch_parser.add_argument(
        "--export", "-o", metavar="PATTERN",
        help="Export each run's result here (a pattern filled from the driver's wildcards), "
             "flattened without Chunk prims as `run -o` does",
    )
    batch_parser.add_argument(
        "--driver", metavar="CHUNK.param", help="Which pattern drives the batch, when more than one matches files"
    )
    batch_parser.add_argument("--jobs", type=int, default=1, help="Runs to cook concurrently")
    batch_parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the plan -- the folder patterns resolve against, every file matched and "
             "where each result goes -- and stop, cooking and writing nothing",
    )
    batch_parser.add_argument("--procedural", action="store_true", help="Keep Chunk prims in each -o export")
    batch_parser.add_argument(
        "--relative", action="store_true",
        help="Write each -o export's asset paths relative to it, as run --relative does",
    )
    batch_parser.add_argument(
        "--all", action="store_true", help="Cook every network per run, not just what the Output reaches"
    )

    deps_parser = sub.add_parser(
        "deps",
        help="Report chunks with undeclared or unpinned dependencies",
        description=(
            "Scan every registered chunk module for third-party imports it does not\n"
            "declare in config().requirements, for requirements declared with no version\n"
            "constraint, and for installed versions that do not satisfy what a chunk asked\n"
            "for.\n\n"
            "These break a recipe's manifest rather than an install: an undeclared\n"
            "dependency's version is never recorded, an unconstrained one can move under an\n"
            "unchanged recipe, and a conflict between two chunks is settled today by\n"
            "whichever install landed last -- silently.\n\n"
            "Chunks declare ranges, not exact pins. Exact pins do not compose, and\n"
            "resolving two of them would mean editing somebody else's chunk; the exact\n"
            "version belongs in the recipe, recorded as what actually ran.\n\n"
            "Imports are read statically, which is complete here only because chunks import\n"
            "at module top level and never inside a function body."
        ),
        epilog="Examples:\n  usdchunks deps\n  usdchunks deps --strict\n",
        formatter_class=RawDescriptionHelpFormatter,
    )
    deps_parser.add_argument(
        "--strict", action="store_true", help="Exit non-zero if anything is reported (for CI)"
    )

    plugin_path_parser = sub.add_parser(
        "plugin-path",
        help="Print the directory to add to PXR_PLUGINPATH_NAME",
        description=(
            "Print the installed host-integration directory, so the plugin can be\n"
            "registered without knowing where pip put the package."
        ),
        epilog=(
            "Examples:\n"
            "  usdchunks plugin-path\n"
            "  for /f \"delims=\" %i in ('usdchunks plugin-path') do set PXR_PLUGINPATH_NAME=%i   (Windows cmd)\n"
            '  export PXR_PLUGINPATH_NAME="$(usdchunks plugin-path)"    (POSIX)\n'
            "  usdchunks plugin-path --host houdini   (the Houdini package's hpath)\n"
        ),
        formatter_class=RawDescriptionHelpFormatter,
    )
    plugin_path_parser.add_argument(
        "--host", choices=["usdview", "houdini"], default="usdview", help="Which host integration (default: usdview)"
    )

    # Overrides are positionals after a positional, and argparse settles a trailing
    # nargs="*" the moment it reads the file name -- so `sweep net.usda --out d x=1`
    # rejected x=1 as unrecognised. Whatever is left that reads as name=value is an
    # override, wherever on the line it was written.
    args, extra = parser.parse_known_args()
    overrides = [a for a in extra if "=" in a and not a.startswith("-")]
    if extra != overrides or (overrides and not hasattr(args, "params")):
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    if overrides:
        args.params = list(args.params) + overrides

    if args.command == "run":
        cmd_run(args)
    elif args.command == "send":
        cmd_send(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "show":
        cmd_show(args)
    elif args.command == "info":
        cmd_info(args)
    elif args.command == "validate":
        cmd_validate(args)
    elif args.command == "pin":
        cmd_pin(args)
    elif args.command == "sweep":
        cmd_sweep(args)
    elif args.command == "batch":
        cmd_batch(args)
    elif args.command == "deps":
        cmd_deps(args)
    elif args.command == "plugin-path":
        cmd_plugin_path(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
