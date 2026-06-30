"""
usdchunks CLI — headless execution of chunk networks serialized as .usda files.

Usage:
    usdchunks run my_setup.usda [ChunkName.param=value ...]
    usdchunks info my_setup.usda
"""

import sys
from argparse import ArgumentParser, RawDescriptionHelpFormatter
from pxr import Usd, UsdUtils
from usd_chunks import chunk, chunk_registry, chunk_core
from plugins.chunk_usd import load_chunks, ChunkLayerManager


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

    RED   = "\033[31m"
    GREEN = "\033[32m"
    RESET = "\033[0m"

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

    args = parser.parse_args()

    if args.command == "run":
        cmd_run(args)
    elif args.command == "info":
        cmd_info(args)
    elif args.command == "validate":
        cmd_validate(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
