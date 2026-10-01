"""Build and drive chunk networks from a plain Python script.

The setup a script needs before it can touch a chunk -- the registry, the layer manager,
the stage sync -- is the same every time and easy to get subtly wrong, so the Scene
constructor does it. Chunks are then made through `scene.create`, one function per chunk
module and per menu variant:

    from usd_chunks import Scene

    with Scene.new() as scene:
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
time, as in any host: close it (or leave the `with` block) before making the next. Inside
an app like usdview, the app's scene is the live one: Scene.current() gets it, and a new
Scene refuses to start.
"""
from __future__ import annotations

import contextlib

from pxr import Usd

from usd_chunks import chunk_registry, chunk_scene
from usd_chunks.chunk_stage import (
    ChunkLayerManager,
    ChunkStageSync,
    initialize as chunk_stage_init,
)


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
        live = chunk_scene.current_scene()
        if live is not None and live.is_app:
            raise RuntimeError(
                f"{type(live).__name__} is already running a scene. "
                "Use Scene.current() to build in it."
            )
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
    def new(cls, host_name: str = "script") -> Scene:
        """Start a scene of your own, on a new in-memory stage. The same as Scene()."""
        return cls(host_name=host_name)

    @staticmethod
    def current() -> chunk_scene.SceneSender | None:
        """The scene that is running now: usdview's inside usdview, or None if there is none."""
        return chunk_scene.current_scene()

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
