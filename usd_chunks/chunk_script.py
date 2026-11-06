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
import logging
import sys

from pxr import Usd

from usd_chunks import chunk_cook, chunk_registry, chunk_scene, configure_logging
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
        lazy: Write the network without cooking it. Loading it, making chunks and setting
            values only mark what they reach; `scene.cook(chunk)` cooks what a read of
            that chunk needs, and `export` and `commit` cook everything. `save` writes the
            network as it stands, cooking only chunks that build a param a value waits
            for. What is still marked when the Scene closes is dropped: closing removes
            every chunk's result anyway. A param a chunk has not built yet is taken as one
            its cook builds, so a misspelled name raises at that cook, not at `create`.
    """

    def __init__(self, stage: Usd.Stage | None = None, host_name: str = "script", *, sync: bool = True,
                 lazy: bool = False):
        live = chunk_scene.current_scene()
        if live is not None and live.is_app:
            raise RuntimeError(
                f"{type(live).__name__} is already running a scene. "
                "Use Scene.current() to build in it."
            )
        # A script is an entry point too. Without this, a library's NullHandler swallowed
        # every warning: a failed package install, a prototype path that matched nothing.
        configure_logging(logging.WARNING)
        super().__init__()
        # Whether the stage is this Scene's own -- made here, or opened by Scene.open -- and
        # so goes when the Scene does. A stage passed in belongs to the caller.
        self._owns_stage = stage is None
        self._stage = stage or Usd.Stage.CreateInMemory()
        self.registry = chunk_registry.ChunkRegistry()
        self.registry.initialize(self, host_name)
        # Open from before the load, so loading a saved network cooks nothing either.
        self._lazy = lazy
        if lazy:
            chunk_cook.begin_deferring()
        if sync:
            # Stage sync rather than a bare load_chunks: it keeps listening while the
            # network loads, which is what binds params a chunk builds during its own
            # execute to their prims, and it restores Edit Target selections.
            try:
                chunk_stage_init(self._stage, self)
            except BaseException:
                self._end_lazy()
                raise

    @classmethod
    def new(cls, host_name: str = "script") -> Scene:
        """Start a scene of your own, on a new in-memory stage. The same as Scene()."""
        return cls(host_name=host_name)

    @staticmethod
    def current() -> chunk_scene.SceneSender | None:
        """The scene that is running now: usdview's inside usdview, or None if there is none."""
        return chunk_scene.current_scene()

    @classmethod
    def open(cls, path: str, host_name: str = "script", *, lazy: bool = False) -> Scene:
        """Open a saved network and load its chunks, the way `usdchunks run` does.

        lazy: load without cooking, as Scene(lazy=True).
        """
        stage = Usd.Stage.Open(path)
        if not stage:
            raise FileNotFoundError(f"Failed to open: {path}")
        scene = cls(stage, host_name, lazy=lazy)
        scene._owns_stage = True
        return scene

    def get_stage(self):
        return self._stage

    def should_install_packages(self, packages, chunk_name: str) -> bool:
        """Ask on the terminal; no when nobody is there to answer (CI, a pipe, a cron job).

        input() on a closed or redirected stdin reads EOF or blocks, so only an interactive
        terminal is asked.
        """
        stdin = sys.stdin
        if stdin is None or not stdin.isatty():
            return False
        target = chunk_registry.ChunkDependencyManager().target_dir()
        try:
            answer = input(
                f"OK to install {', '.join(packages)} for chunk {chunk_name} into {target}? [y/N] "
            )
        except EOFError:
            return False
        return answer.strip().lower() in ("y", "yes")

    def run_install(self, packages, chunk_name: str, run):
        """Echo pip's output as it arrives, so a long download shows it is moving."""
        print(f"Installing {', '.join(packages)} for chunk {chunk_name}...", file=sys.stderr, flush=True)
        return run(lambda line: print(f"  {line}", file=sys.stderr, flush=True))

    def __enter__(self) -> Scene:
        return self

    def __exit__(self, *exc) -> None:
        # Unlike a host's scene, this one is the script's own, so the block ends it.
        self.close()

    def _end_lazy(self) -> None:
        """End a lazy Scene's deferral, once, dropping what it left marked.

        Not cooked: closing deletes every chunk, and each chunk's layer -- its result -- with
        it, so nothing cooked now could be read.
        """
        if self._lazy:
            self._lazy = False
            chunk_cook.end_deferring(cook=False)

    def close(self) -> None:
        """Delete the chunks and reset the layer manager, ready for the next scene.

        Without this the chunks stay registered, and the next scene that loads a file
        finds their names taken and builds a second copy of every chunk beside them.
        """
        self._end_lazy()
        # Unwire first. request_delete does the same disconnect for a chunk that still has
        # outputs, and deleting the downstream end first would not avoid that, since it
        # leaves the upstream's outputs in place.
        inner = chunk_scene.all_chunks(self.get_chunks())
        for c in inner:
            c.get_dag_node().disconnect()
        # A stage this Scene made or opened goes with it, so tidying its prims away chunk by
        # chunk -- each deletion re-healing the exec chain and reconciling Edit Targets over
        # the whole scene -- was most of the time closing took. A stage the caller passed in
        # is theirs, and keeps the tidy-up.
        sync = ChunkStageSync()
        # Compared as stages, not as objects: the sync takes its stage from each notice, and
        # a notice hands out a new Python wrapper of the same stage -- after which `is` said
        # no, the tidy-up ran, and a rescan of the old stage rebuilt the chunks just deleted.
        if self._owns_stage and sync._stage is not None and sync._stage == self._stage:
            teardown = sync.discarding_stage()
        else:
            teardown = contextlib.nullcontext()
        with teardown, ChunkLayerManager().holding_deletes():
            for c in inner + list(self.get_chunks()):
                c.request_delete()
        ChunkLayerManager().reset()
