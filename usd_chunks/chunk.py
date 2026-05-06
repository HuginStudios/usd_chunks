"""
This module is the client-facing interface for working with chunks,
containing concrete implementations of the chunk objects. These
classes can be used by themselves, but they also describe common
UI related information, making them easy to implement in any UI
framework.

As all chunk classes inherit from chunk_core.AttributesMetaclass,
all these classes are in fact attrs classes (dataclass). So creating a new
Chunk Parameter class, one must only declare the class and inherit
from [ChunkParamBase][usd_chunks.chunk_core.ChunkParamBase] or related base class. As the class will, behind
the scenes, be wrapped in attrs.define() we get some method definitions
for free; most notably \\_\\_init\\_\\_(). The \\_\\_init\\_\\_ will
be created from all the fields of the class, including inherited, so
we don't have to define the \\_\\_init\\_\\_ function for each chunk class.

For this reason, any attributes of a chunk class should be defined
using attrs.field().

A class which inherits from [ChunkParamBase][usd_chunks.chunk_core.ChunkParamBase] need to implement [get_type][usd_chunks.chunk_core.ChunkParamBase.get_type],
and possibly [evaluate_type][usd_chunks.chunk_core.ChunkParamBase.evaluate_type] if the type may change during runtime. This
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

!!! example
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

from attr import Attribute as Attribute
from attrs import field

from enum import IntFlag, Enum
from typing import Any, List, Optional, Sequence, TYPE_CHECKING, cast, TypeGuard, overload

from pxr import Usd, Sdf

# Keep the ChunkNoticeAlias import as clients may use usd_chunks.chunk.ChunkNoticeAlias, without having to import chunk_notice
from usd_chunks.chunk_notice import ChunkNoticeAlias  # noqa: F401
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_core import (
    ChunkBase,
    ChunkConfig as _cfg,
    ChunkParamBase,
    ChunkGroupBase,
    ChunkVectorBase,
    ChunkUsdAttributeSupport,
    ChunkCommon,
    ChunkListGroupBase,
    Predicate,
    ValueT,
    is_inst,
)

from usd_chunks.chunk_registry import ChunkRegistry, ChunkLayerManager

if TYPE_CHECKING:
    from attr import Attribute

###########
# CONFIGS #
###########


class DefaulConfig(_cfg):
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


class StandardCategory(str, Enum):
    PROPERTIES = "Properties"
    MODELLING = "Modelling"
    UTILS = "Utility"
    RIGS = "Rigs"
    USER = "User"
    EXAMPLES = "Examples"


##############################################
# CONCRETE IMPLEMENTATIONS FOR CHUNK OBJECTS #
##############################################


class Chunk(ChunkBase):
    """Chunk is the top level container, holding params and groups which in turn make up the final, displayed chunk."""

    # Override defaults
    flags: IntFlag = field(
        default=ChunkFlags.GroupFlags.COLLAPSABLE | ChunkFlags.GroupFlags.LABELED | ChunkFlags.GroupFlags.FRAMED
    )


class Group(ChunkGroupBase):
    """Group is a container holding child ChunkParamBase and ChunkGroupBase instances"""

    # Override defaults
    flags: IntFlag = field(default=ChunkFlags.GroupFlags.LABELED | ChunkFlags.GroupFlags.FRAMED)


class ListGroup(ChunkListGroupBase):
    """ListGroup is a container holding child ChunkParamBase and ChunkGroupBase instances with dynamic size. Params added with add_param will be duplicated size times"""

    # Override defaults
    flags: IntFlag = field(
        default=ChunkFlags.GroupFlags.LABELED | ChunkFlags.GroupFlags.FRAMED | ChunkFlags.GroupFlags.LIST
    )


class Vector(ChunkVectorBase):
    """A Vector is basically a group which holds a set number of parameters"""

    # Override defaults
    flags: IntFlag = field(default=ChunkFlags.GroupFlags.LABELED | ChunkFlags.GroupFlags.FRAMED)


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
    flags: IntFlag = field(default=ChunkFlags.ParamFlags.LABELED)

    @classmethod
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.INT_SLIDER | ChunkFlags.ParamType.FLOAT_SLIDER

    def evaluate_type(self) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.INT_SLIDER if self.is_int else ChunkFlags.ParamType.FLOAT_SLIDER

    @classmethod
    def from_usd_attr(cls, attr: Usd.Attribute, *args, **kwargs) -> ChunkUsdAttributeSupport:
        param = super().from_usd_attr(attr, *args, **kwargs)

        try:
            limits = attr.GetLimits()  # type: ignore
        except Exception:
            limits = None
        if limits:
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
    flags: IntFlag = field(default=ChunkFlags.ParamFlags.LABELED)

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

    flags: IntFlag = field(default=ChunkFlags.ParamFlags.LABELED)

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

    def convert_attr(self, attr: "Attribute", val: Any) -> Any:
        if attr.name in ["default_value", "value"]:
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

    flags: IntFlag = field(default=ChunkFlags.ParamFlags.LABELED)

    def __attrs_post_init__(self):
        super().__attrs_post_init__()

        if self.has_flag(ChunkFlags.ParamFlags.PRIM_SELECTION):
            self.set_prims(self.get_scene().get_selected_prims())

    def value_changed(self, attrname: str, old_value: Any, value: Any):
        if attrname == "flags" and ChunkFlags.ParamFlags.PRIM_SELECTION in value:
            self.set_prims(self.get_scene().get_selected_prims())

        return super().value_changed(attrname, old_value, value)

    def set_prims(self, prims: Sequence[Usd.Prim]):
        """
        Sets the value of the param to a list of prim paths

        Args:
            prims (List[Usd.Prim]): The list of prims to set
        """
        self.value = ",".join([str(x.GetPath()) for x in prims])

    def get_prims(self) -> Sequence[Usd.Prim]:
        """
        If this param is a list of prim paths (comma separated), then return the prims on the stage

        Returns:
            Iterable[Usd.Prim]: The prims
        """
        if not (stage := self.get_stage()):
            return []
        res = []
        for pp in self.value.split(","):
            if pp and (prm := stage.GetPrimAtPath(pp)) and prm.IsValid():
                res.append(prm)
        return res

    @classmethod
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.LINEEDIT

    @classmethod
    def from_usd_rel(cls, rel: Usd.Relationship, *args, **kwargs):
        rel_name = rel.GetName()

        if (args and isinstance(args[0], str)) or kwargs.get("name"):
            param = cls(*args, **kwargs)
        else:
            kwargs["name"] = rel_name.split(":")[-1]
            param = cls(*args, **kwargs)
        try:
            if sender := param.get_attribute_sender("value"):
                param.set_usd_prop(str(rel.GetPath()), sender)
        except AttributeError:
            pass
        return param


class TextEdit(ChunkParamBase[str]):
    """
    An editable string parameter
    """

    flags: IntFlag = field(default=ChunkFlags.ParamFlags.LABELED)

    def __attrs_post_init__(self):
        super().__attrs_post_init__()

    @classmethod
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.TEXTEDIT


class FileBrowser(ChunkParamBase[str | Sdf.AssetPath]):
    """
    A string parameter with the addition of a button for opening a file browser

    Attributes:
        extensions: A list of supported file extensions
        pattern: A regex pattern for filtering file names. Defaults to "\\*"
    """

    flags: IntFlag = field(default=ChunkFlags.ParamFlags.LABELED)

    extensions: List[str] = field(factory=list)
    pattern: str = field(default="*")

    @classmethod
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.FILE

    def validate_attr(self, attr: "Attribute", val: Any) -> bool:
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


#################
# API FUNCTIONS #
#################


def set_validators_enabled(enabled: bool):
    from attrs import validators

    validators.set_disabled(not enabled)


usd_type_map = {
    "float": Slider,
    "int": (Spinbox, {"is_int": True}),
    "token": Menu,
    "bool": Checkbox,
    "string": LineEdit,
    "asset": FileBrowser,
    "path": FileBrowser,
    "float2": (Vector, {"param": Slider(), "suffixes": "xy", "length": 2}),
    "float3": (Vector, {"param": Slider(), "suffixes": "xyz", "length": 3}),
    "float4": (Vector, {"param": Slider(), "suffixes": "xyzw", "length": 4}),
    "vector3f": (Vector, {"param": Slider(), "suffixes": "xyz", "length": 3}),
    "color3f": (Vector, {"param": Slider(), "suffixes": "rgb", "length": 3}),
    "color4f": (Vector, {"param": Slider(), "suffixes": "rgba", "length": 4}),
    "int2": (Vector, {"param": Slider(), "suffixes": "xy", "length": 2}),
    "int3": (Vector, {"param": Slider(), "suffixes": "xyz", "length": 3}),
    "int4": (Vector, {"param": Slider(), "suffixes": "xyzw", "length": 4}),
    "double2": (Vector, {"param": Slider(), "suffixes": "xy", "length": 2}),
    "double3": (Vector, {"param": Slider(), "suffixes": "xyz", "length": 3}),
    "double4": (Vector, {"param": Slider(), "suffixes": "xyw", "length": 4}),
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

    t = usd_type_map.get(str(type_name))
    if not t:
        return None
    if isinstance(t, tuple):
        kwargs.update(t[1])
        return t[0].from_usd_attr(attr, *args, **kwargs)
    else:
        return t.from_usd_attr(attr, *args, **kwargs)


def param_from_rel(attr: Usd.Relationship, *args, **kwargs) -> ChunkCommon | None:
    return LineEdit.from_usd_rel(attr, *args, **kwargs)


def get_chunk_registry():
    res = ChunkRegistry()
    return res if res.initialized else None


def get_scene():
    if reg := get_chunk_registry():
        return reg.scene


def get_layer_manager():
    res = ChunkLayerManager()
    return res if res.initialized else None


def create_chunks(module_name: str, anonymous=False) -> Sequence[Chunk]:
    if not (reg := get_chunk_registry()):
        return []

    if not (mod := reg.get_chunk_module(module_name)):
        return []

    if not mod.loaded:
        chunks = mod.load()
    else:
        chunks = mod.create()

    chunks = cast(List[Chunk], chunks)

    if not anonymous:
        for c in chunks:
            reg.scene.add_chunk(c)

    return chunks


def get_chunk(chunk_name: str):
    if not (reg := get_chunk_registry()):
        return None
    return reg.scene.get_chunk(chunk_name)


##############
# PREDICATES #
##############


def is_param(v: Any) -> TypeGuard[ChunkParamBase]:
    """
    A predicate function for checking if an object is an instance of ChunkParamBase

    Args:
        v (Any): The value to check

    Returns:
        TypeGuard[ChunkParamBase]: The TypeGuard
    """
    return is_inst(ChunkParamBase)(v)


@overload
def is_param_t(t: ChunkFlags.ParamType) -> Predicate[ChunkParamBase]: ...


@overload
def is_param_t(t: type[ValueT]) -> Predicate[ChunkParamBase[ValueT]]: ...


def is_param_t(t: ChunkFlags.ParamType | type[ValueT]) -> Predicate[ChunkParamBase] | Predicate[ChunkParamBase[ValueT]]:
    """
    Creates a predicate function for checking if a parameter is an instance of ChunkParamBase, and that it holds
    a value of type t or the param.evaluate_type() is t if t is a chunkFlags.ParamType enum

    Args:
        t (Any): The value type or chunkFlags.ParamType to match

    Returns:
        Predicate[ChunkParamBase[ValueT]]: The predicate function
    """

    if isinstance(t, ChunkFlags.ParamType):

        def _fn_e(v) -> TypeGuard[ChunkParamBase]:
            if not is_param(v):
                return False
            return t in v.evaluate_type()

        return _fn_e

    def _fn_t(v) -> TypeGuard[ChunkParamBase]:
        if not is_param(v):
            return False
        return t in ChunkFlags.ParamTypeToTypes(v.evaluate_type())

    return _fn_t


def is_group(v: Any) -> TypeGuard[ChunkGroupBase]:
    """
    A predicate function for checking if v is a ChunkGroupBase

    Args:
        v (Any): The input value to check

    Returns:
        TypeGuard[ChunkGroupBase]: The TypeGuard
    """
    return is_inst(ChunkGroupBase)(v)


def is_chunk(v: Any) -> TypeGuard[ChunkBase]:
    """
    A predicate function for checking if v is a ChunkBase

    Args:
        v (Any): The input value to check

    Returns:
        TypeGuard[ChunkBase]: The TypeGuard
    """
    return is_inst(ChunkBase)(v)
