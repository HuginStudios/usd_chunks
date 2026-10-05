"""Blender widgets for chunk params -- the Blender counterpart of `chunk_qt` / `chunk_flet`.

Blender's UI is neither retained nor immediate in the way the other two are, and that one
difference shapes everything here.

Qt builds widget objects that own their value and live as long as the panel. Flet holds
controls and pushes values into them. Blender has **no widget objects at all**: a node draws
itself by calling `layout.prop(self, "name")` inside `draw_buttons`, and the value lives in
an RNA property on the node, not in anything a factory could hand back. So the thing this
module maps a param to is not a widget but a **binding** -- a property declaration plus the
one line that draws it.

Two layers, because RNA properties are fixed when a class is registered and chunks loft
params at runtime:

1. **Declared params become generated RNA properties.** One `bpy.types.Node` subclass per
   chunk type, built from `main()`. These get an `update=` callback, so editing in the node
   body writes straight through to the chunk param and cooks.
2. **Lofted params become per-instance ID properties** (`node["name"]`), drawn with
   `layout.prop(node, '["name"]')`. A Wedge's ranges and a Stage Expression's variables only
   exist after `_sync_*` has run, which is long after registration -- an RNA property cannot
   describe them, and two nodes of the same class need different ones. ID properties carry
   no `update` callback, so those sync on draw instead (see `sync_lofted`).

   **ListGroup rows are ID properties too**, keyed `list.row.param` (`prototypes.0.proto_path`
   -- see `prop_key`). A row is a proxy group whose params carry the template's NAMES, so
   registering the template as RNA gave every row one shared property whose `update`
   wrote to the template, which no row reads: a Scatter's prototype could never be set
   from here. Rows are also per instance and per moment, which is the ID-property case.

The loop that has to stay broken is the same one every frontend has: writing a value into
the param from a draw callback would be re-entered by the notice that write produces. So
`_write_param` guards, and `sync_lofted` compares before assigning.

Requires: bpy (Blender 4.2+). Not importable outside Blender, which is why the graph
frontend and the conformance test both guard the import.
"""
from __future__ import annotations

import logging
import os
import time

import bpy
import bpy.utils.previews

from usd_chunks import chunk_core as core
from usd_chunks import chunk_widget_api
from usd_chunks import chunk_stage
from usd_chunks.chunk_flags import ChunkFlags

logger = logging.getLogger(__name__)

PT = ChunkFlags.ParamType

#: Params carrying this flag are computed, not edited. Blender has no read-only property,
#: so they are drawn disabled rather than hidden -- an OUTPUT is usually the interesting
#: half of a compute chunk and hiding it would make the node look empty.
_OUTPUT = ChunkFlags.ParamFlags.OUTPUT

#: Guard against the write-back loop described in the module docstring. Keyed by id() of the
#: param rather than a single global, so two params updating in one draw pass do not mask
#: each other.
_writing: set = set()


def _write_param(param, value) -> None:
    """Push a UI value into the chunk param, once.

    Re-entrancy is not hypothetical here: setting `param.value` fires ATTRIBUTE_CHANGED,
    the node redraws, `draw_buttons` runs again, and a naive sync writes the same value
    back. The guard makes the second write a no-op rather than relying on the values
    happening to compare equal.
    """
    key = id(param)
    if key in _writing:
        return
    _writing.add(key)
    try:
        if param.value != value:
            param.value = value
    except Exception:  # pragma: no cover - a chunk's setter may reject a value
        logger.exception("could not set %s", getattr(param, "name", "<param>"))
    finally:
        _writing.discard(key)


def _updater(param_name: str):
    """An RNA `update=` callback that writes this property through to its chunk param.

    Resolved by name at call time rather than closing over the param object: the node
    outlives any particular chunk instance (a reload rebuilds the chunks under a tree that
    Blender kept), so a captured reference would go stale and write into a dead param.
    """
    def _update(self, context):
        chunk = self.get_chunk()
        if chunk is None:
            return
        param = chunk.get_param(param_name)
        if param is not None:
            _write_param(param, getattr(self, param_name))
    return _update


#: Blender icons for ImageMenu pictures, keyed by file path. Made on first use: a
#: previews collection is a Blender resource, and most sessions never show an ImageMenu.
_previews = None


def _icon_id(path: str) -> int:
    """The Blender icon for an image file, 0 (no icon) while the file is not there yet."""
    global _previews
    if not path or not os.path.isfile(path):
        return 0
    if _previews is None:
        _previews = bpy.utils.previews.new()
    if path not in _previews:
        try:
            _previews.load(path, path, "IMAGE")
        except Exception:  # an unreadable picture is an item without one
            logger.debug("could not load %s as an icon", path, exc_info=True)
            return 0
    return _previews[path].icon_id


#: Menus with more items than this get a search button beside the dropdown.
SEARCH_FROM = 12

#: id(ImageMenu) -> ((menu_items, item_images) it was built from, the enum items). The
#: source objects are held, so an id cannot be reused while its entry is checked against.
_image_items: dict = {}


def release_previews() -> None:
    """Free the icons (unregister)."""
    global _previews
    _image_items.clear()
    if _previews is not None:
        bpy.utils.previews.remove(_previews)
        _previews = None


def _enum_items(param):
    """`menu_items` as Blender enum tuples, never empty.

    An EnumProperty with no items is a registration error, and a Menu whose items are
    filled in by a callback (`set_function` on menu_items) legitimately has none at class
    build time -- so a placeholder stands in until the node is drawn.

    An ImageMenu's items carry their pictures as icons -- the five-element form, which
    Blender wants for every item once one has an icon -- and template_icon_view draws them.
    """
    items = list(getattr(param, "menu_items", None) or []) if param is not None else []
    if not items:
        return [("", "", "")]
    images = getattr(param, "item_images", None)
    if images is None:
        return [(str(i), str(i), "") for i in items]
    # Built once per change, not per call: Blender asks for the items on every redraw of
    # every node -- panning the editor redraws them all -- and a library of hundreds of
    # pictures cost a stat and an icon lookup per item each time, which made moving
    # around the network crawl. menu_items and item_images are replaced, never mutated,
    # when they change, so identity says whether the cached list is still right.
    source = (param.menu_items, images)
    cached = _image_items.get(id(param))
    if cached is not None and cached[0][0] is source[0] and cached[0][1] is source[1]:
        return cached[1]
    # A thumbnail that lands later comes as a new item_images dict, so it rebuilds this.
    built = [(str(i), str(i), "", _icon_id(images.get(i, "")), n) for n, i in enumerate(items)]
    _image_items[id(param)] = (source, built)
    return built


#: Keeps the tuples a dynamic enum callback returned alive, keyed by (node, param).
#: Blender does NOT own the strings a Python items callback hands back -- if the list is
#: collected before the menu is drawn, the entries come out as garbage. Holding the most
#: recent list per property is the documented way round it.
_enum_cache: dict = {}

#: When each (node, param) last asked its module for its items (Menu.refresh_items).
#: Blender calls an items callback on every redraw of the panel, not only when the menu
#: opens, and the hook may walk the stage -- so it is asked at most this often.
_REFRESH_EVERY = 1.0
_refreshed: dict = {}


def _enum_items_cb(param_name: str):
    """An `items=` CALLBACK for a Menu, rather than a fixed list.

    A list passed to EnumProperty is frozen when the class registers, which is before any
    stage is open. That is wrong for every Menu whose entries are runtime data -- an Edit
    Target's layers, a schema list, anything with `set_function` on menu_items -- and it
    showed as a dropdown that would not open or offered one stale entry. Resolving the
    param on each draw is what makes those menus live.
    """
    def _items(self, context):
        # Blender calls this with no instance in some contexts (a tooltip, an RNA walk).
        chunk = self.get_chunk() if self is not None and hasattr(self, "get_chunk") else None
        param = chunk.get_param(param_name) if chunk is not None else None
        key = (getattr(self, "name", ""), param_name)
        now = time.monotonic()
        if hasattr(param, "refresh_items") and now - _refreshed.get(key, -_REFRESH_EVERY) >= _REFRESH_EVERY:
            _refreshed[key] = now
            param.refresh_items()
        items = _enum_items(param)
        _enum_cache[(getattr(self, "name", ""), param_name)] = items
        return items
    return _items


def _range(param, cast):
    """min/max kwargs for a numeric property, omitted when the param declares none.

    Blender defaults an unbounded property to a sane soft range; passing None would be a
    type error, so an absent bound has to be left out rather than passed through.
    """
    kw = {}
    if getattr(param, "min_value", None) is not None:
        kw["min"] = cast(param.min_value)
    if getattr(param, "max_value", None) is not None:
        kw["max"] = cast(param.max_value)
    return kw


def list_row_of(param):
    """(list group, row group) enclosing `param`, or None when it is not in a ListGroup.

    A row is a ChunkGroupBase directly under a ChunkListGroupBase; the param may sit a
    Vector or two below it. Walks owners up to the chunk, so nesting depth does not
    matter, and stops at the first list found -- a list inside a list row is keyed by the
    inner one, which is the row whose value is actually being edited. The chain
    alternates chunk objects with the NameSyncedContainers that hold them, so only the
    chunk objects count.
    """
    child, owner = param, param.get_owner()
    while owner is not None and not isinstance(owner, core.ChunkBase):
        if isinstance(owner, core.ChunkListGroupBase):
            return owner, child
        if isinstance(owner, core.ChunkCommon):
            child = owner
        owner = owner.get_owner()
    return None


def prop_key(param) -> str:
    """The node property that holds `param`: its name, or `list.row.name` inside a
    ListGroup row. RNA identifiers cannot carry a dot, which is fine -- a row key is
    only ever an ID property.
    """
    found = list_row_of(param)
    if found is None:
        return param.name
    list_group, row = found
    return f"{list_group.name}.{row.name}.{param.name}"


def resolve_key(chunk, key: str):
    """The param a node property key names, or None -- the inverse of prop_key."""
    if "." not in key:
        return chunk.get_param(key)
    list_name, row, name = key.split(".", 2)
    list_group = chunk.get_param(list_name)
    if not isinstance(list_group, core.ChunkListGroupBase) or not row.isdigit():
        return None
    try:
        return list_group.get_indexed_param(name, int(row))
    except (IndexError, ValueError):
        return None


def is_hidden(param) -> bool:
    """Whether `param` is hidden, itself or through any group above it.

    A chunk hides a whole Group at once (mesh keeps cube, tube and grid groups and
    shows the one its menu names) and the params inside carry no bit of their own. Read
    only the param's, as this did, and all three groups' fields drew at once -- eighteen
    of them -- and the menu visibly did nothing.
    """
    obj = param
    while obj is not None and not isinstance(obj, core.ChunkBase):
        if isinstance(obj, core.ChunkCommon) and obj.has_state(ChunkFlags.State.HIDDEN):
            return True
        obj = obj.get_owner()
    return False


def property_for(param):
    """The Blender RNA property that backs one chunk param, or None if it is not a value.

    None means "this param is not a property" rather than "unsupported": a Button is an
    operator and a Separator is a layout call, neither of which stores anything. The caller
    draws those directly.
    """
    t = param.get_type()
    name = param.name
    label = getattr(param, "label", None) or name
    tip = getattr(param, "tooltip", "") or ""
    upd = _updater(name)

    # `t == PT.NONE`, not `PT.NONE in t`. These are IntFlags and `in` is a SUBSET test:
    # NONE is bit 3, and FILE is STRING|bit 3 -- so `PT.NONE in t` was true for every
    # FileBrowser, and every file param returned None here. The node then drew it as a bare
    # label with no field and no browse button, which read as "the file dialog is missing"
    # rather than as a param that was dropped. BUTTON and SEPARATOR are genuine subset
    # tests: both carry EMPTY plus a bit of their own, and nothing else sets that pair.
    if PT.BUTTON in t or PT.SEPARATOR in t or t == PT.NONE:
        return None

    if PT.COLOR_PICKER in t:
        return bpy.props.FloatVectorProperty(
            name=label, description=tip, subtype="COLOR", size=3,
            min=0.0, max=1.0, default=(0.5, 0.5, 0.5), update=upd)

    if PT.STRING_MENU in t:
        return bpy.props.EnumProperty(
            name=label, description=tip, items=_enum_items_cb(name), update=upd)

    # BOOL before INT: CHECKBOX is BOOL, and bool is an int subclass everywhere else in
    # Python, so testing INT first would turn every checkbox into a number field.
    if PT.BOOL in t:
        return bpy.props.BoolProperty(name=label, description=tip, update=upd)

    # A Slider declares BOTH INT and FLOAT (its type is 161 = INT|FLOAT|1) because one
    # Slider class serves either, and `is_int` is what actually decides. So the flags alone
    # cannot answer this: testing INT first made every float slider an IntProperty, and
    # Blender then rejected the float on assignment ("expected an int type, not float") --
    # which surfaced as params silently failing to mirror rather than as an obvious error.
    if PT.INT in t and PT.FLOAT in t:
        if getattr(param, "is_int", False):
            return bpy.props.IntProperty(name=label, description=tip, update=upd,
                                         **_range(param, int))
        return bpy.props.FloatProperty(name=label, description=tip, update=upd,
                                       **_range(param, float))

    if PT.INT in t:
        return bpy.props.IntProperty(name=label, description=tip, update=upd,
                                     **_range(param, int))

    if PT.FLOAT in t:
        return bpy.props.FloatProperty(name=label, description=tip, update=upd,
                                       **_range(param, float))

    if PT.FILE in t:
        # chunk_widget_api.file_dialog_spec is the shared reading of a FileBrowser's own
        # settings -- patterns, directory-vs-file, must-exist -- written down so frontends
        # cannot drift from Qt's interpretation. Blender's file picker takes only the
        # directory distinction (DIR_PATH vs FILE_PATH), so the rest is carried in the
        # tooltip rather than dropped silently: a user picking a .png for a param that
        # wants .usda should at least be told what it expects.
        spec = chunk_widget_api.file_dialog_spec(param)
        if spec.patterns and spec.patterns != ("*",):
            tip = (tip + "  " if tip else "") + f"({' '.join(spec.patterns)})"
        return bpy.props.StringProperty(
            name=label, description=tip,
            subtype="DIR_PATH" if spec.directory else "FILE_PATH", update=upd)

    if PT.STRING in t:
        return bpy.props.StringProperty(name=label, description=tip, update=upd)

    # VECTOR that is not a colour, MATRIX, CUSTOM. A Qt custom_widget has no Blender form
    # and a matrix has no sensible node-body control; both are reported rather than
    # silently dropped, so a chunk that looks empty in Blender says why in the log.
    logger.debug("no Blender property for param %s (type %s)", name, t)
    return None


def annotations_for(chunk) -> tuple[dict, list, list]:
    """(rna properties, button param names, unmapped param names) for one chunk.

    Returned as three lists rather than one dict so the node class knows what to *draw*:
    properties get `layout.prop`, buttons get `layout.operator`, and the rest are skipped
    with a note.
    """
    props: dict = {}
    buttons: list = []
    skipped: list = []
    for param in iter_drawable(chunk, include_hidden=True):
        if list_row_of(param) is not None:
            # A row param is per instance and per row: an ID property, made by
            # sync_to_node, never a class-level RNA one (see the module docstring).
            continue
        prop = property_for(param)
        if prop is not None:
            props[param.name] = prop
        elif PT.BUTTON in param.get_type():
            buttons.append(param.name)
        elif PT.SEPARATOR not in param.get_type():
            skipped.append(param.name)
    return props, buttons, skipped


def iter_drawable(chunk, include_hidden: bool = False, _depth: int = 0):
    """The params of `chunk` that a node body should show, in author order, flattened.

    `include_hidden` widens it to every param the chunk HAS, which is what registration and
    value-mirroring want: a param hidden when the class is built (an ExportUSD output file,
    which appears only once Flatten is ticked) is still a param, and skipping it there left
    it with no RNA property at all -- so the moment it unhid, the node drew a port with a
    name and no control. Drawing still filters hidden out; only these two callers do not.

    `get_my_params`, not `get_params`: the latter walks the nested ChainMap and would reach
    into params this chunk does not own.

    Containers are **recursed into, not skipped**. Anything without `get_type` is a Group,
    ListGroup or Vector rather than a value, and Blender has no nested-panel form inside a
    node body -- but their children are the actual params. Transform keeps tx/ty/tz in a
    Vector, so skipping containers outright produced a node with ports for tz and no
    control for it, and editing the node wrote nowhere.

    Depth-limited because a malformed chunk can nest containers cyclically, and this runs
    during class registration where a hang looks like Blender freezing on startup.
    """
    if _depth > 4:
        return
    for param in chunk.get_my_params():
        # The container's own bit: a hidden Group hides everything in it, and the
        # params inside carry no bit of their own (see is_hidden).
        if not include_hidden and param.has_state(ChunkFlags.State.HIDDEN):
            continue
        if not hasattr(param, "get_type"):
            if hasattr(param, "get_my_params"):
                yield from iter_drawable(param, include_hidden, _depth + 1)
            continue
        yield param


def sync_to_node(node, chunk) -> None:
    """Copy param values chunk -> node, without triggering the write-back.

    Called when a node is (re)built from USD. Assigning an RNA property fires its `update`
    callback, which would write straight back into the param -- harmless when the values
    already agree, but it cooks the chunk, and rebuilding a graph should not cook anything.
    So the guard is taken for the duration.
    """
    for param in iter_drawable(chunk, include_hidden=True):
        guard = id(param)
        _writing.add(guard)
        try:
            # raw_value, not value: chunk_widget_api.ChunkParamWidgetBase reads raw_value
            # for exactly this (setup_chunk_widget, ATTRIBUTE_CHANGED) so Qt and Flet get
            # it for free by subclassing it -- Blender has no widget objects to subclass
            # (see the module docstring), so this hand-rolled sync has to ask for the same
            # thing by hand. value alone is the fully-resolved result, which for an
            # expression- or connection-driven param is not what belongs in the field: the
            # field should show the "@Node.attr@" text (or connected indication), same as
            # every other frontend, not a blank/resolved value that looks unconnected.
            value = param.raw_value
            if value is None:
                continue
            key = prop_key(param)
            if key == param.name and hasattr(node, key):
                setattr(node, key, value)
            else:
                # This CREATES the ID property for a lofted param or a list row. It used
                # to skip any name the node did not already carry, which meant nothing
                # ever created one: a param lofted after registration (an ExportUSD
                # output file, a Wedge range) got a port on the node and no control under
                # it, because `draw_param` has no property to draw. The two-layer split
                # the module docstring describes only works if this is the layer that
                # seeds layer 2.
                node[key] = value
                # Seen as mirrored, not edited: sync_lofted pushes only a user's change.
                _lofted_seen[(node.as_pointer(), key)] = _plain(node[key])
        except Exception:
            # A value with no ID-property form -- a Gf type, an Sdf.Path. Reported rather
            # than crashing the rebuild, since one odd param should not cost the graph.
            logger.debug("could not mirror %s onto the node", param.name)
        finally:
            _writing.discard(guard)


def sync_lofted(node, chunk) -> None:
    """Push lofted ID-property values node -> param.

    ID properties take no `update` callback, so a lofted param has no way to hear that the
    user typed in it. Draw is the only moment Blender reliably gives us, which makes this
    poll-on-draw rather than event-driven -- acceptable because it only touches params
    whose value actually differs.

    Only keys the node class does not declare. Blender stores a declared property's value
    under the same ID-property key once it is set -- an EnumProperty as its item's INDEX --
    so reading those back here wrote `0` into a Menu on every draw. That never matched the
    menu's text, so each redraw changed a param and cooked: panning a network with a
    material menu crawled. Declared properties sync through their own update callback.

    And only what the user changed. The node's ID property is a copy taken when the node
    was mirrored, and nothing refreshes it when the param moves on its own -- a wire, an
    expression, a cook. Pushing every difference wrote that stale copy back over the live
    value on every draw (a Material Library's wired prim path reset to ''), and a colour
    -- an IDPropertyArray, never equal to the param's tuple -- was rewritten and recooked
    every frame. So a key is pushed when the node's value differs from what this last
    saw, which only a user edit does.
    """
    declared = node.bl_rna.properties
    for key in list(node.keys()):
        if key.startswith("_") or key in _RESERVED or key in declared:
            continue
        value = _plain(node[key])
        seen_key = (node.as_pointer(), key)
        seen = _lofted_seen.get(seen_key, _UNSEEN)
        _lofted_seen[seen_key] = value
        if seen is _UNSEEN or seen == value:
            continue
        param = resolve_key(chunk, key)
        if param is not None:
            _write_param(param, value)


#: (node pointer, key) -> the lofted ID property's value as sync_lofted last saw it.
_lofted_seen: dict = {}
_UNSEEN = object()


def _plain(value):
    """An ID property value as the param holds it: an IDPropertyArray becomes a tuple."""
    to_list = getattr(value, "to_list", None)
    return tuple(to_list()) if to_list is not None else value


#: Node keys that are the frontend's own bookkeeping rather than a lofted param.
_RESERVED = frozenset({"chunk_path", "chunk_type"})


def draw_param(node, layout, param, text: str | None = None) -> bool:
    """Draw one param's control into `layout`. True if anything was drawn.

    Split out of `draw_params` because a param is drawn from two places now: the node body,
    and -- for a param that also has an input socket -- the socket's own row, so the port
    sits beside the control the way it does in Qt. Both need the same declared-property vs
    lofted-ID-property fallback, and having it in one place is what keeps a lofted param
    drawable on a socket at all.
    """
    # OUTPUT is computed, so it is shown but not editable. active=False greys it the way
    # Blender greys any driven value, which reads correctly to a Blender user.
    layout.active = not param.has_flag(_OUTPUT)
    # A value overriding one composed in from another file (a sequence's network hosted
    # here) is marked, the way Qt gives it an amber label: the field shows a value nobody
    # on this graph typed, and "what did we change" is the question a shot asks of a
    # sequence. Blender's alert red is the nearest built-in "this is not the default".
    override_source = chunk_stage.param_override_source(param)
    layout.alert = override_source is not None
    # A PRIM_SELECTION field gets a pin button beside it, same as Qt's -- pulling in
    # whatever the viewport-click picker last found (chunk_blender_nodegraph.pin_prim /
    # set_picked_prim_paths), which is this frontend's counterpart of the Qt/usdview
    # viewport-selection pin.
    pinnable = param.has_flag(ChunkFlags.ParamFlags.PRIM_SELECTION)
    # A long menu gets a search button: Blender's dropdown cannot be typed into, and
    # scrolling hundreds of entries (a material library) is no way to find one.
    searchable = len(getattr(param, "menu_items", None) or ()) > SEARCH_FROM
    row = layout.row(align=True) if pinnable or searchable else layout
    drawn = False
    key = prop_key(param)
    if key == param.name and hasattr(node, key) and getattr(param, "item_images", None) is not None:
        # An ImageMenu: no dropdown. The picture of the current pick is the control --
        # clicking it opens a grid of every item's picture (the one large picture Blender
        # can make clickable) -- with the pick's name and the search beside it underneath.
        # A column of its own: the body may hand over a row, which put the label, the
        # picture and the name side by side and the picture shrank to a sliver.
        col = layout.column()
        col.label(text=(param.label or param.name) if text is None else text)
        col.template_icon_view(node, key, show_labels=True, scale=6.0, scale_popup=5.0)
        row = col.row(align=True)
        row.label(text=param.value or "Nothing picked")
        drawn = True
    elif key == param.name and hasattr(node, key):
        # No `text=` at all when the caller has no opinion: a declared property already
        # carries the param's label as its RNA name, and passing text="" would blank it.
        if text is None:
            row.prop(node, key)
        else:
            row.prop(node, key, text=text)
        drawn = True
    elif key in node.keys():
        # An ID property has no name of its own, so a label always has to be supplied.
        row.prop(node, f'["{key}"]',
                 text=(param.label or param.name) if text is None else text)
        drawn = True
    if drawn and searchable:
        op = row.operator("usd_chunks.search_menu", text="", icon="VIEWZOOM")
        op.tree_name = node.id_data.name
        op.node_path = node.name
        op.param_name = key
    if drawn and pinnable:
        op = row.operator("usd_chunks.pin_prim", text="", icon="EYEDROPPER")
        op.node_path = node.name
        op.param_name = key
    if drawn and override_source is not None:
        # Revert, beside the field: the override's only other exit is retyping the
        # sequence's value, which needs knowing it.
        op = row.operator("usd_chunks.revert_override", text="", icon="LOOP_BACK")
        op.node_path = node.name
        op.param_name = key
    return drawn


def draw_params(node, layout, chunk, skip=()) -> None:
    """Draw one chunk's params into a node body.

    Buttons become operators carrying (node, param) rather than a bound callback: Blender
    dispatches a click to an operator class by name, so there is nowhere to hang a closure.

    `skip` names params drawn elsewhere -- in practice, on their own input socket. Blender
    draws outputs, then this, then inputs, and a socket can draw its param inline; a param
    left in both places would appear twice.
    """
    if chunk is None:
        layout.label(text="<no chunk>", icon="ERROR")
        return

    sync_lofted(node, chunk)
    _draw_container(node, layout, chunk, skip)

    # Lofted params the chunk gained after this class was registered. Drawn after the
    # declared ones because that is the order they were added in.
    declared = {prop_key(p) for p in iter_drawable(chunk, include_hidden=True)}
    for key in node.keys():
        if key in _RESERVED or key in declared or key.startswith("_"):
            continue
        layout.prop(node, f'["{key}"]', text=key)


def _draw_container(node, layout, container, skip, _depth: int = 0) -> None:
    """Draw a container's params in author order, recursing into groups.

    A ListGroup is the one container with a shape of its own: a header with the row
    count and +/- (Qt's list has the same pair), then one box per row. Everything else
    flattens -- a plain Group is a hidden/shown unit here, not a visual one, since a
    node body cannot nest panels and most of its params draw on their sockets anyway.
    """
    if _depth > 4:
        return
    for param in container.get_my_params():
        if param.has_state(ChunkFlags.State.HIDDEN):
            continue
        if isinstance(param, core.ChunkListGroupBase):
            _draw_list(node, layout, param, skip, _depth)
            continue
        if not hasattr(param, "get_type"):
            if hasattr(param, "get_my_params"):
                _draw_container(node, layout, param, skip, _depth + 1)
            continue
        t = param.get_type()
        if PT.SEPARATOR in t:
            layout.separator()
            continue
        if PT.BUTTON in t:
            op = layout.operator("usd_chunks.param_button", text=param.label or param.name)
            op.node_path = node.name
            op.param_name = param.name
            continue
        if prop_key(param) in skip:
            continue
        draw_param(node, layout.row(), param, text=None)


def _draw_list(node, layout, list_group, skip, _depth: int) -> None:
    header = layout.row(align=True)
    header.label(text=f"{list_group.label or list_group.name} ({list_group.size})")
    for delta, icon in ((-1, "REMOVE"), (1, "ADD")):
        op = header.operator("usd_chunks.list_resize", text="", icon=icon)
        op.node_path = node.name
        op.list_name = list_group.name
        op.delta = delta
    for row in list_group.get_my_params():
        box = layout.box()
        _draw_container(node, box, row, skip, _depth + 1)


class BlenderParamWidget(chunk_widget_api.ChunkParamWidgetBase):
    """The contract object for one param, for code that expects a widget to exist.

    Blender draws from RNA rather than from widget instances, so this holds no control and
    exists so `ChunkWidgetFactoryBase` has something to map to and so a chunk asking a
    param for its widget gets a real answer. `get_value`/`set_value` proxy the node.
    """

    def __init__(self, param, node=None, *args, **kwargs):
        super().__init__(param, *args, **kwargs)
        self._node = node

    def get_value(self):
        if self._node is None:
            return self.param.raw_value
        name = self.param.name
        if hasattr(self._node, name):
            return getattr(self._node, name)
        return self._node.get(name, self.param.raw_value)

    def set_value(self, value):
        """Only ever touches the node. Writing to the param here would be re-entered by
        the notice that write produces -- the same loop `chunk_flet` documents.
        """
        if self._node is None:
            return
        name = self.param.name
        if hasattr(self._node, name):
            setattr(self._node, name, value)
        else:
            self._node[name] = value
