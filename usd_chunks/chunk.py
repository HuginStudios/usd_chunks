from enum import IntFlag, auto
from typing import List, Any, Callable, Tuple, TypeVar, Generator
from abc import abstractmethod
import copy
import uuid

from functools import partial
from itertools import chain

from pxr import Tf
from . import chunk_notice


class ChunkLayoutFlags(IntFlag):
    HORIZONTAL = 1
    VERTICAL = auto()


class ChunkParamType(IntFlag):
    # Flags for ChunkParam for describing the type of parameter

    # Use 8 bit padding for types. This leaves room for 8 widget variants per type
    NONE = 1 << 3
    FLOAT = auto()
    STRING = auto()
    INT = auto()
    BOOL = auto()
    CUSTOM = auto()  # use self.custom_widget as widget

    # Flags for standard widgets
    # A separator line. Cant be combined and will not have a value
    SEPARATOR = NONE | 1 << 0

    # Real
    FLOAT_SLIDER = FLOAT | (1 << 0)
    FLOAT_SPINBOX = FLOAT | (1 << 1)

    # Int
    INT_SLIDER = INT | (1 << 0)
    INT_SPINBOX = INT | (1 << 1)
    INT_MENU = INT | (1 << 2)

    # String
    LABEL = STRING | (1 << 0)
    LINEEDIT = STRING | (1 << 1)
    FILE = STRING | (1 << 2)
    STRING_MENU = STRING | (1 << 3)

    # Bool
    CHECKBOX = BOOL | (1 << 0)
    BUTTON = BOOL | (1 << 1)


class ChunkGroupFlags(IntFlag):
    NONE = 0
    FILLED = auto()  # Fill the background of the group
    LABELED = auto()  # Add a label on the top of the group
    LABELED_ITEMS = auto()  # Add a label to each item
    COLLAPSABLE = auto()  # Collapsable group


class ChunkParamBase:
    def __init__(self):
        """Base class for Chunk parameters and groups"""
        self.name: str = None
        self.tooltip: str = None

        # Orientation of the widget, if relevant
        self.layout_flags: ChunkLayoutFlags = ChunkLayoutFlags.VERTICAL

        # Store NoticeConnection objects, these hold information about a connection to a callback function.
        self._connections: List[chunk_notice.NoticeConnection] = []

        self._uuid = None
        self._update_uuid()

        # A parameter might have a parent group. Storing a reference to the parent makes it easier to traverse the parameter tree
        self._parent: "ChunkParamGroup" = None

    def __hash__(self):
        return hash((self.name, self._uuid))

    def __eq__(self, value):
        if isinstance(value, ChunkParamBase):
            return self.name == value.name and self._uuid == value._uuid
        return False

    def _ignore_copy_attr(self, name: str) -> Tuple[bool, Any]:
        """Return a tuple of (bool, Any) for named attribute. Used to determine
        what attributes should be copied when attempting to copy this object. A subclass should implement this
        function rather than __copy__ in order to control what arguments should be copied

        Args:
            name (str): A named attribute on this object. Called for each attribute in order to see if it should copy

        Returns:
            Tuple[bool, Any]: A tuple for (ignoreCopy, defaultValue). If (True, []), the named attribute will not be copied, instead will be set to []
        """
        if name == "_connections":
            return True, []
        return False, None

    def __copy__(self):
        # Copy all data except members specified in _ignore_copy_attr
        data = {}
        for k, v in self.__dict__.items():
            ignore, val = self._ignore_copy_attr(k)
            if not ignore:
                data[k] = v
            else:
                data[k] = val

        cls = self.__class__
        result = cls.__new__(cls)
        result.__dict__.update(data)
        result._update_uuid()

        # Add connections to the copy by creating new NoticeConnection objects
        for conn in self._connections:
            sender = None
            if conn.sender == self:
                # The connection need to be rewired to the copy
                sender = result

            new_conn = chunk_notice.NoticeConnection(conn.method, conn.notice_type, sender, conn.globally, result)
            result.add_connection(new_conn)
        return result

    def get_parent(self) -> "ChunkParamGroup":
        """Get parent. A ChunkParam might be added to a group, this would return the group holding this param

        Returns:
            ChunkParamGroup: The parent ChunkParamGroup, or None if no parent is set
        """
        return self._parent

    def add_connection(self, conn: chunk_notice.NoticeConnection):
        """Adds a NoticeConnection to the self._connections list to store it for the lifetime of this object
        Used for callbacks in subclasses.

        Args:
            conn (chunk_notice.NoticeConnection): A NoticeConnection object to add
        """
        self._connections.append(conn)

    def find_connections(
        self, method: Callable = None, sender: Any = None, notice_type: chunk_notice.NoticeType = None
    ) -> List[chunk_notice.NoticeConnection]:
        """Find a NoticeConnection object stored on self. Filter the search using the arguments, f.e. if method is passed,
        will look for NoticeConnection objects where connection.method == method

        Args:
            method (Callable, optional): Filter by method on connections. Defaults to None.
            sender (Any, optional): Filter by sender. Defaults to None.
            notice_type (chunk_notice.NoticeType, optional): Filter by NoticeType. Defaults to None.

        Returns:
            List[chunk_notice.NoticeConnection]: A list of found connections matching the arguments
        """
        search_filter = {}

        if method is not None:
            search_filter["method"] = method
        if sender is not None:
            search_filter["sender"] = sender
        if notice_type is not None:
            search_filter["notice_type"] = notice_type

        result = []
        for conn in self._connections:
            found_match = True

            for key, value in search_filter.items():
                if conn.__getattribute__(key) != value:
                    found_match = False
                    break

            if found_match:
                result.append(conn)

        return result

    def add_callback(
        self,
        method: Callable,
        sender: Any = None,
        listen_globally: bool = False,
        notice_type: chunk_notice.NoticeType = None,
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
        if isinstance(sender, ChunkParamBase) and notice_type is None:
            notice_type = sender.get_defualt_notice_type()

        conn = chunk_notice.NoticeConnection(method, notice_type, sender, listen_globally, self)
        self.add_connection(conn)

    @abstractmethod
    def get_defualt_notice_type(self) -> chunk_notice.NoticeType:
        """Return the default notice type for when add_callback() is called without notice_type argument. Should be overwritten in sub-classes

        Returns:
            chunk_notice.NoticeType: The default notice type to send for callbacks
        """
        return chunk_notice.NoticeType.NONE

    def _update_uuid(self):
        self._uuid: str = uuid.uuid4().hex


class ChunkParam(ChunkParamBase):
    def __init__(self):
        super().__init__()

        # Flags describing the type of this parameter or group
        self.param_type: ChunkParamType = ChunkParamType.NONE
        self.name: str = "param"

        # Explicit label (nice name). Will be derived from name if not set
        self.label: str = None

        # Used by COMBOBOX for setting menu items
        self.menu_items: List[str] = []

        # For numerical values
        self.min_value = None
        self.max_value = None

        # Custom QtWidgets.QWidget instance for when using CUSTOM type
        self.custom_widget = None

        # Private members. Value is should be accessed through self.value rather than self._value. Same for default_value
        self._value: Any = None
        self._default_value: Any = None

    @property
    def value(self):
        # Value of this parameter - type depends on self.param_type
        return self._value

    @value.setter
    def value(self, val: Any):
        # Setter for value. Will send the ParamChangeNotice if value was updated
        if self.value != val:
            self._value = val
            chunk_notice.ParamChangeNotice().Send(self)

    @property
    def default_value(self):
        if self._default_value is None:
            if ChunkParamType.STRING_MENU | ChunkParamType.INT_MENU in self.param_type:
                return self.menu_items[0]

            if ChunkParamType.INT in self.param_type:
                self._default_value = 0
            elif ChunkParamType.FLOAT in self.param_type:
                self._default_value = 0
            elif ChunkParamType.STRING in self.param_type:
                self._default_value = ""
            elif ChunkParamType.BOOL in self.param_type:
                self._default_value = False
            else:
                self.default_value = 0
        return self._default_value

    @default_value.setter
    def default_value(self, val):
        self._default_value = val

    def get_defualt_notice_type(self):
        return chunk_notice.NoticeType.PARAM_CHANGED


TChunkParamGroup = TypeVar("TChunkParamGroup", bound="ChunkParamGroup")


class ChunkParamGroup(ChunkParamBase):
    def __init__(self):
        """ChunkParamGroup is a container of ChunkParam and other ChunkParamGroup objects"""
        super().__init__()

        self.label: str = None
        self.name: str = "group"
        self.group_flags: ChunkGroupFlags = ChunkGroupFlags.NONE

        self._last_notice: Tf.Notice = None
        self._last_sender: ChunkParamBase = None

        # Parameters and nested groups
        self._params: List[ChunkParam] = []
        self._groups: List[ChunkParamGroup] = []

    def _ignore_copy_attr(self, name) -> Tuple[str, Any]:
        if name == "_params" or name == "_groups":
            return True, []
        if name == "_last_notice" or name == "_last_sender":
            return True, None
        return super()._ignore_copy_attr(name)

    def add_params(self, params: List[ChunkParamBase], force_unique=True):
        """Adds parameters to this group. Will call add_param on all params in the list

        Args:
            params (List[ChunkParamBase]): List of ChunkParam or ChunkParamGroup to add
        """
        for param in params:
            self.add_param(param, force_unique)

    def add_param(self, param: ChunkParamBase, force_unique=True):
        """Adds a parameter to this group. Connects the parameter change notices to also emit this group changed notice

        Args:
            param (ChunkParamBase): A parameter to add. Can also be another ChunkParamGroup, which will nest the group
        """

        new_param = None
        if force_unique:
            # Check and resolve duplicate params
            new_param = self.resolve_new_param_or_group(param)

        if new_param is None:
            # Param was not a duplicate, so can use the one passed by caller
            new_param = param

        conn = None
        if isinstance(new_param, ChunkParamGroup):
            conn = chunk_notice.NoticeConnection(self._group_changed, chunk_notice.NoticeType.GROUP_CHANGED, new_param, chunk_item=self)
            self._groups.append(new_param)

        elif isinstance(new_param, ChunkParam):
            conn = chunk_notice.NoticeConnection(self._group_changed, chunk_notice.NoticeType.PARAM_CHANGED, new_param, chunk_item=self)
            self._params.append(new_param)

        if param.get_parent() is None:
            new_param._parent = self

        if conn:
            self.add_connection(conn)

    def resolve_new_param_or_group(self, param: ChunkParamBase) -> ChunkParamBase:
        """Before adding a new param or group, need to validate that the same instance of the param is not already
        added to this group. We also need to make sure there are no duplicate names.
        If a duplicate instance is found, this function will copy or swap the paramaeter/group with
        a valid instance. The valid instance will hava a new name and uuid

        Args:
            param (ChunkParamBase): A parameter or group that should be added to this group

        Returns:
            ChunkParamBase: The originally passed param if no duplication was found, otherwise an altered param
        """

        # TODO: Currently kinda nasty lookups to achieve this - consider caching in the future, or
        # rethink some of this...

        reserved_names = self.get_reserved_names()
        all_params = list(self.iter_all_params())

        # Need to resolve duplicate params and groups differently, since a group will likely have nested params,
        # We need to search for those as well when adding a group
        if isinstance(param, ChunkParam):
            if param in all_params:
                # Param exists on this group already, return a copy of the new param
                return self.copy_group_or_param(param)

        elif isinstance(param, ChunkParamGroup):
            all_groups = list(self.iter_all_groups())

            if param in all_groups:
                # This is a duplicate group. Copy the entire group to resolve conflicts
                return self.copy_group_or_param(param)

            # Need to resolve any nested parameters inside the new group
            for p in param.iter_all_params():
                if p in all_params:
                    parent = p.get_parent()
                    if parent:
                        new_p = self.copy_group_or_param(p)
                        reserved_names.append(new_p.name)
                        parent.replace_param(p, new_p)

                elif p.name in reserved_names:
                    # Did not find param with the same uuid, but duplicate names. Only resolve the name
                    p.name = self.get_valid_child_name(p, reserved_names)

            # Also resolve nested groups
            for g in param.iter_all_groups():
                if g in all_groups:
                    parent = g.get_parent()
                    if parent:
                        new_g = self.copy_group_or_param(g)
                        reserved_names.append(new_g.name)
                        parent.replace_param(g, new_g)

                elif g.name in reserved_names:
                    # Did not find param with the same uuid, but duplicate names. Only resolve the name
                    g.name = self.get_valid_child_name(g, reserved_names)

        if param.name in reserved_names:
            # The new group is not a duplicate but shares name with an already added group
            param.name = self.get_valid_child_name(param, reserved_names)

        return param

    def copy_group_or_param(self, param: ChunkParamBase, reserved_names: List[str] = None) -> ChunkParamBase:
        """Return a copy of a param or group that does not conflict with other parameters or groups on this group

        Args:
            param (ChunkParamBase): A ChunkParam or ChunkParamGroup object to copy
            reserved_names (List[str], optional): A list of reserved names which the copy will respect. Defaults to None.

        Returns:
            ChunkParamBase: The copy of param with new name and updated connections
        """
        if reserved_names is None:
            reserved_names = self.get_reserved_names()

        new_name = self.get_valid_child_name(param, reserved_names)
        new_param = copy.copy(param)
        new_param.name = new_name

        if isinstance(param, ChunkParamGroup):
            # Recorsively copy everything inside the group
            for p in chain(param.get_groups(), param.get_params()):
                p_copy = self.copy_group_or_param(p, reserved_names)

                # Since the copying of a group will not copy any nested params or groups, it's safe to add them back here
                new_param.add_param(p_copy)

        return new_param

    def replace_param(self, old_param: ChunkParam, new_param: ChunkParam):
        """Replace a parameter on this group with another parameter

        Args:
            old_param (ChunkParam): The old param to replace
            new_param (ChunkParam): The new param to replace old_param with
        """
        try:
            idx = self._params.index(old_param)
        except ValueError:
            print("Trying to swap a parameter that does not exist on this group")
            return

        # Set self as parent
        new_param._parent = self

        # Do the swap
        self._params[idx] = new_param

        # Need to update connections
        conns = self.find_connections(sender=old_param)
        for conn in conns:
            conn.connect(sender=self._params[idx])

    def replace_group(self, old_group: "ChunkParamGroup", new_group: "ChunkParamGroup"):
        """Replace a nested group on this group with a new group

        Args:
            old_group (ChunkParam): The old group to replace
            new_group (ChunkParam): The new group to replace old_group with
        """
        try:
            idx = self._groups.index(old_group)
        except ValueError:
            print("Trying to swap a group that does not exist on this group")
            return

        # Set self as parent
        new_group._parent = self

        # Do the swap
        self._groups[idx] = new_group

        # Need to update connections
        conns = self.find_connections(sender=old_group)
        for conn in conns:
            conn.connect(sender=self._groups[idx])

    def get_param(self, param: ChunkParam | str) -> ChunkParam | None:
        """Get a parameter or group inside of this group

        Args:
            param (ChunkParamBase | str): A parameter to find. If string is passed, look for param with name

        Returns:
            ChunkParam: The found parameter or None if not found
        """

        if isinstance(param, ChunkParam):
            return next((i for i in self.get_params() if i == param), None)
        if isinstance(param, str):
            return next((i for i in self.get_params() if i.name == param), None)

    def find_param(self, param: ChunkParam | str) -> ChunkParam:
        """Similar to get_param, but searches in nested groups

        Args:
            param (ChunkParam | str): A parameter or group to find. If string is passed, look for params with name

        Returns:
            ChunkParam: The found parameter or None if not found
        """
        if isinstance(param, ChunkParam):
            return next((i for i in self.iter_all_params() if i == param), None)
        if isinstance(param, str):
            return next((i for i in self.iter_all_params() if i.name == param), None)

    def get_params(self) -> List[ChunkParam]:
        """Get all parameters in this group

        Returns:
            List[ChunkParamBase]: All parameters in this group
        """
        return self._params

    def iter_all_params(self) -> Generator[ChunkParam, None, None]:
        """Get all parameters in this group and in nested groups

        Yields:
            Generator[ChunkParam]: A Generator of all parameters, including from nested groups, contained within this group
        """
        for p in self.get_params():
            yield p

        for group in self._groups:
            yield from group.iter_all_params()

    def get_group(self, group: TChunkParamGroup | str) -> TChunkParamGroup:
        """Get a group inside of this group

        Args:
            group (ChunkParamGroup | str): A group to find. If string is passed, look for group with name

        Returns:
            ChunkParamGroup: The found group or None if not found
        """
        if isinstance(group, ChunkParamGroup):
            return next((i for i in self.get_groups() if i == group), None)
        if isinstance(group, str):
            return next((i for i in self.get_groups() if i.name == group), None)

    def find_group(self, group: TChunkParamGroup | str) -> "ChunkParamGroup":
        """Similar to get_group, but also searches in nested groups

        Args:
            group (ChunkParamGroup | str): A group to find. If string is passed, look for group with name

        Returns:
            ChunkParamGroup: The found group or None if not found
        """

        if isinstance(group, ChunkParamGroup):
            return next((i for i in self.iter_all_groups() if i == group), None)
        if isinstance(group, str):
            return next((i for i in self.iter_all_groups() if i.name == group), None)

    def get_groups(self) -> List["ChunkParamGroup"]:
        """Get all groups on this group, excluding nested

        Returns:
            List[ChunkParamGroup]: Groups contained in this group. Not including nested groups
        """
        return self._groups

    def iter_all_groups(self) -> Generator["ChunkParamGroup", None, None]:
        """Get all groups in this group including nested groups

        Yields:
            Generator[ChunkParamGroup]: All groups, including from nested groups, in this group
        """

        for g in self.get_groups():
            yield g

        for group in self._groups:
            yield from group.iter_all_groups()

    def get_last_change(self) -> Tuple[Tf.Notice, ChunkParamBase]:
        """Retrieve the latest changes on the group. Can include changes to nested groups

        Returns:
            Tuple[Tf.Notice, ChunkParamBase]: Last changed ChunkParam or ChunkParamGroup and the notice associated with the change
        """
        return self._last_notice, self._last_sender

    def get_last_param_change(self) -> Tuple[Tf.Notice, ChunkParam]:
        """Retreive the last changed parameter in this group. Will look inside nested groups as well to find parameter changes

        Returns:
            Tuple[Tf.Notice, ChunkParam]: Last changed ChunkParam and the notice associated with the change on this group or in nested group
        """
        notice, sender = self.get_last_change()
        while isinstance(sender, ChunkParamGroup):
            notice, sender = sender.get_last_change()
        return notice, sender

    def get_valid_child_name(self, param: ChunkParamBase, reserved_names: List[str]) -> str:
        """Get a unique name for param or group to be added on this group

        Args:
            param (ChunkParam): a name that might not be unique
            reserved_names (List[str]): A list of reserved names. This paramater can't have a name which exist in this list. Used when trying to nest a group inside an already created group

        Returns:
            str: a unique name
        """

        orig_name = param.name

        suffix = ""
        while orig_name[-1].isdigit():
            suffix = suffix + orig_name[-1]
            orig_name = orig_name[:-1]

        idx = int(suffix) if suffix else 1
        max_iter = 100

        name = orig_name + suffix

        while name in reserved_names:
            name = orig_name + str(idx)
            idx += 1
            if idx > max_iter:
                raise RuntimeError(f"Failed to resolve name, loop exceeded {max_iter} iterations")

        return name

    def get_reserved_param_names(self) -> List[str]:
        """Get a list of parameter names on this and nested groups. Any new parameter should not have a duplicate name

        Returns:
            List[str]: The reserved parameter names on this group
        """
        return [x.name for x in self.iter_all_params()]

    def get_reserved_group_names(self):
        """Get a list of group names on this and nested groups. Any new group should not have a duplicate name

        Returns:
            List[str]: The reserved group names on this group
        """
        return [x.name for x in self.iter_all_groups()] + [self.name]

    def get_reserved_names(self):
        """A combination of reserved param and group names

        Returns:
            List[str]: A list of reserved group and parameter names which new parameters should not be named
        """
        return list(chain(self.get_reserved_group_names(), self.get_reserved_param_names()))

    def get_defualt_notice_type(self):
        return chunk_notice.NoticeType.GROUP_CHANGED

    def _group_changed(self, _, notice: Tf.Notice, sender: Any):
        """Called whenever any ChunkParam or nested ChunkParamGroup has changed. Will emit the GroupChangeNotice for this group

        Args:
            notice (Tf.Notice): The notice that was sent by the change
            sender (Any): Usually the changed ChunkParam or nested ChunkParamGroup, but can be other types as well
        """
        self._last_notice = notice
        self._last_sender = sender

        chunk_notice.GroupChangeNotice().Send(self)


class Chunk(ChunkParamGroup):
    def __init__(self):
        """A Chunk is a container for parameters and callbacks. Chunk, ChunkParam and ChunkParamGroup can all be used together
        to create a parameter interface that is frontend agnostic. Bindings to Qt can be found in chunk_qt.py.

        Callbacks are added using the add_callback function to any Chunk, ChunkParamGroup or ChunkParam

        Creating a simple Chunk with a group which holds a slider param can be done like so:
        ```
            slider = chunk.ChunkParam()
            slider.param_type = chunk.ChunkParamType.FLOAT_SLIDER
            slider.name = "slider"
            slider.add_callback(aMethod)

            button = chunk.ChunkParam()
            button.param_type = chunk.ChunkParamType.BUTTON
            button.name = "button"

            group = chunk.ChunkParamGroup()
            group.layout_flags = chunk.ChunkLayoutFlags.HORIZONTAL
            group.add_params([slider])

            chunkInfo = chunk.Chunk()
            chunkInfo.name = "Test Chunk"
            chunkInfo.category = "Category"

            chunkInfo.add_params([button, group])

        ```
        Any changes to a Chunk (any ChunkParam or ChunkParamGroup children changes) will emit the chunk_notice.ChunkChangeNotice (chunk_notice.NoticeType.CHUNK_CHANGED)
        and can be used to listen to any changes on a Chunk.
        """
        super().__init__()

        self.category: str = "General"
        self.icon: str = None  # Path to icon
        self.animation_duration: float = 300

        # Overriding this will change the default widget factory when creating widgets from this chunk.
        # Leaving it as None will use the default chunk_widgets.ChunkParamWidgetFactory
        self.widget_factory = None

        # Connect the GROUP_CHANGED notice to also emit CHUNK_CHANGED. GROUP_CHANGED is handled in the ChunkParamGroup baseclass
        # and is emitted whenever a param in the group is updated
        self._group_listener = chunk_notice.NoticeConnection(
            self._chunk_changed, chunk_notice.NoticeType.GROUP_CHANGED, self, chunk_item=self
        )

    def get_defualt_notice_type(self):
        return chunk_notice.NoticeType.CHUNK_CHANGED

    def _chunk_changed(self, _, notice: Tf.Notice, sender: Any):
        """Called whenever any ChunkParam has been changed. Will emit the ChunkChangeNotice

        Args:
            notice (Tf.Notice): The notice that was sent by the change
            sender (Any): Usually the changed ChunkParam, but can be other types as well
        """

        chunk_notice.ChunkChangeNotice().Send(self)
