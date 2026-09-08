"""Dear ImGui param widgets — the third toolkit, and the one that needs the least code.

Immediate mode changes the shape of this layer completely. Qt and Flet both build a widget
OBJECT per param, keep it alive, and register a notice callback so the widget updates when
the param changes underneath it — `chunk_widget_api.ChunkParamWidgetBase` exists to carry
exactly that. Here there is no object and no callback: every frame reads `param.value` and
draws it, so a change made anywhere is on screen on the next frame by construction.

That means this file does NOT implement ChunkWidgetFactoryBase. It is not an oversight —
the base class's whole contract is `set_value`/`get_value` plus a subscription, and an
immediate-mode frontend has nothing to subscribe. What it shares with the other two is the
thing that actually matters: params are dispatched on `ChunkFlags.ParamType`, so this file
knows nothing about specific chunks.
"""
from __future__ import annotations

import logging

from imgui_bundle import imgui

from usd_chunks.chunk_flags import ChunkFlags

logger = logging.getLogger(__name__)

#: Rows are this tall, which the graph needs to place a port beside its control. Declared
#: rather than measured for the same reason Flet declares them: the value is needed before
#: the row is drawn.
ROW_H = 22.0
LABEL_W = 92.0


def _labelled(param) -> str:
    return param.get_label() or param.name


def draw_param(param, width: float = 150.0) -> bool:
    """Draw one param. Returns True if the user changed it.

    Writing back is a plain assignment to `param.value`, the same as every other frontend —
    the notice system does the rest, so this stays ignorant of chunks and of USD.
    """
    kind = param.evaluate_type()
    imgui.push_id(str(id(param)))
    changed = False
    try:
        imgui.set_next_item_width(width)
        if kind in (ChunkFlags.ParamType.FLOAT_SLIDER, ChunkFlags.ParamType.INT_SLIDER):
            value = param.value or 0
            if getattr(param, "is_int", False):
                changed, new = imgui.slider_int(_labelled(param), int(value), -100, 100)
            else:
                changed, new = imgui.slider_float(_labelled(param), float(value), -100.0, 100.0)
            if changed:
                param.value = new

        elif kind == ChunkFlags.ParamType.CHECKBOX:
            changed, new = imgui.checkbox(_labelled(param), bool(param.value))
            if changed:
                param.value = new

        elif kind == ChunkFlags.ParamType.STRING_MENU:
            items = list(getattr(param, "menu_items", None) or [])
            current = str(param.value) if param.value is not None else ""
            index = items.index(current) if current in items else 0
            if items:
                changed, index = imgui.combo(_labelled(param), index, items)
                if changed:
                    param.value = items[index]

        elif kind == ChunkFlags.ParamType.BUTTON:
            if imgui.button(_labelled(param)):
                # A Button param carries no value; toggling is how the other frontends
                # fire it, so the notice fires the same way here.
                param.value = not bool(param.value)
                changed = True

        elif kind == ChunkFlags.ParamType.LABEL:
            imgui.text(str(param.value or _labelled(param)))

        elif kind == ChunkFlags.ParamType.SEPARATOR:
            imgui.separator()

        elif kind == ChunkFlags.ParamType.COLOR_PICKER:
            value = param.value or (0.0, 0.0, 0.0)
            try:
                rgb = [float(value[0]), float(value[1]), float(value[2])]
            except (TypeError, IndexError, ValueError):
                rgb = [0.0, 0.0, 0.0]
            changed, rgb = imgui.color_edit3(_labelled(param), rgb)
            if changed:
                param.value = tuple(rgb)

        else:
            # LINEEDIT, TEXTEDIT, FILE and anything unmapped: a text field is a truthful
            # fallback rather than a blank, and every one of those types IS text.
            text = "" if param.value is None else str(param.value)
            changed, new = imgui.input_text(_labelled(param), text)
            if changed:
                param.value = new
    except Exception:  # noqa: BLE001
        # A param whose value cannot be coerced to what its widget wants must not take the
        # frame down -- one bad chunk would otherwise blank the whole graph. Logged, not
        # swallowed silently.
        logger.exception("could not draw param %r", getattr(param, "name", "?"))
    finally:
        imgui.pop_id()
    return changed


def visible_params(chunk_obj):
    """The params worth drawing, groups flattened, hidden ones dropped."""
    out = []
    for param in chunk_obj.get_my_params():
        if param.has_state(ChunkFlags.State.HIDDEN):
            continue
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
