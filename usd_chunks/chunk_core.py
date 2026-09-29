"""The base classes every chunk object derives from.

These classes are not meant to be instantiated; use the implementations in
[usd_chunks.chunk](chunk.md).

The attrs library gives every chunk class dataclass-like attributes. The attributes and
methods attrs generates are inherited through the MRO, so a subclass only declares its
fields and attrs builds __init__ and the rest. Instead of @attrs.define, every chunk
object uses AttributesMetaclass, which wraps each class in attrs.define with the right
settings and adds custom validation and conversion.

All chunk objects inherit from ChunkCommon, which declares the fields every chunk object
has, such as name. Every field that can change has an AttributeSender, and since
AttributeSender is a ChunkNoticePropagator, attribute changes propagate up the hierarchy
and send notices.
"""
from __future__ import annotations

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
from collections.abc import Callable, Generator, Iterable, Sequence
from typing import (
    Any,
    Generic,
    Protocol,
    TypedDict,
    TypeVar,
    TypeGuard,
    overload,
    TYPE_CHECKING,
)

import attrs
from pxr import Sdf, Tf, Usd
from typing_extensions import Required, Self, override

from usd_chunks.chunk_notice import Predicate
from usd_chunks import ATTRS_VERSION, chunk_notice, chunk_registry, chunk_utils
from usd_chunks.chunk_flags import ChunkFlags

if TYPE_CHECKING:
    # SceneSender moved to chunk_scene, which imports this module -- so the reference
    # this file keeps in its annotations can only be a type-checking one.
    from usd_chunks.chunk_scene import SceneSender
# The attribute layer -- declared attributes, their metadata and change notification --
# lives in chunk_attributes. It is the mechanism; this file is the model that uses it.
from usd_chunks.gizmo.types import (  # noqa: F401  (re-exported)
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
    field_type,
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
    """How highlighted text is drawn: a colour, optionally italic or bold."""
    color: Required[str]
    italic: bool
    bold: bool


class SyntaxHighlightMapping:
    """Regex patterns mapped to the style of the text they match, for a code viewer."""
    def __init__(self):
        self._mappings: dict[str, SyntaxHighlightStyle] = {}

    def add_mapping(self, pattern, style: SyntaxHighlightStyle):
        """Style every match of `pattern` with `style`."""
        self._mappings[pattern] = style

    def match_text(self, text: str) -> Generator[tuple[int, int, SyntaxHighlightStyle], None, None]:
        """Every styled span in `text`.

        Args:
            text: The text to highlight.

        Yields:
            (start, end, style) for each match of each pattern.
        """
        for pattern, fmt in self._mappings.items():
            for m in re.finditer(pattern, text):
                start, end = m.span()
                yield start, end, fmt


class PySyntaxHighlightMapping(SyntaxHighlightMapping):
    """Highlighting for Python source: class and def lines, and comments."""
    def __init__(self) -> None:
        super().__init__()

        pattern = r"^\s*class\s+\w+\(.*$"
        self.add_mapping(pattern, SyntaxHighlightStyle(color="#4c6ea3"))

        pattern = r"^\s*def\s+\w+\s*\(.*\)\s*:\s*$"
        self.add_mapping(pattern, SyntaxHighlightStyle(color="#31a56b"))

        pattern = r"^\s*#.*$"
        self.add_mapping(pattern, SyntaxHighlightStyle(color="#3f883f"))


class UsdSyntaxHighlightMapping(SyntaxHighlightMapping):
    """Highlighting for usda text: class, def and over lines."""
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
    """Chunk is a base class for creating UI representation of chunk containers."""

    #: Build a collapsed group's child widgets only when it is first expanded. Whether a
    #: toolkit can is its own question -- expanding must call ensure_children_built before it
    #: measures anything -- so a subclass opts in. Worth it for big folded groups: a Wedge's
    #: Ranges list is ~6,600 widgets and ~5 s to build, all of it hidden until opened.
    defer_collapsed_children: bool = False

    def __init__(self, group: GroupT, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Hold a weakref to the group. If all other references to the group is killed, then
        # remove it from the UI as well, as the ui should not take ownership of the group
        self._group = weakref.ref(group, self._group_deleted)

        self._callbacks: list[chunk_notice.NoticeConnection] = []

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
        self._children_built = not (
            self.defer_collapsed_children
            and self.group.has_flag(ChunkFlags.GroupFlags.COLLAPSABLE)
            and self.group.has_state(ChunkFlags.State.COLLAPSED)
        )
        if self._children_built:
            self._build_param_widgets(self.group)
        self.update_from_layout()
        self.update_from_flags()
        self.update_from_state()

    @property
    def group(self) -> GroupT:
        """The group this widget shows.

        Raises:
            ReferenceError: The group has been garbage collected.
        """
        if group := self._group():
            return group
        raise ReferenceError("reference to group is dead! :'(")

    @abstractmethod
    def get_widget_factory(self) -> ChunkWidgetFactoryBase:
        """The widget factory that builds this group's param widgets.

        Returns the group's own widget_factory; a UI plugin can override this to return its own.

        Raises:
            AttributeError: The group has no widget factory.

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
        """Delete this group widget. Implemented by a subclass.

        Raises:
            NotImplementedError: A subclass does not implement it.
        """
        raise NotImplementedError

    @abstractmethod
    def remove_widget(self, widget: Any):
        """Remove a child param widget from this group widget. Implemented by a subclass.

        Args:
            widget (Any): The widget to remove from the group widget

        Raises:
            NotImplementedError: A subclass does not implement it.
        """
        raise NotImplementedError

    def ensure_children_built(self) -> None:
        """Build the child widgets a collapsed group deferred (see defer_collapsed_children).

        Built from the group's params as they are now, so rows added while it was folded
        (a ListGroup grown by a load) arrive with the rest. Does nothing once built.
        """
        if self._children_built:
            return
        self._children_built = True
        self._build_param_widgets(self.group)

    def is_collapsed(self) -> bool:
        """Whether the group widget should be collapsed, because it or its parent is.

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
        """Build the group widget.

        Raises:
            NotImplementedError: _description_
        """
        raise NotImplementedError

    @abstractmethod
    def update_from_flags(self):
        """Update the widget from the group's flags. Implemented by a subclass.

        Raises:
            NotImplementedError: A subclass does not implement it.
        """
        raise NotImplementedError

    @abstractmethod
    def set_hidden(self, val: bool):
        """Hide or show the widget. Implemented by a subclass.

        Args:
            val (bool): Whether to hide or show the widget

        Raises:
            NotImplementedError: A subclass does not implement it.
        """
        raise NotImplementedError

    @abstractmethod
    def set_enabled(self, val: bool):
        """Enable or disable the widget. Implemented by a subclass.

        Args:
            val (bool): Whether to enable or disable the widget

        Raises:
            NotImplementedError: A subclass does not implement it.
        """
        raise NotImplementedError

    @abstractmethod
    def set_selected(self, val: bool):
        """Show whether the group is selected. Does nothing by default.

        Args:
            val (bool): Whether the group is selected
        """
        pass

    @abstractmethod
    def update_from_state(self):
        """Apply the group's HIDDEN, DISABLED and SELECTED state to the widget.

        A group is disabled only by its own DISABLED state. ParamFlags.OUTPUT means nothing for a
        group, and it shares a bit with GroupFlags.COLLAPSABLE, so testing it here would disable
        every collapsible group's children.
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
        """Apply the group's layout flags. Does nothing by default."""
        pass

    @abstractmethod
    def set_expanded(self, checked: bool):
        """Expand the widget to show its child widgets, or collapse it to hide them.

        Implemented by a subclass.

        Args:
            checked (bool): Whether the group widget should be expanded or not

        Raises:
            NotImplementedError: A subclass does not implement it.
        """
        raise NotImplementedError

    @abstractmethod
    def update_param_label(self, notice, sender: AttributeSender):
        """Correct a child's label in the layout when its name, label or LABELED flag changes.

        If LABELED was unset, the label is removed and the widget spans the row; if it was set,
        the label is added; otherwise only the label text is updated. A UI plugin implements
        this to get labeled widgets.

        Args:
            notice (Tf.Notice): The notice that was sent
            sender (AttributeSender): The attribute that changed
        """
        pass

    @abstractmethod
    def update_label(self, text: str):
        """Update the label of this container.

        Args:
            text (str): The new label to display
        """
        pass

    @abstractmethod
    def supports_widget(self, widget: Any) -> bool:
        """Whether a widget belongs to this UI plugin's toolkit.

        A Qt plugin, for example, returns ``isinstance(widget, QtWidgets.QWidget)``.

        Args:
            widget (Any): The widget to check

        Raises:
            NotImplementedError: A subclass does not implement it.

        Returns:
            bool: True if the widget is supported by the current ui plugin
        """
        raise NotImplementedError

    @abstractmethod
    def add_widget(self, widget: ChunkContainerWidgetBase | ChunkParamWidgetBase, label: str | None = None):
        """Add a child param's widget to the group's layout. Implemented by a subclass.

        Called after the param has been added to the group.

        Args:
            widget (ChunkContainerWidgetBase | ChunkParamWidgetBase): The widget to add
            label (str, optional): An optional label of the widget. Defaults to None.

        Raises:
            NotImplementedError: A subclass does not implement it.
        """
        raise NotImplementedError

    def _name_changed(self, notice, sender):
        self.update_label(sender.value)

    def _layout_changed(self, notice, sender):
        self.update_from_layout()

    def _disconnect_callbacks(self):
        """Revoke all notice subscriptions held by this widget.

        Must be called when the widget is torn down. The host (Qt) may destroy the
        underlying C++ object while the Python wrapper -- and therefore its notice
        subscriptions -- stays alive, so without this a later notice would invoke a
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
        if not self._children_built:
            return  # built with everything else when the group is first expanded
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

    def _add_param_widget(self, param: ChunkParamBase, group: ChunkContainerBase | None = None, list_index=0):
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

    def _add_group_layout(self, group: ChunkContainerBase) -> ChunkContainerWidgetBase:
        container = self.group_widgets.get(group, None)
        if not container:
            container = self.__class__.__new__(self.__class__)
            container.__init__(group, parent=self)  # type: ignore
            self.add_widget(container)
            self.group_widgets[group.get_hash()] = container

        return container

    def _build_param_widgets(self, group: ChunkContainerBase):
        for param in group.get_my_params():
            self._add_param(param)

    def _add_param(self, param: ChunkCommon):

        if isinstance(param, ChunkContainerBase):
            self._add_group_layout(param)
        if isinstance(param, ChunkParamBase):
            self._add_param_widget(param)


#####################
# FIELD TRANSFORMER #
#####################




class ChunkHash(Protocol):
    """Protocol for a hashable chunk object."""

    @override
    def __hash__(self) -> int: ...
    @override
    def __eq__(self, value: object, /) -> bool: ...


class ProxyParam(chunk_notice.ChunkNoticePropagator):
    """A param that stands in for a concrete one, holding only its own value and children.

    The proxy stores a unique value, and unique children if the param is a group. That is
    what list rows need: one original group dictates the content of every row, and each row
    holds its own values. Any attribute except 'value' can be changed on the original, and
    the proxies follow.

    The proxy has its own hash; the original is its param_ref. ChunkCommon's _hashable
    attribute is not overridden, so ProxyParam._hashable evaluates to the referenced HashVal.

    Args:
        param (ChunkCommon): The chunk object to stand in for
    """

    def __init__(self, param: ChunkCommon, *args, **kwargs):
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

    @override
    def __eq__(self, value: object) -> bool:
        return self.__hashable == value

    @override
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

    @override
    def get_notice_alias(self) -> chunk_notice.ChunkNoticeAlias:
        return self._param_ref.get_notice_alias()

    @override
    def request_delete(self):
        self._children.clear()
        self._children.set_owner(None)

        if self.__param_ref():
            # If the parameter is alive, we should send
            owner = self.get_owner()
            if owner and isinstance(owner, NameSyncedContainer):
                # Owner is a NameSyncedContainer, pop it from the container. When successfully removed,
                # the container will send PARAM_REMOVED with it's owning ChunkContainerBase as sender.
                owner.send_removed(self)

        self.set_owner(None)

    @override
    def data_changed(self, notice: chunk_notice.ChunkNotice | None = None) -> chunk_notice.ChunkNotice | None:
        if not notice or not (res := super().data_changed(notice)):
            return None

        if notice.data == self._param_ref:
            for alias, prop in notice.data.get_dag_node().iter_changes():
                self.send_self(alias, prop)
        return res

    @override
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

    @override
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

    @override
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
            # checked (see its own guard). Without this, restoring a row's saved value would
            # fire a real, immediate ATTRIBUTE_CHANGED here during the bulk load, re-executing
            # the whole downstream chain once per row. load_chunks' own "trigger the DAG solve
            # for root nodes" pass still runs once everything is restored (see the layer
            # pre-registration above the re-sort pass in _add_prim_mods), so this skips only
            # the early, repeated execute() calls, not the real one.
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
    """The container holding a chunk object's children, indexed by hash and by name.

    Both maps are ChainMaps that also reach every nested NameSyncedContainer, so a nested
    child is found without iterating the hierarchy. The get_* methods search nested
    containers too; the get_my_* methods only this one.

    params maps a hash to a ChunkCommon. ChunkCommon objects hash by their uuid, so a param
    can be looked up by the object itself or by obj._hashable; where a lookup is typed as
    taking a HashVal (get_param(), get_my_param()), passing the object works the same.

    names maps each param's current (mutable) name to its hash, for fast lookup by name. It
    must be kept up to date whenever a name changes, which is why this class is a
    ChunkNoticePropagator: it sits in the object hierarchy and updates the mapping when a
    name changes.

    Anything added with add() gets this container as its owner, so a chunk object holding
    one should own the container and never manage its params' ownership itself. The
    container sends no notices of its own; it only propagates its children's.
    """

    def __init__(self):
        super().__init__()

        self._last_added: HashVal | None = None
        self._last_removed: HashVal | None = None

        self._params: ChainMap[ChunkHash, ChunkT] = ChainMap()
        self._names: ChainMap[str, ChunkHash] = ChainMap()

    def __copy__(self):
        """A new container holding copies of this one's direct children, and its callbacks."""
        copied = NameSyncedContainer()

        for param in self._params.maps[0].values():
            copied.add(copy.copy(param))

        copied._copy_callbacks(self)
        return copied

    def empty(self):
        """Whether the container holds nothing."""
        return not bool(self._params)

    def __contains__(self, obj: ChunkCommon | ProxyParam) -> bool:
        """Whether obj is held here or anywhere nested, by identity (its hash).

        The nested hierarchy is not materialised -- see resolve_new_param for why that matters.

        Args:
            obj: The chunk object.

        Returns:
            True if it is held.
        """
        return obj.get_hash() in self._params



    @override
    def get_notice_data(self) -> Self:
        return self

    def clear(self):
        """Clear all children from this container.

        The children are not deleted explicitly, as references to them may be held elsewhere,
        but in practice they are, because other objects should only hold weakrefs to params.
        """
        self._names.clear()
        self._params.clear()

    def add(self, obj: ChunkT | ProxyParam, send=True):
        """Add a chunk object to this container, which becomes its owner.

        The object is stored in params and its name in names, so it can be found with
        get_param() and get_by_name(). The container maintains the name-to-object relationship,
        so never add to params or names directly; use this.

        If the object is a ChunkContainerBase, its children are chained into this container's
        maps, which gives fast access to nested params.

        Args:
            obj (ChunkCommon | ProxyParam): The object to add. Nothing is done to avoid
                duplication across layers.
            send (bool, optional): Send PARAM_ADDED for it. Defaults to True.
        """
        # Append child the object + name to the first layer of the ChainMaps
        self._params.maps[0][obj.get_hash()] = obj  # type: ignore
        self._names.maps[0][obj.name] = obj.get_hash()

        # Mark self as the owner of the added obj as we need to be notified when the object changes
        obj.set_owner(self)

        if send:
            self.send_added(obj)

    def add_nested(self, nested: NameSyncedContainer[ChunkT]):
        """Appends nested params to the ChainMap. This allows for fast lookups across the layers.

        Args:
            nested ("NameSyncedContainer[ChunkT]"): The nested params to add
        """
        self._params.maps.append(nested._params)
        self._names.maps.append(nested._names)

    @staticmethod
    def create_proxy(source: NameSyncedContainer, target: NameSyncedContainer):
        """Fill `target` with a ProxyParam for each of `source`'s direct children.

        Args:
            source: The container to mirror.
            target: The container to fill.

        Returns:
            `target`.
        """
        for param in source.get_my_params():
            proxy = ProxyParam(param)
            target.add(proxy)
        return target

    @override
    def data_changed(self, notice: chunk_notice.ChunkNotice | None = None) -> chunk_notice.ChunkNotice | None:
        # Override to reflect any changes on the children in this ds. Used to update the names when a name changes
        if notice and (p := notice.data):
            if isinstance(p, AttributeSender) and p.name == "name" and p.old_value in self._names.maps[0]:
                self.name_changed(p.old_value)  # type: ignore

        return super().data_changed(notice)

    @override
    def get_notice_alias(self) -> chunk_notice.ChunkNoticeAlias:
        # We are only interested in hijacking data_changed, and should not send any signals.
        return chunk_notice.ChunkNoticeAlias.NONE

    def name_changed(self, old_name: str):
        """Re-key a renamed child in this level's names map.

        The entry for old_name is replaced by the object's new name, so the child is found by
        its new name and no longer by its old one::

            c = chunk.Slider("A")
            g = chunk.Group("group")
            g.add_param(c)
            assert g.get_param("A") is not None

            c.name = "B"
            assert g.get_param("B") is not None and g.get_param("A") is None

        Args:
            old_name (str): The name the object was mapped under

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
    def get_param(self, key: ChunkHash | str) -> ChunkT | None: ...

    @overload
    def get_param(self, key: ChunkHash | str, predicate: Predicate[ValueT] | None) -> ValueT: ...

    def get_param(
        self, key: ChunkHash | str, predicate: Predicate[ValueT] | None = None
    ) -> ChunkT | None | ValueT:
        """Find a param by hash, by object, or by name or dotted path.

        A string that is a path to a param, such as aChunk.aGroup.aParam, finds the param at
        that path; a bare name finds the first param with that name.

        Args:
            key: A ChunkCommon (it hashes to its HashVal), a HashVal, or a name or path.
            predicate: Only return the param if it matches this.

        Returns:
            ChunkCommon | None: The param, or None if not found
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
    def get_by_name(self, name: str) -> ChunkT | None: ...

    @overload
    def get_by_name(self, name: str, predicate: Predicate[ValueT] | None) -> ValueT: ...

    def get_by_name(self, name: str, predicate: Predicate[ValueT] | None = None) -> ChunkT | None | ValueT:
        """Find a param by name, or by a dotted path through nested groups.

        For example::

            my_chunk.get_param("groupA.groupB.slider")

        Args:
            name: The name or dotted path.
            predicate: Only return the param if it matches this.

        Returns:
            ChunkCommon | None: The param, or None if not found
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

    def get_my_names(self) -> tuple[str, ...]:
        """Get the names at this level only.

        Returns:
            tuple[str, ...]: The param names of this level
        """
        return tuple(self._names.maps[0].keys())

    @overload
    def get_params(self) -> tuple[ChunkT, ...]: ...

    @overload
    def get_params(self, predicate: Predicate[ValueT] | None) -> tuple[ValueT, ...]: ...

    def get_params(self, predicate: Predicate[ValueT] | None = None) -> tuple[ChunkT, ...] | tuple[ValueT, ...]:
        """Get every param at this level and all nested levels.

        Args:
            predicate: Only return params matching this.

        Returns:
            tuple[ChunkCommon, ...]: The params
        """
        ps = self._params.values()

        if not predicate:
            return tuple(ps)
        return tuple(filter(predicate, ps))

    @overload
    def get_my_params(self) -> tuple[ChunkT, ...]: ...
    @overload
    def get_my_params(self, predicate: Predicate[ValueT] | None) -> tuple[ValueT, ...]: ...

    def get_my_params(self, predicate: Predicate[ValueT] | None = None) -> tuple[ChunkT, ...] | tuple[ValueT, ...]:
        """Get the params at this level only.

        Args:
            predicate: Only return params matching this.

        Returns:
            tuple[ChunkCommon, ...]: The params
        """
        ps = self._params.maps[0].values()
        if not predicate:
            return tuple(ps)
        return tuple(filter(predicate, ps))

    def send_added(self, obj: ChunkCommon | ProxyParam):
        """Sends a PARAM_ADDED notice with the new obj as notice data and owner as the sender.

        Args:
            obj (ChunkCommon | ProxyParam): the new parameter
        """
        self._last_added = obj._hashable

        if owner := self.get_owner():
            owner.send_self(chunk_notice.ChunkNoticeAlias.PARAM_ADDED, obj)

    def send_removed(self, obj: ChunkCommon | ProxyParam):
        """Sends a PARAM_REMOVED notice with the new obj as notice data and owner as the sender.

        Args:
            obj (ChunkCommon | ProxyParam): the removed parameter
        """
        self._last_removed = obj._hashable
        if owner := self.get_owner():
            owner.send_self(chunk_notice.ChunkNoticeAlias.PARAM_REMOVED, obj)

    def __delitem__(self, key: ChunkHash):
        """Remove a child, sending PARAM_REMOVED and releasing its ownership."""
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

    def pop(self, key: ChunkHash) -> ChunkCommon | None:
        """Remove and return the child at key.

        Args:
            key (ChunkHash): The chunk object to remove

        Returns:
            ChunkCommon | None: The removed object, or None if it is not here
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
    """The unique identity of a chunk object, created from a uuid4.

    Every ChunkCommon holds one.
    """

    @staticmethod
    def generate_key() -> int:
        """Generate a new uuid value to hash by. For internal use."""
        return uuid.uuid4().int  # type: ignore

    _uuid: int = attrs.field(
        factory=generate_key,
        init=False,
        metadata=AttributeMetadata(copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND),
    )

    @override
    def __hash__(self) -> int:
        return hash(self._uuid)

    @override
    def __eq__(self, value: object) -> bool:
        return hash(self) == hash(value)

    def __copy__(self):
        # Should never be called, but just in case...
        # Copying this class should result in a new key
        """A new HashVal with a fresh key: a copy of an identity is a different identity."""
        cls = self.__class__
        c = cls.__new__(cls)
        c.__init__()
        return c


class ChunkCommon(AttributesBase):
    """Base class for all chunk objects, holding the attributes every one of them supports.

    Not instantiated directly; concrete classes inherit from it.

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
    label: str | None = attrs.field(default="")
    tooltip: str | None = attrs.field(default="")
    icon: str | None = attrs.field(default="")  # Path to icon

    # Hold custom data which is not used directly by the chunk
    user_data: dict[str, Any] = attrs.field(factory=dict, repr=False)

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
    _chunk_parent: weakref.ReferenceType | None = attrs.field(
        default=None, init=False, metadata=AttributeMetadata(send_mode=SendMode.NO_SEND)
    )

    # Set once, on the clone chunk_stage.loft_param_to_context/_restore_lofted_params
    # produces -- a static marker, not a live link to the source chunk (there is no live
    # link; label is a one-time default, see loft_param_to_context). Its only job is
    # letting the params panel offer "Rename..." for a lofted param specifically, rather
    # than for every param generally.
    _lofted: bool = attrs.field(
        default=False, init=False, metadata=AttributeMetadata(send_mode=SendMode.NO_SEND)
    )

    def get_identifier(self) -> str:
        """A dotted path from the top-most owner to this object, e.g. MyChunk.SomeGroup.MyParam.

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

    def get_chunk(self) -> ChunkBase | None:
        """Get the owning Chunk if it exists.

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
        """Get the layer identifier used by the ChunkLayerManager.

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

    @override
    def get_logger(self) -> logging.Logger:
        if not self._logger:
            self._logger = logging.getLogger(f"usd_chunks.{self.__class__.__name__}.{self.name}")
        return self._logger

    @override
    def is_blocked(self) -> bool:
        v = self.has_state(ChunkFlags.State.DISABLED)
        return v or super().is_blocked()

    def get_hash(self) -> HashVal:
        """Get the HashVal of this chunk object.

        Useful for storing the object in a hash map without holding a reference to it.

        Returns:
            HashVal: The hash value of this object
        """
        return self._hashable

    def get_edit_context(self) -> Sdf.Layer | None:
        """The layer to author this object's stage edits in: its chunk's layer.

        Every param uses the layer of the nearest ChunkBase above it, rather than a layer of its
        own::

            with Usd.EditContext(stage, param.get_edit_context()):
                ...  # modify the stage

        Returns:
            Sdf.Layer | None: The layer, or None when the layer manager is not initialized or
            the layer is muted
        """
        if not chunk_registry.ChunkLayerManager().initialized:
            return

        if c := self.get_chunk():
            layer = chunk_registry.ChunkLayerManager().get_or_create_layer(c)
            if layer and layer.IsMuted():
                return None
            return layer

    @override
    def convert_attr(self, attr: attrs.Attribute, val: Any) -> Any:
        if attr.name == "name":
            return re.sub(r"\W+", "", str(val))[0:100]

        ftype = field_type(type(self), attr)
        if inspect.isclass(ftype) and issubclass(ftype, Enum):
            if val is not None and not isinstance(val, ftype):
                try:
                    return ftype(val)
                except TypeError:
                    return val

        return super().convert_attr(attr, val)

    @override
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
        """Check whether a value is a valid filesystem path for an attribute. Always True for now.

        Can be called from _validate or _convert in a subclass. Use _validate_existing_path to
        also check that the path exists. Not implemented yet, because some string params may
        hold an Sdf.AssetPath or a pathlib.Path.

        Args:
            attr (attrs.Attribute): The attribute being validated
            value (Any): The value to validate

        Returns:
            bool: True if the value is a valid path
        """
        return True

    def _validate_existing_path(self, attr: attrs.Attribute, value) -> bool:
        """Check if the given value is a valid os path and that it exists.

        Args:
            attr (attrs.Attribute): The attribute to validate
            value (str): The value to validate

        Returns:
            bool: True if success
        """
        if not self._validate_path(attr, value):
            return False

        # A `#`-padded value is a per-frame template (chunks/usd/value_clips.py's Clip
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
        """Get the label of this chunk object, or its name if no label is set.

        Returns:
            str: The label
        """
        return self.name if not self.label else self.label

    def set_flag(self, flag: IntFlag):
        """Enable a flag.

        Args:
            flag (IntFlag): The flag to enable
        """
        self.flags |= flag

    def unset_flag(self, flag: IntFlag):
        """Disable a flag.

        Args:
            flag (IntFlag): The flag to disable
        """
        self.flags &= ~flag

    def has_flag(self, flag: IntFlag) -> bool:
        """Check if a flag is enabled.

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
        """Set a state.

        Args:
            state (ChunkFlags.State): The state to set
        """
        self.state |= state

    def unset_state(self, state: ChunkFlags.State):
        """Unset a state.

        Args:
            state (ChunkFlags.State): The state to unset
        """
        self.state &= ~state

    def has_state(self, state: ChunkFlags.State) -> bool:
        """Check if state is set.

        Args:
            state (ChunkFlags.State): The state to check

        Returns:
            bool: True if the state is set, otherwise False
        """
        return bool(self.state & state)

    def set_selected(self, val: bool):
        """Set or clear the SELECTED state."""
        if val:
            self.set_state(ChunkFlags.State.SELECTED)
        else:
            self.unset_state(ChunkFlags.State.SELECTED)

    @override
    def __hash__(self) -> int:
        return hash(self._hashable)

    @override
    def __eq__(self, value: object) -> bool:
        return self._hashable == value

    def get_stage(self) -> Usd.Stage | None:
        """Easy access to the current UsdStage.

        Returns:
            Usd.Stage: The stage held by the SceneSender of ChunkRegistry
        """
        return self.get_scene().get_stage() or chunk_notice._loading_stage

    def get_scene(self) -> SceneSender:
        """Easy access to the current chunk scene.

        Returns:
            SceneSender: The SceneSender of ChunkRegistry
        """
        return chunk_registry.ChunkRegistry().scene

    def can_delete(self):
        """Whether nothing depends on this object, so it can be deleted."""
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
        """Ask the owners to delete this object; removing their references is their job.

        If the object lies within a ChunkContainerBase, the container's remove_param_or_group is
        called with it. All callbacks are removed from the object and its owner is set to None.
        """
        # Retire the chunk from module execution before anything is dismantled. Teardown
        # detaches params and edits the layer stack, and both cascade as notices that can
        # re-enter the DAG solve and call this chunk's module.execute() again -- against a
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
            logger.warning("Deleting node: %s with connected outputs. Stuff may break", self.name)
            self.get_dag_node().disconnect()

        owner = self.get_owner()
        if owner and isinstance(owner, ChunkContainerBase):
            # Owner is a group, so delete this param from the group
            owner.remove_param_or_group(self)
        elif owner and isinstance(owner, NameSyncedContainer):
            # Owner is a NameSyncedContainer, pop it from the container. When successfully removed,
            # the container will send PARAM_REMOVED with it's owning ChunkContainerBase as sender.
            owner.pop(self)

        self._callbacks.clear()
        self.set_owner(None)
        self.send_self(chunk_notice.ChunkNoticeAlias.CHUNK_DELETED)


# Every param bound to a USD property, by that property's path. This is how a changed path
# USD reports (Usd.Notice.ObjectsChanged) finds its params: without it the listener walked
# every param of every chunk on every notice and compared paths -- 62,000 param visits on a
# measured 34-chunk scene open. Maintained by the _param_attr_path setter, so it is right
# by construction wherever the binding is made or moved. Weak, so it never keeps a param
# alive; several params may share one path (two Property Editors on the same prim).
_params_by_attr_path: dict[str, weakref.WeakSet] = {}


def params_bound_to(path: str) -> list[ChunkUsdAttributeSupport]:
    """The params currently bound to this USD property path (see _params_by_attr_path)."""
    bound = _params_by_attr_path.get(path)
    return list(bound) if bound else []


class ChunkUsdAttributeSupport(AttributesBase):
    """Mixin that lets a valued param be bound to a USD attribute.

    A param can then be built from a Usd.Attribute, with the NoticeConnections that keep the
    param's value and the attribute in sync.
    """

    # The storage attribute path (/chunks/<name>/inputs:<param>) -- the param's authoritative USD value.
    # Read and written as _param_attr_path; the property below keeps _params_by_attr_path in step.
    _param_attr_path_store: str | None = attrs.field(
        default=None,
        init=False,
        metadata=AttributeMetadata(copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND),
    )

    # (anchor_prim_path, vs_name, variant) tuples active when this param was bound to
    # _param_attr_path -- set only for a param bound to a REAL prim's attribute via
    # chunk.show_attribute (the Property Editor, Light, ...), never for the ordinary
    # chunk-storage-attr binding. data_changed's write re-enters these via
    # chunk_utils.author_in_variant_selections instead of get_attr_home_layer, which cannot
    # see inside a variant. Empty for a param bound outside any variant context.
    _var_selections: tuple = attrs.field(
        default=(),
        init=False,
        metadata=AttributeMetadata(copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND),
    )

    @property
    def _param_attr_path(self) -> str | None:
        return self._param_attr_path_store

    @_param_attr_path.setter
    def _param_attr_path(self, path: str | None) -> None:
        old = self._param_attr_path_store
        if old == path:
            return
        if old is not None and (bound := _params_by_attr_path.get(old)) is not None:
            bound.discard(self)
            if not bound:
                del _params_by_attr_path[old]
        self._param_attr_path_store = path
        if path is not None:
            _params_by_attr_path.setdefault(path, weakref.WeakSet()).add(self)

    # Real prim attr paths this storage attr drives via USD native connections.
    # Connections are re-authored whenever _param_attr_path is set.
    _driven_attr_paths: list[str] = attrs.field(
        factory=list,
        init=False,
        metadata=AttributeMetadata(copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND),
    )

    # {driven attr path: (anchor_prim_path, vs_name, variant) tuples} for a driven attr
    # connect_usd_attr registered from inside a variant context -- a param that started as
    # a direct real-attr binding (show_attribute) before _sync_chunk_inputs promoted it into
    # one the chunk's own storage attr drives (see chunk_stage._drive_from_storage). Keyed
    # per path, not a single param-wide field: _driven_attr_paths is itself a list, and one
    # param could in principle drive several real attrs with different variant scopes.
    # _write_driven_attrs / _author_driven_connections re-enter the recorded selections
    # (chunk_utils.author_in_variant_selections) instead of the chunk's plain edit layer,
    # which cannot see inside a variant. Empty/missing for a path bound outside any variant.
    _driven_var_selections: dict[str, tuple] = attrs.field(
        factory=dict,
        init=False,
        metadata=AttributeMetadata(copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND),
    )

    # Lifecycle connection -- fires update_usd_prop when param value changes.
    _usd_prop_connection: chunk_notice.NoticeConnection | None = attrs.field(
        default=None,
        init=False,
        metadata=AttributeMetadata(copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND),
    )

    @override
    def data_changed(self, notice: chunk_notice.ChunkNotice | None = None) -> chunk_notice.ChunkNotice | None:
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
            # ChunkVectorBase's "value" bubbles up from a child's scalar AttributeSender --
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
                    # Entered before the lookup, not only around the write: a prim that
                    # exists only in the recorded variant is not on the stage while another
                    # one is selected, so looking it up first found nothing and the edit was
                    # dropped. A no-op outside a variant; the inner edit_context_for below
                    # re-enters the same scopes, which is skipped rather than nested.
                    # Inside a variant the write still goes into this chunk's own layer.
                    variant_layer = chunk_utils.chunk_write_layer(stage, self) if self._var_selections else None
                    with chunk_utils.edit_context_for(stage, self._var_selections, variant_layer):
                        if usd_attr := stage.GetAttributeAtPath(Sdf.Path(self._param_attr_path)):
                            # self._param_attr_path is usually the chunk-prim's own storage attr --
                            # bookkeeping, independent of the stage's ambient/Edit-Target-driven
                            # edit target (which _write_driven_attrs below correctly targets
                            # separately via get_edit_context()). It already exists (that's what
                            # having a _param_attr_path means), so update it wherever its spec
                            # already lives -- not wherever Edit Target currently resolves to,
                            # which may have moved on since it was first authored.
                            #
                            # No home means the attr is new; it goes where a new bookkeeping
                            # attr of this chunk goes (get_bookkeeping_layer). An attr composed
                            # in from a sequence file has a home: the host's edit target.
                            #
                            # A param bound straight to a real prim's attribute from inside a
                            # variant (show_attribute, from the Property Editor or Light) is the
                            # exception: get_attr_home_layer walks the stage's plain layer stack,
                            # which cannot see a spec keyed under a variant's own internal path,
                            # so it always comes back empty there and the edit would otherwise
                            # fall through to the fallback -- outside the variant entirely.
                            # Re-enter the selections recorded at bind time instead.
                            fallback = chunk_utils.get_attr_home_layer(usd_attr) or chunk_utils.get_bookkeeping_layer(
                                stage, self.get_chunk()
                            )
                            with chunk_utils.edit_context_for(stage, self._var_selections, fallback):
                                if not is_expr:
                                    current = usd_attr.Get(0)
                                    # Don't write a string to a typed (numeric) attr -- an unresolved
                                    # forwarded value can arrive as text and raise a type-mismatch.
                                    bad_str = isinstance(val, str) and not chunk_utils.attr_holds_text(usd_attr)
                                    if not bad_str and current != val:
                                        usd_attr.Set(val)
                                if is_expr:
                                    chunk_root = str(usd_attr.GetPrim().GetParent().GetPath())
                                    conns = []
                                    for node, attr_name in chunk_utils.parse_expression(raw):
                                        ref_prim = stage.GetPrimAtPath(Sdf.Path(f"{chunk_root}/{node}"))
                                        # The referenced node may not exist (a stale/renamed expression
                                        # ref, or a lofted chain pointing at a prim not on this stage) --
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
        and the storage attr are topology only -- USD Get() on the driven attr doesn't
        follow them, so the resolved value must be copied explicitly for any GUI
        reading that attr directly to see it.

        A path connect_usd_attr recorded a variant scope for (show_attribute, from inside a
        variant) is the exception: get_edit_context()'s plain layer would author the value
        outside the variant, applying it to every variant equally, so that path re-enters
        the recorded selections (chunk_utils.author_in_variant_selections) instead.
        """
        if val is None or not self._driven_attr_paths:
            return
        chunk_layer = self.get_edit_context()
        # USD refuses to author into a muted layer (a chunk gated off by a Switch or a
        # filter); chunk_stage.set_chunks_muted re-pushes these values on un-mute.
        if not chunk_layer or stage.IsLayerMuted(chunk_layer.identifier):
            return
        for path in self._driven_attr_paths:
            # Looked up inside the recorded variant, not before entering it: a prim that
            # only exists in that variant is not on the stage while another one is
            # selected, so the lookup came back empty and the edit was silently dropped.
            var_selections = self._driven_var_selections.get(path, ())
            with chunk_utils.edit_context_for(stage, var_selections, chunk_layer):
                driven_attr = stage.GetAttributeAtPath(Sdf.Path(path))
                if not driven_attr:
                    continue
                # An expression that did not resolve (a typo, an unset variable) stays
                # text; a typed attr refuses it, and the raise took the whole edit down.
                if isinstance(val, str) and not chunk_utils.attr_holds_text(driven_attr):
                    continue
                if driven_attr.Get(0) != val:
                    driven_attr.Set(val)

    @abstractmethod
    def update_usd_prop(self, notice: chunk_notice.AttributeChangeNotice, sender: AttributeSender):
        """Write the param's change to its bound USD attribute.

        Invoked when the 'value' AttributeSender changes, with the attribute as notice.data.

        Args:
            notice (chunk_notice.AttributeChangeNotice): The notice holding the Usd.Attribute
            sender (AttributeSender): The sender of the notice
        """
        # ChunkVectorBase.data_changed already handles this write path itself (correctly,
        # with outputs:-preferring connection routing) -- this method's SetConnections
        # below hardcodes inputs: unconditionally, so letting both run fights over two
        # different connection targets on every value change.
        if isinstance(notice.data, Usd.Attribute) and not isinstance(self, ChunkVectorBase):
            raw = getattr(self, 'raw_value', None)
            is_expr = isinstance(raw, str) and chunk_utils.has_expression(raw)
            # Only String-typed attrs can hold the raw expression text as their value
            # (needed so _patch_expressions can rewrite it on rename); other types (Float,
            # Float3, etc.) store the resolved value and keep the text in customData
            # instead (chunk_utils.store_expression_text) -- rebuilding it from
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
                # _connect_param_attr -- connect_usd_attr's driven-content path is a
                # separate mechanism, _author_driven_connections, scoped via
                # get_edit_context()). It already exists (that's what being bound via
                # _connect_param_attr means), so update it wherever its spec already
                # lives, rather than wherever the stage's ambient/Edit-Target-driven
                # edit target currently points -- that may have moved on since it was
                # first authored. No home means the attr is new, and it goes where a
                # new bookkeeping attr of this chunk would.
                stage = notice.data.GetStage()
                home = chunk_utils.get_attr_home_layer(notice.data) or chunk_utils.get_bookkeeping_layer(
                    stage, self.get_chunk()
                )
                with Usd.EditContext(stage, home):
                    # A non-String attr must never receive raw expression text: if an expression ref
                    # can't resolve (e.g. a lofted cross-context ref like @ChunkGroup.tx@), sender.value
                    # stays the expression STRING and Set() on a typed (float/etc) attr raises a
                    # type-mismatch. The connection authored below carries the topology, so skip the
                    # value write in that case.
                    bad_str = isinstance(store_val, str) and not chunk_utils.attr_holds_text(notice.data)
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
        """Update the bound AttributeSender's value from the USD attribute after the attribute changes."""
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
        """Bind this param to a specific USD property with an explicit sender (used for relationships).

        Prefer _connect_param_attr for standard attribute binding.

        Args:
            prop_path: The property, or its path.
            sender: The AttributeSender whose changes are written to the property.
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
        """Return the storage attribute this param is bound to.

        Returns:
            Usd.Property | None: The bound storage USD property, or None.
        """
        stage = self.get_stage()
        if stage and self._param_attr_path:
            return stage.GetPropertyAtPath(self._param_attr_path)

    def _connect_param_attr(
        self, attr: Usd.Property, var_selections: tuple | None = None, *, read_back: bool = True
    ) -> None:
        """Bind this param to its storage attribute for UI-to-USD sync.

        After binding, re-authors any pending USD connections to driven real attrs. Called by
        _load_wire_inputs; not part of the public chunk-module API.

        Args:
            attr (Usd.Property): The storage attribute (/chunks/<name>/inputs:<param>).
            var_selections: See _var_selections. None (the default) leaves whatever is
                already recorded untouched -- _sync_chunk_inputs rebinds every param to its
                own chunk-prim storage attr on every load/sync, and passes nothing here, so
                that generic rebind must not clobber what from_usd_attr recorded when this
                param was first built from a real prim's attribute (show_attribute). Only a
                caller that actually knows the answer -- from_usd_attr -- passes a tuple,
                even () for "bound outside any variant".
            read_back: Take the attr's value into the param, which is what a load wants.
                False for a caller that has just written the param's own value into the
                attr: reading it back then only rounds a double through a float attr, and
                the param announces a change it never had.
        """
        if var_selections is not None:
            self._var_selections = tuple(var_selections)
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
        if isinstance(attr, Usd.Attribute) and read_back:
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
                # bare @a.x@@b.y@ expression from the connections. Lossy by nature -- the
                # operators were never in the topology -- so it is only correct for the
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
    def _expression_from_connections(connections) -> str | None:
        tokens = []
        for conn in connections:
            path = Sdf.Path(str(conn))
            node_name = path.GetPrimPath().name
            param_name = path.name.rsplit(":", 1)[-1]
            tokens.append(f"@{node_name}.{param_name}@")
        return "".join(tokens) if tokens else None

    def _resolve_expression_ref(self, node_name: str, param_name: str) -> ChunkCommon | None:
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

    def connect_usd_attr(self, attr: Usd.Property, var_selections: tuple = ()) -> None:
        """Drive a real prim attribute from this param's storage attribute via a USD connection.

        The storage attr becomes the connection source; the real attr is the target. If the
        real attr does not exist yet it is created in _author_driven_connections once the
        storage attr is known. If the storage attr is not yet known the whole operation is
        deferred until _connect_param_attr is called.

        Args:
            attr (Usd.Property): The real prim attribute to drive (e.g. /cameras/Cam.focalLength).
                May be an invalid/non-existent attribute -- the path is stored and the attribute
                is created on demand.
            var_selections: The (anchor_prim_path, vs_name, variant) tuples active when attr was
                resolved, if any -- see _driven_var_selections. chunk_stage._drive_from_storage
                passes the param's own _var_selections through here when it promotes a direct
                real-attr binding (show_attribute) into one the storage attr drives.
        """
        path = str(attr.GetPath())
        if path not in self._driven_attr_paths:
            self._driven_attr_paths.append(path)
        if var_selections:
            self._driven_var_selections[path] = tuple(var_selections)
        elif path in self._driven_var_selections:
            del self._driven_var_selections[path]
        if self._param_attr_path:
            self._author_driven_connections()

    def _author_driven_connections(self) -> None:
        """Connect every driven real attribute to this param's storage attribute.

        For every path in _driven_attr_paths the attribute is created on the real prim if absent
        (with the storage attr's type), then real_attr.SetConnections([storage_path]) is authored.

        Always writes into the chunk's own layer (anonymous for procedural, file for
        interactive) so these connections never land in the session layer and can be
        overridden in the same layer by wire_shader_input after connect_usd_attr -- unless
        connect_usd_attr recorded a variant scope for that path (show_attribute, from inside
        a variant), in which case the connection is authored inside that variant instead, so
        it doesn't leak into every variant equally.
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
        if chunk_layer and stage.IsLayerMuted(chunk_layer.identifier):
            chunk_layer = None  # can't author into it; the connection is topology only

        for path in self._driven_attr_paths:
            sdf_path = Sdf.Path(path)
            # Inside the recorded variant before the lookup -- see _write_driven_attrs.
            var_selections = self._driven_var_selections.get(path, ())
            with chunk_utils.edit_context_for(stage, var_selections, chunk_layer):
                real_attr = stage.GetAttributeAtPath(sdf_path)
                if not (real_attr and real_attr.IsValid()):
                    prim = stage.GetPrimAtPath(sdf_path.GetPrimPath())
                    if not (prim and prim.IsValid()):
                        continue
                    real_attr = prim.CreateAttribute(sdf_path.name, type_name, custom=False)
                if real_attr and real_attr.IsValid():
                    real_attr.SetConnections([storage_path])

        # SetConnections only authors topology -- Get() on the driven attr doesn't follow
        # it, so push the current resolved value too. Needed because connect_usd_attr is
        # (re-)called on every load/execute, e.g. after a roundtrip restores an expression
        # via _connect_param_attr before this chunk's own driven wiring runs.
        self._write_driven_attrs(stage, getattr(self, 'value', None))

    @classmethod
    def from_usd_attr(cls, attr: Usd.Attribute, *args, **kwargs) -> ChunkUsdAttributeSupport:
        """Create a param from a Usd.Attribute, bound so the param's changes write the attribute.

        Args:
            attr (Usd.Attribute): The attribute to represent as a param
            *args: Passed to the param constructor.
            **kwargs: Passed to the param constructor; `name` defaults to the attribute's base
                name. `var_selections` (see ChunkUsdAttributeSupport._var_selections) is popped
                out before construction -- it is never a constructor argument, only something
                _connect_param_attr records afterward.

        Returns:
            ChunkUsdAttributeSupport: The new param
        """
        var_selections = kwargs.pop("var_selections", ())
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

        param._connect_param_attr(attr, var_selections=var_selections)

        return param


class ChunkParamBase(Generic[ValueT], ChunkCommon, ChunkUsdAttributeSupport):
    """Base class for valued params.

    Attributes:
        layout (ChunkFlags.LayoutFlags): The layout of this parameter. Defaults to HORIZONTAL
        default_value (ValueT | None): The default value of this parameter
        value (ValueT | None): The value of this parameter
        custom_widget (ChunkParamWidgetBase | None): An optional custom widget. get_type()
            must resolve to ParamType.CUSTOM for it to be used
    """

    layout: ChunkFlags.LayoutFlags = attrs.field(default=ChunkFlags.LayoutFlags.HORIZONTAL)

    default_value: ValueT | None = attrs.field(default=None)

    # Value is not an argument to __init__. Will set to default_value
    value: ValueT = attrs.field(
        default=None, init=False, metadata=AttributeMetadata(sender_alias=chunk_notice.ChunkNoticeAlias.VALUE_CHANGED)
    )

    # If possible, will use this custom widget in the ui.
    # TODO: Currently, this only works with ChunkParamBase objects with type == CUSTOM
    #       This should also work on groups and chunks... But thats a bit more complicated
    custom_widget: ChunkParamWidgetBase | None = attrs.field(default=None)

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

    @override
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

    @override
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
        """The ParamType of this param class, which decides the widget a UI builds for it.

        Required of every valued param. If this returns a combination of ParamTypes, the
        subclass also implements evaluate_type() to pick the one in use at runtime.

        Returns:
            ChunkFlags.ParamType: The type. NONE by default; subclasses override it
        """
        return ChunkFlags.ParamType.NONE

    def evaluate_type(self) -> ChunkFlags.ParamType:
        """The ParamType this param has right now.

        If get_type() allows several types (FLOAT_SLIDER | INT_SLIDER), this decides which one
        is in use.

        Returns:
            ChunkFlags.ParamType: The runtime type. get_type() by default
        """
        return self.get_type()

    @override
    def get_notice_alias(self):
        return chunk_notice.ChunkNoticeAlias.PARAM_CHANGED

    def _cross_chunk_ref(self, other: ChunkCommon) -> tuple[ChunkBase, ChunkBase] | None:
        """Return (my chunk, other's chunk) when the two params live on different chunks.

        None means "same chunk, or not resolvable yet" -- a param still being constructed
        has no owner to walk up to, and warning about an unresolved owner would fire on
        every list-group template and every param built before it is added to anything.
        """
        mine, theirs = self.get_chunk(), other.get_chunk()
        if mine is None or theirs is None or mine is theirs:
            return None
        return mine, theirs

    def _expression_ref_to(self, other: ChunkCommon, other_chunk: ChunkBase) -> str | None:
        """The @Node.param@ text that would reference other, or None if it can't be named.

        The grammar is @word.word@ (chunk_utils._EXPR_RE), so the reference carries a bare
        param name -- which _resolve_expression_ref then looks up with get_param. A nested
        param is fine (get_param finds it by name), but a name shared by two params in the
        same chunk resolves to whichever comes first, so check that the lookup actually
        lands back on this param before promising an expression can address it.
        """
        if other_chunk.get_param(other.name) is not other:
            return None
        return f"@{other_chunk.name}.{other.name}@"

    def add_dependency(self, input: chunk_notice.ChunkNoticePropagator) -> int:
        """Add a DagNode input, warning if it crosses a chunk boundary.

        A param-level dependency is in-memory only: ChunkStageSync._on_dependency_added
        authors inputs:chunk for ChunkBase pairs and returns early for anything else, so
        an edge between two params is never written to USD, draws no wire in the node
        graph, and is gone after a reload. Within one chunk that is exactly right -- this
        is the dirty/ordering edge behind set_function, and it can drive attributes
        (menu_items, a label, visibility) that no expression can reach. Across two chunks
        it silently loses the connection the caller thought they made; an expression is
        the mechanism that persists. See connect_value, which redirects rather than warns.

        Args:
            input: The propagator to depend on.

        Returns:
            The dependency index.
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

    def connect_value(self, other: ChunkParamBase[ValueT]):
        """Make this param's value follow another param's.

        Across chunks this authors an expression instead of a DagNode edge, since an expression
        is written to USD, draws a wire and survives a reload.

        Args:
            other: The param to follow.

        Raises:
            ValueError: The two params' types are not compatible.
        """
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

    #: Param types whose value USD itself evaluates expression variables in: an asset path.
    #: A variant selection is the other place USD substitutes, and chunks author those as
    #: text from a chunk module rather than from a param (see chunks/usd/set_variant.py).
    _STAGE_EXPRESSION_TYPES = ChunkFlags.ParamType.FILE

    @override
    def value_changed(self, attrname: str, old_value: Any, value: Any):
        if attrname == "default_value" and hasattr(self, "value") and (self.value is None or self.value == old_value):
            self.value = value

        if attrname == "value":
            # A prim path set inside a namespaced host is stored relative to the namespace,
            # whatever it arrived as: a viewport pick hands over the full stage path, and a
            # chunk that writes its actual path back (prim.py) hands over the resolved,
            # already-prefixed one. Stored as-is, the read would prefix it again.
            if isinstance(value, str) and value and self._is_prim_path_param():
                namespace = self.network_namespace()
                if namespace:
                    stripped = chunk_utils.strip_namespace(namespace, value)
                    if stripped != value:
                        self.__dict__["value"] = stripped
                        value = stripped
            if isinstance(value, str) and chunk_utils.has_stage_expression(value):
                # `in`, not `&`: ParamType packs a widget variant beside its value type, so
                # FILE and LINEEDIT share the STRING bit and a mask test is true for both.
                if self._STAGE_EXPRESSION_TYPES not in self.evaluate_type():
                    # Not an error and not rewritten -- the text is kept exactly as typed --
                    # but it will never resolve, and silently reading back as the literal
                    # "${SHOT}" is worse than saying so. USD substitutes expression variables
                    # into asset paths and variant selections only; a Stage Expression Switch
                    # is how a chunk graph acts on one anywhere else.
                    logger.warning(
                        "%s: expression variables only resolve in asset paths, so %r stays "
                        "literal here. Branch on it with a Stage Expression Switch instead.",
                        self.name, value,
                    )
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
                            logger.debug("Expression ref @%s.%s@ not resolvable yet (loading)", node_name, param_name)
                        else:
                            logger.warning("Expression ref @%s.%s@ not found", node_name, param_name)
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
        self.refresh_expression_value()

    def refresh_expression_value(self) -> None:
        """Re-announce this param's value because what its expression reads has changed.

        The raw text is untouched -- only what it resolves to moved -- so nothing would
        otherwise fire: widgets keep showing the old number and the USD attrs this param
        drives keep the old value. Used by an expression ref changing upstream and by a
        Stage Expression changing a variable.
        """
        if self._resolving_ref:
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

    @override
    def __getattribute__(self, name: str) -> Any:
        if name == "value" and "value" in self.__dict__:
            raw = self.__dict__["value"]
            if isinstance(raw, str) and chunk_utils.has_expression(raw):
                raw = self._resolve_expression_string(raw)
            # A ${VAR} is deliberately NOT substituted here. USD evaluates expression
            # variables in asset paths and variant selections and nowhere else, so a param
            # carrying one hands the text through untouched and lets composition do it --
            # the one thing usd_chunks must not do is make them work in places OpenUSD says
            # they do not. See ChunkParamBase.value_changed for the warning on misuse.
            #
            # A prim path inside a namespaced host resolves under the namespace: the
            # network's chunks were written against the root, and the host says where in
            # the scene that root is. raw_value stays what the file holds.
            if isinstance(raw, str) and raw and self._is_prim_path_param():
                namespace = self.network_namespace()
                if namespace:
                    raw = chunk_utils.apply_namespace(namespace, raw)
            return raw
        return super().__getattribute__(name)

    def _is_prim_path_param(self) -> bool:
        """Whether this param names prim paths.

        Decided by the convention the gizmo system also reads (chunk_gizmo): a LineEdit called
        primpath_location, or one flagged PRIM_SELECTION. There is no type-level marker for a
        prim path; this is it.
        """
        try:
            if ChunkFlags.ParamType.LINEEDIT not in self.get_type():
                return False
        except Exception:
            return False
        return self.name == "primpath_location" or self.has_flag(ChunkFlags.ParamFlags.PRIM_SELECTION)

    def network_namespace(self) -> str:
        """The scene root every prim path this param names is relative to.

        That is the namespaces of the network hosts enclosing it (chunk_stage.host_networks),
        outermost first, joined. Empty outside any namespaced host, which is the common case
        and costs a walk up the owner chain.

        Returns:
            The namespace, "" outside any namespaced host.
        """
        parts = []
        # From the owning chunk's owner, not from the param's: a host's own params are its
        # interface to the graph outside, and its location field IS the namespace -- it
        # must not resolve inside it, or typing /a/b there with /a set reads back as /b.
        chunk = self.get_chunk()
        owner = chunk.get_owner() if chunk is not None else self.get_owner()
        while owner is not None:
            namespace = getattr(owner, "_network_namespace", "")
            if namespace:
                parts.append(namespace)
            owner = getattr(owner, "get_owner", lambda: None)()
        return "".join(reversed(parts))

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
        # gets the wrong type. String params keep interpolation -- that is what builds a
        # path like "@A.primpath_location@/geo" -- and eval_arithmetic returns None for
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
    """Base class for containers: Vectors, Groups and Chunks.

    A container holds params (ChunkParamBase) and other containers.

    Attributes:
        children (NameSyncedContainer): The container owning the child params
        widget_factory (ChunkWidgetFactoryBase | None): A factory a UI implementation can use
            to replace the default widgets of the container and its children
    """

    children: NameSyncedContainer[ChunkCommon] = attrs.field(
        init=False,
        factory=NameSyncedContainer[ChunkCommon],
        metadata=AttributeMetadata(send_mode=SendMode.NO_SEND, copy_mode=CopyMode.COPY),
    )

    widget_factory: ChunkWidgetFactoryBase | None = attrs.field(
        default=None,
        validator=attrs.validators.optional(attrs.validators.instance_of(ChunkWidgetFactoryBase)),
        metadata=AttributeMetadata(copy_mode=CopyMode.SHARE),
    )

    @override
    def __attrs_post_init__(self):
        super().__attrs_post_init__()
        self.children.set_owner(self)

    @override
    def get_notice_alias(self) -> chunk_notice.ChunkNoticeAlias:
        return chunk_notice.ChunkNoticeAlias.GROUP_CHANGED

    @override
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
        """Remove a direct child of this container.

        Args:
            param (ChunkCommon): The child to remove
        """
        own_p = self.children.pop(param)
        if not own_p:
            return

        own_p.set_owner(None)

    def request_delete(self):
        """Delete this container, and with it its children."""
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
        # leaving that sub-chunk alive but gutted -- get_param() returning None on a chunk that
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
    def get_param(self, param: ChunkCommon | str | HashVal) -> ChunkCommon | None: ...

    @overload
    def get_param(self, param: ChunkCommon | str | HashVal, predicate: Predicate[ValueT] | None) -> ValueT: ...

    def get_param(
        self, param: ChunkCommon | str | HashVal, predicate: Predicate[ValueT] | None = None
    ) -> ChunkCommon | None | ValueT:
        """Find a param or group inside this container, by object, hash, name or dotted path.

        For example::

            my_chunk.get_param("groupA.groupB.param")

        A bare name returns the first param with that name. A group requires unique names among
        its own children only, so several params in one hierarchy can share a name.

        A predicate narrows and checks the result type::

            p = my_chunk.get_param("a_slider", chunk.is_inst(chunk.Slider))
            p.value = 10

        Args:
            param (ChunkCommon | str | HashVal): The param to find, or its name or path
            predicate (Predicate[ValueT] | None): Only return the param if it matches this

        Returns:
            ChunkCommon | None: The param, or None if not found
        """
        if isinstance(param, (ChunkCommon, HashVal, ProxyParam)):
            return self.children.get_param(param, predicate)
        elif isinstance(param, str):
            return self.children.get_by_name(param, predicate)

    @overload
    def get_params(self) -> tuple[ChunkCommon, ...]: ...

    @overload
    def get_params(self, predicate: Predicate[ValueT] | None) -> tuple[ValueT, ...]: ...

    def get_params(self, predicate: Predicate[ValueT] | None = None) -> tuple[ChunkCommon, ...] | tuple[ValueT, ...]:
        """Get every param in this container, nested ones included.

        Args:
            predicate (Predicate[ValueT] | None): Only return params matching this. Defaults to None.

        Returns:
            tuple[ChunkCommon, ...]: The params
        """
        return self.children.get_params(predicate)

    @overload
    def get_my_params(self) -> tuple[ChunkCommon, ...]: ...

    @overload
    def get_my_params(self, predicate: Predicate[ValueT] | None) -> tuple[ValueT, ...]: ...

    def get_my_params(
        self, predicate: Predicate[ValueT] | None = None
    ) -> tuple[ChunkCommon, ...] | tuple[ValueT, ...]:
        """Get the params added directly to this container.

        Args:
            predicate (Predicate[ValueT] | None): Only return params matching this. Defaults to None.

        Returns:
            tuple[ChunkCommon, ...]: The params
        """
        return self.children.get_my_params(predicate)

    def check_hierarchy(self):
        """A debug function for checking the hierarchy structure of a group.

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
        found_names: dict[int, set[str]] = {}

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
        """Print the container and its children as a tree, for debugging."""

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

    def iter_hierarchy(self, level=0) -> Generator[tuple[int, ChunkCommon], None, None]:
        """Walk this container and everything under it, depth first.

        Args:
            level (int, optional): The depth of this container. Defaults to 0.

        Yields:
            tuple[int, ChunkCommon]: (depth, object), starting with this container
        """
        yield level, self
        for p in self.get_my_params():
            if isinstance(p, ChunkGroupBase):
                yield from p.iter_hierarchy(level + 1)
            else:
                yield level + 1, p


class ChunkGroupBase(ChunkContainerBase):
    """Base class for dynamic containers, which can hold any number of children of any type.

    Chunks and Groups are dynamic in that sense. Vectors are not, and do not subclass this.
    """

    def add_params(self, params: Iterable[ChunkT], force_unique: bool = True, clone: bool = False) -> list[ChunkT]:
        """Add several params, calling add_param on each.

        Args:
            params (Iterable[ChunkCommon]): The chunk objects to add
            force_unique (bool, optional): Force unique param names. Defaults to True.
            clone (bool, optional): Add clones instead of the params themselves. Defaults to False.

        Returns:
            list[ChunkCommon]: The added params, which differ from the input if clones were made
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
        """Add a param to this group's children.

        Changes to the param propagate to this group, since they share the same hierarchy; see
        ChunkNoticePropagator. To add the same param several times, add clones::

            group.add_param(param.clone())
            group.add_params([param, param.clone(), param.clone()])

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
                # Also append the children of the group, as this allows for fast lookups across the layers
                self.children.add_nested(new_param.children)

        else:
            raise ValueError("Chunks only support params inherited from ChunkCommon")
        return new_param

    def resolve_new_param(self, param: ChunkT) -> ChunkT | None:
        """The param, renamed if its name conflicts with another child of this group.

        Args:
            param (ChunkParamBase): A param or container

        Returns:
            ChunkT: The param, possibly with a new name

        Raises:
            ValueError: The same param (same hash) is already here
        """
        # A key lookup down the ChainMap, not `param in self.get_params()`: that materialised
        # every param of every nested container (a tuple of ChainMap.values(), each entry a
        # walk of the chain) to answer a membership question, on every add. On a context it
        # is the whole scene, so appending the 50th chunk to a chain cost 5x the first --
        # the one thing about a node graph that must not get slower as the graph grows.
        if param in self.children:
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

    def get_reserved_names(self) -> set[str]:
        """The names a new child may not take: every direct child's name, and this group's own.

        Returns:
            set[str]: The reserved names
        """
        return set(self.children.get_my_names()) | set([self.name])

    @staticmethod
    def get_valid_child_name(orig_name: str, reserved_names: set[str]) -> str:
        """A unique form of orig_name, numbered so it does not clash with reserved_names.

        Args:
            orig_name (str): The name to make valid
            reserved_names (set[str]): The names already taken. The new name is added to it, so
                a group nested inside an already created group gets a unique name too

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
    """Base class for list containers, whose rows each hold proxies of the children.

    Besides the container's children, the group holds one group per row, holding proxies of
    those children. A UI shows the rows (proxy_params) and hides the children. A proxy is
    practically identical to the param it references, except for its 'value': renaming the
    referenced param renames every proxy of it. Proxies are read and set like regular
    params, but reached through get_indexed_param() rather than get_param().

    get_params() and get_my_params() return the proxies; get_param() does not, and returns
    from the 'children' container the proxies reference. So get_param() changes every row
    at once, and add_param() adds a param to every row.

    Attributes:
        size (int): The number of rows. It can change at runtime to grow or shrink the list
        proxy_params (list[ChunkGroupBase]): The groups holding each row's proxies. Managed
            internally, never modified from outside
    """

    size: int = attrs.field(default=0)
    proxy_params: list[ChunkGroupBase] = attrs.field(factory=list, init=False)

    @overload
    def get_indexed_param(self, param: ChunkCommon | str | HashVal, index: int) -> ChunkCommon | None: ...
    @overload
    def get_indexed_param(
        self, param: ChunkCommon | str | HashVal, index: int, predicate: Predicate[ValueT] | None = None
    ) -> ValueT: ...

    def get_indexed_param(
        self, param: ChunkCommon | str | HashVal, index: int, predicate: Predicate[ValueT] | None = None
    ) -> ChunkCommon | None | ValueT:
        """The proxy of a param in one row.

        Args:
            param (ChunkCommon | str | HashVal): The param to look for, or its name
            index (int): The row, an index into proxy_params
            predicate (Predicate[ValueT] | None): Only return the proxy if it matches this. Defaults to None

        Returns:
            ChunkCommon | None: The proxy of param in that row

        Raises:
            IndexError: If the index is out of bounds
        """
        if len(self.proxy_params) == 0 and not predicate:
            return

        if index >= len(self.proxy_params):
            raise IndexError("Index out of range")

        res = self.proxy_params[index].get_param(param)
        if not predicate or predicate(res):
            return res

        raise ValueError(f"Predicate {predicate} failed for get_indexed_param with index {index}")


    @override
    def value_changed(self, attrname: str, old_value: Any, value: Any):
        if old_value is None or attrname != "size":
            return super().value_changed(attrname, old_value, value)

        if value > old_value:
            for i in range(old_value, value):
                # No LABELED: the index string ("0", "1", ...) as a row header serves
                # little purpose and just costs vertical space -- the row's own fields
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
    def get_params(self) -> tuple[ChunkCommon, ...]: ...

    @overload
    def get_params(self, predicate: Predicate[ValueT] | None = None) -> tuple[ValueT, ...]: ...

    @override
    def get_params(self, predicate: Predicate[ValueT] | None = None):
        res = []
        for container in self.proxy_params:
            res.extend(container.get_params(predicate))
        return tuple(res)

    @override
    def get_my_params(self, *args, **kwargs) -> tuple[ChunkGroupBase, ...]:
        return tuple(self.proxy_params)

    def remove_index(self, index: int):
        """Remove one row, reducing the size by 1.

        The row's group gets request_delete(). An index out of range is logged and ignored.

        Args:
            index (int): The row to remove
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

    @override
    def add_param(self, param: ChunkT, force_unique: bool = True) -> ChunkT:
        param = super().add_param(param, force_unique)

        for proxy_group in self.proxy_params:
            proxy_group.add_param(ProxyParam(param))  # type: ignore
        return param


class ChunkBase(ChunkGroupBase):
    """Base class for Chunks. Similar to a group but meant to be the top level owner of other params."""

    # The config is set from the chunk_registry.ChunkModule.load_config() function.
    # This is because the config can be loaded independently from the chunk, without
    # having to execute main() of the chunk module.
    _config: ChunkConfig | None = attrs.field(default=None, init=False, metadata=AttributeMetadata(serialize=True))

    # For CONTEXT chunks: the USD prim path this context maps to. Chunks within
    # the context are placed at {prim_path}/chunks/{chunk_name}. Falls back to
    # _CHUNKS_ROOT when None.
    prim_path: str | None = attrs.field(default=None)

    @override
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

    def __enter__(self) -> Self:
        """`with chunk:` -- where chunks created inside the block go is the scene's to say
        (see SceneSender.enter_chunk)."""
        self.get_scene().enter_chunk(self)
        return self

    def __exit__(self, *exc) -> None:
        self.get_scene().exit_chunk(self)

    @override
    def convert_attr(self, attr: attrs.Attribute, val: Any) -> Any:
        # Sanitize each path segment so prim_path stays a valid USD path, but leave None
        # alone -- otherwise str(None) makes prim_path the string "None", which is truthy
        # and Sdf.Path("None") collapses to an invalid empty path (breaks chunkgroups /
        # _resolve_chunk_root).
        #
        # Every illegal character matters, not just spaces. Anything -- a comma or a hyphen in a context's display name
        # ("SO-101 Bench", "One Stage, Three Shots") -- produced a prim_path Sdf.Path
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
        """Send GIZMO_START; a callback for when a host starts a drag."""
        self.send_self(chunk_notice.ChunkNoticeAlias.GIZMO_START)

    def stop_gizmo(self, notice=None, sender=None):
        """Send GIZMO_STOP; a callback for when a host ends a drag."""
        self.send_self(chunk_notice.ChunkNoticeAlias.GIZMO_STOP)

    def disable(self, notice=None, sender=None):
        """Set the DISABLED state; usable as a callback."""
        self.set_state(ChunkFlags.State.DISABLED)

    def enable(self, notice=None, sender=None):
        """Clear the DISABLED state; usable as a callback."""
        self.unset_state(ChunkFlags.State.DISABLED)

    def remove(self, notice=None, sender=None):
        """Delete this chunk; usable as a callback."""
        self.request_delete()

    def trigger_on_create_callbacks(self, notice=None, sender=None):
        """Trigger every on-create callback in this chunk's hierarchy.

        Runs once the chunk is loaded and added to the scene, as a callback on CHUNK_LOADED.

        Args:
            notice: The notice, when called as a callback.
            sender: The sender, when called as a callback.
        """
        for _, child in self.iter_hierarchy():
            for cb in child.get_on_create_callbacks():
                if cb.alias == chunk_notice.ChunkNoticeAlias.CHUNK_LOADED:
                    continue
                cb.trigger()

    def get_config(self) -> ChunkConfig | None:
        """The config this chunk's module declared, or None."""
        return self._config

    @override
    def get_edit_context(self) -> Sdf.Layer | None:
        if self._config and self._config.interactive:
            return None
        return super().get_edit_context()

    def reauthor_driven_attrs(self) -> None:
        """Author again the connection and value of every param of this chunk that drives a
        real prim attribute (connect_usd_attr).

        Those live in the chunk's layer, but they are the params' state rather than what a
        cook produced: a param is bound once -- by the module, by show_attribute, or on
        being edited away from its default -- and stays bound whether or not the next cook
        binds it again. The framework clears the layer before every cook, so it puts them
        back after one. Only this chunk's own params: a chunk nested inside keeps its own.
        """
        for _, param in self.iter_hierarchy():
            if getattr(param, "_driven_attr_paths", None) and param.get_chunk() is self:
                param._author_driven_connections()

    @override
    def get_chunk_module(self) -> chunk_registry.ChunkModule | None:
        """The registered module this chunk's type names, or None.

        Looked up in the registry by config.module -- the same name a saved file records as
        info:chunkType -- rather than held on the chunk. A chunk can therefore only ever run
        what a registered module defines, which is exactly what reopening the file will run:
        a saved file is the whole description of what the network does.

        The registry's module is the copy chunks are built from. A chunk module can exist
        twice in sys.modules under different keys (its bare name, and a package path when
        its folder is importable), so it is found by the registry's name, never by import.

        A dict lookup (ChunkRegistry.get_chunk_module does no discovery), so it is cheap
        enough for the gizmo hooks, which are asked per draw and per drag tick.

        Returns:
            The ChunkModule, or None for a chunk no module made (a plain Chunk built in the
            interpreter, a tab context) or whose module is not registered.
        """
        if not (cfg := self._config) or not cfg.module:
            return None
        return chunk_registry.ChunkRegistry().get_chunk_module(cfg.module)

    def _module_hook(self, name: str) -> Callable | None:
        """The function this chunk's module defines under name, or None.

        The single place a chunk's behaviour hooks -- execute, commit, the gizmo hooks -- are
        resolved: everything that runs module code for a chunk asks here rather than reading
        the module itself. It reads the module get_chunk_module finds in the registry, so
        whatever the registry holds -- after ChunkRegistry.reload_modules too -- is what runs.

        Args:
            name: The hook's name in the module, e.g. "execute".

        Returns:
            The callable, or None when the chunk has no module or its module defines no
            callable of that name.
        """
        module = getattr(self.get_chunk_module(), "module", None)
        hook = getattr(module, name, None)
        return hook if callable(hook) else None

    def can_cook(self) -> bool:
        """Whether this chunk's module defines execute(), i.e. whether cook() has anything to run.

        A chunk without one (a chunkgroup) holds other chunks and authors nothing itself.
        """
        return self._module_hook("execute") is not None

    def cook(self) -> bool:
        """Run this chunk's module into its own edit layer, now, and nothing downstream of it.

        Owned by the framework, not meant to be overridden: a chunk's behaviour is its
        module's execute(). Deciding WHEN to cook -- a data change rather than a pure state
        change, not twice in one batch -- is the solve's (ChunkNoticePropagator.execute);
        this only decides whether the chunk CAN run right now, and runs it. It does not
        mark the chunk stale first: the solve does, so a cook that is refused leaves it
        stale for recook_for_view to pick up.

        Returns:
            True if the module was run -- also when its layer left the stage's layer stack
            part way through, which is skipped rather than raised. False if a guard refused
            it (no execute(), no stage, muted, not in the layer stack yet, blocked, being
            deleted, or already executing).
        """
        execute = self._module_hook("execute")
        stage = self.get_stage()
        ctx = self.get_edit_context()
        # A disabled chunk's edit layer is muted, i.e. removed from the stage's local
        # layer stack. Don't re-run its module: authoring into a muted layer raises
        # ("Layer ... is not in the local LayerStack"), and a disabled chunk must not
        # execute anyway.
        muted = bool(stage and ctx and stage.IsLayerMuted(ctx.identifier))
        # Separately: a chunk's own edit layer may not be inserted into the stage's
        # local layer stack YET at all -- not muted (present-but-disabled), just not
        # there yet. Hit live during load_chunks's bulk restore: a ListGroup proxy
        # row's value setter (chunk_core.py's ProxyParam.__setattr__, "value" case)
        # calls straight through to this same dispatch, bypassing the
        # chunk_notice._loading_stage guard that suppresses every OTHER reactive path
        # during that restore -- reaching here before get_or_create_layer has run for
        # this specific chunk, same "not in the local LayerStack" raise as the muted
        # case. load_chunks's own later, correctly-ordered explicit .execute() call
        # (after every chunk's layer is properly registered) still runs normally.
        not_yet_in_stack = bool(stage and ctx and not stage.HasLocalLayer(ctx))
        # A blocked chunk (view-driven: not in the active view's tree -- e.g. connected to
        # an inactive view) must never run its module, whatever triggered the solve. recook
        # unblocks the active view's tree before executing it, so cooking is unaffected.
        # A chunk being deleted must not run its module -- teardown notices can land
        # back here after its params are already detached (see request_delete).
        # With no stage there is nothing for a module to author into: every chunk module
        # works through get_stage(), so running one stage-less is never useful and
        # crashes on the first stage call. The host can genuinely have no stage for a
        # window -- usdview clears its stage before opening the next file while still
        # pumping the Qt event loop, so a deferred rebuild, or a click landing in that
        # window, can reach here.
        if not (execute is not None and stage is not None
                and not getattr(self, '_executing', False)
                and not getattr(self, '_deleting', False)
                and not muted and not not_yet_in_stack and not self.is_blocked()):
            return False

        with chunk_notice.executing(self):
            if ctx:
                try:
                    with Usd.EditContext(stage, ctx):
                        # A cook starts from an empty layer, so a module
                        # authors its whole result every time and never reads
                        # its own last one back. Anonymous only: that is a
                        # layer this chunk alone owns and rebuilds on load.
                        # A chunk in a context with a file layer is handed
                        # that file layer, shared and the user's own.
                        if ctx.anonymous:
                            ctx.Clear()
                        execute(self)
                        # What a param drives is its own state, not the cook's
                        # output -- see ChunkBase.reauthor_driven_attrs.
                        if ctx.anonymous:
                            self.reauthor_driven_attrs()
                    self._cook_stale = False
                except Tf.ErrorException:
                    # The layer left the stage's local layer stack between the
                    # check above and here. It can: a cook reached from inside
                    # another cook mutes layers (a gate suppressing a branch),
                    # and this dispatch is reached re-entrantly -- a module
                    # setting a param during its own execute lands straight back
                    # here for whatever that param drives.
                    #
                    # Re-checked rather than swallowed: only the "not in the
                    # local LayerStack" case is skipped, and any other error a
                    # module raises still surfaces. Skipping is right because it
                    # is what the guards above already decided -- a chunk whose
                    # layer is not in the stack has nowhere to author, and the
                    # cook that put it back will run it.
                    if stage.IsLayerMuted(ctx.identifier) or not stage.HasLocalLayer(ctx):
                        # Logged as the solve always has, under chunk_notice.
                        chunk_notice.logger.debug(
                            "skipped cooking %s: its layer left the stage's "
                            "layer stack mid-solve",
                            self.name,
                        )
                    else:
                        raise
            else:
                # No edit layer of its own -- author into the stage's current target.
                execute(self)
                self._cook_stale = False
        return True

    @override
    def get_chunk(self) -> ChunkBase | None:
        return self

    @override
    def get_notice_alias(self) -> chunk_notice.ChunkNoticeAlias:
        return chunk_notice.ChunkNoticeAlias.CHUNK_CHANGED

    @override
    def add_param(self, param: ChunkT, force_unique: bool = True, notify: bool = True) -> ChunkT:
        into_context = isinstance(param, ChunkBase) and self.has_flag(ChunkFlags.GroupFlags.CONTEXT)
        if into_context:
            # A context is what a scene holds, so this is where a chunk enters one -- every
            # path does: SceneSender.add_chunk, a paste, a load, grouping into a chunkgroup.
            # Before anything is added, so a refused chunk leaves nothing half-wired.
            check_chunk_type(param)
        elif isinstance(param, ChunkBase) and self.get_context() is not None:
            # Nested into a chunk that is already in a scene, so it enters the scene here
            # instead -- after the check its owner passed on the way in. Lacking a module of
            # its own, it answers to its owner's, as check_chunk_type does for a chunk nested
            # before entry. A chunk being built outside any scene (a module's main()) is not
            # checked yet: it is, whole, when it enters one.
            check_chunk_type(param, self.get_chunk_module())
        do_context_setup = notify and into_context
        if do_context_setup:
            # Clear the chunk's edit layer to a fresh slate BEFORE adding -- adding fires
            # PARAM_ADDED, which can trigger the chunk's execute() (e.g. via _wire_inputs);
            # clearing afterwards would wipe what execute just authored.
            lm = chunk_registry.ChunkLayerManager()
            if lm.initialized:
                if layer := lm.get_or_create_layer(param):
                    layer.Clear()
                    chunk_notice.mark_cook_stale(param)
        res = super().add_param(param, force_unique)
        if do_context_setup:
            chunk_notice.send_notice(chunk_notice.ChunkNoticeAlias.CHUNK_LOADED, param, data=param)
        return res

    def get_context(self) -> ChunkBase | None:
        """The nearest CONTEXT chunk at or above this one, or None."""
        owner = self
        while owner:
            if isinstance(owner, ChunkBase) and owner.has_flag(ChunkFlags.GroupFlags.CONTEXT):
                return owner
            owner = owner.get_owner()
        return None


class UnregisteredChunkTypeError(TypeError):
    """A chunk whose class is defined outside the framework and outside its registered module.

    Raised when such a chunk is added to a scene; see check_chunk_type.
    """


def _is_framework_class(cls: type) -> bool:
    """Whether cls is defined in usd_chunks itself (Chunk, ChunkBase, a context)."""
    return cls.__module__ == "usd_chunks" or cls.__module__.startswith("usd_chunks.")


def check_chunk_type(c: ChunkBase, fallback: chunk_registry.ChunkModule | None = None) -> None:
    """Refuse a chunk whose behaviour a saved file could not bring back.

    A chunk can't do anything its module doesn't define, so a saved file is always the whole
    description of what the network does: behaviour comes only from a registered chunk
    module, and data lives in USD. The one way round that is a chunk *class* -- a subclass
    of Chunk with methods, callbacks wired in __init__ or overrides -- defined somewhere a
    reopen never runs: a script, the interpreter, a test. It would work for this session
    and come back from the file as its registered type (or a plain runtime chunk), silently
    without whatever the class added.

    So a chunk entering a scene must be one of:

    - a framework class (Chunk, ChunkBase, a context -- anything in usd_chunks). It has no
      behaviour of its own: its hooks are looked up in its registered module, and one with
      no module has none (a chunk built in the interpreter, a tab context). Callbacks a
      script adds to it are session-only by design, like a script editor's.
    - a class defined in the registered module its config names. That is the module a
      reopen runs, so the class comes back with it.

    Compared by the registry's module name, not by module object: a chunk module can exist
    twice in sys.modules under different keys (its bare name, and a package path), and both
    resolve to one registered module. ChunkBase nested inside c are checked too, against
    their own module or, lacking one, c's -- the module whose main() built them.

    Args:
        c: The chunk being added.
        fallback: The module c answers to when it has none of its own: that of the chunk it
            is being nested into, when that one is already in a scene.

    Raises:
        UnregisteredChunkTypeError: c, or a chunk nested in it, has such a class.
    """
    registry = chunk_registry.ChunkRegistry()
    outer = c.get_chunk_module() or fallback
    for _, obj in c.iter_hierarchy():
        if not isinstance(obj, ChunkBase) or _is_framework_class(cls := type(obj)):
            continue
        own = obj.get_chunk_module() or outer
        if own is None or registry.get_chunk_module(cls.__module__) is not own:
            where = f"registered module {own.module_name!r}" if own else "no registered module"
            raise UnregisteredChunkTypeError(
                f"Cannot add chunk {obj.name!r}: its class {cls.__qualname__} is defined in "
                f"{cls.__module__!r}, and the chunk has {where}. A chunk type must live in a "
                "registered chunk module (a .py on USD_CHUNK_DIRS with main() and config()): "
                "a saved file records only the module's name, so a class defined anywhere "
                "else would not survive a save and reopen. Move the class into its chunk "
                "module, or build a plain chunk.Chunk and wire session callbacks onto it."
            )


class ChunkVectorBase(ChunkContainerBase, ChunkUsdAttributeSupport):
    """Base class for vector params: a container with a fixed number of params.

    A vector is used like a valued param, with 'value' reading and writing its children::

        vec = chunk.Vector("my_vec", param=chunk.Slider(), length=3, suffixes="xyz")

        vec[0] = 10
        vec.x = 10
        vec.value = (0, 1, 2)

    Attributes:
        param (ChunkParamBase): The param each component is cloned from
        length (int): How many components the vector has
        suffixes (tuple[str, ...] | str | None): A string or tuple of strings with
            len(suffixes) == length, naming the components. They are appended to the param's
            name if it has one
    """

    param: ChunkParamBase = attrs.field(default=None, repr=False, metadata=AttributeMetadata(serialize=False))
    length: int = attrs.field(default=0)
    suffixes: Sequence[str] | str | None = attrs.field(default=None)
    _default_value: Sequence[Any] | None = attrs.field(default=None, alias="default_value")

    # A Vector's own .flags is GroupFlags-typed (needed for widget construction -- see
    # has_flag override below) and can't also carry ChunkFlags.ParamFlags.OUTPUT: GroupFlags
    # and ParamFlags are separate IntFlag classes with overlapping bit values, and has_flag()
    # does a strict same-class check, so combining them via `|` makes OUTPUT silently
    # undetectable (or, the other way round, makes GroupFlags checks crash). is_output is a
    # dedicated field so a Vector can be a computed output without touching .flags at all.
    is_output: bool = attrs.field(default=False)

    # Raw @Node.vecParam@ expression string driving this vector's value, when set.
    # Whole-vector references only -- the referenced param must itself resolve to a
    # same-length sequence. None means the vector's value comes from its own children.
    _expr_value: str | None = attrs.field(
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

    @override
    def validate_attr(self, attr: attrs.Attribute, val: Any) -> bool:
        if attr.name == "suffixes" and val is not None:
            if len(val) < self.length:
                raise ValueError(
                    "Vector suffixes length is smaller than vector length. Set either as 'XYZ' or ('X', 'Y', 'Z') for a 3 length vector"
                )
        return super().validate_attr(attr, val)

    @override
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
                # Children are plain scalar params (Slider etc.) -- no GroupFlags/ParamFlags
                # collision for them, unlike the vector itself, so OUTPUT can go straight
                # on .flags to get the normal widget.set_enabled(False) treatment.
                np.set_flag(ChunkFlags.ParamFlags.OUTPUT)

            self.children.add(np)

        if self._default_value:
            self.default_value = self._default_value

    def __getitem__(self, key: str | int) -> ChunkParamBase:
        """A component by index or name, e.g. vec[0] or vec["x"].

        Args:
            key (str | int): name or index of the component

        Raises:
            KeyError: if key is not accessible

        Returns:
            ChunkParamBase: The component param
        """
        if isinstance(key, str):
            param = self.get_param(key)
        elif isinstance(key, int):
            param = self.get_my_params()[key]
        if not param or not isinstance(param, ChunkParamBase):
            raise KeyError(f"Index {key} does not exist on vector")
        return param

    @override
    def data_changed(self, notice: chunk_notice.ChunkNotice | None = None) -> chunk_notice.ChunkNotice | None:
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
            # the DAG), whose .value is a single scalar -- not usable here. This vector's
            # own self.value (tuple, already expression-aware) is what must be written.
            raw = self.raw_value
            is_expr = isinstance(raw, str) and chunk_utils.has_expression(raw)
            val = self.value
            # _set_children_blocked sets this while assigning each child in turn, to
            # avoid writing a partial tuple to the storage attr on the first (still
            # mid-assignment) bubble -- that write would otherwise be the one that lazily
            # authors the attr and never gets corrected afterward. Notices still need to
            # propagate normally during the loop (this doesn't use is_blocked()) so each
            # child's own widget refreshes live as it's set.
            if val is not None and not self._suppress_storage_write:
                prev_write = chunk_notice._usd_write_active
                chunk_notice._usd_write_active = True
                try:
                    # Inside a variant the write still goes into this chunk's own layer.
                    variant_layer = chunk_utils.chunk_write_layer(stage, self) if self._var_selections else None
                    with chunk_utils.edit_context_for(stage, self._var_selections, variant_layer):
                        if self._param_attr_path and (usd_attr := stage.GetAttributeAtPath(Sdf.Path(self._param_attr_path))):
                            # See ChunkParamBase.data_changed for why a param bound from inside a
                            # variant (show_attribute) must re-enter it here rather than rely on
                            # the ambient edit target -- that is the ordinary case for a ChunkVectorBase
                            # too (displayColor, extent, ... are common on a variant-scoped prim).
                            # Inside one, the chunk's own layer, where a chunk's edits always go.
                            #
                            # Outside a variant, the attr's own home layer, as its scalar
                            # counterpart resolves it. This used to keep the ambient edit
                            # target, which is not where _sync_vector_input authors the attr
                            # (the bookkeeping layer, session by default) -- so every edit
                            # after the first landed in a weaker layer than the value it was
                            # meant to replace, and a vector never changed again.
                            home = chunk_utils.get_attr_home_layer(usd_attr) or chunk_utils.get_bookkeeping_layer(
                                stage, self.get_chunk()
                            )
                            if not is_expr:
                                current = usd_attr.Get(0)
                                if current != val:
                                    with chunk_utils.edit_context_for(stage, self._var_selections, home):
                                        usd_attr.Set(chunk_utils.as_attr_value(usd_attr, val))
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

    def __setitem__(self, key: str | int, val: Any) -> ChunkParamBase | None:
        """Set a component's value by index or name.

        Args:
            key: The component's index or name.
            val: The new value.

        Raises:
            ValueError: The component is a container, not a valued param.
        """
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
        """The components' values as a tuple, or the resolved expression if one is set."""
        if self._expr_value is not None and chunk_utils.has_expression(self._expr_value):
            return self._resolve_expression_tuple(self._expr_value)
        return tuple(x.value for x in self.get_my_params() if isinstance(x, ChunkParamBase))

    @value.setter
    def value(self, val: Sequence[Any] | str):
        """Set every component from a sequence, or from a whole-vector expression."""
        if isinstance(val, str) and chunk_utils.has_expression(val):
            self._expr_value = val
            self._expression_connections = []
            for node_name, param_name in chunk_utils.parse_expression(val):
                ref = self._resolve_expression_ref(node_name, param_name)
                if ref is None:
                    logger.warning("Expression ref @%s.%s@ not found", node_name, param_name)
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
        """Push the resolved tuple down into the child params.

        Their own widgets then display real numbers rather than the parent's expression string.
        """
        resolved = self.value
        if resolved is None or len(resolved) != self.length:
            return
        self._set_children_blocked(resolved)

    def _set_children_blocked(self, val: Sequence[Any]) -> None:
        """Set every child from val, holding this vector's storage-attr write until all are set.

        data_changed checks _suppress_storage_write. Without it, the first (still
        mid-assignment) child change would be the one that lazily authors the chunk-prim storage
        attr, permanently baking in a stale value with only some components set.

        Deliberately does NOT use set_blocked(), which is transitive to children via their
        owner-chain is_blocked() check -- that would also suppress each child's own notify,
        which their widgets rely on to refresh live as they're set.

        Announces nothing while a load is restoring values, the same rule value_changed
        follows. This call used to solve right there, cooking the chunk before the rest of
        its inputs were back: a camera restored its clipping range, cooked with the default
        target, and wrote its lens onto /cameras/camera1. The load cooks once, after
        everything is restored.
        """
        self._suppress_storage_write = True
        try:
            for i in range(self.length):
                self[i] = val[i]
        finally:
            self._suppress_storage_write = False
        if not chunk_notice._loading_stage:
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
        """The components' default values as a tuple."""
        return tuple(x.default_value for x in self.get_my_params() if isinstance(x, ChunkParamBase))

    @default_value.setter
    def default_value(self, val: Sequence[Any]):
        """Set every component's default from a sequence of the vector's length."""
        if val is None:
            return

        if not len(val) == self.length:
            raise ValueError(
                f"Trying to set value {val} on vector param {self.name} with wring size. Size should be {self.length}"
            )

        for i in range(self.length):
            self[i].default_value = val[i]

    def get_type(self):
        """VECTOR combined with the component param's type."""
        return ChunkFlags.ParamType.VECTOR | self.param.get_type()

    def evaluate_type(self):
        """VECTOR combined with the component param's current type."""
        return ChunkFlags.ParamType.VECTOR | self.param.evaluate_type()

    def has_flag(self, flag: IntFlag) -> bool:
        """Check a flag, reading OUTPUT from is_output rather than from .flags.

        A vector's flags are GroupFlags, and ParamFlags.OUTPUT shares a bit with one of them;
        see the is_output field. Everything else defers to the normal check.

        Args:
            flag: The flag to check.

        Returns:
            True if the flag is set.
        """
        if flag == ChunkFlags.ParamFlags.OUTPUT:
            return self.is_output
        return super().has_flag(flag)




class ChunkConfig(AttributesBase):
    """Base class for Configs. A config sets some load time configurations for a chunk."""

    def get_variants(self) -> list:
        """The menu entries, building them first if they were declared lazily.

        Memoised onto the field, so the callable runs once per process however many menus
        are drawn. Callers should use this rather than reading .variants, which may still
        be the callable.

        Returns:
            The variants, empty if building them failed.
        """
        variants = self.variants
        if callable(variants):
            try:
                variants = list(variants())
            except Exception as e:  # noqa: BLE001 - a menu must still open
                # Variants are an optional extra on one entry, and this runs while a menu is
                # being drawn for EVERY chunk -- one module that cannot build its list must
                # not take the whole chunk menu with it. Sdr.Registry() can raise, for
                # example, when an installed plugin advertises an invalid version.
                logger.warning("Could not build menu variants for %r: %s", self.module, e)
                return []
            object.__setattr__(self, "variants", variants)
        return variants

    category: str = attrs.field(default="Chunks")
    description: str = attrs.field(default="Chunk description...")
    load_for_plugin: str | None = attrs.field(default=None)
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
    # POSITIONAL and each gets its own port -- Stage Expression Switch's slot 0 is the
    # normal path and slot 1 the matched one, so which wire landed where is the whole
    # meaning. A dynamic-slot chunk (dynamic_exec_inputs) is neither: its slot
    # count grows with what is wired into it, which is that field's business, not this one's.
    exec_inputs: int = attrs.field(default=1)
    # Grows a fresh numbered exec input slot (inputs:chunk, inputs:chunk1, ...) per branch
    # wired in, plus one empty trailing slot to attach the next branch to -- Merge and
    # Switch both need every upstream branch as its own port rather than several wires
    # stacked on one. Distinct from exec_inputs, which is a fixed, positional count set at
    # config time; here the count is not known until the graph is wired.
    dynamic_exec_inputs: bool = attrs.field(default=False)
    outputs: list[tuple[str, str]] = attrs.field(factory=list)  # extra typed output ports: [(name, SdfTypeName), ...]
    # Extra menu entries: [(label, {param: value}), ...], or a callable returning them.
    #
    # A callable because building this list can be expensive and is only ever needed to
    # draw a menu. mtlx_shader enumerates 784 MaterialX definitions from the Sdr registry
    # to collapse them into 217 entries, which measured at 1.88s -- 76% of the whole
    # registry discovery -- paid in every process, including headless sweeps that never
    # draw a menu at all. Pass a callable and that cost moves to the first menu build.
    variants: list[tuple[str, dict]] | Callable[[], list[tuple[str, dict]]] = attrs.field(
        factory=list
    )
    gizmos: list[GizmoBase] = attrs.field(factory=list)  # viewport handles: authored as ui:gizmo:* on the chunk prim

    @override
    def get_notice_alias(self) -> chunk_notice.ChunkNoticeAlias:
        return chunk_notice.ChunkNoticeAlias.CHUNK_CONFIG_CHANGED





def is_inst(cls: type[ValueT]) -> Predicate[ValueT]:
    """Create a predicate that checks, and narrows the type of, an instance of `cls`.

    Args:
        cls (type[ValueT]): The type to check

    Returns:
        Predicate[ValueT]: The predicate function
    """

    def _fn(v: Any) -> TypeGuard[ValueT]:
        return isinstance(v, cls)

    return _fn
