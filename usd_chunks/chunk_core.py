"""
Defines all base classes which the chunks and associated classes should derive from. These classes should not
be instatiated, instead use the implementations from [usd_chunks.chunk](chunk.md).

We use the attrs library behind the scenes to get dataclass-like behaviour for all the Chunk classes in order to
easily create and manage attributes. The attributes and function definitions from attrs are inherited through the mro
so we don't need to define __init__ for each and every subclass, we can instead just define some fields and trust attrs
with building the right functions for us. Instead of using @attrs.define, we use AttributesMetaclass as metaclass for
all chunk objects. AttributesMetaclass wraps the classes around attrs.define with the correct settings, removing the need
of further interfering with attrs. AttributesMetaclass also add some custom validation and conversion behaviour.

All chunk objects inherits from ChunkCommon, which declare some fields that all chunk objects should have; name, f.e.

Every modifyable field are themselves instances of AttributeSender. As AttributeSender also inherits from ChunkNoticePropagator,
we can propagate the attribute changes up the hierarchy in order to send notices and similar when any attribute is modified.

"""

import copy
import inspect
import logging
import os
import re
import uuid
import weakref
from abc import abstractmethod
from collections import ChainMap
from enum import IntFlag, Enum
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    Generic,
    Iterable,
    List,
    Optional,
    Protocol,
    Set,
    Tuple,
    TypedDict,
    TypeVar,
    Union,
    Sequence,
    TypeGuard,
    overload,
    TYPE_CHECKING,
)

import attrs
from pxr import Sdf, Tf, Usd
from typing_extensions import Self, Required

from usd_chunks.chunk_notice import Predicate
from usd_chunks import ATTRS_VERSION, chunk_notice, chunk_registry, chunk_utils
from usd_chunks.chunk_flags import ChunkFlags

if TYPE_CHECKING:
    # SceneSender moved to chunk_scene, which imports this module -- so the reference
    # this file keeps in its annotations can only be a type-checking one.
    from usd_chunks.chunk_scene import SceneSender
# The attribute layer -- declared attributes, their metadata and change notification --
# lives in chunk_attributes. It is the mechanism; this file is the model that uses it.
from usd_chunks.chunk_gizmo_types import (  # noqa: F401  (re-exported)
    GIZMO_AXES, GIZMO_BRUSH_TYPE, GIZMO_DEFAULT_EDIT_MODE, GIZMO_EDIT_MODES,
    GIZMO_MANIPULATOR_TYPES, GIZMO_SHAPE_TYPES, GIZMO_TRANSFORM_ARITY,
    GIZMO_TRANSFORM_TYPE, GIZMO_TYPES, GizmoBase, check_gizmo_sugar,
)
from usd_chunks.chunk_attributes import (  # noqa: F401  (re-exported)
    AttributeMetadata,
    AttributeSender,
    AttributesBase,
    AttributesMetaclass,
    CopyMode,
    SendMode,
    add_validate_and_convert,
    get_attr_index,
)

# The widget contracts live in chunk_widget_api. Imported rather than defined here
# because ChunkParamBase annotates custom_widget with ChunkParamWidgetBase and attrs
# resolves that when the class is built -- the dependency is real, not stylistic.
from usd_chunks.chunk_widget_api import ChunkParamWidgetBase, ChunkWidgetFactoryBase



_ATTRS_SUPPORT_CONVERTER = ATTRS_VERSION >= (24, 1)  # type: ignore
_ATTRS_SUPPORT_INSPECT = ATTRS_VERSION >= (25, 4)  # type: ignore

logger = logging.getLogger(__name__)

ValueT = TypeVar("ValueT")
ParamT = TypeVar("ParamT", bound="ChunkParamBase")
GroupT = TypeVar("GroupT", bound="ChunkContainerBase")
ChunkT = TypeVar("ChunkT", bound="ChunkCommon")

################################
# BASE CLASSES FOR UI ELEMENTS #
################################


class SyntaxHighlightStyle(TypedDict, total=False):
    color: Required[str]
    italic: bool
    bold: bool


class SyntaxHighlightMapping:
    def __init__(self):
        self._mappings: Dict[str, SyntaxHighlightStyle] = {}

    def add_mapping(self, pattern, style: SyntaxHighlightStyle):
        self._mappings[pattern] = style

    def get_mappings(self):
        return self._mappings.items()

    def match_text(self, text: str) -> Generator[Tuple[int, int, SyntaxHighlightStyle], None, None]:
        for pattern, fmt in self._mappings.items():
            for m in re.finditer(pattern, text):
                start, end = m.span()
                yield start, end, fmt


class PySyntaxHighlightMapping(SyntaxHighlightMapping):
    def __init__(self) -> None:
        super().__init__()

        pattern = r"^\s*class\s+\w+\(.*$"
        self.add_mapping(pattern, SyntaxHighlightStyle(color="#4c6ea3"))

        pattern = r"^\s*def\s+\w+\s*\(.*\)\s*:\s*$"
        self.add_mapping(pattern, SyntaxHighlightStyle(color="#31a56b"))

        pattern = r"^\s*#.*$"
        self.add_mapping(pattern, SyntaxHighlightStyle(color="#3f883f"))


class UsdSyntaxHighlightMapping(SyntaxHighlightMapping):
    def __init__(self) -> None:
        super().__init__()

        pattern = r"^\s*class\s+\w+\(.*$"
        self.add_mapping(pattern, SyntaxHighlightStyle(color="#4c6ea3"))

        pattern = r"^\s*(def|over)"
        self.add_mapping(pattern, SyntaxHighlightStyle(color="#4a80ec"))

        pattern = r"^\s*#.*$"
        self.add_mapping(pattern, SyntaxHighlightStyle(color="#3f883f"))

        pattern = r"(?<=def )(\w+)"
        self.add_mapping(pattern, SyntaxHighlightStyle(color="#51c765"))

        pattern = r"(\"|\')\w+(\"|\')"
        self.add_mapping(pattern, SyntaxHighlightStyle(color="#e17a3f"))

        pattern = r"(double|float|int|uniform|asset|token|color)\w?.(\[\])?"
        self.add_mapping(pattern, SyntaxHighlightStyle(color="#4aabec"))




class ChunkContainerWidgetBase(Generic[GroupT]):
    """
    Chunk is a base class for creating UI representation of chunk containers.
    """

    def __init__(self, group: GroupT, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Hold a weakref to the group. If all other references to the group is killed, then
        # remove it from the UI as well, as the ui should not take ownership of the group
        self._group = weakref.ref(group, self._group_deleted)

        self._callbacks: List[chunk_notice.NoticeConnection] = []

        # Add a parameter to this widget whenever a param was added to the group
        self._callbacks.append(
            chunk_notice.NoticeConnection(
                self._param_added_callback, chunk_notice.ChunkNoticeAlias.PARAM_ADDED, self.group
            )
        )

        # Remove a parameter when the parameter was removed from the group
        self._callbacks.append(
            chunk_notice.NoticeConnection(
                self._param_removed_callback, chunk_notice.ChunkNoticeAlias.PARAM_REMOVED, self.group
            )
        )

        # Calls _delete_self() whenever the chunk sends the CHUNK_DELETED
        self._callbacks.append(
            chunk_notice.NoticeConnection(self._delete_self, chunk_notice.ChunkNoticeAlias.CHUNK_DELETED, self.group)
        )

        # Listen to state changes on the group
        self._callbacks.append(
            chunk_notice.NoticeConnection(
                self._state_changed,
                chunk_notice.ChunkNoticeAlias.STATE_CHANGED,
                self.group.get_attribute_sender("state"),
            )
        )

        self._callbacks.append(
            chunk_notice.NoticeConnection(
                self._name_changed,
                chunk_notice.ChunkNoticeAlias.ATTRIBUTE_CHANGED,
                self.group.get_attribute_sender("name"),
            )
        )

        self._callbacks.append(
            chunk_notice.NoticeConnection(
                self._name_changed,
                chunk_notice.ChunkNoticeAlias.ATTRIBUTE_CHANGED,
                self.group.get_attribute_sender("label"),
            )
        )

        self._callbacks.append(
            chunk_notice.NoticeConnection(
                self._layout_changed,
                chunk_notice.ChunkNoticeAlias.ATTRIBUTE_CHANGED,
                self.group.get_attribute_sender("layout"),
            )
        )

        # Associate ChunkParamGroup objects with group widgets
        self.group_widgets: weakref.WeakValueDictionary[ChunkHash, ChunkContainerWidgetBase] = (
            weakref.WeakValueDictionary()
        )

        # Container for all widgets in this chunk. Dict will map between ChunkParam -> widget
        # for each widget created from the same ChunkParam instance
        self.param_widgets: weakref.WeakValueDictionary[ChunkHash, ChunkParamWidgetBase] = weakref.WeakValueDictionary()

        # Initialize everything
        self.factory = self.get_widget_factory()
        self.build_collapsable()
        self._build_param_widgets(self.group)
        self.update_from_layout()
        self.update_from_flags()
        self.update_from_state()

    @property
    def group(self) -> GroupT:
        if group := self._group():
            return group
        raise ReferenceError("reference to group is dead! :'(")

    @abstractmethod
    def get_widget_factory(self) -> ChunkWidgetFactoryBase:
        """
        Override this function to return a specific ui plugin widget factory

        Raises:
            AttributeError: _description_

        Returns:
            ChunkWidgetFactoryBase: The factory that can create the valued parameters from ChunkParamBase objects
        """
        factory = self.group.widget_factory
        if factory is None:
            raise AttributeError(
                "No widget factory supplied to ChunkContainerWidgetBase. Implement get_widget_factory()"
            )
        return factory

    @abstractmethod
    def delete_self(self):
        """
        The entire group should be deleted

        Raises:
            NotImplementedError: _description_
        """
        raise NotImplementedError

    @abstractmethod
    def remove_widget(self, widget: Any):
        """
        Called when a child param widget should be removed from this group widget

        Args:
            widget (Any): The widget to remove from the group widget

        Raises:
            NotImplementedError: _description_
        """
        raise NotImplementedError

    def is_collapsed(self) -> bool:
        """
        Returns whether the group widget should be collapsed

        Returns:
            bool: True if the group should be collapsed
        """
        val = self.group.has_state(ChunkFlags.State.COLLAPSED)
        parent = self.group.get_owner()
        if parent and isinstance(parent, ChunkContainerBase):
            val |= parent.has_state(ChunkFlags.State.COLLAPSED)
        return val

    @abstractmethod
    def build_collapsable(self):
        """
        Build the group widget.

        Raises:
            NotImplementedError: _description_
        """
        raise NotImplementedError

    @abstractmethod
    def update_from_flags(self):
        """
        Update the widget from the flags of self.group

        Raises:
            NotImplementedError: _description_
        """
        raise NotImplementedError

    @abstractmethod
    def set_hidden(self, val: bool):
        """
        Set the ui widget hidden based on the value of val

        Args:
            val (bool): Whether to hide or show the widget

        Raises:
            NotImplementedError: _description_
        """
        raise NotImplementedError

    @abstractmethod
    def set_enabled(self, val: bool):
        """
        Set the ui widget enabled or disabled based on value of val

        Args:
            val (bool): Whether to enable or disable the widget

        Raises:
            NotImplementedError: _description_
        """
        raise NotImplementedError

    @abstractmethod
    def set_selected(self, val: bool):
        """
        Called when the SELECTED state is changed. Should display the selection on the widget. By default,
        this does nothing

        Args:
            val (bool): Whether to enable or disable the widget
        """
        pass

    @abstractmethod
    def update_from_state(self):
        """
        Called when state of the group has changed
        """
        self.set_hidden(self.group.has_state(ChunkFlags.State.HIDDEN))
        # NB: OUTPUT is a ParamFlags value and has no meaning for a group. Checking it
        # here against the group's GroupFlags mis-fires because ParamFlags.OUTPUT and
        # GroupFlags.COLLAPSABLE share the same bit (2), so every collapsible group
        # read as "output" and disabled all its children. A group is only disabled by
        # its own DISABLED state.
        self.set_enabled(not self.group.has_state(ChunkFlags.State.DISABLED))
        self.set_selected(self.group.has_state(ChunkFlags.State.SELECTED))

    @abstractmethod
    def update_from_layout(self):
        """
        Called when layout of the group has changed
        """
        pass

    @abstractmethod
    def set_expanded(self, checked: bool):
        """
        Set if the widget should be expanded and show all child widgets. If false, this should
        hide all the child widgets

        Args:
            checked (bool): Whether the group widget should be expanded or not

        Raises:
            NotImplementedError: _description_
        """
        raise NotImplementedError

    @abstractmethod
    def update_param_label(self, notice, sender: "AttributeSender"):
        """
        When a param name, label or flag is changed, this will correct the label in the FormLayout.
        If LABELED was unset, then remove the label and set the widget role to SpanningRole
        If LABELED was set, add the label
        otherwise, just update the label text if name or label changed.

        A ui implementation plugin should implement this function to get labeled widgets

        Args:
            notice (Tf.Notice): The notice that was sent
            sender (chunk_core.AttributeSender): The attribute that changed
        """
        pass

    @abstractmethod
    def update_label(self, text: str):
        """
        Update the label of this container

        Args:
            text (str): The new label to display
        """
        pass

    @abstractmethod
    def supports_widget(self, widget: Any) -> bool:
        """
        For ui implementation plugins to make sure the supplied widget is supported. F.e:
        ``` py
        return isinstance(widget, QtWidgets.QWidget)
        ```

        Args:
            widget (Any): The widget to check

        Raises:
            NotImplementedError: _description_

        Returns:
            bool: True if the widget is supported by the current ui plugin
        """
        raise NotImplementedError

    @abstractmethod
    def add_widget(self, widget: "ChunkContainerWidgetBase | ChunkParamWidgetBase", label: Optional[str] = None):
        """
        Called after a param has been added to the group. This should add the widget to the relevant layouts

        Args:
            widget (ChunkContainerWidgetBase | ChunkParamWidgetBase): The widget to add
            label (str, optional): An optional label of the widget. Defaults to None.

        Raises:
            NotImplementedError: _description_
        """
        raise NotImplementedError

    @abstractmethod
    def add_list_widget(
        self, widget: "ChunkContainerWidgetBase | ChunkParamWidgetBase", label: Optional[str] = None, list_index=0
    ):
        """
        Called after a param has been added to the group. This should add the widget to the relevant layouts

        Args:
            widget (ChunkContainerWidgetBase | ChunkParamWidgetBase): The widget to add
            label (str, optional): An optional label of the widget. Defaults to None.

        Raises:
            NotImplementedError: _description_
        """
        raise NotImplementedError

    def _name_changed(self, notice, sender):
        self.update_label(sender.value)

    def _layout_changed(self, notice, sender):
        self.update_from_layout()

    def _disconnect_callbacks(self):
        """Revoke all notice subscriptions held by this widget.

        Must be called when the widget is torn down. The host (Qt) may destroy the
        underlying C++ object while the Python wrapper — and therefore its notice
        subscriptions — stays alive, so without this a later notice would invoke a
        method on a deleted object (e.g. ``setHidden`` on a dead ChunkWidget).
        """
        for conn in self._callbacks:
            try:
                conn.disconnect()
            except Exception:
                pass
        self._callbacks.clear()

    def _group_deleted(self, ref):
        self._disconnect_callbacks()
        self.delete_self()

    def _delete_self(self, notice, sender):
        if sender is not self.group:
            return

        self.group_widgets.clear()
        self.param_widgets.clear()

        self._disconnect_callbacks()
        self.delete_self()

    def _param_added_callback(self, notice, sender: GroupT):
        h = notice.data
        if not h:
            return
        param = None

        if isinstance(self.group, ChunkListGroupBase):
            param = self.group.get_my_params()[-1]
        else:
            param = self.group.get_param(h)

        if not param:
            return

        self._add_param(param)

    def _param_removed_callback(self, notice, sender: GroupT):
        h = notice.data
        if not h:
            return

        if isinstance(h, ChunkParamBase):
            if wid := self.param_widgets.get(h):
                self.remove_widget(wid)

        elif isinstance(h, ChunkContainerBase):
            if wid := self.group_widgets.get(h):
                self.remove_widget(wid)

    def _state_changed(self, notice, sender):
        if notice.data.get_owner() == self.group:
            try:
                self.update_from_state()
            except RuntimeError:
                # Host C++ object was already destroyed but this widget's subscription
                # outlived it (stale widget from a panel rebuild). Self-heal by revoking.
                self._disconnect_callbacks()

    def _add_param_widget(self, param: "ChunkParamBase", group: Optional["ChunkContainerBase"] = None, list_index=0):
        is_output = param.has_flag(ChunkFlags.ParamFlags.OUTPUT)
        t = param.evaluate_type()
        # MATRIX joins NONE as a type that draws nothing: sixteen numbers have no obvious
        # widget, and every use so far is data a chunk stores rather than something anyone
        # edits by hand. Listed here rather than left to fall through the factory because
        # the fallback there logs a missing-mapping warning, which would be wrong -- the
        # mapping is absent on purpose. Drop it from this tuple the day a matrix widget
        # exists and it will start drawing.
        if t in (ChunkFlags.ParamType.NONE, ChunkFlags.ParamType.MATRIX):
            return

        widget: ChunkParamWidgetBase | None
        # Custom widget is created and managed externally
        if t == ChunkFlags.ParamType.CUSTOM and param.custom_widget and self.supports_widget(param.custom_widget):
            # If the widget already has a parent (displayed in another panel),
            # create a fresh instance so both panels get their own widget.
            existing = param.custom_widget
            if hasattr(existing, 'parent') and callable(existing.parent) and existing.parent() is not None:
                widget = type(existing)(param)
            else:
                widget = existing
        else:
            if not self.factory.has_mapping(t):
                logger.warning("Missing widget mapping for param %s", param.name)
                return

            # Create a widget from the factory based on the param.get_type()
            widget = self.factory.create_widget(param)

        if not widget:
            return

        if is_output:
            widget.set_enabled(False)

        self._callbacks.extend(
            [
                chunk_notice.NoticeConnection(
                    self.update_param_label,
                    chunk_notice.ChunkNoticeAlias.ATTRIBUTE_CHANGED,
                    param.get_attribute_sender("name"),
                ),
                chunk_notice.NoticeConnection(
                    self.update_param_label,
                    chunk_notice.ChunkNoticeAlias.GROUP_CHANGED,
                    param.get_attribute_sender("name"),
                ),
            ]
        )

        if group is not None:
            layout = self._add_group_layout(group)
        else:
            layout = self

        self.param_widgets[param.get_hash()] = widget

        if param.has_flag(ChunkFlags.ParamFlags.LABELED):
            layout.add_widget(widget, param.get_label())
        else:
            layout.add_widget(widget)

        return widget

    def _add_group_layout(self, group: "ChunkContainerBase") -> "ChunkContainerWidgetBase":
        container = self.group_widgets.get(group, None)
        if not container:
            container = self.__class__.__new__(self.__class__)
            container.__init__(group, parent=self)  # type: ignore
            self.add_widget(container)
            self.group_widgets[group.get_hash()] = container

        return container

    def _build_param_widgets(self, group: "ChunkContainerBase"):
        for param in group.get_my_params():
            self._add_param(param)

    def _add_param(self, param: "ChunkCommon"):

        if isinstance(param, ChunkContainerBase):
            self._add_group_layout(param)
        if isinstance(param, ChunkParamBase):
            self._add_param_widget(param)


#####################
# FIELD TRANSFORMER #
#####################




class ChunkHash(Protocol):
    """Protocol for hashable Chunk object"""

    def __hash__(self) -> int: ...
    def __eq__(self, value: object, /) -> bool: ...


class ProxyParam(chunk_notice.ChunkNoticePropagator):
    """
    A proxy parameter lies ontop of a concrete parameter. The proxy stores only a uniuqe value and unique children
    if the parameter is a group. This is useful for lists, where an original group dictates the content of each list
    entry, but the list entries can hold unique values. This means we can change any attribute except 'value' on the
    original, and the proxies will update.

    The proxy will have a unique hash value. To get the original parameter, use the param_ref property of the proxy.
    Worth noting is the ChunkCommon's _hashable attribute is not overridden, meaning ProxyParam._hashable will evaluate to the
    referenced HashVal

    Args:
        param (ChunkCommon): A chunk object which to reference
    """

    def __init__(self, param: "ChunkCommon", *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.__hashable = HashVal()
        self.__param_ref = weakref.ref(param, self._param_died)

        if isinstance(param, ChunkParamBase):
            self._value = param.value
            self.__value_sender = AttributeSender(self, "value", param.get_attribute_sender("value").alias)
        else:
            self._value = None
            self.__value_sender = None

        # A ProxyParam mirrors its template's non-value attribute changes via this
        # dependency, but must not be pulled into the template's graph_id: set_graph_id
        # merges the *established* side into whichever DagGraphID it's called with, and a
        # freshly-constructed proxy's root would otherwise become the new root for the
        # whole tab it was just added to, orphaning that tab's graph_XXXX layer.
        self.get_dag_node()._part_of_graph = False
        self.add_dependency(param)

        self._children = NameSyncedContainer()

        if isinstance(param, ChunkContainerBase):
            self._children.set_owner(self)
            self._children = NameSyncedContainer.create_proxy(param.children, self._children)

            self.add_callback(self._ref_child_added, param, notice_alias=chunk_notice.ChunkNoticeAlias.PARAM_ADDED)
            self.add_callback(self._ref_child_removed, param, notice_alias=chunk_notice.ChunkNoticeAlias.PARAM_REMOVED)


    def _ref_child_added(self, notice, sender):
        if isinstance(notice.data, ChunkParamBase):
            self._children.add(ProxyParam(notice.data))

    def _ref_child_removed(self, notice, sender):
        if isinstance(notice.data, ChunkCommon):
            p = None
            for param in self._children.get_my_params():
                if isinstance(param, ProxyParam) and param._param_ref == notice.data:
                    p = param
                    break
                if param == notice.data:
                    p = param
                    break

            if not p:
                raise ReferenceError("Could not find ref child")

            del self._children[p]

    def _param_died(self, ref):
        self.request_delete()

    @property
    def _param_ref(self):
        if param := self.__param_ref():
            return param
        raise ReferenceError("Reference to param is dead")

    def __eq__(self, value: object) -> bool:
        return self.__hashable == value

    def __hash__(self) -> int:
        return self.__hashable.__hash__()

    def _get_my_params(self):
        return self._children.get_my_params()

    def _get_params(self):
        return self._children.get_params()

    def _get_param(self, *args, **kwargs):
        return self._children.get_param(*args, **kwargs)

    def _iter_hierarchy(self, level=0):
        yield level, self
        for p in self._get_my_params():
            if isinstance(p, ChunkGroupBase):
                yield from p.iter_hierarchy(level + 1)
            else:
                yield level + 1, p

    def _get_attribute_sender(self, name):
        if name == "value":
            return self.__value_sender
        return self._param_ref.get_attribute_sender(name)

    def get_notice_alias(self) -> chunk_notice.ChunkNoticeAlias:
        return self._param_ref.get_notice_alias()

    def request_delete(self):
        self._children.clear()
        self._children.set_owner(None)

        if self.__param_ref():
            # If the parameter is alive, we should send
            owner = self.get_owner()
            if owner and isinstance(owner, NameSyncedContainer):
                # Owner is a NameSyncedContainer, pop it from the contiainer. When successfully removed,
                # the container will send PARAM_REMOVED with it's owning ChunkContainerBase as sender.
                owner.send_removed(self)

        self.set_owner(None)

    def data_changed(self, notice: chunk_notice.ChunkNotice | None = None) -> chunk_notice.ChunkNotice | None:
        if not notice or not (res := super().data_changed(notice)):
            return None

        if notice.data == self._param_ref:
            for alias, prop in notice.data.get_dag_node().iter_changes():
                self.send_self(alias, prop)
        return res

    def send_self(
        self, alias: chunk_notice.ChunkNoticeAlias | None = None, data: Any = None
    ) -> chunk_notice.ChunkNotice | None:

        if not (res := super().send_self(alias, data)):
            return None

        if alias is None:
            alias = self.get_notice_alias()

        if alias is chunk_notice.ChunkNoticeAlias.ATTRIBUTE_CHANGED:
            # Run all methods connected to the reference param, with self as
            # sender. This is possibly idiotic
            notice_class = chunk_notice.get_notice(alias)
            if issubclass(notice_class, chunk_notice.ChunkNotice):
                notice = notice_class(self.get_notice_data())
            else:
                notice = notice_class()

            for conn in self._param_ref.get_callbacks():
                conn.handle_callback(notice, self)
        return res

    def __getattribute__(self, name: str) -> Any:
        if name in ("value", "raw_value"):
            # Return the value stored on the proxy so widgets display the proxy's own value,
            # not the template's default.
            return super().__getattribute__("_value")
        if name == "iter_hierarchy":
            return super().__getattribute__("_iter_hierarchy")
        if name == "get_my_params":
            return super().__getattribute__("_get_my_params")
        if name == "get_params":
            return super().__getattribute__("_get_params")
        if name == "get_param":
            return super().__getattribute__("_get_param")
        if name == "get_attribute_sender":
            return super().__getattribute__("_get_attribute_sender")
        if name == "_param_died":
            return super().__getattribute__("_param_died")
        if name == "request_delete":
            return super().__getattribute__("request_delete")

        if name in ["_dag_node", "_callbacks", "_blocked"]:
            return super().__getattribute__(name)

        if name != "__class__" and hasattr(chunk_notice.ChunkNoticePropagator, name):
            return super().__getattribute__(name)

        if name in ["children", "_children"]:
            return super().__getattribute__("_children")

        try:
            if not name.endswith("_param_ref") and hasattr(self._param_ref, name):
                return getattr(self._param_ref, name)
        except (ReferenceError, AttributeError):
            pass

        return super().__getattribute__(name)

    def __setattr__(self, name: str, value: Any) -> None:
        # All attributes except 'owner' and 'value' are proxied, meaning we set the attributes
        # back on to the parameter we're proxying.

        if name != "__class__" and hasattr(chunk_notice.ChunkNoticePropagator, name):
            return super().__setattr__(name, value)

        if name in ["_dag_node", "_callbacks", "_blocked"]:
            return super().__setattr__(name, value)

        if name == "children":
            return super().__setattr__("_children", value)

        if name == "value" and self.__value_sender:
            # Instead of setting the value on the param, we intercept and store the value on the
            # proxy itself. This will propagate the notices through the correct channel instead
            # of through the original param ownership channel.
            self.__value_sender.old_value = self._value
            self._value = value
            # A list-group row (e.g. Sublayer's imports:sublayer_path) is a ProxyParam, so
            # _load_wire_inputs' bulk restore reaches this setattr directly -- not through
            # ChunkBase.value_changed, the only place chunk_notice._loading_stage is normally
            # checked (see its own guard). Without this, restoring a row's saved value fired a
            # real, immediate ATTRIBUTE_CHANGED here regardless of the bulk load in progress,
            # live-reported as the whole downstream chain re-executing repeatedly on every
            # reload of a saved scene (a list-group row was the one param path load_chunks'
            # suppression never actually covered). load_chunks' own later, correctly-ordered
            # "trigger the DAG solve for root nodes" pass still runs once everything is
            # restored -- and now reliably reaches this chunk's real content (see the layer
            # pre-registration added just above the re-sort pass in _add_prim_mods), so this
            # doesn't lose the eventual real execute(), only the early, repeated ones.
            if not chunk_notice._loading_stage:
                self.__value_sender.execute()
            return

        try:
            if not name.endswith("_param_ref") and hasattr(self._param_ref, name):
                return setattr(self._param_ref, name, value)
        except (ReferenceError, AttributeError):
            pass
        return super().__setattr__(name, value)


class NameSyncedContainer(Generic[ChunkT], chunk_notice.ChunkNoticePropagator):
    """Container for holding ChunkCommon objects. The container holds two maps: params & names. The maps
    are ChainMaps, meaning they provide access to all nested NameSyncedContainer param and names as well.
    In practice, this means we can quickly lookup nested children of chunks or groups, without having to
    manually iterate the hierarchy. For this reason, this class provide get_* as well as get_my_* functions,
    where get_my_* functions will only return objects directly owned by this container, and not any nested
    objects.

    params maps from a uuid to a ChunkCommon object. ChunkCommon objects hash by their uuid, so accessing a param
    can be done either by the ChunkCommon object as params[obj], or the uuid: params[obj._hashable]. Where a typehint
    suggests a HashVal argument for lookup (get_param(), get_my_param()), passing a ChunkCommon obj should
    return same as passing obj._hashable.
    TODO: This is a bit un-intuituve, and should perhaps be reconsidered.

    names map betweeen the current (mutable) name of a param and the uuid. This exists in order to quicker acccess
    params by their name. This container need to be maintained and updated whenever a param name is changed, which is currently
    achieved by subclassing ChunkNoticePropagator, in order to insert itself in hierarchy of objects and modifying the mapping
    whenever a name changes.

    Any objects added though
    ``` py
    NameSyncedContainer.add()
    ```
    will have the NameSyncedContainer as owner. Any concrete chunk object who holds an instance of this class should therefore not
    manage ownership of params, instead, it should own the NameSyncedContainer only. This class will not send any notices though, only propagate
    notices from children.
    """

    def __init__(self):
        super().__init__()

        self._last_added: Optional[HashVal] = None
        self._last_removed: Optional[HashVal] = None

        self._params: ChainMap[ChunkHash, ChunkT] = ChainMap()
        self._names: ChainMap[str, ChunkHash] = ChainMap()

    def __copy__(self):
        copied = NameSyncedContainer()

        for param in self._params.maps[0].values():
            copied.add(copy.copy(param))

        copied._copy_callbacks(self)
        return copied

    def empty(self):
        return not bool(self._params)



    def get_notice_data(self) -> Self:
        return self

    def clear(self):
        """
        Clear all children from this container. This will not delete the children explicitly as references to the children may
        be held elsewhere. However, it can be expected that the children are implicitly deleted when calling this function, as
        other objects should preferably only store weakrefs to the params
        """
        self._names.clear()
        self._params.clear()

    def add(self, obj: "ChunkT | ProxyParam", send=True):
        """Add a chunk object to this container. This will make the container the owner of the param, and store the
        object in self.params, and obj.name in self.names. Access to the object can thus be done through
            self.get_param(), self.get_by_name()
        or
            self.params.maps[0].get(self.names.maps[0].get("some_name"))
        since the container manages the name<->obj relationship, some care need to be taken in order for this relationship to be maintained.
        For example, do not add params to self.params or self.names directly, instead, use this function to get proper behaviour.

        If the added param is an instance of ChunkContainerBase, the children of the new param will be appended to the self.params and self.names
        ChainMaps, which allows for fast access of nested params.

        Args:
            obj (ChunkCommon): The param to add. Nothing is done to avoid duplication across layers.
        """

        # Append child the object + name to the first layer of the ChainMaps
        self._params.maps[0][obj.get_hash()] = obj  # type: ignore
        self._names.maps[0][obj.name] = obj.get_hash()

        # Mark self as the owner of the added obj as we need to be notified when the object changes
        obj.set_owner(self)

        if send:
            self.send_added(obj)

    def add_nested(self, nested: "NameSyncedContainer[ChunkT]"):
        """
        Appends nested params to the ChainMap. This allows for fast lookups accross the layers.

        Args:
            nested ("NameSyncedContainer[ChunkT]"): The nested params to add
        """
        self._params.maps.append(nested._params)
        self._names.maps.append(nested._names)

    @staticmethod
    def create_proxy(source: "NameSyncedContainer", target: "NameSyncedContainer"):
        for param in source.get_my_params():
            proxy = ProxyParam(param)
            target.add(proxy)
        return target

    def data_changed(self, notice: Optional[chunk_notice.ChunkNotice] = None) -> Optional[chunk_notice.ChunkNotice]:
        # Override to reflect any changes on the children in this ds. Used to update the names when a name changes
        if notice and (p := notice.data):
            if isinstance(p, AttributeSender) and p.name == "name" and p.old_value in self._names.maps[0]:
                self.name_changed(p.old_value)  # type: ignore

        return super().data_changed(notice)

    def get_notice_alias(self) -> chunk_notice.ChunkNoticeAlias:
        # We are only interested in hijacking data_changed, and should not send any signals.
        return chunk_notice.ChunkNoticeAlias.NONE

    def update_names(self):
        """Update the names ChainMap for all layers to reflect the names of the params. Can be used as brute-force
        to get correct mapping
        """
        invalid = []
        for i, mapping in enumerate(self._names.maps):
            for name, idx in mapping.items():
                obj = self.get_param(idx)

                if obj and obj.name != name:
                    invalid.append((i, name, obj.name))

        for i, old_name, new_name in invalid:
            idx = self._names.maps[i].pop(old_name)
            self._names.maps[i][new_name] = idx

    def name_changed(self, old_name: str):
        """Marks the old_name as changed, and update the names dict for this layer only, to reflect the change.
        old_name will be replaced with whatever the new name of the corresponding object is. This allows for:
        ``` py
            c = Chunk.Slider("A")
            g = Chunk.Group("group")
            g.add_param(c)

            p = g.get_param("A")
            asset(p != None)

            p.name = "B"
            p = g.get_param("B")
            asset(p != None && g.get_param("A") == None)
        ```
        Args:
            old_name (str): The old name mapped to the changed object. This will be removed from the first layer of the ChainMap if found

        Raises:
            ValueError: The old_name was not found
        """

        # Only look for names on the first layer. Groups are responsible for their respective layers
        mapping = self._names.maps[0]

        if old_name in mapping:
            idx = mapping.pop(old_name)
            if obj := self.get_param(idx):
                mapping[obj.name] = idx
        else:
            raise ValueError(f"Failed to update old name {old_name} in container")

    @overload
    def get_param(self, key: Union[ChunkHash, str]) -> Optional[ChunkT]: ...

    @overload
    def get_param(self, key: Union[ChunkHash, str], predicate: Optional[Predicate[ValueT]]) -> ValueT: ...

    def get_param(
        self, key: Union[ChunkHash, str], predicate: Optional[Predicate[ValueT]] = None
    ) -> Optional[ChunkT] | ValueT:
        """Retrieve a param from this datastructure. Key can be either a ChunkCommon (It hashes to the HashVal),
        an explicit HashVal (ChunkObject.get_hash()) or a string (name or path). If the key is a path (identifier) to a param such as:
        aChunk.aGroup.aParam, the returned param will look for a param at the path specified. If just a name is supplied, the first
        found param with the name is returned.

        Returns:
            Optional[ChunkT]: The found Chunk object if found
            ValueT: The found Chunk object matching the predicate if supplied
        """
        if isinstance(key, str):
            res = self.get_by_name(key)
        else:
            res = self._params.get(key)

        if predicate is None:
            return res

        if not predicate(res):
            raise ValueError(f"get_param predicate {predicate} failed for parameter {key}")
        return res

    @overload
    def get_by_name(self, name: str) -> Optional[ChunkT]: ...

    @overload
    def get_by_name(self, name: str, predicate: Optional[Predicate[ValueT]]) -> ValueT: ...

    def get_by_name(self, name: str, predicate: Optional[Predicate[ValueT]] = None) -> Optional[ChunkT] | ValueT:
        """
        Excplicitly retrieve a param by its name. In order to find nested children, one can
        pass the hierarchy to the 'name' argument by adding a dot (.) for each level in the hierarchy:

        ``` py
        myChunk.get_param("groupA.groupB.slider")
        ```

        Returns:
            Optional[ChunkT]: The found Chunk object if found
            ValueT: The found Chunk object matching the predicate, if supplied
        """
        if "." in name:
            hierarchy = name.split(".")
            cm: ChainMap = self._names
            idx = 0
            g = hierarchy[0]

            while idx < len(hierarchy) - 1:
                if g not in cm:
                    return None

                if g in cm.maps[0]:
                    # Found g in first layer, so can look for next entry in the query
                    idx += 1

                    # FInd the parameter matching g and update cm to reflected the parameters own children.
                    grp = self._params.get(cm.maps[0].get(g))
                    if not grp or not isinstance(grp, ChunkContainerBase):
                        raise NameError(f"Could not find param {name}. {g} is not a container type parameter")

                    cm = grp.children._names

                g = hierarchy[idx]

            if key := cm.get(g):
                res = self._params.get(key)
                if not predicate or predicate(res):
                    return res
                raise ValueError(f"get_by_name predicate {predicate} failed for parameter {key}")

        if key := self._names.get(name):
            res = self.get_param(key)
            if not predicate or predicate(res):
                return res
            raise ValueError(f"get_by_name predicate {predicate} failed for parameter {key}")

    def get_names(self) -> Tuple[str, ...]:
        """Get all names of this layer and all child layers

        Returns:
            tuple[str, ...]: All param names on this layer and under
        """
        return tuple(self._names.keys())

    def get_my_names(self) -> Tuple[str, ...]:
        """Get all names on this layer only

        Returns:
            tuple[str, ...]: The param names of the first layer
        """
        return tuple(self._names.maps[0].keys())

    @overload
    def get_params(self) -> Tuple[ChunkT, ...]: ...

    @overload
    def get_params(self, predicate: Optional[Predicate[ValueT]]) -> Tuple[ValueT, ...]: ...

    def get_params(self, predicate: Optional[Predicate[ValueT]] = None) -> Tuple[ChunkT, ...] | Tuple[ValueT, ...]:
        """Get all params on this layer and under

        Returns:
            Tuple[ChunkT, ...]: The params
            Tuple[ValueT, ...]: The params matching the predicate if supplied
        """
        ps = self._params.values()

        if not predicate:
            return tuple(ps)
        return tuple(filter(predicate, ps))

    @overload
    def get_my_params(self) -> Tuple[ChunkT, ...]: ...
    @overload
    def get_my_params(self, predicate: Optional[Predicate[ValueT]]) -> Tuple[ValueT, ...]: ...

    def get_my_params(self, predicate: Optional[Predicate[ValueT]] = None) -> Tuple[ChunkT, ...] | Tuple[ValueT, ...]:
        """Get all params on this layer only

        Returns:
            Tuple[ChunkT, ...]: The params
            Tuple[ValueT, ...]: The params matching the predicate, if supplied
        """
        ps = self._params.maps[0].values()
        if not predicate:
            return tuple(ps)
        return tuple(filter(predicate, ps))

    def send_added(self, obj: "ChunkCommon | ProxyParam"):
        """
        Sends a PARAM_ADDED notice with the new obj as notice data and owner as the sender.

        Args:
            obj (ChunkCommon | ProxyParam): the new parameter
        """
        self._last_added = obj._hashable

        if owner := self.get_owner():
            owner.send_self(chunk_notice.ChunkNoticeAlias.PARAM_ADDED, obj)

    def send_removed(self, obj: "ChunkCommon | ProxyParam"):
        """
        Sends a PARAM_REMOVED notice with the new obj as notice data and owner as the sender.

        Args:
            obj (ChunkCommon | ProxyParam): the removed parameter
        """

        self._last_removed = obj._hashable
        if owner := self.get_owner():
            owner.send_self(chunk_notice.ChunkNoticeAlias.PARAM_REMOVED, obj)

    def __delitem__(self, key: ChunkHash):
        if obj := self.get_param(key):
            # send_removed uses obj.send_self(). For this reason, we need to call send_remove before set_owner(None),
            # as send_self() will propagate the notice to the owners
            self.send_removed(obj)

            # obj.name can drift from the name-map key (a rename or a partially-applied clone can
            # desync the map). Remove the entry by matching value (the key) and fall back to name,
            # popping defensively so delete never crashes with a KeyError.
            names0 = self._names.maps[0]
            if names0.get(obj.name) == key:
                del names0[obj.name]
            else:
                for nm in [n for n, k in list(names0.items()) if k == key]:
                    del names0[nm]
            self._params.maps[0].pop(key, None)

            if obj.get_owner() == self:
                obj.set_owner(None)

    def pop(self, key: ChunkHash) -> Optional["ChunkCommon"]:
        """
        Pop the object at key without calling send_remove

        Args:
            key (ChunkHash): The chunk object to remove

        Returns:
            ChunkCommon | None: The pop'ed object, or None if the object is unavailable
        """
        obj = self.get_param(key)
        if obj:
            del self[obj]
            return obj


######################
# CHUNK BASE CLASSES #
######################


@attrs.frozen
class HashVal:
    """
    A hash value used by Chunk objects. The actual hash is a created from a uuid4. This
    is held by ChunkCommon, and thus provides a unique id for all chunk objects
    """

    @staticmethod
    def generate_key() -> int:
        """Generate a new uuid value used for hashing. Should only be used internally"""
        return uuid.uuid4().int  # type: ignore

    _uuid: int = attrs.field(
        factory=generate_key,
        init=False,
        metadata=AttributeMetadata(copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND),
    )

    def __hash__(self) -> int:
        return hash(self._uuid)

    def __eq__(self, value: object) -> bool:
        return hash(self) == hash(value)

    def __copy__(self):
        # Should never be called, but just in case...
        # Copying this class should result in a new key
        cls = self.__class__
        c = cls.__new__(cls)
        c.__init__()
        return c


class ChunkCommon(AttributesBase):
    """Base class for all Chunk objects. Should not be instantiated directly, but inherited by concrete classes.
    Holds attributes that all chunk objects are excpected to support

    Attributes:
        name: The name of the chunk object
        label: An optional label to display in the ui
        tooltip: A description of the chunk object
        icon: A path to an image which to use as icon
        user_data: A dict of arbitrary data to store on the param
        layout: Flags for setting the layout of the chunk object in the UI
        state: The current state
        flags: Some implementation specific flags. Unused by default

    """

    # Please do not mutate!
    _hashable: HashVal = attrs.field(
        init=False,
        factory=HashVal,
        metadata=AttributeMetadata(copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND),
    )

    # Name for the chunk object
    name: str = attrs.field(default="")

    # Explicit label (nice name). Will be derived from name if not set
    label: Optional[str] = attrs.field(default="")
    tooltip: Optional[str] = attrs.field(default="")
    icon: Optional[str] = attrs.field(default="")  # Path to icon

    # Hold custom data which is not used directly by the chunk
    user_data: Dict[str, Any] = attrs.field(factory=dict, repr=False)

    # Orientation of the widget, respected if relevant
    layout: ChunkFlags.LayoutFlags = attrs.field(default=ChunkFlags.LayoutFlags.VERTICAL)

    # State flags of the param - hidden, collapsed, disabled
    state: ChunkFlags.State = attrs.field(
        default=ChunkFlags.State.NONE,
        metadata=AttributeMetadata(sender_alias=chunk_notice.ChunkNoticeAlias.STATE_CHANGED),
    )

    # Type specific flags, can be used for setting custom flags.
    flags: IntFlag = attrs.field(
        default=0,
        metadata=AttributeMetadata(sender_alias=chunk_notice.ChunkNoticeAlias.FLAGS_CHANGED),
    )  # type: ignore

    # Hold a weakref to the owning chunk for faster access to it, without having to traverse the
    # hierarchy
    _chunk_parent: Optional[weakref.ReferenceType] = attrs.field(
        default=None, init=False, metadata=AttributeMetadata(send_mode=SendMode.NO_SEND)
    )

    def get_identifier(self) -> str:
        """
        Get an identifier for this chunk object. The identifier is a dot(.) separated path, from the top-most
        owner, usually the ChunkBase, to this object. F.e. MyChunk.SomeGroup.MyParam

        Returns:
            str: The string identifier for this chunk object
        """
        names = [self.name]
        owner = self.get_owner()
        while owner:
            if isinstance(owner, ChunkCommon):
                names.append(owner.name)
            owner = owner.get_owner()
        return ".".join(reversed(names))

    def get_chunk(self) -> Optional["ChunkBase"]:
        """
        Get the owning Chunk if it exists.

        Returns:
            ChunkBase | None: The chunk that owns this object
        """
        if self._chunk_parent is None or self._chunk_parent() is None:
            owner = self.get_owner()
            while owner and not isinstance(owner, ChunkBase):
                owner = owner.get_owner()

            if not owner:
                return None
            self._chunk_parent = weakref.ref(owner)

        return self._chunk_parent()

    def get_layer_identifier(self) -> str:
        """
        Get the layer identifier used by the ChunkLayerManager

        Returns:
            str: the layer identifier for this param
        """

        if c := self.get_chunk():
            name = c.name
            val = str(hash(c))
        else:
            name = self.name
            val = str(hash(self))

        return f"{name}_{val}"

    def get_logger(self) -> logging.Logger:
        if not self._logger:
            self._logger = logging.getLogger(f"usd_chunks.{self.__class__.__name__}.{self.name}")
        return self._logger

    def is_blocked(self) -> bool:
        v = self.has_state(ChunkFlags.State.DISABLED)
        return v or super().is_blocked()

    def get_hash(self) -> HashVal:
        """
        Get the HasVal associated with the chunk object. Can be useful to not increase the ref
        count when storing this object is a hash map

        Returns:
            ChunkHash: The hashval of this object
        """
        return self._hashable

    def get_edit_context(self) -> Sdf.Layer | None:
        """
        Use when modifying the stage and the changes should be added to the local
        Sdf.Layer corresponding to this chunk object. Will traverse up the hierarchy
        to find the nearest ChunkBase parent and use the layer from it, instead of having
        seperate layers for every parameter

        ``` py
        with Usd.EditContext(stage, param.get_edit_context()):
            # modify the stage
        ```

        Returns:
            Sdf.Layer: A layer for this object
        """
        if not chunk_registry.ChunkLayerManager().initialized:
            return

        if c := self.get_chunk():
            layer = chunk_registry.ChunkLayerManager().get_or_create_layer(c)
            if layer and layer.IsMuted():
                return None
            return layer

    def reset_state(self):
        """
        Clears DISABLED and HIDDEN states
        """
        self.unset_state(ChunkFlags.State.DISABLED | ChunkFlags.State.HIDDEN)

    def convert_attr(self, attr: attrs.Attribute, val: Any) -> Any:
        if attr.name == "name":
            return re.sub(r"\W+", "", str(val))[0:100]

        if inspect.isclass(attr.type) and issubclass(attr.type, Enum):
            if val is not None and not isinstance(val, attr.type):
                try:
                    return attr.type(val)
                except TypeError:
                    return val

        return super().convert_attr(attr, val)

    def validate_attr(self, attr: attrs.Attribute, val: Any) -> bool:
        # Called when a attrs.field is modified. Should raise ValueError if the validation fails
        if attr.name in ["name"]:
            return self._validate_name(attr, val)
        if attr.name == "icon" and val:
            # Icon path should exist if. if bool(val) is False, then we can ignore the validation
            return self._validate_existing_path(attr, val)
        if attr.name in ["tooltip", "label"]:
            return self._validate_string(attr, val)

        return super().validate_attr(attr, val)

    def _validate_name(self, attribute, value) -> bool:
        self._validate_string(attribute, value)

        if " " in value:
            raise ValueError(f"{value} contains spaces")

        if value:
            expr = r"[_a-zA-Z0-9]+"
            found = re.fullmatch(expr, value)
            if found is None:
                raise ValueError(f"Name: '{value}' contains illegal characters. Legal characters = [a-z, A-Z, 0-9, _]")

        return True

    def _validate_path(self, attr: attrs.Attribute, value) -> bool:
        """Check if the given value is a valid os path to be used as an attribute.
        Can be called in _validate or _convert of subclasses when checking a path attribute.
        Use _validate_existing_path to check for existing paths. Currently this does nothing.
        Holding off with this as some string parameter may support Sdf.AssetPath or pathlib.Path
        TODO: implement

        Args:
            attr (Attrs.Attribute): _description_
            value (_type_): _description_

        Returns:
            bool: True if the value is a valid path
        """
        return True

    def _validate_existing_path(self, attr: attrs.Attribute, value) -> bool:
        """Check if the given value is a valid os path and that it exists

        Args:
            attr (attrs.Attribute): The attribute to validate
            value (str): The value to validate

        Returns:
            bool: True if succcess
        """
        if not self._validate_path(attr, value):
            return False

        # A `#`-padded value is a per-frame template (scripts/usd/value_clips.py's Clip
        # field is the case this exists for: "./clips/points.###.usd"), not one literal
        # path -- by construction nothing on disk is ever named exactly that, so the
        # existence check does not apply. FILE_EXISTING still does its other job here:
        # the browse dialog opens in Open, not Save, mode (chunk_widget_api.file_dialog_spec
        # reads the same flag), and a real value still gets checked below.
        if "#" in value:
            return True

        if not os.path.exists(value):
            raise ValueError(f"path for attribute {attr.name} does not exist")

        return True

    def _validate_string(self, attr: attrs.Attribute, value) -> bool:
        if not isinstance(value, str):
            raise ValueError(f"attribute {attr.name} should be a string. Attempted to set {value}")
        return True

    def get_label(self) -> str:
        """
        Get the label of this chunk object. If no label is set, returns the name

        Returns:
            str: The label
        """
        return self.name if not self.label else self.label

    def set_flag(self, flag: IntFlag):
        """
        Enables a flag

        Args:
            flag (IntFlag): The flag to enable
        """
        self.flags |= flag

    def unset_flag(self, flag: IntFlag):
        """
        Disable a flag

        Args:
            flag (IntFlag): The flag to disable
        """
        self.flags &= ~flag

    def has_flag(self, flag: IntFlag) -> bool:
        """
        Check if a flag is enabled

        Args:
            flag (IntFlag): The flag to check

        Returns:
            bool: If the flag is enabled or not
        """
        # Compared as plain ints: IntFlag.__and__ constructs (and caches, and looks up) a
        # whole new flag member just to be thrown away by bool(). This is called millions of
        # times when walking a large graph -- get_chunks() alone asks twice per chunk -- and
        # the enum machinery was measurably the second-largest cost of building one.
        return bool(int(self.flags) & int(flag))

    def set_state(self, state: ChunkFlags.State):
        """
        Set a state

        Args:
            state (ChunkFlags.State): The state to set
        """
        self.state |= state

    def unset_state(self, state: ChunkFlags.State):
        """
        Unset a state

        Args:
            state (ChunkFlags.State): The state to unset
        """
        self.state &= ~state

    def has_state(self, state: ChunkFlags.State) -> bool:
        """
        Check if state is set

        Args:
            state (ChunkFlags.State): The state to check

        Returns:
            bool: True if the state is set, otherwise False
        """
        return bool(self.state & state)

    def set_selected(self, val: bool):
        if val:
            self.set_state(ChunkFlags.State.SELECTED)
        else:
            self.unset_state(ChunkFlags.State.SELECTED)

    def __hash__(self) -> int:
        return hash(self._hashable)

    def __eq__(self, value: object) -> bool:
        return self._hashable == value

    def get_stage(self) -> Usd.Stage | None:
        """Easy access to the current UsdStage.

        Returns:
            Usd.Stage: The stage held by the SceneSender of ChunkRegistry
        """
        return self.get_scene().get_stage() or chunk_notice._loading_stage

    def get_scene(self) -> "SceneSender":
        """Easy access to the current chunk scene.

        Returns:
            SceneSender: The SceneSender of ChunkRegistry
        """
        return chunk_registry.ChunkRegistry().scene

    def can_delete(self):
        return not bool(list(self.get_dag_node().get_outputs()))

    def is_deleting(self) -> bool:
        """Whether request_delete has been called on this object.

        True from the start of the teardown onwards, and never reset: a deleted object is
        not reusable. Anything that holds on to a chunk past its own call -- a timer, a
        file watcher -- should check this before acting on it.

        Returns:
            bool: True once deletion has started
        """
        return getattr(self, "_deleting", False)

    def request_delete(self):
        """Sends a notice for owners to delete this Chunk object. It's the owners responsibility to remove their reference to this object.
        If this parameter lies withing a ChunkContainerBase, then the containers' delete_param_or_group is called, with this instance as argument.

        In addition, all callbacks will be removed from this object, and the owner will be set to None.
        """

        # Retire the chunk from module execution before anything is dismantled. Teardown
        # detaches params and edits the layer stack, and both cascade as notices that can
        # re-enter the DAG solve and call this chunk's module.execute() again — against a
        # half-dismantled chunk whose get_param() now returns None, which is an AttributeError
        # in essentially every chunk script (none of them defend against their own params
        # vanishing mid-run, nor should they have to). Never cleared: a chunk is not reusable
        # after request_delete. Read by chunk_notice's execute dispatch.
        self._deleting = True

        # Make sure we're enabled when deleting as being disabled will block necessary notices from being sent
        self.unset_state(ChunkFlags.State.DISABLED)
        # Same for the 'blocked' flag (set by view-driven re-cook on out-of-view chunks): a blocked
        # chunk suppresses its own notices, so CHUNK_DELETED would never fire and the USD prim would
        # be left behind on the stage.
        self.set_blocked(False)

        if not self.can_delete():
            logger.warning(f"Deleting node: {self.name} with connected outputs. Stuff may break")
            self.get_dag_node().disconnect()

        owner = self.get_owner()
        if owner and isinstance(owner, ChunkContainerBase):
            # Owner is a group, so delete this param from the group
            owner.remove_param_or_group(self)
        elif owner and isinstance(owner, NameSyncedContainer):
            # Owner is a NameSyncedContainer, pop it from the contiainer. When successfully removed,
            # the container will send PARAM_REMOVED with it's owning ChunkContainerBase as sender.
            owner.pop(self)

        self._callbacks.clear()
        self.set_owner(None)
        self.send_self(chunk_notice.ChunkNoticeAlias.CHUNK_DELETED)


class ChunkUsdAttributeSupport(AttributesBase):
    """
    This inteface class can be subclassed for valued parameter classes in order to support connections
    to UsdAttribute objects. This enables a parameter to be generated from a UsdAttribute, and creating
    the NoticeConnections for managaging the value setting and getting between the param and the
    UsdAttribute
    """

    # The storage attribute path (/chunks/<name>/inputs:<param>) — the param's authoritative USD value.
    _param_attr_path: Optional[str] = attrs.field(
        default=None,
        init=False,
        metadata=AttributeMetadata(copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND),
    )

    # Real prim attr paths this storage attr drives via USD native connections.
    # Connections are re-authored whenever _param_attr_path is set.
    _driven_attr_paths: List[str] = attrs.field(
        factory=list,
        init=False,
        metadata=AttributeMetadata(copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND),
    )

    # Lifecycle connection — fires update_usd_prop when param value changes.
    _usd_prop_connection: Optional[chunk_notice.NoticeConnection] = attrs.field(
        default=None,
        init=False,
        metadata=AttributeMetadata(copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND),
    )

    def data_changed(self, notice: Optional[chunk_notice.ChunkNotice] = None) -> Optional[chunk_notice.ChunkNotice]:
        my_notice = super().data_changed(notice)

        stage = chunk_registry.ChunkRegistry().scene.get_stage()
        if (
            notice
            and stage
            and self._param_attr_path
            # Not gated on is_blocked(): this writes the param's own storage attribute, a
            # record of the value rather than a reaction to it. A blocked chunk still has to
            # save what was typed into it. What IS a reaction -- pushing the value onto the
            # real prims it drives -- is gated below.
            and isinstance(notice.data, AttributeSender)
            and notice.data.name == "value"
            and hasattr(self, 'raw_value')
            # ChunkVectorBase's "value" bubbles up from a child's scalar AttributeSender —
            # notice.data.value below would be a single float, not the vector's tuple.
            # ChunkVectorBase.data_changed handles its own (correctly-typed) version of this.
            and not isinstance(self, ChunkVectorBase)
        ):
            raw = self.raw_value
            is_expr = isinstance(raw, str) and chunk_utils.has_expression(raw)
            val = notice.data.value
            if val is not None:
                prev_write = chunk_notice._usd_write_active
                chunk_notice._usd_write_active = True
                try:
                    if usd_attr := stage.GetAttributeAtPath(Sdf.Path(self._param_attr_path)):
                        # self._param_attr_path is always the chunk-prim's own storage attr —
                        # bookkeeping, independent of the stage's ambient/Edit-Target-driven
                        # edit target (which _write_driven_attrs below correctly targets
                        # separately via get_edit_context()). It already exists (that's what
                        # having a _param_attr_path means), so update it wherever its spec
                        # already lives — not wherever Edit Target currently resolves to,
                        # which may have moved on since it was first authored.
                        home = chunk_utils.get_attr_home_layer(usd_attr) or stage.GetSessionLayer()
                        with Usd.EditContext(stage, home):
                            if not is_expr:
                                current = usd_attr.Get(0)
                                # Don't write a string to a typed (non-String) attr — an unresolved
                                # forwarded value can arrive as text and raise a type-mismatch.
                                bad_str = isinstance(val, str) and usd_attr.GetTypeName() != Sdf.ValueTypeNames.String
                                if not bad_str and current != val:
                                    usd_attr.Set(val)
                            if is_expr:
                                chunk_root = str(usd_attr.GetPrim().GetParent().GetPath())
                                conns = []
                                for node, attr_name in chunk_utils.parse_expression(raw):
                                    ref_prim = stage.GetPrimAtPath(Sdf.Path(f"{chunk_root}/{node}"))
                                    # The referenced node may not exist (a stale/renamed expression
                                    # ref, or a lofted chain pointing at a prim not on this stage) —
                                    # GetAttribute on a null prim raises, so guard it and default to
                                    # the inputs: port. USD lets a connection target a not-yet-present
                                    # path, so the connection is still authored.
                                    use_out = (
                                        ref_prim.IsValid()
                                        and ref_prim.GetAttribute(f"outputs:{attr_name}").IsValid()
                                    )
                                    suffix = f"outputs:{attr_name}" if use_out else f"inputs:{attr_name}"
                                    conns.append(Sdf.Path(f"{chunk_root}/{node}.{suffix}"))
                                usd_attr.SetConnections(conns)
                                if usd_attr.GetTypeName() == Sdf.ValueTypeNames.String and usd_attr.Get(0) != raw:
                                    usd_attr.Set(raw)
                    if not self.is_blocked():
                        self._write_driven_attrs(stage, val)
                finally:
                    chunk_notice._usd_write_active = prev_write

        return my_notice

    def _write_driven_attrs(self, stage: Usd.Stage, val: Any) -> None:
        """Push val (already resolved) to every real prim attr in _driven_attr_paths.

        Uses get_edit_context() so the write lands in the chunk's own layer (anonymous
        for procedural chunks, file layer for interactive ones), keeping the user's
        edit-target layer free of value opinions. Connections between the driven attr
        and the storage attr are topology only — USD Get() on the driven attr doesn't
        follow them, so the resolved value must be copied explicitly for any GUI
        reading that attr directly to see it.
        """
        if val is None or not self._driven_attr_paths:
            return
        chunk_layer = self.get_edit_context()
        if not chunk_layer:
            return
        for path in self._driven_attr_paths:
            sdf_path = Sdf.Path(path)
            if driven_attr := stage.GetAttributeAtPath(sdf_path):
                if driven_attr.Get(0) != val:
                    with Usd.EditContext(stage, chunk_layer):
                        driven_attr.Set(val)

    @abstractmethod
    def update_usd_prop(self, notice: chunk_notice.AttributeChangeNotice, sender: AttributeSender):
        """
        A callback for setting the UsdAttribute on changes to the sender. Used by parameters when
        modifying the 'value' attribute, then this will be invoked with sender build the 'value'
        AttributeSender object, and the notice.data will be the UsdAttribute

        Args:
            notice (chunk_notice.AttributeChangeNotice): The notice holding the UsdAttribute
            sender (AttributeSender): The sender of the notice
        """
        # ChunkVectorBase.data_changed already handles this write path itself (correctly,
        # with outputs:-preferring connection routing) — this method's SetConnections
        # below hardcodes inputs: unconditionally, so letting both run fights over two
        # different connection targets on every value change.
        if isinstance(notice.data, Usd.Attribute) and not isinstance(self, ChunkVectorBase):
            raw = getattr(self, 'raw_value', None)
            is_expr = isinstance(raw, str) and chunk_utils.has_expression(raw)
            # Only String-typed attrs can hold the raw expression text as their value
            # (needed so _patch_expressions can rewrite it on rename); other types (Float,
            # Float3, etc.) store the resolved value and keep the text in customData
            # instead (chunk_utils.store_expression_text) — rebuilding it from
            # SetConnections cannot work, since the connections name only the refs and
            # drop every operator between them.
            if is_expr and notice.data.GetTypeName() != Sdf.ValueTypeNames.String:
                store_val = sender.value
            else:
                store_val = raw if is_expr else sender.value
            prev_write = chunk_notice._usd_write_active
            chunk_notice._usd_write_active = True
            try:
                # notice.data here is always the chunk-prim's own storage attr (bound via
                # _connect_param_attr — connect_usd_attr's driven-content path is a
                # separate mechanism, _author_driven_connections, scoped via
                # get_edit_context()). It already exists (that's what being bound via
                # _connect_param_attr means), so update it wherever its spec already
                # lives, rather than wherever the stage's ambient/Edit-Target-driven
                # edit target currently points — that may have moved on since it was
                # first authored.
                stage = notice.data.GetStage()
                home = chunk_utils.get_attr_home_layer(notice.data) or stage.GetSessionLayer()
                with Usd.EditContext(stage, home):
                    # A non-String attr must never receive raw expression text: if an expression ref
                    # can't resolve (e.g. a lofted cross-context ref like @ChunkGroup.tx@), sender.value
                    # stays the expression STRING and Set() on a typed (float/etc) attr raises a
                    # type-mismatch. The connection authored below carries the topology, so skip the
                    # value write in that case.
                    bad_str = isinstance(store_val, str) and notice.data.GetTypeName() != Sdf.ValueTypeNames.String
                    if not bad_str and notice.data.Get(0) != store_val:
                        notice.data.Set(store_val)
                    if is_expr:
                        chunk_root = str(notice.data.GetPrim().GetParent().GetPath())
                        notice.data.SetConnections([
                            Sdf.Path(f"{chunk_root}/{node}.inputs:{attr_name}")
                            for node, attr_name in chunk_utils.parse_expression(raw)
                        ])
                        # The connections carry the refs; only this carries the operators
                        # and literals between them.
                        chunk_utils.store_expression_text(notice.data, raw)
                    else:
                        chunk_utils.clear_expression_text(notice.data)
            finally:
                chunk_notice._usd_write_active = prev_write

        elif isinstance(notice.data, Usd.Relationship) and sender.value is not None:
            # Currently just assume the string is a comma separated list of paths. This will likely
            # be changed in the future for a more robust approach
            paths = sender.value.split(",")
            if not all([Sdf.Path.IsValidPathString(x) for x in paths]):
                return
            targets = [Sdf.Path(x) for x in paths]
            notice.data.SetTargets(targets)

    @abstractmethod
    def usd_prop_changed(self):
        """
        A callback for when the UsdAttribute member was changed. This will update the value of
        the AttributeSender passed to set_usd_attr()..
        """
        if not self._usd_prop_connection:
            return
        if not isinstance(self._usd_prop_connection.sender, AttributeSender):
            return

        attr = self.get_usd_prop()
        if isinstance(attr, Usd.Attribute):
            self._usd_prop_connection.sender.value = attr.Get(0)
        elif isinstance(attr, Usd.Relationship):
            self._usd_prop_connection.sender.value = ",".join([str(x) for x in attr.GetForwardedTargets()])

    def set_usd_prop(self, prop_path: str | Usd.Property, sender: AttributeSender):
        """
        Bind this param to a specific USD property with an explicit sender (used for relationships).
        Prefer _connect_param_attr for standard attribute binding.
        """
        path = str(prop_path.GetPath()) if isinstance(prop_path, Usd.Property) else prop_path
        self._param_attr_path = path

        if self._usd_prop_connection:
            self._usd_prop_connection.disconnect()

        self._usd_prop_connection = chunk_notice.NoticeConnection(
            self.update_usd_prop, chunk_notice.ChunkNoticeAlias.ATTRIBUTE_CHANGED, sender, owner=self
        )
        self.usd_prop_changed()

    def get_usd_prop(self) -> Usd.Property | None:
        """
        Return the storage attribute this param is bound to.

        Returns:
            Usd.Property | None: The bound storage USD property, or None.
        """
        stage = self.get_stage()
        if stage and self._param_attr_path:
            return stage.GetPropertyAtPath(self._param_attr_path)

    def _connect_param_attr(self, attr: Usd.Property) -> None:
        """
        Internal: bind this param to its storage attribute for UI↔USD sync.
        After binding, re-authors any pending USD connections to driven real attrs.
        Called by _load_wire_inputs; not part of the public chunk-module API.

        Args:
            attr (Usd.Property): The storage attribute (/chunks/<name>/inputs:<param>).
        """
        path = str(attr.GetPath())
        if self._param_attr_path == path:
            return
        self._param_attr_path = path
        if not self._usd_prop_connection:
            try:
                if sender := self.get_attribute_sender("value"):
                    self._usd_prop_connection = chunk_notice.NoticeConnection(
                        self.update_usd_prop,
                        chunk_notice.ChunkNoticeAlias.ATTRIBUTE_CHANGED,
                        sender,
                        owner=self,
                    )
            except AttributeError:
                pass
        self._author_driven_connections()
        if isinstance(attr, Usd.Attribute):
            val = attr.Get(0)
            connections = attr.GetConnections()
            if not connections:
                # A ${VAR} stage expression on a non-String attr is stored resolved, with
                # its text in customData -- same split store_expression_text makes for a
                # numeric param wire. Without reading it back here the param reloads
                # holding whatever the variable happened to resolve to when it was saved,
                # and stops tracking the variable at all.
                if text := chunk_utils.read_expression_text(attr):
                    self.value = text
                elif val is not None:
                    self.value = val
            elif text := chunk_utils.read_expression_text(attr):
                self.value = text
            else:
                # Fallback for attrs authored before the text was kept in customData (and
                # for any hand-written .usda that wires connections without it): rebuild a
                # bare @a.x@@b.y@ expression from the connections. Lossy by nature — the
                # operators were never in the topology — so it is only correct for the
                # single-ref case. Only SIBLING refs are used here. A loft
                # connection points UP to this param's own container (the group's interface attr);
                # it's driven by the loft forwarding callback, not the expression system. Rebuilding
                # @Container.attr@ from it makes an unresolvable sibling ref (the "Expression ref
                # not found" spam) and would overwrite the forwarded value with the expression text.
                my_prim = Sdf.Path(path).GetPrimPath()
                sibling_conns = []
                for c in connections:
                    target_prim = Sdf.Path(str(c)).GetPrimPath()
                    if my_prim != target_prim and my_prim.HasPrefix(target_prim):
                        continue  # ancestor target == loft (upward) connection
                    sibling_conns.append(c)
                if expr := self._expression_from_connections(sibling_conns):
                    self.value = expr

    @staticmethod
    def _expression_from_connections(connections) -> Optional[str]:
        tokens = []
        for conn in connections:
            path = Sdf.Path(str(conn))
            node_name = path.GetPrimPath().name
            param_name = path.name.rsplit(":", 1)[-1]
            tokens.append(f"@{node_name}.{param_name}@")
        return "".join(tokens) if tokens else None

    def _resolve_expression_ref(self, node_name: str, param_name: str) -> Optional["ChunkCommon"]:
        chunk = self.get_chunk()
        if not chunk:
            return None
        context = chunk.get_context()
        if context:
            return context.get_param(f"{node_name}.{param_name}")
        try:
            scene = chunk_registry.ChunkRegistry().get_scene()
            target = scene.get_chunk(node_name)
            return target.get_param(param_name) if target else None
        except Exception:
            return None

    def connect_usd_attr(self, attr: Usd.Property) -> None:
        """
        Drive a real prim attribute from this param's storage attribute via a USD native
        connection. The storage attr becomes the connection source; the real attr is the target.
        If the real attr does not exist yet it will be created in _author_driven_connections once
        the storage attr is known. If the storage attr is not yet known the whole operation is
        deferred until _connect_param_attr is called.

        Args:
            attr (Usd.Property): The real prim attribute to drive (e.g. /cameras/Cam.focalLength).
                                 May be an invalid/non-existent attribute — the path is stored and
                                 the attribute is created on demand.
        """
        path = str(attr.GetPath())
        if path in self._driven_attr_paths:
            return
        self._driven_attr_paths.append(path)
        if self._param_attr_path:
            self._author_driven_connections()

    def _author_driven_connections(self) -> None:
        """
        For every path in _driven_attr_paths: ensure the attribute exists on the real prim
        (creating it with the storage attr's type if absent), then author
        real_attr.SetConnections([storage_path]).

        Always writes into the chunk's own layer (anonymous for procedural, file for
        interactive) so these connections never land in the session layer and can be
        overridden in the same layer by wire_shader_input after connect_usd_attr.
        """
        if not self._param_attr_path:
            return
        stage = chunk_registry.ChunkRegistry().scene.get_stage()
        if not stage:
            return
        storage_attr = stage.GetAttributeAtPath(Sdf.Path(self._param_attr_path))
        if not (storage_attr and storage_attr.IsValid()):
            return
        storage_path = Sdf.Path(self._param_attr_path)
        type_name = storage_attr.GetTypeName()
        chunk_layer = self.get_edit_context()

        for path in self._driven_attr_paths:
            sdf_path = Sdf.Path(path)
            real_attr = stage.GetAttributeAtPath(sdf_path)
            if not (real_attr and real_attr.IsValid()):
                prim = stage.GetPrimAtPath(sdf_path.GetPrimPath())
                if not (prim and prim.IsValid()):
                    continue
                real_attr = prim.CreateAttribute(sdf_path.name, type_name, custom=False)
            if real_attr and real_attr.IsValid():
                if chunk_layer:
                    with Usd.EditContext(stage, chunk_layer):
                        real_attr.SetConnections([storage_path])
                else:
                    real_attr.SetConnections([storage_path])

        # SetConnections only authors topology — Get() on the driven attr doesn't follow
        # it, so push the current resolved value too. Needed because connect_usd_attr is
        # (re-)called on every load/execute, e.g. after a roundtrip restores an expression
        # via _connect_param_attr before this chunk's own driven wiring runs.
        self._write_driven_attrs(stage, getattr(self, 'value', None))

    @classmethod
    def from_usd_attr(cls, attr: Usd.Attribute, *args, **kwargs) -> "ChunkUsdAttributeSupport":
        """
        Create a new parameter from a UsdAttribute and set up the connection for setting
        the attribute upon value changed on the param.

        Args:
            attr (Usd.Attribute): The attribute to represent as a param

        Returns:
            ChunkUsdAttributeSupport: A parameter that implements the ChunkUsdAttributeSupport interface
        """
        attr_name = attr.GetName()

        if (args and isinstance(args[0], str)) or kwargs.get("name"):
            param = cls(*args, **kwargs)
        else:
            kwargs["name"] = attr_name.split(":")[-1]
            param = cls(*args, **kwargs)

        if hasattr(param, "default_value"):
            if attr.HasFallbackValue():
                default_value = attr.GetMetadata("default")
            else:
                default_value = attr.Get(0)

            param.default_value = default_value  # type: ignore

        param._connect_param_attr(attr)

        return param


class ChunkParamBase(Generic[ValueT], ChunkCommon, ChunkUsdAttributeSupport):
    """
    Base class for valued parameters

    Attributes:
        layout (ChunkFlags.LayoutFlags): The layout of this parameter. Defaults to HORIZONTAL
        default_value (Optional[ValueT]): The default value of this parameter
        value (Optional[ValueT]): The value of this parameter
        custom_widget (Optional[ChunkParamWidget]): An optional custom widget. get_type() must resolve to ParamType.CUSTOM in order to use this
    """

    layout: ChunkFlags.LayoutFlags = attrs.field(default=ChunkFlags.LayoutFlags.HORIZONTAL)

    default_value: Optional[ValueT] = attrs.field(default=None)

    # Value is not an argument to __init__. Will set to default_value
    value: ValueT = attrs.field(
        default=None, init=False, metadata=AttributeMetadata(sender_alias=chunk_notice.ChunkNoticeAlias.VALUE_CHANGED)
    )

    # If possible, will use this custom widget in the ui.
    # TODO: Currently, this only works with ChunkParamBase objects with type == CUSTOM
    #       This should also work on groups and chunks... But thats a bit more complicated
    custom_widget: Optional[ChunkParamWidgetBase] = attrs.field(default=None)

    _expression_connections: list = attrs.field(
        default=attrs.Factory(list), init=False, metadata=AttributeMetadata(send_mode=SendMode.NO_SEND)
    )

    # Re-entrancy guards for expression cycles (a.p = "@b.p@" while b.p = "@a.p@").
    # Two flags, not one: the notice path calls execute(), which legitimately reads
    # self.value and so must be allowed through the read guard.
    _resolving_ref: bool = attrs.field(
        default=False,
        init=False,
        metadata=AttributeMetadata(
            copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND, validate=False, convert=False
        ),
    )
    _resolving_expr: bool = attrs.field(
        default=False,
        init=False,
        metadata=AttributeMetadata(
            copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND, validate=False, convert=False
        ),
    )

    def convert_attr(self, attr: attrs.Attribute, val: Any) -> Any:
        if attr.name == "value":
            if val is None:
                return self.default_value
            if chunk_utils.is_expression_text(val):
                # Either kind: a @Node.param@ wire or a ${VAR} stage expression. Both are
                # text a numeric param legitimately holds -- the number appears when the
                # value is READ (__getattribute__ resolves it), so converting here would
                # destroy the text that makes the value track its source at all.
                return val  # Store expression strings as-is without type conversion

        if attr.name == "default_value":
            supported = ChunkFlags.ParamTypeToTypes(self.get_type())

            if not supported:
                return val

            type_default = next(iter(supported))

            if val is None:
                return type_default()

            if type(val) not in supported:
                # When the value does not match the type_default, we can try to convert it. If failed, use the type default
                try:
                    # Try casting the type_default to val
                    return type_default(val)
                except Exception:
                    # Casting failed, so lets just use the type_default
                    return type_default()

        return super().convert_attr(attr, val)

    def validate_attr(self, attr: attrs.Attribute, val: Any) -> bool:
        if attr.name == "value" and val is not None:
            if chunk_utils.is_expression_text(val):
                return True  # Expression strings bypass type checking; resolved in value_changed
            expected_type = ChunkFlags.ParamTypeToTypes(self.get_type())

            t = type(val)
            if t is AttributeSender:
                if not val.value:
                    return True
                t = type(val.value)

            if t not in expected_type:
                raise ValueError(
                    f"Value of {val} is not supported for attribute {attr.name}, type is {t}. Expects {expected_type}"
                )
            return True

        if attr.name == "custom_widget":
            # Custom widget should be a subclass of ChunkParamWidget
            if val is not None and not isinstance(val, ChunkParamWidgetBase):
                raise ValueError(f"custom_widget {val} is not a subclass of ChunkParamWidget")
            return True
        return super().validate_attr(attr, val)

    @classmethod
    def get_type(cls) -> ChunkFlags.ParamType:
        """Get the ParamType for this parameter.
        If this returns a combination of ParamType enums, the subclass should also implement evaluate_type() to evaluate the runtime type.
        Required by values parameters

        Returns:
            ChunkFlags.ParamType: The type of this parameter. Defaults to NONE, but should be overwritten in subclasses
        """
        return ChunkFlags.ParamType.NONE

    def evaluate_type(self) -> ChunkFlags.ParamType:
        """Get the runtime ParamType for this parameter. If get_type() supports multiple types (FLOAT_SLIDER | INT_SLIDER), then this function should
        evaluate what type is currently used.

        Returns:
            ChunkFlags.ParamType: The runtime type of this parameter. Uses get_type() by default
        """
        return self.get_type()

    def get_notice_alias(self):
        return chunk_notice.ChunkNoticeAlias.PARAM_CHANGED

    def _cross_chunk_ref(self, other: "ChunkCommon") -> "tuple[ChunkBase, ChunkBase] | None":
        """Return (my chunk, other's chunk) when the two params live on different chunks.

        None means "same chunk, or not resolvable yet" — a param still being constructed
        has no owner to walk up to, and warning about an unresolved owner would fire on
        every list-group template and every param built before it is added to anything.
        """
        mine, theirs = self.get_chunk(), other.get_chunk()
        if mine is None or theirs is None or mine is theirs:
            return None
        return mine, theirs

    def _expression_ref_to(self, other: "ChunkCommon", other_chunk: "ChunkBase") -> "str | None":
        """The @Node.param@ text that would reference other, or None if it can't be named.

        The grammar is @word.word@ (chunk_utils._EXPR_RE), so the reference carries a bare
        param name — which _resolve_expression_ref then looks up with get_param. A nested
        param is fine (get_param finds it by name), but a name shared by two params in the
        same chunk resolves to whichever comes first, so check that the lookup actually
        lands back on this param before promising an expression can address it.
        """
        if other_chunk.get_param(other.name) is not other:
            return None
        return f"@{other_chunk.name}.{other.name}@"

    def add_dependency(self, input: "chunk_notice.ChunkNoticePropagator") -> int:
        """Add a DagNode input, warning if it crosses a chunk boundary.

        A param-level dependency is in-memory only: ChunkStageSync._on_dependency_added
        authors inputs:chunk for ChunkBase pairs and returns early for anything else, so
        an edge between two params is never written to USD, draws no wire in the node
        graph, and is gone after a reload. Within one chunk that is exactly right — this
        is the dirty/ordering edge behind set_function, and it can drive attributes
        (menu_items, a label, visibility) that no expression can reach. Across two chunks
        it silently loses the connection the caller thought they made; an expression is
        the mechanism that persists. See connect_value, which redirects rather than warns.
        """
        if isinstance(input, ChunkParamBase) and (pair := self._cross_chunk_ref(input)):
            mine, theirs = pair
            ref = self._expression_ref_to(input, theirs) or f"@{theirs.name}.<param>@"
            logger.warning(
                "Param dependency %s.%s <- %s.%s crosses chunks: it is not authored to USD "
                "and will not survive a reload. Use an expression instead: %s.%s = \"%s\"",
                mine.name, self.name, theirs.name, input.name, mine.name, self.name, ref,
            )
        return super().add_dependency(input)

    def connect_value(self, other: "ChunkParamBase[ValueT]"):
        my_t = self.evaluate_type()
        other_t = other.evaluate_type()
        if my_t not in other_t:
            raise ValueError(f"Can't connect {other_t.name} to parameter {my_t}")

        # Across chunks, do what the caller asked for in the form that survives: an
        # expression authors the USD connection, draws the wire, and is patched on rename,
        # where the DagNode edge below would quietly vanish on the next load. The type
        # check above still ran, which the raw expression path skips.
        if pair := self._cross_chunk_ref(other):
            if ref := self._expression_ref_to(other, pair[1]):
                logger.debug(
                    "connect_value across chunks (%s -> %s): authoring %s",
                    pair[1].name, pair[0].name, ref,
                )
                self.value = ref
                return

        idx = -1
        # If other already is connected to self, then we can use that index
        for i, node in self.get_dependencies(is_inst(type(other))):
            if node == other:
                idx = i
                break

        if idx == -1:
            # The nodes are not connected, so we add other as dependency
            idx = self.add_dependency(other)
        self.set_function("value", lambda x, _: x.get_dependency(idx, is_inst(type(other))).value)

    def value_changed(self, attrname: str, old_value: Any, value: Any):
        if attrname == "default_value" and hasattr(self, "value") and (self.value is None or self.value == old_value):
            self.value = value

        if attrname == "value":
            self._expression_connections = []
            if isinstance(value, str) and chunk_utils.has_expression(value):
                for node_name, param_name in chunk_utils.parse_expression(value):
                    ref = self._resolve_expression_ref(node_name, param_name)
                    if ref is None:
                        # During a load this is the expected first outcome, not a fault:
                        # values are restored before their chunk is registered, so a
                        # sibling ref cannot resolve yet. load_chunks retries once the
                        # batch is registered and reports whatever is still dangling
                        # then, so warning here would only ever be a false alarm.
                        if chunk_notice._loading_stage is not None:
                            logger.debug(f"Expression ref @{node_name}.{param_name}@ not resolvable yet (loading)")
                        else:
                            logger.warning(f"Expression ref @{node_name}.{param_name}@ not found")
                        continue
                    conn = chunk_notice.NoticeConnection(
                        method=self._on_ref_changed,
                        alias=chunk_notice.ChunkNoticeAlias.VALUE_CHANGED,
                        sender=ref.get_attribute_sender("value"),
                    )
                    self._expression_connections.append(conn)

        return super().value_changed(attrname, old_value, value)

    def _on_ref_changed(self, notice=None, sender=None):
        if self._resolving_ref:
            # An expression cycle. Each ref installs a NoticeConnection back to this
            # method, so a.p = "@b.p@" with b.p = "@a.p@" bounces between the two senders
            # until the interpreter runs out of stack. Stop at the second visit rather
            # than raise: unlike the DagNode side (topo_order raises CircularDependency
            # Error on a solve), this fires from notice dispatch during ordinary editing,
            # where the useful outcome is a settled value plus a diagnosable warning.
            logger.warning(
                "Expression cycle at %s: %r refers back to itself through another param; "
                "leaving it at its previous value",
                self.name, self.__dict__.get("value"),
            )
            return
        self._resolving_ref = True
        try:
            attr_sender = self.get_attribute_sender("value")
            attr_sender.old_value = self.__dict__.get("value")
            attr_sender.execute()
        finally:
            self._resolving_ref = False

    @property
    def raw_value(self) -> Any:
        """The stored value before expression evaluation. Use this for display purposes."""
        return self.__dict__.get("value")

    def __getattribute__(self, name: str) -> Any:
        if name == "value" and "value" in self.__dict__:
            raw = self.__dict__["value"]
            if isinstance(raw, str) and chunk_utils.has_expression(raw):
                raw = self._resolve_expression_string(raw)
            # Second, and in this order: a param wire can deliver a string that itself
            # carries a ${VAR}, so the @refs@ have to be substituted before the stage
            # variables are. The reverse order would leave a variable arriving through a
            # wire unexpanded.
            if isinstance(raw, str) and chunk_utils.has_stage_expression(raw):
                return self._resolve_stage_expression(raw)
            return raw
        return super().__getattribute__(name)

    def _resolve_stage_expression(self, expr: str) -> Any:
        """Substitute the stage's expression variables into expr.

        Separate from the @Node.param@ machinery above and deliberately much simpler: a
        stage variable is a value on the stage, not a param, so there is no ref to resolve,
        no NoticeConnection to install and no cycle to guard against. The dict is read on
        every access rather than cached, which is what makes changing a variable take
        effect everywhere at once without anything having to be invalidated.

        A numeric param gets the same arithmetic treatment an expression does: USD hands
        back a typed value for a fully-delimited expression, but the loose ${VAR} form
        substitutes text, and "${COUNT}" on a Slider has to read back as a number or every
        consumer downstream -- the USD attr write included -- gets a str.
        """
        resolved = chunk_utils.resolve_stage_expression(expr, self.get_stage())
        if isinstance(resolved, str) and self._expression_is_numeric():
            if (number := chunk_utils.eval_arithmetic(resolved)) is not None:
                types = ChunkFlags.ParamTypeToTypes(self.evaluate_type())
                return int(number) if types and types[0] is int else float(number)
        return resolved

    def _resolve_expression_string(self, expr: str) -> Any:
        if self._resolving_expr:
            # Reading a cyclic expression re-enters through the ref's own .value
            # (__getattribute__ resolves it), so break the loop with the default. Only
            # debug: a value read happens on every widget paint, and a cycle would turn
            # the warning in _on_ref_changed into a flood.
            logger.debug("Expression cycle while resolving %s — falling back to default", self.name)
            return self.__dict__.get("default_value")
        self._resolving_expr = True
        try:
            return self._resolve_expression_string_uncached(expr)
        finally:
            self._resolving_expr = False

    def _resolve_expression_string_uncached(self, expr: str) -> Any:
        tokens = chunk_utils.parse_expression(expr)
        if len(tokens) == 1 and expr.strip() == f"@{tokens[0][0]}.{tokens[0][1]}@":
            ref = self._resolve_expression_ref(*tokens[0])
            return ref.value if ref is not None else self.__dict__.get("default_value")
        result = expr
        for node_name, param_name in tokens:
            ref = self._resolve_expression_ref(node_name, param_name)
            val = str(ref.value) if ref is not None and ref.value is not None else ""
            result = result.replace(f"@{node_name}.{param_name}@", val)

        # On a numeric param the substituted string is meant as a sum, not as text:
        # "@a.x@ * @b.y@" becomes "20.0 * 3.0" above and has to come back as 60.0, or the
        # param reads as a str and every consumer downstream (the USD attr write included)
        # gets the wrong type. String params keep interpolation — that is what builds a
        # path like "@A.primpath_location@/geo" — and eval_arithmetic returns None for
        # anything that isn't pure arithmetic, so a numeric param holding something
        # unevaluable still falls through to the substituted text rather than raising.
        if self._expression_is_numeric():
            if (number := chunk_utils.eval_arithmetic(result)) is not None:
                types = ChunkFlags.ParamTypeToTypes(self.evaluate_type())
                return int(number) if types and types[0] is int else float(number)
        return result

    def _expression_is_numeric(self) -> bool:
        """True if this param holds a number, so an expression on it should be computed."""
        types = ChunkFlags.ParamTypeToTypes(self.evaluate_type())
        return bool(types) and all(t in (int, float) for t in types)


class ChunkContainerBase(ChunkCommon):
    """
    A base class for container type parameters such as Vectors, Groups and Chunks. The container can hold
    parameters (ChunkParamBase) or other ChunkContainerBase objects.

    Attributes:
        children (NameSyncedContainer): The underlying container owning the child parameters
        widget_factory (Optional[ChunkWidgetFactoryBase]): A factory that the ui implementation can use for
            changing the default ui implementation of the container and its children
    """

    children: NameSyncedContainer[ChunkCommon] = attrs.field(
        init=False,
        factory=NameSyncedContainer[ChunkCommon],
        metadata=AttributeMetadata(send_mode=SendMode.NO_SEND, copy_mode=CopyMode.COPY),
    )

    widget_factory: Optional[ChunkWidgetFactoryBase] = attrs.field(
        default=None,
        validator=attrs.validators.optional(attrs.validators.instance_of(ChunkWidgetFactoryBase)),
        metadata=AttributeMetadata(copy_mode=CopyMode.SHARE),
    )

    def __attrs_post_init__(self):
        super().__attrs_post_init__()
        self.children.set_owner(self)

    def get_notice_alias(self) -> chunk_notice.ChunkNoticeAlias:
        return chunk_notice.ChunkNoticeAlias.GROUP_CHANGED

    def data_changed(self, notice: chunk_notice.ChunkNotice | None = None) -> chunk_notice.ChunkNotice | None:
        if isinstance(notice, chunk_notice.AttributeStateChangeNotice) and notice.data.get_owner() == self:
            # If this container holds nested chunks, and the DISABLED state was switched, we should propagate the state
            # to all nested chunks. This is done because the chunk layers are muted/unmuted based on the ENABLED state,
            # and if parent chunks are DISABLED and nested chunks are not, the nested chunk layer will not be muted
            old_state = ChunkFlags.State(notice.data.old_value)
            new_state = ChunkFlags.State(notice.data.value)
            added = new_state ^ (new_state & old_state)
            removed = old_state ^ (new_state & old_state)

            if ChunkFlags.State.DISABLED in (added | removed):

                def _pred(val) -> TypeGuard[ChunkBase]:
                    return isinstance(val, ChunkBase)

                for cc in self.get_params(_pred):
                    if ChunkFlags.State.DISABLED in added:
                        cc.set_state(ChunkFlags.State.DISABLED)
                    else:
                        cc.unset_state(ChunkFlags.State.DISABLED)

        return super().data_changed(notice)

    def remove_param_or_group(self, param: ChunkCommon):
        """
        Remove a child on this group

        Args:
            param (ChunkCommon): The child to remove
        """

        own_p = self.children.pop(param)
        if not own_p:
            return

        own_p.set_owner(None)

    def request_delete(self):
        """
        Request delete this container, and subsequently clear its children
        """

        # Retire from module execution up here, not in the super() call at the end: the child
        # loop below detaches this container's own params, and its PARAM_REMOVED notices can
        # re-enter the DAG solve long before super().request_delete() would set the flag. See
        # ChunkCommon.request_delete for what the flag protects against.
        self._deleting = True

        # Call request_delete on all children. This will send PARAM_REMOVED on the removed
        # children.
        #
        # get_my_params(), not get_params(): the latter walks the whole nested ChainMap, so a
        # context would delete a grandchild param (a sub-chunk's own Menu/Slider) directly,
        # leaving that sub-chunk alive but gutted — get_param() returning None on a chunk that
        # was never asked to delete, and so never marked _deleting. Anything the teardown then
        # cascades into (reconcile_edit_targets re-running an Edit Target, a refresh callback)
        # crashes on the missing param. Deleting only direct children keeps the order right:
        # each child container tears down its own subtree, marking itself first.
        for param in self.get_my_params():
            param.request_delete()

        self.children.clear()
        self.children.set_owner(None)

        super().request_delete()

    @overload
    def get_param(self, param: ChunkCommon | str | HashVal) -> Optional[ChunkCommon]: ...

    @overload
    def get_param(self, param: ChunkCommon | str | HashVal, predicate: Optional[Predicate[ValueT]]) -> ValueT: ...

    def get_param(
        self, param: ChunkCommon | str | HashVal, predicate: Optional[Predicate[ValueT]] = None
    ) -> Optional[ChunkCommon] | ValueT:
        """
        Get a parameter or group inside of this group. A string can be passed to get a parameter by name.
        The string can also be the explicit hierarchical identifier of a param by spacing the hiearchy levels with a dot

        ``` py
        myChunk.get_param("groupA.groupB.param")
        ```

        If just a name is passed, the first found param with said name will be returned. A ChunkGroupBase object enforce that
        it's own child params have unique names. However, the names must not be unique to nested parameters. For this reason,
        multiple params with the same name may exist in a hierarchy.

        A Predicate function can be used to narrow the result type and validates the result:
        ``` py
        p = myChunk.get_param("aSlider", chunk.is_ins(chunk.Slider))
        p.value = 10
        ```

        Args:
            param (ChunkParamBase | str): A parameter to find. If string is passed, look for param with name
            predicate (Optional[Predicate[ValueT]]): A predicate function used to check and narrow the type of the parameter

        Returns:
            Optional[ChunkCommon]: The found parameter or None if not found
            ValueT: The found parameter matching the predicate if supplied

        """

        if isinstance(param, (ChunkCommon, HashVal, ProxyParam)):
            return self.children.get_param(param, predicate)
        elif isinstance(param, str):
            return self.children.get_by_name(param, predicate)

    @overload
    def get_params(self) -> Tuple[ChunkCommon, ...]: ...

    @overload
    def get_params(self, predicate: Optional[Predicate[ValueT]]) -> Tuple[ValueT, ...]: ...

    def get_params(self, predicate: Optional[Predicate[ValueT]] = None) -> Tuple[ChunkCommon, ...] | Tuple[ValueT, ...]:
        """Get all parameters in this group including nested

        Args:
            predicate (Optional[Predicate[ValueT]]): An optional predicate function to filter the result. Defaults to None.

        Returns:
            Tuple[ChunkCommon, ...]: All parameters in this group
            Tuple[ValueT, ...]: All parameters in this group matching the predicate, if supplied
        """
        return self.children.get_params(predicate)

    @overload
    def get_my_params(self) -> Tuple[ChunkCommon, ...]: ...

    @overload
    def get_my_params(self, predicate: Optional[Predicate[ValueT]]) -> Tuple[ValueT, ...]: ...

    def get_my_params(
        self, predicate: Optional[Predicate[ValueT]] = None
    ) -> Tuple[ChunkCommon, ...] | Tuple[ValueT, ...]:
        """Get all parameters added directly to this group

        Args:
            predicate (Optional[Predicate[ValueT]]): An optional predicate function to filter the result. Defaults to None.

        Returns:
            Tuple[ChunkCommon, ...]: All parameters owned by this group
            Tuple[ValueT, ...]: All parameters owned by this group matching the predicate, if supplied
        """
        return self.children.get_my_params(predicate)

    def check_hierarchy(self):
        """
        A debug function for checking the hierarchy structure of a group.

        Raises:
            TypeError: An owner is not set to a child of this group
            TypeError: The owner of a child is not a ChunkNoticePropagator
            ValueError: The owner of a child does not itself hold the child
            NameError: Duplicate chunk in the hierarchy
            NameError: Duplicate name in the hierarchy
        """
        failed_msg = f"check_hierarchy for {self.name} failed! \n"
        failed_msg += " Reason: {}"

        found_params: set[ChunkCommon] = set()

        # Names are checked among siblings, not across the whole hierarchy: a param is
        # addressed relative to its owner, so two groups may each hold a "General"
        # without either becoming unreachable. Renderer settings are the case that
        # proves it -- every installed renderer gets a group and every one of them ends
        # up with a General for its leftovers.
        #
        # Keyed by id() rather than by the owner itself, so this never depends on how a
        # param defines equality: two distinct groups that happen to compare equal must
        # still be two scopes.
        found_names: Dict[int, set[str]] = {}

        for p in self.get_params():
            if isinstance(p, ChunkBase):
                # Chunks (inherit from ChunkAttributes) do not need owners
                continue

            owner = p.get_owner()
            if isinstance(owner, NameSyncedContainer):
                # Get the owner of the container, i.e the group or chunk
                owner = owner.get_owner()

            if not owner:
                raise TypeError(
                    failed_msg.format(f"{p.name} does not have an owner. All params except Chunks should have an owner")
                )

            if not isinstance(owner, chunk_notice.ChunkNoticePropagator):
                raise TypeError(failed_msg.format(f"{p.name}'s owner is not a ChunkNoticePropagator object"))

            if isinstance(owner, ChunkContainerBase) and not isinstance(owner, ChunkListGroupBase):
                if p not in [x._param_ref if isinstance(x, ProxyParam) else x for x in owner.get_my_params()]:
                    raise ValueError(
                        failed_msg.format(f"{p.name} has owner: {owner.name}. Owner does not hold this param")
                    )

            if p in found_params:
                raise NameError(failed_msg.format(f"{p.name} is duplicated in the hierarchy"))
            found_params.add(p)

            direct_owner = p.get_owner()
            if isinstance(direct_owner, NameSyncedContainer):
                direct_owner = direct_owner.get_owner()

            siblings = found_names.setdefault(id(direct_owner), set())
            if p.name in siblings:
                owner_name = getattr(direct_owner, "name", "<no owner>")
                raise NameError(
                    failed_msg.format(f"param name: {p.name} is not unique under {owner_name}")
                )
            siblings.add(p.name)

    def print_hierarchy(self):
        """
        A debug function printing the hierarchy of the group and its children
        """

        def inset(i):
            return "." * i * 4

        max_inset = 15

        msg = f"\n{'-' * max_inset}* {self.name} hierarchy *{'-' * max_inset}\n\n"
        for indent, p in self.iter_hierarchy():
            cls_name = f"[{p.__class__.__name__}]"
            if len(cls_name) > max_inset:
                cls_name = f"[{p.__class__.__name__[0 : max_inset - 1]}]"

            offset = "." * (max_inset - len(cls_name))

            msg += f"{cls_name}{offset}{inset(indent)}{p.name}"
            if isinstance(p, ChunkParamBase):
                msg += f" -> {p.evaluate_type().name}"

            owner = p.get_owner()
            if isinstance(owner, NameSyncedContainer):
                # Get the owner of the container, i.e the group or chunk
                owner = owner.get_owner()

            if owner and isinstance(owner, ChunkContainerBase):
                msg += f". Owner = ({owner.name})"

            msg += f". Id = ({hex(id(p))})"
            msg += "\n"

        self.get_logger().info(msg)

    def iter_hierarchy(self, level=0) -> Generator[Tuple[int, ChunkCommon], None, None]:
        """
        Provides a generator for iterating over the hierarchy, starting from self,
        going to all children and recursively through nested groups.

        Args:
            level (int, optional): A value saying what level of the hierarchy the yielded value come from, starting at 0. Defaults to 0.

        Yields:
            Tuple[int, ChunkCommon]:
        """
        yield level, self
        for p in self.get_my_params():
            if isinstance(p, ChunkGroupBase):
                yield from p.iter_hierarchy(level + 1)
            else:
                yield level + 1, p


class ChunkGroupBase(ChunkContainerBase):
    """
    A base class for dynamic group objects. Chunk and Groups are dynamic in the sense that they can add an arbitrarily number of
    children of direfent types. Vectors on the other hand should not subclass from this class as they are not dynamic in the
    same sense.

    """

    def add_params(self, params: Iterable[ChunkT], force_unique: bool = True, clone: bool = False) -> List[ChunkT]:
        """Adds parameters to this group. Will call add_param on all params in the list

        Args:
            params (Iterable[ChunkCommon]): List of chunk objects to add to this gruo
            force_unique (bool, optional): Force unique param names. Defaults to True.
            clone (bool, optional): Clones all params. Defaults to True.

        Returns:
            List[ChunkCommon]: A list of the added parameters. Will differ from the input params if copies were made
        """
        new_params = []
        for param in params:
            if clone:
                p = self.add_param(param.clone(), force_unique)
            else:
                p = self.add_param(param, force_unique)
            new_params.append(p)
        return new_params

    def add_param(self, param: ChunkT, force_unique: bool = True) -> ChunkT:
        """
        Adds a parameter to this group. The new param will be added to the 'children' NameSyncedContainer attribute.
        Any modifications of the param will propagate to this group as they will share the same hierarchy. See the
        ChunkNoticePropagator for more information on how this is implemented. Adding mupliples of the same param,
        consider using:

        ``` py
        group.add_param(param.clone())
        ```
        or, for multiple params at once:
        ``` py
        group.add_params([param, param.clone(), param.clone()])
        ```

        Args:
            param (ChunkT): A parameter to add. May also be another ChunkContainer, which will nest the group
            force_unique (bool): If a param already exists in the hierarchy, we most often do not want a direct duplicate.
                This enables the check, and if a duplicate exists, raises a ValueError. However, if the hash is unique but
                the name is a duplicate, this will resolve the name conflict by incrementing a numerical suffix. If set to False,
                this check is never performed, instead any duplicate will simply replace the old parameter. Defaults to True

        Returns:
            ChunkT: The param

        Raises:
            ValueError: If the param is a direct duplicate (hash exists already)
        """

        new_param = None
        if force_unique:
            new_param = self.resolve_new_param(param)

        if new_param is None or new_param is param:
            new_param = param

        if isinstance(new_param, ChunkCommon):
            # Add the new param to self.children container. The owner/ownee relationship is handled
            # by the container, so should not modify owner here
            self.children.add(new_param)

            if isinstance(new_param, ChunkContainerBase):
                # Also append the children of the group, as this allows for fast lookups accross the layers
                self.children.add_nested(new_param.children)

        else:
            raise ValueError("Chunks only support params inherited from ChunkCommon")
        return new_param

    def resolve_new_param(self, param: ChunkT) -> Optional[ChunkT]:
        """
        Return a copy of a param or group that does not conflict with other child on this group. Raises an error
        if a param with the same hash already exists

        Args:
            param (ChunkParamBase): A ChunkParam or ChunkContainer object to copy

        Returns:
            ChunkT: If the parameter was valid, return the parameter but with posibly a new name as to not fix name conflicts

        Raises:
            ValueError: If a duplicate was found
        """
        if param in self.get_params():
            raise ValueError(f"A duplicate for param {param.name} was found on container {self.name}")

        new_param = param

        if not isinstance(param, ProxyParam):
            reserved_names = self.get_reserved_names()

            new_name = self.get_valid_child_name(param.name, reserved_names)
            new_param.name = new_name

            reserved_names.add(new_name)

            if isinstance(new_param, ChunkGroupBase):
                for p in new_param.get_params():
                    if not isinstance(p, ProxyParam):
                        new_name = self.get_valid_child_name(p.name, reserved_names)
                        reserved_names.add(new_name)
                        p.name = new_name

        return new_param

    def get_reserved_names(self) -> Set[str]:
        """Get a set of reserved names. These are the names of all parameters and groups under this group + this groups name.
        A new parameters name should not exist in the set in order to be valid.

        Returns:
            Set[str]: A set of reserved group and parameter names which new parameters should not be named
        """
        return set(self.children.get_my_names()) | set([self.name])

    @staticmethod
    def get_valid_child_name(orig_name: str, reserved_names: Set[str]) -> str:
        """Get a unique name from orig_name that does not intersect reserved_names by adding a numerical suffix to duplicate names

        Args:
            orig_name (str): The name to make valid
            reserved_names (Set[str]): A list of reserved names. This paramater can't have a name
                which exist in this list. The new, valid name will be appended to this list to nest a group inside an already created group

        Returns:
            str: a unique name
        """

        suffix = ""

        if m := re.match("(.*?)([0-9]+)$", orig_name):
            orig_name = m.group(1)
            suffix = m.group(2)

        idx = int(suffix) if suffix else 1
        max_iter = 1000

        name = orig_name + suffix

        while name in reserved_names:
            name = orig_name + str(idx)
            idx += 1
            if idx > max_iter:
                raise RuntimeError(f"Failed to resolve name, loop exceeded {max_iter} iterations")
        return name


class ChunkListGroupBase(ChunkGroupBase):
    """
    A base group for list style container. In addition to the containers children, the group also hold a list of groups that in turn holds
    proxies to the children. In a ui implementation, the 'children' should be hidden, and instead the proxy_params should be used.
    The proxy parameters are practically identical to their referenced parameters, with the exception of their 'value' attributes. If changing
    the name of the referenced parameter, all proxies will reflect this change and also get a new name. Getting and setting values of the
    proxies can be done similarly to a regular parameter, but access to said proxies should be done through get_indexed_param() rather than
    get_param().

    get_params() and get_my_params() are overridden to return the proxy parameters. get_param() is not. The destinction being that get_param()
    will return from the 'children' container. i.e the container which the proxies references. To modify all proxies simultaneously one can modify
    the referenced parameters with get_param() and similarly to add a parameter to all instances, use add_param().

    Attributes:
        size (int): The size of the list. This can be modified at runtime to extend / shrink the list
        proxy_params (List[ChunkGroupBase]): A list of groups holding the proxy parameters. This is managed internally, and should not be modified
            externally
    """

    size: int = attrs.field(default=0)
    proxy_params: List[ChunkGroupBase] = attrs.field(factory=list, init=False)

    @overload
    def get_indexed_param(self, param: ChunkCommon | str | HashVal, index: int) -> Optional[ChunkCommon]: ...
    @overload
    def get_indexed_param(
        self, param: ChunkCommon | str | HashVal, index: int, predicate: Optional[Predicate[ValueT]] = None
    ) -> ValueT: ...

    def get_indexed_param(
        self, param: ChunkCommon | str | HashVal, index: int, predicate: Optional[Predicate[ValueT]] = None
    ) -> Optional[ChunkCommon] | ValueT:
        """
        Get a param from the proxy list. The index should be an index of the proxy_params list. If found, The returned
        value is a ProxyParam referencing the param input, at index.

        Args:
            param (ChunkCommon | str | HashVal): The param to look for. If a string is supplied, the parameters name will be used for equality check
            index (int): The index in to proxy_params which to get the parameter from
            predicate (Optional[Predicate[ValueT]]): A predicate function to use for filtering the params. Defaults to None

        Returns:
            Optional[ChunkCommon]: The proxy parameter referencing param
            ValueT: The proxy parameter referencing param, matching the predicate if predicate is not None

        Raises:
            IndexError:
                If the index is out of bounds
        """

        if len(self.proxy_params) == 0 and not predicate:
            return

        if index >= len(self.proxy_params):
            raise IndexError("Index out of range")

        res = self.proxy_params[index].get_param(param)
        if not predicate or predicate(res):
            return res

        raise ValueError(f"Predicate {predicate} failed for get_indexed_param with index {index}")


    def value_changed(self, attrname: str, old_value: Any, value: Any):
        if old_value is None or attrname != "size":
            return super().value_changed(attrname, old_value, value)

        if value > old_value:
            for i in range(old_value, value):
                # No LABELED: the index string ("0", "1", ...) as a row header serves
                # little purpose and just costs vertical space — the row's own fields
                # already identify it (e.g. a name/label field, per-row). Inherit the
                # list's own layout (e.g. HORIZONTAL) so a list author can lay each row's
                # fields out side by side instead of always stacked.
                group = ChunkGroupBase(str(i), flags=ChunkFlags.GroupFlags.NONE, layout=self.layout)
                group.set_owner(self)
                self.proxy_params.append(group)

                NameSyncedContainer.create_proxy(self.children, group.children)
                self.send_self(chunk_notice.ChunkNoticeAlias.PARAM_ADDED, group)

        elif value < old_value and len(self.proxy_params) > value:
            # Pop from the tail inward: popping ascending indices while iterating shifts
            # every later element down one, so proxy_params[i] would run off the end (and
            # delete the wrong rows). Removing the highest index first keeps the lower ones
            # stable. Clamp the top to the actual length in case it's already shorter.
            for i in range(min(old_value, len(self.proxy_params)) - 1, value - 1, -1):
                self.proxy_params[i].request_delete()
                self.proxy_params.pop(i)

        if value != old_value:
            for i, grp in enumerate(self.proxy_params):
                grp.name = str(i)

        return super().value_changed(attrname, old_value, value)

    @overload
    def get_params(self) -> Tuple[ChunkCommon, ...]: ...

    @overload
    def get_params(self, predicate: Optional[Predicate[ValueT]] = None) -> Tuple[ValueT, ...]: ...

    def get_params(self, predicate: Optional[Predicate[ValueT]] = None):
        res = []
        for container in self.proxy_params:
            res.extend(container.get_params(predicate))
        return tuple(res)

    def get_my_params(self, *args, **kwargs) -> Tuple[ChunkGroupBase, ...]:
        return tuple(self.proxy_params)

    def remove_index(self, index: int):
        """
        Remove an index from the proxy_params list. This will decrament the 'size' attribute by 1, and
        call request_delete() on the group at index. If The index is out of range, then print the error
        and return

        Args:
            index (int): The index to remove
        """
        try:
            param = self.proxy_params[index]
            param.get_dag_node().disconnect()

            param.request_delete()
            self.proxy_params.pop(index)

        except IndexError as e:
            self.get_logger().warning("Failed to remove index from list. Index out of range %s", e)
            return

        self.size -= 1

    def add_param(self, param: ChunkT, force_unique: bool = True) -> ChunkT:
        param = super().add_param(param, force_unique)

        for proxy_group in self.proxy_params:
            proxy_group.add_param(ProxyParam(param))  # type: ignore
        return param


class ChunkBase(ChunkGroupBase):
    """
    Base class for Chunks. Similar to a group but meant to be the top level owner of other params.
    """

    # The config is set from the chunk_registry.ChunkModule.load_config() function.
    # This is because the config can be loaded independently from the chunk, without
    # having to execute main() of the chunk module.
    _config: Optional["ChunkConfig"] = attrs.field(default=None, init=False, metadata=AttributeMetadata(serialize=True))

    # For CONTEXT chunks: the USD prim path this context maps to. Chunks within
    # the context are placed at {prim_path}/chunks/{chunk_name}. Falls back to
    # _CHUNKS_ROOT when None.
    prim_path: Optional[str] = attrs.field(default=None)

    def __attrs_post_init__(self):
        super().__attrs_post_init__()
        self.add_callback(
            self.disable,
            notice_alias=chunk_notice.ChunkNoticeAlias.MENU_ACTION_NOTICE,
            send_on_create=False,
            private=True,
        )
        self.add_callback(
            self.enable,
            notice_alias=chunk_notice.ChunkNoticeAlias.MENU_ACTION_NOTICE,
            send_on_create=False,
            private=True,
        )
        self.add_callback(
            self.remove,
            notice_alias=chunk_notice.ChunkNoticeAlias.MENU_ACTION_NOTICE,
            send_on_create=False,
            private=True,
        )
        self.add_callback(
            self.trigger_on_create_callbacks,
            notice_alias=chunk_notice.ChunkNoticeAlias.CHUNK_LOADED,
            send_on_create=False,
            private=True,
        )

    def convert_attr(self, attr: attrs.Attribute, val: Any) -> Any:
        # Sanitize each path segment so prim_path stays a valid USD path, but leave None
        # alone — otherwise str(None) makes prim_path the string "None", which is truthy
        # and Sdf.Path("None") collapses to an invalid empty path (breaks chunkgroups /
        # _resolve_chunk_root).
        #
        # Used to only strip spaces, which is the one illegal character a name happened to
        # hit first. Anything else — a comma or a hyphen in a context's display name
        # ("SO-101 Bench", "One Stage, Three Shots") — produced a prim_path Sdf.Path
        # silently refuses to compose into a real prim: the tab looked created, but no
        # chunk placed "under" it ever actually had anywhere to live. Tf.MakeValidIdentifier
        # is USD's own routine for this and handles every illegal character, not just one;
        # applied per path segment (split on "/") so it sanitizes names, not the slashes
        # that separate them.
        if attr.name == "prim_path" and val is not None:
            text = str(val)
            return "/".join(Tf.MakeValidIdentifier(part) if part else part for part in text.split("/"))
        return super().convert_attr(attr, val)

    def start_gizmo(self, notice=None, sender=None):
        self.send_self(chunk_notice.ChunkNoticeAlias.GIZMO_START)

    def stop_gizmo(self, notice=None, sender=None):
        self.send_self(chunk_notice.ChunkNoticeAlias.GIZMO_STOP)

    def disable(self, notice=None, sender=None):
        self.set_state(ChunkFlags.State.DISABLED)

    def enable(self, notice=None, sender=None):
        self.unset_state(ChunkFlags.State.DISABLED)

    def remove(self, notice=None, sender=None):
        self.request_delete()

    def trigger_on_create_callbacks(self, notice=None, sender=None):
        """
        Call trigger() on all callbacks in the container from get_on_create_callbacks(). This function should be called once the chunk is loaded
        properly and added to the scene. This is a callback that is called when the chunk is loaded ( CHUNK_LOADED is sent ).
        """
        for _, child in self.iter_hierarchy():
            for cb in child.get_on_create_callbacks():
                if cb.alias == chunk_notice.ChunkNoticeAlias.CHUNK_LOADED:
                    continue
                cb.trigger()

    def get_config(self) -> Optional["ChunkConfig"]:
        return self._config

    def get_edit_context(self) -> "Sdf.Layer | None":
        if self._config and self._config.interactive:
            return None
        return super().get_edit_context()

    def get_chunk(self) -> Optional["ChunkBase"]:
        return self

    def get_notice_alias(self) -> chunk_notice.ChunkNoticeAlias:
        return chunk_notice.ChunkNoticeAlias.CHUNK_CHANGED

    def add_param(self, param: ChunkT, force_unique: bool = True, notify: bool = True) -> ChunkT:
        do_context_setup = notify and self.has_flag(ChunkFlags.GroupFlags.CONTEXT) and isinstance(param, ChunkBase)
        if do_context_setup:
            # Clear the chunk's edit layer to a fresh slate BEFORE adding — adding fires
            # PARAM_ADDED, which can trigger the chunk's execute() (e.g. via _wire_inputs);
            # clearing afterwards would wipe what execute just authored.
            lm = chunk_registry.ChunkLayerManager()
            if lm.initialized:
                if layer := lm.get_or_create_layer(param):
                    layer.Clear()
        res = super().add_param(param, force_unique)
        if do_context_setup:
            chunk_notice.send_notice(chunk_notice.ChunkNoticeAlias.CHUNK_LOADED, param, data=param)
        return res

    def get_context(self) -> Optional["ChunkBase"]:
        owner = self
        while owner:
            if isinstance(owner, ChunkBase) and owner.has_flag(ChunkFlags.GroupFlags.CONTEXT):
                return owner
            owner = owner.get_owner()
        return None


class ChunkVectorBase(ChunkContainerBase, ChunkUsdAttributeSupport):
    """
    Base for vector style parameters. Function similar to a group but with fixed number of params. Interfacing
    with a Vector is similar to a regular valued parameter, where the 'value' attribute reference child params.
    These ops can be used:

    ``` py
    vec = chunk.Vector(
        "myVec", param=chunk.Slider(), length=3, suffix="xyz"
    )

    vec[0] = 10
    vec.x = 10
    vec.value = (0, 1, 2)
    ```

    Attributes:
        param (ChunkParamBase): The parameter to use as for the vector. Will clone it lenght times
        length (int): How many elements in the vector
        suffixes (Optional[Tuple[str, ...] | str]): A string or tuple of strings where len(suffixes) == length. These decide how the
            parameters should be nammed. If the param has a name already, the suffixes will be appended to the name
    """

    param: ChunkParamBase = attrs.field(default=None, repr=False, metadata=AttributeMetadata(serialize=False))
    length: int = attrs.field(default=0)
    suffixes: Optional[Sequence[str] | str] = attrs.field(default=None)
    _default_value: Optional[Sequence[Any]] = attrs.field(default=None, alias="default_value")

    # A Vector's own .flags is GroupFlags-typed (needed for widget construction — see
    # has_flag override below) and can't also carry ChunkFlags.ParamFlags.OUTPUT: GroupFlags
    # and ParamFlags are separate IntFlag classes with overlapping bit values, and has_flag()
    # does a strict same-class check, so combining them via `|` makes OUTPUT silently
    # undetectable (or, the other way round, makes GroupFlags checks crash). is_output is a
    # dedicated field so a Vector can be a computed output without touching .flags at all.
    is_output: bool = attrs.field(default=False)

    # Raw @Node.vecParam@ expression string driving this vector's value, when set.
    # Whole-vector references only — the referenced param must itself resolve to a
    # same-length sequence. None means the vector's value comes from its own children.
    _expr_value: Optional[str] = attrs.field(
        default=None, init=False, metadata=AttributeMetadata(copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND)
    )
    _expression_connections: list = attrs.field(
        default=attrs.Factory(list), init=False, metadata=AttributeMetadata(send_mode=SendMode.NO_SEND)
    )

    # Re-entrancy guards for expression cycles (a.p = "@b.p@" while b.p = "@a.p@").
    # Two flags, not one: the notice path calls execute(), which legitimately reads
    # self.value and so must be allowed through the read guard.
    _resolving_ref: bool = attrs.field(
        default=False,
        init=False,
        metadata=AttributeMetadata(
            copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND, validate=False, convert=False
        ),
    )
    _resolving_expr: bool = attrs.field(
        default=False,
        init=False,
        metadata=AttributeMetadata(
            copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND, validate=False, convert=False
        ),
    )

    # Set by _set_children_blocked while assigning children one at a time, so
    # data_changed can skip writing a (still partial) tuple to the storage attr
    # without suppressing each child's own notify (which widgets rely on to refresh).
    _suppress_storage_write: bool = attrs.field(
        default=False, init=False, metadata=AttributeMetadata(copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND)
    )

    def validate_attr(self, attr: attrs.Attribute, val: Any) -> bool:
        if attr.name == "suffixes" and val is not None:
            if len(val) < self.length:
                raise ValueError(
                    "Vector suffixes length is smaller than vector length. Set either as 'XYZ' or ('X', 'Y', 'Z') for a 3 length vector"
                )
        return super().validate_attr(attr, val)

    def __attrs_post_init__(self):
        super().__attrs_post_init__()
        self._attr_senders["value"] = AttributeSender(self, "value", chunk_notice.ChunkNoticeAlias.ATTRIBUTE_CHANGED)

        if not self.children.empty() or not self.param:
            # __attrs_post_init__ was called when copied, so no need to reinitialize the children as they
            # have already been initialized
            return

        if self.length and self._default_value and (self.length != len(self._default_value)):
            raise ValueError(f"Length and default_value of vector {self.name} does not match")

        if not self.length and self._default_value:
            self.length = len(self._default_value)
        elif not self.length and self.suffixes:
            self.length = len(self.suffixes)

        if not self.suffixes:
            self.suffixes = "abcdefghijklmnopqrstuvwxyz"[: self.length]

        for i in range(self.length):
            # Clone the self.param length times, and add the clones to the clildren container
            np = self.param.clone()

            if self.suffixes:
                np.name += self.suffixes[i]

            if self.is_output:
                # Children are plain scalar params (Slider etc.) — no GroupFlags/ParamFlags
                # collision for them, unlike the vector itself, so OUTPUT can go straight
                # on .flags to get the normal widget.set_enabled(False) treatment.
                np.set_flag(ChunkFlags.ParamFlags.OUTPUT)

            self.children.add(np)

        if self._default_value:
            self.default_value = self._default_value

    def __getitem__(self, key: str | int) -> ChunkParamBase:
        """Allows for Vector[0] or Vector['paramName'] access to the vector values

        Args:
            key (str | int): name or index of the requested value parameter

        Raises:
            KeyError: if key is not accessible

        Returns:
            Optional[ChunkCommon]: Param if found
        """

        if isinstance(key, str):
            param = self.get_param(key)
        elif isinstance(key, int):
            param = self.get_my_params()[key]
        if not param or not isinstance(param, ChunkParamBase):
            raise KeyError(f"Index {key} does not exist on vector")
        return param

    def data_changed(self, notice: Optional[chunk_notice.ChunkNotice] = None) -> Optional[chunk_notice.ChunkNotice]:
        my_notice = super().data_changed(notice)

        stage = chunk_registry.ChunkRegistry().scene.get_stage()
        if (
            notice
            and stage
            and isinstance(notice.data, AttributeSender)
            and notice.data.name == "value"
            # Recorded even while blocked; only the driven-attr push below is a reaction.
            # See ChunkParamBase.data_changed.
        ):
            # notice.data is typically a *child's* AttributeSender (bubbled up through
            # the DAG), whose .value is a single scalar — not usable here. This vector's
            # own self.value (tuple, already expression-aware) is what must be written.
            raw = self.raw_value
            is_expr = isinstance(raw, str) and chunk_utils.has_expression(raw)
            val = self.value
            # _set_children_blocked sets this while assigning each child in turn, to
            # avoid writing a partial tuple to the storage attr on the first (still
            # mid-assignment) bubble — that write would otherwise be the one that lazily
            # authors the attr and never gets corrected afterward. Notices still need to
            # propagate normally during the loop (this doesn't use is_blocked()) so each
            # child's own widget refreshes live as it's set.
            if val is not None and not self._suppress_storage_write:
                prev_write = chunk_notice._usd_write_active
                chunk_notice._usd_write_active = True
                try:
                    if self._param_attr_path and (usd_attr := stage.GetAttributeAtPath(Sdf.Path(self._param_attr_path))):
                        if not is_expr:
                            current = usd_attr.Get(0)
                            if current != val:
                                usd_attr.Set(val)
                        else:
                            chunk_root = str(usd_attr.GetPrim().GetParent().GetPath())
                            usd_attr.SetConnections([
                                Sdf.Path(
                                    f"{chunk_root}/{node}.outputs:{attr_name}"
                                    if stage.GetPrimAtPath(Sdf.Path(f"{chunk_root}/{node}")).GetAttribute(f"outputs:{attr_name}").IsValid()
                                    else f"{chunk_root}/{node}.inputs:{attr_name}"
                                )
                                for node, attr_name in chunk_utils.parse_expression(raw)
                            ])
                    if not self.is_blocked():
                        self._write_driven_attrs(stage, val)
                finally:
                    chunk_notice._usd_write_active = prev_write

            # Forward on this vector's own "value" sender so anything depending on the
            # whole vector (e.g. a downstream @Node.vecParam@ expression) reacts too.
            chunk_notice.send_notice(notice.data.get_notice_alias(), self.get_attribute_sender("value"), self.get_usd_prop())

        return my_notice

    def __setitem__(self, key: str | int, val: Any) -> Optional[ChunkParamBase]:
        param = self[key]
        if not isinstance(param, ChunkParamBase):
            raise ValueError("ChunkContainers does not support value assignment")

        param.value = val

    @property
    def raw_value(self) -> Any:
        """The stored expression string, or the tuple of children's raw values."""
        if self._expr_value is not None:
            return self._expr_value
        return tuple(x.raw_value for x in self.get_my_params() if isinstance(x, ChunkParamBase))

    @property
    def value(self):
        if self._expr_value is not None and chunk_utils.has_expression(self._expr_value):
            return self._resolve_expression_tuple(self._expr_value)
        return tuple(x.value for x in self.get_my_params() if isinstance(x, ChunkParamBase))

    @value.setter
    def value(self, val: Union[Sequence[Any], str]):
        if isinstance(val, str) and chunk_utils.has_expression(val):
            self._expr_value = val
            self._expression_connections = []
            for node_name, param_name in chunk_utils.parse_expression(val):
                ref = self._resolve_expression_ref(node_name, param_name)
                if ref is None:
                    logger.warning(f"Expression ref @{node_name}.{param_name}@ not found")
                    continue
                conn = chunk_notice.NoticeConnection(
                    method=self._on_ref_changed,
                    alias=chunk_notice.ChunkNoticeAlias.VALUE_CHANGED,
                    sender=ref.get_attribute_sender("value"),
                )
                self._expression_connections.append(conn)
            self._push_resolved_children()
            return

        self._expr_value = None
        self._expression_connections = []
        if not len(val) == self.length:
            raise ValueError(
                f"Trying to set value {val} on vector param {self.name} with wring size. Size should be {self.length}"
            )

        self._set_children_blocked(val)

    def _push_resolved_children(self) -> None:
        """Push the resolved tuple down into child params so their own widgets display
        real numbers rather than the parent's expression string."""
        resolved = self.value
        if resolved is None or len(resolved) != self.length:
            return
        self._set_children_blocked(resolved)

    def _set_children_blocked(self, val: Sequence[Any]) -> None:
        """Set every child from val, suppressing this vector's own storage-attr write
        until all children are set (data_changed checks _suppress_storage_write).
        Without this, the first (still mid-assignment) child change would be the one
        that lazily authors the chunk-prim storage attr, permanently baking in a
        stale value with only some components set.

        Deliberately does NOT use set_blocked(), which is transitive to children via
        their owner-chain is_blocked() check — that would also suppress each child's
        own notify, which their widgets rely on to refresh live as they're set."""
        self._suppress_storage_write = True
        try:
            for i in range(self.length):
                self[i] = val[i]
        finally:
            self._suppress_storage_write = False
        self.get_attribute_sender("value").execute()

    def _on_ref_changed(self, notice=None, sender=None):
        if self._resolving_ref:
            logger.warning(
                "Expression cycle at %s: %r refers back to itself through another param; "
                "leaving it at its previous value",
                self.name, self._expr_value,
            )
            return
        self._resolving_ref = True
        try:
            self._push_resolved_children()
        finally:
            self._resolving_ref = False

    def _resolve_expression_tuple(self, expr: str) -> tuple:
        if self._resolving_expr:
            logger.debug("Expression cycle while resolving %s — falling back to default", self.name)
            return self.default_value
        self._resolving_expr = True
        try:
            return self._resolve_expression_tuple_uncached(expr)
        finally:
            self._resolving_expr = False

    def _resolve_expression_tuple_uncached(self, expr: str) -> tuple:
        tokens = chunk_utils.parse_expression(expr)
        if len(tokens) == 1 and expr.strip() == f"@{tokens[0][0]}.{tokens[0][1]}@":
            ref = self._resolve_expression_ref(*tokens[0])
            if ref is not None:
                ref_val = ref.value
                if isinstance(ref_val, (tuple, list)) and len(ref_val) == self.length:
                    return tuple(ref_val)
        return self.default_value

    @property
    def default_value(self):
        return tuple(x.default_value for x in self.get_my_params() if isinstance(x, ChunkParamBase))

    @default_value.setter
    def default_value(self, val: Sequence[Any]):
        if val is None:
            return

        if not len(val) == self.length:
            raise ValueError(
                f"Trying to set value {val} on vector param {self.name} with wring size. Size should be {self.length}"
            )

        for i in range(self.length):
            self[i].default_value = val[i]

    def get_type(self):
        return ChunkFlags.ParamType.VECTOR | self.param.get_type()

    def evaluate_type(self):
        return ChunkFlags.ParamType.VECTOR | self.param.evaluate_type()

    def has_flag(self, flag: IntFlag) -> bool:
        """ChunkFlags.ParamFlags.OUTPUT is tracked via is_output instead of .flags — see
        the field's docstring. Everything else defers to the normal GroupFlags check."""
        if flag == ChunkFlags.ParamFlags.OUTPUT:
            return self.is_output
        return super().has_flag(flag)




class ChunkConfig(AttributesBase):
    """
    Base class for Configs. A config sets some load time configurations for a chunk.
    """

    def get_variants(self) -> list:
        """The menu entries, building them first if they were declared lazily.

        Memoised onto the field, so the callable runs once per process however many menus
        are drawn. Callers should use this rather than reading .variants, which may still
        be the callable.
        """
        variants = self.variants
        if callable(variants):
            try:
                variants = list(variants())
            except Exception as e:  # noqa: BLE001 - a menu must still open
                # Variants are an optional extra on one entry, and this runs while a menu is
                # being drawn for EVERY chunk -- so one module that cannot build its list
                # took the whole chunk menu with it. Live-reported on a USD 26.05 build
                # where Sdr.Registry() raises because some installed plugin advertises an
                # invalid version: right-clicking the node graph produced a traceback and no
                # menu at all, for every chunk in it.
                logger.warning("Could not build menu variants for %r: %s", self.module, e)
                return []
            object.__setattr__(self, "variants", variants)
        return variants

    category: str = attrs.field(default="Chunks")
    description: str = attrs.field(default="Chunk description...")
    load_for_plugin: Optional[str] = attrs.field(default=None)
    requirements: list[str] = attrs.field(factory=list)
    required_modules: list[str] = attrs.field(factory=list)
    module: str = attrs.field(default="")
    interactive: bool = attrs.field(default=False)
    # Kept out of the chunk menu. For a type that exists so something can be SAVED and read
    # back rather than created by hand -- `runtime`, the type a chunk built in the
    # interpreter is written as. It still has to be a registered module, since that is how
    # a load finds what to rebuild a prim from; it just should not be offered as a thing to
    # add to a graph.
    hidden: bool = attrs.field(default=False)
    terminal: bool = attrs.field(default=False)  # no outputs:chunk authored; chunk terminates the exec chain
    # How many exec input slots the chunk has. 1 is the ordinary case: one inputs:chunk
    # port, which may still carry several wires stacked on it. Above 1 the slots are
    # POSITIONAL and each gets its own port -- Stage Expression Filter's slot 0 is the
    # stream it always forwards and slot 1 the branch it gates, so which wire landed where
    # is the whole meaning. A Merge is neither: its slot count grows with what is wired
    # into it, which is is_merge_chunk's business, not this field's.
    exec_inputs: int = attrs.field(default=1)
    outputs: list[tuple[str, str]] = attrs.field(factory=list)  # extra typed output ports: [(name, SdfTypeName), ...]
    # Extra menu entries: [(label, {param: value}), ...], or a callable returning them.
    #
    # A callable because building this list can be expensive and is only ever needed to
    # draw a menu. mtlx_shader enumerates 784 MaterialX definitions from the Sdr registry
    # to collapse them into 217 entries, which measured at 1.88s -- 76% of the whole
    # registry discovery -- paid in every process, including headless sweeps that never
    # draw a menu at all. Pass a callable and that cost moves to the first menu build.
    variants: "list[tuple[str, dict]] | Callable[[], list[tuple[str, dict]]]" = attrs.field(
        factory=list
    )
    gizmos: list[GizmoBase] = attrs.field(factory=list)  # viewport handles: authored as ui:gizmo:* on the chunk prim

    def get_notice_alias(self) -> chunk_notice.ChunkNoticeAlias:
        return chunk_notice.ChunkNoticeAlias.CHUNK_CONFIG_CHANGED


# SceneSender moved to chunk_scene. It is re-exported lazily rather than imported at the
# top, because chunk_scene imports THIS module: a top-level import either way round is a
# cycle. A plain import at the bottom of this file would work only if chunk_core happened
# to be imported first -- `import usd_chunks.chunk_scene` on a cold interpreter would then
# fail. PEP 562 module __getattr__ resolves on first access instead, by which point both
# modules are fully loaded, so it is correct under any import order.
def __getattr__(name: str):
    if name == "SceneSender":
        from usd_chunks.chunk_scene import SceneSender

        return SceneSender
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")




def is_inst(cls: type[ValueT]) -> Predicate[ValueT]:
    """
    Created a predicate function which can be used for type narrowing the input parameter v

    Args:
        cls (type[ValueT]): The type which to check

    Returns:
        Predicate[ValueT]: The predicate function
    """

    def _fn(v: Any) -> TypeGuard[ValueT]:
        return isinstance(v, cls)

    return _fn
