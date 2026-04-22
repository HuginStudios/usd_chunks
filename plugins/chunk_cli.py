"""
A simple CLI for running chunks headless. A module should be provided as argument. The last chunk
exported from the module will be executed if -e is set.

F.e:
```
python chunk_cli.py --module examples.dependencies A.A_param=2 B.B_param=3
```

will run the examples.dependency module. This modules exports 4 chunks: A, B, C, D. Where C is dependant on A and B, and D is Dependant on A,B, and C.
The arguments passed to chunk_cli will set the value A_param on chunk A to 2, the value of B_param in chunk B to 3. Since chunk C of the module is
dependent on A and B, C will solve, then finally triggering D to solve.

In this case, the -e flag is unessesary as the chunk D is already solved after A,B,C are solved.
"""

from usd_chunks import chunk_registry, chunk_core, chunk
from argparse import ArgumentParser, ArgumentError

from pxr import Usd


class HeadlessScene(chunk_core.SceneSender):
    def __init__(self, usdFile=None) -> None:
        super().__init__()

        if usdFile:
            self.stage = Usd.Stage.Open(usdFile)
        else:
            self.stage = Usd.Stage.CreateInMemory()

    def get_stage(self) -> Usd.Stage:
        return self.stage
    
    def should_install_package(self, package_name: str) -> bool:
        inp = input(f"Install package: {package_name}? Y or N... ").lower()
        return inp in ["y", "1"]


def initialize_modules(module_names, usdFile=None):
    scene = HeadlessScene(usdFile)

    creg = chunk_registry.ChunkRegistry()
    creg.initialize(scene, "cli")

    chunk_registry.ChunkLayerManager().initialize(scene.get_stage())
    for name in module_names:
        chunk.create_chunks(name)

    return scene.get_chunks()

def initialize_scene(scene_path, usdFile=None):
    scene = HeadlessScene(usdFile)

    creg = chunk_registry.ChunkRegistry()
    creg.initialize(scene, "cli", scene_path)

    chunk_registry.ChunkLayerManager().initialize(scene.get_stage())
    return scene.get_chunks()


def parse_params(chunks, args):
    cs = {x.name: x for x in chunks}

    for arg in args:
        name, value = arg.split("=")
        cname, parampath = name.split(".", maxsplit=2)

        if not (c := cs.get(cname)):
            print(f"Could not find chunk {cname}")
            continue

        if not (param := c.get_param(parampath, chunk.is_param)):
            print(f"Param '{parampath}' is invalid for chunk '{c.name}'")
            continue

        types = chunk.ChunkFlags.ParamTypeToTypes(param.evaluate_type())
        if not types:
            return

        cast = types[0](value)
        param.value = cast

def execute_chunks(chunk_names):
    scene = chunk.get_scene()
    if not scene:
        return

    if "*" in chunk_names:
        for c in scene.get_chunks():
            c.execute()
        return

    for m in chunk_names:
        if not (c := scene.get_chunk(m)):
            continue
        c.execute()

if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument(
        "--file", required=False, help="A path to a usd file. Supported formats: (.usda, .usd, .usdc, .usdz)"
    )
    parser.add_argument("--scene", help="Load a scene")
    parser.add_argument("--modules", nargs="+", help="The chunk modules to load")
    parser.add_argument("--args", nargs="+", help="Arguments to the chunk loaded from the module")
    parser.add_argument("--execute", "-e", nargs="+", help="Should the last loaded chunk from the module call execute")

    args = parser.parse_args()

    if args.scene:
        chunks = initialize_scene(args.scene, args.file)
    elif args.modules:
        chunks = initialize_modules(args.modules, args.file)
    else:
        raise ArgumentError(None, "Missing scene or module argument")

    try:
        parse_params(chunks, args.args)
    except Exception as e:
        print(e)
        raise

    if args.execute:
        execute_chunks(args.execute)
