r"""The client-facing chunk API: the concrete chunk, group and param classes.

These classes can be used by themselves, but they also describe common UI information,
which makes them easy to implement in any UI framework.

As all chunk classes use chunk_core.AttributesMetaclass, they are all attrs classes
(dataclasses). Creating a new param class only takes declaring the class and inheriting
from [ChunkParamBase][usd_chunks.chunk_core.ChunkParamBase] or a related base class. The
class is wrapped in attrs.define() behind the scenes, so it gets some methods for free,
most notably \_\_init\_\_(), built from all the fields of the class, inherited ones
included. For this reason, any attribute of a chunk class should be declared with
attrs.field().

A class inheriting from [ChunkParamBase][usd_chunks.chunk_core.ChunkParamBase] needs to
implement [get_type][usd_chunks.chunk_core.ChunkParamBase.get_type], and
[evaluate_type][usd_chunks.chunk_core.ChunkParamBase.evaluate_type] too if the type can
change at runtime. That is what maps a param to a widget in whatever UI framework is
used.

!!! example

    ``` py
    class CustomStrParam(ChunkParamBase[str]):
        some_attrib: str = attrs.field(default="hello")

        @classmethod
        def get_type(cls) -> ChunkFlags.ParamType:
            return ChunkFlags.ParamType.LINEEDIT
    ```

In a chunk module, chunk.Chunk() is the top-level container that every param and group
is added to. A chunk with a button is as simple as:

!!! example
    ``` py
    from usd_chunks import chunk


    def main():
        my_chunk = chunk.Chunk("my_chunk")
        button = chunk.Button("my_button")

        my_chunk.add_param(button)
        return my_chunk
    ```

See [example](../example.md) for a more extensive example.
"""
from __future__ import annotations

import logging

from attr import Attribute as Attribute
from attrs import field, validators

from enum import IntFlag, Enum
from collections.abc import Sequence
from typing import Any, TYPE_CHECKING, cast, TypeGuard, overload
from typing_extensions import override

from pxr import Gf, Usd, Sdf

# Keep the ChunkNoticeAlias import as clients may use usd_chunks.chunk.ChunkNoticeAlias, without having to import chunk_notice
from usd_chunks.chunk_notice import ChunkNoticeAlias  # noqa: F401
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_core import (
    ChunkBase,
    Checkbox,
    ChunkConfig as _cfg,
    GizmoBase as _gizmo,
    check_gizmo_sugar as _check_gizmo_sugar,
    GIZMO_TRANSFORM_TYPE as _GIZMO_TRANSFORM,
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
from usd_chunks import chunk_utils

if TYPE_CHECKING:
    from attr import Attribute

    from usd_chunks.chunk_scene import SceneSender

logger = logging.getLogger(__name__)


###########
# CONFIGS #
###########


class DefaultConfig(_cfg):
    """This class is used as a default config.

    To use, in the chunk module, add an additional function besides main():

    ``` py
    def config():
        return chunk.DefaultConfig(
            load=True, load_for_plugin="chunk_usdview"
        )
    ```
    """

    pass


class Gizmo(_gizmo):
    """A viewport handle driven by this chunk's params.

    usd_chunks records; the DCC interprets. Declaring one authors ui:gizmo:* attributes on
    the chunk prim -- what the handle is, which prim it hangs off, and which params it
    writes. Drawing, picking and dragging live in the viewport layer.

    ``` py
    def config():
        return chunk.DefaultConfig(
            gizmos=[
                chunk.Gizmo("body", type="transform", anchor="primpath_location",
                            driver={"translate": ("tx", "ty", "tz")}),
            ],
        )
    ```

    Write-back goes through params, never through the prim: a chunk like transform.py
    clears its own layer and re-authors on every cook, so a handle that wrote the prim
    directly would be clobbered by the next execute.
    """


class StandardCategory(str, Enum):
    """The menu categories the bundled chunks use; a "/" nests a category."""
    MODELLING = "Modelling"
    DYNAMICS = "Dynamics"
    TRANSFORM = "Transform"
    UTILS = "Utility"
    RIGS = "Rigs"
    USER = "User"
    EXAMPLES = "Examples"
    USD = "USD"
    PROPERTIES = USD + "/Properties"
    USD_RENDER = USD + "/Render"
    USD_LAYER = USD + "/Layer"
    CORE = "Core"
    MATERIALS = "Materials"
    MTLX = MATERIALS + "/MaterialX"


##############################################
# CONCRETE IMPLEMENTATIONS FOR CHUNK OBJECTS #
##############################################


class Chunk(ChunkBase):
    """Chunk is the top level container, holding params and groups which in turn make up the final, displayed chunk.

    `gizmo="transform"` declares a single whole-chunk manipulator -- in effect "host, run
    your native transform handle and give me the result". It absorbs the `gizmo=` groups
    below it into one handle instead of leaving three:

    ``` py
    chunk_api = chunk.Chunk("Transform", gizmo="transform")
    ```
    """

    # Override defaults
    flags: IntFlag = field(
        default=ChunkFlags.GroupFlags.COLLAPSABLE | ChunkFlags.GroupFlags.LABELED | ChunkFlags.GroupFlags.FRAMED
    )
    gizmo: str | None = field(default=None)

    @override
    def __attrs_post_init__(self):
        super().__attrs_post_init__()
        if self.gizmo is not None and self.gizmo != _GIZMO_TRANSFORM:
            raise ValueError(
                f"Chunk {self.name!r}: only gizmo={_GIZMO_TRANSFORM!r} can be declared on a chunk. "
                f"Put {self.gizmo!r} on the param or group it drives."
            )


class Group(ChunkGroupBase):
    """A container holding child params and groups.

    A group can carry a `gizmo=` type, which is usually the lightest way to declare a
    handle: the group already *is* the params the handle drives, so nothing needs listing.

    ``` py
    grp = chunk.Group("translate_grp", "Translate", gizmo="translate")
    ```
    """

    # Override defaults
    flags: IntFlag = field(default=ChunkFlags.GroupFlags.LABELED | ChunkFlags.GroupFlags.FRAMED)
    gizmo: str | None = field(default=None)

    @override
    def __attrs_post_init__(self):
        super().__attrs_post_init__()
        if self.gizmo is not None:
            _check_gizmo_sugar(self.name, self.gizmo, scalar=False)


class ListGroup(ChunkListGroupBase):
    """A container of params and groups whose size can change.

    Params added with add_param are duplicated `size` times.
    """

    # Override defaults
    flags: IntFlag = field(
        default=ChunkFlags.GroupFlags.LABELED | ChunkFlags.GroupFlags.FRAMED | ChunkFlags.GroupFlags.LIST
    )


class Vector(ChunkVectorBase):
    """A group holding a fixed number of params, one per component."""

    # Override defaults
    flags: IntFlag = field(default=ChunkFlags.GroupFlags.LABELED | ChunkFlags.GroupFlags.FRAMED)
    gizmo: str | None = field(default=None)

    @override
    def __attrs_post_init__(self):
        super().__attrs_post_init__()
        if self.gizmo is not None:
            _check_gizmo_sugar(self.name, self.gizmo, scalar=False)


def _apply_usd_hard_limits(param, attr) -> None:
    """Copy an attribute's hard min/max limits onto a numeric param, if it has them."""
    try:
        limits = attr.GetLimits()  # type: ignore
    except Exception:
        limits = None
    if limits and (hard := limits.get("hard")):
        if hasattr(param, "min_value"):
            param.min_value = hard.get("minimum", None)  # type: ignore
        if hasattr(param, "max_value"):
            param.max_value = hard.get("maximum", None)  # type: ignore


class Slider(ChunkParamBase[int | float]):
    """A slider param holding a float or int value.

    Attributes:
        min_value: A minimum value which the value should not go below
        max_value: A maximum value which the value should not exceed
        is_int: Set whether this parameter should be an integer
    """

    min_value: float | int | None = field(default=None)
    max_value: float | int | None = field(default=None)
    is_int: bool = field(default=False)
    flags: IntFlag = field(default=ChunkFlags.ParamFlags.LABELED)
    gizmo: str | None = field(default=None)

    @override
    def __attrs_post_init__(self):
        super().__attrs_post_init__()
        if self.gizmo is not None:
            _check_gizmo_sugar(self.name, self.gizmo, scalar=True)

    @classmethod
    @override
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.INT_SLIDER | ChunkFlags.ParamType.FLOAT_SLIDER

    @override
    def evaluate_type(self) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.INT_SLIDER if self.is_int else ChunkFlags.ParamType.FLOAT_SLIDER

    @classmethod
    @override
    def from_usd_attr(cls, attr: Usd.Attribute, *args, **kwargs) -> ChunkUsdAttributeSupport:
        param = super().from_usd_attr(attr, *args, **kwargs)
        _apply_usd_hard_limits(param, attr)
        return param


class Spinbox(ChunkParamBase[float | int]):
    """A spinbox param holding a float or int value.

    Attributes:
        min_value: A minimum value which the value should not go below
        max_value: A maximum value which the value should not exceed
        is_int: Set whether this parameter should be an integer
    """

    min_value: float | int | None = field(default=None)
    max_value: float | int | None = field(default=None)
    is_int: bool = field(default=False)
    flags: IntFlag = field(default=ChunkFlags.ParamFlags.LABELED)

    @classmethod
    @override
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.INT_SPINBOX | ChunkFlags.ParamType.FLOAT_SPINBOX

    @override
    def evaluate_type(self) -> ChunkFlags.ParamType:
        if self.is_int:
            return ChunkFlags.ParamType.INT_SPINBOX
        return ChunkFlags.ParamType.FLOAT_SPINBOX

    @classmethod
    @override
    def from_usd_attr(cls, attr: Usd.Attribute, *args, **kwargs) -> ChunkUsdAttributeSupport:
        param = super().from_usd_attr(attr, *args, **kwargs)
        _apply_usd_hard_limits(param, attr)
        return param


class Menu(ChunkParamBase[str]):
    """A menu param: one choice out of menu_items.

    Attributes:
        menu_items: A list of items to display in the menu
    """

    flags: IntFlag = field(default=ChunkFlags.ParamFlags.LABELED)

    menu_items: Sequence[str] = field(factory=list)

    #: True when menu_items is a snapshot of something that changes -- the expression
    #: variables a stage declares, say -- rather than the fixed set of legal values. A value
    #: outside the list is then kept instead of being discarded back to menu_items[0]: on
    #: load a param's value is restored before whatever rebuilds the list has run, and a
    #: saved choice would otherwise be silently replaced by the first entry.
    dynamic_items: bool = field(default=False)

    @classmethod
    @override
    def from_usd_attr(cls, attr: Usd.Attribute, *args, **kwargs):
        param = super().from_usd_attr(attr, *args, **kwargs)

        menu_items = set((param.default_value,))  # type: ignore
        if tokens := attr.GetMetadata("allowedTokens"):
            menu_items.update(tokens)

        param.menu_items = [str(x) for x in menu_items]  # type: ignore
        return param

    @classmethod
    @override
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.STRING_MENU

    @override
    def convert_attr(self, attr: Attribute, val: Any) -> Any:
        if attr.name in ["default_value", "value"]:
            if self.menu_items and not self.dynamic_items and val is not None and val not in self.menu_items:
                if isinstance(val, str) and chunk_utils.has_expression(val):
                    # A node-graph connection (_author_connection) writes an @Node.param@
                    # expression here, resolved live at read time -- trust it even though
                    # it isn't a literal menu_items entry, rather than discarding it back
                    # to menu_items[0]. Checking get_usd_prop().GetConnections() instead
                    # doesn't work here: for a param at its untouched default, nothing has
                    # ever wired _param_attr_path yet, so get_usd_prop() always returns
                    # None right when the connection is first being authored -- the one
                    # moment this check actually needs to succeed.
                    return val
                return self.menu_items[0]

        return super().convert_attr(attr, val)

    def get_selected_index(self):
        """The index of the current value in menu_items, 0 if it is not there."""
        try:
            return self.menu_items.index(self.value)
        except ValueError:
            return 0

    def set_items(self, items) -> None:
        """Replace menu_items with items, keeping the current value listed.

        The current value stays in the list even when items no longer has it: a saved
        choice arrives before the upstream chunk that makes it has cooked, and a view-driven
        recook mutes layers one at a time, so "not there right now" is routinely transient.
        Dropping it would lose the choice for good.

        Only assigned when something changed: menu_items is a plain field, and writing it
        re-triggers the chunk's execute() whether or not anything differs.

        Args:
            items: The new choices.
        """
        current = self.value or ""
        items = list(items)
        if current and current not in items and not chunk_utils.has_expression(current):
            items.append(current)
        if list(self.menu_items or []) != items:
            self.menu_items = items


class Button(ChunkParamBase):
    """A button param.

    It stores no usable value and is used with callbacks only.
    """

    @classmethod
    @override
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.BUTTON

    def click(self):
        """Click the button, triggering its connected callbacks."""
        self.get_attribute_sender("value").execute()


class LineEdit(ChunkParamBase[str]):
    """An editable single-line string param.

    Set ParamFlags.PRIM_SELECTION when the param holds Usd.Prim paths.
    """

    flags: IntFlag = field(default=ChunkFlags.ParamFlags.LABELED)

    @override
    def __attrs_post_init__(self):
        super().__attrs_post_init__()

        if self.has_flag(ChunkFlags.ParamFlags.PRIM_SELECTION):
            self.set_prims(self.get_scene().get_selected_prims())

    @override
    def value_changed(self, attrname: str, old_value: Any, value: Any):
        if attrname == "flags" and ChunkFlags.ParamFlags.PRIM_SELECTION in value:
            self.set_prims(self.get_scene().get_selected_prims())

        return super().value_changed(attrname, old_value, value)

    def set_prims(self, prims: Sequence[Usd.Prim]):
        """Set the value to a comma-separated list of these prims' paths.

        Args:
            prims (Sequence[Usd.Prim]): The prims to set
        """
        self.value = ",".join([str(x.GetPath()) for x in prims])

    def resolve_paths(self) -> list[str]:
        """The prim paths in the value, with globs and collection references expanded.

        Supports space/comma separation and Houdini-style glob patterns: ``*`` within one path
        component, ``**`` across any depth, ``?`` a single character. See
        chunk_utils.resolve_prim_paths.

        Returns:
            The resolved prim path strings, empty without a stage or a value.
        """
        if not (stage := self.get_stage()) or not self.value:
            return []
        return chunk_utils.resolve_prim_paths(stage, self.value)

    def get_prims(self) -> Sequence[Usd.Prim]:
        """The valid prims on the stage for the paths in the value.

        Returns:
            Sequence[Usd.Prim]: The prims
        """
        if not (stage := self.get_stage()):
            return []
        return [prm for pp in self.resolve_paths() if (prm := stage.GetPrimAtPath(pp)) and prm.IsValid()]

    @classmethod
    @override
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.LINEEDIT

    @classmethod
    def from_usd_rel(cls, rel: Usd.Relationship, *args, **kwargs):
        """Create a param that holds a relationship's targets as paths.

        Args:
            rel: The relationship.
            *args: Passed to the param constructor.
            **kwargs: Passed to the param constructor; `name` defaults to the relationship's
                base name.

        Returns:
            The new param.
        """
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
    """An editable multi-line string param."""

    flags: IntFlag = field(default=ChunkFlags.ParamFlags.LABELED)
    rows: int | None = field(default=None, metadata={"validate": False, "convert": False})

    @classmethod
    @override
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.TEXTEDIT


class FileBrowser(ChunkParamBase[str | Sdf.AssetPath]):
    r"""A string param with a button that opens a file browser.

    Attributes:
        extensions: A list of supported file extensions
        pattern: A glob pattern for filtering file names. Defaults to "\*"
    """

    flags: IntFlag = field(default=ChunkFlags.ParamFlags.LABELED)

    extensions: list[str] = field(factory=list)
    pattern: str = field(default="*")

    @classmethod
    @override
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.FILE

    @override
    def validate_attr(self, attr: Attribute, val: Any) -> bool:
        if attr.name in ["value", "default_value"] and val:
            if self.has_flag(ChunkFlags.ParamFlags.FILE_EXISTING):
                self._validate_existing_path(attr, val)
            else:
                self._validate_path(attr, val)

        return super().validate_attr(attr, val)


class Label(ChunkParamBase[str]):
    """A read-only text param.

    Its name or label attribute is the text displayed.
    """

    # Text shown to the user, never an input: a module setting its own status line during
    # execute() must not recook everything downstream (AttributeSender.is_presentation_change).
    presentation_only = True

    @classmethod
    @override
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.LABEL


class Separator(ChunkParamBase):
    """A param that only separates others in the UI, and holds no value."""

    @classmethod
    @override
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.SEPARATOR


class ColorPicker(ChunkParamBase[tuple]):
    """A colour param holding an (r, g, b) tuple with values in the 0-1 range.

    A UI shows it as a colour swatch that opens a colour dialog.
    """

    flags: IntFlag = field(default=ChunkFlags.ParamFlags.LABELED)

    @override
    def convert_attr(self, attr: Attribute, val: Any) -> Any:
        # A colour read back from USD arrives as Gf.Vec3f, which the tuple validator
        # rejects -- so a ColorPicker bound to a chunk-prim Color3f attr would fail on the
        # value it had just written. Anything three-long and iterable is a colour here.
        if attr.name in ("default_value", "value") and val is not None:
            if not isinstance(val, (tuple, list, str)):
                try:
                    return tuple(val)
                except TypeError:
                    pass

        return super().convert_attr(attr, val)

    @classmethod
    @override
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.COLOR_PICKER

    @override
    def evaluate_type(self) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.COLOR_PICKER


class Matrix(ChunkParamBase[Gf.Matrix4d]):
    """A parameter holding a Gf.Matrix4d, stored in USD as a native matrix4d attribute.

    Draws nothing. A matrix is data a chunk keeps -- a camera's placement, a baked
    transform -- rather than something anyone wants to edit as sixteen numbers, so this
    exists for values that have to survive a save, not for the UI.

    Defaults to the identity, which also means an untransformed param stays at its
    default and never authors an attribute -- the same "only write what changed" rule
    every other param follows.
    """

    @override
    def convert_attr(self, attr: Attribute, val: Any) -> Any:
        # A matrix arriving from USD or from a sequence of sixteen numbers is still a
        # matrix; only reject what cannot be read as one. Without this, restoring a saved
        # value would have to be typed exactly right by every caller.
        if attr.name in ("default_value", "value") and val is not None and not isinstance(val, Gf.Matrix4d):
            try:
                return Gf.Matrix4d(*[float(c) for row in val for c in row])
            except (TypeError, ValueError):
                try:
                    return Gf.Matrix4d(*[float(c) for c in val])
                except (TypeError, ValueError):
                    pass

        return super().convert_attr(attr, val)

    @classmethod
    @override
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.MATRIX


class Custom(ChunkParamBase):
    """A param whose widget is supplied by the chunk, through param.custom_widget."""

    @classmethod
    @override
    def get_type(cls) -> ChunkFlags.ParamType:
        return ChunkFlags.ParamType.CUSTOM


#################
# API FUNCTIONS #
#################


def set_validators_enabled(enabled: bool):
    """Turn attrs validators on or off globally."""
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
    # A colour channel is 0..1 by definition. Without the range the slider widget falls back
    # to 0..100, so displayColor read off a prim by the Property Editor came up as three
    # sliders on which the whole usable range sat in the first percent.
    "color3f": (Vector, {"param": Slider(min_value=0.0, max_value=1.0), "suffixes": "rgb", "length": 3}),
    "color4f": (Vector, {"param": Slider(min_value=0.0, max_value=1.0), "suffixes": "rgba", "length": 4}),
    "int2": (Vector, {"param": Slider(), "suffixes": "xy", "length": 2}),
    "int3": (Vector, {"param": Slider(), "suffixes": "xyz", "length": 3}),
    "int4": (Vector, {"param": Slider(), "suffixes": "xyzw", "length": 4}),
    "double2": (Vector, {"param": Slider(), "suffixes": "xy", "length": 2}),
    "double3": (Vector, {"param": Slider(), "suffixes": "xyz", "length": 3}),
    "double4": (Vector, {"param": Slider(), "suffixes": "xyw", "length": 4}),
}


def param_from_attr(attr: Usd.Attribute, *args, **kwargs) -> ChunkCommon | None:
    """Create a param for a Usd.Attribute, if its type is supported.

    Uses the from_usd_attr class method of the matching param type, which connects the
    ATTRIBUTE_CHANGED notice so the param controls the USD attribute. The result is a
    ChunkCommon rather than a ChunkParamBase because some params are really groups
    (Vectors, for example).

    Args:
        attr (Usd.Attribute): The attribute to create a param for
        *args: Passed to the param constructor.
        **kwargs: Passed to the param constructor.

    Returns:
        ChunkCommon | None: The param, or None if no param type maps the attr's type.
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
    """Similar to param_from_attr but for a Usd.Relationship. Will create a LineEdit.

    Args:
        attr (Usd.Relationship): The relationship to connect with a param
        *args: Passed to the param constructor.
        **kwargs: Passed to the param constructor.

    Returns:
        ChunkCommon | None: The resulting param if created successfully
    """
    return LineEdit.from_usd_rel(attr, *args, **kwargs)


def show_attribute(prim, target_group, collapse_non_inputs=True, var_selections=None):
    """Populate a group with typed editable params for all attributes and relationships on a prim.

    This is the shared utility for dynamically building attribute editor UIs.
    Used by the Dynamic chunk, Light chunk, and any chunk that needs to expose
    a prim's attributes as interactive widgets.

    Args:
        prim: The Usd.Prim whose attributes to display.
        target_group: A ChunkGroupBase to populate with params. Existing children
            are cleared before populating -- unless this group was last populated from
            the same attributes, in which case they are kept and re-authored.
        collapse_non_inputs: If True, namespace groups other than 'inputs' start
            collapsed. Keeps the UI tidy for prims with many attributes.
        var_selections: The (anchor_prim_path, vs_name, variant) tuples active when `prim`
            was resolved (split_variant_path / author_in_variant_context), if any. Recorded
            on each generated attribute param so a later edit re-enters the same variant
            (author_in_variant_selections) instead of authoring outside it -- see
            ChunkUsdAttributeSupport.data_changed. None, the default, takes whichever
            variant scopes are open around `prim` right now
            (chunk_utils.open_variant_selections), so a call inside
            author_in_variant_contexts' loop records its variant without being told. Pass
            () to record none.
    """
    if not prim or not prim.IsValid():
        # Clean existing params: no prim means nothing to show.
        for grp in target_group.get_my_params():
            grp.request_delete()
        target_group._shown_signature = None
        return

    if var_selections is None:
        var_selections = chunk_utils.open_variant_selections(prim.GetStage(), prim.GetPath())

    # The same attributes as last time, and every param still bound: keep the params.
    #
    # A chunk that shows its own prim (Light) calls this on every cook, and every edit to
    # one of these params is a cook -- rebuilding here deleted and recreated every param and
    # its widget on each tick of a slider, the one being dragged included. A param binds to
    # its attribute by path, so an unchanged signature means each existing param is already
    # bound to the right place. What the cook's cleared layer lost is only what they
    # authored -- the driven connection and value of a param that was edited -- and the
    # framework authors those again after the cook (ChunkBase.reauthor_driven_attrs). An
    # untouched param authors nothing and reads the schema fallback, the same as before.
    try:
        signature = _shown_signature(prim, var_selections, collapse_non_inputs)
    except RuntimeError:
        logger.debug("show_attribute: %s went away while it was being read; leaving this "
                     "chunk's params alone for the next cook", prim.GetPath())
        return
    if (signature == getattr(target_group, "_shown_signature", None)
            and target_group.get_my_params()
            and _shown_params_bound(target_group, str(prim.GetPath()))):
        return

    # Read the prim FIRST, and build every param, before touching target_group at all.
    #
    # Installing a param fires notices; a cook reached that way can recompose the stage; and
    # a resync EXPIRES this prim handle along with every attribute already read off it, so
    # the next read raises. For example, a Stage Expression Switch writes the record of
    # what it muted, that write cascades into cooking a light on the branch it just
    # suppressed, and the light's own prim is gone by the time this loop reaches its
    # second attribute.
    #
    # Raising there is not a local failure -- the exception would escape into
    # gate_to_branches and leave the gate half done, with the branch it was suppressing
    # still composed. Collecting first turns that into a lost cook rather than a
    # half-emptied chunk: nothing has been deleted yet, the params the chunk already has
    # stay exactly as they were, and the next cook rebuilds them.
    prim_path = prim.GetPath()
    specs = []
    try:
        for attr in prim.GetAttributes():
            specs.append((attr.GetNamespace(),
                          param_from_attr(attr, var_selections=var_selections),
                          collapse_non_inputs))
        for rel in prim.GetRelationships():
            specs.append((rel.GetNamespace(), param_from_rel(rel), False))
    except RuntimeError:
        logger.debug("show_attribute: %s went away while it was being read; leaving this "
                     "chunk's params alone for the next cook", prim_path)
        return

    # Clean existing params, now that there is something to replace them with.
    for grp in target_group.get_my_params():
        grp.request_delete()

    groups = {}
    for ns, param, collapse in specs:
        if not param:
            continue

        if not isinstance(param, ChunkGroupBase):
            param.set_flag(ChunkFlags.ParamFlags.LABELED)

        tgt_grp = target_group if not ns else groups.get(ns)
        if not tgt_grp:
            tgt_grp = Group(
                ns,
                flags=ChunkFlags.GroupFlags.COLLAPSABLE | ChunkFlags.GroupFlags.LABELED,
            )
            if collapse and ns and ns != "inputs":
                tgt_grp.set_state(ChunkFlags.State.COLLAPSED)
            target_group.add_param(tgt_grp)
            groups[ns] = tgt_grp

        tgt_grp.add_param(param)

    target_group._shown_signature = signature


def _shown_signature(prim, var_selections, collapse_non_inputs) -> tuple:
    """What show_attribute builds from a prim: every attribute by path and type, every
    relationship, and the variant scopes they were read in. Two calls with the same one
    would build the same params.
    """
    return (
        tuple(var_selections),
        collapse_non_inputs,
        tuple((str(attr.GetPath()), str(attr.GetTypeName())) for attr in prim.GetAttributes()),
        tuple(str(rel.GetPath()) for rel in prim.GetRelationships()),
    )


def _shown_params_bound(target_group, prim_path: str) -> bool:
    """Whether every param show_attribute built still reaches its attribute on prim_path --
    bound to it directly, or driving it from the chunk prim once edited. One that reaches
    neither drives nothing, and only a rebuild binds it again.
    """
    prefix = prim_path + "."
    for _, param in target_group.iter_hierarchy():
        if not isinstance(param, ChunkUsdAttributeSupport) or isinstance(param, ChunkGroupBase):
            continue
        if getattr(param, "_driven_attr_paths", None):
            continue
        if not (param._param_attr_path or "").startswith(prefix):
            return False
    return True


def get_chunk_registry() -> ChunkRegistry | None:
    """Get the ChunkRegistry instance, which finds and registers chunk modules.

    Returns:
        ChunkRegistry | None: The registry if it has been initialized, otherwise None
    """
    res = ChunkRegistry()
    return res if res.initialized else None


def get_scene() -> SceneSender | None:
    """Return the current SceneSender, which finds, adds, saves and loads chunks.

    Returns:
        SceneSender | None: The current scene, or None if there is no scene
    """
    if reg := get_chunk_registry():
        return reg.scene


def get_layer_manager() -> ChunkLayerManager | None:
    """Return the ChunkLayerManager. This can be used to manage the chunk layers. Useful for layer management such as clearing, muting, removing, etc.

    Returns:
        ChunkLayerManager | None: The ChunkLayerManager if found, otherwise None
    """
    res = ChunkLayerManager()
    return res if res.initialized else None


def create_chunks(module_name: str, anonymous=False) -> Sequence[Chunk]:
    """Find a ChunkModule in the registry and run it's main() function, then add the resulting chunk(s) to the scene if not anonymous.

    To build a network from a script, use `scene.create.<module>(...)` instead (see
    docs/scripting.md): it also sets params, wires chunks together and reaches menu
    variants. This stays for making a chunk without adding it (anonymous=True).

    Args:
        module_name (str): The name of the module to load, relative to USD_CHUNK_DIRS
        anonymous (bool, optional): If set to True, the resulting chunks will be added to the scene directly. Defaults to False.

    Returns:
        Sequence[Chunk]: The created chunks
    """
    if not (reg := get_chunk_registry()):
        return []

    if not (mod := reg.get_chunk_module(module_name)):
        return []

    if not mod.loaded:
        chunks = mod.load()
    else:
        chunks = mod.create()

    chunks = cast(list[Chunk], chunks)

    if not anonymous:
        for c in chunks:
            reg.scene.add_chunk(c)

    return chunks


def get_chunk(chunk_name: str):
    """A simple method to find a chunk in the current scene.

    Args:
        chunk_name (str): The name of the chunk to find. Note that this should be the current chunk instance name

    Returns:
        Chunk: The found chunk, or None if not found
    """
    if not (reg := get_chunk_registry()):
        return None
    return reg.scene.get_chunk(chunk_name)


##############
# PREDICATES #
##############


def is_param(v: Any) -> TypeGuard[ChunkParamBase]:
    """Check whether an object is a ChunkParamBase.

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
    """Create a predicate matching params of a value type or a ParamType.

    A value type matches a param holding a value of that type; a ChunkFlags.ParamType
    matches a param whose evaluate_type() includes it.

    Args:
        t (ChunkFlags.ParamType | type): The value type or ParamType to match

    Returns:
        Predicate[ChunkParamBase]: The predicate function
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
    """Check whether v is a ChunkGroupBase.

    Args:
        v (Any): The input value to check

    Returns:
        TypeGuard[ChunkGroupBase]: The TypeGuard
    """
    return is_inst(ChunkGroupBase)(v)


def is_chunk(v: Any) -> TypeGuard[ChunkBase]:
    """Check whether v is a ChunkBase.

    Args:
        v (Any): The input value to check

    Returns:
        TypeGuard[ChunkBase]: The TypeGuard
    """
    return is_inst(ChunkBase)(v)
