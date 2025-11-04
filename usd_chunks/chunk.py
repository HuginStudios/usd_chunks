from attr import Attribute
from attrs import field

from enum import IntFlag, auto
from typing import Any, List, Optional, Sequence

from . import chunk_notice


class ChunkFlags:
    """Hold common flags for chunks"""

    class LayoutFlags(IntFlag):
        """Flags for layout hinting parameters and groups"""

        HORIZONTAL = 1
        VERTICAL = auto()

    class State(IntFlag):
        """Store current state of parameters and containers"""

        NONE = 0  # Default
        HIDDEN = auto()  # The param/group is currently hidden
        DISABLED = auto()  # The param/group is currently disabled
        COLLAPSED = auto()  # The group is collapsed. Only works on groups with COLLAPSABE flag set

    class GroupFlags(IntFlag):
        """Flags for chunk constainer type objects (Chunk, Group)"""

        LABELED = 1  # Add a label on the top of the group based on group.label if available, otherwise group.name
        COLLAPSABLE = auto()  # The group should be collapsable

        FRAMED = auto()  # The group should be framed
        # FRAME_RAISED = auto()  # The group frame should appear raised

    class ParamFlags(IntFlag):
        """Flags for chunk param types"""

        LABELED = 1  # Add a label on thnext to the param. Use param.label if available, otherwise param.name

        # These are only valid for FILE type param
        FILE_EXISTING = auto()  # File param must point to an existing path

    class ParamType(IntFlag):
        """Flags for parameters, describing their types. UI factories should use the param.get_type() to get these types, and
        implement the UI based on these
        """

        # Use 8 bit padding for types. This leaves room for 8 widget variants per type
        NONE = 1 << 3
        EMPTY = auto()
        FLOAT = auto()
        STRING = auto()
        INT = auto()
        BOOL = auto()
        CUSTOM = auto()  # use self.custom_widget as widget

        # Flags for standard widgets
        # A separator line. Cant be combined and will not have a value
        SEPARATOR = EMPTY | (1 << 0)
        BUTTON = EMPTY | (1 << 1)

        # Real
        FLOAT_SLIDER = FLOAT | (1 << 0)
        FLOAT_SPINBOX = FLOAT | (1 << 1)

        # Int
        INT_SLIDER = INT | (1 << 0)
        INT_SPINBOX = INT | (1 << 1)

        # String
        LABEL = STRING | (1 << 0)
        LINEEDIT = STRING | (1 << 1)
        FILE = STRING | (1 << 2)
        STRING_MENU = STRING | (1 << 3)

        # Bool
        CHECKBOX = BOOL | (1 << 0)

    @staticmethod
    def ParamTypeToTypes(t: ParamType) -> List[type]:
        """Provides default values for ParamType enums. If t is a combination of multiple ParamTypes, returns all supported values

        Args:
            t (ParamType): Input ParamType which to request value for

        Returns:
            Any: A default value for t
        """
        result = []

        if t in (ChunkFlags.ParamType.EMPTY | ChunkFlags.ParamType.NONE):
            result.append(None)
        if ChunkFlags.ParamType.INT in t:
            result.append(int)
        if ChunkFlags.ParamType.FLOAT in t:
            result.append(float)
        if ChunkFlags.ParamType.BOOL in t:
            result.append(bool)
        if ChunkFlags.ParamType.STRING in t:
            result.append(str)
        return result


from .chunk_interface import (  # noqa: E402
    ChunkBase,
    ChunkParamBase,
    ChunkGroupBase,
    ChunkVectorBase,
)

##############################################
# CONCRETE IMPLEMENTATIONS FOR CHUNK OBJECTS #
##############################################


class Chunk(ChunkBase):
    """Chunk is the top level container, holding params and groups which in turn make up the chunk"""

    # Override defaults
    flags: ChunkFlags.GroupFlags = field(
        default=ChunkFlags.GroupFlags.COLLAPSABLE | ChunkFlags.GroupFlags.LABELED | ChunkFlags.GroupFlags.FRAMED
    )

    def get_notice_alias(self):
        return chunk_notice.ChunkNoticeAlias.CHUNK_CHANGED


class Group(ChunkGroupBase):
    """Group is a container holding child ChunkParamBase and ChunkGroupBase instances"""

    # Override defaults
    flags: ChunkFlags.GroupFlags = field(default=ChunkFlags.GroupFlags.LABELED | ChunkFlags.GroupFlags.FRAMED)

    def get_notice_alias(self):
        return chunk_notice.ChunkNoticeAlias.GROUP_CHANGED


class Vector(ChunkVectorBase):
    """A Vector is basically a group which holds a set number of parameters"""

    # Override defaults
    flags: ChunkFlags.GroupFlags = field(default=ChunkFlags.GroupFlags.LABELED | ChunkFlags.GroupFlags.FRAMED)

    def get_notice_alias(self) -> chunk_notice.ChunkNoticeAlias:
        return chunk_notice.ChunkNoticeAlias.PARAM_CHANGED


class Slider(ChunkParamBase[int | float]):
    min_value: Optional[float | int] = field(default=None)
    max_value: Optional[float | int] = field(default=None)
    is_int: bool = field(default=False)

    @classmethod
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.INT_SLIDER | ChunkFlags.ParamType.FLOAT_SLIDER

    def evaluate_type(self) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.INT_SLIDER if self.is_int else ChunkFlags.ParamType.FLOAT_SLIDER


class Spinbox(ChunkParamBase[float | int]):
    min_value: Optional[float | int] = field(default=None)
    max_value: Optional[float | int] = field(default=None)
    is_int: bool = field(default=False)

    @classmethod
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.INT_SPINBOX | ChunkFlags.ParamType.FLOAT_SPINBOX

    def evaluate_type(self) -> ChunkFlags.ParamType:
        if self.is_int:
            return ChunkFlags.ParamType.INT_SPINBOX
        return ChunkFlags.ParamType.FLOAT_SPINBOX


class Menu(ChunkParamBase[str]):
    menu_items: Sequence[str] = field(factory=list)

    @classmethod
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.STRING_MENU

    def convert_attr(self, attr: Attribute, val: Any) -> Any:
        if attr.name == "value" and val not in self.menu_items:
            try:
                return self.value
            except AttributeError:
                return self.default_value

        elif attr.name == "default_value":
            if self.menu_items and val not in self.menu_items:
                return self.menu_items[0]

        return super().convert_attr(attr, val)


class Button(ChunkParamBase):
    @classmethod
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.BUTTON


class Checkbox(ChunkParamBase[bool]):
    # Type specific flags, can be used for setting custom flags.
    flags: IntFlag = field(default=ChunkFlags.ParamFlags.LABELED)

    @classmethod
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.CHECKBOX


class LineEdit(ChunkParamBase[str]):
    @classmethod
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.LINEEDIT


class FileBrowser(ChunkParamBase[str]):
    extensions: List[str] = field(factory=list)
    pattern: str = field(default="*")

    @classmethod
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.FILE

    def validate_attr(self, attr: Attribute, val: Any) -> bool:
        if attr.name in ["value", "default_value"] and val:
            if self.has_flag(ChunkFlags.ParamFlags.FILE_EXISTING):
                self._validate_existing_path(attr, val)
            else:
                self._validate_path(attr, val)

        return super().validate_attr(attr, val)


class Label(ChunkParamBase[str]):
    @classmethod
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.LABEL


class Separator(ChunkParamBase):
    @classmethod
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.SEPARATOR


class Custom(ChunkParamBase):
    @classmethod
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.CUSTOM
