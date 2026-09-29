"""Dear ImGui param widgets -- the third toolkit, and the one that needs the least code.

Immediate mode changes the shape of this layer completely. Qt and Flet both build a widget
OBJECT per param, keep it alive, and register a notice callback so the widget updates when
the param changes underneath it -- `chunk_widget_api.ChunkParamWidgetBase` exists to carry
exactly that. Here there is no object and no callback: every frame reads `param.value` and
draws it, so a change made anywhere is on screen on the next frame by construction.

That means this file does NOT implement ChunkWidgetFactoryBase. It is not an oversight --
the base class's whole contract is `set_value`/`get_value` plus a subscription, and an
immediate-mode frontend has nothing to subscribe. What it shares with the other two is the
thing that actually matters: params are dispatched on `ChunkFlags.ParamType`, so this file
knows nothing about specific chunks.
"""
from __future__ import annotations

import logging

from imgui_bundle import imgui
from imgui_bundle import portable_file_dialogs as pfd

from usd_chunks import chunk_widget_api
from usd_chunks.chunk_flags import ChunkFlags

logger = logging.getLogger(__name__)

#: Rows are this tall, which the graph needs to place a port beside its control. Declared
#: rather than measured for the same reason Flet declares them: the value is needed before
#: the row is drawn.
ROW_H = 22.0
#: Width of the label column. Labels go on the LEFT, in their own column, because ImGui's
#: default puts them to the right of the widget -- which reads as "0.000 X" and looks like
#: the value is labelling the axis rather than the other way round.
LABEL_W = 124.0
#: How much a drag has to move to change a value by one unit.
DRAG_SPEED = 0.01

_GROUP_TITLE = (0.72, 0.72, 0.76, 1.0)

#: The file dialog in flight, if any: {param id: (dialog, param)}.
#:
#: portable_file_dialogs is asynchronous -- result() blocks until the user is done, which
#: in an immediate-mode loop would freeze the graph mid-frame. The dialog is kept here and
#: polled with ready() on subsequent frames instead.
_pending_dialogs: dict = {}


def _browse(param) -> None:
    """Open a file dialog that honours what this param actually asked for."""
    spec = chunk_widget_api.file_dialog_spec(param)
    current = str(param.value) if param.value else ""
    if spec.directory:
        dialog = pfd.select_folder(spec.title, current)
    else:
        # pfd takes filters as description/patterns PAIRS.
        filters = ["Supported", " ".join(spec.patterns), "All files", "*"]
        if spec.must_exist:
            dialog = pfd.open_file(spec.title, current, filters)
        else:
            dialog = pfd.save_file(spec.title, current, filters)
    _pending_dialogs[id(param)] = (dialog, param)


def poll_dialogs() -> bool:
    """Apply any finished file dialog. Call once a frame; True if something changed."""
    changed = False
    for key, (dialog, param) in list(_pending_dialogs.items()):
        if not dialog.ready():
            continue
        del _pending_dialogs[key]
        result = dialog.result()
        # open_file returns a LIST, save_file and select_folder return a string.
        if isinstance(result, (list, tuple)):
            result = result[0] if result else ""
        if result:
            param.value = result
            changed = True
    return changed


def _label(text: str, width: float = LABEL_W) -> None:
    """A left-hand label, vertically centred against the widget it names.

    A label longer than the column pushes the widget right rather than overlapping it:
    same_line's absolute position cannot move something left, so "Import Prim Path" used
    to sit underneath its own field. The max() makes the column grow for that row instead.
    """
    imgui.align_text_to_frame_padding()
    imgui.text(text)
    imgui.same_line(max(width, imgui.calc_text_size(text).x + 8.0))


def _numeric(param, ident: str, width: float) -> bool:
    """A number field. Drag, not slider.

    A slider needs a range, and a transform has none -- the previous version clamped every
    value to +/-100, so typing a translate of 250 silently became 100. drag_float with
    v_min == v_max leaves the value unbounded, which is what these params actually are.
    """
    imgui.set_next_item_width(width)
    if getattr(param, "is_int", False):
        changed, value = imgui.drag_int(ident, int(param.value or 0), 1.0)
    else:
        changed, value = imgui.drag_float(ident, float(param.value or 0.0), DRAG_SPEED,
                                          0.0, 0.0, "%.3f")
    if changed:
        param.value = value
    return changed


def _is_numeric(param) -> bool:
    return param.evaluate_type() in (
        ChunkFlags.ParamType.FLOAT_SLIDER, ChunkFlags.ParamType.INT_SLIDER,
        ChunkFlags.ParamType.FLOAT_SPINBOX, ChunkFlags.ParamType.INT_SPINBOX,
    )


def draw_param(param, width: float = 150.0) -> bool:
    """Draw one param as a labelled row. Returns True if the user changed it.

    The imgui label is always `##something`: the visible text is drawn by _label on the
    left, and the `##` form keeps the widget's identity without printing it again.
    """
    kind = param.evaluate_type()
    name = param.get_label() or param.name
    ident = f"##{param.name}"
    imgui.push_id(str(id(param)))
    changed = False
    try:
        if kind == ChunkFlags.ParamType.SEPARATOR:
            imgui.separator()
        elif kind == ChunkFlags.ParamType.LABEL:
            imgui.text_disabled(str(param.value or name))
        elif kind == ChunkFlags.ParamType.BUTTON:
            if imgui.button(name, imgui.ImVec2(width + LABEL_W * 0.5, 0)):
                # A Button param carries no value; toggling is how the other frontends
                # fire it, so the notice fires the same way here.
                param.value = not bool(param.value)
                changed = True
        elif _is_numeric(param):
            _label(name)
            changed = _numeric(param, ident, width)
        elif kind == ChunkFlags.ParamType.CHECKBOX:
            _label(name)
            changed, value = imgui.checkbox(ident, bool(param.value))
            if changed:
                param.value = value
        elif kind == ChunkFlags.ParamType.STRING_MENU:
            _label(name)
            items = list(getattr(param, "menu_items", None) or [])
            current = str(param.value) if param.value is not None else ""
            index = items.index(current) if current in items else 0
            if items:
                imgui.set_next_item_width(width)
                changed, index = imgui.combo(ident, index, items)
                if changed:
                    param.value = items[index]
        elif kind == ChunkFlags.ParamType.COLOR_PICKER:
            _label(name)
            value = param.value or (0.0, 0.0, 0.0)
            try:
                rgb = [float(value[0]), float(value[1]), float(value[2])]
            except (TypeError, IndexError, ValueError):
                rgb = [0.0, 0.0, 0.0]
            imgui.set_next_item_width(width)
            changed, rgb = imgui.color_edit3(ident, rgb)
            if changed:
                param.value = tuple(rgb)
        elif kind == ChunkFlags.ParamType.FILE:
            # A path, plus the browse button that makes it a FileBrowser rather than a
            # text field. The dialog honours the param's own pattern/extensions and its
            # DIRECTORY and FILE_EXISTING flags -- see chunk_widget_api.file_dialog_spec.
            _label(name)
            imgui.set_next_item_width(width - 30.0)
            changed, value = imgui.input_text(ident, "" if param.value is None else str(param.value))
            if changed:
                param.value = value
            imgui.same_line(0.0, 4.0)
            if imgui.button("...##browse", imgui.ImVec2(24.0, 0.0)):
                _browse(param)
        else:
            # LINEEDIT, TEXTEDIT and anything unmapped: a text field is a truthful
            # fallback rather than a blank, and every one of those types IS text.
            _label(name)
            imgui.set_next_item_width(width)
            changed, value = imgui.input_text(ident, "" if param.value is None else str(param.value))
            if changed:
                param.value = value
    except Exception:  # noqa: BLE001
        # A param whose value cannot be coerced to what its widget wants must not take the
        # frame down -- one bad chunk would otherwise blank the whole graph. Logged, not
        # swallowed silently.
        logger.exception("could not draw param %r", getattr(param, "name", "?"))
    finally:
        imgui.pop_id()
    return changed


def _visible_children(group):
    return [p for p in group.get_my_params() if not p.has_state(ChunkFlags.State.HIDDEN)]


def _inline_group(group, children, width: float) -> None:
    """A group of same-typed numbers on ONE row: `translate  [x][y][z]`.

    chunks/transforms/transform.py models a vector as a Group of three Sliders, not as a Vector
    param, so flattening gave twelve labelled rows and a node taller than the window. This
    is the whole reason that node was unreadable.
    """
    _label(group.get_label() or group.name)
    each = max(38.0, (width - (len(children) - 1) * 4.0) / len(children))
    for index, child in enumerate(children):
        if index:
            imgui.same_line(0.0, 4.0)
        imgui.push_id(str(id(child)))
        try:
            _numeric(child, f"##{child.name}", each)
        finally:
            imgui.pop_id()


def draw_params(chunk_obj, width: float = 150.0, depth: int = 0) -> None:
    """Every visible param of a chunk, groups kept as groups.

    Flattening was the first version and it is what made a Transform node fifteen rows
    tall. A group whose children are all plain numbers becomes one inline row; any other
    group becomes a titled block.
    """
    for param in _visible_children(chunk_obj):
        if not hasattr(param, "get_my_params"):
            draw_param(param, width)
            continue

        children = _visible_children(param)
        if not children:
            continue
        if all(_is_numeric(child) for child in children) and len(children) <= 4:
            _inline_group(param, children, width)
            continue

        numbers = [c for c in children if _is_numeric(c)]
        others = [c for c in children if not _is_numeric(c)]
        if numbers and len(numbers) <= 4:
            _inline_group(param, numbers, width)
        else:
            imgui.text_colored(imgui.ImVec4(*_GROUP_TITLE), param.get_label() or param.name)
        for child in others:
            draw_params_child(child, width, depth + 1)


def draw_params_child(param, width: float, depth: int) -> None:
    """One child of a group: either a nested group or a plain param."""
    if hasattr(param, "get_my_params"):
        draw_params(param, width, depth)
    else:
        draw_param(param, width)


def visible_params(chunk_obj):
    """Every visible leaf param, groups flattened.

    Kept because the graph frontend's tests use it to assert what is drawable; the drawing
    itself goes through draw_params, which does NOT flatten.
    """
    out = []
    for param in _visible_children(chunk_obj):
        if hasattr(param, "get_my_params"):
            out.extend(visible_params(param))
        else:
            out.append(param)
    return out


def is_editing() -> bool:
    """True while any ImGui widget has keyboard focus.

    The equivalent of Flet's FocusTracker, and a single call rather than a class: ImGui
    already knows, because it owns every widget. Same purpose -- a graph shortcut must not
    fire while a text field is being typed into.
    """
    return imgui.is_any_item_active() or imgui.get_io().want_text_input
