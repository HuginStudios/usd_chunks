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

chunk_core re-exports SceneSender lazily (see its module __getattr__) so the many
existing `from usd_chunks.chunk_core import SceneSender` imports keep working.
"""
from __future__ import annotations

import contextlib
import logging
import weakref
from abc import abstractmethod
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

from pxr import Gf, Usd, UsdGeom

from usd_chunks import chunk_notice, chunk_registry
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_core import (
    ChunkBase,
    ChunkHash,
    NameSyncedContainer,
    is_inst,
)

logger = logging.getLogger(__name__)


# The chunk module backing every context. "context == chunkgroup": a CONTEXT chunk with no
# module was only ever a stand-in from before this type existed. The /chunks prim itself is a
# scope (plugins-layer _CHUNKS_ROOT), like /lights or /render -- nothing lives at it, and the
# default chunkgroup sits under it at /chunks/<name>.
CHUNKGROUP_MODULE = "chunkgroup"


class SceneSender(chunk_notice.ChunkNoticePropagator):
    """
    Abstract class for implementing scene access for different DCC's. Should have access to the UsdStage and send notices for scene changes.
    See implementation in plugins/usdview/__init__.py for an example implementation.

    The class is not purely abstract, as most functions dealing with chunks are implemented. However, a subclass should implement all relevant
    @abstractmethod functions.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self.set_chunks(NameSyncedContainer())

        self._modified = False
        self._scene_file = Path()
        self._current_context: Optional[weakref.ReferenceType[ChunkBase]] = None
        self._selected_chunks: weakref.WeakSet[ChunkBase] = weakref.WeakSet()
        self._stage_replaced_listeners: list = []

    @property
    def current_context(self) -> "ChunkBase | None":
        """The chunk new chunks are parented to, or None for the scene root.

        add_chunk reads this. Both graph frontends have to set it -- creating a node while
        dived into a group must land inside the group -- so it is public API rather than
        the private attribute each of them was reaching into.
        """
        return self._current_context() if self._current_context else None

    @contextlib.contextmanager
    def creating_in(self, context: "ChunkBase | None"):
        """Parent chunks created in this block to `context`.

        Replaces the save-assign-restore each frontend wrote by hand around
        _current_context: Qt's _push_nest_context and Flet's _nest were the same six lines
        against a private attribute, and both had to remember the restore or every later
        chunk would silently keep landing in the group.

        Passing None means the scene root. Nesting restores the previous context, not the
        root, so a dive inside a paste inside a group unwinds correctly.
        """
        previous = self._current_context
        self._current_context = weakref.ref(context) if context is not None else None
        try:
            yield
        finally:
            self._current_context = previous

    def add_stage_replaced_listener(self, callback) -> None:
        """Register a callback invoked when the DCC opens a new stage.
        Called by chunk_usd.initialize so that reload_for_stage runs on file open."""
        self._stage_replaced_listeners.append(callback)

    def notify_stage_replaced(self, stage) -> None:
        """DCC implementations call this once a new stage is fully loaded."""
        for cb in self._stage_replaced_listeners:
            cb(stage)


    def remove_chunk(self, chunk: str | ChunkHash):
        """
        Unload this module by deleting the associated chunk object
        """

        if not (c := self.get_chunk(chunk)):
            return

        c.request_delete()
        self._modified = True

    # Guards the recursion in get_root_context: creating the root goes through add_chunk,
    # which asks for a root to parent the new context to.
    _creating_root_context = False

    def peek_root_context(self) -> "ChunkBase | None":
        """The scene's outermost container if it exists, without creating one.

        get_root_context is find-or-CREATE, so read paths (get_chunks, and anything built
        on it) must not call it -- merely asking what the chunks are would author a context.
        """
        for c in self.chunks.get_my_params():
            if isinstance(c, ChunkBase) and c.has_flag(ChunkFlags.GroupFlags.CONTEXT):
                return c
        return None

    def get_root_context(self) -> "ChunkBase | None":
        """The scene's default container, created on demand. Find-or-create.

        A chunkgroup carries a module config, so add_chunk restores _current_context after
        adding it (the group must not swallow whatever is created next). That makes this
        find-or-create rather than create: without the lookup every chunk added at the root
        would mint a group of its own.
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
            f"chunk module {CHUNKGROUP_MODULE!r} unavailable; falling back to a plain context"
        )
        return self.add_context("Chunks")

    def add_context(self, name: str, prim_path: Optional[str] = None) -> ChunkBase | None:
        """
        Adds a context chunk to the scene. Concrete chunks can be added to the context using add_param and similar. The returned
        ChunkBase object is just a ChunkBase with the CONTEXT flag. This can be used as a organisatorial chunk that contains other chunks
        under a shared layer

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
        """
        Sets the container storing all the chunks. Will override all currently loaded chunks

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
        """
        Adds a chunk to the root of the scene.

        Args:
            c (ChunkBase): The chunk to add. Ideally this should be a context chunk, and nested chunks should live underneath
            notify (bool): Whether to fire CHUNK_LOADED after adding. Pass False to defer notification (e.g. to apply stored values first).

        Returns:
            ChunkBase | None: The added chunk
        """

        names = self.chunks.get_my_names()
        # The scene container is flat and keyed by name, but the model it holds is not: two
        # chunkgroups at different prim paths may legitimately share a name, and a live scene
        # already ends up that way (a group nested in another both default to "ChunkGroup").
        # Restoring one from a file must therefore not rename it -- the file is authoritative
        # about what it is called, its prim path already makes it unique, and the name is what
        # expressions reference and what a saved file records. Renaming on load meant a scene
        # came back with a group its own expressions no longer named.
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
            # container's graph. Without this it fell back to the /chunks scope and became a
            # SIBLING of the root instead (/chunks/Group next to /chunks/ChunkGroup), so it
            # was a node in no graph at all and simply never appeared (live-reported).
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
                # group, exactly like one made later ("droppar du ned en chunkgroup i
                # graphen ska den neatly under parentchunkgroupen"). Without this the very
                # first group silently became the root, so whether a group was a node or a
                # container depended on how empty the scene happened to be.
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
            logger.error(f"Failed to add chunk {c}")
            return

        self._modified = True
        return c

    def add_chunks(self, chunks: Sequence[ChunkBase]):
        """
        Add all chunks in the sequence

        Args:
            chunks (Sequence[ChunkBase]): The chunks to add to the scene
        """
        for c in chunks:
            self.add_chunk(c)

    def clear(self):
        """
        Clear the chunk container. This may not delete the chunks if string references to the chunks
        are maintained elsewhere
        """
        self.chunks.clear()
        self._modified = True

    def holds_chunk(self, chunk: str | ChunkHash) -> bool:
        """
        Check if the scene holds a chunk with name or hash

        Args:
            chunk (str | ChunkHash): The name or hash of the chunk to find

        Returns:
            bool: True if the scene holds the chunk, False otherwise
        """
        return self.get_chunk(chunk) is not None

    def get_chunk(self, chunk: str | ChunkHash) -> ChunkBase | None:
        """
        Return a single chunk held by the scene. If no chunk is found, None is returned.
        Contrary to get_chunks, this may return context chunks as well

        Args:
            chunk (str | ChunkHash): The name or hash of the chunk to find

        Returns:
            ChunkBase | None: The found chunk or None
        """
        res = self.chunks.get_param(chunk)
        return res if isinstance(res, ChunkBase) else None

    def get_context_for_prim_path(self, path: str) -> "ChunkBase | None":
        """Return the CONTEXT chunk whose prim_path matches path, or None."""
        for c in self.get_chunks():
            if isinstance(c, ChunkBase) and c.has_flag(ChunkFlags.GroupFlags.CONTEXT):
                if getattr(c, "prim_path", None) == path:
                    return c
        return None

    def get_contexts(self) -> Tuple[ChunkBase, ...]:
        """Every container in the scene, including the ones get_chunks() leaves out.

        A container owns lofted interface params, and those are the names a tool presents
        to whoever drives it -- so anything resolving a param by owner rather than by
        execution order has to see all of them.

        get_chunks() answers a different question: what is a node in some graph. The
        outermost container is a node in nothing (it has no parent graph to be one in),
        and a plain module-less context never was one either. Both still hold interface
        params, which is the gap this fills.

        Stated as "every container" rather than "every CONTEXT chunk including plain
        tabs", because a plain tab is the legacy shape -- context == chunkgroup now, and
        a new scene's default container is a real chunkgroup under the /chunks scope. This
        stays correct as those disappear.
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

    def get_chunks(self) -> Tuple[ChunkBase, ...]:
        # The scene's outermost container is the one context that is not a node in any
        # graph. Dan: "default chunkgroupen ska inte vara en chunk i graphen, den ska vara
        # en upp" -- a chunkgroup is a node in its PARENT's graph, and this one has no
        # parent graph to be a node in.
        #
        # Identity, not depth: add_chunk puts EVERY context straight into self.chunks, so a
        # user-created chunkgroup sits at the same container depth as the root and a
        # depth test hides all of them (live-reported: no chunkgroup visible in the graph,
        # and none creatable).
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
        """
        Clear the chunk selection
        """
        for i in self.get_selected_chunks():
            i.set_selected(False)

    def get_selected_chunks(self) -> Tuple[ChunkBase, ...]:
        """
        get the currently selected chunks

        Returns:
            Tuple[ChunkBase, ...]: The selected chunks
        """
        return tuple(self._selected_chunks)

    def set_selected_chunks(self, selection: Sequence[ChunkBase]):
        """
        Deselects the currently selected chunks and sets the selection to the sequence of
        chunks passed as selection

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
        """
        Returns whether the scene has been modified since created

        Returns:
            bool: True if the scene has been modified, False otherwise
        """
        return self._modified

    def data_changed(self, notice: chunk_notice.ChunkNotice | None = None) -> chunk_notice.ChunkNotice | None:
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
        self._modified = True
        return super().send_self(alias, data)

    @abstractmethod
    def should_install_package(self, package_name: str) -> bool:
        """
        When a modules config has requirements to external pip packages, the packages may be installed automatically by the ChunkDependencyManager.
        Before any package is installed though, the user should have the option to opt out of this. This method is called when packages can be installed,
        and should return True if the package may be installed. Any installed package is put in the

        Args:
            package_name (str): The name of the package to install

        Returns:
            bool: If the package can be installed
        """
        return False

    @abstractmethod
    def get_stage(self) -> Usd.Stage | None:
        """
        Get the current scene stage. Should be implemented by a plugin

        Returns:
            Usd.Stage | None: The stage if available
        """
        return None

    @abstractmethod
    def get_selected_prims(self) -> Sequence[Usd.Prim]:
        """
        Get the selected prims in the scene. Should be implemented by a plugin

        Returns:
            Sequence[Usd.Prim]: Selected prims
        """
        return []

    @abstractmethod
    def get_selected_property(self) -> Usd.Property | None:
        """
        Get the selected property in the scene. Should be implemented by a plugin

        Returns:
            Usd.Property: Selected property
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
        """
        Get the current

        Returns:
            float: _description_
        """
        raise NotImplementedError

    @abstractmethod
    def get_end_frame(self) -> float:
        """
        Get the current end frame

        Returns:
            float: _description_
        """
        raise NotImplementedError

    @abstractmethod
    def get_start_frame(self) -> float:
        """
        Get the current start frame

        Returns:
            float: _description_
        """
        raise NotImplementedError

    @abstractmethod
    def set_current_frame(self, frame: float):
        """
        In the plugin implementation, set the current frame

        Args:
            frame (float): The frame to set
        """
        raise NotImplementedError

    @abstractmethod
    def set_end_frame(self, frame: float):
        """
        In the plugin implementation, set the end frame

        Args:
            frame (float): The frame to set
        """
        raise NotImplementedError

    @abstractmethod
    def set_start_frame(self, frame: float):
        """
        In the plugin implementation, set the start frame

        Args:
            frame (float): The frame to set
        """
        raise NotImplementedError

    def set_viewport_camera(self, prim: UsdGeom.Camera) -> None:
        """Set the viewport to look through the given camera prim."""
        pass

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

    def show_gizmos(self, chunk: "ChunkBase", gizmos: list) -> None:
        """Ask the host to display these gizmos. See chunk_gizmo.resolve_gizmos."""
        pass

    def hide_gizmos(self, chunk: "ChunkBase") -> None:
        """Ask the host to stop displaying this chunk's gizmos."""
        pass

    def primSelectionChangedCallback(self, *args, **kwargs):
        chunk_notice.send_notice(
            chunk_notice.ChunkNoticeAlias.PRIM_SELECTION_CHANGED, sender=self, data=self.get_selected_prims()
        )

    def propSelectionChangedCallback(self, *args, **kwargs):
        chunk_notice.send_notice(
            chunk_notice.ChunkNoticeAlias.PROP_SELECTION_CHANGED, sender=self, data=self.get_selected_property()
        )

    def activeCameraChangedCallback(self, *args, **kwargs):
        chunk_notice.send_notice(
            chunk_notice.ChunkNoticeAlias.CAMERA_CHANGED, sender=self, data=self.get_active_camera()
        )

    def get_notice_alias(self) -> chunk_notice.ChunkNoticeAlias:
        return chunk_notice.ChunkNoticeAlias.NONE

    # Optional DCC capability with a no-op default so hosts that don't support
    # it still work; usdview overrides it (ctrl+click on a prim path field).
    def select_prims(self, paths) -> bool:
        """Make `paths` the host's prim selection, replacing what was selected.

        The other direction from get_selected_prims(). Returns False when the host has no
        selection it can drive, so a caller can leave the click to do its normal job.
        """
        return False

    # Optional DCC capability with a no-op default so hosts that don't support
    # it still work; usdview overrides it (used by the RBD solver after baking).
    def refresh_timeline(self, start: float, end: float) -> None:
        """Update the host timeline range."""
