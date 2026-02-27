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
)

from attrs import define, field
from pxr import Sdf, Tf, Usd
from typing_extensions import Self

logger = logging.getLogger(__name__)


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
    # Sent when the flags attribute changes
    FLAGS_CHANGED = auto()
    # Sent when the state attribute changes
    STATE_CHANGED = auto()

    PARAM_CHANGED = auto()  # Sent when a param changes
    PARAM_ADDED = auto()  # Sent when a param was added as a child to a group or chunk
    PARAM_REMOVED = auto()  # Sent when a param was removed as a child from a group or chunk. Not necessarily deleted

    GROUP_CHANGED = auto()  # Sent when a group changes. This includes nested groups and param changes

    CHUNK_CHANGED = auto()  # Similar to GROUP_CHANGED but only for chunks
    CHUNK_EXECUTED = auto()  # Sent when a chunks execute() function has been called

    # A chunk object was deleted. Use instead of PARAM_REMOVED for most applications, as PARAM_REMOVED only sends when
    # a param was added as a child to a NamedSyncedContainer (group / chunk / vector). CHUNK_DELETED on the other hand, sends whenever
    # ANY chunk object was deleted
    CHUNK_DELETED = auto()

    # UI notices
    MENU_ACTION_NOTICE = auto()

    # Stage notices
    PRIM_SELECTION_CHANGED = auto()
    PROP_SELECTION_CHANGED = auto()
    OBJECT_CHANGED = auto()  # Something was changed on the stage
    STAGE_CHANGED = auto()
    STAGE_CONTENTS_CHANGED = auto()
    STAGE_EDIT_TARGET_CHANGED = auto()

    # Registry notices
    CHUNK_LOADED = auto()  # Sent when a new chunk has beed added to the registry
    CHUNK_UNLOADED = auto()  # Sent when a chunk was unloaded

    CHUNK_CONFIG_CHANGED = auto()  # Sent when a chunk config was changed

    CUSTOM = auto()  # Custom notices should be registered with aliases > CUSTOM


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

    registered: Dict[int, type[ChunkNotice]] = {}

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
        notice = cls.registered.get(alias.value)
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
            cls.registered[alias.value] = notice_class


def chunk_notice(alias: ChunkNoticeAlias):
    """Used as decorator for new Tf.Notice classes that should be registered by the ChunkNoticeRegistry. This maps the alias to the decorated class,
    so a user can do get_notice(alias).Send()

    Args:
        alias (ChunkNoticeAlias): The alias which maps to the new Tf.Notice class
    """

    def _wrapper(cls: Type[ChunkNotice]):
        ChunkNoticeRegistry().register(alias, cls)
        Tf.Type.Define(cls)

        return cls

    return _wrapper


### Chunk / Group / Param changed notices
@chunk_notice(ChunkNoticeAlias.PARAM_CHANGED)
class ParamChangeNotice(ChunkNotice):
    pass


@chunk_notice(ChunkNoticeAlias.INPUT_CHANGED)
class InputChangedNotice(ChunkNotice):
    pass


@chunk_notice(ChunkNoticeAlias.GROUP_CHANGED)
class GroupChangeNotice(ChunkNotice):
    pass


@chunk_notice(ChunkNoticeAlias.CHUNK_CHANGED)
class ChunkChangeNotice(ChunkNotice):
    pass


@chunk_notice(ChunkNoticeAlias.CHUNK_EXECUTED)
class ChunkExecuteNotice(ChunkNotice):
    pass


### Create / Delete notices
@chunk_notice(ChunkNoticeAlias.PARAM_ADDED)
class ParamAddedNotice(ParamChangeNotice):
    pass


@chunk_notice(ChunkNoticeAlias.PARAM_REMOVED)
class ParamRemovedNotice(ParamChangeNotice):
    pass


@chunk_notice(ChunkNoticeAlias.CHUNK_DELETED)
class ChunkDeletedNotice(ChunkChangeNotice):
    pass


### Attribute notices
@chunk_notice(ChunkNoticeAlias.ATTRIBUTE_CHANGED)
class AttributeChangeNotice(ChunkNotice):
    pass


@chunk_notice(ChunkNoticeAlias.FLAGS_CHANGED)
class AttributeFlagsChangeNotice(AttributeChangeNotice):
    pass


@chunk_notice(ChunkNoticeAlias.STATE_CHANGED)
class AttributeStateChangeNotice(AttributeChangeNotice):
    pass


### Chunk registry load / unload notices
@chunk_notice(ChunkNoticeAlias.CHUNK_LOADED)
class ChunkLoadedNotice(ChunkNotice):
    pass


@chunk_notice(ChunkNoticeAlias.CHUNK_UNLOADED)
class ChunkUnloadedNotice(ChunkNotice):
    pass


@chunk_notice(ChunkNoticeAlias.CHUNK_CONFIG_CHANGED)
class ChunkConfigChanged(ChunkNotice):
    pass


### UI Notices
@chunk_notice(ChunkNoticeAlias.MENU_ACTION_NOTICE)
class MenuActionNotice(ChunkNotice):
    pass


### UsdView selection notices
@chunk_notice(ChunkNoticeAlias.PRIM_SELECTION_CHANGED)
class PrimSelectionChangedNotice(ChunkNotice):
    pass


@chunk_notice(ChunkNoticeAlias.PROP_SELECTION_CHANGED)
class PropSelectionChangedNotice(ChunkNotice):
    pass


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


def is_notice_type(notice: Tf.Notice, alias: ChunkNoticeAlias) -> bool:
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
        if data is not None:
            logger.warning(
                f"Attempting to send notice {alias} with custom data which is not supported. Consider sending a ChunkNotice instead"
            )
        notice = notice_class()

    if sender is None:
        notice.SendGlobally()
    else:
        notice.Send(sender)
    return notice


CallbackT: TypeAlias = Callable[[Optional[ChunkNotice], Any, Optional[Set[ChunkNotice]]], Optional[ChunkNotice]]
ConnSetT: TypeAlias = weakref.WeakValueDictionary[int, "DagNode"]


@define(slots=True)
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
        "_input_data",
        "_dirty",
        "_connections",
        "_callback",
        "_propagator",
        "__weakref__",
    )

    def __init__(self, propagator: "ChunkNoticePropagator"):
        self._input_data: Set[Tuple[ChunkNoticeAlias, "DagNode"]] = set()
        self._dirty = True
        self._connections: DagConnections = DagConnections()
        self._propagator = weakref.ref(propagator)
        self._index: int = 0

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

    def add_dependency(self, node: "DagNode", connect_reverse=True):
        """
        Add a dependency (aka input) to this node. This will dirty self

        Args:
            node (DagNode): The new dependency to add
            connect_reverse (bool, optional): If the input node should connect this node to its output. Defaults to True.
        """
        self._connections.inputs[self.next_key(self._connections.inputs)] = node
        if connect_reverse:
            node.add_dependant(self)
        else:
            self.dirty = True

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

        if owner is None:
            self._connections.owner = None
        else:
            self._connections.owner = weakref.ref(owner)

        if node := self.get_owner_node():
            node.dirty = True

    def get_inputs(self) -> Iterator["DagNode"]:
        """
        Get the inputs (dependency) nodes of this node

        Returns:
            Iterator[DagNode]: An iterator for the input nodes
        """
        return self._connections.inputs.values()

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

    def get_outputs(self) -> Iterator["DagNode"]:
        """
        Get the output (dependant) nodes of this node

        Returns:
            Iterator[DagNode]: An iterator for the output nodes
        """
        return self._connections.outputs.values()

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

        for node in self.get_outputs():
            node._index += self._index + 1

            node._input_data.add((ChunkNoticeAlias.INPUT_CHANGED, self))
            node.dirty_dependants(searched.copy())

        if owner := self.get_owner_node():
            owner._index += self._index + 1

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
        return any([x.dirty for x in self.get_inputs()])

    def solve_inputs(self) -> Generator:
        """
        Safely solve all inputs of this node. Will find all dirty input paths and solve their outputs, consequently solving this node

        Returns:
            Optional[ChunkNotice]: The result of the solve if successful, otherwise None
        """
        dirty_inputs = False
        for node in filter(lambda x: x.dirty, self.get_inputs()):
            if res := node.solve_inputs():
                yield from res

            dirty_inputs = True

        if not dirty_inputs:
            yield from self.solve_outputs()

    def solve_outputs(self) -> Generator["DagNode", None, None]:
        """
        Solve self + outputs + owner. This is the main solve method that clients should use, as it will trigger all dependent nodes
        to solve when this node has solved.

        Args:
            notice (Optional[ChunkNotice]): The input notice
            sender (Any): The input sender
            propagated (Optional[ChunkNoticePropagator], optional): An optional propagator. Defaults to None.

        Returns:
            Optional[ChunkNotice]: The result notice, being either the input notice, or the result from the callback, or None
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

        for node in self.get_outputs():
            yield from node.solve_outputs()

        if node := self.get_owner_node():
            yield from node.solve_outputs()

        self._input_data.clear()
        self._index = 0


class ChunkNoticePropagator:
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
    Connections added with add_connection (as well as add_callback) are not physical, meaning they are not managed by the DAG graph.
    These are instead managed by the Tf.Notice system, without any dependency information. Hence a callback will not trigger another callback
    implicitly.

    By default, the send_self() method is called whenever an attribute or child attribute has been modified. The notice that is sent is
    determined by the return value of get_notice_alias(). This notice is then also propagated up the owner hierarchy by the DagNode

    get_edit_context(), get_layer_identifier() and get_stage() can be implemented to support layer contexts when modifying the stage.
    Any NoticeConnection will intercept the connected notices and set the edit context to the context (Sdf.Layer) returned by get_edit_context()
    before running the actual callback function. This is to safely run callbacks and contain them to the layers managed by ChunkLayerManager.
    See NoticeConnection.handle_callback for more info
    """

    _callbacks: List["NoticeConnection"]
    _dag_node: Optional[DagNode]
    _blocked: bool

    def __init__(self, *args, **kwargs) -> None:
        self._callbacks = []
        self._dag_node = None
        self._blocked = False

        super().__init__(*args, **kwargs)

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
            sender (Any): The python object which was used as sender for the notice
            propagated (ChunkNotice): The propagated notice. The first notice that was sent

        Returns:
            Optional[ChunkNotice]: The result from calling send_self
        """

        if not notice or (notice and notice.alias != ChunkNoticeAlias.STATE_CHANGED):
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

        if alias != ChunkNoticeAlias.STATE_CHANGED and self.is_blocked():
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
        for conn in src._callbacks:
            self._copy_callback(conn, src)

    def _copy_callback(self, conn: "NoticeConnection", src: "ChunkNoticePropagator"):
        """
        Copy a connection from src to this object.
        Rewire the connection if conn.sender is the source of the copy (self)

        Args:
            conn (NoticeConnection): The connection to copy
            src (ChunkNoticePropagator): The source ChunkNoticePropagator to copy from
        """
        if meth := conn.get_method():
            sender = self if conn.sender == src else conn.sender
            owner = self if conn.owner is not None else None
            new_conn = NoticeConnection(meth, conn.alias, sender, conn.globally, owner)
            self.add_connection(new_conn)

    def add_connection(self, conn: "NoticeConnection"):
        """
        Add a non-physical connection to be stored on this instance

        Args:
            conn (NoticeConnection): The connection to add
        """
        self._callbacks.append(conn)

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

            for alias, iprop in n.iter_changes():
                if not iprop:
                    continue

                if alias == prop.get_notice_alias():
                    continue

                msg += f"   {alias.name}\n"
                prop.data_changed(get_notice(alias)(iprop.get_notice_data()))

            if notice := prop.send_self():
                msg += f"   {notice.alias.name}\n"

        msg += "====== FINISHED ======\n"
        logger.debug(msg)

    def add_dependency(self, input: "ChunkNoticePropagator"):
        """
        Add an input. This propagator will not 'evaulate' until all inputs are solved

        Args:
            input (ChunkNoticePropagator): The node to add as input
        """
        self.get_dag_node().add_dependency(input.get_dag_node())

    def get_dependency(self, index: int = 0) -> Optional["ChunkNoticePropagator"]:
        """
        Get an input of this propagator. The input is found from the managed DagNode. Return None if not found

        Returns:
            ChunkNoticePropagator | None: The input connected at index, or None if not found
        """
        try:
            return self.get_dag_node().get_input(index).propagator
        except (IndexError, AttributeError):
            return None

    def get_callbacks(self) -> List["NoticeConnection"]:
        """
        Get the lsut of callback connections

        Returns:
            List[NoticeConnection]: The callback connections
        """
        return self._callbacks

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

        query = {"method": method, "sender": sender, "alias": notice_type}
        result = []
        for conn in self.get_callbacks():
            if any([query[y] is not None and getattr(conn, y) == query[y] for y in query]):
                result.append(conn)

        return result

    def add_callback(
        self,
        method: Callable[[ChunkNotice, Any], Any] | Callable[[Self, ChunkNotice, Any], Any],
        sender: Optional["ChunkNoticePropagator"] = None,
        listen_globally: bool = False,
        notice_alias: ChunkNoticeAlias = ChunkNoticeAlias.NONE,
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
        """

        # If sender is not set and we should not listen for global notices, the sender will be self
        if sender is None and not listen_globally:
            sender = self

        # If sender is specified, only subscribe to changes from it.
        if isinstance(sender, ChunkNoticePropagator) and notice_alias is ChunkNoticeAlias.NONE:
            notice_alias = sender.get_notice_alias()

        self.add_connection(NoticeConnection(method, notice_alias, sender, listen_globally, self))

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


class NoticeConnection:
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

    __slots__ = ("alias", "listener", "globally", "_method", "_sender", "_owner", "__weakref__")

    def __init__(
        self,
        method: Callable,
        alias: ChunkNoticeAlias,
        sender: Optional[Any] = None,
        globally: bool = False,
        owner: Optional[ChunkNoticePropagator] = None,
    ):
        self.alias: ChunkNoticeAlias = alias
        self.listener: Optional[Tf.Notice.Listener] = None

        self.globally: bool = globally

        if inspect.ismethod(method):
            self._method: Callable = weakref.WeakMethod(method, self._method_died)
        else:
            self._method: Callable = weakref.ref(method, self._method_died)

        self._sender: Optional[weakref.ReferenceType] = weakref.ref(sender, self._sender_died) if sender else None
        self._owner: Optional[weakref.ReferenceType] = weakref.ref(owner, self._owner_died) if owner else None

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
        self._owner = weakref.ref(obj, self._owner_died) if obj else None

    @property
    def sender(self) -> Optional[Any]:
        return None if self._sender is None else self._sender()

    @sender.setter
    def sender(self, obj):
        self._sender = None if obj is None else weakref.ref(obj, self._sender_died)

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

        if self.listener is not None:
            self.disconnect()

        if sender is not None:
            self._sender = weakref.ref(sender)

        if self.sender is None or self.globally:
            self.listener = Tf.Notice.RegisterGlobally(notice, self.handle_callback)
        elif self.sender:
            self.listener = Tf.Notice.Register(notice, self.handle_callback, self.sender)

    def _sender_died(self, ref):
        self.disconnect()

    def _owner_died(self, ref):
        self.disconnect()

    def _method_died(self, ref):
        self.disconnect()

    def disconnect(self):
        """
        Calls Revoke() on the stored TfListener
        """
        if self.listener is not None:
            self.listener.Revoke()
            self.listener = None


class SceneSender(ChunkNoticePropagator):
    """
    Abstract class for implementing scene access for different DCC's. Should have access to the UsdStage and send notices for scene changes.
    See implementation in plugins/usdview/__init__.py
    """

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
    def get_stage(self) -> Usd.Stage:
        raise NotImplementedError

    def get_selected_prims(self) -> List[Usd.Prim]:
        """
        Get the selected prims in the scene

        Returns:
            List[Usd.Prim]: Selected prims
        """
        raise NotImplementedError

    def get_selected_property(self) -> Usd.Property:
        """
        Get the selected property in the scene

        Returns:
            Usd.Property: Selected property
        """
        raise NotImplementedError

    def primSelectionChangedCallback(self, *args, **kwargs):
        try:
            send_notice(ChunkNoticeAlias.PRIM_SELECTION_CHANGED, sender=self, data=self.get_selected_prims())
        except Tf.ErrorException:
            pass

    def propSelectionChangedCallback(self, *args, **kwargs):
        try:
            send_notice(ChunkNoticeAlias.PROP_SELECTION_CHANGED, sender=self, data=self.get_selected_property())
        except Tf.ErrorException:
            pass

    def get_notice_alias(self) -> ChunkNoticeAlias:
        return ChunkNoticeAlias.NONE