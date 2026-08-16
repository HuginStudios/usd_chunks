"""
usdchunks CLI — headless execution of chunk networks serialized as .usda files.

Usage:
    usdchunks run my_setup.usda [ChunkName.param=value ...]
    usdchunks batch my_setup.usda -o 'out/*.usda'
    usdchunks info my_setup.usda
"""

import os
import sys
from argparse import ArgumentParser, RawDescriptionHelpFormatter
from importlib.resources import files
from pathlib import Path

from pxr import Sdf, Usd, UsdUtils
from usd_chunks import chunk, chunk_registry, chunk_core, chunk_scheduler
from usd_chunks.plugins import chunk_batch
from usd_chunks.plugins.chunk_usd import load_chunks, ChunkLayerManager


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


def _parse_param_overrides(chunks, overrides):
    chunk_map = {c.name: c for c in _all_chunks(chunks)}
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
            for c in _all_chunks(chunks):
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

        types = chunk.ChunkFlags.ParamTypeToTypes(param.evaluate_type())
        if types:
            param.value = types[0](value)


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
    load_chunks(stage, scene)

    if args.params:
        _parse_param_overrides(scene.get_chunks(), args.params)
        _re_execute_roots(scene.get_chunks())

    # The solve has settled, so anything the network declared as an output on disk (an
    # Export USD chunk, ...) gets written now — before --export/--printout report on the
    # composed stage.
    chunk_scheduler.commit_all(scene.get_chunks())

    if args.export:
        stage.Export(args.export)
        print(f"Exported: {args.export}")

    if args.printout:
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
        print(flat.ExportToString())


def _pin_chunk_dirs_to_cwd():
    """Rewrite relative USD_CHUNK_DIRS entries as absolute ones.

    A batch runs from the network's own directory (see cmd_batch), while chunk module files
    are opened lazily — long after that move. A relative entry means "relative to where the
    user typed the command", so it has to be pinned down before the directory changes under it.
    """
    raw = os.environ.get("USD_CHUNK_DIRS")
    if not raw:
        return
    os.environ["USD_CHUNK_DIRS"] = os.pathsep.join(
        str(Path(entry).resolve()) for entry in raw.split(os.pathsep) if entry
    )


def cmd_batch(args):
    usda = Path(args.usda)
    anchor = usda.resolve().parent
    root_layer = Sdf.Layer.FindOrOpen(str(usda))
    if not root_layer:
        print(f"Failed to open: {args.usda}")
        sys.exit(1)

    # Glob overrides have to be on the stage before planning (one may be the driver);
    # everything else waits until the chunks exist, where a param is present whether or not
    # USD ever authored it and its real type is known.
    pattern_params, plain_params = chunk_batch.split_overrides(args.params)

    plan_stage = Usd.Stage.Open(root_layer, Sdf.Layer.CreateAnonymous("batch-plan.usda"))
    try:
        overrides = chunk_batch.apply_overrides(plan_stage, pattern_params)
        plan = chunk_batch.build_plan(plan_stage, anchor, output_pattern=args.export, driver=args.driver)
    except chunk_batch.BatchError as exc:
        print(f"{RED}batch: {exc}{RESET}")
        sys.exit(1)

    for line in overrides:
        print(f"Set:    {line}")
    print(chunk_batch.format_plan(plan, args.usda))
    print()

    # Discover chunk modules once, and before the chdir below.
    _pin_chunk_dirs_to_cwd()
    registry = chunk_registry.ChunkRegistry()
    registry.initialize(HeadlessScene(plan_stage), "cli")

    total = len(plan.runs)
    # The values authored per run are relative to the anchor, so that neither the network nor
    # its outputs carry a machine-specific absolute path. A relative asset path in a chunk's
    # (anonymous) edit layer resolves against the process directory, so the runs happen with
    # the anchor as the working directory — that, not the shell's cwd, is what a pattern in
    # this network means.
    prev_cwd = Path.cwd()
    os.chdir(anchor)
    try:
        for i, run in enumerate(plan.runs, 1):
            print(f"[{i}/{total}] {run.source}")
            # Each run cooks the network from scratch (PDG-style), so it gets its own session
            # layer holding only its own overrides — nothing carries over from the last file.
            stage = Usd.Stage.Open(root_layer, Sdf.Layer.CreateAnonymous(f"batch-run-{i}.usda"))
            # Overrides first, then this run's own values: the driver's concrete path has to
            # land on top of whatever pattern the override put there.
            chunk_batch.apply_overrides(stage, pattern_params)
            chunk_batch.apply_run(stage, plan, run)

            scene = HeadlessScene(stage)
            registry.scene = scene
            ChunkLayerManager().reset()
            ChunkLayerManager().initialize(stage)
            load_chunks(stage, scene)
            # Now that the params exist, apply the overrides that had no attribute to
            # author onto — before the cook below, so the network cooks with them.
            try:
                chunk_batch.apply_loaded_overrides(scene.get_chunks(), plain_params)
            except chunk_batch.BatchError as exc:
                print(f"{RED}batch: {exc}{RESET}")
                sys.exit(1)
            # Every run enters with a param whose value differs from the file's, so the
            # network is cooked here the same way `run` cooks it after an override.
            _re_execute_roots(scene.get_chunks())

            if run.output:
                out_path = anchor / run.output
                out_path.parent.mkdir(parents=True, exist_ok=True)
                stage.Export(str(out_path))
                print(f"        -> {out_path}")
            else:
                # The network writes its own output: this run's sink values are already on
                # the params, so committing now writes them where this run's captures say.
                # Checked on the first run only — the network is the same every time, and a
                # committing chunk with a fixed path would silently leave one file behind.
                if i == 1:
                    try:
                        committing = chunk_batch.check_commit_targets(scene.get_chunks(), plan)
                    except chunk_batch.BatchError as exc:
                        print(f"{RED}batch: {exc}{RESET}")
                        sys.exit(1)
                    commit_names = {c.name for c in committing}
                # USD will not create the folder on the way out, and the whole point of a
                # sink pattern is that it names one that need not exist yet.
                for site, value in run.sink_values:
                    if site.chunk_name in commit_names:
                        (anchor / value).parent.mkdir(parents=True, exist_ok=True)
                for committed in chunk_scheduler.commit_all(scene.get_chunks()):
                    print(f"        -> committed {committed.name}")

            scene.clear()
            del stage
            # The network file itself is shared between runs; revert anything a cook dirtied
            # in it so run N+1 starts from the file as authored, not from run N's leftovers.
            if root_layer.dirty:
                root_layer.Reload()
    finally:
        os.chdir(prev_cwd)

    print()
    print(f"{GREEN}Batch complete: {total} run{'s' if total != 1 else ''}.{RESET}")


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
            "  usdchunks batch setup.usda -o 'out/*.usda'\n"
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
    run_parser.add_argument("--export", "-o", metavar="FILE", help="Flatten and export the resulting stage to FILE")
    run_parser.add_argument("--printout", "-p", action="store_true", help="Print the flattened stage to stdout")

    batch_parser = sub.add_parser(
        "batch",
        help="Run a chunk network once per file matched by a glob",
        description=(
            "Convert a folder of assets by cooking the whole network once per matched file,\n"
            "one after the other. The network's own params supply the patterns: every\n"
            "inputs: attribute holding a glob is a pattern, and the single one that resolves\n"
            "to existing files drives the batch. All other patterns (and -o) are filled from\n"
            "its captures, left to right, so subfolder structure survives the conversion.\n"
            "Patterns resolve relative to the .usda's own directory, not the current one.\n"
            "\n"
            "Param overrides take the same CHUNK.param=value form as `run`, and are applied\n"
            "before planning — so an override can supply the glob itself, and one network can\n"
            "be pointed at a different asset folder without editing it. Quote patterns, or the\n"
            "shell expands them before usdchunks ever sees them."
        ),
        epilog=(
            "Examples:\n"
            "  usdchunks batch convert.usda -o 'out/*.usda'\n"
            "  usdchunks batch convert.usda -o 'out/**/*.usda'      # in: assets/**/*.fbx\n"
            "  usdchunks batch convert.usda --driver Reference.file_path -o 'out/*.usda'\n"
            "  usdchunks batch convert.usda Reference.file_path='/myassets/*.usd' \\\n"
            "                               -o '/myassets/new/*.usda'\n"
        ),
        formatter_class=RawDescriptionHelpFormatter,
    )
    batch_parser.add_argument("usda", help="Path to the .usda chunk network file")
    batch_parser.add_argument(
        "params", nargs="*", default=[],
        help="Param overrides: CHUNK.param=value (applied before planning)",
    )
    batch_parser.add_argument(
        "--export", "-o", metavar="PATTERN",
        help="Output path pattern; its wildcards are filled from the driver's captures",
    )
    batch_parser.add_argument(
        "--driver", metavar="CHUNK.param",
        help="Force which pattern drives the batch instead of auto-detecting it",
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
    elif args.command == "batch":
        cmd_batch(args)
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
