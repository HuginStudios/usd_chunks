"""
This module is the client-facing interface for working with chunks,
containing concrete implementations of the chunk objects. These
classes can be used by themselves, but they also describe common
UI related information, making them easy to implement in any UI
framework.

As all chunk classes inherit from chunk_core.AttributesMetaclass,
all these classes are in fact attrs classes (dataclass). So creating a new
Chunk Parameter class, one must only declare the class and inherit
from ChunkParamBase or related base class. As the class will, behind
the scenes, be wrapped in attrs.define() we get some method definitions
for free; most notably \\_\\_init\\_\\_(). The \\_\\_init\\_\\_ will
be created from all the fields of the class, including inherited, so
we don't have to define the \\_\\_init\\_\\_ function for each chunk class.

For this reason, any attributes of a chunk class should be defined
using attrs.field().

A class which inherits from ChunkParamBase need to implement get_type(),
and possibly evaluate_type() if the type may change during runtime. This
is to ensure an easy mapping between chunks and whatever UI framework is used

!!! example

    ``` py
    class CustomStrParam(ChunkParamBase[str]):
        some_attrib: str = attrs.field(default="hello")

        @classmethod
        def get_type(cls) -> ChunkFlags.ParamType:
            return ChunkFlags.ParamType.LINEEDIT
    ```

When creating a chunk module, the chunk.Chunk() class is the top level
container for which all parameters and groups should be added to. Creating
a simple chunk with a button is as simple as:

``` py
from usd_chunks import chunk


def main():
    myChunk = chunk.Chunk("myChunk", category="example")
    btnParam = chunk.Button("myButton")

    myChunk.add_param(btnParam)
    return myChunk
```

See [example](../example.md) for a more extensive example.

"""

from attr import Attribute
from attrs import field, define

from enum import IntFlag, auto
from typing import Any, List, Dict, Optional, Sequence, Type, Tuple
from usd_chunks import chunk_notice

from pxr import Usd, Sdf


class ChunkFlags:
    """Hold common flags for chunks"""

    class LayoutFlags(IntFlag):
        """Flags for layout hinting parameters and groups

        Attributes:
            HORIZONTAL: Hint the ui to display this parameter or group horizontally
            VERTICAL: Hint the ui to display this parameter or group vertically
        """

        HORIZONTAL = 0
        VERTICAL = auto()

    class State(IntFlag):
        """Store current state of parameters and containers

        Attributes:
            NONE: No specified state
            HIDDEN: Hint the ui to not draw this param or group
            DISABLED: Hint the ui to disable this param or group
            COLLAPSED: For COLLAPSABLE groups, this should collapse the group if set
        """

        NONE = 0
        HIDDEN = auto()
        DISABLED = auto()
        COLLAPSED = auto()

    class GroupFlags(IntFlag):
        """Flags for chunk constainer type objects (Chunk, Group)

        Attributes:
            LABELED: If the group should have a label in the ui
            COLLAPSABLE: If the group should be able to collapse
            FRAMED: A simple style flag
        """

        LABELED = 1
        COLLAPSABLE = auto()
        FRAMED = auto()

    class ParamFlags(IntFlag):
        """Flags for chunk params

        Attributes:
            LABELED: If the param should have a label in the ui
            FILE_EXISTING: For FILE type params, this rule hints to the file browser that we can not create new files
        """

        LABELED = 1
        FILE_EXISTING = auto()

    class ParamType(IntFlag):
        """Flags for parameters, describing their types. UI factories should use the param.get_type() to get these types, and
        implement the UI based on these.

        The concrete types are combinations of any of NONE, EMPTY, FLOAT, STRING, INT, BOOL.
        As these are flags, they can be combined using bitwise operators to create more complex
        parameters. It's up the the ui impelementation to handle these.

        Attributes:
            NONE : A default param type. Nothing will be drawn in the ui
            EMPTY : The param does not manage a value. used for separators and similar
            FLOAT : The parameter can hold a float value
            STRING : The parameter can hold a string value
            INT : The parameter can hold a int value
            BOOL : The parameter can hold a bool value
            CUSTOM : The parameter is custom, will use param.custom_widget when creating the widget
            SEPARATOR : The parameter is a separator
            BUTTON : The parameter is a button
            FLOAT_SLIDER : The parameter is a float valued slider
            FLOAT_SPINBOX : The parameter is a float valued spinbox
            INT_SLIDER : The parameter is an int valued slider
            INT_SPINBOX : The parameter is an int valued spinbox
            LABEL : The parameter is a string valued label
            LINEEDIT : The parameter is an editable stiring
            FILE : The parameter is a string value referring to a file
            STRING_MENU : The parameter is a string valued menu with multiple choices
            CHECKBOX : The parameter is a bool valued checkbox
        """

        # Use 8 bit padding for types. This leaves room for 8 widget variants per type
        NONE = 1 << 3
        EMPTY = auto()
        FLOAT = auto()
        STRING = auto()
        INT = auto()
        BOOL = auto()
        VECTOR = auto()
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
            result.append(Sdf.AssetPath)
        return result


from .chunk_core import (  # noqa: E402
    ChunkBase,
    ChunkConfig,
    ChunkParamBase,
    ChunkGroupBase,
    ChunkVectorBase,
    ChunkUsdAttributeSupport,
    ChunkCommon,
)

###########
# CONFIGS #
###########


class DefaulConfig(ChunkConfig):
    """
    This class is used as a default config. 
    
    To use, in the chunk module, add an additional function besides main():

    ``` py
    def config():
        return chunk.DefaultConfig(
            load=True, load_for_plugin="chunk_usdview"
        )
    ```
    """
    pass


##############################################
# CONCRETE IMPLEMENTATIONS FOR CHUNK OBJECTS #
##############################################


class Chunk(ChunkBase):
    """Chunk is the top level container, holding params and groups which in turn make up the final, displayed chunk."""

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
        return chunk_notice.ChunkNoticeAlias.GROUP_CHANGED


class Slider(ChunkParamBase[int | float]):
    """
    A slider parameter holding a float or int value

    Attributes:
        min_value: A minimum value which the value should not subseed
        max_value: A maximum value which the value should not exceed
        is_int: Set whether this parameter sould be an integer
    """

    min_value: Optional[float | int] = field(default=None)
    max_value: Optional[float | int] = field(default=None)
    is_int: bool = field(default=False)

    @classmethod
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.INT_SLIDER | ChunkFlags.ParamType.FLOAT_SLIDER

    def evaluate_type(self) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.INT_SLIDER if self.is_int else ChunkFlags.ParamType.FLOAT_SLIDER

    @classmethod
    def from_usd_attr(cls, attr: Usd.Attribute, *args, **kwargs) -> ChunkUsdAttributeSupport:
        param = super().from_usd_attr(attr, *args, **kwargs)

        if limits := attr.GetLimits():  # type: ignore
            if hard := limits.get("hard"):
                limit_min = hard.get("minimum", None)
                limit_max = hard.get("maximum", None)

                if hasattr(param, "min_value"):
                    param.min_value = limit_min  # type: ignore
                if hasattr(param, "max_value"):
                    param.max_value = limit_max  # type: ignore
        return param


class Spinbox(ChunkParamBase[float | int]):
    """
    A spinbox parameter holding a float or int value

    Attributes:
        min_value: A minimum value which the value should not subseed
        max_value: A maximum value which the value should not exceed
        is_int: Set whether this parameter sould be an integer
    """

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

    @classmethod
    def from_usd_attr(cls, attr: Usd.Attribute, *args, **kwargs) -> ChunkUsdAttributeSupport:
        param = super().from_usd_attr(attr, *args, **kwargs)

        if limits := attr.GetLimits():  # type: ignore
            if hard := limits.get("hard"):
                limit_min = hard.get("minimum", None)
                limit_max = hard.get("maximum", None)

                if hasattr(param, "min_value"):
                    param.min_value = limit_min  # type: ignore
                if hasattr(param, "max_value"):
                    param.max_value = limit_max  # type: ignore
        return param


class Menu(ChunkParamBase[str]):
    """
    A menu parameter with menu_items

    Attributes:
        menu_items: A list of items to display in the menu
    """

    menu_items: Sequence[str] = field(factory=list)

    @classmethod
    def from_usd_attr(cls, attr: Usd.Attribute, *args, **kwargs):
        param = super().from_usd_attr(attr, *args, **kwargs)

        menu_items = set((param.default_value,))  # type: ignore
        if tokens := attr.GetMetadata("allowedTokens"):
            menu_items.update(tokens)

        param.menu_items = [str(x) for x in menu_items]  # type: ignore
        return param

    @classmethod
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.STRING_MENU

    def convert_attr(self, attr: Attribute, val: Any) -> Any:
        if attr.name == "default_value":
            if self.menu_items and val not in self.menu_items:
                return self.menu_items[0]

        return super().convert_attr(attr, val)


class Button(ChunkParamBase):
    """
    A button parameter. Does not store any usable value. Should be used with callbacks only
    """

    @classmethod
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.BUTTON


class Checkbox(ChunkParamBase[bool]):
    """
    A checkbox parameter. Stores a boolean value
    """

    flags: IntFlag = field(default=ChunkFlags.ParamFlags.LABELED)

    @classmethod
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.CHECKBOX


class LineEdit(ChunkParamBase[str]):
    """
    An editable string parameter
    """

    @classmethod
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.LINEEDIT


class FileBrowser(ChunkParamBase[str | Sdf.AssetPath]):
    """
    A string parameter with the addition of a button for opening a file browser

    Attributes:
        extensions: A list of supported file extensions
        pattern: A regex pattern for filtering file names. Defaults to "\\*"
    """

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
    """
    A label parameter. Should not be editable
    """

    @classmethod
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.LABEL


class Separator(ChunkParamBase):
    """
    An organizatorial parameter. Does not store a value
    """

    @classmethod
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.SEPARATOR


class Custom(ChunkParamBase):
    """
    A customizable parameter for holding custom widgets
    """

    @classmethod
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.CUSTOM


_type_map = {
    "float": Slider,
    "int": (Spinbox, {"is_int": True}),
    "token": Menu,
    "bool": Checkbox,
    "string": LineEdit,
    "asset": FileBrowser,
    "path": FileBrowser,
    "float2": (Vector, {"param": Slider("0"), "length": 2}),
    "float3": (Vector, {"param": Slider("0"), "length": 3}),
    "float4": (Vector, {"param": Slider("0"), "length": 4}),
    "vector3f": (Vector, {"param": Slider("0"), "length": 3}),
    "color3f": (Vector, {"param": Slider("0"), "length": 3}),
    "int2": (Vector, {"param": Slider("0"), "length": 2}),
    "int3": (Vector, {"param": Slider("0"), "length": 3}),
    "int4": (Vector, {"param": Slider("0"), "length": 4}),
    "double2": (Vector, {"param": Slider("0"), "length": 2}),
    "double3": (Vector, {"param": Slider("0"), "length": 3}),
    "double4": (Vector, {"param": Slider("0"), "length": 4}),
}


def param_from_attr(attr: Usd.Attribute, *args, **kwargs) -> ChunkCommon | None:
    """
    Create a new paramaeter from a Usd.Attribute if the attribute type is supported. Will use
    the from_usd_attr class method of the found param type. It will connect the ATTRIBUTE_CHANGED
    notice to control the usd attribute. We return a ChunkCommon object rather than a ChunkParamBase as
    as some parameters may actually be groups (Vectors f.e.)

    Args:
        attr (Usd.Attribute): The Usd.Attribute which to create a parameter for

    Returns:
        ChunkCommon | None: If a mapping exists for the attr type, then returns a ChunkCommon object. Else None.
    """
    type_name = attr.GetTypeName()

    t = _type_map.get(str(type_name))
    if not t:
        return None

    if isinstance(t, tuple):
        kwargs.update(t[1])
        return t[0].from_usd_attr(attr, *args, **kwargs)
    else:
        return t.from_usd_attr(attr, *args, **kwargs)
