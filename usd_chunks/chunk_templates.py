from typing import List, Any
from dataclasses import dataclass, field

from enum import IntFlag, auto


class ChunkParamType(IntFlag):
    # Flags for ChunkParam for describing the type of parameter

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
    INT_MENU = INT | (1 << 2)

    # String
    LABEL = STRING | (1 << 0)
    LINEEDIT = STRING | (1 << 1)
    FILE = STRING | (1 << 2)
    STRING_MENU = STRING | (1 << 3)

    # Bool
    CHECKBOX = BOOL | (1 << 0)


@dataclass
class ChunkParamTemplate:
    """A base dataclass holding type specific information for a parameter"""

    param_type: ChunkParamType = field(init=False, default=ChunkParamType.NONE)
    default_value: Any = field(default=None)

    def supports_value(self, val: Any) -> bool:
        """Check if the type of val is supported by this template. In this base class, return false
        if the default_value and val is not the same type. Also check for EMPTY param types, as templates
        without a usable value should return True for any type.

        Override in subclasses if the subclass supports multiple types

        Args:
            val (Any): A value to check

        Returns:
            bool: If this value is supported by the template
        """
        return ChunkParamType.EMPTY in self.param_type or type(self.default_value) is type(val)


@dataclass
class SliderTemplate(ChunkParamTemplate):
    default_value: float | int = field(default=0)
    min_value: float | int = field(default=None)
    max_value: float | int = field(default=None)
    is_int: bool = field(default=False)

    def __post_init__(self):
        self.param_type = ChunkParamType.INT_SLIDER if self.is_int else ChunkParamType.FLOAT_SLIDER

    def supports_value(self, val):
        if isinstance(val, (int, float)):
            return True
        return super().supports_value(val)


@dataclass
class SpinboxTemplate(ChunkParamTemplate):
    default_value: float | int = field(default=0)
    min_value: float | int = field(default=None)
    max_value: float | int = field(default=None)
    is_int: bool = field(default=False)

    def __post_init__(self):
        self.param_type = ChunkParamType.INT_SPINBOX if self.is_int else ChunkParamType.FLOAT_SPINBOX

    def supports_value(self, val):
        if isinstance(val, (int, float)):
            return True
        return super().supports_value(val)


@dataclass
class MenuTemplate(ChunkParamTemplate):
    menu_items: List[str | int] = field(default_factory=[])
    default_value: str | int = field(default=None)
    is_int: bool = field(default=False)

    def __post_init__(self):
        self.param_type = ChunkParamType.INT_MENU if self.is_int else ChunkParamType.STRING_MENU
        if self.menu_items and not self.default_value:
            self.default_value = self.menu_items[0]

    def supports_value(self, val):
        if isinstance(val, (int, str)):
            return val in self.menu_items
        return super().supports_value(val)


@dataclass
class ButtonTemplate(ChunkParamTemplate):
    default_value: Any = field(init=False, default=None)

    def __post_init__(self):
        self.param_type = ChunkParamType.BUTTON


@dataclass
class CheckboxTemplate(ChunkParamTemplate):
    default_value: bool = field(default=False)

    def __post_init__(self):
        self.param_type = ChunkParamType.CHECKBOX


@dataclass
class LineEditTemplate(ChunkParamTemplate):
    default_value: str = field(default="")

    def __post_init__(self):
        self.param_type = ChunkParamType.LINEEDIT


@dataclass
class FileBrowserTemplate(ChunkParamTemplate):
    default_value: str = field(default="")

    def __post_init__(self):
        self.param_type = ChunkParamType.FILE


@dataclass
class LabelTemplate(ChunkParamTemplate):
    default_value: str = field(default="")

    def __post_init__(self):
        self.param_type = ChunkParamType.LABEL


@dataclass
class SeparatorTemplate(ChunkParamTemplate):
    default_value: Any = field(init=False, default=None)

    def __post_init__(self):
        self.param_type = ChunkParamType.SEPARATOR
