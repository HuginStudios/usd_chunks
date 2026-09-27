"""Flet widgets for chunk params -- the Flet counterpart of `chunk_qt`.

The framework was built for this: `chunk_widget_api.ChunkWidgetFactoryBase` is a Protocol
documented as "implemented for any specific UI library", and every param already declares
what it is through `ChunkFlags.ParamType` rather than through a Qt class. So a second
toolkit needs a factory and a widget per type, and nothing else.

One structural difference from Qt, forced by Flet. A Qt widget subclasses both the toolkit
class and `ChunkParamWidgetBase`; Flet's controls are not designed to be mixed into
someone else's hierarchy, so these widgets *hold* a control instead of being one. The
bridge is the same either way -- `get_value` / `set_value` and the ATTRIBUTE_CHANGED
callback the base class registers -- and `.control` is what a layout adds.

The loop that has to stay broken: `set_value` only ever touches the control. Writing back
to the param there would be re-entered by the notice that write produces.
"""
from __future__ import annotations

import logging

import flet as ft

from usd_chunks import chunk, chunk_core, chunk_widget_api
from usd_chunks.chunk_flags import ChunkFlags

logger = logging.getLogger(__name__)


#: Row metrics. Flet lays out at render time and gives no way to ask a control how tall it
#: ended up, so the heights a port needs to line up against have to be declared rather than
#: measured. Qt reads live widget geometry instead -- same intent, different means.
class FocusTracker:
    """How many param controls currently hold keyboard focus.

    A graph frontend puts its shortcuts on the page, because Flet has no per-control key
    handling -- so Delete would delete the selected NODE while you are editing a text
    field, and D would toggle disable instead of typing a "d". The widgets know when they
    are being typed into and nothing else does, so they report it here and the graph asks.

    An object rather than a module global: two graph windows in one process shared a
    single counter, so typing in one suppressed the other's shortcuts, and a widget
    destroyed while focused leaked its count for the life of the process. Each view owns
    one, and it dies with the widgets that feed it.
    """

    __slots__ = ("_count",)

    def __init__(self):
        self._count = 0

    def enter(self) -> None:
        self._count += 1

    def leave(self) -> None:
        # Never below zero: a stray blur would otherwise make it take two focuses to
        # register one, and the guard would quietly stop working.
        self._count = max(0, self._count - 1)

    def is_editing(self) -> bool:
        return self._count > 0


LABEL_H = 14.0
ROW_GAP = 3.0
GROUP_HEADER_H = 22.0


class FletParamWidget(chunk_widget_api.ChunkParamWidgetBase):
    """A Flet control bound to one chunk param, in both directions."""

    #: Height of this widget's control, label excluded.
    CONTROL_H = 30.0
    #: Whether a label is stacked above it.
    HAS_LABEL = True

    @classmethod
    def row_height(cls) -> float:
        return (LABEL_H if cls.HAS_LABEL else 0.0) + cls.CONTROL_H

    def __init__(self, param, *args, **kwargs):
        self.control: ft.Control | None = None
        self.label_control = ft.Text("", size=10, color="#7a7a7a")
        self._suppress = False
        super().__init__(param, *args, **kwargs)
        self.build()
        self.setup_chunk_widget()

    # -- to be provided by each concrete widget ----------------------------

    def build(self):
        """Create self.control. Called once, before the param's values are applied."""
        raise NotImplementedError

    # -- param -> widget ---------------------------------------------------

    def set_value(self, value):
        if self.control is None:
            return
        # Guarded because assigning to a Flet control fires its own change event on some
        # controls; without this a param change becomes a widget change becomes a param
        # change, which is the loop the base class docs warn about.
        self._suppress = True
        try:
            self.apply_value(value)
        finally:
            self._suppress = False
        self._update()

    def apply_value(self, value):
        self.control.value = value

    def get_value(self):
        return None if self.control is None else self.control.value

    def set_label(self, text: str):
        self.label_control.value = text or ""
        self.label_control.visible = bool(text)
        self._update(self.label_control)

    def set_icon(self, icon: str):
        # Flet takes icon names, not paths, and a chunk's icon is a path. Ignored rather
        # than guessed at: showing the wrong glyph is worse than showing none.
        pass

    def set_param_layout(self, layout_flags):
        pass

    def set_hidden(self, val: bool):
        if self.control is not None:
            self.control.visible = not val
            self._update()

    def set_enabled(self, val: bool):
        if self.control is not None:
            self.control.disabled = not val
            self._update()

    def update_from_state(self):
        state = getattr(self.param, "state", None)
        if state is None:
            return
        self.set_hidden(bool(state & ChunkFlags.State.HIDDEN))
        self.set_enabled(not (state & ChunkFlags.State.DISABLED))

    # -- widget -> param ---------------------------------------------------

    #: Set by build_chunk_controls. None when a widget is built outside a view, in which
    #: case focus simply goes unreported -- there is no graph listening for it.
    focus: FocusTracker | None = None

    def on_focus(self, _event):
        if self.focus is not None:
            self.focus.enter()

    def on_blur(self, _event):
        if self.focus is not None:
            self.focus.leave()

    def _typing(self, control):
        """Wire a control's focus signals, if it has them. Called by the widgets whose
        control accepts typed text -- a Checkbox or a Button cannot swallow a shortcut."""
        if hasattr(control, "on_focus"):
            control.on_focus = self.on_focus
            control.on_blur = self.on_blur
        return control

    def on_changed(self, event):
        if self._suppress:
            return
        self.param.value = self.read_event(event)

    def read_event(self, event):
        return event.control.value

    # -- plumbing ----------------------------------------------------------

    #: Set once the controls are actually on a page. Until then update() must not be
    #: called at all: on an unmounted control it does not fail fast, it can block waiting
    #: for a client round-trip that will never come -- and since every widget sets its
    #: initial value during construction, that is a hang before the first frame is ever
    #: drawn. Headless never saw it, because with no page at all update() fails instantly.
    live = False

    def _update(self, control=None):
        """Push a change to the client, if the widget is on a page."""
        if not self.live:
            return
        target = control if control is not None else self.control
        if target is None:
            return
        try:
            target.update()
        except (RuntimeError, AssertionError):
            # "Control must be added to the page first" -- the widget outlived the page,
            # or `live` was set before this particular control was mounted. Narrowed from
            # bare Exception, which also swallowed real errors raised while rendering.
            pass

    def as_row(self) -> ft.Control:
        """Label above control, which is how the Qt LabeledWidget stacks them."""
        return ft.Column([self.label_control, self.control], spacing=1, tight=True)


class FletSliderWidget(FletParamWidget):
    CONTROL_H = 26.0
    def build(self):
        param = self.param
        self.readout = ft.Text("", size=10, color="#cab46a", width=52,
                               text_align=ft.TextAlign.RIGHT)
        self.slider = ft.Slider(
            min=float(getattr(param, "min_value", 0.0) or 0.0),
            max=float(getattr(param, "max_value", 1.0) or 1.0),
            on_change=self.on_changed, expand=True, height=26,
        )
        # A bare Flet slider shows no number. Qt's does, and a param you can only read by
        # dragging it is not the same control.
        self.control = ft.Row([self.slider, self.readout], spacing=4, tight=True)

    def apply_value(self, value):
        try:
            self.slider.value = float(value)
        except (TypeError, ValueError):
            # An expression (@Node.param@) is a legal param value and not a number, so the
            # slider cannot show it -- but the readout can, which is better than nothing.
            self.readout.value = str(value)
            return
        self.readout.value = ("%d" if getattr(self.param, "is_int", False) else "%.3f") % (
            self.slider.value)

    def get_value(self):
        return self.slider.value

    def read_event(self, event):
        value = float(event.control.value)
        return int(round(value)) if getattr(self.param, "is_int", False) else value


class FletFileWidget(FletParamWidget):
    """A path field with the browse button that makes it a FileBrowser.

    What the dialog asks for comes from chunk_widget_api.file_dialog_spec, shared with the
    Qt and ImGui frontends: the param's own pattern and extensions, and its DIRECTORY and
    FILE_EXISTING flags. Rendering a FileBrowser as a plain text field -- which is what
    this frontend did -- left no way to pick a file at all.
    """

    CONTROL_H = 30.0

    def build(self):
        self.field = self._typing(ft.TextField(
            dense=True, on_change=self.on_changed, expand=True, text_size=11,
            content_padding=ft.Padding.symmetric(vertical=6, horizontal=8),
            filled=True, fill_color="#202020", border_color="#404040",
            focused_border_color="#cab46a", border_radius=3, height=30))
        # No on_result in Flet 0.80: the picker's methods are coroutines that RETURN the
        # chosen path, so browse() is async and applies the result itself.
        self.picker = ft.FilePicker()
        self.control = ft.Row(
            [self.field, ft.IconButton(icon=ft.Icons.FOLDER_OPEN, icon_size=16,
                                       tooltip="Browse", on_click=self.browse)],
            spacing=4, tight=True)

    async def browse(self, _event):
        """Open the dialog this param asked for and apply what comes back.

        Async because Flet 0.80's picker methods are coroutines returning the result --
        there is no on_result callback to register.
        """
        spec = chunk_widget_api.file_dialog_spec(self.param)
        # Flet wants bare extensions, not globs.
        extensions = [p.rsplit(".", 1)[-1] for p in spec.patterns if "." in p] or None
        if spec.directory:
            chosen = await self.picker.get_directory_path(dialog_title=spec.title)
        elif spec.must_exist:
            chosen = await self.picker.pick_files(
                dialog_title=spec.title, allow_multiple=False,
                allowed_extensions=extensions)
        else:
            chosen = await self.picker.save_file(
                dialog_title=spec.title, allowed_extensions=extensions)
        self.apply_picked(chosen)

    def apply_picked(self, chosen) -> None:
        """Write a picked path back. pick_files hands back a LIST of files; the other two
        hand back a path or None."""
        if isinstance(chosen, (list, tuple)):
            chosen = chosen[0] if chosen else None
        path = getattr(chosen, "path", chosen)
        if path:
            self.param.value = path

    def apply_value(self, value):
        self.field.value = "" if value is None else str(value)


class FletTextWidget(FletParamWidget):
    CONTROL_H = 30.0
    def build(self):
        self.control = self._typing(ft.TextField(
            dense=True, on_change=self.on_changed, text_size=11,
            content_padding=ft.Padding.symmetric(vertical=6, horizontal=8),
            filled=True, fill_color="#202020", border_color="#404040",
            focused_border_color="#cab46a", border_radius=3, height=30))

    def apply_value(self, value):
        self.control.value = "" if value is None else str(value)


class FletMultilineTextWidget(FletTextWidget):
    CONTROL_H = 76.0
    def build(self):
        self.control = self._typing(ft.TextField(
            multiline=True, min_lines=3, dense=True, on_change=self.on_changed,
            text_size=11, filled=True, fill_color="#202020", border_color="#404040",
            focused_border_color="#cab46a", border_radius=3,
            content_padding=ft.Padding.all(8)))


class FletCheckboxWidget(FletParamWidget):
    CONTROL_H = 26.0
    def build(self):
        self.control = ft.Checkbox(on_change=self.on_changed)

    def apply_value(self, value):
        self.control.value = bool(value)

    def read_event(self, event):
        return bool(event.control.value)


class FletMenuWidget(FletParamWidget):
    CONTROL_H = 34.0
    def build(self):
        items = [ft.dropdown.Option(str(i)) for i in getattr(self.param, "menu_items", [])]
        # on_select, not on_change: Flet 0.80's Dropdown has no on_change at all and
        # raises on the keyword. It does have on_focus/on_blur, so it takes part in the
        # is_editing() guard like the text fields -- an open dropdown must not let a
        # keystroke through to the graph's shortcuts.
        self.control = self._typing(ft.Dropdown(
            options=items, dense=True, on_select=self.on_changed, text_size=11,
            filled=True, fill_color="#202020", border_color="#404040",
            border_radius=3, content_padding=ft.Padding.symmetric(vertical=4, horizontal=8)))

    def apply_value(self, value):
        self.control.value = None if value is None else str(value)


class FletButtonWidget(FletParamWidget):
    CONTROL_H = 34.0
    HAS_LABEL = False
    def build(self):
        self.control = ft.ElevatedButton(content=self.param.get_label() or self.param.name,
                                         on_click=self.on_clicked)

    def apply_value(self, value):
        pass

    def get_value(self):
        return None

    def set_label(self, text: str):
        # A button carries its label on itself rather than above it.
        self.control.content = text or self.param.name
        self.label_control.visible = False
        self._update()

    def on_clicked(self, _event):
        # A Button's "value" is the act of pressing it: the param's callbacks fire off the
        # value change, so toggling it is what invokes them.
        self.param.value = not bool(self.param.value)

    def as_row(self) -> ft.Control:
        return self.control


class FletColorWidget(FletParamWidget):
    """No native colour picker in Flet, so: a swatch plus editable "r, g, b"."""
    CONTROL_H = 30.0

    def build(self):
        self.swatch = ft.Container(width=22, height=22, border_radius=3,
                                   bgcolor="#808080")
        self.field = self._typing(
            ft.TextField(dense=True, on_change=self.on_changed, expand=True))
        self.control = ft.Row([self.swatch, self.field], spacing=6, tight=True)

    def apply_value(self, value):
        try:
            red, green, blue = (float(c) for c in list(value)[:3])
        except (TypeError, ValueError):
            self.field.value = "" if value is None else str(value)
            return
        self.field.value = f"{red:.3f}, {green:.3f}, {blue:.3f}"
        channels = (max(0, min(255, int(c * 255))) for c in (red, green, blue))
        self.swatch.bgcolor = "#" + "".join(f"{c:02x}" for c in channels)

    def get_value(self):
        return self.read_text(self.field.value)

    def read_event(self, event):
        return self.read_text(event.control.value)

    @staticmethod
    def read_text(text):
        try:
            parts = [float(p) for p in str(text).replace(",", " ").split()]
        except ValueError:
            return None
        return tuple(parts[:3]) if len(parts) >= 3 else None


class FletSeparatorWidget(FletParamWidget):
    CONTROL_H = 8.0
    HAS_LABEL = False
    def build(self):
        self.control = ft.Divider(height=1, color="#4a4a4a")

    def apply_value(self, value):
        pass

    def get_value(self):
        return None

    def set_label(self, text: str):
        self.label_control.visible = False

    def as_row(self) -> ft.Control:
        return self.control


class FletLabelWidget(FletParamWidget):
    CONTROL_H = 16.0
    HAS_LABEL = False
    def build(self):
        self.control = ft.Text("", size=11, color="#cccccc")

    def apply_value(self, value):
        self.control.value = "" if value is None else str(value)

    def as_row(self) -> ft.Control:
        return self.control


class ChunkFletWidgetFactory(chunk_widget_api.ChunkWidgetFactoryBase):
    """Param type -> Flet widget. The same shape as ChunkQTWidgetFactory, by design.

    Keyed on ChunkFlags.ParamType, which is what params already declare themselves as, so
    neither factory has to know about the other's classes -- or about the chunks.
    """

    widget_map = {
        ChunkFlags.ParamType.FLOAT_SLIDER: FletSliderWidget,
        ChunkFlags.ParamType.INT_SLIDER: FletSliderWidget,
        ChunkFlags.ParamType.FLOAT_SPINBOX: FletTextWidget,
        ChunkFlags.ParamType.INT_SPINBOX: FletTextWidget,
        ChunkFlags.ParamType.LABEL: FletLabelWidget,
        ChunkFlags.ParamType.LINEEDIT: FletTextWidget,
        ChunkFlags.ParamType.TEXTEDIT: FletMultilineTextWidget,
        ChunkFlags.ParamType.FILE: FletFileWidget,
        ChunkFlags.ParamType.STRING_MENU: FletMenuWidget,
        ChunkFlags.ParamType.CHECKBOX: FletCheckboxWidget,
        ChunkFlags.ParamType.BUTTON: FletButtonWidget,
        ChunkFlags.ParamType.SEPARATOR: FletSeparatorWidget,
        ChunkFlags.ParamType.COLOR_PICKER: FletColorWidget,
    }

    def create_widget(self, param: chunk.ChunkParamBase):
        widget_class = self.get_map().get(param.evaluate_type())
        if widget_class is None:
            logger.warning("no Flet widget for param %r (%r)",
                           param.name, param.evaluate_type())
            return None
        return widget_class(param)


class FletGroupWidget:
    """A collapsible group, mirroring Qt's ContainerWidget.

    COLLAPSABLE is a group FLAG (may it fold) and COLLAPSED is a group STATE (is it folded
    now). The state is written back on toggle so the fold survives a rebuild and matches
    what Qt would show for the same chunk -- and it is deliberately only a presentation
    bit: folding a section open does not dirty the chunk or cause a cook.
    """

    ARROW_OPEN, ARROW_SHUT = "▾", "▸"

    def __init__(self, group, content_controls):
        self.group = group
        self.collapsible = bool(group.has_flag(ChunkFlags.GroupFlags.COLLAPSABLE))
        self.collapsed = bool(group.has_state(ChunkFlags.State.COLLAPSED))

        self.content = ft.Column(content_controls, spacing=3, tight=True,
                                 visible=not (self.collapsible and self.collapsed))
        self.arrow = ft.Text(self.ARROW_SHUT if self.content.visible is False
                             else self.ARROW_OPEN, size=10, color="#cab46a")
        label = group.get_label() if hasattr(group, "get_label") else group.name
        header = ft.Row(
            ([self.arrow] if self.collapsible else [])
            + [ft.Text(label or "", size=11, weight=ft.FontWeight.W_500, color="#cccccc")],
            spacing=4, tight=True)
        self.header = ft.Container(
            header, padding=ft.Padding.only(top=6, bottom=2),
            on_click=self.toggle if self.collapsible else None)
        self.control = ft.Column([self.header, self.content], spacing=0, tight=True)

    live = False

    def toggle(self, _event=None):
        self.collapsed = not self.collapsed
        if self.collapsed:
            self.group.set_state(ChunkFlags.State.COLLAPSED)
        else:
            self.group.unset_state(ChunkFlags.State.COLLAPSED)
        self.content.visible = not self.collapsed
        self.arrow.value = self.ARROW_SHUT if self.collapsed else self.ARROW_OPEN
        if not self.live:
            return
        for control in (self.content, self.arrow):
            try:
                control.update()
            except (RuntimeError, AssertionError):
                # Same as FletParamWidget._update: an unmounted control raises RuntimeError
                # rather than failing quietly, and that is the only case worth ignoring.
                pass


def build_chunk_controls(chunk_obj, factory: ChunkFletWidgetFactory | None = None,
                         focus: FocusTracker | None = None):
    """Every param of a chunk as Flet controls, groups collapsible, nothing hidden.

    Returns (controls, widgets, offsets). The widgets come back because nothing else holds
    them:
    each registers a notice callback, and a widget that is collected stops updating -- the
    same lifetime trap plain closures have with add_callback. Group widgets are included
    for the same reason: they own the collapse toggle.
    """
    factory = factory or ChunkFletWidgetFactory()
    widgets = []
    #: {param name: y of the row's centre}, in the same coordinate space as the body's
    #: contents, so a port can be drawn level with the control it belongs to instead of on
    #: an arbitrary grid.
    offsets: dict[str, float] = {}

    def walk(owner, top: float):
        controls = []
        y = top
        for param in owner.get_my_params():
            if not isinstance(param, chunk_core.ChunkParamBase):
                collapsed = (param.has_flag(ChunkFlags.GroupFlags.COLLAPSABLE)
                             and param.has_state(ChunkFlags.State.COLLAPSED))
                inner, inner_height = ([], 0.0) if collapsed else walk(param, y + GROUP_HEADER_H)
                group = FletGroupWidget(param, inner)
                widgets.append(group)
                controls.append(group.control)
                y += GROUP_HEADER_H + inner_height
                continue
            widget = factory.create_widget(param)
            if widget is not None:
                widget.focus = focus
            if widget is None:
                continue
            widgets.append(widget)
            controls.append(widget.as_row())
            height = type(widget).row_height()
            # The centre of the CONTROL, not of the row: the label sits above it, and a
            # port level with the label points at nothing.
            offsets[param.name] = y + (LABEL_H if type(widget).HAS_LABEL else 0.0)                 + type(widget).CONTROL_H / 2.0
            y += height + ROW_GAP
        return controls, y - top

    controls, _ = walk(chunk_obj, 0.0)
    return controls, widgets, offsets


def file_pickers(widgets) -> list:
    """Every FilePicker the widgets built.

    Flet requires a picker to be on the page (in page.overlay) before it can open one, and
    only the view can put it there -- so they come back with the widgets rather than being
    added by the widget itself.
    """
    return [w.picker for w in widgets if isinstance(w, FletFileWidget)]


def mark_live(widgets) -> None:
    """Let widgets talk to the client, once their controls are on a page.

    Called by the view after page.add. Before that a widget must stay silent: update() on
    an unmounted control can block on a round-trip that never happens, and every widget
    sets its initial value while being built.
    """
    for widget in widgets:
        widget.live = True
