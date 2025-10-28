from attr import Attribute
from attrs import field

from enum import IntFlag, auto
from typing import Any, List, Optional, Sequence

from . import chunk_notice

# if TYPE_CHECKING:
#     from pxr import Tf, Usd, Usdviewq


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

        FRAME_SUNKEN = auto()  # The group frame should appear sunken
        FRAME_RAISED = auto()  # The group frame should appear raised

    class ParamFlags(IntFlag):
        """Flags for chunk param types"""

        LABELED = 1  # Add a label on thnext to the param. Use param.label if available, otherwise param.name

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
    ChunkAttributes,
    ChunkParamBase,
    ChunkGroupBase,
    ChunkVectorBase,
)

##############################################
# CONCRETE IMPLEMENTATIONS FOR CHUNK OBJECTS #
##############################################


class Chunk(ChunkAttributes, ChunkGroupBase):
    """Chunk is the top level container, holding params and groups which in turn make up the chunk"""

    flags: ChunkFlags.GroupFlags = field(
        default=ChunkFlags.GroupFlags.COLLAPSABLE | ChunkFlags.GroupFlags.LABELED | ChunkFlags.GroupFlags.FRAME_RAISED
    )

    def get_notice_alias(self):
        return chunk_notice.ChunkNoticeAlias.CHUNK_CHANGED
    
    def reset(self):
        self.unset_state(ChunkFlags.State.DISABLED | ChunkFlags.State.HIDDEN)


class Group(ChunkGroupBase):
    """Group is a container holding child ChunkParamBase and ChunkGroupBase instances"""

    flags: ChunkFlags.GroupFlags = field(default=ChunkFlags.GroupFlags.LABELED | ChunkFlags.GroupFlags.FRAME_SUNKEN)

    def get_notice_alias(self):
        return chunk_notice.ChunkNoticeAlias.GROUP_CHANGED

class Vector(ChunkVectorBase):
    """A Vector is basically a group which holds a set number of parameters. Need to supply a parameter instance to the init:
        Vector(param=Slider(), name="my_vector", length=3)
    Vector also support suffixes:
        Vector(param=Slider(), name="my_vector", length=3, suffixes="RGB")
    Args:
        ChunkVectorBase (_type_): _description_
    """

    flags: ChunkFlags.GroupFlags = field(default=ChunkFlags.GroupFlags.LABELED | ChunkFlags.GroupFlags.FRAME_SUNKEN)


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
    default_value: str = field(default="", init=False)
    value: str = field(default="")

    def get_type(self) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.STRING_MENU

    def _convert(self, attr: Attribute, val: Any) -> Any:
        if attr.name == "value" and val not in self.menu_items:
            try:
                return self.value
            except AttributeError:
                return self.default_value
        if attr.name == "default_value" and val not in self.menu_items:
            return next(iter(self.menu_items), None)
        return super()._convert(attr, val)


class Button(ChunkParamBase):
    def get_type(self) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.BUTTON


class Checkbox(ChunkParamBase[bool]):
    def get_type(self) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.CHECKBOX


class LineEdit(ChunkParamBase[str]):
    def get_type(self) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.LINEEDIT


class FileBrowser(ChunkParamBase[str]):
    def get_type(self) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.FILE


class Label(ChunkParamBase[str]):
    def get_type(self) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.LABEL


class Separator(ChunkParamBase):
    def get_type(self) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.SEPARATOR


# class CustomInterface(ChunkParam):
#     def __init__(self, *args, **kwargs):
#         super().__init__()

#         return ChunkFlags.ParamType.CUSTOM
#         self.args = args
#         self.kwargs = kwargs
