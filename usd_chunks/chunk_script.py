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

The calls themselves are ScriptingMixin, which Scene and every app's scene inherit, so
`Scene.current()` in usdview has them too.
"""
from __future__ import annotations

import contextlib
import re
from collections.abc import Callable, Iterable

from pxr import Sdf, Usd, UsdUtils

from usd_chunks import chunk_naming, chunk_registry, chunk_scene, chunk_scheduler, chunk_stage
from usd_chunks.chunk_core import ChunkBase, ChunkParamBase
from usd_chunks.graph import engine
from usd_chunks.chunk_stage import (
    ChunkLayerManager,
    ChunkStageSync,
    initialize as chunk_stage_init,
)

#: Set by a graph frontend: returns the USD position at the center of the graph it shows, or
#: None. Nodes a script makes in an app start there, so they land where the user is looking.
graph_center_hook: Callable[[], tuple[float, float] | None] | None = None

#: Space between two nodes a script makes, in a row. The same as the default layout's.
_SCRIPT_NODE_STEP = 260.0



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

    # Chunks connect the attributes they drive to their own params (a camera's focalLength
    # to Camera.inputs:...:focalLength). The values are already flattened in, so the links
    # would only point at prims this file no longer has.
    def drop_links_to_chunks(path):
        if not path.IsPropertyPath():
            return
        prop = flat.GetPropertyAtPath(path)
        links = prop.connectionPathList if isinstance(prop, Sdf.AttributeSpec) else prop.targetPathList
        items = list(links.GetAddedOrExplicitItems())
        kept = [t for t in items if not any(t.HasPrefix(c) for c in chunk_paths)]
        if len(kept) == len(items):
            return
        if kept:
            for target in set(items) - set(kept):
                links.RemoveItemEdits(target)
        else:
            links.ClearEdits()  # no `.connect = None` left behind

    flat.Traverse(Sdf.Path.absoluteRootPath, drop_links_to_chunks)

    # The scope the chunks hung under is recipe as well. Nothing lives AT /chunks, so once
    # its chunks are gone an empty `def Scope "chunks" {}` is the one trace of the network
    # left in a file whose whole point is to carry none -- and it is enough to make a
    # delivered asset look like it wants usd_chunks installed. Only when it IS empty: a
    # stage that parks something else there keeps both the scope and that something.
    root = flat.GetPrimAtPath(Sdf.Path(chunk_naming.CHUNKS_ROOT))
    if root and root.typeName == chunk_naming.CHUNKS_ROOT_TYPENAME and not root.nameChildren:
        del flat.pseudoRoot.nameChildren[root.name]
    return flat


class ScriptingMixin:
    """The scripting calls: `scene.create`, `set`, `view`, `save`, `export` and the rest.

    Mixed into Scene and into every app's scene (usdview, Blender, Flet, imgui), before
    SceneSender, so `Scene.current()` in an app has them too. They live here, not on
    SceneSender, because they build on chunk_stage and the graph engine, which import
    chunk_scene themselves.
    """

    # Class-level defaults, so a host needs no __init__ of its own to mix this in.
    _creator: Creator | None = None
    # Where this script's nodes go in an app's graph: the first node's position (None for
    # the default row), whether it has been looked up yet, and how many have been placed in
    # the row since.
    _script_origin: tuple[float, float] | None = None
    _script_origin_found = False
    _script_count = 0

    def __enter__(self) -> ScriptingMixin:
        """`with scene:` -- a host's scene is borrowed, so leaving the block keeps it."""
        self._script_origin_found = False
        return self

    def __exit__(self, *exc) -> None:
        self._script_origin_found = False

    def _next_node_position(self) -> tuple[float, float] | None:
        """Where the next scripted node goes in an app's graph, or None for the default.

        The first goes at the center of the graph view, the rest in a row to its right.
        Each `with` block starts a new row.
        """
        if not self.is_app:
            return None
        if not self._script_origin_found:
            # Looked up once per block: with no view yet, the whole block keeps the default
            # row rather than jumping to the view once the first node has made one.
            self._script_origin_found = True
            self._script_origin = graph_center_hook() if graph_center_hook else None
            self._script_count = 0
        if self._script_origin is None:
            return None
        x, y = self._script_origin
        self._script_count += 1
        return (x + (self._script_count - 1) * _SCRIPT_NODE_STEP, y)

    def _add_scripted(self, chunk: ChunkBase) -> None:
        """add_chunk, at the position _next_node_position picks.

        Like a node made in the graph, a chunk made while its graph has a View starts
        blocked: it is not in the View's chain yet, so it must not cook. scene.view() on it,
        or on a chunk after it, is what lets it cook.
        """
        context = self.current_context or self.peek_root_context()
        if (engine.context_has_view(self, context)
                and not chunk_stage.is_view_chunk(chunk) and not chunk_stage.is_group_chunk(chunk)):
            chunk.set_blocked(True)
        with chunk_stage.creating_at(self._next_node_position()):
            self.add_chunk(chunk)

    @property
    def create(self) -> Creator:
        """One function per chunk module and per menu variant: `scene.create.cube(...)`."""
        if self._creator is None:
            self._creator = Creator(self)
        return self._creator

    def add(self, chunk_type: str, *inputs: ChunkBase, **kwargs) -> ChunkBase:
        """`scene.create.<chunk_type>(...)` with the type as a string, for types picked at
        runtime."""
        return getattr(self.create, chunk_type)(*inputs, **kwargs)

    def place(
        self,
        new: ChunkBase,
        inputs: Iterable[ChunkBase] = (),
        name: str | None = None,
        **params,
    ) -> ChunkBase:
        """Add a created chunk to the scene, wire it and set its params.

        Args:
            new: The chunk, fresh from its module or a menu entry.
            inputs: The chunks wired into its input port -- the exec edge, so it runs
                after them. None given, the innermost `with` chunk, if there is one.
            name: The node's name; the module's own when omitted.
            **params: See `set`.
        """
        if name:
            new.name = name
        # Plain values before the add, so the chunk's first cook -- the one add_chunk
        # triggers -- already sees them, rather than cooking the defaults and then once more
        # per param. Values only, no cook, under the same guard MenuEntry.create sets for
        # its presets. A wire waits until after: connecting a param authors on the chunk's
        # prim, which does not exist until the chunk is in the scene.
        values = {k: v for k, v in params.items() if not isinstance(v, ChunkParamBase)}
        wires = {k: v for k, v in params.items() if isinstance(v, ChunkParamBase)}
        # A param the chunk only builds once it is in the scene (Light's) waits until then.
        early = {k: v for k, v in values.items() if new.get_param(k) is not None}
        prev = getattr(new, "_executing", False)
        new._executing = True
        try:
            self.set(new, **early)
        finally:
            new._executing = prev
        self._add_scripted(new)
        # A menu that gets its choices only once the chunk is in the scene (Edit Target's
        # layer list) drops a value set before that, so set any value that did not stick.
        late = {
            k: v for k, v in values.items()
            if k not in early or new.get_param(k).value != v
        }
        self.set(new, **late)
        inputs = list(inputs)
        if not inputs and self._upstream and self._upstream[-1] is not None:
            inputs = [self._upstream[-1]]
        for i in inputs:
            new.add_dependency(i)
        self.set(new, **wires)
        return new

    def set(self, target: ChunkBase, **params) -> None:
        """Set a chunk's params by name.

        A plain value is assigned. A param (`other.get_param("prim_path")`) connects this
        one to it, so it follows that value from then on and the wire is saved with the
        network.
        """
        for name, value in params.items():
            param = target.get_param(name)
            if param is None:
                raise KeyError(f"{target.name} has no param {name!r}")
            if isinstance(value, ChunkParamBase):
                param.connect_value(value)
            else:
                param.value = value

    def cook(self) -> None:
        """Run every chunk, upstream first.

        Making a chunk and setting a value already run what they affect, so this is rarely
        needed.
        """
        chunk_scene.execute_roots(self.get_chunks())

    def save(self, path: str) -> None:
        """Write the network itself -- Chunk prims and wires -- so it reopens as a graph."""
        chunk_stage.export_chunks(self, path)

    def export(self, path: str) -> None:
        """Write the result as plain USD, with the Chunk prims stripped out."""
        flatten_without_chunks(self.get_stage()).Export(path)

    def commit(self) -> None:
        """Run every chunk's commit hook -- the ones that write files, like Export USD."""
        stage = self.get_stage()
        chunk_scheduler.commit_all(
            self.get_chunks(),
            order=lambda cs: chunk_stage.commit_order_from_stage(cs, stage),
        )

    def view(self, target: ChunkBase) -> ChunkBase | None:
        """Make the active View show `target`, and cook what it now reaches.

        The same as dragging a wire from `target` into the View in the graph: the View's
        old input is replaced. A graph with no View gets one. Nothing else is rewired.

        Args:
            target: The chunk to view, usually the last one in a chain.

        Returns:
            The View chunk, or None if no View could be made.
        """
        stage = self.get_stage()
        target_path = chunk_stage.get_chunk_prim_path(target)
        context_path = str(Sdf.Path(target_path).GetParentPath())
        by_path = {chunk_stage.get_chunk_prim_path(c): c for c in (*self.get_contexts(), *self.get_chunks())}
        context = by_path.get(context_path)

        slot = engine.read_active_slot(stage, context_path)
        view = chunk_stage.find_view_chunk_for_slot(slot, stage, self, context) if slot else None
        if view is None:
            # Reuses a View already wired to target, or makes one.
            slot = engine.create_view_for(stage, self, target_path)
            view = chunk_stage.find_view_chunk_for_slot(slot, stage, self, context) if slot else None
            if view is None:
                return None
        else:
            chunk_stage.connect_exec(stage, self, target_path, chunk_stage.get_chunk_prim_path(view))
        engine.write_active_slot(stage, context_path, slot)
        chunk_stage.recook_for_view(stage, self, context, view)
        return view


class Creator:
    """`scene.create`: one function per chunk module and per menu variant.

    A module is found by its file name (`transform`, `edit_target`); a variant by its menu
    label in snake_case ("Dome Light" -> `dome_light`). A module name wins over a variant
    label, and a label two variants share is left out rather than guessed at.

    Each function takes its input chunks positionally -- what is wired into its input
    port -- then `name=` for the node's name and params by keyword; see ScriptingMixin.place
    and ScriptingMixin.set.
    """

    def __init__(self, scene: ScriptingMixin):
        self._scene = scene
        self._variants: dict[str, chunk_registry.MenuEntry | None] | None = None

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        entry = self._entry(name)

        def create(*inputs: ChunkBase, name: str | None = None, **params):
            # A module's main() may return helpers alongside its chunk; the first is the one
            # the params are for, the rest go into the scene beside it.
            primary, *extra = entry.create()
            placed = self._scene.place(primary, inputs, name, **params)
            for c in extra:
                self._scene._add_scripted(c)
            return placed

        create.__name__ = name
        create.__doc__ = entry.tooltip or None
        return create

    def __dir__(self):
        registry = chunk_registry.ChunkRegistry()
        modules = {m.module_name.split(".")[-1] for m in registry.get_chunk_modules()}
        return sorted(modules | {k for k, v in self._variant_entries().items() if v is not None})

    def _entry(self, name: str) -> chunk_registry.MenuEntry:
        if module := chunk_registry.ChunkRegistry().get_chunk_module(name):
            return chunk_registry.MenuEntry(
                module=module, namespace=(name,),
                tooltip=chunk_registry.clean_description(module.config.description),
            )
        variants = self._variant_entries()
        if name not in variants:
            raise AttributeError(f"No chunk module or variant called {name!r} -- is USD_CHUNK_DIRS set?")
        if variants[name] is None:
            raise AttributeError(
                f"{name!r} names more than one variant; create its module and set the "
                f"params that pick it instead"
            )
        return variants[name]

    def _variant_entries(self) -> dict[str, chunk_registry.MenuEntry | None]:
        # Built on first use only: a catalog-driven module can declare hundreds of variants,
        # and a script that only makes modules should not pay for listing them.
        if self._variants is None:
            self._variants = {}
            for entry in chunk_registry.ChunkRegistry().get_menu_entries(sort_result=False):
                if entry.presets is None:
                    continue
                key = _script_name(entry.label)
                self._variants[key] = None if key in self._variants else entry
        return self._variants


def _script_name(label: str) -> str:
    """A menu label as a Python name: "Dome Light" -> "dome_light"."""
    return re.sub(r"\W+", "_", label.strip()).strip("_").lower()


class Scene(ScriptingMixin, chunk_scene.SceneSender):
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
