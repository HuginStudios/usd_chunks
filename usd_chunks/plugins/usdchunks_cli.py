"""
usdchunks CLI — headless execution of chunk networks serialized as .usda files.

Usage:
    usdchunks run my_setup.usda [ChunkName.param=value ...]
    usdchunks info my_setup.usda
"""

import sys
from argparse import ArgumentParser, RawDescriptionHelpFormatter
from importlib.resources import files
from pathlib import Path

from pxr import Usd, UsdUtils
from usd_chunks import chunk, chunk_registry, chunk_core, chunk_scheduler
from usd_chunks.plugins.chunk_usd import ChunkLayerManager, ChunkStageSync, commit_order_from_stage


# ANSI colors for CLI output
RED = "\033[31m"
GREEN = "\033[32m"
RESET = "\033[0m"


class HeadlessScene(chunk_core.SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage


def _all_chunks(chunks):
    """Yield every chunk recursively, including sub-chunks inside ChunkGroups."""
    for c in chunks:
        yield c
        sub = [p for p in c.get_my_params() if isinstance(p, chunk_core.ChunkBase)]
        yield from _all_chunks(sub)


def _re_execute_roots(chunks):
    """Call execute() on every root node (no dependencies), recursing into groups."""
    for c in chunks:
        sub = [p for p in c.get_my_params() if isinstance(p, chunk_core.ChunkBase)]
        if sub:
            _re_execute_roots(sub)
        elif not c.get_dependencies():
            c.execute()


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


def _flatten_without_chunks(stage):
    """Flatten the stage to a layer carrying the result but none of the recipe.

    A Chunk prim left in a delivered file is not inert: open it anywhere usd_chunks is
    installed and the network reloads and re-cooks, overriding the very geometry the
    file was exported to carry. Stripping them is what makes the output plain USD that
    anyone can open with nothing installed.

    FlattenLayerStack rather than Stage.Export, because it keeps references as
    references where Export inlines the referenced asset outright -- which stops
    mattering only while a network builds all its own geometry.

    Shallowest path first, so removing a parent Chunk takes its children with it and
    the later lookup for those children simply finds nothing.
    """
    flat = UsdUtils.FlattenLayerStack(stage)
    tmp = Usd.Stage.Open(flat)
    chunk_paths = sorted(
        (p.GetPath() for p in tmp.TraverseAll() if p.GetTypeName() == "Chunk"),
        key=lambda p: p.pathElementCount,
    )
    for path in chunk_paths:
        spec = flat.GetPrimAtPath(path)
        if not spec:
            continue  # already removed as child of a shallower Chunk
        parent = spec.nameParent
        if parent:
            del parent.nameChildren[spec.name]
        else:
            del flat.pseudoRoot.nameChildren[spec.name]
    return flat


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
            if not param:
                print(f"Param not found: {name!r}")
                continue
        else:
            chunk_name, param_path = name.split(".", 1)
            c = chunk_map.get(chunk_name)
            if not c:
                print(f"Chunk not found: {chunk_name!r}")
                continue
            param = c.get_param(param_path, chunk.is_param)
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
        if prim.GetTypeName() != "Chunk":
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

def cmd_validate(args):
    stage = Usd.Stage.Open(args.usda)
    if not stage:
        print(f"Failed to open: {args.usda}")
        sys.exit(1)

    scene = HeadlessScene(stage)
    registry = chunk_registry.ChunkRegistry()
    registry.initialize(scene, "cli")

    print(f"Validating: {stage.GetRootLayer().identifier}")
    print()

    missing = set()
    passed = 0
    total = 0
    for prim in stage.Traverse():
        if prim.GetTypeName() != "Chunk":
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
    from usd_chunks import pin as pinmod

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
    print(f"Prepend to USD_CHUNK_DIRS:  {result.pin_dir}")


def cmd_run(args):
    stage = Usd.Stage.Open(args.usda)
    if not stage:
        print(f"Failed to open: {args.usda}")
        sys.exit(1)

    scene = HeadlessScene(stage)
    chunk_registry.ChunkRegistry().initialize(scene, "cli")
    ChunkLayerManager().initialize(stage)
    # This replaces a bare load_chunks: initialize loads the network itself and, unlike a
    # bare call, leaves the sync listening while it does.
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
    ChunkStageSync().initialize(stage, scene)

    if args.params:
        _parse_param_overrides(scene, args.params)
        _re_execute_roots(scene.get_chunks())

    # The solve has settled, so anything the network declared as an output on disk (an
    # Export USD chunk, ...) gets written now — before --export/--printout report on the
    # composed stage.
    # Ordered from the stage's own connections, not DagNode depth: a param port wired in
    # the node graph authors an expression rather than an add_dependency edge, so depth
    # cannot tell that an exporter feeds the chunk that reads what it wrote.
    chunk_scheduler.commit_all(
        scene.get_chunks(),
        order=lambda cs: commit_order_from_stage(cs, stage),
    )

    if args.export:
        if args.procedural:
            stage.Export(args.export)
        else:
            _flatten_without_chunks(stage).Export(args.export)
        print(f"Exported: {args.export}")

    if args.printout:
        print(_flatten_without_chunks(stage).ExportToString())


def cmd_plugin_path(args):
    """Print the directory to put on PXR_PLUGINPATH_NAME for a given host.

    Resolved from the installed package rather than the source tree, so the printed path
    is correct whether usd_chunks was pip-installed or is being run from a checkout.
    """
    host_dirs = {"usdview": "chunk_usdview", "kraken": "chunk_kraken", "houdini": "chunk_houdini"}
    path = Path(files("usd_chunks.plugins")) / host_dirs[args.host]
    if not path.is_dir():
        print(f"{RED}plugin directory not found: {path}{RESET}", file=sys.stderr)
        sys.exit(1)
    print(path)


def main():
    parser = ArgumentParser(
        prog="usdchunks",
        description="Execute and inspect USD chunk networks serialized as .usda files.",
        epilog=(
            "Examples:\n"
            "  usdchunks run setup.usda\n"
            "  usdchunks run setup.usda -o result.usda\n"
            "  usdchunks run setup.usda -p\n"
            "  usdchunks run setup.usda MyChunk.primpath_location=/geo\n"
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
            "Use --export or --printout to inspect the composed result."
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
            "  set PXR_PLUGINPATH_NAME=%usdchunks plugin-path%          (Windows)\n"
            '  export PXR_PLUGINPATH_NAME="$(usdchunks plugin-path)"    (POSIX)\n'
        ),
        formatter_class=RawDescriptionHelpFormatter,
    )
    plugin_path_parser.add_argument(
        "--host", choices=["usdview", "kraken", "houdini"], default="usdview", help="Which host integration (default: usdview)"
    )

    args = parser.parse_args()

    if args.command == "run":
        cmd_run(args)
    elif args.command == "info":
        cmd_info(args)
    elif args.command == "validate":
        cmd_validate(args)
    elif args.command == "pin":
        cmd_pin(args)
    elif args.command == "plugin-path":
        cmd_plugin_path(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
