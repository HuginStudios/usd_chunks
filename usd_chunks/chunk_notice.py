from pxr import Tf

# from enum import IntEnum, auto
from enum import IntEnum, auto
from typing import Callable, Any, List, Type, Tuple, Dict, Optional, Generator, TypeVar, Generic, overload
from pxr.Usdviewq import usdviewApi as UsdviewModule
from pxr import Usd
from functools import partial
from abc import ABC, abstractmethod


class ChunkNoticeAlias(IntEnum):
    # Chunk parameters and value notices
    NONE = 0
    PARAM_CHANGED = auto()  # Sent when a param changes
    GROUP_CHANGED = auto()  # Sent when a group changes. This includes nested groups and param changes
    CHUNK_CHANGED = auto()  # Similar to GROUP_CHANGED but only for chunks

    CHUNK_DELETE = auto()  # A chunk object was deleted

    # Notice sent whenever the value of a valued parameter changes
    ATTRIBUTE_VALUE_CHANGED = auto()

    # Default notice for attributes that does not set an alias excplicitly
    ATTRIBUTE_CHANGED = auto()

    # Sent when the flags attribute changes
    FLAGS_CHANGED = auto()
    # Sent when the state attribute changes
    STATE_CHANGED = auto()

    # Stage notices
    PRIM_SELECTION_CHANGED = auto()
    PROP_SELECTION_CHANGED = auto()
    OBJECT_CHANGED = auto()  # Something was changed on the stage

    # Registry notices
    CHUNK_LOADED = auto()  # Sent when a new chunk has beed added to the registry
    CHUNK_UNLOADED = auto()  # Sent when a chunk was unloaded

    CUSTOM = 200  # Custom notices should be registered with aliases > CUSTOM


class RegistrySingleton(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super(RegistrySingleton, cls).__call__(*args, **kwargs)
        return cls._instances[cls]


class ChunkNoticeRegistry(metaclass=RegistrySingleton):
    registered: Dict[int, type[Tf.Notice]] = {}

    @classmethod
    def get_from_alias(cls, alias: ChunkNoticeAlias) -> Tf.Notice:
        notice = cls.registered.get(alias.value)
        if notice is None:
            raise Tf.ErrorException(f"No notice found for alias {alias}")
        return notice()

    @classmethod
    def register(cls, alias: ChunkNoticeAlias, notice_class: Type[Tf.Notice]):
        if alias not in cls.registered:
            cls.registered[alias.value] = notice_class


def chunk_notice(alias: ChunkNoticeAlias):
    """Used as decorator for Tf.Notice object who should be registered by the ChunkNoticeRegistry. This maps the alias to the decorated class,
    so a user can do get_notice(alias).Send()

    Args:
        alias (ChunkNoticeAlias): The alias which maps to the new Tf.Notice class
    """

    def decorator(cls: Type["Tf.Notice"]):
        ChunkNoticeRegistry().register(alias, cls)
        Tf.Type.Define(cls)

    return decorator


@chunk_notice(ChunkNoticeAlias.PARAM_CHANGED)
class ParamChangeNotice(Tf.Notice):
    pass


@chunk_notice(ChunkNoticeAlias.GROUP_CHANGED)
class GroupChangeNotice(Tf.Notice):
    pass


@chunk_notice(ChunkNoticeAlias.CHUNK_CHANGED)
class ChunkChangeNotice(Tf.Notice):
    pass


@chunk_notice(ChunkNoticeAlias.CHUNK_DELETE)
class ChunkDeletedNotice(Tf.Notice):
    pass


@chunk_notice(ChunkNoticeAlias.ATTRIBUTE_VALUE_CHANGED)
class AttributeValueChangeNotice(Tf.Notice):
    pass


@chunk_notice(ChunkNoticeAlias.ATTRIBUTE_CHANGED)
class AttributeChangeNotice(Tf.Notice):
    pass


@chunk_notice(ChunkNoticeAlias.FLAGS_CHANGED)
class AttributeFlagsChangeNotice(Tf.Notice):
    pass


@chunk_notice(ChunkNoticeAlias.STATE_CHANGED)
class AttributeStateChangeNotice(Tf.Notice):
    pass


@chunk_notice(ChunkNoticeAlias.CHUNK_LOADED)
class ChunkLoadedNotice(Tf.Notice):
    pass


@chunk_notice(ChunkNoticeAlias.CHUNK_UNLOADED)
class ChunkUnLoadedNotice(Tf.Notice):
    pass


@chunk_notice(ChunkNoticeAlias.PRIM_SELECTION_CHANGED)
class PrimSelectionChangedNotice(Tf.Notice):
    pass


@chunk_notice(ChunkNoticeAlias.PROP_SELECTION_CHANGED)
class PropSelectionChangedNotice(Tf.Notice):
    pass


# Register externally manages notice just to map to the custom enum
ChunkNoticeRegistry().register(ChunkNoticeAlias.OBJECT_CHANGED, Usd.Notice.ObjectsChanged)


def get_notice(alias: ChunkNoticeAlias) -> Tf.Notice:
    """Return a class of type Tf.Notice for the specified ChunkNoticeAlias

    Args:
        notice_type (ChunkNoticeAlias): The ChunkNoticeAlias enum to get a Tf.Notice class from

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
    # return isinstance(notice, typ(get_notice(alias))
    return type(notice) is type(get_notice(alias))


def send_notice(alias: ChunkNoticeAlias, sender: Any = None):
    notice_class = get_notice(alias)
    if not notice_class:
        return

    if sender is None:
        notice_class.SendGlobally()
    else:
        notice_class.Send(sender)


class ChunkNoticePropagator:
    _last_notice: Optional[Tf.Notice]
    _last_sender: Optional[Any]
    _connections: List["NoticeConnection"]
    _owner: Optional["ChunkNoticePropagator"]

    def __init__(self) -> None:
        self._last_notice = None
        self._last_sender = None
        self._connections = []
        self._owner = None

    def _copy_connections(self, src: "ChunkNoticePropagator"):
        # Add connections to the copy by creating new NoticeConnection objects
        for conn in src._connections:
            self._copy_connection(conn, src)

    def _copy_connection(self, conn: "NoticeConnection", src: "ChunkNoticePropagator"):
        # Add connections to the copy by creating new NoticeConnection objects
        # Rewire the connection to the copy if conn.sender is the source of the copy (self)
        sender = self if conn.sender == src else conn.sender

        new_conn = NoticeConnection(conn.method, conn.alias, sender, conn.globally, self)
        self.add_connection(new_conn)

    @abstractmethod
    def get_notice_alias(self) -> ChunkNoticeAlias:
        """Return the default notice type for when add_callback() is called without notice_type argument. Should be overwritten in sub-classes

        Returns:
            chunk_notice.NoticeType: The default notice type to send for callbacks
        """
        raise NotImplementedError

    def iter_changes(self) -> Generator[Tuple[Tf.Notice | None, "ChunkNoticePropagator"], None, None]:
        ln, ls = self.get_last_change()

        if not ls or ls is self:
            return

        yield (ln, ls)
        if isinstance(ls, ChunkNoticePropagator):
            yield from ls.iter_changes()

    def get_owner(self) -> Optional["ChunkNoticePropagator"]:
        """Get the owner of this object. A ChunkParam might be added to a group, this would return the group holding this param

        Returns:
            ChunkContainer: The parent ChunkContainer, or None
        """
        return self._owner

    def set_owner(self, owner: Optional["ChunkNoticePropagator"]):
        """Set the owner of this object. The owner is represents a "parent" in a parent-child relationship. This function
        should therefor be used once the parent have actually taken ownership of this object. I.e, this function should only
        be used to update the child when the relationship have changed.

        The ChunkNoticePropagator does nothing with the owner except propagating notices from the child

        Args:
            owner (ChunkNoticePropagator | None): The owning (parent) object that should recieve notices when this instance is modified
        """
        self._owner = owner

    def data_changed(self, chunk_item: Any, notice: Tf.Notice, sender: Any) -> Tf.Notice:
        """A callback which, by default, is only called when attributes or children are modified. Subclasses can connect custom
        notices to this function in order to propagate the signals to listeners of this instance, f.e. ChunkParam changes inside a
        ChunkParamGroup will also call this function on the group

        Args:
            chunk_item (ChunkParamBase): The chunk_item of the connection. Usually the same as sender
            notice (chunk_notice.InterfaceChangeNotice): The notice object which was sent
            sender (Any): The python object which was used as sender for the notice
        """

        self._last_notice = notice
        self._last_sender = sender

        my_notice = self.send_self()

        # Set owner data_changed if available
        if self._owner:
            self._owner.data_changed(self, my_notice, self)

        return my_notice

    def send_self(self) -> Tf.Notice:
        """Send a Tf.Notice of type default_notice_type, with self as sender"""
        notice = get_notice(self.get_notice_alias())
        notice.Send(self)
        return notice

    def get_last_change(self) -> Tuple[Tf.Notice | None, Any | None]:
        """Retrieve the latest change on this chunk object.

        Returns:
            Tuple[Tf.Notice, ChunkParamBase]: Last changed ChunkParam or ChunkParamGroup and the notice associated with the change
        """

        return self._last_notice, self._last_sender

    def add_connection(self, conn: "NoticeConnection"):
        """Adds a NoticeConnection to the self._connections list to store it for the lifetime of this object
        Used for callbacks in subclasses.

        Args:
            conn (chunk_notice.NoticeConnection): A NoticeConnection object to add
        """
        self._connections.append(conn)

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

        query = {"method": method, "sender": sender, "notice_type": notice_type}
        result = []
        for conn in self._connections:
            if any([query[y] is not None and getattr(conn, y) == query[y] for y in query]):
                result.append(conn)

        return result

    def add_callback(
        self,
        method: Callable,
        sender: Optional["ChunkNoticePropagator"] = None,
        listen_globally: bool = False,
        notice_type: ChunkNoticeAlias = ChunkNoticeAlias.NONE,
    ):
        """Subscribe to a notice. When the notice is sent, the suplpied method will be called with two arguments:
        (notice(Tf.Notice), sender(Any))

        If the sender argument is specified, the method will be called only when said sender sends a notice. If not, ans also listen_globally is False, then assume the sender is self

        If listen_globally = True, subscribe to all notices of the same type regardless of the sender. If listening to param changes globally f.e., then all param changes will trigger method,
        not just changes from a certain param or group

        Args:
            method (Callable): The method to call when the notice is sent
            sender (Any, optional): A sender which to subscribe to. If None, will use self as sender if listen_globally=False, else None. Defaults to None.
            listen_globally (bool, optional): Whether to subscribe to all events of type notice_type. Defaults to False.
            notice_type (NoticeType, optional): A specific type of notice to subscribe to. If left as None, will use the get_default_notice_type(). Defaults to None
        """

        # If sender is not set and we should not listen for global notices, the sender will be self
        if sender is None and not listen_globally:
            sender = self

        # If sender is specified, only subscribe to changes from it.
        if isinstance(sender, ChunkNoticePropagator) and notice_type is ChunkNoticeAlias.NONE:
            notice_type = sender.get_notice_alias()

        conn = NoticeConnection(method, notice_type, sender, listen_globally, self)
        self.add_connection(conn)


class NoticeConnection:
    def __init__(
        self,
        method: Callable,
        alias: ChunkNoticeAlias,
        sender: Optional[Any] = None,
        globally: bool = False,
        chunk_item=None,
    ):
        self.sender: Any = sender
        self.chunk_item: Any = chunk_item
        self.alias: ChunkNoticeAlias = alias

        self.listener: Optional[Tf.Notice.Listener] = None

        self.globally: bool = globally
        self.method: Callable = method

        if chunk_item is None:
            self.__method = self.method
        else:
            self.__method = partial(self.method, self.chunk_item)

        self.connect()

    def connect(self, sender=None):
        notice = get_notice(self.alias)
        if not notice:
            print("Could not find appropriate notice to connect")
            return None

        if self.listener is not None:
            self.disconnect()

        if sender is not None:
            self.sender = sender

        if self.sender is None or self.globally:
            self.listener = Tf.Notice.RegisterGlobally(type(notice), self.__method)
        elif self.sender:
            self.listener = Tf.Notice.Register(type(notice), self.__method, self.sender)

    def disconnect(self):
        if self.listener is not None:
            self.listener.Revoke()
            self.listener = None


class UsdviewNotice:
    def __init__(self, usdviewAPI: UsdviewModule.UsdviewApi):
        self.usdviewAPI: UsdviewModule.UsdviewApi = usdviewAPI

        # Usdview stubs are not up to date, so need to ignore typing
        data_model = self.usdviewAPI.dataModel

        selection_model = data_model.selection  # type: ignore
        selection_model.signalPrimSelectionChanged.connect(self._primSelectionChanged)
        selection_model.signalPropSelectionChanged.connect(self._propSelectionChanged)

    @property
    def selectedPrims(self) -> List[Usd.Prim]:
        return self.usdviewAPI.selectedPrims  # type: ignore

    @property
    def selectedProp(self) -> Usd.Property:
        return self.usdviewAPI.property  # type: ignore

    def _primSelectionChanged(self, *args, **kwargs):
        send_notice(ChunkNoticeAlias.PRIM_SELECTION_CHANGED, self)

    def _propSelectionChanged(self, *args, **kwargs):
        send_notice(ChunkNoticeAlias.PROP_SELECTION_CHANGED, self)
