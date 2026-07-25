"""
The chunk_notice module defines the base classes for all TfNotice and hierarchical logic.
TfNotice is the USD built-in callback system, similar to Qt's signals. This module uses
TfNotices extensively, but hides most of the Tf.Notice API behind our own logic. The
goal is for end-users to easily be up and running with the callbacks, without having to
manage the lifetime of the listeners.

We promote the ChunkNoticeAlias as an alias for the supported TfNotice classes, which
makes it easy to find the notice type you're interested in without having to care about
its implementation.

All the custom TfNotices are subclassed from ChunkNotice, which provide some additional data,
that may vary in type depending on the notice and how sends it.

The easiest way of sending a notice would be to do it explicitly, using send_notice:
``` py
send_notice(ChunkNoticeAlias.PARAM_CHANGED, someParam, {"any": "data"}):
```

"""

import functools
import inspect
import logging
import weakref
from abc import abstractmethod
from enum import IntEnum, auto
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    Iterator,
    List,
    Optional,
    Set,
    Tuple,
    Type,
    TypeAlias,
    TypeVar,
    TYPE_CHECKING,
    overload,
    TypeGuard,
    Literal,
)

from attrs import define, field
from pxr import Sdf, Tf, Usd, Gf
from typing_extensions import Self
from usd_chunks import chunk_serial

logger = logging.getLogger(__name__)

# Set to True while a param is writing its value to USD attrs so that
# ChunkStageSync._on_objects_changed skips those synchronous ObjectsChanged callbacks.
_usd_write_active: bool = False
_loading_stage = None  # Set during load_chunks so callbacks can access the stage during file open

# The chunk whose module.execute() is currently running, or None. Execution-scoped (set and
# cleared around each execute() call below), NOT persistent config — it only answers "who is
# authoring right now" so chunk_utils.get_edit_layer can resolve that chunk's Edit Target from
# graph topology when a caller (e.g. author_in_variant_context) didn't pass an explicit owner.
_executing_chunk = None


_T = TypeVar("_T")
Predicate: TypeAlias = Callable[[Any], TypeGuard[_T]]

if TYPE_CHECKING:
    from usd_chunks import chunk_core


class ChunkNoticeAlias(IntEnum):
    """
    An alias is essentially just the name of a supported Tf.Notice implmentation. We're using aliases as a client-facing interface for
    working with the notices.

    Any supported notice should be reflected with an alias in this enum class

    Attributes:
        NONE : A default, empty alias
        PARAM_CHANGED : Sent when a param changes
        GROUP_CHANGED : Sent when a group changes. This includes nested groups and param changes
        CHUNK_CHANGED : Similar to GROUP_CHANGED but only for chunks

        PARAM_ADDED : Sent when a param was added as a child to a group or chunk
        PARAM_REMOVED : Sent when a param was removed as a child from a group or chunk. Not necessarily deleted

        CHUNK_DELETED : A chunk object was deleted. Use instead of PARAM_REMOVED for most applications, as PARAM_REMOVED only sends when
                        a param was added as a child to a NamedSyncedContainer (group / chunk / vector). CHUNK_DELETED on the other hand,
                        sends wheneverANY chunk object was deleted

        ATTRIBUTE_CHANGED : Default notice for attributes that does not set an alias excplicitly
        FLAGS_CHANGED : Sent when the flags attribute changes
        STATE_CHANGED : Sent when the state attribute changes

        MENU_ACTION_NOTICE: Sent when a menu action should be executed

        PRIM_SELECTION_CHANGED : Sent whenever the prim selection changed in UsdView
        PROP_SELECTION_CHANGED : Sent whenever the prop selection changed in UsdView
        OBJECT_CHANGED : Sent whenever anything was changed on the stage
        STAGE_CHANGED : Sent when the stage is changed
        STAGE_CONTENTS_CHANGED : Sent when stage contents changed
        STAGE_EDIT_TARGET_CHANGED : Sent when the edit target on the stage changed

        CHUNK_LOADED : Sent when a new chunk has beed added to the registry
        CHUNK_UNLOADED : Sent when a chunk was unloaded

        CUSTOM : Custom notices should be registered with aliases > CUSTOM

    """

    # Chunk parameters and value notices
    NONE = 0

    INPUT_CHANGED = auto()
    # Default notice for attributes that does not set an alias excplicitly
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

    # Gizmo notices
    GIZMO_START = auto()
    GIZMO_STOP = auto()
    GIZMO_CHNAGED = auto()

    # Stage notices
    PRIM_SELECTION_CHANGED = auto()
    PROP_SELECTION_CHANGED = auto()
    CAMERA_CHANGED = auto()
    OBJECT_CHANGED = auto()  # Something was changed on the stage
    STAGE_CHANGED = auto()
    STAGE_CONTENTS_CHANGED = auto()
    STAGE_EDIT_TARGET_CHANGED = auto()

    # Registry notices
    CHUNK_LOADED = auto()  # Sent when a new chunk has beed added to the registry
    CHUNK_UNLOADED = auto()  # Sent when a chunk was unloaded

    CHUNK_CONFIG_CHANGED = auto()  # Sent when a chunk config was changed

    DEPENDENCY_ADDED = auto()  # Sent when a DAG dependency is added between two propagators

    CUSTOM = auto()  # Custom notices should be registered with aliases > CUSTOM


# Notices that must be delivered even from a blocked chunk. Blocking (view-driven: the chunk is
# dormant/hidden) suppresses data/execution notices, but STRUCTURAL edits — adding/removing
# child chunks, load/delete — must still work so you can edit inside a group that isn't wired to
# an active view (a blocked group otherwise swallows its children's PARAM_ADDED, so their prims
# never get created). Execution is still gated separately (execute()/callback muted-layer guards).
_STRUCTURAL_ALIASES = frozenset({
    ChunkNoticeAlias.STATE_CHANGED,
    ChunkNoticeAlias.PARAM_ADDED,
    ChunkNoticeAlias.PARAM_REMOVED,
    ChunkNoticeAlias.CHUNK_LOADED,
    ChunkNoticeAlias.CHUNK_DELETED,
})


class RegistrySingleton(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super(RegistrySingleton, cls).__call__(*args, **kwargs)
        return cls._instances[cls]


class ChunkNotice(Tf.Notice):
    """
    A Chunk notice is a wrapper around TfNotice but with some additional data. This interface is meant for recievers of the notice to easily
    find what has been changed. It also enables globally registered listeners to know what data was changed.

    Attributes:
        data(ChunkNoticeData): The data to send with the notice. This may vary between
        alias(ChunkNoticeAlias): The alias assiciated with this notice. This may vary between
    """

    data: Any = None
    alias: ChunkNoticeAlias = ChunkNoticeAlias.NONE

    def __init__(self, data: Any = None):
        super().__init__()
        self.data = data


NoticeT = TypeVar("NoticeT", bound=ChunkNotice)


class ChunkNoticeRegistry(metaclass=RegistrySingleton):
    """
    Register a new notice. Easiest way to register a new notice is to wrap a class declaration with @chunk_notice, as:
    ``` py
    @chunk_notice(ChunkNoticeAlias.PARAM_CHANGED)
    class ParamChangeNotice(ChunkNotice):
        pass
    ```

    Attributes:
        registered (Dict[int, Type[Tf.Notice]]): A dictionary mapping between a ChunkNoticeAlias and a registered TfNotice
    """

    registered: Dict[ChunkNoticeAlias, type[ChunkNotice]] = {}

    @classmethod
    def get_from_alias(cls, alias: ChunkNoticeAlias) -> Type[ChunkNotice]:
        """
        Get a registered notice from a ChunkNoticeAlias

        Args:
            alias (ChunkNoticeAlias): The alias to look up

        Raises:
            Tf.ErrorException: If the alias has not been registered

        Returns:
            Type[Tf.Notice]: The class of the registered notice
        """
        notice = cls.registered.get(alias)
        if notice is None:
            raise Tf.ErrorException(f"No notice found for alias {alias}")
        return notice

    @classmethod
    def register(cls, alias: ChunkNoticeAlias, notice_class: Type[Any]):
        """
        Register a new notice and associate it with an alias

        Args:
            alias (ChunkNoticeAlias): The alias which to associate with the notice
            notice_class (Type[Any]): The class that should be registered
        """
        if alias not in cls.registered:
            notice_class.alias = alias
            if not hasattr(notice_class, "data"):
                notice_class.data = None
            cls.registered[alias] = notice_class


def chunk_notice(alias: ChunkNoticeAlias):
    """Used as decorator for new Tf.Notice classes that should be registered by the ChunkNoticeRegistry. This maps the alias to the decorated class,
    so a user can do get_notice(alias).Send()

    Args:
        alias (ChunkNoticeAlias): The alias which maps to the new Tf.Notice class
    """

    def _wrapper(cls):
        ChunkNoticeRegistry().register(alias, cls)
        Tf.Type.Define(cls)
        return cls

    return _wrapper


### Chunk / Group / Param changed notices
@chunk_notice(ChunkNoticeAlias.PARAM_CHANGED)
class ParamChangeNotice(ChunkNotice):
    data: "chunk_core.ChunkParamBase"


@chunk_notice(ChunkNoticeAlias.INPUT_CHANGED)
class InputChangedNotice(ChunkNotice):
    data: "chunk_core.ChunkCommon"


@chunk_notice(ChunkNoticeAlias.GROUP_CHANGED)
class GroupChangeNotice(ChunkNotice):
    data: "chunk_core.ChunkGroupBase"


@chunk_notice(ChunkNoticeAlias.CHUNK_CHANGED)
class ChunkChangeNotice(ChunkNotice):
    data: "chunk_core.ChunkBase"


### Create / Delete notices
@chunk_notice(ChunkNoticeAlias.PARAM_ADDED)
class ParamAddedNotice(ChunkNotice):
    data: "chunk_core.ChunkCommon"


@chunk_notice(ChunkNoticeAlias.PARAM_REMOVED)
class ParamRemovedNotice(ChunkNotice):
    data: "chunk_core.ChunkCommon"


@chunk_notice(ChunkNoticeAlias.CHUNK_DELETED)
class ChunkDeletedNotice(ChunkNotice):
    data: "chunk_core.ChunkCommon"


### Attribute notices
@chunk_notice(ChunkNoticeAlias.ATTRIBUTE_CHANGED)
class AttributeChangeNotice(ChunkNotice):
    data: "chunk_core.AttributeSender"


@chunk_notice(ChunkNoticeAlias.VALUE_CHANGED)
class ValueChangedNotice(AttributeChangeNotice):
    pass


@chunk_notice(ChunkNoticeAlias.FLAGS_CHANGED)
class AttributeFlagsChangeNotice(AttributeChangeNotice):
    pass


@chunk_notice(ChunkNoticeAlias.STATE_CHANGED)
class AttributeStateChangeNotice(AttributeChangeNotice):
    pass


### Gizmo notices
@chunk_notice(ChunkNoticeAlias.GIZMO_START)
class GizmoStartedNotice(ChunkNotice):
    pass


@chunk_notice(ChunkNoticeAlias.GIZMO_STOP)
class GizmoStoppedNotice(ChunkNotice):
    pass


@chunk_notice(ChunkNoticeAlias.GIZMO_CHNAGED)
class GizmoChangedNotice(ChunkNotice):
    pass


### Chunk registry load / unload notices
@chunk_notice(ChunkNoticeAlias.CHUNK_LOADED)
class ChunkLoadedNotice(ChunkNotice):
    data: "chunk_core.ChunkBase"


@chunk_notice(ChunkNoticeAlias.CHUNK_UNLOADED)
class ChunkUnloadedNotice(ChunkNotice):
    data: "chunk_core.ChunkBase"


@chunk_notice(ChunkNoticeAlias.CHUNK_CONFIG_CHANGED)
class ChunkConfigChanged(ChunkNotice):
    data: "chunk_core.ChunkConfig"


@chunk_notice(ChunkNoticeAlias.DEPENDENCY_ADDED)
class DependencyAddedNotice(ChunkNotice):
    """Sent when add_dependency() is called. sender=dependent, data=input propagator."""
    data: "chunk_notice.ChunkNoticePropagator"


### UI Notices
@chunk_notice(ChunkNoticeAlias.MENU_ACTION_NOTICE)
class MenuActionNotice(ChunkNotice):
    pass


@chunk_notice(ChunkNoticeAlias.WIDGET_CHANGED)
class WidgetChangedNotice(ChunkNotice):
    data: "chunk_core.ChunkParamWidgetBase"


### UsdView selection notices
@chunk_notice(ChunkNoticeAlias.PRIM_SELECTION_CHANGED)
class PrimSelectionChangedNotice(ChunkNotice):
    data: List[Usd.Prim]


@chunk_notice(ChunkNoticeAlias.PROP_SELECTION_CHANGED)
class PropSelectionChangedNotice(ChunkNotice):
    data: Usd.Property


@chunk_notice(ChunkNoticeAlias.CAMERA_CHANGED)
class CameraChangedNotice(ChunkNotice):
    data: Gf.Camera


# Register externally manages notice just to map to the custom enum
ChunkNoticeRegistry().register(ChunkNoticeAlias.OBJECT_CHANGED, Usd.Notice.ObjectsChanged)
ChunkNoticeRegistry().register(ChunkNoticeAlias.STAGE_CHANGED, Usd.Notice.StageNotice)
ChunkNoticeRegistry().register(ChunkNoticeAlias.STAGE_CONTENTS_CHANGED, Usd.Notice.StageContentsChanged)
ChunkNoticeRegistry().register(ChunkNoticeAlias.STAGE_EDIT_TARGET_CHANGED, Usd.Notice.StageEditTargetChanged)


def get_notice(alias: ChunkNoticeAlias) -> Type[ChunkNotice]:
    """Return a class of type Tf.Notice for the specified ChunkNoticeAlias

    Args:
        alias (ChunkNoticeAlias): The ChunkNoticeAlias enum to get a Tf.Notice class from

    Returns:
        Tf.Notice | None: None if the mapping to notice_type does not exist. Else, a Tf.Notice class representing the ChunkNoticeAlias
    """
    return ChunkNoticeRegistry().get_from_alias(alias)


def is_notice_type(notice: Tf.Notice, alias: ChunkNoticeAlias) -> TypeGuard[ChunkNotice]:
    """Check if the alias stands for the specific Tf.Notice class

    Args:
        notice (Tf.Notice): The Tf Notice class to compare
        alias (ChunkNoticeAlias): The notice type to compare

    Returns:
        bool: True if get_notice(alias) returns the same type as notice
    """
    return type(notice) is get_notice(alias)


def send_notice(alias: ChunkNoticeAlias, sender: Any = None, data: Any = None) -> Optional[ChunkNotice]:
    """
    Sends a notice of type alias, triggering subscribed callback functions to run

    Args:
        alias (ChunkNoticeAlias): The type of the notice to send
        sender (Any, optional): The object to pass as sender. If None, the notice will be sent globally. Defaults to None.
        data (Any, optional): Any additional data to attach to the notice object. Defaults to None.
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
        logger.exception(f"Could not send notice {notice}")
        return

    return notice


ConnSetT: TypeAlias = weakref.WeakValueDictionary[int, "DagNode"]


@define(slots=True, hash=True)
class DagConnections:
    """
    A dataclass for the DagNode connections

    Attributes:
        owner: The owning node of the DagNode. This is similar to a an output, with the difference being that clients
               may use the owner like a parent of the node. Thre result being that any changes to a DagNode will propagate
               to the owner. An ownership hierarchy may look like: AttributeSender->ChunkParamBase->NameSyncedContainer->Chunk
               whereas 'outputs' would typically reference nodes outside of the hierarchy
        inputs: A WeakValueDictionary of DagNodes. Any input node is per definition a dependency for this node. All inputs must be
                clean before this node can evaluate.
        outputs: A WeakValueDictionary of DagNodes. Referencing the outgoing connections from this node. When this node is dirtied, so will
                 all outputs be.
    """

    owner: Optional[weakref.ReferenceType["ChunkNoticePropagator"]] = field(default=None)
    inputs: ConnSetT = field(factory=ConnSetT)
    outputs: ConnSetT = field(factory=ConnSetT)


class DagGraphID:
    def __init__(self):
        self._store: Optional[DagGraphID] = None

    def root(self) -> "DagGraphID":
        """
        Resolve the terminal DagGraphID of the _store chain iteratively, compressing
        the path so every visited id points directly at the root afterwards.
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
        return id(self.root())


class DagNode:
    """
    A DagNode represents a connectable node in a DAG graph. This class handles any 'physical' connections between different
    ChunkNoticePropagator objects, contrary to the NoticeConnection class which handles non-physical connections. A DagNode may
    have a number of inputs/outputs, and one owner. Dirtying a node will dirty all outputs + owner in a recursive fashion. A DagNode
    holds a function that is called when the node is solved prior to being marked clean. A client may solve this node + all outgoing
    connections by calling solve_outputs()

    Attributes:
        dirty (bool): If this node is dirty, i.e solve_self has not been called
        propagator(ChunkNoticePropagator): The propagator managing this node.
    """

    __slots__ = (
        "_index",
        "_sort_key",
        "_graph_id",
        "_part_of_graph",
        "_input_data",
        "_dirty",
        "_connections",
        "_propagator",
        "__weakref__",
    )

    def __init__(self, propagator: "ChunkNoticePropagator"):
        self._input_data: Set[Tuple[ChunkNoticeAlias, "DagNode"]] = set()
        self._dirty = True
        self._connections: DagConnections = DagConnections()
        self._propagator = weakref.ref(propagator)
        self._index: int = 0

        self._sort_key: int = 0
        self._graph_id: DagGraphID = DagGraphID()
        self._part_of_graph = True

    @property
    def propagator(self):
        if prop := self._propagator():
            return prop
        return None

    @property
    def dirty(self):
        return self._dirty

    @property
    def name(self):
        # Useful for debugging the solve
        return self.propagator.name if hasattr(self.propagator, "name") else self.propagator.__class__.__name__  # type: ignore

    @dirty.setter
    def dirty(self, val: bool):
        self._dirty = val
        if val:
            self.dirty_dependants()

    def disconnect(self):
        self._connections.inputs.clear()
        self._connections.outputs.clear()

    def next_key(self, items: ConnSetT) -> int:
        """
        Get the next key for either inputs or outputs dictionaries. The result is safe to use as key for the dict.

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
    ) -> Generator[Tuple[ChunkNoticeAlias, Optional["ChunkNoticePropagator"]], None, StopIteration]:
        for alias, node in sorted(self._input_data, key=lambda x: x[1]._index):
            yield alias, node.propagator
        return StopIteration()

    def set_graph_id(self, node: "DagNode"):
        if not (node._part_of_graph and self._part_of_graph):
            return

        my_root = self._graph_id.root()
        other_root = node._graph_id.root()
        if my_root is other_root:
            # Already the same graph; linking again would create a cycle
            return
        other_root._store = my_root

    def add_dependency(self, node: "DagNode", connect_reverse=True) -> int:
        """
        Add a dependency (aka input) to this node. This will dirty self

        Args:
            node (DagNode): The new dependency to add
            connect_reverse (bool, optional): If the input node should connect this node to its output. Defaults to True.
        """
        key = self.next_key(self._connections.inputs)
        self._connections.inputs[key] = node
        self.set_graph_id(node)

        if connect_reverse:
            node.add_dependant(self)
        else:
            self.dirty = True

        self._sort_key += 1 + node._sort_key
        for _, o in self.get_outputs():
            o._sort_key += 1 + node._sort_key

        owner = self.get_owner_node()
        while owner:
            owner._sort_key += 1 + node._sort_key
            owner = owner.get_owner_node()

        return key

    def add_dependant(self, node: "DagNode"):
        """
        Add a node that is dependant of this node (aka output)

        Args:
            node (DagNode): The dependant node
        """
        self._connections.outputs[self.next_key(self._connections.outputs)] = node
        self.dirty = True

    def set_owner(self, owner: Optional["ChunkNoticePropagator"]):
        """
        Set an owner of this node. The owner is treated similarly to an output, but with some semantic differences. See the description
        of DagConnections for more info

        Args:
            owner (Optional[ChunkNoticePropagator]): The owner propagator
        """
        if owner == self.get_owner():
            return

        assert isinstance(owner, ChunkNoticePropagator) or owner is None

        if owner is None:
            self._connections.owner = None
        else:
            self._connections.owner = weakref.ref(owner)

        if node := self.get_owner_node():
            node.dirty = True
            node.set_graph_id(self)

    def get_inputs(self) -> Iterator[Tuple[int, "DagNode"]]:
        """
        Get the inputs (dependency) nodes of this node

        Returns:
            Iterator[DagNode]: An iterator for the input nodes
        """
        return self._connections.inputs.items()

    def get_input(self, index: int) -> "DagNode":
        """
        Get a specific input, with key of index

        Args:
            index (int): The index of the input to find

        Raises:
            KeyError: If the index does not exist

        Returns:
            DagNode: The found dag node

        """
        try:
            return self._connections.inputs[index]
        except KeyError:
            raise

    def get_outputs(self) -> Iterator[Tuple[int, "DagNode"]]:
        """
        Get the output (dependant) nodes of this node

        Returns:
            Iterator[DagNode]: An iterator for the output nodes
        """
        return self._connections.outputs.items()

    def get_output(self, index: int) -> "DagNode":
        """
        Get a specific output, with key of index

        Args:
            index (int): The index of the output to find

        Raises:
            KeyError: If the index does not exist

        Returns:
            DagNode: The found dag node
        """

        return self._connections.outputs[index]

    def get_owner(self) -> Optional["ChunkNoticePropagator"]:
        """
        get the owner of this node. Contrary to the propagator member, the owner is essentially the 'parent' node,
        treated similarly to an output node, while the propagator member is the ChunkNoticePropagator that this node
        essentially represents in the DAG graph

        Returns:
            ChunkNoticePropagator | None: The owner if it exists, else None
        """
        if owner := self._connections.owner:
            return owner()
        return None

    def get_owner_node(self) -> Optional["DagNode"]:
        """
        Get the node of the owner. Since the owner is a ChunkNoticePropagator, this is just a wrapper around
        owner.get_dag_node()

        Returns:
            Optional[DagNode]: The dag node of the owner, or None if it does not exist
        """
        if owner := self.get_owner():
            return owner.get_dag_node()
        return None

    def should_solve(self) -> bool:
        """
        Return whether this node should solve. The node should solve if:
        not dirty, has outputs, has callback or has owner.

        Returns:
            bool: whether the node should solve
        """
        if not self.dirty:
            return False

        if self.get_outputs() or self.get_owner_node():
            return True
        return False

    def get_sort_key(self):
        return self._sort_key

    def get_graph_id(self):
        return self._graph_id.get_identifier()

    def dirty_dependants(self, searched: Optional[Set["DagNode"]] = None):
        """
        Dirty all dependants. This will recursively set dependant (output) nodes dirty=True
        """

        if searched is None:
            searched = set()

        if self in searched:
            # If the node was added to the local copy of the searched set, then it is a circular dependency
            raise RecursionError("DAG has circular dependencies at node:", self.propagator)

        self._dirty = True
        searched.add(self)

        for _, node in self.get_outputs():
            node._index += 1
            node._input_data.add((ChunkNoticeAlias.INPUT_CHANGED, self))
            node.dirty_dependants(searched.copy())

        if owner := self.get_owner_node():
            owner._index += 1

            owner._input_data |= self._input_data
            if self.propagator and (alias := self.propagator.get_notice_alias()):
                owner._input_data.add((alias, self))
            owner.dirty_dependants(searched.copy())

    def is_inputs_dirty(self) -> bool:
        """
        Check if any dependency (input) is dirty

        Returns:
            bool: True if any dependency is dirty
        """
        return any([x.dirty for _, x in self.get_inputs()])

    def solve_inputs(self) -> Generator:
        """
        Safely solve all inputs of this node. Will find all dirty input paths and solve their outputs, consequently solving this node

        Returns:
            Optional[ChunkNotice]: The result of the solve if successful, otherwise None
        """
        dirty_inputs = False
        for _, node in filter(lambda x: x[1].dirty, self.get_inputs()):
            if res := node.solve_inputs():
                yield from res

            dirty_inputs = True

        if not dirty_inputs:
            yield from self.solve_outputs()

    def solve_outputs(self) -> Generator["DagNode", None, None]:
        """
        Solve self + outputs + owner. This is the main solve method that clients should use, as it will trigger all dependent nodes
        to solve when this node has solved.

        Returns:
            None: If the node should not solve

        Yields:
            DagNode: All solved outputs, including self
        """
        if not self.should_solve():
            return None

        if self.is_inputs_dirty():
            if g := self.solve_inputs():
                yield from g

            self._input_data.clear()
            self._index = 0
            return

        self.dirty = False
        yield self

        # Snapshot the outputs: get_outputs() is a live view over a WeakValueDictionary, and
        # a downstream solve_outputs() can add new output nodes (e.g. a chunk that grows a
        # dynamic list of rows during load), which would otherwise raise "dictionary changed
        # size during iteration". Newly-added nodes drive their own solve when created, so
        # skipping them in this pass is fine.
        for _, node in list(self.get_outputs()):
            yield from node.solve_outputs()

        if node := self.get_owner_node():
            yield from node.solve_outputs()

        self._input_data.clear()
        self._index = 0


class CallbackContainer(chunk_serial.Serializeable):
    """
    A container for managing callbacks on the ChunkNoticePropagator. Some callbacks can be tagged private, or should be executed when a chunk is
    loaded. This is handled here rather than directly on the NoticeConnection objects as to not pollute that class.

    This is mostly to have an interface for managing private and non-private callbacks. Private callbcks will not be serialized and not copied between
    ChunkNoticePropagators. When a propagator is serialized, callbacks that are created in __init__ should ideally not be serialized, as they will be recreated
    when de-serializing. Hence, if creating callbacks in __init__, make sure to tag the callbacks as private to not get duplicate callbacks when de-serializing.

    Callbacks are stored in self.callbacks as a list of dicts. An entry of the list will have keys 'conn', 'on_create' and 'private', where conn is the NoticeConnection
    object
    """

    def __init__(self):
        super().__init__()
        self.callbacks: List[Dict[Literal["conn", "on_create", "private"], Any]] = []

    def should_serialize_attribute(self, name: str, value: Any) -> Tuple[bool, Any]:
        # We serialize only non-private callbacks
        if name == "callbacks":
            return True, [
                {"conn": x.get("conn"), "on_create": x.get("on_create")} for x in self.callbacks if not x.get("private")
            ]
        return False, None

    def post_serial_setattr(self, name, value):
        if name == "on_create_callbacks":
            return super().post_serial_setattr(name, weakref.WeakSet(value))
        super().post_serial_setattr(name, value)

    def add(self, conn: "NoticeConnection", trigger_on_create=True, private=False):
        self.callbacks.append({"conn": conn, "on_create": trigger_on_create, "private": private})

    def get(self, include_private=False):
        return [x["conn"] for x in self.callbacks if include_private or (not x.get("private"))]

    def get_on_create(self, include_private=False):
        return [x["conn"] for x in self.callbacks if x.get("on_create") and (include_private or (not x.get("private")))]

    def extend(self, other: "CallbackContainer"):
        for data in other.callbacks:
            if data.get("private"):
                continue

            self.add(data["conn"], data.get("on_create", True))

    def clear(self):
        self.callbacks.clear()


class ChunkNoticePropagator(chunk_serial.Serializeable):
    """
    A base class that implements the connectability of chunk objects. Chunk objects may be connected to eachother through
    'physical' connections, and to callbacks through 'non-physical' connections. These two connection methods differ in how
    they are executed, where physical connections rely on dependencies (inputs) and dependants (outputs and owners) and non-physical
    connections rely on the Tf.Notice system, where the connections do not have dependencies. Callbacks are non-physical,
    inputs/outputs/owner are physical.

    **Physical:**\n
    'Children' reference their owning object (parent), and call data_changed() on the owner when the child has been
    modified. This is achieved by the DagNode, which is meant to manage the physical connections between objects. The DagNode
    holds references to inputs, outputs and an owner. DagNodes solve their outputs recursively while respecting the dependencies,
    meaning a node will not solve until all it's inputs are solved.
    Any number of dependencies (aka inputs) can be added to an instance of this class. When All dependencies are
    solved, the DagNode of the instance will also solve; resulting in the data_changed() being called. This is not really useful
    until callbacks are used with the instance; then a client can build dependency graphs of relevant chunk classes,
    and have the system trigger callbacks when appropriate.

    **Non-physical:**\n
    Connections added with add_callback are not physical, meaning they are not managed by the DAG graph.
    These are instead managed by the Tf.Notice system, without any dependency information. Hence a callback will not trigger another callback
    implicitly.

    By default, the send_self() method is called whenever an attribute or child attribute has been modified. The notice that is sent is
    determined by the return value of get_notice_alias(). This notice is then also propagated up the owner hierarchy by the DagNode

    get_edit_context(), get_layer_identifier() and get_stage() can be implemented to support layer contexts when modifying the stage.
    Any NoticeConnection will intercept the connected notices and set the edit context to the context (Sdf.Layer) returned by get_edit_context()
    before running the actual callback function. This is to safely run callbacks and contain them to the layers managed by ChunkLayerManager.
    See NoticeConnection.handle_callback for more info
    """

    _callbacks: CallbackContainer
    _dag_node: Optional[DagNode]
    _blocked: bool

    def __init__(self, *args, **kwargs) -> None:
        self._callbacks = CallbackContainer()
        self._dag_node = None
        self._blocked = False
        super().__init__(*args, **kwargs)

    def serialize(self, glb):
        if (res := super().serialize(glb)) and isinstance(res, chunk_serial.SerializedClass):
            res.attributes["dependencies"] = [node for _, node in self.get_dependencies()]

            if self.get_dag_node()._part_of_graph and (owner := self.get_owner()):
                # The owner should always be serialized as a reference. This ensures no circular dependencies
                res.attributes["owner"] = chunk_serial.SerializedReference(hash(owner)).as_dict()
        return res

    def should_serialize_attribute(self, name: str, value: Any) -> Tuple[bool, Any]:
        if name == "_callbacks":
            return True, value
        if name.startswith("_"):
            return False, value
        return super().should_serialize_attribute(name, value)

    def post_serial_setattr(self, name, value):
        self.set_blocked(True)
        if name == "dependencies":
            # Since the _dependencies is an attribute of the DagNode, calling setattr(self, '_dependencies', value)
            # would set the _dependencies on the AttributesBase instance. So instead we make sure the dependencies goes to the
            # DagNode. The dependencies are serialized to the AttributesBase
            for dep in value:
                self.add_dependency(dep)
        elif name == "_callbacks":
            # Instead of overriding the callbacks, we extend it in order to keep callbacks created prior to post_serial_setattr() is called, i.e. from __init__.
            # An example of this is the ChunkBase class that creates some callbacks in __init__, but as they are private, they are not serialized, therefore
            # if we were to do self._callbacks = value, those initial callbacks would be deleted
            self._callbacks.extend(value)
        elif name == "owner":
            self.set_owner(value)
        else:
            super().post_serial_setattr(name, value)
        self.set_blocked(False)

    @abstractmethod
    def request_delete(self):
        raise NotImplementedError

    def get_notice_data(self) -> Self:
        """
        When calling send_self, the notice of type self.get_notice_alias() will add custom data to the notice before sending.
        The returned object from this function is the data associtated with the notice

        Returns:
            Any : The data which will be added to the Tf.Notice object before sending.
        """
        return self

    @abstractmethod
    def get_notice_alias(self) -> ChunkNoticeAlias:
        """Return the default notice type for when add_callback() is called without notice_type argument, or when calling send_self().
        This should be overwritten in sub-classes in order to send the proper notice when something on the chunk object changed
        Returns:

            chunk_notice.NoticeType: The default notice type to send for callbacks
        """
        raise NotImplementedError

    @abstractmethod
    def is_blocked(self) -> bool:
        """
        Check whether this instance should be blocked from sending notices

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
        self._blocked = block

    def get_owner(self) -> Optional["ChunkNoticePropagator"]:
        """Get the owning ChunkNoticePropagator of this object

        Returns:
            Optional["ChunkNoticePropagator"]: The owning object, or None
        """
        return self.get_dag_node().get_owner()

    def set_owner(self, owner: Optional["ChunkNoticePropagator"]):
        """Set the owner of this object. The owner is represents a "parent" in a parent-child relationship. This function
        should therefore be used once the parent have actually taken ownership of this object. I.e, this function should only
        be used to update the child when the relationship have changed.

        Args:
            owner (ChunkNoticePropagator | None): The owning (parent) object that should recieve notices when this instance is modified
        """
        self.get_dag_node().set_owner(owner)

    def data_changed(self, notice: Optional[ChunkNotice] = None) -> Optional[ChunkNotice]:
        """Called by the DagNode when the node is solved. This will be called when all inputs (dependencies) are solved.
        The returned notice will be propagated by the DagNode

        Args:
            notice (chunk_notice.InterfaceChangeNotice): The notice object which was sent

        Returns:
            Optional[ChunkNotice]: The result from calling send_self
        """

        if not notice or (notice and notice.alias not in _STRUCTURAL_ALIASES):
            if self.is_blocked():
                return None
        if notice:
            notice.Send(self)
        return notice

    def send_self(self, alias: Optional[ChunkNoticeAlias] = None, data: Any = None) -> Optional[ChunkNotice]:
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

    def _copy_callbacks(self, src: "ChunkNoticePropagator"):
        """
        Copy connections to this object from src

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
        """
        get the DagNode for this ChunkNoticePropagator instance. If it does not exist, a new one will be
        created

        Returns:
            DagNode: The DagNode held by this ChunkNoticePropagator instance
        """
        if not self._dag_node:
            self._dag_node = DagNode(self)
        return self._dag_node

    def execute(self):
        """
        Calls solve_outputs() on the managed DagNode. This will trigger the ChunkNoticePropagator to call data_changed,
        and subsequently send_self(), triggering any connected callbacks
        """

        node = self.get_dag_node()
        node.dirty = True

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

            # A pure state change (selection, collapse, disable) propagates only as
            # STATE_CHANGED — it must not re-run the module. Data changes (params/inputs) and a
            # direct dirty solve (execute-on-creation, which carries no propagated change) still
            # execute. Selection/disable side effects run above via data_changed / send_self.
            state_only = bool(changes) and all(a == ChunkNoticeAlias.STATE_CHANGED for a, _ in changes)
            if not state_only and (module := getattr(prop, '_module', None)):
                stage = prop.get_stage()
                ctx = prop.get_edit_context()
                # A disabled chunk's edit layer is muted, i.e. removed from the stage's local
                # layer stack. Don't re-run its module: authoring into a muted layer raises
                # ("Layer ... is not in the local LayerStack"), and a disabled chunk must not
                # execute anyway.
                muted = bool(stage and ctx and stage.IsLayerMuted(ctx.identifier))
                # A blocked chunk (view-driven: not in the active view's tree — e.g. connected to
                # an inactive view) must never run its module, whatever triggered the solve. recook
                # unblocks the active view's tree before executing it, so cooking is unaffected.
                if (callable(getattr(module, 'execute', None)) and not getattr(prop, '_executing', False)
                        and not muted and not prop.is_blocked()):
                    global _usd_write_active, _executing_chunk
                    prev_executing_chunk = _executing_chunk
                    prop._executing = True
                    _usd_write_active = True
                    _executing_chunk = getattr(prop, "get_chunk", lambda: None)()
                    try:
                        if stage and ctx:
                            with Usd.EditContext(stage, ctx):
                                module.execute(prop)
                        else:
                            module.execute(prop)
                    finally:
                        _usd_write_active = False
                        _executing_chunk = prev_executing_chunk
                        prop._executing = False

        msg += "====== FINISHED ======\n"
        logger.debug(msg)

    def add_dependency(self, input: "ChunkNoticePropagator") -> int:
        """
        Add an input. This propagator will not 'evaulate' until all inputs are solved

        Args:
            input (ChunkNoticePropagator): The node to add as input

        Returns:
            int: The dependency index
        """
        idx = self.get_dag_node().add_dependency(input.get_dag_node())
        send_notice(ChunkNoticeAlias.DEPENDENCY_ADDED, self, data=input)
        return idx

    @overload
    def get_dependency(self, index: int = 0) -> Optional["ChunkNoticePropagator"]: ...
    @overload
    def get_dependency(self, index: int = 0, predicate: Optional[Predicate[_T]] = None) -> _T: ...

    def get_dependency(
        self, index: int = 0, predicate: Optional[Predicate[_T]] = None
    ) -> Optional["ChunkNoticePropagator"] | _T:
        """
        Get an input of this propagator. The input is found from the managed DagNode. Return None if not found

        Returns:
            ChunkNoticePropagator | None: The input connected at index, or None if not found
            _T: The input connected at index matching the predicate if supplied
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
    def get_dependencies(self) -> List[Tuple[int, "ChunkNoticePropagator"]]: ...
    @overload
    def get_dependencies(self, predicate: Optional[Predicate[_T]] = None) -> List[Tuple[int, _T]]: ...

    def get_dependencies(self, predicate: Optional[Predicate[_T]] = None):
        """
        Get dependencies of this node. If no predicate is supplied, returns all dependencies.

        Returns:
            List[Tuple[int, "ChunkNoticePropagator"]]: All inputs
            List[Tuple[int, _T]]: The inputs matching the predicate
        """
        if not predicate:
            return [(x, y.propagator) for x, y in self.get_dag_node().get_inputs() if y.propagator]
        return [(x, y.propagator) for x, y in self.get_dag_node().get_inputs() if predicate(y.propagator)]

    @overload
    def get_dependent(self, index: int = 0) -> Optional["ChunkNoticePropagator"]: ...

    @overload
    def get_dependent(self, index: int = 0, predicate: Optional[Predicate[_T]] = None) -> _T: ...

    def get_dependent(
        self, index: int = 0, predicate: Optional[Predicate[_T]] = None
    ) -> Optional["ChunkNoticePropagator"] | _T:
        """
        Get an input of this propagator. The input is found from the managed DagNode. Return None if not found

        Returns:
            ChunkNoticePropagator | None: The input connected at index, or None if not found
            _T: The input connected at index matching the predicate if supplied
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
    def get_dependents(self) -> List[Tuple[int, "ChunkNoticePropagator"]]: ...
    @overload
    def get_dependents(self, predicate: Optional[Predicate[_T]] = None) -> List[Tuple[int, _T]]: ...

    def get_dependents(self, predicate: Optional[Predicate[_T]] = None):
        """
        Get all propagators that depend on this propagator (nodes connected to the output of this node)

        Args:
            predicate (Optional[Predicate[_T]], optional): An optional predicate function. Defaults to None.

        Returns:
            List[ChunkNoticePropagator]: When no predicate is supplied, all dependents are returned
            List[_T]: Dependents matching the predicate
        """
        if not predicate:
            return [(x, y.propagator) for x, y in self.get_dag_node().get_outputs() if y.propagator]
        return [(x, y.propagator) for x, y in self.get_dag_node().get_outputs() if predicate(y.propagator)]

    def get_callbacks(self) -> List["NoticeConnection"]:
        """
        Get the list of callback connections

        Returns:
            List[NoticeConnection]: The callback connections
        """
        return self._callbacks.get(True)

    def get_on_create_callbacks(self) -> List["NoticeConnection"]:
        """
        Get all callbacks that should be triggered when the chunk is added to the scene

        Returns:
            List[NoticeConnection]: A tuple containing all connections that should be triggered when loaded
        """
        return self._callbacks.get_on_create()

    def find_callbacks(
        self,
        method: Optional[Callable] = None,
        sender: Optional["ChunkNoticePropagator"] = None,
        notice_type: Optional[ChunkNoticeAlias] = None,
    ) -> List["NoticeConnection"]:
        """Find a NoticeConnection object stored on self. Filter the search using the arguments, f.e. if method is passed,
        will look for NoticeConnection objects where connection.method == method

        Args:
            method (Callable, optional): Filter by method on connections. Defaults to None.
            sender (ChunkNoticePropagator, optional): Filter by sender. Defaults to None.
            notice_type (chunk_notice.NoticeType, optional): Filter by NoticeType. Defaults to None.

        Returns:
            List[chunk_notice.NoticeConnection]: A list of found connections matching the arguments
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
        sender: Optional["ChunkNoticePropagator"] = None,
        listen_globally: bool = False,
        notice_alias: ChunkNoticeAlias = ChunkNoticeAlias.NONE,
        send_on_create=False,
        private=False,
    ):
        """Subscribe to a notice. When the notice is sent, the supplied method will be called, by default, with three arguments:
        (owner(self), notice(Tf.Notice), sender(Any))
        if `send_self==True`
        then the notice will expect 2 parameters:
        (notice(Tf.Notice), sender(Any))

        If `sender==None`, then we assume self to be the sender of the notice. This enables some nice and simple syntax:
        ``` py
        myParam.add_callback(myCallback)
        ```
        If listen_globally is True, subscribe to all notices of the same type regardless of the sender.

        Args:
            method (Callable): The method to call when the notice is sent
            sender (Any, optional): A sender which to subscribe to. If None, will use self as sender if listen_globally=False, else None. Defaults to None.
            listen_globally (bool, optional): Whether to subscribe to all events of type notice_alias. Defaults to False.
            notice_alias (NoticeType, optional): A specific type of notice to subscribe to. If left at NONE, will use the get_notice_alias(). Defaults to ChunkNoticeAlias.NONE
            send_on_create (bool): If the callback should be called when the owning object is added to the scene. Defaults to True
            private (bool): Mark the callback as private, meaning the callback will not be serialized or copied. This is useful when adding callbacks inside an __init__ method. Defaults to False
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
        """
        Get a Sdf.Layer for which the chunk object can safely add changes to the stage

        Returns:
            Sdf.Layer: The layer that can be modified
        """
        pass

    @abstractmethod
    def get_layer_identifier(self) -> str:
        """
        Get a unique layer identifier for creation / access of layers

        Returns:
            str: The unique layer identifier
        """
        pass

    @abstractmethod
    def get_stage(self) -> Usd.Stage | None:
        """
        Get a handle to the stage

        Returns:
            Usd.Stage | None: A Usd.Stage if available
        """
        pass


class _GlobalDispatch:
    """Bookkeeping for the one real Tf.Notice.RegisterGlobally() per alias shared by
    every globally=True NoticeConnection for that alias. See _register_global.

    `callback` must be kept here: Tf.Notice.RegisterGlobally only holds a *weak*
    reference to the Python callable, so a functools.partial passed in without a
    surviving strong reference elsewhere gets garbage-collected almost immediately —
    the registration then silently goes dead (Tf logs "expired python callback" and
    never calls it again). Keeping it as an attribute here ties its lifetime to
    `listener`, which is what we actually want.
    """

    __slots__ = ("listener", "callback", "subscribers")

    def __init__(self):
        self.listener: Optional[Tf.Notice.Listener] = None
        self.callback: Optional[Callable] = None
        self.subscribers: "weakref.WeakSet[NoticeConnection]" = weakref.WeakSet()


class _SharedGlobalListener:
    """Stand-in for NoticeConnection._listener when it shares a _GlobalDispatch.

    Revoke() (called by NoticeConnection.disconnect()) must only drop this one
    connection from the shared subscriber set — never the underlying Tf listener,
    since other connections for the same alias may still be alive and depend on it.
    """

    __slots__ = ("_dispatch", "_connection")

    def __init__(self, dispatch: "_GlobalDispatch", connection: "NoticeConnection"):
        self._dispatch = dispatch
        self._connection = connection

    def Revoke(self):
        self._dispatch.subscribers.discard(self._connection)


# One entry per alias, for the lifetime of the process — never torn down, same as the
# infrastructure singletons (ChunkLayerManager, ChunkStageSync) that already register
# real Usd notices exactly once and keep them alive forever.
_global_dispatches: Dict["ChunkNoticeAlias", _GlobalDispatch] = {}


def _dispatch_global(alias: "ChunkNoticeAlias", notice, sender):
    dispatch = _global_dispatches.get(alias)
    if not dispatch:
        return
    for connection in list(dispatch.subscribers):
        connection.handle_callback(notice, sender)


def _register_global(alias: "ChunkNoticeAlias", notice_type, connection: "NoticeConnection") -> _SharedGlobalListener:
    """Ensure exactly one Tf.Notice.RegisterGlobally() exists for `alias`, and add
    `connection` as one of its subscribers.

    A separate NoticeConnection created per chunk instance for the same alias used to
    each call Tf.Notice.RegisterGlobally() directly — harmless for rarely-fired custom
    notices (e.g. CAMERA_CHANGED), but for the notices backed by real Usd.Notice types
    (STAGE_CONTENTS_CHANGED, STAGE_CHANGED, OBJECT_CHANGED, STAGE_EDIT_TARGET_CHANGED)
    2+ independent registrations reliably hung the interpreter on shutdown. Routing all
    globally=True connections for the same alias through one shared listener avoids the
    whole class of bug, for every alias, not just the ones we've hit so far.
    """
    dispatch = _global_dispatches.get(alias)
    if dispatch is None:
        dispatch = _GlobalDispatch()
        dispatch.callback = functools.partial(_dispatch_global, alias)
        dispatch.listener = Tf.Notice.RegisterGlobally(notice_type, dispatch.callback)
        _global_dispatches[alias] = dispatch
    dispatch.subscribers.add(connection)
    return _SharedGlobalListener(dispatch, connection)


class NoticeConnection(chunk_serial.Serializeable):
    """
    A storage for a TfListener object. This class provides a simple client-facing interface for
    creating callbacks for the TfNotice system, with some additional functionality. This exists also
    to store the information for the callbacks held by ChunkNoticePropagators.

    All references to external objects, i.e, method, sender and owner, are stored as weakrefs as to not
    take ownership. Properties are implemented to access them. Because of this, notice.method,
    notice.owner or notice.sender may be None if they have been garbage collected. If any of the weakrefs
    dies, the connection is broken.

    The owner can be used to pass an additional object as an argument to the function call, enabling
    ``` py
    def func(obj: AClass, notice, sender):
        pass


    class AClass:
        def __init__(self):
            NoticeConnection(func, owner=self)
    ```
    as well as:

    ``` py
    class AnotherClass:
        def func(self, notice, sender):
            pass


    def bind(obj: AnotherClass):
        return NoticeConnection(obj.func, owner=obj)
    ```

    If the owner implements get_stage() and get_edit_context(), the method will be called with the
    edit context as to contain all edits from the method to the edit context layer.

    Attributes:
        alias: A ChunkNoticeAlias associated to the notice we want to send
        sender: The sender of the notice. When globally is True, this has not effect
        globally: If we dont care about the sender but listening for all notices
            of the same alias.
        owner: An optional owner object. Usually 'self', when adding callbacks on a class
    """

    __slots__ = ("alias", "_listener", "globally", "_method", "_sender", "_owner", "__weakref__")

    def __init__(
        self,
        method: Optional[Callable] = None,
        alias: ChunkNoticeAlias = ChunkNoticeAlias.NONE,
        sender: Optional[Any] = None,
        globally: bool = False,
        owner: Optional[ChunkNoticePropagator] = None,
    ):
        self.alias: ChunkNoticeAlias = alias
        self.globally: bool = globally

        self._listener: Optional[Tf.Notice.Listener] = None

        if method and method.__name__ == "<lambda>":
            self._method = method
        elif inspect.ismethod(method):
            self._method: Callable = weakref.WeakMethod(method, self._connection_broke)
        else:
            self._method: Callable = weakref.ref(method, self._connection_broke)

        self._sender: Optional[weakref.ReferenceType] = weakref.ref(sender, self._connection_broke) if sender else None
        self._owner: Optional[weakref.ReferenceType] = weakref.ref(owner, self._connection_broke) if owner else None

        self.connect()

    def get_method(self) -> Callable | None:
        """
        Get the referenced method or function

        Returns:
            Callable | None: A callable function or method if it's still alive. Otherwise None
        """
        if not isinstance(self._method, weakref.ReferenceType):
            return self._method
        return self._method()

    @property
    def owner(self) -> Optional[ChunkNoticePropagator]:
        return None if not self._owner else self._owner()

    @owner.setter
    def owner(self, obj: Optional[ChunkNoticePropagator]):
        self._owner = weakref.ref(obj, self._connection_broke) if obj else None

    @property
    def sender(self) -> Optional[Any]:
        return None if self._sender is None else self._sender()

    @sender.setter
    def sender(self, obj):
        self._sender = None if obj is None else weakref.ref(obj, self._connection_broke)

    def serialize_kwarg(self, name: str, default: Any) -> Any:
        if name == "method":
            return self.get_method()
        if name == "alias":
            return self.alias
        if name == "sender":
            return self.sender
        if name == "globally":
            return self.globally
        if name == "owner":
            return self.owner
        return super().serialize_kwarg(name, default)

    def should_serialize_attribute(self, name: str, value: Any) -> Tuple[bool, Any]:
        # No attributes should be serialized as the attributes are serialized by kwargs
        return False, None

    def trigger(self):
        """
        Trigger the connection by calling the callback with the alias and sender members as arguments.
        This will not send any TfNotice.
        """
        self.handle_callback(get_notice(self.alias), self.sender)

    def handle_callback(self, notice, sender=None, context=True):
        """
        Forwards the notice after checking that the method, sender and owner are still alive and valid.
        If the owner is valid and implements get_stage() and get_edit_context(), the method is called
        with the respective edit context as to contain any stage edits to the correct layer.

        Args:
            notice (Tf.Notice): The notice to forward
            sender (ChunkNoticePropagator, optional): The sender of the original notice. Defaults to None.
        """
        if not (meth := self.get_method()):
            return

        args = (self.owner, notice, sender) if (self.owner and not inspect.ismethod(meth)) else (notice, sender)
        if self.owner:
            # Try running the callback function with the edit context to safely add any changes to the stage
            # to the correct layer

            stage = self.owner.get_stage()
            ctx = self.owner.get_edit_context()
            try:
                if stage and ctx and context:
                    # A muted chunk layer (view-driven: the chunk is dormant/hidden) is removed
                    # from the local layer stack, so entering an EditContext on it raises
                    # ("not in the local LayerStack"). A dormant chunk must not author anyway —
                    # skip; it re-syncs when it unmutes and recooks. Same guard as the execute path.
                    if stage.IsLayerMuted(ctx.identifier):
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
        self.disconnect()

    def connect(self, sender=None):
        """
        Builds the Tf.Listener from the member variables

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
            self._sender = weakref.ref(sender, self._connection_broke)

        if self.globally:
            self._listener = _register_global(self.alias, notice, self)
        elif self.sender is None:
            self._listener = Tf.Notice.RegisterGlobally(notice, self.handle_callback)
        elif self.sender:
            self._listener = Tf.Notice.Register(notice, self.handle_callback, self.sender)

    def _connection_broke(self, ref):
        # This is called when any of the _sender, _owner or _method weakrefs is killed, and will in turn call disconnect()
        self.disconnect()

    def disconnect(self):
        """
        Calls Revoke() on the stored TfListener
        """
        if self._listener is not None:
            self._listener.Revoke()
            self._listener = None


