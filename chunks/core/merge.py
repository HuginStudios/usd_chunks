from __future__ import annotations
from usd_chunks import chunk


def execute(node):
    """Author nothing. A Merge is a pure topology marker: wiring branches into it is what
    puts them in the same DagGraphID (add_dependency unions the ids), which is what puts
    their layers in the same graph layer, which is what lets recook_for_view order them
    against each other at all. Composition is entirely a side effect of that wiring, so
    there is no merged result to write here.
    """


def main():
    return chunk.Chunk("Merge")


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.CORE,
        description="Fan-in for the execution chain: many upstream branches converge on one "
        "output, instead of being strung out in a single serial line. Inputs are ordered and "
        "the order decides who wins — input 0 is the strongest opinion, so when two branches "
        "author the same prim, the one in the topmost slot takes precedence (same convention "
        "as Houdini's Merge LOP). Slots stay dense: clearing one shifts the rest up, and the "
        "trailing empty slot is where a new branch attaches.",
        dynamic_exec_inputs=True,
    )
