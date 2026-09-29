"""Notices and the dependency graph behind every chunk object.

Tf.Notice is USD's built-in callback system, similar to Qt's signals. This module uses
Tf.Notice extensively but hides most of its API, so a client can be up and running with
callbacks without managing the lifetime of listeners.

ChunkNoticeAlias names each supported notice class, so the notice you are interested in
can be found without caring how it is implemented. Every custom notice subclasses
ChunkNotice, which carries additional data whose type depends on the notice and on who
sends it.

The easiest way to send a notice is explicitly, with send_notice:

``` py
send_notice(ChunkNoticeAlias.PARAM_CHANGED, some_param, {"any": "data"})
```
"""
from __future__ import annotations

import collections
import functools
import inspect
import itertools
import logging
import weakref
from abc import abstractmethod
from contextlib import contextmanager
from enum import IntEnum, auto
from collections.abc import Callable, Generator, Iterator
from typing import (
    Any,
    TypeAlias,
    TypeVar,
    TYPE_CHECKING,
    cast,
    overload,
    TypeGuard,
    Literal,
)

from attrs import define, field
from pxr import Sdf, Tf, Usd, Gf
from typing_extensions import Self, override
from usd_chunks import chunk_scheduler

logger = logging.getLogger(__name__)

# Set to True while a param is writing its value to USD attrs so that
# ChunkStageSync._on_objects_changed skips those synchronous ObjectsChanged callbacks.
_usd_write_active: bool = False
_loading_stage = None  # Set during load_chunks so callbacks can access the stage during file open

# Set while ChunkLayerManager is rearranging its OWN layers -- the per-chunk procedural layers
# and the graph layers that group them. Layer stack changes normally invalidate Edit Target
# resolution (a marker only counts while the layer it names is composed), but these layers can
# never be what a marker names: the Edit Target picker excludes them by construction, and they
# are anonymous, so a saved identifier could not match one across sessions. Every chunk creates
# one as it first cooks, so treating them as ordinary layer stack changes throws the resolution
# away once per chunk and makes cooking a graph quadratic.
_internal_layer_write_active: bool = False

# The chunk whose module.execute() is currently running, or None. Execution-scoped (set and
# cleared around each execute() call below), NOT persistent config -- it only answers "who is
# authoring right now" so chunk_utils.get_edit_layer can resolve that chunk's Edit Target from
# graph topology when a caller (e.g. author_in_variant_context) didn't pass an explicit owner.
_executing_chunk = None

# The propagators whose solve an enclosing batched_solve() is holding back, in the order they
# asked, each with the batch clock at the time it asked; None outside a batch.
_batched: list | None = None

# Inside a batch, a clock that ticks once per queued solve and once per cook, and when each
# chunk last cooked by it. None outside a batch. See _cooked_since.
_batch_clock = 0
_batch_cooked: dict | None = None

# The batch clock when the solve now draining was queued; None when no queued solve is running.
_solving_since: int | None = None

# Follow-up work held until an enclosing batch has drained, by key; None outside a batch.
_batch_deferred: dict | None = None


def defer_to_batch_end(key, fn) -> bool:
    """Run fn once when the enclosing batch has drained, instead of now.

    For bookkeeping that only has to be right once the dust settles and costs something each
    time -- refreshing a chunk's gizmo anchors reads every param it has, and a Wedge change
    asked for it once per param written, hundreds of times. Deferred under the same key it
    runs once, with the batch's final values.

    Args:
        key: Identifies the work, so asking twice runs it once.
        fn: What to run.

    Returns:
        False outside a batch -- the caller runs fn itself, straight away.
    """
    if _batch_deferred is None:
        return False
    _batch_deferred[key] = fn
    return True


def _tick() -> int:
    global _batch_clock
    _batch_clock += 1
    return _batch_clock


def _cooked_since(prop) -> bool:
    """Whether prop's chunk already cooked after the change the draining solve is for.

    A batch solves the changes it held back one after another, and the first solve cooks
    everything below them with every new value in place. Expression params re-announce their
    value only once the param they read has changed -- which happens during that first solve,
    so they are queued behind it, and solving them re-cooked every chunk below a second time
    with the same values. A chunk that cooked after the queued change was made has read it
    already: its module is skipped, while everything else the solve does -- widgets, the
    param's own USD write -- still happens. A later change is queued with a later clock, and
    cooks it again.
    """
    if _batch_cooked is None or _solving_since is None:
        return False
    return _batch_cooked.get(id(prop), -1) > _solving_since


@contextmanager
def batched_solve():
    """Hold back every solve until the block ends, then solve once.

    Setting a value solves everything downstream of it straight away, which is right for
    one edit and wasteful for many: setting N values that feed the same chunks cooks those
    chunks N times, each against a half-updated set of inputs, and only the last cook
    counts. A Wedge cooking one variant sets every range it has -- dozens -- and that alone
    cooked the chunks below it hundreds of times per variant.

    Inside the block a value change only marks what it affects dirty. When the block ends,
    each propagator that asked to solve is solved -- and the first solve already takes in
    every dirty node it is connected to, in dependency order, so the rest are clean by the
    time their turn comes and are skipped. Everything downstream cooks once, with all the
    new values in place.

    Solves asked for while the batch is being solved -- a param binding its storage
    attribute sets its value, which asks for a solve -- are queued behind it rather than
    run inside it. Run inside, each one nested a whole solve in the one before, and a batch
    of dozens of changes nested deep enough to exhaust the stack.

    Nested blocks join the outermost one.
    """
    global _batched, _batch_cooked, _solving_since, _batch_deferred
    if _batched is not None:
        yield
        return
    _batched = collections.deque()
    _batch_cooked = {}
    _batch_deferred = {}
    try:
        yield
        while _batched or _batch_deferred:
            while _batched:
                prop, queued_at = _batched.popleft()
                # Not re-dirtied: an earlier solve in this loop may have cleaned it along
                # with everything else it reached, and dirtying it again would cook that twice.
                if prop.get_dag_node().dirty:
                    _solving_since = queued_at
                    try:
                        prop._solve_downstream()
                    finally:
                        _solving_since = None
            # Deferred work runs once the solves have settled; anything it changes queues
            # another round rather than being lost.
            pending = list(_batch_deferred.values())
            _batch_deferred.clear()
            for fn in pending:
                fn()
    finally:
        _batched = None
        _batch_cooked = None
        _batch_deferred = None


def is_cook_stale(chunk) -> bool:
    """Whether a chunk's layer may not hold the result of its current inputs.

    A chunk goes stale when a change reaches it and its module does not run -- it was
    blocked outside the active view, its layer was muted, or it had no layer to author into
    yet -- and when its layer is created, which is empty. A cook that runs clears it.
    chunk_stage.recook_for_view reads this to re-run only the chunks that need it: a chunk
    that is not stale still has its last result in its layer, muted or not.

    A chunk that has never cooked is stale.

    Args:
        chunk: The chunk.

    Returns:
        True if the chunk needs to cook.
    """
    return getattr(chunk, "_cook_stale", True)


def mark_cook_stale(chunk) -> None:
    """Record that a chunk's layer no longer holds the result of its inputs. See is_cook_stale."""
    chunk._cook_stale = True


@contextmanager
def executing(prop):
    """The execution scope around one chunk's module.execute(): what a cook sets while it runs.

    Marks prop as executing (its own guard against being cooked re-entrantly), raises
    _usd_write_active so the stage listeners skip the writes the module makes, and names the
    chunk in _executing_chunk so an Edit Target resolves from its place in the graph.
    ChunkBase.cook is the one caller; the state lives here because every listener reads it
    from this module.

    Args:
        prop: The chunk about to run its module.
    """
    global _usd_write_active, _executing_chunk
    prev_executing_chunk = _executing_chunk
    prev_usd_write_active = _usd_write_active
    prop._executing = True
    _usd_write_active = True
    _executing_chunk = getattr(prop, "get_chunk", lambda: None)()
    try:
        yield
    finally:
        # Restored, not cleared: a cook can be reached from inside a guarded
        # write (a rename triggers one), and clearing outright unsuppressed
        # that outer write for whatever it still had left to do.
        _usd_write_active = prev_usd_write_active
        _executing_chunk = prev_executing_chunk
        prop._executing = False


_T = TypeVar("_T")
Predicate: TypeAlias = Callable[[Any], TypeGuard[_T]]

if TYPE_CHECKING:
    from usd_chunks import chunk_core, chunk_registry, chunk_widget_api


class ChunkNoticeAlias(IntEnum):
    """The name of each supported Tf.Notice class, used as the client-facing way to pick a notice.

    Any supported notice has an alias in this enum.

    Attributes:
        NONE: A default, empty alias
        PARAM_CHANGED: Sent when a param changes
        GROUP_CHANGED: Sent when a group changes. This includes nested groups and param changes
        CHUNK_CHANGED: Similar to GROUP_CHANGED but only for chunks
        PARAM_ADDED: Sent when a param was added as a child to a group or chunk
        PARAM_REMOVED: Sent when a param was removed as a child from a group or chunk. Not
            necessarily deleted
        CHUNK_DELETED: A chunk object was deleted. Use instead of PARAM_REMOVED for most
            applications: PARAM_REMOVED is only sent for a child of a NameSyncedContainer
            (group / chunk / vector), while CHUNK_DELETED is sent whenever ANY chunk object is
            deleted
        ATTRIBUTE_CHANGED: Default notice for attributes that do not set an alias explicitly
        FLAGS_CHANGED: Sent when the flags attribute changes
        STATE_CHANGED: Sent when the state attribute changes
        MENU_ACTION_NOTICE: Sent when a menu action should be executed
        PRIM_SELECTION_CHANGED: Sent whenever the host's prim selection changes
        PROP_SELECTION_CHANGED: Sent whenever the host's property selection changes
        OBJECT_CHANGED: Sent whenever anything was changed on the stage
        STAGE_CHANGED: Sent when the stage is changed
        STAGE_CONTENTS_CHANGED: Sent when stage contents changed
        STAGE_EDIT_TARGET_CHANGED: Sent when the edit target on the stage changed
        CHUNK_LOADED: Sent when a new chunk has been added to the registry
        CHUNK_UNLOADED: Sent when a chunk was unloaded
        CUSTOM: Custom notices should be registered with aliases > CUSTOM
    """

    # Chunk parameters and value notices
    NONE = 0

    INPUT_CHANGED = auto()
    # Default notice for attributes that does not set an alias explicitly
    ATTRIBUTE_CHANGED = auto()
    # Specific ATTRIBUTE_CHANGED notice for when a 'value' attribute is changed
    VALUE_CHANGED = auto()
    # Sent when the flags attribute changes
    FLAGS_CHANGED = auto()
    # Sent when the state attribute changes
    STATE_CHANGED = auto()

    PARAM_CHANGED = auto()  # Sent when a param changes
    PARAM_ADDED = auto()  # Sent when a param was added as a child to a group or chunk
    PARAM_REMOVED = auto()  # Sent when a param was removed as a child from a group or chunk. Not necessarily deleted

    GROUP_CHANGED = auto()  # Sent when a group changes. This includes nested groups and param changes

    CHUNK_CHANGED = auto()  # Similar to GROUP_CHANGED but only for chunks

    # A chunk object was deleted. Use instead of PARAM_REMOVED for most applications, as PARAM_REMOVED only sends when
    # a param was added as a child to a NamedSyncedContainer (group / chunk / vector). CHUNK_DELETED on the other hand, sends whenever
    # ANY chunk object was deleted
    CHUNK_DELETED = auto()

    # UI notices
    MENU_ACTION_NOTICE = auto()
    WIDGET_CHANGED = auto()

    # Gizmo notices. START and STOP bracket a stream of CHANGED, one per drag tick, so a
    # native host with its own undo stack can open a block on START and close it on STOP.
    # usdview and Kraken need none of this -- usd-undo intercepts at Sdf level and coalesces
    # a drag on its own, without caring whether the write came from a gizmo or a slider.
    GIZMO_START = auto()
    GIZMO_STOP = auto()
    GIZMO_CHANGED = auto()

    # Stage notices
    PRIM_SELECTION_CHANGED = auto()
    PROP_SELECTION_CHANGED = auto()
    CAMERA_CHANGED = auto()
    OBJECT_CHANGED = auto()  # Something was changed on the stage
    STAGE_CHANGED = auto()
    STAGE_CONTENTS_CHANGED = auto()
    STAGE_EDIT_TARGET_CHANGED = auto()

    # Registry notices
    CHUNK_LOADED = auto()  # Sent when a new chunk has been added to the registry
    CHUNK_UNLOADED = auto()  # Sent when a chunk was unloaded

    CHUNK_CONFIG_CHANGED = auto()  # Sent when a chunk config was changed

    DEPENDENCY_ADDED = auto()  # Sent when a DAG dependency is added between two propagators

    CUSTOM = auto()  # Custom notices should be registered with aliases > CUSTOM


# Notices that must be delivered even from a blocked chunk. Blocking (view-driven: the chunk is
# dormant/hidden) suppresses data/execution notices, but STRUCTURAL edits -- adding/removing
# child chunks, load/delete -- must still work so you can edit inside a group that isn't wired to
# an active view (a blocked group otherwise swallows its children's PARAM_ADDED, so their prims
# never get created). Execution is still gated separately (execute()/callback muted-layer guards).
_STRUCTURAL_ALIASES = frozenset({
    ChunkNoticeAlias.STATE_CHANGED,
    ChunkNoticeAlias.PARAM_ADDED,
    ChunkNoticeAlias.PARAM_REMOVED,
    ChunkNoticeAlias.CHUNK_LOADED,
    ChunkNoticeAlias.CHUNK_DELETED,
})


#: Relayed through a blocked propagator as well. Structural changes, and ATTRIBUTE_CHANGED:
#: an attribute's change is the record that something was renamed or set -- the widget
#: showing it and the USD attribute storing it both follow it -- not a reaction to it.
#: A blocked chunk still sends none of its own notices (send_self), so nothing that reacts
#: to a param or chunk changing runs; see AttributeSender.is_blocked.
_RECORD_ALIASES = _STRUCTURAL_ALIASES | {ChunkNoticeAlias.ATTRIBUTE_CHANGED}


class RegistrySingleton(type):
    """Metaclass that makes a class a process-wide singleton: every call returns the one instance."""
    _instances = {}

    @override
    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]


class ChunkNotice(Tf.Notice):
    """A Tf.Notice that carries data about what changed.

    Receivers use the data to find what has been changed, including globally registered
    listeners that did not subscribe to a particular sender.

    Attributes:
        data: The data sent with the notice. Its type varies between notices.
        alias (ChunkNoticeAlias): The alias associated with this notice.
    """

    data: Any = None
    alias: ChunkNoticeAlias = ChunkNoticeAlias.NONE

    def __init__(self, data: Any = None):
        super().__init__()
        self.data = data


NoticeT = TypeVar("NoticeT", bound=ChunkNotice)


class ChunkNoticeRegistry(metaclass=RegistrySingleton):
    """The registry mapping each ChunkNoticeAlias to its Tf.Notice class.

    The easiest way to register a new notice is to decorate its class with @chunk_notice::

        @chunk_notice(ChunkNoticeAlias.PARAM_CHANGED)
        class ParamChangeNotice(ChunkNotice):
            pass

    Attributes:
        registered (dict[int, type[Tf.Notice]]): Maps a ChunkNoticeAlias to a registered notice class
    """

    registered: dict[ChunkNoticeAlias, type[ChunkNotice]] = {}

    @classmethod
    def get_from_alias(cls, alias: ChunkNoticeAlias) -> type[ChunkNotice]:
        """Get a registered notice from a ChunkNoticeAlias.

        Args:
            alias (ChunkNoticeAlias): The alias to look up

        Raises:
            Tf.ErrorException: If the alias has not been registered

        Returns:
            type[Tf.Notice]: The class of the registered notice
        """
        notice = cls.registered.get(alias)
        if notice is None:
            raise Tf.ErrorException(f"No notice found for alias {alias}")
        return notice

    @classmethod
    def register(cls, alias: ChunkNoticeAlias, notice_class: type[Any]):
        """Register a new notice and associate it with an alias.

        Args:
            alias (ChunkNoticeAlias): The alias to associate with the notice
            notice_class (type[Any]): The class that should be registered
        """
        if alias not in cls.registered:
            notice_class.alias = alias
            if not hasattr(notice_class, "data"):
                notice_class.data = None
            cls.registered[alias] = notice_class


def chunk_notice(alias: ChunkNoticeAlias):
    """Decorator that registers a Tf.Notice class with the ChunkNoticeRegistry under an alias.

    Once registered, ``get_notice(alias).Send()`` sends it.

    Args:
        alias (ChunkNoticeAlias): The alias which maps to the new Tf.Notice class

    Returns:
        The class decorator.
    """

    def _wrapper(cls):
        ChunkNoticeRegistry().register(alias, cls)
        Tf.Type.Define(cls)
        return cls

    return _wrapper


### Chunk / Group / Param changed notices
@chunk_notice(ChunkNoticeAlias.PARAM_CHANGED)
class ParamChangeNotice(ChunkNotice):
    """Sent when a param changes; data is the param."""
    data: chunk_core.ChunkParamBase


@chunk_notice(ChunkNoticeAlias.INPUT_CHANGED)
class InputChangedNotice(ChunkNotice):
    """Sent when a dependency of a chunk object changes; data is the object."""
    data: chunk_core.ChunkCommon


@chunk_notice(ChunkNoticeAlias.GROUP_CHANGED)
class GroupChangeNotice(ChunkNotice):
    """Sent when a group or anything inside it changes; data is the group."""
    data: chunk_core.ChunkGroupBase


@chunk_notice(ChunkNoticeAlias.CHUNK_CHANGED)
class ChunkChangeNotice(ChunkNotice):
    """Sent when a chunk or anything inside it changes; data is the chunk."""
    data: chunk_core.ChunkBase


### Create / Delete notices
@chunk_notice(ChunkNoticeAlias.PARAM_ADDED)
class ParamAddedNotice(ChunkNotice):
    """Sent when a param is added to a group or chunk; data is the param."""
    data: chunk_core.ChunkCommon


@chunk_notice(ChunkNoticeAlias.PARAM_REMOVED)
class ParamRemovedNotice(ChunkNotice):
    """Sent when a param is removed from a group or chunk; data is the param."""
    data: chunk_core.ChunkCommon


@chunk_notice(ChunkNoticeAlias.CHUNK_DELETED)
class ChunkDeletedNotice(ChunkNotice):
    """Sent when any chunk object is deleted; data is the object."""
    data: chunk_core.ChunkCommon


### Attribute notices
@chunk_notice(ChunkNoticeAlias.ATTRIBUTE_CHANGED)
class AttributeChangeNotice(ChunkNotice):
    """Sent when an attribute changes; data is its AttributeSender."""
    data: chunk_core.AttributeSender


@chunk_notice(ChunkNoticeAlias.VALUE_CHANGED)
class ValueChangedNotice(AttributeChangeNotice):
    """Sent when a param's value changes."""
    pass


@chunk_notice(ChunkNoticeAlias.FLAGS_CHANGED)
class AttributeFlagsChangeNotice(AttributeChangeNotice):
    """Sent when an object's flags change."""
    pass


@chunk_notice(ChunkNoticeAlias.STATE_CHANGED)
class AttributeStateChangeNotice(AttributeChangeNotice):
    """Sent when an object's state bits change."""
    pass


### Gizmo notices
@chunk_notice(ChunkNoticeAlias.GIZMO_START)
class GizmoStartedNotice(ChunkNotice):
    """Sent when a host starts dragging a gizmo."""
    pass


@chunk_notice(ChunkNoticeAlias.GIZMO_STOP)
class GizmoStoppedNotice(ChunkNotice):
    """Sent when a host stops dragging a gizmo."""
    pass


@chunk_notice(ChunkNoticeAlias.GIZMO_CHANGED)
class GizmoChangedNotice(ChunkNotice):
    """Sent when a gizmo drag moves."""
    pass


### Chunk registry load / unload notices
@chunk_notice(ChunkNoticeAlias.CHUNK_LOADED)
class ChunkLoadedNotice(ChunkNotice):
    """Sent when a chunk is added to the scene; data is the chunk."""
    data: chunk_core.ChunkBase


@chunk_notice(ChunkNoticeAlias.CHUNK_UNLOADED)
class ChunkUnloadedNotice(ChunkNotice):
    """Sent when a chunk is unloaded; data is the chunk."""
    data: chunk_core.ChunkBase


@chunk_notice(ChunkNoticeAlias.CHUNK_CONFIG_CHANGED)
class ChunkConfigChanged(ChunkNotice):
    """Sent when a chunk's config changes; data is the config."""
    data: chunk_core.ChunkConfig


@chunk_notice(ChunkNoticeAlias.DEPENDENCY_ADDED)
class DependencyAddedNotice(ChunkNotice):
    """Sent when add_dependency() is called. sender=dependent, data=input propagator."""
    data: chunk_notice.ChunkNoticePropagator


### UI Notices
@chunk_notice(ChunkNoticeAlias.MENU_ACTION_NOTICE)
class MenuActionNotice(ChunkNotice):
    """Sent when a menu action should run."""
    pass


@chunk_notice(ChunkNoticeAlias.WIDGET_CHANGED)
class WidgetChangedNotice(ChunkNotice):
    """Sent when a param widget changes; data is the widget."""
    data: chunk_widget_api.ChunkParamWidgetBase


### UsdView selection notices
@chunk_notice(ChunkNoticeAlias.PRIM_SELECTION_CHANGED)
class PrimSelectionChangedNotice(ChunkNotice):
    """Sent when the host's prim selection changes; data is the selected prims."""
    data: list[Usd.Prim]


@chunk_notice(ChunkNoticeAlias.PROP_SELECTION_CHANGED)
class PropSelectionChangedNotice(ChunkNotice):
    """Sent when the host's property selection changes; data is the property."""
    data: Usd.Property


@chunk_notice(ChunkNoticeAlias.CAMERA_CHANGED)
class CameraChangedNotice(ChunkNotice):
    """Sent when the host's active camera changes; data is the camera."""
    data: Gf.Camera


# Register externally manages notice just to map to the custom enum
ChunkNoticeRegistry().register(ChunkNoticeAlias.OBJECT_CHANGED, Usd.Notice.ObjectsChanged)
ChunkNoticeRegistry().register(ChunkNoticeAlias.STAGE_CHANGED, Usd.Notice.StageNotice)
ChunkNoticeRegistry().register(ChunkNoticeAlias.STAGE_CONTENTS_CHANGED, Usd.Notice.StageContentsChanged)
ChunkNoticeRegistry().register(ChunkNoticeAlias.STAGE_EDIT_TARGET_CHANGED, Usd.Notice.StageEditTargetChanged)


def get_notice(alias: ChunkNoticeAlias) -> type[ChunkNotice]:
    """The Tf.Notice class registered for a ChunkNoticeAlias.

    Args:
        alias (ChunkNoticeAlias): The alias to look up

    Returns:
        type[ChunkNotice]: The notice class

    Raises:
        Tf.ErrorException: If the alias has not been registered
    """
    return ChunkNoticeRegistry().get_from_alias(alias)


def is_notice_type(notice: Tf.Notice, alias: ChunkNoticeAlias) -> TypeGuard[ChunkNotice]:
    """Check whether a notice is the class registered for an alias.

    Args:
        notice (Tf.Notice): The notice to check
        alias (ChunkNoticeAlias): The alias to compare against

    Returns:
        bool: True if get_notice(alias) is exactly the notice's type
    """
    return type(notice) is get_notice(alias)


def send_notice(alias: ChunkNoticeAlias, sender: Any = None, data: Any = None) -> ChunkNotice | None:
    """Send a notice of type alias, running the callbacks subscribed to it.

    Args:
        alias (ChunkNoticeAlias): The type of the notice to send
        sender (Any, optional): The object to pass as sender. If None, the notice is sent
            globally. Defaults to None.
        data (Any, optional): Any additional data to attach to the notice object. Defaults to None.

    Returns:
        ChunkNotice | None: The sent notice, or None if the alias is not a ChunkNotice.
    """
    # Exception is handled upstream
    notice_class = get_notice(alias)

    if issubclass(notice_class, ChunkNotice):
        notice = notice_class(data)
    else:
        # notices not derived from ChunkNotice should not be sent
        return None

    try:
        if sender is None:
            notice.SendGlobally()
        else:
            notice.Send(sender)
    except Tf.ErrorException:
        logger.exception("Could not send notice %s", notice)
        return

    return notice


ConnSetT: TypeAlias = weakref.WeakValueDictionary[int, "DagNode"]


@define(slots=True, hash=True)
class DagConnections:
    """The connections of one DagNode.

    Attributes:
        owner: The owning node. It is similar to an output, except that clients may use the
            owner like a parent of the node, so any change to a DagNode propagates to its
            owner. An ownership hierarchy may look like
            AttributeSender -> ChunkParamBase -> NameSyncedContainer -> Chunk, whereas
            'outputs' typically reference nodes outside of the hierarchy.
        inputs: A WeakValueDictionary of DagNodes. Every input is by definition a dependency
            of this node, and all inputs must be clean before this node can evaluate.
        outputs: A WeakValueDictionary of DagNodes, the outgoing connections from this node.
            When this node is dirtied, so are all its outputs.
    """

    owner: weakref.ReferenceType[ChunkNoticePropagator] | None = field(default=None)
    inputs: ConnSetT = field(factory=ConnSetT)
    outputs: ConnSetT = field(factory=ConnSetT)


_dag_graph_id_counter = itertools.count()


class DagGraphID:
    """Which connected graph a DagNode belongs to, merged union-find style as nodes connect."""
    def __init__(self):
        self._store: DagGraphID | None = None
        # A counter, NOT id(self): CPython only guarantees an object's id() is unique among
        # objects alive at the same time, and a scene reload creates and drops these fast
        # enough for a freed address to be reused. Two graphs would then share one
        # identifier, and with it one graph_layers key -- inserting the same layer into a
        # stage's subLayerPaths twice, which Sdf refuses. A counter is unique for the
        # process's lifetime regardless of when any instance is freed.
        self._uid = next(_dag_graph_id_counter)

    def root(self) -> DagGraphID:
        """The terminal DagGraphID of the _store chain, found iteratively.

        The path is compressed on the way, so every visited id points directly at the root
        afterwards.

        Returns:
            The root id.
        """
        node = self
        while node._store is not None:
            node = node._store

        # Path compression: repoint the whole chain at the root
        current = self
        while current is not node:
            nxt = current._store
            current._store = node
            current = nxt  # type: ignore[assignment]
        return node

    def get_identifier(self):
        """The graph's identifier: the root id's unique counter value."""
        return self.root()._uid


class DagNode:
    """A connectable node in the dependency graph between chunk objects.

    A DagNode handles the 'physical' connections between ChunkNoticePropagator objects, as
    opposed to NoticeConnection, which handles non-physical ones. It may have any number of
    inputs and outputs, and one owner. Dirtying a node dirties its outputs and owner,
    recursively. A client solves this node and everything connected downstream by calling
    solve_outputs().

    Attributes:
        dirty (bool): Whether this node needs solving
        propagator (ChunkNoticePropagator): The propagator this node represents
    """

    __slots__ = (
        "_index",
        "_graph_id",
        "_part_of_graph",
        "_changes",
        "_owned",
        "_dirty",
        "_connections",
        "_propagator",
        "__weakref__",
    )

    def __init__(self, propagator: ChunkNoticePropagator):
        # Insertion-ordered map of the change reasons that dirtied this node, maintained
        # by chunk_scheduler.propagate_dirty and replayed by iter_changes(). The value is
        # the presentation-vs-data verdict for that reason, decided once at origin by
        # AttributeSender.is_presentation_change and carried unchanged from there --
        # see only_presentation_changed().
        self._changes: dict[tuple[ChunkNoticeAlias, DagNode], bool] = {}
        self._dirty = True
        self._connections: DagConnections = DagConnections()
        self._propagator = weakref.ref(propagator)
        self._index: int = 0

        # Reverse index of the owner link, so the scheduler can walk ownership in both
        # directions without a global node registry. Created on demand: most nodes are
        # owned by something but own nothing themselves, and a WeakSet per node is real
        # allocation and GC pressure at the scale this graph reaches.
        self._owned: weakref.WeakSet[DagNode] | None = None
        self._graph_id: DagGraphID = DagGraphID()
        self._part_of_graph = True

    @property
    def propagator(self):
        """The propagator this node represents, or None once it is gone."""
        if prop := self._propagator():
            return prop
        return None

    @property
    def dirty(self):
        """Whether this node needs solving; setting it True dirties everything downstream."""
        return self._dirty

    @property
    def name(self):
        # Useful for debugging the solve
        """The propagator's name, or its class name; used when debugging a solve."""
        return self.propagator.name if hasattr(self.propagator, "name") else self.propagator.__class__.__name__  # type: ignore

    @dirty.setter
    def dirty(self, val: bool):
        """Mark the node dirty or clean; dirty propagates to its dependants."""
        self._dirty = val
        if val:
            self.dirty_dependants()

    def disconnect(self):
        """Drop every edge into and out of this node, on both sides.

        Clearing only this node's own dicts leaves each peer still pointing back here, so a
        disconnected node stays reachable as somebody else's input or output. That was
        invisible while chunk-level edges were rarely populated, but request_delete calls
        this on any node with outputs -- a stale reverse link there resurrects the deleted
        chunk as a live dependency of its former successor.
        """
        # Snapshot before mutating: the peer loops write to dicts these views come from, and
        # _connections.inputs/outputs are WeakValueDictionaries, which raise if they change
        # size mid-iteration.
        inputs = list(self._connections.inputs.values())
        outputs = list(self._connections.outputs.values())

        self._connections.inputs.clear()
        self._connections.outputs.clear()

        for node in inputs:
            node.remove_dependant(self)
        for node in outputs:
            node.remove_dependency(self, disconnect_reverse=False)

        chunk_scheduler.topology_changed()

    def next_key(self, items: ConnSetT) -> int:
        """Get the next key for either inputs or outputs dictionaries. The result is safe to use as key for the dict.

        Args:
            items (ConnSetT): A dictionairy t find the next best key for

        Raises:
            KeyError: No valid key was found

        Returns:
            int: A new valid key for the dict
        """
        for i in range(len(items) + 1):
            if i not in items:
                return i
        raise KeyError("Somewthign went wrong")

    def iter_changes(
        self,
    ) -> Generator[tuple[ChunkNoticeAlias, ChunkNoticePropagator | None], None, StopIteration]:
        # Sorted by how often the source node was itself dirtied, so the shallowest
        # change is reported first; _changes is insertion-ordered, which makes the
        # tie-break between equally-dirtied sources the order they arrived in.
        """The changes that dirtied this node, shallowest first.

        Sorted by how often the source node was itself dirtied; ties keep the order the changes
        arrived in.

        Yields:
            (alias, propagator) for each change.
        """
        for alias, node in sorted(self._changes, key=lambda x: x[1]._index):
            yield alias, node.propagator
        return StopIteration()

    def only_presentation_changed(self) -> bool:
        """Whether every change that dirtied this node was presentation only.

        That is, something nothing computes from. A chunk whose changes are all presentation
        must not re-cook.

        The verdict is read back from what chunk_scheduler recorded when each change was
        propagated, not re-derived here. Both other ways of asking are lossy:

        * The aliases in _changes are only a proxy. A `presentation_only` param raises
          PARAM_CHANGED rather than STATE_CHANGED, so an alias test disagrees with
          AttributeSender.is_presentation_change in exactly that case.
        * Asking the sender again reads an old_value/value pair that has moved on since,
          and is only meaningful while the solve that recorded it is still running.

        False when nothing was recorded, so a direct dirty solve -- execute-on-creation,
        which carries no propagated change -- still counts as real work and cooks.

        Returns:
            bool: True if there is at least one recorded change and all of them are
            presentation only
        """
        seen = False
        for (_alias, node), presentation in self._changes.items():
            # A dead propagator's reason is dropped, matching what iter_changes()'s
            # consumers already do: it can no longer describe anything.
            if node.propagator is None:
                continue
            if not presentation:
                return False
            seen = True
        return seen

    def set_graph_id(self, node: DagNode):
        """Merge this node's graph with `node`'s, if both take part in a graph.

        Args:
            node: The node being connected to.
        """
        if not (node._part_of_graph and self._part_of_graph):
            return

        my_root = self._graph_id.root()
        other_root = node._graph_id.root()
        if my_root is other_root:
            # Already the same graph; linking again would create a cycle
            return
        other_root._store = my_root

    def add_dependency(self, node: DagNode, connect_reverse=True) -> int:
        """Add a dependency (an input) to this node, which dirties it.

        Args:
            node (DagNode): The new dependency to add
            connect_reverse (bool, optional): Also add this node to the input's outputs.
                Defaults to True.

        Returns:
            int: The key the dependency was stored under
        """
        key = self.next_key(self._connections.inputs)
        self._connections.inputs[key] = node
        self.set_graph_id(node)
        # Only self's depth (and its dependants'/owner's) can have moved -- node keeps
        # whatever depth it had, since gaining a dependant says nothing about its own
        # distance from the roots.
        chunk_scheduler.topology_changed(self)

        if connect_reverse:
            node.add_dependant(self)
        else:
            self.dirty = True

        return key

    def add_dependant(self, node: DagNode):
        """Add a node that depends on this one (an output).

        Args:
            node (DagNode): The dependant node
        """
        self._connections.outputs[self.next_key(self._connections.outputs)] = node
        chunk_scheduler.topology_changed(self, node)
        self.dirty = True

    def remove_dependency(self, node: DagNode, disconnect_reverse=True) -> bool:
        """Remove a dependency (an input) from this node, which dirties it.

        Losing an input changes what this node evaluates to. The counterpart to add_dependency;
        disconnect() drops every edge at once and is not a substitute when a single wire is
        detached.

        Args:
            node (DagNode): The dependency to remove
            disconnect_reverse (bool, optional): If the input node should drop this node from
                its outputs. Defaults to True.

        Returns:
            bool: True if node was an input and got removed
        """
        # Keys are collected before deleting: _connections.inputs is a WeakValueDictionary,
        # so mutating it while iterating raises.
        keys = [key for key, value in self._connections.inputs.items() if value is node]
        if not keys:
            return False

        for key in keys:
            del self._connections.inputs[key]
        chunk_scheduler.topology_changed(self, node)

        if disconnect_reverse:
            node.remove_dependant(self)

        self.dirty = True
        return True

    def remove_dependant(self, node: DagNode) -> bool:
        """Remove a node that depends on this one (an output).

        Args:
            node (DagNode): The dependant node to remove

        Returns:
            bool: True if node was an output and got removed
        """
        keys = [key for key, value in self._connections.outputs.items() if value is node]
        if not keys:
            return False

        for key in keys:
            del self._connections.outputs[key]
        # node is passed as well as self: the edge is being torn down from this side, so
        # node is already out of self's downstream closure by the time this runs.
        chunk_scheduler.topology_changed(self, node)
        return True

    def set_owner(self, owner: ChunkNoticePropagator | None, *, dirty: bool = True):
        """Set the owner of this node.

        The owner is treated like an output, with some semantic differences; see DagConnections.

        Args:
            owner (ChunkNoticePropagator | None): The owner propagator
            dirty (bool): Whether joining marks the owner dirty. A new child is new data for
                its owner; an attribute's own sender is not -- see AttributeSender.
        """
        if owner == self.get_owner():
            return

        assert isinstance(owner, ChunkNoticePropagator) or owner is None

        if (previous := self.get_owner_node()) and previous._owned is not None:
            previous._owned.discard(self)

        if owner is None:
            self._connections.owner = None
        else:
            self._connections.owner = weakref.ref(owner)

        # An owner takes the depth of the deepest thing it owns, so both the owner being
        # left and the one being joined have to be re-measured; self reaches the new one
        # through its own downstream closure.
        if previous is not None:
            chunk_scheduler.topology_changed(self, previous)
        else:
            chunk_scheduler.topology_changed(self)

        if node := self.get_owner_node():
            if node._owned is None:
                node._owned = weakref.WeakSet()
            node._owned.add(self)
            if dirty:
                node.dirty = True
            node.set_graph_id(self)

    def get_inputs(self) -> Iterator[tuple[int, DagNode]]:
        """Get the input (dependency) nodes of this node.

        Returns:
            Iterator[tuple[int, DagNode]]: (key, node) for each input
        """
        return self._connections.inputs.items()

    def get_input(self, index: int) -> DagNode:
        """Get the input stored under a key.

        Args:
            index (int): The key of the input to find

        Raises:
            KeyError: If the key does not exist

        Returns:
            DagNode: The found dag node
        """
        try:
            return self._connections.inputs[index]
        except KeyError:
            raise

    def get_outputs(self) -> Iterator[tuple[int, DagNode]]:
        """Get the output (dependant) nodes of this node.

        Returns:
            Iterator[tuple[int, DagNode]]: (key, node) for each output
        """
        return self._connections.outputs.items()

    def get_output(self, index: int) -> DagNode:
        """Get the output stored under a key.

        Args:
            index (int): The key of the output to find

        Raises:
            KeyError: If the key does not exist

        Returns:
            DagNode: The found dag node
        """
        return self._connections.outputs[index]

    def get_owner(self) -> ChunkNoticePropagator | None:
        """Get the owner of this node: its 'parent', treated like an output.

        The propagator, by contrast, is the ChunkNoticePropagator this node represents in the
        graph.

        Returns:
            ChunkNoticePropagator | None: The owner if it exists, else None
        """
        if owner := self._connections.owner:
            return owner()
        return None

    def get_owner_node(self) -> DagNode | None:
        """Get the owner's dag node: owner.get_dag_node().

        Returns:
            DagNode | None: The dag node of the owner, or None if there is no owner
        """
        if owner := self.get_owner():
            return owner.get_dag_node()
        return None

    def should_solve(self) -> bool:
        """Whether this dirty node should solve.

        A clean node never does. For a dirty one the check is whether it has outputs or an
        owner to propagate to -- but get_outputs() returns a WeakValueDictionary's items(), a
        generator, which is always truthy, so in practice every dirty node solves. Callers
        rely on that; it is not to be "fixed" in isolation.

        Returns:
            bool: Whether the node should solve
        """
        if not self.dirty:
            return False

        if self.get_outputs() or self.get_owner_node():
            return True
        return False

    def get_owned(self) -> Iterator[DagNode]:
        """The nodes that have this node as their owner.

        The reverse of get_owner_node(), maintained by set_owner() so the scheduler can walk
        ownership downward without a global registry of nodes.

        Returns:
            Iterator[DagNode]: An iterator over the owned nodes
        """
        return iter(() if self._owned is None else list(self._owned))

    def get_sort_key(self) -> int:
        """This node's position in the dependency order: the longest chain of dependencies ending here.

        Derived from the graph on demand by chunk_scheduler rather than maintained on every
        connect.

        Returns:
            int: The sort key, 0 for a node with no dependencies
        """
        return chunk_scheduler.get_depth(self)

    def get_graph_id(self):
        """The identifier of the connected graph this node belongs to."""
        return self._graph_id.get_identifier()

    def dirty_dependants(self):
        """Mark every output and the owner dirty, transitively.

        Each one records which change caused it. See chunk_scheduler.propagate_dirty.
        """
        chunk_scheduler.propagate_dirty(self)

    def is_inputs_dirty(self) -> bool:
        """Check if any dependency (input) is dirty.

        Returns:
            bool: True if any dependency is dirty
        """
        return any([x.dirty for _, x in self.get_inputs()])

    def solve_outputs(self) -> Generator[DagNode, None, None]:
        """Solve this node, then everything downstream of it and its owner.

        This is the solve method clients should use, as it makes every dependent node solve once
        this one has. The solve starts at the dirty frontier upstream of this node, so a node
        whose own dependencies have not run yet still solves in the right order. See
        chunk_scheduler.solve.

        Yields:
            DagNode: Every node solved, this one included
        """
        yield from chunk_scheduler.solve(self)


class CallbackContainer:
    """The callbacks on one ChunkNoticePropagator, with their private and on-create tags.

    Kept here rather than on the NoticeConnection objects so as not to pollute that class.
    Private callbacks are neither serialized nor copied between propagators. Callbacks
    created in __init__ should be private: they are recreated when deserializing, so
    serializing them too would duplicate them.

    Callbacks are stored in self.callbacks as a list of dicts with the keys 'conn' (the
    NoticeConnection), 'on_create' and 'private'.
    """

    def __init__(self):
        super().__init__()
        self.callbacks: list[dict[Literal["conn", "on_create", "private"], Any]] = []



    def add(self, conn: NoticeConnection, trigger_on_create=True, private=False):
        """Store a callback.

        Args:
            conn: The connection.
            trigger_on_create: Run it when the owning object is added to the scene.
            private: Keep it out of copies and serialization.
        """
        self.callbacks.append({"conn": conn, "on_create": trigger_on_create, "private": private})

    def get(self, include_private=False):
        """The stored connections.

        Args:
            include_private: Include private callbacks.

        Returns:
            The connections.
        """
        return [x["conn"] for x in self.callbacks if include_private or (not x.get("private"))]

    def get_on_create(self, include_private=False):
        """The connections to run when the owning object is added to the scene.

        Args:
            include_private: Include private callbacks.

        Returns:
            The connections.
        """
        return [x["conn"] for x in self.callbacks if x.get("on_create") and (include_private or (not x.get("private")))]

    def extend(self, other: CallbackContainer):
        """Add another container's non-private callbacks.

        Args:
            other: The container to copy from.
        """
        for data in other.callbacks:
            if data.get("private"):
                continue

            self.add(data["conn"], data.get("on_create", True))

    def clear(self):
        """Drop every stored callback."""
        self.callbacks.clear()


class ChunkNoticePropagator:
    """Base class that makes chunk objects connectable, to each other and to callbacks.

    Chunk objects connect to each other through 'physical' connections and to callbacks
    through 'non-physical' ones. Physical connections rely on dependencies (inputs) and
    dependants (outputs and owners); non-physical connections rely on the Tf.Notice system
    and have no dependencies. Callbacks are non-physical; inputs, outputs and the owner are
    physical.

    **Physical:**

    'Children' reference their owning object (parent), and call data_changed() on the owner
    when the child has been modified. The DagNode manages these connections: it holds
    references to inputs, outputs and an owner, and solves its outputs recursively while
    respecting dependencies, so a node does not solve until all its inputs are solved. Any
    number of dependencies (inputs) can be added to an instance of this class; once all of
    them are solved, the instance's DagNode solves too, calling data_changed(). Combined with
    callbacks, this lets a client build dependency graphs of chunk classes and have the
    system trigger callbacks when appropriate.

    **Non-physical:**

    Connections added with add_callback are not managed by the DAG. The Tf.Notice system
    manages them, without any dependency information, so a callback never triggers another
    callback implicitly.

    By default send_self() is called whenever an attribute or child attribute has been
    modified. The notice sent is decided by get_notice_alias(), and the DagNode also
    propagates it up the owner hierarchy.

    get_edit_context(), get_layer_identifier() and get_stage() can be implemented to
    support layer contexts when modifying the stage. A NoticeConnection sets the edit
    context to the layer returned by get_edit_context() before running its callback, which
    keeps callbacks contained to the layers managed by ChunkLayerManager. See
    NoticeConnection.handle_callback.
    """

    _callbacks: CallbackContainer
    _dag_node: DagNode | None
    _blocked: bool

    def __init__(self, *args, **kwargs) -> None:
        self._callbacks = CallbackContainer()
        self._dag_node = None
        self._blocked = False
        super().__init__(*args, **kwargs)




    @abstractmethod
    def request_delete(self):
        """Delete this object. Implemented by a subclass.

        Raises:
            NotImplementedError: A subclass does not implement it.
        """
        raise NotImplementedError

    def get_notice_data(self) -> Self:
        """The data attached to the notice send_self() sends.

        Returns:
            Self: This object, by default
        """
        return self

    @abstractmethod
    def get_notice_alias(self) -> ChunkNoticeAlias:
        """The notice this object sends from send_self(), and listens for by default in add_callback().

        Subclasses override this to send the right notice when something on the chunk object
        changes.

        Returns:
            ChunkNoticeAlias: The default notice alias

        Raises:
            NotImplementedError: A subclass does not implement it.
        """
        raise NotImplementedError

    def get_chunk_module(self) -> chunk_registry.ChunkModule | None:
        """The registered chunk module this object's behaviour comes from, or None.

        Only a chunk has one -- ChunkBase overrides this to look its module up in the
        registry. Declared here so the solve can ask any propagator whether it has
        behaviour to run without knowing the chunk classes, which this module cannot import.

        Returns:
            Always None here.
        """
        return None

    @abstractmethod
    def is_blocked(self) -> bool:
        """Check whether this instance should be blocked from sending notices.

        Returns:
            bool: True if blocked or the owner is blocked, False otherwise
        """
        if self._blocked:
            return True

        if self.get_notice_alias() == ChunkNoticeAlias.STATE_CHANGED:
            return False

        if owner := self.get_owner():
            return owner.is_blocked()
        return False

    def set_blocked(self, block: bool):
        """Block or unblock this object's notices."""
        self._blocked = block

    def get_owner(self) -> ChunkNoticePropagator | None:
        """Get the owning ChunkNoticePropagator of this object.

        Returns:
            ChunkNoticePropagator | None: The owning object, or None
        """
        return self.get_dag_node().get_owner()

    def set_owner(self, owner: ChunkNoticePropagator | None, *, dirty: bool = True):
        """Set the owner of this object: its parent in a parent-child relationship.

        Call this once the parent has actually taken ownership of this object, to update the
        child when the relationship has changed.

        Args:
            owner (ChunkNoticePropagator | None): The owning (parent) object that should receive notices when this instance is modified
            dirty (bool): Whether joining marks the owner dirty; see DagNode.set_owner.
        """
        self.get_dag_node().set_owner(owner, dirty=dirty)

    def is_presentation_change(self) -> bool:
        """Whether the change this propagator is being dirtied for is presentation only.

        A presentation change is one nothing downstream computes from -- hiding a param,
        folding a group open, selecting a node. Such a change still has to reach the UI, so
        it solves and sends its notice as usual, but it must not dirty anything that reads
        this propagator's data: see chunk_scheduler.propagate_dirty.

        Asked at dirty time, so an implementation describes the change that just happened
        rather than the object in general. Only AttributeSender answers True (for the state
        attribute, and only while DISABLED is untouched) -- everything else is a data change.

        Returns:
            bool: True if this change must not dirty dependants
        """
        return False

    def data_changed(self, notice: ChunkNotice | None = None) -> ChunkNotice | None:
        """Called by the DagNode when it solves, once all inputs (dependencies) are solved.

        The returned notice is propagated by the DagNode.

        Args:
            notice (ChunkNotice | None): The notice that was sent

        Returns:
            ChunkNotice | None: The notice to propagate, or None when blocked
        """
        if not notice or (notice and notice.alias not in _RECORD_ALIASES):
            if self.is_blocked():
                return None
        if notice:
            notice.Send(self)
        return notice

    def send_self(self, alias: ChunkNoticeAlias | None = None, data: Any = None) -> ChunkNotice | None:
        """Sends a Tf.Notice, with self as sender. The type of notice is determined by get_notice_alias(), or optionally the alias argument.

        Args:
            alias (Optional[ChunkNoticeAlias]): An optional alias to send. If alias is not None, it will be used instead of the default from get_notice_alias()
            data (Any): The data to attach to the notice. If None, will use self.get_notice_data(). Defaults to None

        Returns:
            Optional[ChunkNotice]: The notice that was sent, or None if no notice was sent
        """
        if alias is None:
            alias = self.get_notice_alias()

        if not alias:
            return None

        if alias not in _STRUCTURAL_ALIASES and self.is_blocked():
            return None

        notice_data = data if data is not None else self.get_notice_data()
        notice = send_notice(alias, self, notice_data)

        if not notice:
            return None

        return notice

    def _copy_callbacks(self, src: ChunkNoticePropagator):
        """Copy connections to this object from src.

        Args:
            src (ChunkNoticePropagator): The source object to copy connections from
        """
        for data in src._callbacks.callbacks:
            conn = data["conn"]
            if meth := conn.get_method():
                sender = self if conn.sender == src else conn.sender
                owner = self if conn.owner is not None else None
                new_conn = NoticeConnection(meth, conn.alias, sender, conn.globally, owner)
                self._callbacks.add(new_conn)

    def get_dag_node(self) -> DagNode:
        """Get this object's DagNode, creating it on first use.

        Returns:
            DagNode: The DagNode held by this ChunkNoticePropagator instance
        """
        if not self._dag_node:
            self._dag_node = DagNode(self)
        return self._dag_node

    def execute(self):
        """Dirty this object and solve everything downstream of it.

        Each solved object's data_changed() runs, and with it send_self(), which triggers any
        connected callbacks. Inside batched_solve() the solve waits for the block to end.
        """
        node = self.get_dag_node()
        node.dirty = True
        if _batched is not None:
            _batched.append((self, _tick()))
            return
        self._solve_downstream()

    def _solve_downstream(self):
        """Solve this object's already-dirty node and everything downstream of it."""
        node = self.get_dag_node()
        msg = "\n====== SOLVE ======\n"
        for n in node.solve_outputs():
            if not (prop := n.propagator):
                continue

            msg += f"solved: {n.name}\n"

            changes = [(alias, iprop) for alias, iprop in n.iter_changes() if iprop]
            for alias, iprop in changes:
                if alias == prop.get_notice_alias():
                    continue

                msg += f"   {alias.name}\n"
                prop.data_changed(get_notice(alias)(iprop.get_notice_data()))

            if notice := prop.send_self():
                msg += f"   {notice.alias.name}\n"

            # A pure state change (selection, collapse, disable) must not re-run the module.
            # Data changes (params/inputs) and a direct dirty solve (execute-on-creation,
            # which carries no propagated change) still execute. Selection/disable side
            # effects run above via data_changed / send_self.
            #
            # Read back from the verdict chunk_scheduler recorded rather than re-derived from
            # the aliases: a `presentation_only` param raises PARAM_CHANGED, not
            # STATE_CHANGED, so testing the alias would call it a data change and re-cook.
            state_only = n.only_presentation_changed()
            # Only a chunk made from a module has a module: ChunkModule.create names it in
            # the config of each chunk it creates, and those are ChunkBase. A module without
            # execute() (a chunkgroup) still counts here -- it goes stale below.
            # The module is not attached to the chunk: ChunkBase.get_chunk_module looks it
            # up in the registry by that name, and only a ChunkBase answers anything but
            # None -- which is what makes the cast below sound.
            if not state_only and prop.get_chunk_module() is not None:
                chunk = cast("chunk_core.ChunkBase", prop)
                # Stale until the module actually runs below. Every reason this dispatch has
                # for not running it -- blocked, muted, no layer yet, no stage -- leaves the
                # layer holding a result the change never reached, and recook_for_view cooks
                # a chunk again on exactly that. A chunk with no execute() (a chunkgroup)
                # stays stale, which tells recook_for_view its inside needs cooking.
                mark_cook_stale(chunk)
                if _cooked_since(chunk):
                    chunk._cook_stale = False
                # The cook itself -- and every guard on whether it may run now -- is
                # ChunkBase.cook. A chunk whose module has no execute() has nothing to run
                # and stays stale.
                elif chunk.cook():
                    # True once cook() got past its guards, including when the layer left
                    # the stack mid-cook. A module that raises ends the whole batch, and
                    # this record with it, so there is nothing to note on that path.
                    if _batch_cooked is not None:
                        _batch_cooked[id(prop)] = _tick()

        msg += "====== FINISHED ======\n"
        logger.debug(msg)

    def add_dependency(self, input: ChunkNoticePropagator) -> int:
        """Add an input. This propagator does not evaluate until all inputs are solved.

        Args:
            input (ChunkNoticePropagator): The node to add as input

        Returns:
            int: The dependency index
        """
        idx = self.get_dag_node().add_dependency(input.get_dag_node())
        send_notice(ChunkNoticeAlias.DEPENDENCY_ADDED, self, data=input)
        return idx

    def remove_dependency(self, input: ChunkNoticePropagator) -> bool:
        """Remove an input previously added with add_dependency.

        Dirties this propagator so the loss of the input propagates downstream like any other
        change. No DEPENDENCY_ADDED counterpart notice is sent: the only listener
        (ChunkStageSync._on_dependency_added) exists to author the USD connection, and every
        caller of this method is reacting to that connection already being gone.

        Args:
            input (ChunkNoticePropagator): The node to drop as input

        Returns:
            bool: True if input was a dependency and got removed
        """
        return self.get_dag_node().remove_dependency(input.get_dag_node())

    @overload
    def get_dependency(self, index: int = 0) -> ChunkNoticePropagator | None: ...
    @overload
    def get_dependency(self, index: int = 0, predicate: Predicate[_T] | None = None) -> _T: ...

    def get_dependency(
        self, index: int = 0, predicate: Predicate[_T] | None = None
    ) -> ChunkNoticePropagator | None | _T:
        """Get one input of this propagator.

        Args:
            index: The key of the input.
            predicate: Only return the input if it matches this.

        Returns:
            ChunkNoticePropagator | None: The input at index, or None if there is none

        Raises:
            ValueError: The input exists but does not match the predicate.
        """
        try:
            res = self.get_dag_node().get_input(index).propagator
        except (KeyError, IndexError, AttributeError):
            # A missing connection is "not found" -> None regardless of a type predicate.
            # The predicate only filters a connection that exists (see ValueError below).
            return None
        if not predicate or predicate(res):
            return res
        raise ValueError(f"Predicate {predicate} failed for get_dependency({index})")

    @overload
    def get_dependencies(self) -> list[tuple[int, ChunkNoticePropagator]]: ...
    @overload
    def get_dependencies(self, predicate: Predicate[_T] | None = None) -> list[tuple[int, _T]]: ...

    def get_dependencies(self, predicate: Predicate[_T] | None = None):
        """Get the inputs of this propagator.

        Args:
            predicate: Only return inputs matching this.

        Returns:
            list[tuple[int, ChunkNoticePropagator]]: (key, input) pairs
        """
        if not predicate:
            return [(x, y.propagator) for x, y in self.get_dag_node().get_inputs() if y.propagator]
        return [(x, y.propagator) for x, y in self.get_dag_node().get_inputs() if predicate(y.propagator)]

    @overload
    def get_dependent(self, index: int = 0) -> ChunkNoticePropagator | None: ...

    @overload
    def get_dependent(self, index: int = 0, predicate: Predicate[_T] | None = None) -> _T: ...

    def get_dependent(
        self, index: int = 0, predicate: Predicate[_T] | None = None
    ) -> ChunkNoticePropagator | None | _T:
        """Get one output of this propagator.

        Args:
            index: The key of the output.
            predicate: Only return the output if it matches this.

        Returns:
            ChunkNoticePropagator | None: The output at index, or None if there is none

        Raises:
            ValueError: The output exists but does not match the predicate.
        """
        try:
            res = self.get_dag_node().get_output(index).propagator
        except (KeyError, IndexError, AttributeError):
            # A missing connection is "not found" -> None regardless of a type predicate.
            # The predicate only filters a connection that exists (see ValueError below).
            return None
        if not predicate or predicate(res):
            return res
        raise ValueError(f"Predicate {predicate} failed for get_dependency({index})")

    @overload
    def get_dependents(self) -> list[tuple[int, ChunkNoticePropagator]]: ...
    @overload
    def get_dependents(self, predicate: Predicate[_T] | None = None) -> list[tuple[int, _T]]: ...

    def get_dependents(self, predicate: Predicate[_T] | None = None):
        """Get every propagator that depends on this one (connected to its output).

        Args:
            predicate (Predicate[_T] | None, optional): Only return dependents matching this.
                Defaults to None.

        Returns:
            list[tuple[int, ChunkNoticePropagator]]: (key, dependent) pairs
        """
        if not predicate:
            return [(x, y.propagator) for x, y in self.get_dag_node().get_outputs() if y.propagator]
        return [(x, y.propagator) for x, y in self.get_dag_node().get_outputs() if predicate(y.propagator)]

    def get_callbacks(self) -> list[NoticeConnection]:
        """Get the callback connections, private ones included.

        Returns:
            list[NoticeConnection]: The callback connections
        """
        return self._callbacks.get(True)

    def get_on_create_callbacks(self) -> list[NoticeConnection]:
        """Get the callbacks to trigger when the chunk is added to the scene.

        Returns:
            list[NoticeConnection]: The connections to trigger when loaded
        """
        return self._callbacks.get_on_create()

    def find_callbacks(
        self,
        method: Callable | None = None,
        sender: ChunkNoticePropagator | None = None,
        notice_type: ChunkNoticeAlias | None = None,
    ) -> list[NoticeConnection]:
        """Find the NoticeConnections stored on this object that match every given filter.

        For example, passing `method` finds connections whose method is that function.

        Args:
            method (Callable, optional): Filter by method on connections. Defaults to None.
            sender (ChunkNoticePropagator, optional): Filter by sender. Defaults to None.
            notice_type (ChunkNoticeAlias, optional): Filter by alias. Defaults to None.

        Returns:
            list[NoticeConnection]: The matching connections
        """
        query = {"get_method": method, "sender": sender, "alias": notice_type}
        result = []

        def _conn_attr(conn, q):
            res = getattr(conn, q)
            if callable(res):
                return res()
            return res

        for conn in self.get_callbacks():
            if any([query[y] is not None and _conn_attr(conn, y) == query[y] for y in query]):
                result.append(conn)

        return result

    def add_callback(
        self,
        method: Callable[[NoticeT, Any], Any] | Callable[[Self, NoticeT, Any], Any],
        sender: ChunkNoticePropagator | None = None,
        listen_globally: bool = False,
        notice_alias: ChunkNoticeAlias = ChunkNoticeAlias.NONE,
        send_on_create=False,
        private=False,
    ):
        """Subscribe a callback to a notice.

        When the notice is sent, `method` is called with one of two signatures, chosen from the
        callback's own declared parameters:

        (notice(Tf.Notice), sender(Any))
            Matches Tf.Notice. This is what a bound method gets, where `self` is already the
            object holding the state -- the preferred form. Plain functions may declare it too.

        (owner(Any), notice(Tf.Notice), sender(Any))
            The convenience form for module-level functions, where `owner` is the object
            add_callback was called on. Note `owner` is not the same as `sender`: `sender` is
            whatever dispatched the notice, which may be a proxy, an attribute sender, or an
            unrelated object when listening globally.

        With no sender, self is assumed to be the sender, which keeps the simple case simple::

            my_param.add_callback(my_callback)

        Args:
            method (Callable): The method to call when the notice is sent
            sender (Any, optional): The sender to subscribe to. If None, self is used unless
                listen_globally is True. Defaults to None.
            listen_globally (bool, optional): Subscribe to every notice of type notice_alias,
                whatever its sender. Defaults to False.
            notice_alias (ChunkNoticeAlias, optional): The notice to subscribe to. NONE uses the
                sender's get_notice_alias(). Defaults to ChunkNoticeAlias.NONE.
            send_on_create (bool): Also call it when the owning object is added to the scene.
                Defaults to False.
            private (bool): Keep the callback out of serialization and copies, which is what a
                callback added inside __init__ needs. Defaults to False.
        """
        # If sender is not set and we should not listen for global notices, the sender will be self
        if sender is None and not listen_globally:
            sender = self

        # If sender is specified, only subscribe to changes from it.
        if isinstance(sender, ChunkNoticePropagator) and notice_alias is ChunkNoticeAlias.NONE:
            notice_alias = sender.get_notice_alias()

        self._callbacks.add(
            NoticeConnection(method, notice_alias, sender, listen_globally, self), send_on_create, private
        )

    @abstractmethod
    def get_edit_context(self) -> Sdf.Layer | None:
        """Get a layer the chunk object can safely add changes to the stage in.

        Returns:
            Sdf.Layer | None: The layer that can be modified
        """
        pass

    @abstractmethod
    def get_layer_identifier(self) -> str:
        """Get a unique layer identifier for creating and accessing layers.

        Returns:
            str: The unique layer identifier
        """
        pass

    @abstractmethod
    def get_stage(self) -> Usd.Stage | None:
        """Get a handle to the stage.

        Returns:
            Usd.Stage | None: A Usd.Stage if available
        """
        pass


class _GlobalDispatch:
    """The one real Tf.Notice.RegisterGlobally() per alias, shared by its global connections.

    Every globally=True NoticeConnection for that alias subscribes here; see
    _register_global.

    `callback` must be kept here: Tf.Notice.RegisterGlobally only holds a *weak* reference
    to the Python callable, so a functools.partial passed in without a surviving strong
    reference elsewhere gets garbage-collected almost immediately -- the registration then
    silently goes dead (Tf logs "expired python callback" and never calls it again). Keeping
    it as an attribute here ties its lifetime to `listener`, which is what we actually want.
    """

    __slots__ = ("listener", "callback", "subscribers")

    def __init__(self):
        self.listener: Tf.Notice.Listener | None = None
        self.callback: Callable | None = None
        self.subscribers: weakref.WeakSet[NoticeConnection] = weakref.WeakSet()


class _SharedGlobalListener:
    """Stand-in for NoticeConnection._listener when it shares a _GlobalDispatch.

    Revoke() (called by NoticeConnection.disconnect()) must only drop this one
    connection from the shared subscriber set -- never the underlying Tf listener,
    since other connections for the same alias may still be alive and depend on it.
    """

    __slots__ = ("_dispatch", "_connection")

    def __init__(self, dispatch: _GlobalDispatch, connection: NoticeConnection):
        self._dispatch = dispatch
        # Weak: the connection holds this object as its _listener, and a strong reference
        # back made a cycle. A replaced connection then stayed subscribed until the cyclic
        # GC ran, and every notice reached each stale copy -- after N re-initializations,
        # ChunkStageSync handled every PARAM_ADDED N times.
        self._connection = weakref.ref(connection)

    def Revoke(self):
        if (connection := self._connection()) is not None:
            self._dispatch.subscribers.discard(connection)


# One entry per alias, for the lifetime of the process -- never torn down, same as the
# infrastructure singletons (ChunkLayerManager, ChunkStageSync) that already register
# real Usd notices exactly once and keep them alive forever.
_global_dispatches: dict[ChunkNoticeAlias, _GlobalDispatch] = {}


def _dispatch_global(alias: ChunkNoticeAlias, notice, sender):
    dispatch = _global_dispatches.get(alias)
    if not dispatch:
        return
    for connection in list(dispatch.subscribers):
        connection.handle_callback(notice, sender)


def _register_global(alias: ChunkNoticeAlias, notice_type, connection: NoticeConnection) -> _SharedGlobalListener:
    """Ensure exactly one Tf.Notice.RegisterGlobally() exists for `alias`, and subscribe `connection`.

    Each NoticeConnection must not call Tf.Notice.RegisterGlobally() itself -- harmless for
    rarely-fired custom notices (e.g. CAMERA_CHANGED), but for the notices backed by real
    Usd.Notice types (STAGE_CONTENTS_CHANGED, STAGE_CHANGED, OBJECT_CHANGED,
    STAGE_EDIT_TARGET_CHANGED) two or more independent registrations reliably hang the
    interpreter on shutdown. Routing all globally=True connections for the same alias
    through one shared listener rules that out for every alias.
    """
    dispatch = _global_dispatches.get(alias)
    if dispatch is None:
        dispatch = _GlobalDispatch()
        dispatch.callback = functools.partial(_dispatch_global, alias)
        dispatch.listener = Tf.Notice.RegisterGlobally(notice_type, dispatch.callback)
        _global_dispatches[alias] = dispatch
    dispatch.subscribers.add(connection)
    return _SharedGlobalListener(dispatch, connection)


class NoticeConnection:
    """A Tf.Notice listener with a simple client-facing interface, as ChunkNoticePropagators store them.

    All references to external objects -- method, sender and owner -- are weakrefs, so the
    connection never takes ownership; properties give access to them. Because of this,
    connection.method, connection.owner or connection.sender may be None once collected. If
    any of the weakrefs dies, the connection is broken.

    The owner passes an additional object to the call, enabling::

        def func(obj: AClass, notice, sender):
            pass


        class AClass:
            def __init__(self):
                NoticeConnection(func, owner=self)

    as well as::

        class AnotherClass:
            def func(self, notice, sender):
                pass


        def bind(obj: AnotherClass):
            return NoticeConnection(obj.func, owner=obj)

    If the owner implements get_stage() and get_edit_context(), the method runs inside that
    edit context, so all its edits land in that layer.

    Attributes:
        alias: The ChunkNoticeAlias of the notice to listen for
        sender: The sender of the notice. When globally is True, this has no effect
        globally: Listen for every notice of this alias, whatever its sender
        owner: An optional owner object. Usually 'self', when adding callbacks on a class
    """

    __slots__ = ("alias", "_listener", "globally", "_method", "_sender", "_owner", "__weakref__")

    def __init__(
        self,
        method: Callable | None = None,
        alias: ChunkNoticeAlias = ChunkNoticeAlias.NONE,
        sender: Any | None = None,
        globally: bool = False,
        owner: ChunkNoticePropagator | None = None,
    ):
        self.alias: ChunkNoticeAlias = alias
        self.globally: bool = globally

        self._listener: Tf.Notice.Listener | None = None

        if method and method.__name__ == "<lambda>":
            self._method = method
        elif inspect.ismethod(method):
            self._method: Callable = weakref.WeakMethod(method, self._broke_callback())
        else:
            self._method: Callable = weakref.ref(method, self._broke_callback())

        self._sender: weakref.ReferenceType | None = weakref.ref(sender, self._broke_callback()) if sender else None
        self._owner: weakref.ReferenceType | None = weakref.ref(owner, self._broke_callback()) if owner else None

        self.connect()

    def get_method(self) -> Callable | None:
        """Get the referenced method or function.

        Returns:
            Callable | None: The callable if it is still alive, otherwise None
        """
        if not isinstance(self._method, weakref.ReferenceType):
            return self._method
        return self._method()

    @property
    def owner(self) -> ChunkNoticePropagator | None:
        """The owner passed to the callback, or None once collected."""
        return None if not self._owner else self._owner()

    @owner.setter
    def owner(self, obj: ChunkNoticePropagator | None):
        """Hold the owner weakly; the connection breaks when it is collected."""
        self._owner = weakref.ref(obj, self._broke_callback()) if obj else None

    @property
    def sender(self) -> Any | None:
        """The sender listened to, or None once collected."""
        return None if self._sender is None else self._sender()

    @sender.setter
    def sender(self, obj):
        """Hold the sender weakly; the connection breaks when it is collected."""
        self._sender = None if obj is None else weakref.ref(obj, self._broke_callback())



    def trigger(self):
        """Call the callback directly with this connection's alias and sender.

        No Tf.Notice is sent.
        """
        self.handle_callback(get_notice(self.alias), self.sender)

    @staticmethod
    def _wants_owner(meth) -> bool:
        """Whether `meth` declares the (owner, notice, sender) form rather than (notice, sender).

        Read from the callback's own signature, never from whether an owner happens to
        still be alive: that would make the arity depend on garbage-collection timing, and
        a three-argument callback would be called with two once its owner died.

        Uses ``co_argcount`` rather than ``inspect.signature`` because this runs on every
        dispatch. Callables without a ``__code__`` (builtins, ``functools.partial``,
        objects with ``__call__``) are assumed to take the (notice, sender) form.

        A decorated callback is read through to the function it wraps. A decorator's
        wrapper is usually ``(*args, **kwargs)``, whose ``co_argcount`` is 0, so an
        (owner, notice, sender) callback under one was called with two arguments and
        failed. ``functools.wraps`` records the original as ``__wrapped__``; without it
        there is nothing to read, and the wrapper's own signature decides.
        """
        func = meth.__func__ if inspect.ismethod(meth) else meth
        func = inspect.unwrap(func)
        code = getattr(func, "__code__", None)
        if code is None:
            return False
        count = code.co_argcount - 1 if inspect.ismethod(meth) else code.co_argcount
        return count >= 3

    def handle_callback(self, notice, sender=None, context=True):
        """Forward a notice to the callback, if the method, sender and owner are still alive.

        If the owner is valid and implements get_stage() and get_edit_context(), the method is
        called inside that edit context, so its stage edits land in the right layer.

        The callback may declare either (notice, sender) -- matching Tf.Notice -- or
        (owner, notice, sender). Which one it gets is decided by its own signature, so both
        forms work whether the callback is a plain function or a bound method.

        Args:
            notice (Tf.Notice): The notice to forward
            sender (ChunkNoticePropagator, optional): The sender of the original notice. Defaults to None.
            context (bool, optional): Run inside the owner's edit context. Defaults to True.
        """
        if not (meth := self.get_method()):
            return

        owner = self.owner
        if self._owner is not None and owner is None:
            # This connection was registered with an owner and that owner has since been
            # collected, so the subscriber no longer exists. Dispatching now would hand the
            # callback a dead owner; the connection is torn down via _connection_broke.
            return

        args = (owner, notice, sender) if self._wants_owner(meth) else (notice, sender)
        if owner:
            # Try running the callback function with the edit context to safely add any changes to the stage
            # to the correct layer

            stage = owner.get_stage()
            ctx = owner.get_edit_context()
            try:
                if stage and ctx and context:
                    # A muted chunk layer (view-driven: the chunk is dormant/hidden) is removed
                    # from the local layer stack, so entering an EditContext on it raises
                    # ("not in the local LayerStack"). A dormant chunk must not author anyway --
                    # skip; it re-syncs when it unmutes and recooks. Same guard as the execute path.
                    if stage.IsLayerMuted(ctx.identifier):
                        return
                    if not stage.HasLocalLayer(ctx):
                        # Not muted -- just not inserted into the local layer stack YET (hit
                        # live during load_chunks's bulk restore: a ListGroup proxy row's
                        # value setter bypasses the chunk_notice._loading_stage guard that
                        # suppresses every OTHER reactive path during that restore, so this
                        # can fire before get_or_create_layer has run for this chunk). Unlike
                        # the muted case, skipping the call entirely would be wrong here:
                        # nothing re-triggers this specific ATTRIBUTE_CHANGED notice later the
                        # way load_chunks's own explicit final execute() loop does for the
                        # reserved hook, so this would-be edit would just be lost. Call it
                        # without the EditContext wrapper instead -- a callback that (like
                        # chunks/usd/sublayer.py's) authors straight into its own
                        # get_edit_context() layer object, not through the stage's ambient
                        # edit target, works fine either way.
                        meth(*args)
                        return
                    with Usd.EditContext(stage, ctx):
                        meth(*args)
                else:
                    meth(*args)
            except Exception:
                logger.exception("failed to run method %s", meth)
            return

        meth(*args)

    def __del__(self):
        """Revoke the listener when the connection is collected."""
        self.disconnect()

    def connect(self, sender=None):
        """Build the Tf listener from this connection's settings.

        Args:
            sender (Any, optional): Override the sender. Defaults to None.
        """
        notice = get_notice(self.alias)
        if not notice:
            logger.warning("Could not find appropriate notice to connect for alias %s", self.alias)
            return

        if self._listener is not None:
            self.disconnect()

        if sender is not None:
            self._sender = weakref.ref(sender, self._broke_callback())

        if self.globally:
            self._listener = _register_global(self.alias, notice, self)
        elif self.sender is None:
            self._listener = Tf.Notice.RegisterGlobally(notice, self.handle_callback)
        elif self.sender:
            self._listener = Tf.Notice.Register(notice, self.handle_callback, self.sender)

    def _connection_broke(self, ref):
        # This is called when any of the _sender, _owner or _method weakrefs is killed, and will in turn call disconnect()
        self.disconnect()

    def _broke_callback(self) -> Callable:
        """_connection_broke for a weakref callback, holding this connection weakly.

        The bound method itself would hold it strongly, and the weakref holding the callback
        is this connection's own attribute -- a cycle that kept a replaced connection
        subscribed until the cyclic GC ran.
        """
        me = weakref.ref(self)

        def broke(ref):
            if (conn := me()) is not None:
                conn._connection_broke(ref)

        return broke

    def disconnect(self):
        """Revoke the stored Tf listener."""
        if self._listener is not None:
            self._listener.Revoke()
            self._listener = None


