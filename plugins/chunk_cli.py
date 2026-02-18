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

from usd_chunks import chunk_registry, chunk_notice, chunk
from argparse import ArgumentParser

from pxr import Usd


class HeadlessScene(chunk_notice.SceneSender):
    def __init__(self, usdFile=None) -> None:
        if usdFile:
            self.stage = Usd.Stage.Open(usdFile)
        else:
            self.stage = Usd.Stage.CreateInMemory()

    def get_stage(self) -> Usd.Stage:
        return self.stage


def initialize(module, usdFile=None):
    scene = HeadlessScene(usdFile)

    creg = chunk_registry.ChunkRegistry()
    creg.initialize(scene, "cli", True)
    chunk_registry.ChunkLayerManager().initialize(scene.get_stage())

    if mod := creg.get_chunk_module(module):
        mod.load()
        return mod
    return None


def parse_params(mod: chunk_registry.ChunkModule, args):
    for arg in args:
        name, value = arg.split("=")
        cname, parampath = name.split(".", maxsplit=2)

        if not (c := mod.loaded_chunks.get(cname)):
            print(f"Could not find chunk {cname} from module {mod.module_name}")
            continue

        if not (param := c.get_param(parampath)):
            print(f"Param '{parampath}' is invalid for chunk '{c.name}'")
            continue

        assert isinstance(param, chunk.ChunkParamBase)

        types = chunk.ChunkFlags.ParamTypeToTypes(param.evaluate_type())
        if not types:
            return

        cast = types[0](value)
        param.value = cast


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument(
        "--file", required=False, help="A path to a usd file. Supported formats: (.usda, .usd, .usdc, .usdz)"
    )
    parser.add_argument("--module", help="The chunk module to load")
    parser.add_argument("-e", action="store_true", help="Should the last loaded chunk from the module call execute")
    parser.add_argument("args", nargs="+", help="Arguments to the chunk loaded from the module")

    args = parser.parse_args()

    mod = initialize(args.module, args.file)

    if mod and mod.loaded_chunks:
        parse_params(mod, args.args)

        if args.e:
            print("EXECUTE")
            c = list(mod.loaded_chunks.values())[-1]
            c.execute()
