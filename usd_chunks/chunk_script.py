"""Build and drive chunk networks from a plain Python script.

The setup a script needs before it can touch a chunk -- the registry, the layer manager,
the stage sync -- is the same every time and easy to get subtly wrong, so the Scene
constructor does it. Chunks are then made through `scene.create`, one function per chunk
module and per menu variant:

    from usd_chunks import Scene

    with Scene() as scene:
        shot = scene.create.layer(layer_name="shot")
        with scene.create.edit_target(shot, edit_target="shot"):
            box = scene.create.cube(prim_path="/geo/box")
            scene.create.transform(box, primpath_location=box.get_param("prim_path"), ty=3.0)
        scene.save("box_network.usda")      # the network: reopens as a graph
        scene.export("box.usda")            # the result: plain USD, no Chunk prims

`with chunk:` says where the chunks created inside it go. A chunkgroup takes them in as its
children; any other chunk goes before them, so they run after it -- which, for an Edit
Target, is what puts their edits in its layer.

The registry and layer manager are process-wide singletons, so one scene is live at a
time, as in any host: close it (or leave the `with` block) before making the next.
"""
from __future__ import annotations

import contextlib

from pxr import Sdf, Usd, UsdUtils

from usd_chunks import chunk_naming, chunk_registry, chunk_scene, chunk_scheduler
from usd_chunks.chunk_stage import (
    ChunkLayerManager,
    ChunkStageSync,
    commit_order_from_stage,
    export_chunks,
    initialize as chunk_stage_init,
)


def flatten_without_chunks(stage):
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
        (p.GetPath() for p in tmp.TraverseAll() if p.GetTypeName() == chunk_naming.CHUNK_PRIM_TYPENAME),
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

    # The scope the chunks hung under is recipe as well. Nothing lives AT /chunks, so once
    # its chunks are gone an empty `def Scope "chunks" {}` is the one trace of the network
    # left in a file whose whole point is to carry none -- and it is enough to make a
    # delivered asset look like it wants usd_chunks installed. Only when it IS empty: a
    # stage that parks something else there keeps both the scope and that something.
    root = flat.GetPrimAtPath(Sdf.Path(chunk_naming.CHUNKS_ROOT))
    if root and root.typeName == chunk_naming.CHUNKS_ROOT_TYPENAME and not root.nameChildren:
        del flat.pseudoRoot.nameChildren[root.name]
    return flat


class Scene(chunk_scene.SceneSender):
    """A host with a stage and nothing else: no selection, no viewport, no timeline.

    Constructing one sets up everything a chunk needs to run, and loads any network the
    stage already holds.

    Args:
        stage: The stage to work on. A new in-memory stage when omitted.
        host_name: What the registry records as the host; only shows up in logs.
        sync: False to start the registry only, without the layer manager or stage sync --
            for reading chunk modules without loading or cooking the stage's network.
    """

    def __init__(self, stage: Usd.Stage | None = None, host_name: str = "script", *, sync: bool = True):
        super().__init__()
        # Whether the stage is this Scene's own -- made here, or opened by Scene.open -- and
        # so goes when the Scene does. A stage passed in belongs to the caller.
        self._owns_stage = stage is None
        self._stage = stage or Usd.Stage.CreateInMemory()
        self.registry = chunk_registry.ChunkRegistry()
        self.registry.initialize(self, host_name)
        if sync:
            # Stage sync rather than a bare load_chunks: it keeps listening while the
            # network loads, which is what binds params a chunk builds during its own
            # execute to their prims, and it restores Edit Target selections.
            chunk_stage_init(self._stage, self)

    @classmethod
    def open(cls, path: str, host_name: str = "script") -> Scene:
        """Open a saved network and load its chunks, the way `usdchunks run` does."""
        stage = Usd.Stage.Open(path)
        if not stage:
            raise FileNotFoundError(f"Failed to open: {path}")
        scene = cls(stage, host_name)
        scene._owns_stage = True
        return scene

    def get_stage(self):
        return self._stage

    def __enter__(self) -> Scene:
        return self

    def __exit__(self, *exc) -> None:
        # Unlike a host's scene, this one is the script's own, so the block ends it.
        self.close()

    def commit(self) -> None:
        """Run every chunk's commit hook -- the ones that write files, like Export USD."""
        chunk_scheduler.commit_all(
            self.get_chunks(),
            order=lambda cs: commit_order_from_stage(cs, self._stage),
        )

    def save(self, path: str) -> None:
        """Write the network itself -- Chunk prims and wires -- so it reopens as a graph."""
        export_chunks(self, path)

    def export(self, path: str) -> None:
        """Write the result as plain USD, with the Chunk prims stripped out."""
        flatten_without_chunks(self._stage).Export(path)

    def close(self) -> None:
        """Delete the chunks and reset the layer manager, ready for the next scene.

        Without this the chunks stay registered, and the next scene that loads a file
        finds their names taken and builds a second copy of every chunk beside them.
        """
        # Unwire first. request_delete does the same disconnect for a chunk that still has
        # outputs, but warns "Stuff may break" doing it -- and deleting the downstream end
        # first does not avoid that, since it leaves the upstream's outputs in place.
        inner = chunk_scene.all_chunks(self.get_chunks())
        for c in inner:
            c.get_dag_node().disconnect()
        # A stage this Scene made or opened goes with it, so tidying its prims away chunk by
        # chunk -- each deletion re-healing the exec chain and reconciling Edit Targets over
        # the whole scene -- was most of the time closing took. A stage the caller passed in
        # is theirs, and keeps the tidy-up.
        sync = ChunkStageSync()
        if self._owns_stage and sync._stage is self._stage:
            teardown = sync.discarding_stage()
        else:
            teardown = contextlib.nullcontext()
        with teardown:
            for c in inner + list(self.get_chunks()):
                c.request_delete()
        ChunkLayerManager().reset()
