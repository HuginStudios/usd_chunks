"""Property Editor chunk -- pick prims and override their attributes.

The chunk keeps one list of edits: attribute name -> value. It is not tied to a prim. Every
cook writes the list onto every prim the Prim param names, into the chunk's own layer. So:

- pick several prims and one edit lands on all of them;
- duplicate the chunk, change its Prim, and the copy makes the same edits on the new prims;
- change Prim and the old prims lose the edits, since the cook starts from an empty layer.

The widgets are only a view of that list. They show the first target prim's attributes,
with the edited value where there is one. They are session state (TRANSIENT): nothing about
them is saved or copied. The list is saved and copied, as the "edits" text param.

An edit can also make an attribute the prim does not have yet: put its type before the name,
as in a .usda file (`bool vray:env:bg:tex:use = true`). Without a type, a prim that lacks the
attribute is skipped.
"""
from __future__ import annotations

import logging

from pxr import Sdf, Tf, Usd

from usd_chunks import chunk, chunk_utils

# Parented under "usd_chunks": a chunk module is imported under its own file name.
logger = logging.getLogger("usd_chunks.property_editor")

_TRANSIENT = chunk.ChunkFlags.ParamFlags.TRANSIENT
_LABELED = chunk.ChunkFlags.ParamFlags.LABELED

# user_data keys on each widget: the full property name it edits, and the value the widget
# last showed. A callback whose widget still shows that value is not an edit (a collapse, a
# label change, the sync itself).
_PROP = "property_editor.prop"
_SHOWN = "property_editor.shown"

# Chunks whose widgets are being set by _sync_widgets, not by a person.
_syncing: set[int] = set()

# Types usd_type_map does not cover, but which are common on real prims.
_EXTRA_TYPES = {
    "double": (chunk.Slider, {}),
    "half": (chunk.Slider, {}),
}
# An array edited as its single element. Only offered while the array holds at most one.
_SINGLE_ELEMENT_ARRAYS = {
    "color3f[]": (chunk.Vector, {"param": chunk.Slider(min_value=0.0, max_value=1.0), "suffixes": "rgb", "length": 3}),
}


# ---------------------------------------------------------------------------
# The edit list
# ---------------------------------------------------------------------------


def _jsonable(value):
    """value as plain JSON data: tuples and Gf vectors become lists, asset paths strings."""
    if isinstance(value, Sdf.AssetPath):
        return value.path
    if isinstance(value, (str, bool, int, float)) or value is None:
        return value
    try:
        return [_jsonable(v) for v in value]
    except TypeError:
        return str(value)


def _parse_edits(node) -> tuple[dict, dict]:
    """The chunk's edits and the types written before their names.

    One line per edit, `name = <json value>`, or `type name = <json value>` to create the
    attribute where a prim lacks it. A line that does not parse is skipped, so a typo in the
    text drops that edit rather than all of them.

    Returns:
        ({name: value}, {name: type name}), in the order the edits were made.
    """
    import json

    edits, types = {}, {}
    for line in (node.get_param("edits").value or "").splitlines():
        head, sep, raw = line.partition("=")
        words = head.split()
        if not sep or len(words) not in (1, 2):
            continue
        name = words[-1]
        try:
            edits[name] = json.loads(raw.strip())
        except ValueError:
            logger.warning("Property Editor %s: skipped an edit it could not read: %r", node.name, line)
            continue
        if len(words) == 2:
            types[name] = words[0]
    return edits, types


def read_edits(node) -> dict:
    """The chunk's edits, {property name: value}, in the order they were made."""
    return _parse_edits(node)[0]


def write_edits(node, edits: dict) -> None:
    """Store the edits on the chunk, keeping any type written before a name. Setting the
    text cooks the chunk."""
    import json

    types = _parse_edits(node)[1]
    text = "\n".join(
        f"{types[name] + ' ' if name in types else ''}{name} = {json.dumps(value)}" for name, value in edits.items()
    )
    param = node.get_param("edits")
    if param.value != text:
        param.value = text


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------


def _read_properties(stage, prim_param) -> list[tuple]:
    """(name, type, value, allowed tokens, limits) for every property on the target prims.

    The union over all targets, first prim first. A property on several prims shows the
    first one's value. Read inside each path's variant, so /asset{color=red}/geo shows red.
    A relationship's type is "rel" and its value the comma-joined targets.
    """
    found: dict[str, tuple] = {}
    for path in chunk_utils.author_in_variant_contexts(stage, prim_param):
        prim = stage.GetPrimAtPath(path)
        if not prim.IsValid():
            continue
        for attr in prim.GetAttributes():
            name = attr.GetName()
            if name in found:
                continue
            type_name = str(attr.GetTypeName())
            value = attr.Get(0)
            if type_name in _SINGLE_ELEMENT_ARRAYS:
                if value is not None and len(value) > 1:
                    continue
                value = tuple(value[0]) if value else None
            limits = attr.GetMetadata("limits") or {}
            found[name] = (name, type_name, _jsonable(value),
                           tuple(attr.GetMetadata("allowedTokens") or ()), limits.get("hard") or {})
        for rel in prim.GetRelationships():
            name = rel.GetName()
            if name not in found:
                targets = ",".join(str(t) for t in rel.GetTargets())
                found[name] = (name, "rel", targets, (), {})
    return list(found.values())


def _make_widget(name, type_name, value, tokens, hard_limits):
    """An unbound, TRANSIENT param for one property, or None for a type with no widget."""
    label = name.split(":")[-1]
    kwargs = {"name": label, "label": label}
    if type_name == "rel":
        return chunk.LineEdit(**kwargs, default_value=value or "", flags=_LABELED | _TRANSIENT)
    if type_name == "token" and not tokens:
        # A free-form token (info:id, a shader's token input): a menu of one could not be changed.
        return chunk.LineEdit(**kwargs, default_value=value or "", flags=_LABELED | _TRANSIENT)

    entry = chunk.usd_type_map.get(type_name) or _EXTRA_TYPES.get(type_name) or _SINGLE_ELEMENT_ARRAYS.get(type_name)
    if entry is None:
        return None
    cls, extra = entry if isinstance(entry, tuple) else (entry, {})
    kwargs.update(extra)

    if issubclass(cls, chunk.Vector):
        length = extra["length"]
        if value is None:
            value = [0.5] * length if type_name.startswith("color") else [0.0] * length
        widget = cls(**kwargs, default_value=tuple(value))
        for part in widget.get_my_params():
            part.set_flag(_TRANSIENT)
        return widget

    if cls is chunk.Menu:
        items = [str(t) for t in tokens]
        if value is not None and value not in items:
            items.insert(0, value)
        widget = cls(**kwargs, menu_items=items, default_value=value if value is not None else items[0])
    elif cls in (chunk.Slider, chunk.Spinbox):
        if value is None:
            value = 0 if extra.get("is_int") else 0.0
        widget = cls(**kwargs, default_value=value,
                     min_value=hard_limits.get("minimum"), max_value=hard_limits.get("maximum"))
    elif cls is chunk.Checkbox:
        widget = cls(**kwargs, default_value=bool(value))
    else:
        widget = cls(**kwargs, default_value=value if value is not None else "")
    widget.set_flag(_LABELED | _TRANSIENT)
    return widget


def _signature(node, props) -> tuple:
    """What the widgets are built from. The same signature builds the same widgets."""
    return (node.get_param("prim").value, tuple((p[0], p[1], p[3]) for p in props))


def _iter_widgets(dyn_grp):
    for _, param in dyn_grp.iter_hierarchy():
        if _PROP in getattr(param, "user_data", {}):
            yield param


def _sync_widgets(node) -> None:
    """Make the widgets match the target prims and the edit list.

    Rebuilds them only when the targets' properties changed. Otherwise just sets each one to
    its edited value, or the prim's own value if it has no edit.
    """
    dyn_grp = node.get_param("dyn_grp", chunk.is_group)
    stage = node.get_stage()
    if not stage:
        return
    try:
        props = _read_properties(stage, node.get_param("prim"))
    except RuntimeError:
        # A prim can expire mid-read when a cook this triggers recomposes the stage. Keep
        # the widgets as they are; the next cook reads again.
        logger.debug("Property Editor %s: target changed while being read", node.name)
        return

    _syncing.add(id(node))
    try:
        signature = _signature(node, props)
        if signature != getattr(dyn_grp, "_pe_signature", None):
            _rebuild(dyn_grp, props)
            dyn_grp._pe_signature = signature

        edits = read_edits(node)
        values = {p[0]: p[2] for p in props}
        for widget in _iter_widgets(dyn_grp):
            prop = widget.user_data[_PROP]
            shown = edits.get(prop, values.get(prop))
            widget.user_data[_SHOWN] = shown
            if shown is None or _jsonable(widget.value) == shown:
                continue
            try:
                widget.value = tuple(shown) if isinstance(shown, list) else shown
            except Exception:
                logger.debug("Property Editor %s: could not show %s = %r", node.name, prop, shown)
    finally:
        _syncing.discard(id(node))


def _rebuild(dyn_grp, props) -> None:
    """Replace the widgets. Built first, installed after, so a failed read leaves the old ones."""
    built = []
    for name, type_name, value, tokens, limits in props:
        widget = _make_widget(name, type_name, value, tokens, limits)
        if widget is None:
            continue
        # execute() reads the edit list, never a widget. The widget's callback writes the
        # list, and that change is the one cook; without this, a vector edit cooked twice.
        for part in (widget, *widget.get_my_params()) if isinstance(widget, chunk.Vector) else (widget,):
            part.presentation_only = True
        widget.user_data[_PROP] = name
        widget.user_data[_SHOWN] = value
        widget.add_callback(_on_widget_changed, private=True)
        namespace = name.rpartition(":")[0]
        built.append((namespace, widget))

    for grp in dyn_grp.get_my_params():
        grp.request_delete()

    groups = {}
    for namespace, widget in built:
        target = groups.get(namespace) if namespace else dyn_grp
        if target is None:
            target = chunk.Group(
                namespace,
                flags=chunk.ChunkFlags.GroupFlags.COLLAPSABLE | chunk.ChunkFlags.GroupFlags.LABELED,
            )
            if namespace != "inputs":
                target.set_state(chunk.ChunkFlags.State.COLLAPSED)
            dyn_grp.add_param(target)
            groups[namespace] = target
        target.add_param(widget)


def _on_widget_changed(owner, notice, sender):
    """A widget changed: record its value as an edit."""
    node = owner.get_chunk()
    if node is None or id(node) in _syncing:
        return
    prop = owner.user_data.get(_PROP)
    value = _jsonable(owner.value)
    edits = read_edits(node)
    if prop is None or edits.get(prop, owner.user_data.get(_SHOWN)) == value:
        return
    edits[prop] = value
    owner.user_data[_SHOWN] = value
    write_edits(node, edits)


def _on_targets_changed(owner, notice, sender):
    """Prim or edits changed: show it now, even if the chunk is not cooking."""
    if node := owner.get_chunk():
        _sync_widgets(node)


def _clear_edits(owner, notice, sender):
    if node := owner.get_chunk():
        write_edits(node, {})


# ---------------------------------------------------------------------------
# Cook
# ---------------------------------------------------------------------------


def _as_usd_value(attr: Usd.Attribute, value):
    """A JSON edit value in a form attr.Set() takes."""
    type_name = attr.GetTypeName()
    if type_name.isArray:
        if value and not isinstance(value[0], list):
            value = [value]  # a single element, see _SINGLE_ELEMENT_ARRAYS
        return type_name.type.pythonClass([tuple(v) if isinstance(v, list) else v for v in value])
    if isinstance(value, list):
        return chunk_utils.as_attr_value(attr, tuple(value))
    if isinstance(value, float) and type_name.type.pythonClass is int:
        return int(round(value))
    return value


def _apply(prim: Usd.Prim, name: str, value, type_name: str | None = None) -> None:
    """Author one edit on one prim. A missing attribute is created when type_name is given,
    else the prim is skipped."""
    if rel := prim.GetRelationship(name):
        rel.SetTargets([Sdf.Path(p.strip()) for p in str(value).split(",") if p.strip()])
        return
    attr = prim.GetAttribute(name)
    if not attr and type_name:
        value_type = Sdf.ValueTypeNames.Find(type_name)
        if not value_type:
            logger.warning("Property Editor: unknown type %r for %s", type_name, name)
            return
        attr = prim.CreateAttribute(name, value_type, custom=True)
    if not attr:
        return  # this prim has no such attribute; the edit is for the others
    try:
        attr.Set(_as_usd_value(attr, value))
    except (Tf.ErrorException, TypeError, ValueError) as exc:
        logger.warning("Property Editor: could not set %s to %r: %s", attr.GetPath(), value, exc)


def execute(node: chunk.Chunk) -> None:
    """Write every edit onto every target prim. The framework has already cleared the layer."""
    _sync_widgets(node)
    stage = node.get_stage()
    edits, types = _parse_edits(node)
    if not stage or not edits:
        return
    for path in chunk_utils.author_in_variant_contexts(stage, node.get_param("prim")):
        prim = stage.GetPrimAtPath(path)
        if not prim.IsValid():
            continue
        for name, value in edits.items():
            _apply(prim, name, value, types.get(name))


def main():
    """Build the Property Editor: a Prim param, the edit list, and the widgets."""
    myChunk = chunk.Chunk("Property Editor")

    primSel = chunk.LineEdit(
        "prim",
        "Prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="The prims to edit. Every edit applies to all of them.",
    )
    myChunk.add_param(primSel)

    edits_grp = chunk.Group(
        "edits_grp",
        "Edits",
        flags=chunk.ChunkFlags.GroupFlags.COLLAPSABLE | chunk.ChunkFlags.GroupFlags.LABELED,
    )
    edits_grp.set_state(chunk.ChunkFlags.State.COLLAPSED)
    edits = chunk.TextEdit(
        "edits",
        "Edits",
        default_value="",
        tooltip="One edit per line, `name = value`. Put a type first, `float name = value`, to "
        "create the attribute where a prim lacks it. Delete a line to drop that edit.",
    )
    edits_grp.add_param(edits)
    clear = chunk.Button("clear_edits", "Clear Edits", tooltip="Drop every edit.")
    edits_grp.add_param(clear)
    myChunk.add_param(edits_grp)

    dynGroup = chunk.Group("dyn_grp", flags=chunk.ChunkFlags.GroupFlags.FRAMED)
    myChunk.add_param(dynGroup)

    primSel.add_callback(_on_targets_changed, send_on_create=True)
    edits.add_callback(_on_targets_changed)
    clear.add_callback(_clear_edits)

    return myChunk


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.PROPERTIES,
        icon="tag",
        description="""
        Property Editor -- pick prims and override their attributes.

        Parameters:
          - Prim: the prims to edit (viewport picking, globs, and a variant selection in
            the path, e.g. /asset{color=red}/geo). Every edit applies to all of them.
          - Edits: the list of edits, one `name = value` per line. Delete a line to drop
            that edit, or press Clear Edits to drop them all.

        The widgets below show the first prim's attributes. Changing one adds it to the
        edit list. Only edited attributes are written; the rest are left alone.

        The edits are not tied to a prim. Duplicate the chunk and change its Prim, and
        the copy makes the same edits on the new prims. A prim without an edited
        attribute is skipped for that edit.

        In the node graph: accepts a prim input port. Outputs the prim for
        downstream processing.
        """,
    )
