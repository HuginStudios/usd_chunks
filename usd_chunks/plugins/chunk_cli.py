"""
A simple CLI for running chunks headless. A module should be provided as argument. The last chunk
exported from the module will be executed if -e is set.

F.e:
```
python chunk_cli.py --module examples.example "Hello World.slider=2"
```

will run the examples.example module, which exports a single chunk named "Hello World", and set that chunk's
`slider` param to 2. Arguments take the form `<chunk name>.<param name>=<value>`, so a chunk name containing
a space needs quoting for the shell.

Setting a param solves the chunk it belongs to, and anything downstream of it. The -e flag is only needed for a
chunk that no argument touched -- pass `-e "*"` to execute every chunk the module exported.
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
