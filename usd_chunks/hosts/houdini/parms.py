"""Chunk params as native Houdini parms -- the Houdini counterpart of a frontend's factory.

A chunk node's interface is built from its chunk's params once, when the node is created,
and stored on the node as spare parms; it is saved with the .hip like any other parm. Houdini
owns the values from then on: the parms are the source of truth and every cook pushes them
into the chunk (push). What a cook changes on the chunk itself -- an OUTPUT param, a param
a callback set, a group a menu hid -- comes back to the parms afterwards (pull).

Both directions walk the chunk with one function, entries(), so a parm's name is always
the same for the same param. Names are the params' own where they are unique on the node,
which keeps channel references readable (``ch("../mesh1/radius")``); a clash gets the
owning group's name in front.

Mapping:

- Slider/Spinbox -> Int or Float parm (is_int decides; a Slider declares both types).
- Vector of numbers -> one tuple parm (Translate as one x/y/z row, like Houdini's own).
- ColorPicker -> RGB colour parm. Checkbox -> toggle. Button -> button.
- Menu -> string parm with a menu whose items are asked from the chunk when it opens, so a
  menu filled at runtime (layers, schemas) is live.
- LineEdit -> string; with PRIM_SELECTION it gets Solaris's prim picker.
- FileBrowser -> file parm. TextEdit -> multi-line string. Label/Separator -> the same.
- Group -> folder (collapsible when the group is). ListGroup -> multiparm block.
- Matrix and Custom have no Houdini form and are left out (logged).
"""
from __future__ import annotations

import logging
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass

import hou

from usd_chunks import chunk_core as core
from usd_chunks import chunk_widget_api
from usd_chunks.chunk_flags import ChunkFlags

logger = logging.getLogger(__name__)

PT = ChunkFlags.ParamType

#: The node type's own parm naming the chunk module. Never a chunk param's name.
TYPE_PARM = "usdchunks_type"
#: The node type's own parm holding the variant's menu path ("Materials/standard_surface/Gold"), or "".
VARIANT_PARM = "usdchunks_variant"
#: The node type's own parms: every other parm on a chunk node is one of its chunk's params.
TYPE_PARMS = (TYPE_PARM, VARIANT_PARM)

_LOP = "from usd_chunks.hosts.houdini import lop\n"
#: Menu items come from the chunk each time the menu opens.
MENU_SCRIPT = _LOP + "return lop.menu_items(kwargs['node'], kwargs['parm'].name())"
#: A Button parm clicks its chunk Button.
BUTTON_SCRIPT = "from usd_chunks.hosts.houdini import lop; lop.press_button(kwargs)"

#: What Solaris's own prim path parms carry: the arrow button that picks in the viewport.
PRIM_PICKER_TAGS = {
    "script_action": "import loputils\nloputils.selectPrimsInParm(kwargs, False)",
    "script_action_help": "Select a primitive in the Scene Viewer or Scene Graph Tree pane.",
    "script_action_icon": "BUTTONS_reselect",
    "sidefx::usdpathtype": "prim",
}


@dataclass
class Entry:
    """One parm (tuple) and the chunk object behind it.

    kind is "value" (one param), "vector" (a tuple parm for a Vector), "button", "folder"
    (a Group), "list" (a ListGroup's multiparm count), "label" (a Label: text a cook may
    rewrite, like a House's readout) or "static" (a Separator).
    """

    name: str
    param: object
    kind: str


def _names() -> dict:
    return {"used": set(TYPE_PARMS)}


def _unique(base: str, scope: str, names: dict) -> str:
    """base, or scope_base when another param on the node already has base.

    Made a valid Houdini parm name first: a chunk param's name need not be one (a
    Separator's often is not).
    """
    base = re.sub(r"\W", "_", str(base))
    if not base or base[0].isdigit():
        base = f"p_{base}"
    name = base
    if name in names["used"]:
        name = f"{scope}_{base}" if scope else base
        n = 2
        while name in names["used"]:
            name = f"{scope}_{base}{n}"
            n += 1
    names["used"].add(name)
    return name


def _kind(param) -> str | None:
    if isinstance(param, core.ProxyParam) and not hasattr(param, "get_type"):
        return None
    if isinstance(param, core.ChunkListGroupBase):
        return "list"
    if isinstance(param, core.ChunkVectorBase):
        return "vector"
    if isinstance(param, core.ChunkBase):
        return None  # a chunk inside a chunk is its own node, not a parm
    if isinstance(param, core.ChunkGroupBase):
        return "folder"
    if not hasattr(param, "get_type"):
        return None
    t = param.get_type()
    if PT.BUTTON in t:
        return "button"
    if PT.SEPARATOR in t:
        return "static"
    if PT.LABEL in t:
        return "label"
    if t == PT.NONE or PT.MATRIX in t or PT.CUSTOM in t:
        logger.debug("no Houdini parm for %s (type %s)", param.name, t)
        return None
    return "value"


def entries(container, names: dict | None = None, scope: str = "", rows: bool = True) -> Iterator[Entry]:
    """Every parm a chunk has, in author order, with the name it has on the node.

    A list's rows follow its "list" entry, named ``<list>_<param><row+1>`` as Houdini names
    multiparm instances. The rows are read when the walk reaches them, so a caller that
    resizes the list on its "list" entry (push) walks the rows the new size made. rows=False
    leaves rows out: building the interface describes a list by its template instead.
    """
    names = names if names is not None else _names()
    for param in container.get_my_params():
        kind = _kind(param)
        if kind is None:
            continue
        name = _unique(param.name, scope, names)
        yield Entry(name, param, kind)
        if kind == "folder":
            yield from entries(param, names, param.name, rows)
        elif kind == "list":
            templates = [(p, _unique(f"{name}_{p.name}", "", names)) for p in _list_templates(param)]
            if not rows:
                continue
            for i, row in enumerate(param.get_my_params()):
                for template, base in templates:
                    proxy = row.get_param(template.name)
                    proxy_kind = _kind(proxy) if proxy is not None else None
                    if proxy_kind in ("value", "vector", "button"):
                        yield Entry(f"{base}{i + 1}", proxy, proxy_kind)


def _list_templates(list_group) -> list:
    return [p for p in list_group.children.get_my_params() if _kind(p) in ("value", "vector", "button")]


def param_path(chunk, param) -> str:
    """param's dotted path below chunk ("footprint_grp.floors"), as chunk.get_param and a
    saved network's ``inputs:footprint_grp:floors`` name it.

    The owner chain alternates chunk objects with the containers holding them; only the
    chunk objects are named in the path.
    """
    names = []
    obj = param
    while obj is not None and obj is not chunk:
        if isinstance(obj, core.ChunkCommon):
            names.append(obj.name)
        obj = obj.get_owner()
    return ".".join(reversed(names))


def find(chunk, parm_name: str):
    """The chunk object behind a parm on the node, or None."""
    for e in entries(chunk):
        if e.name == parm_name:
            return e.param
    return None


# --------------------------------------------------------------------------- templates


def _label(param) -> str:
    try:
        return param.get_label() or param.name
    except AttributeError:
        return param.name


def _help(param) -> str:
    return getattr(param, "tooltip", "") or ""


def _range(param, default_lo: float, default_hi: float) -> dict:
    lo = getattr(param, "min_value", None)
    hi = getattr(param, "max_value", None)
    lo = default_lo if lo is None else lo
    hi = (max(lo, 0) + 10 if hi is None else hi)
    return {"min": lo, "max": hi, "min_is_strict": False, "max_is_strict": False}


def _default(param):
    value = getattr(param, "default_value", None)
    return param.value if value is None else value


def _is_int(param) -> bool:
    t = param.get_type()
    if PT.INT in t and PT.FLOAT in t:
        return bool(getattr(param, "is_int", False))
    return PT.INT in t


def _value_template(name: str, param, label: str | None = None):
    t = param.get_type()
    label = label if label is not None else _label(param)
    help = _help(param)
    default = _default(param)

    if PT.BUTTON in t:
        return hou.ButtonParmTemplate(
            name, label, script_callback=BUTTON_SCRIPT,
            script_callback_language=hou.scriptLanguage.Python, help=help)
    if PT.SEPARATOR in t:
        return hou.SeparatorParmTemplate(name)
    if PT.LABEL in t:
        return hou.LabelParmTemplate(name, label, column_labels=(str(param.value or ""),), help=help)
    if PT.COLOR_PICKER in t:
        rgb = tuple(float(v) for v in (default or (0.5, 0.5, 0.5)))[:3]
        return hou.FloatParmTemplate(
            name, label, 3, default_value=rgb, min=0.0, max=1.0,
            look=hou.parmLook.ColorSquare, naming_scheme=hou.parmNamingScheme.RGBA, help=help)
    if PT.STRING_MENU in t:
        items = [str(i) for i in (getattr(param, "menu_items", None) or [])]
        return hou.StringParmTemplate(
            name, label, 1, default_value=(str(default if default is not None else ""),),
            menu_items=items, menu_labels=items, menu_type=hou.menuType.Normal,
            item_generator_script=MENU_SCRIPT,
            item_generator_script_language=hou.scriptLanguage.Python, help=help)
    # BOOL before INT: bool is an int, and a checkbox must not become a number field.
    if PT.BOOL in t:
        return hou.ToggleParmTemplate(name, label, default_value=bool(default), help=help)
    if PT.INT in t or PT.FLOAT in t:
        if _is_int(param):
            return hou.IntParmTemplate(
                name, label, 1, default_value=(int(default or 0),), help=help, **_range(param, 0, 10))
        return hou.FloatParmTemplate(
            name, label, 1, default_value=(float(default or 0.0),), help=help, **_range(param, 0.0, 10.0))
    if PT.FILE in t:
        spec = chunk_widget_api.file_dialog_spec(param)
        tags = {"filechooser_mode": "read" if spec.must_exist else "read_and_write"}
        if spec.patterns and spec.patterns != ("*",):
            tags["filechooser_pattern"] = " ".join(spec.patterns)
        return hou.StringParmTemplate(
            name, label, 1, default_value=(str(default or ""),),
            string_type=hou.stringParmType.FileReference,
            file_type=hou.fileType.Directory if spec.directory else hou.fileType.Any,
            tags=tags, help=help)
    if PT.TEXTEDIT in t:
        return hou.StringParmTemplate(
            name, label, 1, default_value=(str(default or ""),),
            tags={"editor": "1", "editorlines": "6-30"}, help=help)
    if PT.STRING in t:
        tags = dict(PRIM_PICKER_TAGS) if param.has_flag(ChunkFlags.ParamFlags.PRIM_SELECTION) else {}
        return hou.StringParmTemplate(
            name, label, 1, default_value=(str(default or ""),), tags=tags, help=help)
    return None


def _vector_template(name: str, vector):
    children = [c for c in vector.get_my_params() if hasattr(c, "get_type")]
    if not children:
        return None
    first = children[0]
    if PT.INT not in first.get_type() and PT.FLOAT not in first.get_type():
        return None
    size = len(children)
    scheme = hou.parmNamingScheme.XYZW if size <= 4 else hou.parmNamingScheme.Base1
    defaults = tuple(_default(c) or 0 for c in children)
    if _is_int(first):
        return hou.IntParmTemplate(
            name, _label(vector), size, default_value=tuple(int(v) for v in defaults),
            naming_scheme=scheme, help=_help(vector), **_range(first, 0, 10))
    return hou.FloatParmTemplate(
        name, _label(vector), size, default_value=tuple(float(v) for v in defaults),
        naming_scheme=scheme, help=_help(vector), **_range(first, 0.0, 10.0))


def _folder_type(group):
    if group.has_flag(ChunkFlags.GroupFlags.COLLAPSABLE):
        return hou.folderType.Collapsible
    return hou.folderType.Simple


def templates(container, names: dict | None = None, scope: str = "") -> list:
    """The parm templates for a chunk's params, nested in folders as its groups are."""
    names = names if names is not None else _names()
    result = []
    for param in container.get_my_params():
        kind = _kind(param)
        if kind is None:
            continue
        name = _unique(param.name, scope, names)
        if kind == "folder":
            folder = hou.FolderParmTemplate(name, _label(param), folder_type=_folder_type(param))
            for child in templates(param, names, param.name):
                folder.addParmTemplate(child)
            result.append(folder)
        elif kind == "list":
            block = hou.FolderParmTemplate(
                name, _label(param), folder_type=hou.folderType.MultiparmBlock,
                default_value=int(getattr(param, "size", 0) or 0))
            for template in _list_templates(param):
                base = _unique(f"{name}_{template.name}", "", names)
                if isinstance(template, core.ChunkVectorBase):
                    child = _vector_template(base + "#", template)
                else:
                    child = _value_template(base + "#", template)
                if child is not None:
                    block.addParmTemplate(child)
            result.append(block)
        elif kind == "vector":
            if (template := _vector_template(name, param)) is not None:
                result.append(template)
        elif (template := _value_template(name, param)) is not None:
            result.append(template)
    return result


def template_names(parm_templates) -> list[str]:
    """Every template name in parm_templates, folders' contents included, in order."""
    names = []
    for template in parm_templates:
        if template.name() in TYPE_PARMS:
            continue
        names.append(template.name())
        if isinstance(template, hou.FolderParmTemplate):
            names.extend(template_names(template.parmTemplates()))
    return names


def interface_matches(node: hou.Node, chunk) -> bool:
    """Whether node's parms are still the chunk's params -- False after its module changed."""
    return template_names(node.parmTemplateGroup().entries()) == template_names(templates(chunk))


def build_interface(node: hou.Node, chunk) -> None:
    """Replace node's spare parms with the chunk's params, keeping values that still fit."""
    old = {}
    for pt in node.spareParms():
        if pt.name() not in TYPE_PARMS and not pt.keyframes():
            old[pt.name()] = pt.eval()

    group = node.parmTemplateGroup()
    for template in list(group.entries()):
        if template.name() not in TYPE_PARMS:
            group.remove(template.name())
    for template in templates(chunk):
        group.append(template)
    node.setParmTemplateGroup(group)

    for name, value in old.items():
        if (parm := node.parm(name)) is not None:
            try:
                parm.set(value)
            except hou.Error:
                pass


# --------------------------------------------------------------------------- values


def _same(a, b) -> bool:
    if isinstance(a, (tuple, list)) or isinstance(b, (tuple, list)):
        try:
            return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
        except TypeError:
            return False
    if isinstance(a, float) or isinstance(b, float):
        try:
            return math.isclose(float(a), float(b), rel_tol=1e-6, abs_tol=1e-9)
        except (TypeError, ValueError):
            return False
    return a == b


def _read(pt: hou.ParmTuple, param, kind: str):
    """The value a parm tuple holds, as the chunk param wants it."""
    if kind == "vector":
        children = [c for c in param.get_my_params() if hasattr(c, "get_type")]
        cast = int if children and _is_int(children[0]) else float
        return tuple(cast(v) for v in pt.eval())
    t = param.get_type()
    if PT.COLOR_PICKER in t:
        return tuple(float(v) for v in pt.eval())
    parm = pt[0]
    if PT.STRING in t:
        return parm.evalAsString()
    if PT.BOOL in t:
        return bool(parm.eval())
    if _is_int(param):
        return int(parm.eval())
    return float(parm.eval())


def _is_output(param) -> bool:
    if isinstance(param, core.ChunkVectorBase):
        return bool(param.is_output)
    return bool(param.has_flag(ChunkFlags.ParamFlags.OUTPUT))


def _is_transient(param) -> bool:
    return bool(param.has_flag(ChunkFlags.ParamFlags.TRANSIENT))


def push(node: hou.Node, chunk) -> None:
    """Parm values -> chunk params, for every parm whose value differs.

    Evaluated during the node's cook, which is also what makes Houdini recook the node
    when any of these parms changes (or a channel reference they hold does). So outputs and
    session state, which the chunk writes and pull only shows, are not read: a Layer's new
    layer id differs every cook, and reading it back would recook the node forever.
    """
    for e in entries(chunk):
        pt = node.parmTuple(e.name)
        if pt is None:
            continue
        if e.kind == "list":
            size = int(pt.eval()[0])
            if size != e.param.size:
                e.param.size = size
            continue
        if e.kind not in ("value", "vector") or _is_output(e.param) or _is_transient(e.param):
            continue
        value = _read(pt, e.param, e.kind)
        if not _same(value, e.param.value):
            e.param.value = value


def pull(node: hou.Node, chunk) -> None:
    """Chunk -> parms: values a cook changed, and each param's hidden/disabled state.

    Never written while the node cooks (Houdini forbids it); lop schedules this after. A
    parm holding an expression or keys is the user's, and is left alone.
    """
    for e in entries(chunk):
        pt = node.parmTuple(e.name)
        if pt is None:
            continue
        param = e.param
        if isinstance(param, core.ChunkCommon) or hasattr(param, "has_state"):
            hidden = bool(param.has_state(ChunkFlags.State.HIDDEN))
            disabled = bool(param.has_state(ChunkFlags.State.DISABLED))
            if e.kind in ("value", "vector") and _is_output(param):
                disabled = True
            if pt.isHidden() != hidden:
                pt.hide(hidden)
            if pt.isDisabled() != disabled:
                pt.disable(disabled)
        if e.kind == "list":
            if int(pt.eval()[0]) != param.size and not pt[0].keyframes():
                pt.set((param.size,))
            continue
        if e.kind == "label":
            # A Label parm shows its value, so the text a cook wrote is set like any other.
            text = "" if param.value is None else str(param.value)
            if pt[0].evalAsString() != text:
                pt[0].set(text)
            continue
        if e.kind not in ("value", "vector"):
            continue
        if any(p.keyframes() for p in pt):
            continue
        value = param.value
        if value is None or _same(_read(pt, param, e.kind), value):
            continue
        try:
            pt.set(tuple(value) if e.kind == "vector" or PT.COLOR_PICKER in param.get_type() else (value,))
        except (hou.Error, TypeError) as exc:
            logger.debug("could not show %s on %s: %s", e.name, node.path(), exc)
