"""Flet widgets for chunk params -- the Flet counterpart of `chunk_qt`.

The framework was built for this: `chunk_core.ChunkWidgetFactoryBase` is a Protocol
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

import flet as ft

from usd_chunks import chunk, chunk_core
from usd_chunks.chunk_flags import ChunkFlags


class FletParamWidget(chunk_core.ChunkParamWidgetBase):
    """A Flet control bound to one chunk param, in both directions."""

    def __init__(self, param, *args, **kwargs):
        self.control: ft.Control | None = None
        self.label_control = ft.Text("", size=11, color="#b9b9b9")
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

    def on_changed(self, event):
        if self._suppress:
            return
        self.param.value = self.read_event(event)

    def read_event(self, event):
        return event.control.value

    # -- plumbing ----------------------------------------------------------

    def _update(self, control=None):
        """Push a change to the client, if this widget has reached a page yet.

        A control that is not on a page raises when updated, and widgets are built before
        they are added -- so this is the normal case during construction, not an error.
        """
        target = control if control is not None else self.control
        try:
            if target is not None:
                target.update()
        except Exception:  # noqa: BLE001 -- not mounted yet
            pass

    def as_row(self) -> ft.Control:
        """Label above control, which is how the Qt LabeledWidget stacks them."""
        return ft.Column([self.label_control, self.control], spacing=2, tight=True)


class FletSliderWidget(FletParamWidget):
    def build(self):
        param = self.param
        self.control = ft.Slider(
            min=float(getattr(param, "min_value", 0.0) or 0.0),
            max=float(getattr(param, "max_value", 1.0) or 1.0),
            on_change=self.on_changed,
        )

    def apply_value(self, value):
        try:
            self.control.value = float(value)
        except (TypeError, ValueError):
            # An expression (@Node.param@) is a legal param value and not a number. The
            # slider cannot show it; the text field variants can, so it is left alone here.
            pass

    def read_event(self, event):
        value = float(event.control.value)
        return int(round(value)) if getattr(self.param, "is_int", False) else value


class FletTextWidget(FletParamWidget):
    def build(self):
        self.control = ft.TextField(dense=True, on_change=self.on_changed)

    def apply_value(self, value):
        self.control.value = "" if value is None else str(value)


class FletMultilineTextWidget(FletTextWidget):
    def build(self):
        self.control = ft.TextField(multiline=True, min_lines=3, dense=True,
                                    on_change=self.on_changed)


class FletCheckboxWidget(FletParamWidget):
    def build(self):
        self.control = ft.Checkbox(on_change=self.on_changed)

    def apply_value(self, value):
        self.control.value = bool(value)

    def read_event(self, event):
        return bool(event.control.value)


class FletMenuWidget(FletParamWidget):
    def build(self):
        items = [ft.dropdown.Option(str(i)) for i in getattr(self.param, "menu_items", [])]
        self.control = ft.Dropdown(options=items, dense=True, on_change=self.on_changed)

    def apply_value(self, value):
        self.control.value = None if value is None else str(value)


class FletButtonWidget(FletParamWidget):
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

    def build(self):
        self.swatch = ft.Container(width=22, height=22, border_radius=3,
                                   bgcolor="#808080")
        self.field = ft.TextField(dense=True, on_change=self.on_changed, expand=True)
        self.control = ft.Row([self.swatch, self.field], spacing=6, tight=True)

    def apply_value(self, value):
        try:
            red, green, blue = (float(c) for c in list(value)[:3])
        except (TypeError, ValueError):
            self.field.value = "" if value is None else str(value)
            return
        self.field.value = f"{red:.3f}, {green:.3f}, {blue:.3f}"
        self.swatch.bgcolor = "#%02x%02x%02x" % (
            max(0, min(255, int(red * 255))),
            max(0, min(255, int(green * 255))),
            max(0, min(255, int(blue * 255))),
        )

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
    def build(self):
        self.control = ft.Text("", size=11, color="#cccccc")

    def apply_value(self, value):
        self.control.value = "" if value is None else str(value)

    def as_row(self) -> ft.Control:
        return self.control


class ChunkFletWidgetFactory(chunk_core.ChunkWidgetFactoryBase):
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
        ChunkFlags.ParamType.FILE: FletTextWidget,
        ChunkFlags.ParamType.STRING_MENU: FletMenuWidget,
        ChunkFlags.ParamType.CHECKBOX: FletCheckboxWidget,
        ChunkFlags.ParamType.BUTTON: FletButtonWidget,
        ChunkFlags.ParamType.SEPARATOR: FletSeparatorWidget,
        ChunkFlags.ParamType.COLOR_PICKER: FletColorWidget,
    }

    def create_widget(self, param: "chunk.ChunkParamBase"):
        widget_class = self.get_map().get(param.evaluate_type())
        if widget_class is None:
            print(f"[ChunkFlet] no widget for param {param.name!r} "
                  f"({param.evaluate_type()!r})")
            return None
        return widget_class(param)


def build_chunk_controls(chunk_obj, factory: ChunkFletWidgetFactory | None = None):
    """Every param of a chunk as Flet controls, groups included.

    Returns (controls, widgets). The widgets are returned because nothing else holds them:
    each registers a notice callback, and a widget that is collected stops updating -- the
    same lifetime trap plain closures have with add_callback.
    """
    factory = factory or ChunkFletWidgetFactory()
    controls, widgets = [], []

    def walk(owner, depth=0):
        for param in owner.get_my_params():
            if not isinstance(param, chunk_core.ChunkParamBase):
                # A group: a heading, then its contents indented.
                label = param.get_label() if hasattr(param, "get_label") else param.name
                controls.append(ft.Container(
                    ft.Text(label, size=11, weight=ft.FontWeight.W_500, color="#9fb0c0"),
                    padding=ft.Padding.only(left=depth * 10, top=6)))
                walk(param, depth + 1)
                continue
            widget = factory.create_widget(param)
            if widget is None:
                continue
            widgets.append(widget)
            controls.append(ft.Container(widget.as_row(),
                                         padding=ft.Padding.only(left=depth * 10)))

    walk(chunk_obj)
    return controls, widgets
