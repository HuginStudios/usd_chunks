from enum import IntFlag, IntEnum, auto
from typing import List
from pxr import Sdf


class _FlagRepr(IntFlag):
    def __repr__(self) -> str:
        return f"{self.__module__}.{self.__class__.__qualname__}({self.value})"


class ChunkFlags:
    """Hold common flags for chunks"""

    class LayoutFlags(IntEnum):
        """Flags for layout hinting parameters and groups

        Attributes:
            HORIZONTAL: Hint the ui to display this parameter or group horizontally
            VERTICAL: Hint the ui to display this parameter or group vertically
        """

        HORIZONTAL = 0
        VERTICAL = auto()

    class State(_FlagRepr):
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

    class GroupFlags(_FlagRepr):
        """Flags for chunk constainer type objects (Chunk, Group)

        Attributes:
            LABELED: If the group should have a label in the ui
            COLLAPSABLE: If the group should be able to collapse
            FRAMED: A simple style flag
            LIST: Parameters on this group is treated as a list that can be extended and shrinked
        """

        NONE = 0
        LABELED = auto()
        COLLAPSABLE = auto()
        FRAMED = auto()
        LIST = auto()

    class ParamFlags(_FlagRepr):
        """Flags for chunk params

        Attributes:
            LABELED: If the param should have a label in the ui
            FILE_EXISTING: For FILE type params, this rule hints to the file browser that we can not create new files
            DIRECTORY: For FILE type params. Allows selection of folders
            PRIM_SELECTION: For LINEEDIT type params, this says that the param is meant for UsdPrim selections
            PROP_SELECTION: For LINEEDIT type params, this says that the param is meant for UsdProperty selections
        """
        NONE = 0
        LABELED = auto()

        # FILE
        FILE_EXISTING = auto()
        DIRECTORY = auto()

        # LINEEDIT
        PRIM_SELECTION = auto()
        PROP_SELECTION = auto()

        # TEXTEDIT
        PY_SYNTAX_HIGHLIGHT = auto()
        USD_SYNTAX_HIGHLIGHT = auto()

    class ParamType(_FlagRepr):
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
        TEXTEDIT = STRING | (1 << 2)
        FILE = STRING | (1 << 3)
        STRING_MENU = STRING | (1 << 4)

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
