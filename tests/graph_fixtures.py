"""Build a real chunk network file for the graph frontends to open.

Hand-writing the .usda is what these tests did first, and it was wrong in a way that
looked like a product bug: Chunk prims placed directly under a Scope, with no context
prim, are not what the framework writes. Opening such a file makes load_chunks build a
ChunkGroup and re-home copies into it, so a two-node file came back with four nodes and
a node created afterwards landed somewhere the view was not looking.

Going through export_chunks means the fixture is a file the framework itself produced.
Engine-only tests (test_chunk_graph) can keep using a literal string -- they never call
load_chunks -- but anything that builds a GraphView must start from a real one.
"""
from __future__ import annotations

from pxr import Gf, Sdf, Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.plugins.chunk_usd import export_chunks, initialize as chunk_usd_init

POS_ATTR = "ui:nodegraph:node:pos"


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []

    def should_install_package(self, package):
        return False


def make_network(path, modules=("transform", "transform"),
                 positions=((100.0, -50.0), (400.0, -50.0)), chain=True):
    """Write a genuine chunk network to `path`; return (path, context).

    Positions are in USD coordinates (Y-up), the way a saved file holds them.

    The context comes back because chunks do not live at the stage root: add_chunk puts
    them in a chunkgroup, and a view opened at the root shows that GROUP as a single node
    -- correct, and the same thing Qt shows before you dive in, but not where the chunks
    are. Tests that want the chunks must open the view at this context.
    """
    stage = Usd.Stage.CreateInMemory()
    scene = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_usd_init(stage, scene)

    made = []
    registry = chunk_registry.ChunkRegistry()
    for name in modules:
        module = registry.get_chunk_module(name)
        assert module is not None, f"chunk module {name!r} not found"
        chunks = module.create() if module.loaded else module.load()
        scene.add_chunk(chunks[0])
        made.append(chunks[0])

    if chain:
        for upstream, downstream in zip(made, made[1:]):
            downstream.add_dependency(upstream)

    path = str(path)
    export_chunks(scene, path)

    # Positions after the export: create() does not place a node, and the tests need two
    # nodes that do not sit on top of each other to hit-test against.
    saved = Usd.Stage.Open(path)
    chunk_prims = [p for p in saved.Traverse() if p.GetTypeName() == "Chunk"]
    inner = [p for p in chunk_prims
             if (p.GetAttribute("info:chunkType").Get() or "") != "chunkgroup"]
    context = str(inner[0].GetParent().GetPath()) if inner else None
    for prim, (x, y) in zip(inner, positions):
        attr = prim.GetAttribute(POS_ATTR) or prim.CreateAttribute(
            POS_ATTR, Sdf.ValueTypeNames.Float2)
        attr.Set(Gf.Vec2f(float(x), float(y)))
    saved.GetRootLayer().Save()

    # Tear the builder's session down before anyone opens the file. The chunks made above
    # stay registered otherwise, and load_chunks then finds the names taken and creates a
    # SECOND context -- a two-node file opening as four nodes, which reads exactly like a
    # loader bug rather than a dirty process.
    for chunk in list(scene.get_chunks()):
        chunk.request_delete()
    chunk_registry.ChunkLayerManager().reset()
    return path, context
