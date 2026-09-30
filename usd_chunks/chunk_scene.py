"""The DCC boundary: the object a host implements to give chunks a scene.

SceneSender is what usdview, and any other host, subclasses -- it owns the chunk
container, the current context, the selection, and the save/load entry points. It was
the tail of chunk_core, and nothing in chunk_core refers to it, so the dependency runs
one way only: this module imports the model, the model never imports this.

That one-way direction is the reason it could move at all. It does NOT make this
module lightweight -- SceneSender is built on ChunkBase and NameSyncedContainer, so
importing it still loads the whole model. What it buys is that the host-facing surface
is now one file you can read end to end, rather than the last 600 lines of a 4900-line
one.

Import it from here. chunk_core used to re-export it lazily for older code; that alias is
gone, because chunk_core cannot import this module without a cycle.
"""
from __future__ import annotations

import contextlib
import re
import logging
import weakref
from abc import abstractmethod
from pathlib import Path
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from pxr import Gf, Usd, UsdGeom

from usd_chunks import chunk_notice, chunk_registry
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_core import (
    ChunkBase,
    ChunkParamBase,
    ChunkHash,
    NameSyncedContainer,
    check_chunk_type,
    is_inst,
)

logger = logging.getLogger(__name__)


# The chunk module backing every context. "context == chunkgroup": a CONTEXT chunk with no
# module was only ever a stand-in from before this type existed. The /chunks prim itself is a
# scope (plugins-layer _CHUNKS_ROOT), like /lights or /render -- nothing lives at it, and the
# default chunkgroup sits under it at /chunks/<name>.
CHUNKGROUP_MODULE = "chunkgroup"


#: Set by a graph frontend: returns the USD position at the center of the graph it shows, or
#: None. Nodes a script makes in an app start there, so they land where the user is looking.
graph_center_hook: Callable[[], tuple[float, float] | None] | None = None

#: Space between two nodes a script makes, in a row. The same as the default layout's.
_SCRIPT_NODE_STEP = 260.0


class SceneSender(chunk_notice.ChunkNoticePropagator):
    """Scene access for one DCC: the stage, its selection and time, and notices for changes.

    A DCC host subclasses this; see hosts/usdview/__init__.py for an example. The
    class is not purely abstract, as most functions dealing with chunks are implemented
    here, but a subclass should implement every @abstractmethod.
    """

    #: Whether a load cooks only what the active views reach. A host whose graph recooks
    #: for its active view straight after a load sets this: that recook mutes everything the
    #: view cannot reach, so cooking it during the load is work nobody sees. The rest stays
    #: stale and cooks when its view is activated. Off by default, because a script or the
    #: CLI never runs that recook and relies on the load to cook the whole network.
    load_cooks_active_views_only: bool = False

    #: Whether this scene belongs to an app the user works in, like usdview. A script's own
    #: Scene refuses to start while one is live: it would build on a stage nobody sees and
    #: take the registry away from the app. Scene.current() is the way in instead.
    is_app: bool = False

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self.set_chunks(NameSyncedContainer())

        self._modified = False
        self._scene_file = Path()
        self._current_context: weakref.ReferenceType[ChunkBase] | None = None
        self._selected_chunks: weakref.WeakSet[ChunkBase] = weakref.WeakSet()
        self._stage_replaced_listeners: list = []
        # Scripting: scene.create, and the `with chunk:` blocks open around it.
        self._creator: Creator | None = None
        self._scopes: list = []
        self._upstream: list[ChunkBase | None] = []
        # Where this script's nodes go in an app's graph: the first node's position (None
        # for the default row), whether it has been looked up yet, and how many have been
        # placed in the row since.
        self._script_origin: tuple[float, float] | None = None
        self._script_origin_found = False
        self._script_count = 0

    @property
    def current_context(self) -> ChunkBase | None:
        """The chunk new chunks are parented to, or None for the scene root.

        add_chunk reads this. Both graph frontends have to set it -- creating a node while
        dived into a group must land inside the group -- so it is public API rather than
        the private attribute each of them was reaching into.
        """
        return self._current_context() if self._current_context else None

    @contextlib.contextmanager
    def creating_in(self, context: ChunkBase | None):
        """Parent chunks created in this block to `context`.

        Frontends use this instead of saving, assigning and restoring _current_context by hand,
        where forgetting the restore would leave every later chunk silently landing in the group.

        Nesting restores the previous context, not the root, so a dive inside a paste inside a
        group unwinds correctly.

        Args:
            context: The context to create chunks in; None means the scene root.

        Yields:
            None, with `context` current.
        """
        previous = self._current_context
        self._current_context = weakref.ref(context) if context is not None else None
        try:
            yield
        finally:
            self._current_context = previous

    def add_stage_replaced_listener(self, callback) -> None:
        """Register a callback invoked when the DCC opens a new stage.

        chunk_stage.initialize calls this so that reload_for_stage runs on file open.

        Args:
            callback: Called with no arguments after the stage is replaced.
        """
        self._stage_replaced_listeners.append(callback)

    def notify_stage_replaced(self, stage) -> None:
        """DCC implementations call this once a new stage is fully loaded."""
        for cb in self._stage_replaced_listeners:
            cb(stage)


    def remove_chunk(self, chunk: str | ChunkHash):
        """Unload a chunk by deleting its chunk object.

        Args:
            chunk: The chunk's name or hash.
        """
        if not (c := self.get_chunk(chunk)):
            return

        c.request_delete()
        self._modified = True

    # Guards the recursion in get_root_context: creating the root goes through add_chunk,
    # which asks for a root to parent the new context to.
    _creating_root_context = False

    def peek_root_context(self) -> ChunkBase | None:
        """The scene's outermost container if it exists, without creating one.

        get_root_context is find-or-CREATE, so read paths (get_chunks, and anything built
        on it) must not call it -- merely asking what the chunks are would author a context.

        Returns:
            The root context, or None.
        """
        for c in self.chunks.get_my_params():
            if isinstance(c, ChunkBase) and c.has_flag(ChunkFlags.GroupFlags.CONTEXT):
                return c
        return None

    def get_root_context(self) -> ChunkBase | None:
        """The scene's default container, created on demand. Find-or-create.

        A chunkgroup carries a module config, so add_chunk restores _current_context after
        adding it (the group must not swallow whatever is created next). That makes this
        find-or-create rather than create: without the lookup every chunk added at the root
        would mint a group of its own.

        Returns:
            The root context, or None if the chunkgroup module is unavailable.
        """
        if (existing := self.peek_root_context()) is not None:
            return existing

        module = chunk_registry.ChunkRegistry().get_chunk_module(CHUNKGROUP_MODULE)
        if module is not None:
            created = module.create() if module.loaded else module.load()
            if created:
                # The root is itself added through add_chunk, whose CONTEXT branch asks for
                # the root to parent to -- straight back into here. The flag is what stops
                # that recursion: while the root is being made, there is nothing above it.
                self._creating_root_context = True
                try:
                    return self.add_chunk(created[0])
                finally:
                    self._creating_root_context = False

        # No registry or no chunkgroup module on the search path (a bare unit test, or a
        # misconfigured USD_CHUNK_DIRS). Fall back to a plain context so a chunk still has
        # somewhere to go rather than being dropped.
        logger.warning(
            "chunk module %r unavailable; falling back to a plain context", CHUNKGROUP_MODULE
        )
        return self.add_context("Chunks")

    def add_context(self, name: str, prim_path: str | None = None) -> ChunkBase | None:
        """Add a context chunk to the scene.

        The returned ChunkBase is a plain ChunkBase with the CONTEXT flag: an organisational
        chunk that holds other chunks under a shared layer. Concrete chunks can be added to it
        with add_param and similar.

        Args:
            name (str): The name of the context
            prim_path (str, optional): USD prim path this context maps to. Chunks within it are
                placed at ``{prim_path}/chunks/{chunk_name}``. Defaults to the global /chunks root.

        Returns:
            ChunkBase | None: The context chunk that was added to the scene
        """
        path = prim_path if prim_path else "/" + name
        return self.add_chunk(ChunkBase(name, flags=ChunkFlags.GroupFlags.CONTEXT, prim_path=path))

    def set_chunks(self, chunks: NameSyncedContainer[ChunkBase]):
        """Set the container storing all the chunks, replacing every currently loaded chunk.

        Args:
            chunks (NameSyncedContainer[ChunkBase]): The container
        """
        self.chunks = chunks
        self.chunks.get_dag_node()._part_of_graph = False
        self.get_dag_node()._part_of_graph = False
        self.chunks.set_owner(self)

        for c in self.chunks.get_my_params():
            try:
                c.check_hierarchy()
            except Exception as e:
                logger.exception(e)
                c.print_hierarchy()
                continue

            # TODO: The chunk may have been loaded before, so dumb to send CHUNK_LOADED here perhaps? CHUNK_LOADED should
            # just be called when a new chunk instance is created, but after initialization
            chunk_notice.send_notice(chunk_notice.ChunkNoticeAlias.CHUNK_LOADED, c, data=c)

            if c.has_flag(ChunkFlags.GroupFlags.CONTEXT):
                for i in c.get_my_params(is_inst(ChunkBase)):
                    chunk_notice.send_notice(chunk_notice.ChunkNoticeAlias.CHUNK_LOADED, i, data=i)

        self._modified = True

    def add_chunk(self, c: ChunkBase, notify: bool = True) -> ChunkBase | None:
        """Adds a chunk to the root of the scene.

        Args:
            c (ChunkBase): The chunk to add. Ideally this should be a context chunk, and nested chunks should live underneath
            notify (bool): Whether to fire CHUNK_LOADED after adding. Pass False to defer notification (e.g. to apply stored values first).

        Returns:
            ChunkBase | None: The added chunk

        Raises:
            UnregisteredChunkTypeError: c's class is defined outside the framework and outside
                its registered chunk module, so a save and reopen would lose it. See
                chunk_core.check_chunk_type.
        """
        # First, before a rename or a context is touched. A plain chunk is checked again as
        # it enters its context (ChunkBase.add_param), which is the choke point every other
        # way in goes through; a context itself never passes there -- it goes into the flat
        # scene container below -- so it is checked here.
        check_chunk_type(c)
        names = self.chunks.get_my_names()
        # The scene container is flat and keyed by name, but the model it holds is not: two
        # chunkgroups at different prim paths may legitimately share a name, and a live scene
        # already ends up that way (a group nested in another both default to "ChunkGroup").
        # Restoring one from a file must therefore not rename it -- the file is authoritative
        # about what it is called, its prim path already makes it unique, and the name is what
        # expressions reference and what a saved file records. Renaming on load would bring a
        # scene back with a group its own expressions no longer name.
        restoring = (
            chunk_notice._loading_stage is not None
            and c.has_flag(ChunkFlags.GroupFlags.CONTEXT)
            and getattr(c, "prim_path", None)
        )
        if c.name in names and not restoring:
            c.name = c.get_valid_child_name(c.name, set(names))
        try:
            c.check_hierarchy()
        except Exception as e:
            logger.exception(e)
            return

        if c.has_flag(ChunkFlags.GroupFlags.CONTEXT):
            # A chunkgroup created with nothing dived into still belongs INSIDE the scene's
            # outermost container, exactly like a plain chunk does -- it is a node in that
            # container's graph. Without this it would fall back to the /chunks scope and
            # become a SIBLING of the root (/chunks/Group next to /chunks/ChunkGroup) -- a
            # node in no graph at all, never shown.
            # peek_root_context is None while the root itself is being created, which is what
            # keeps the root from trying to parent itself.
            parent = self._current_context
            # Only a real chunkgroup nests. add_context() makes plain module-less contexts
            # -- the named tabs load_chunks recreates from a file -- and those ARE the
            # container for what they hold; giving them a parent conjured a spurious
            # default group around every tab on reload.
            is_chunkgroup = bool(c.get_config() and c.get_config().module)
            if (
                parent is None
                and is_chunkgroup
                and not self._creating_root_context
                # Never while loading: the file's own structure is authoritative. A saved
                # chunkgroup arriving here would otherwise conjure a second, empty default
                # to nest under, and then lose a name collision with it -- reopening a
                # scene produced ChunkGroup (empty) plus ChunkGroup1 (the real one).
                and chunk_notice._loading_stage is None
            ):
                # Find-or-CREATE: a chunkgroup made when the scene is still empty must not
                # become the container itself -- it is a node dropped into the default
                # group, exactly like one made later. Otherwise whether a group is a node
                # or a container would depend on how empty the scene happened to be.
                root = self.get_root_context()
                if root is not None and root is not c:
                    parent = weakref.ref(root)
            c._parent_context = parent
            # A CONTEXT is not in the flat scene container, so the name check above cannot
            # see the one collision that matters most: a group taking the name of the group
            # it sits inside. A new scene produced /chunks/ChunkGroup/ChunkGroup -- two tabs
            # both reading "ChunkGroup", and a context path that repeated itself.
            #
            # Only for a group being CREATED. A restored one keeps its name whatever it
            # collides with: the file is authoritative about what it is called, its prim
            # path already makes it unique, and the name is what expressions reference.
            parent_ctx = parent() if parent else None
            # `restoring` above is not enough on its own: it also requires prim_path, which
            # a nested group does not have yet at this point in a load, so relying on it
            # renamed exactly the groups a file is most authoritative about. During a load,
            # nothing here renames anything.
            loading = chunk_notice._loading_stage is not None
            if parent_ctx is not None and not restoring and not loading:
                reserved = set(names) | set(parent_ctx.get_reserved_names())
                if c.name in reserved:
                    c.name = c.get_valid_child_name(c.name, reserved)
            self._current_context = weakref.ref(c)
            # User-created group chunks (have a module config) need PARAM_ADDED to fire
            # so ChunkStageSync can create their USD prim. Tab contexts skip this.
            send_added = bool(c.get_config() and c.get_config().module)
            self.chunks.add(c, send_added)
            self.chunks.add_nested(c.children)

            if send_added:
                # Restore parent context so subsequent chunks don't fall into the group.
                self._current_context = c._parent_context
            if notify:
                chunk_notice.send_notice(chunk_notice.ChunkNoticeAlias.CHUNK_LOADED, c, data=c)

        elif self._current_context and (ctx := self._current_context()):
            ctx.add_param(c, notify=notify)
        # Nowhere to put it yet: fall back to the scene's default chunkgroup, which
        # _resolve_chunk_root places under the /chunks scope.
        elif ctx := self.get_root_context():
            ctx.add_param(c, notify=notify)
        else:
            logger.error("Failed to add chunk %s", c)
            return

        self._modified = True
        return c

    def add_chunks(self, chunks: Sequence[ChunkBase]):
        """Add all chunks in the sequence.

        Args:
            chunks (Sequence[ChunkBase]): The chunks to add to the scene
        """
        for c in chunks:
            self.add_chunk(c)

    def clear(self):
        """Clear the chunk container.

        This may not delete the chunks if references to them are held elsewhere.
        """
        self.chunks.clear()
        self._modified = True

    def holds_chunk(self, chunk: str | ChunkHash) -> bool:
        """Check if the scene holds a chunk with name or hash.

        Args:
            chunk (str | ChunkHash): The name or hash of the chunk to find

        Returns:
            bool: True if the scene holds the chunk, False otherwise
        """
        return self.get_chunk(chunk) is not None

    def get_chunk(self, chunk: str | ChunkHash) -> ChunkBase | None:
        """Return a single chunk held by the scene.

        Unlike get_chunks, this may return context chunks as well.

        Args:
            chunk (str | ChunkHash): The name or hash of the chunk to find

        Returns:
            ChunkBase | None: The found chunk, or None
        """
        res = self.chunks.get_param(chunk)
        return res if isinstance(res, ChunkBase) else None

    def get_context_for_prim_path(self, path: str) -> ChunkBase | None:
        """Return the CONTEXT chunk whose prim_path matches path, or None."""
        for c in self.get_chunks():
            if isinstance(c, ChunkBase) and c.has_flag(ChunkFlags.GroupFlags.CONTEXT):
                if getattr(c, "prim_path", None) == path:
                    return c
        return None

    def get_contexts(self) -> tuple[ChunkBase, ...]:
        """Every container in the scene, including the ones get_chunks() leaves out.

        A container owns lofted interface params, and those are the names a tool presents
        to whoever drives it -- so anything resolving a param by owner rather than by
        execution order has to see all of them.

        get_chunks() answers a different question: what is a node in some graph. The
        outermost container is a node in nothing (it has no parent graph to be one in),
        and a plain module-less context never was one either. Both still hold interface
        params, which is the gap this fills.

        Stated as "every container" rather than "every CONTEXT chunk including plain tabs",
        because a plain tab is the legacy shape: a new scene's default container is a real
        chunkgroup under the /chunks scope.

        Returns:
            Every container chunk.
        """
        seen: set = set()
        res: list = []
        stack: list = list(self.chunks.get_my_params())
        while stack:
            i = stack.pop()
            if not isinstance(i, ChunkBase) or i in seen:
                continue
            seen.add(i)
            if i.has_flag(ChunkFlags.GroupFlags.CONTEXT):
                res.append(i)
            stack.extend(i.get_my_params())
        return tuple(res)

    def get_chunks(self) -> tuple[ChunkBase, ...]:
        """Every chunk that is a node in some graph, nested chunkgroups included.

        The scene's outermost container is the one context that is not a node in any graph: a
        chunkgroup is a node in its PARENT's graph, and this one has no parent graph to be a
        node in. It is left out by identity, not depth: add_chunk puts EVERY context straight
        into self.chunks, so a user-created chunkgroup sits at the same container depth as the
        root and a depth test would hide all of them.

        Returns:
            The chunks.
        """
        root = self.peek_root_context()
        seen: set = set()
        res: list = []
        stack: list = list(self.chunks.get_my_params())
        while stack:
            i = stack.pop()
            if not isinstance(i, ChunkBase) or i in seen:
                continue
            seen.add(i)
            cfg = i.get_config()
            is_group_chunk = i.has_flag(ChunkFlags.GroupFlags.CONTEXT) and cfg and cfg.module
            # A plain tab context is skipped for the same reason at any depth: it predates
            # the chunkgroup type and was never a node either.
            if i.has_flag(ChunkFlags.GroupFlags.CONTEXT) and (not is_group_chunk or i is root):
                # Don't include it, but visit its children.
                stack.extend(i.get_my_params())
            else:
                res.append(i)
                if is_group_chunk:
                    # A nested ChunkGroup is a node in this graph AND a container: include
                    # it and recurse so its sub-chunks stay reachable.
                    stack.extend(i.get_my_params())
        return tuple(res)

    def clear_chunk_selection(self):
        """Clear the chunk selection."""
        for i in self.get_selected_chunks():
            i.set_selected(False)

    def get_selected_chunks(self) -> tuple[ChunkBase, ...]:
        """Get the currently selected chunks.

        Returns:
            tuple[ChunkBase, ...]: The selected chunks
        """
        return tuple(self._selected_chunks)

    def set_selected_chunks(self, selection: Sequence[ChunkBase]):
        """Replace the current chunk selection.

        Args:
            selection (Sequence[ChunkBase]): The new selection to set
        """
        old_selection = self.get_selected_chunks()
        for c in old_selection:
            if c not in selection:
                c.set_selected(False)

        for c in selection:
            if c not in old_selection:
                c.set_selected(True)

    def is_modified(self) -> bool:
        """Return whether the scene has been modified since it was created.

        Returns:
            bool: True if the scene has been modified, False otherwise
        """
        return self._modified

    def data_changed(self, notice: chunk_notice.ChunkNotice | None = None) -> chunk_notice.ChunkNotice | None:
        """Track chunk selection from STATE_CHANGED notices, and mark the scene modified.

        A change to a chunk's SELECTED bit only updates the selection; it does not count as a
        modification.

        Args:
            notice: The notice being handled.

        Returns:
            The notice to propagate.
        """
        if (
            notice
            and chunk_notice.is_notice_type(notice, chunk_notice.ChunkNoticeAlias.STATE_CHANGED)
            and isinstance(notice.data.get_owner(), ChunkBase)
        ):
            # Toggle the selected chunk in self._selected_chunks
            old_state = ChunkFlags.State(notice.data.old_value)
            new_state = ChunkFlags.State(notice.data.value)
            added = new_state ^ (new_state & old_state)
            removed = old_state ^ (new_state & old_state)
            if ChunkFlags.State.SELECTED in (added | removed):
                self._selected_chunks ^= set((notice.data.get_owner(),))
                # Return early to skip changing self._modified
                return super().data_changed(notice)

        self._modified = True
        return super().data_changed(notice)

    def send_self(
        self, alias: chunk_notice.ChunkNoticeAlias | None = None, data: Any = None
    ) -> chunk_notice.ChunkNotice | None:
        """Send a notice from the scene, marking it modified.

        Args:
            alias: The notice alias to send.
            data: The notice data.

        Returns:
            The notice that was sent.
        """
        self._modified = True
        return super().send_self(alias, data)

    @abstractmethod
    def should_install_package(self, package_name: str) -> bool:
        """Whether a chunk's pip requirement may be installed automatically.

        When a module's config requires external pip packages, the ChunkDependencyManager can
        install them into the chunk package directory, but the user must be able to opt out.
        This is called before each install; a host asks the user or applies its own policy.

        Args:
            package_name (str): The name of the package to install

        Returns:
            bool: True if the package may be installed
        """
        return False

    @abstractmethod
    def get_stage(self) -> Usd.Stage | None:
        """Get the current scene stage. Implemented by a host.

        Returns:
            Usd.Stage | None: The stage if available
        """
        return None

    @abstractmethod
    def get_selected_prims(self) -> Sequence[Usd.Prim]:
        """Get the selected prims in the scene. Implemented by a host.

        Returns:
            Sequence[Usd.Prim]: Selected prims
        """
        return []

    @abstractmethod
    def get_selected_property(self) -> Usd.Property | None:
        """Get the selected property in the scene. Implemented by a host.

        Returns:
            Usd.Property | None: Selected property
        """
        return None

    @abstractmethod
    def get_active_camera(self) -> Gf.Camera | None:
        """Return the current viewport camera as a Gf.Camera, or None if unsupported."""
        return None

    @abstractmethod
    def get_viewport_renderer(self) -> str:
        """Hydra plugin id the viewport is drawing with, or "" when there is no viewport.

        Used to decide which renderer's settings are worth sending: a delegate ignores a
        namespace it does not own, so sending it another renderer's is only wasted work.

        Returns:
            The plugin id.
        """
        return ""

    @abstractmethod
    def set_viewport_renderer_settings(self, settings: Mapping[str, Any]) -> None:
        """Send renderer settings straight to the viewport's render delegate.

        Deliberately not scene description. Most delegates -- Cycles and RPR among them
        -- never read a UsdRenderSettings prim, and take settings only through this
        call, so a chunk that authors a prim and stops has no effect on what they draw.
        Authoring stays the portable record; this is what makes the viewport agree with
        it.

        Every value is sent, so the caller is the one that must avoid re-sending what has
        not changed: a delegate generally restarts its render on each setting it is
        handed, whether or not the value is new.

        Args:
            settings (Mapping[str, Any]): Setting key to value, keys spelled the way the
                delegate declares them rather than as USD attribute names -- the two
                differ for a renderer whose keys are not valid USD identifiers.
        """

    @abstractmethod
    def get_current_frame(self) -> float:
        """Get the current frame. Implemented by a host.

        Returns:
            float: The current frame
        """
        raise NotImplementedError

    @abstractmethod
    def get_end_frame(self) -> float:
        """Get the end frame of the playback range. Implemented by a host.

        Returns:
            float: The end frame
        """
        raise NotImplementedError

    @abstractmethod
    def get_start_frame(self) -> float:
        """Get the start frame of the playback range. Implemented by a host.

        Returns:
            float: The start frame
        """
        raise NotImplementedError

    @abstractmethod
    def set_current_frame(self, frame: float):
        """Set the current frame. Implemented by a host.

        Args:
            frame (float): The frame to set
        """
        raise NotImplementedError

    @abstractmethod
    def set_end_frame(self, frame: float):
        """Set the end frame of the playback range. Implemented by a host.

        Args:
            frame (float): The frame to set
        """
        raise NotImplementedError

    @abstractmethod
    def set_start_frame(self, frame: float):
        """Set the start frame of the playback range. Implemented by a host.

        Args:
            frame (float): The frame to set
        """
        raise NotImplementedError

    def set_viewport_camera(self, prim: UsdGeom.Camera) -> None:
        """Set the viewport to look through the given camera prim."""
        pass

    def look_at(self, point: Gf.Vec3d) -> None:
        """Turn the viewport camera in place to face the world-space `point`."""
        pass

    def look_at_selection(self) -> bool:
        """Turn the viewport camera to face the selected prims. False if none is measurable."""
        from usd_chunks import chunk_utils  # deferred: chunk_utils imports this package's model

        stage = self.get_stage()
        point = chunk_utils.focus_point_of(stage, self.get_selected_prims()) if stage else None
        if point is None:
            return False
        self.look_at(point)
        return True

    def release_viewport_camera(self) -> None:
        """The prim handed to set_viewport_camera no longer claims the viewport.

        Called when a camera chunk's freecam lock comes off. A host that swapped state to
        show the prim (usdview keeps the user's free-camera lens across it) undoes that here;
        the default is nothing, matching set_viewport_camera.
        """
        pass

    # -- Gizmo capability -----------------------------------------------------------------
    #
    # Reserved shape only: nothing in v1 calls these. Activation is declarative -- a gizmo's
    # `editmode` says onPrimSelection, the host already knows its own viewport selection, and
    # the layer stack does the rest, so no push from us is needed. They exist for the case
    # `editmode = onChunkSelection` describes, where node-graph selection should drive
    # viewport handles and the host has no way to know which chunk is selected.
    #
    # Non-abstract with safe defaults, exactly like get_active_camera / set_viewport_camera
    # above: usdview implements those, Kraken does not, and both work.

    def supports_gizmos(self) -> bool:
        """Whether this host can display chunk gizmos at all."""
        return False

    def show_gizmos(self, chunk: ChunkBase, gizmos: list) -> None:
        """Ask the host to display these gizmos. See chunk_gizmo.resolve_gizmos."""
        pass

    def hide_gizmos(self, chunk: ChunkBase) -> None:
        """Ask the host to stop displaying this chunk's gizmos."""
        pass

    def primSelectionChangedCallback(self, *args, **kwargs):
        """Send PRIM_SELECTION_CHANGED with the host's selected prims."""
        chunk_notice.send_notice(
            chunk_notice.ChunkNoticeAlias.PRIM_SELECTION_CHANGED, sender=self, data=self.get_selected_prims()
        )

    def propSelectionChangedCallback(self, *args, **kwargs):
        """Send PROP_SELECTION_CHANGED with the host's selected property."""
        chunk_notice.send_notice(
            chunk_notice.ChunkNoticeAlias.PROP_SELECTION_CHANGED, sender=self, data=self.get_selected_property()
        )

    def activeCameraChangedCallback(self, *args, **kwargs):
        """Send CAMERA_CHANGED with the host's active camera."""
        chunk_notice.send_notice(
            chunk_notice.ChunkNoticeAlias.CAMERA_CHANGED, sender=self, data=self.get_active_camera()
        )

    def get_notice_alias(self) -> chunk_notice.ChunkNoticeAlias:
        """The scene's own notices carry no alias."""
        return chunk_notice.ChunkNoticeAlias.NONE

    # -- Scripting ------------------------------------------------------------------
    # The same calls in any host: a script run headless (usd_chunks.Scene) and a line typed
    # into usdview's interpreter (usd_chunks.current_scene()) build chunks the same way, and
    # in a host with a graph they land in the network it is showing -- the frontend keeps
    # current_context on it.

    def __enter__(self) -> SceneSender:
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
        """add_chunk, at the position _next_node_position picks."""
        from usd_chunks import chunk_stage  # deferred: chunk_stage imports this module

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
        execute_roots(self.get_chunks())

    # save, export and commit live here rather than on the script's Scene, so they work on
    # usdview's scene too (Scene.current()). Deferred imports: both modules import this one.

    def save(self, path: str) -> None:
        """Write the network itself -- Chunk prims and wires -- so it reopens as a graph."""
        from usd_chunks import chunk_stage

        chunk_stage.export_chunks(self, path)

    def export(self, path: str) -> None:
        """Write the result as plain USD, with the Chunk prims stripped out."""
        from usd_chunks import chunk_script

        chunk_script.flatten_without_chunks(self.get_stage()).Export(path)

    def commit(self) -> None:
        """Run every chunk's commit hook -- the ones that write files, like Export USD."""
        from usd_chunks import chunk_scheduler, chunk_stage

        stage = self.get_stage()
        chunk_scheduler.commit_all(
            self.get_chunks(),
            order=lambda cs: chunk_stage.commit_order_from_stage(cs, stage),
        )

    def enter_chunk(self, target: ChunkBase) -> None:
        """`with target:` -- a chunkgroup takes what is created inside as its children;
        any other chunk runs before it."""
        cfg = target.get_config()
        if cfg and cfg.module == CHUNKGROUP_MODULE:
            scope = self.creating_in(target)
            scope.__enter__()
            # A chunk inside the group runs after nothing outside it: exec wires stay
            # within one context, as the graph draws them.
            self._scopes.append((scope, None))
        else:
            self._scopes.append((None, target))
        self._upstream.append(self._scopes[-1][1])

    def exit_chunk(self, target: ChunkBase) -> None:
        """Leave the `with target:` block."""
        scope, _ = self._scopes.pop()
        self._upstream.pop()
        if scope is not None:
            scope.__exit__(None, None, None)

    # Optional DCC capability with a no-op default so hosts that don't support
    # it still work; usdview overrides it (ctrl+click on a prim path field).
    def select_prims(self, paths) -> bool:
        """Make `paths` the host's prim selection, replacing what was selected.

        The other direction from get_selected_prims().

        Args:
            paths: Prim paths to select.

        Returns:
            False when the host has no selection it can drive, so a caller can leave the click
            to do its normal job.
        """
        return False

    # Optional DCC capability with a no-op default so hosts that don't support
    # it still work; usdview overrides it (used by the RBD solver after baking).
    def refresh_timeline(self, start: float, end: float) -> None:
        """Update the host timeline range."""


def current_scene() -> SceneSender | None:
    """The live scene -- usdview's inside usdview, a script's Scene in a script."""
    registry = chunk_registry.ChunkRegistry()
    return registry.scene if registry.initialized else None


class Creator:
    """`scene.create`: one function per chunk module and per menu variant.

    A module is found by its file name (`transform`, `edit_target`); a variant by its menu
    label in snake_case ("Dome Light" -> `dome_light`). A module name wins over a variant
    label, and a label two variants share is left out rather than guessed at.

    Each function takes its input chunks positionally -- what is wired into its input
    port -- then `name=` for the node's name and params by keyword; see SceneSender.place
    and SceneSender.set.
    """

    def __init__(self, scene: SceneSender):
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


def all_chunks(chunks) -> list[ChunkBase]:
    """Every chunk inside the given ones, groups excluded, at any depth."""
    found = []
    for c in chunks:
        sub = [p for p in c.get_my_params() if isinstance(p, ChunkBase)]
        found.extend(all_chunks(sub) if sub else [c])
    return found


def execute_roots(chunks) -> None:
    """Call execute() on every root node (no dependencies), recursing into groups."""
    for c in chunks:
        sub = [p for p in c.get_my_params() if isinstance(p, ChunkBase)]
        if sub:
            execute_roots(sub)
        elif not c.get_dependencies():
            c.execute()
