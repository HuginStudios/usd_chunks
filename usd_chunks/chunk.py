import os
from PySide6 import QtWidgets, QtCore, QtGui
import inspect
from functools import partial
from typing import Sequence, Tuple, List, Union, Callable, Dict, Any, Optional


class Chunk(QtWidgets.QWidget):
    """
    Chunk is a base class for creating UI components.
    It provides a structure for initializing the API and creating UI Chunks.
    """
    

    def __init__(self, api=None, name="Unknown Widget", parent=None):
        if parent is None:
            parent = api.qMainWindow
        super().__init__(parent)
        self.api = api
        self.name = name
        self.category = "General"
        self.animation_duration = 300
        self.registered_widgets = []

        self.build()

    def build(self):
        # Setup chunk content area
        self.content = QtWidgets.QScrollArea()
        self.content.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.content.setMaximumHeight(0)
        self.content.setMinimumHeight(0)
        self.content_layout = QtWidgets.QVBoxLayout()
        self.content.setLayout(self.content_layout)
        
        # Setup toggle minimize/maximize animation.
        self.toggle_anim = QtCore.QParallelAnimationGroup()
        self.toggle_anim.addAnimation(QtCore.QPropertyAnimation(self, b"minimumHeight"))
        self.toggle_anim.addAnimation(QtCore.QPropertyAnimation(self, b"maximumHeight"))
        self.toggle_anim.addAnimation(QtCore.QPropertyAnimation(self.content, b"maximumHeight"))
        
        # Setup minimize/maximize button
        self.minimize_button = QtWidgets.QToolButton()
        self.minimize_button.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.minimize_button.setSizePolicy(QtWidgets.QSizePolicy.MinimumExpanding, QtWidgets.QSizePolicy.Maximum)
        self.minimize_button.setArrowType(QtCore.Qt.RightArrow)
        self.minimize_button.setText(self.name)
        self.minimize_button.setCheckable(True)
        self.minimize_button.setChecked(False)
        self.minimize_button.clicked.connect(self._animate)

        self.__layout__ = QtWidgets.QVBoxLayout()
        self.__layout__.addWidget(self.minimize_button)
        self.__layout__.addWidget(self.content)
        self.setLayout(self.__layout__)
        self.setMinimumSize(300,10)

    def _animate(self, checked):
        arrow_type = QtCore.Qt.DownArrow if checked else QtCore.Qt.RightArrow
        direction = QtCore.QAbstractAnimation.Forward if checked else QtCore.QAbstractAnimation.Backward
        self.minimize_button.setArrowType(arrow_type)
        self.toggle_anim.setDirection(direction)
        self.toggle_anim.start()

    def update_anim(self):
        self.minimize_button.setText(self.name)
        collapsed_height = self.sizeHint().height() - self.content.maximumHeight()
        content_height = self.content_layout.sizeHint().height()
        for i in range(self.toggle_anim.animationCount()-1):
            animation = self.toggle_anim.animationAt(i)
            animation.setDuration(self.animation_duration)
            animation.setStartValue(collapsed_height)
            animation.setEndValue(collapsed_height + content_height)
        content_animation = self.toggle_anim.animationAt(self.toggle_anim.animationCount() - 1)
        content_animation.setDuration(self.animation_duration)
        content_animation.setStartValue(0)
        content_animation.setEndValue(content_height)
        

    def _find_args_by_name(
        self, callback: Callable, args: Sequence = (), kwargs: dict = {}
    ) -> List:
        """Finds the arguments for a callback function.

        This method inspects the callback function to determine its parameters
        and matches them with the provided args and kwargs. It returns a list of
        arguments that can be passed to the callback.

        Args:
            callback (callable): The callback function to inspect.
            args (Sequence): Positional arguments to match with the callback.
            kwargs (dict): Keyword arguments to match with the callback.
        Returns:
            List: A list of arguments that can be passed to the callback.
        """
        spec = inspect.getfullargspec(callback)
        # Only skip 'self' if callback is a method
        arg_names = spec.args[1:] if inspect.ismethod(callback) else spec.args
        call_args = []
        idx = 0
        for name in arg_names:
            if hasattr(self, name):
                call_args.append(getattr(self, name))
                continue
            k_value =  kwargs.get(name, None)
            if k_value is not None:
                call_args.append(k_value)
            else:
                call_args.append(args[idx])
                idx += 1
        return call_args

    def _add_widget(self, widget, layout=None):
        self.registered_widgets.append(widget)
        if layout is not None:
            layout.addWidget(widget)
        else:
            self.content_layout.addWidget(widget)
        self.update_anim()

    def _set_tooltip(self, widget, kwargs):
        tooltip = kwargs.get("tooltip", None)
        if tooltip:
            widget.setToolTip(tooltip)

    def create_button(self, text="", callback=None, *args, **kwargs):
        """Create a button with the specified text and callback."""
        button = QtWidgets.QPushButton(text, parent=self)
        width = kwargs.get("width", None)
        height = kwargs.get("height", None)
        self._set_tooltip(button, kwargs)
        if width and height:
            button.setFixedSize(QtCore.QSize(width, height))

        icon = kwargs.get("icon", None)
        if icon and os.path.exists(icon):
            qicon = QtGui.QIcon(icon)
            icon_size = QtCore.QSize( 40, 40 )
            button.setIcon(qicon)
            button.setIconSize(icon_size)

        button.adjustSize()
        call_args = self._find_args_by_name(callback, args, kwargs)
        button.clicked.connect(partial(callback, *call_args))
        self.registered_widgets.append(button)
        layout = kwargs.get("layout", None)
        self._add_widget(button, layout)
        return button

    def create_label(self, text, layout=None):
        """Create a label with the specified text."""
        label = QtWidgets.QLabel(text, self)
        self._add_widget(label, layout)
        return label

    def create_filebrowser(self, layout=None):
        """Create a label with the specified text."""
        layout = self.create_layout()
        line_edit = self.create_line_edit("", layout=layout)
        button = self.create_button("Browse",self._open_file_dialog, line_edit, layout=layout)
        return line_edit

    def _open_file_dialog(self, line_edit):
         dialog = QtWidgets.QFileDialog(self)
         if dialog.exec():
            filenames = dialog.selectedFiles()
            if filenames:
                line_edit.setText(filenames[0])

    def create_layout(self, layout_type=None, layout=None):
        """Create a layout of the specified type."""
        if layout_type is None:
            layout_type = QtWidgets.QHBoxLayout
        new_layout = layout_type()
        self.registered_widgets.append(new_layout)
        if layout is not None:
            layout.addLayout(new_layout)
        else:
            self.content_layout.addLayout(new_layout)
        return new_layout

    def create_checkbox(self, text, checked=False, layout=None):
        """Create a checkbox with the specified text and initial state."""
        checkbox = QtWidgets.QCheckBox(text, self)
        checkbox.setChecked(checked)
        self._add_widget(checkbox, layout)
        return checkbox

    def create_slider(self, min_value=0, max_value=100, value=50, layout=None):
        """Create a slider with the specified range and initial value."""
        slider = QtWidgets.QSlider(QtCore.Qt.Horizontal, self)
        slider.setMinimum(min_value)
        slider.setMaximum(max_value)
        slider.setValue(value)
        self._add_widget(slider, layout)
        return slider

    def create_line_edit(self, placeholder_text="", layout=None):
        """Create a line edit with the specified placeholder text."""
        line_edit = QtWidgets.QLineEdit(self)
        line_edit.setPlaceholderText(placeholder_text)
        self._add_widget(line_edit, layout)
        return line_edit

    def create_combobox(self, items=None, default_index=0, layout=None):
        """Create a combobox with the specified items."""
        combobox = QtWidgets.QComboBox(self)
        if items:
            combobox.addItems(items)
        if default_index < combobox.count():
            combobox.setCurrentIndex(default_index)
        else:
            combobox.setCurrentIndex(0)
        self._add_widget(combobox, layout)
        return combobox

    def create_text_edit(self, placeholder_text="", layout=None):
        """Create a text edit with the specified placeholder text."""
        text_edit = QtWidgets.QTextEdit(self)
        text_edit.setPlaceholderText(placeholder_text)
        self._add_widget(text_edit, layout)
        return text_edit

    def create_spinbox(self, min_value=0, max_value=100, value=0, layout=None):
        """Create a spinbox with the specified range and initial value."""
        spinbox = QtWidgets.QSpinBox(self)
        spinbox.setMinimum(min_value)
        spinbox.setMaximum(max_value)
        spinbox.setValue(value)
        self._add_widget(spinbox, layout)
        return spinbox

    def create_float_spinbox(self, min_value=None, max_value=None, value=0.0, layout=None):
        """Create a float spinbox with the specified range and initial value."""
        float_spinbox = QtWidgets.QDoubleSpinBox(self)
        if min_value is not None:
            float_spinbox.setMinimum(min_value)
        if max_value is not None:
            float_spinbox.setMaximum(max_value)
        float_spinbox.setValue(value)
        self._add_widget(float_spinbox, layout)
        return float_spinbox