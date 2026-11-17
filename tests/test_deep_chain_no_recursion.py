"""A chain longer than Python's recursion limit can still be ordered.

chunk_stage._roots_first ordered a graph's chunks by recursing up the chain, one Python
frame per chunk. A chain of about a thousand chunks (a script building a long network)
raised RecursionError at the end of its change_block, and the network could not be built.
"""

from __future__ import annotations

import sys

from pxr import Sdf, Usd

from usd_chunks import chunk_naming, chunk_stage


def _chain_stage(length: int) -> tuple[Usd.Stage, list[str]]:
    """Chunk prims /chunks/n0 ... wired n0 -> n1 -> ... through inputs:chunk."""
    stage = Usd.Stage.CreateInMemory()
    paths = [f"/chunks/n{i}" for i in range(length)]
    with Sdf.ChangeBlock():
        layer = stage.GetRootLayer()
        Sdf.CreatePrimInLayer(layer, "/chunks")
        for i, path in enumerate(paths):
            spec = Sdf.CreatePrimInLayer(layer, path)
            spec.specifier = Sdf.SpecifierDef
            spec.typeName = chunk_naming.CHUNK_PRIM_TYPENAME
            if i:
                attr = Sdf.AttributeSpec(spec, chunk_naming.EXEC_INPUT, Sdf.ValueTypeNames.Token)
                attr.connectionPathList.explicitItems = [Sdf.Path(f"{paths[i - 1]}.{chunk_naming.EXEC_OUTPUT}")]
    return stage, paths


def test_a_chain_past_the_recursion_limit_orders_roots_first():
    length = sys.getrecursionlimit() + 200
    stage, paths = _chain_stage(length)
    prim_to_chunk = {p: object() for p in paths}
    order: list[str] = []
    chunk_stage._roots_first(stage, prim_to_chunk, paths[-1], set(), order)
    assert order == paths


def test_shared_sources_come_once_and_first():
    stage, paths = _chain_stage(3)
    prim_to_chunk = {p: object() for p in paths}
    seen: set = set()
    order: list[str] = []
    chunk_stage._roots_first(stage, prim_to_chunk, paths[2], seen, order)
    chunk_stage._roots_first(stage, prim_to_chunk, paths[1], seen, order)
    assert order == paths
