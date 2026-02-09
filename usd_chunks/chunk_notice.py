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

from pxr import Tf, Usd, Sdf

from enum import IntEnum, auto, Enum
from typing import Callable, Any, List, Type, Tuple, Dict, Optional, Generator, TypeAlias, DefaultDict, TypedDict
from typing_extensions import Self, NotRequired
from abc import abstractmethod
import weakref
import inspect
from collections import defaultdict
from attrs import define, field


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
    PARAM_CHANGED = auto()  # Sent when a param changes
    GROUP_CHANGED = auto()  # Sent when a group changes. This includes nested groups and param changes
    CHUNK_CHANGED = auto()  # Similar to GROUP_CHANGED but only for chunks

    PARAM_ADDED = auto()  # Sent when a param was added as a child to a group or chunk
    PARAM_REMOVED = auto()  # Sent when a param was removed as a child from a group or chunk. Not necessarily deleted

    # A chunk object was deleted. Use instead of PARAM_REMOVED for most applications, as PARAM_REMOVED only sends when
    # a param was added as a child to a NamedSyncedContainer (group / chunk / vector). CHUNK_DELETED on the other hand, sends whenever
    # ANY chunk object was deleted
    CHUNK_DELETED = auto()

    # Default notice for attributes that does not set an alias excplicitly
    ATTRIBUTE_CHANGED = auto()

    # Sent when the flags attribute changes
    FLAGS_CHANGED = auto()
    # Sent when the state attribute changes
    STATE_CHANGED = auto()

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


@chunk_notice(ChunkNoticeAlias.GROUP_CHANGED)
class GroupChangeNotice(ChunkNotice):
    pass


@chunk_notice(ChunkNoticeAlias.CHUNK_CHANGED)
class ChunkChangeNotice(ChunkNotice):
    pass


### Create / Delete notices
@chunk_notice(ChunkNoticeAlias.PARAM_ADDED)
class ParamAddedNotice(ChunkNotice):
    pass


@chunk_notice(ChunkNoticeAlias.PARAM_REMOVED)
class ParamRemovedNotice(ChunkNotice):
    pass


@chunk_notice(ChunkNoticeAlias.CHUNK_DELETED)
class ChunkDeletedNotice(ChunkNotice):
    pass


### Attribute notices
@chunk_notice(ChunkNoticeAlias.ATTRIBUTE_CHANGED)
class AttributeChangeNotice(ChunkNotice):
    pass


@chunk_notice(ChunkNoticeAlias.FLAGS_CHANGED)
class AttributeFlagsChangeNotice(ChunkNotice):
    pass


@chunk_notice(ChunkNoticeAlias.STATE_CHANGED)
class AttributeStateChangeNotice(ChunkNotice):
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
            print(
                f"Attempting to send notice {alias} with custom data which is not supported. Consider sending a ChunkNotice instead"
            )
        notice = notice_class()

    if sender is None:
        notice.SendGlobally()
    else:
        notice.Send(sender)
    return notice


CallbackT: TypeAlias = Callable[[Optional[ChunkNotice], Any, Optional["ChunkNoticePropagator"]], Optional[ChunkNotice]]
ConnSetT: TypeAlias = weakref.WeakSet["DagNode"]


@define(slots=True)
class DagConnections:
    owner: Optional[weakref.ReferenceType["ChunkNoticePropagator"]] = field(default=None)
    inputs: ConnSetT = field(factory=ConnSetT)
    outputs: ConnSetT = field(factory=ConnSetT)


class DagNode:
    __slots__ = ("_dirty", "_connections", "_callback", "name", "__weakref__")

    def __init__(self, callback: Optional[CallbackT] = None):

        self._dirty = False
        self._connections: DagConnections = DagConnections()

        self._callback: Optional[weakref.ReferenceType[CallbackT]] = None
        self.set_callback(callback)

        self.name = ""

    def set_callback(self, callback: CallbackT | None):
        if not callback:
            self._callback = None
        elif inspect.ismethod(callback):
            self._callback = weakref.WeakMethod(callback)
        else:
            self._callback = weakref.ref(callback)

    @property
    def dirty(self):
        return self._dirty

    @dirty.setter
    def dirty(self, val: bool):
        self._dirty = val
        if val:
            self.dirty_dependents()

    def add_input(self, node: "DagNode", connect_output=True):
        self._connections.inputs.add(node)
        if connect_output:
            node.add_output(self)
        self.dirty = True

    def add_output(self, node: "DagNode"):
        self._connections.outputs.add(node)

    def set_owner(self, owner: Optional["ChunkNoticePropagator"]):
        if owner is None:
            self._connections.owner = None
            return

        self._connections.owner = weakref.ref(owner)

    def get_inputs(self) -> weakref.WeakSet["DagNode"]:
        return self._connections.inputs

    def get_outputs(self) -> weakref.WeakSet["DagNode"]:
        return self._connections.outputs

    def get_owner(self) -> Optional["ChunkNoticePropagator"]:
        if owner := self._connections.owner:
            return owner()
        return None

    def get_owner_node(self) -> Optional["DagNode"]:
        if owner := self.get_owner():
            return owner.get_dag_node()
        return None

    def should_solve(self) -> bool:
        if not self.dirty:
            return False

        if self.get_outputs() or (self._callback and self._callback()) or self.get_owner_node():
            return True
        return False

    def dirty_dependents(self):
        for node in self.get_outputs():
            if node.dirty:
                continue
            node.dirty = True

        if (owner := self.get_owner_node()) and not owner.dirty:
            owner.dirty = True

    def is_inputs_dirty(self) -> bool:
        return any([x.dirty for x in self.get_inputs()])

    def solve_self(
        self, notice: Optional[ChunkNotice], sender: Any, propagated: Optional["ChunkNoticePropagator"] = None
    ) -> Optional[ChunkNotice]:

        if not self.should_solve():
            return

        if self.is_inputs_dirty():
            return self.solve_inputs()

        my_notice = None
        self.dirty = False

        if self._callback:
            if not (cb := self._callback()):
                raise ReferenceError("Callback of DagNode is dead")
            try:
                my_notice = cb(notice, sender, propagated)
            except Exception as e:
                self.dirty = True
                raise e

        if not my_notice:
            return notice

        msg = f"Solved DagNode: {self.name}: payload: {my_notice.alias.name if my_notice else None}"
        print(msg)

        return my_notice

    def solve_inputs(self) -> Optional[ChunkNotice]:
        dirty_inputs = False
        for node in filter(lambda x: x.dirty, self.get_inputs()):
            node.solve_inputs()
            dirty_inputs = True

        if not dirty_inputs:
            return self.solve_outputs(None, None)

    def solve_outputs(
        self, notice: Optional[ChunkNotice], sender: Any, propagated: Optional["ChunkNoticePropagator"] = None
    ) -> Optional[ChunkNotice]:
        if not (my_notice := self.solve_self(notice, sender, propagated)):
            return notice

        for o in self.get_outputs():
            o.solve_outputs(my_notice, my_notice.data, propagated)

        if node := self.get_owner_node():
            node.solve_outputs(my_notice, my_notice.data, propagated)
        return my_notice


class ChunkNoticePropagator:
    """
    The base class for managing chunk hierarchies as well as implementing the notice system used by chunks.
    A ChunkNoticePropagator stores the connections created with add_connection or add_callback, and implements
    the logic to propagate the notices to the owning chunk objects.

    Instead of having owners registering listeners for the changes of its children, it's much simpler to work the other
    way araound, and have the children reference the owning object, and call data_changed() on the owner when the child has been
    modified.

    By default, the send_self() is called whenever an attribute or child attribute has been modified. The notice that is sent is
    determined by the return value of get_notice_alias(). This notice is then also propagated up the owner hierarchy

    The majority of the logic is implemented in send_self() and data_changed().

    get_edit_context(), get_layer_identifier() and get_stage() can be implemented to support layer contexts when modifying the stage.
    Any NoticeConnection will intercept the connected notices and set the edit context to the context (Sdf.Layer) returned by get_edit_context()
    before running the actual callback function. This is to safely run callbacks and contain them to the layers managed by ChunkLayerManager.
    See NoticeConnection.handle_callback for more info
    """

    _last_notice: Optional[Tf.Notice]
    _last_sender: Optional[weakref.ReferenceType]
    _callbacks: List["NoticeConnection"]
    _dag_node: Optional[DagNode]

    def __init__(self, *args, **kwargs) -> None:
        self._last_notice = None
        self._last_sender = None
        self._callbacks = []
        self._dag_node = None

        # self._execution_function: Optional[
        #     Callable[["ChunkNoticePropagator", Tuple["ChunkNoticePropagator", ...]], Any]
        # ] = None

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
        return False

    def iter_changes(self) -> Generator[Tuple[Tf.Notice | None, Optional["ChunkNoticePropagator"]], None, None]:
        """
        Returns a generator for iterating over the notices that lead up to the last change of this object

        Yields:
            Generator[Tuple[Tf.Notice | None, Optional["ChunkNoticePropagator"]], None, None]: The generator to loop over
        """
        ln, ls = self.get_last_change()

        if not ls or ls is self:
            return

        yield (ln, ls)
        if isinstance(ls, ChunkNoticePropagator):
            yield from ls.iter_changes()

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

        The ChunkNoticePropagator does nothing with the owner except propagating notices from the child

        Args:
            owner (ChunkNoticePropagator | None): The owning (parent) object that should recieve notices when this instance is modified
        """
        self.get_dag_node().set_owner(owner)

    def data_changed(
        self, notice: Optional[ChunkNotice], sender: Any, propagated: Optional["ChunkNoticePropagator"] = None
    ) -> Optional[ChunkNotice]:
        """A callback which, by default, is only called when attributes or children are modified. Subclasses can connect custom
        notices to this function in order to propagate the signals to listeners of this instance, f.e. ChunkParam changes inside a
        ChunkParamGroup will also call this function on the group

        Args:
            notice (chunk_notice.InterfaceChangeNotice): The notice object which was sent
            sender (Any): The python object which was used as sender for the notice
            propagated (ChunkParamBase): The propagated sender. The sender that triggered the change
        """

        if self.is_blocked():
            return None

        self._last_notice = notice
        self._last_sender = None if sender is None else weakref.ref(sender)

        if not (my_notice := self.send_self()):
            return notice

        return my_notice

    def send_self(self, alias: Optional[ChunkNoticeAlias] = None, data: Any = None) -> Optional[ChunkNotice]:
        """Sends a Tf.Notice, with self as sender. The type of notice is determined by get_notice_alias(), or optionally the alias argument.

        Args:
            alias (Optional[ChunkNoticeAlias]): An optional alias to send. If alias is not None, it will be used instead of the default from get_notice_alias()
        """
        if self.is_blocked():
            return None

        if alias is None:
            alias = self.get_notice_alias()

        if not alias:
            return None

        notice_data = data if data is not None else self.get_notice_data()
        notice = send_notice(alias, self, notice_data)

        return notice

    def get_last_change(self) -> Tuple[Tf.Notice | None, Any | None]:
        """Retrieve the latest change on this chunk object.

        Returns:
            Tuple[Tf.Notice, ChunkParamBase]: Last changed ChunkParam or ChunkParamGroup and the notice associated with the change
        """

        return self._last_notice, None if self._last_sender is None else self._last_sender()

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
        self._callbacks.append(conn)

    def get_dag_node(self) -> DagNode:
        if not self._dag_node:
            self._dag_node = DagNode(self.data_changed)
            self._dag_node.name = self.name if hasattr(self, "name") else self.__class__.__name__

        return self._dag_node

    def add_input(self, input: "ChunkNoticePropagator"):
        self.get_dag_node().add_input(input.get_dag_node())

    def get_input(self, index: int = 0) -> Optional["ChunkNoticePropagator"]:
        inputs = list(self.get_dag_node().get_inputs())
        try:
            return inputs[index].get_owner()
        except IndexError:
            return None

    def evaluate(self, value: Any) -> bool:
        self.get_dag_node().solve_inputs()
        return value

    def get_callbacks(self) -> List["NoticeConnection"]:
        return self._callbacks

    def find_connections(
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
        meth = self.get_method()
        if not meth:
            return

        if self.owner:
            # Try running the callback function with the edit context to safely add any changes to the stage
            # to the correct layer
            stage = self.owner.get_stage()
            ctx = self.owner.get_edit_context()

            if stage and ctx and context:
                with Usd.EditContext(stage, ctx):
                    if not inspect.ismethod(meth):
                        meth(self.owner, notice, sender)
                    else:
                        meth(notice, sender)
                return

        if self.owner and not inspect.ismethod(meth):
            meth(self.owner, notice, sender)
        else:
            meth(notice, sender)

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
            print("Could not find appropriate notice to connect")
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
