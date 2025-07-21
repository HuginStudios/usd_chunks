"""Custom modules for the Usdview UI Framework.

This module provides a framework for dynamically loading and managing custom UI modules
for the Usdview application. It includes functionality to read Python files from a
specified directory, register them as modules,
and create dockable widgets in the Usdview main window.
"""

from typing import List, Callable, Optional
from pxr.Usdviewq.plugin import PluginContainer
from pxr.Usdviewq import usdviewApi as UsdviewModule
from PySide6 import QtWidgets, QtCore, QtGui
import os
import sys
from importlib import reload, util
from dataclasses import dataclass
import inspect
import glob


@dataclass
class ModuleInfo:
    module: Optional[Callable] = None
    failed: bool = False
    file_name: str = ""


class FileReader:
    """A class to read and register Python Chunk files in specified directories."""

    def __init__(self) -> None:
        """Initialize the FileReader with the working directory."""
        self.working_dirs = FileReader.register_chunk_directories()
        self.registered_modules: List[ModuleInfo] = FileReader.read_usd_chunk_files(self.working_dirs)

    def __repr__(self):
        """Return a string representation of the FileReader."""
        return f"FileReader(registered_modules={self.registered_modules})"

    def __str__(self):
        """Return a string representation of the FileReader."""
        return f"FileReader with {len(self.registered_modules)} registered modules."

    def __len__(self):
        """Return the number of registered modules."""
        return len(self.registered_modules)

    @staticmethod
    def register_chunk_directories() -> List[str]:
        """Register the directory containing the Python modules for this plugin."""
        working_dirs = os.environ.get("USD_CHUNK_DIR", os.path.sep.join([os.path.dirname(__file__), "scripts"])).split(os.pathsep)
        # make sure only reading in the dirs once.
        working_dirs = list(set(working_dirs))
        for working_dir in working_dirs:
            if not os.path.isdir(working_dir):
                print(f"USD_CHUNK_DIR:{working_dir} directory not found.")
                continue
            sys.path.append(working_dir)
        return working_dirs

    @staticmethod
    def read_usd_chunk_files(working_dirs: List[str]) -> List[ModuleInfo]:
        """Read and register Python files in the working directory.

        Args:
            working_dir: The directories containing the Python chunk files to register.

        Returns:
            List[ModuleInfo]: A list of ModuleInfo objects containing
                the loaded modules and their file names.
        """
        registered_modules: callable = []
        for working_dir in working_dirs:
            chunk_python_files = glob.glob(os.path.sep.join([working_dir,"**","*.py"]), recursive = True)
            for module_path in chunk_python_files:
                if not module_path.endswith(".py"):
                    continue
                if not os.path.isfile(module_path):
                    continue
                # Register modules relative to the USD_CHUNK_DIR
                module_name = os.path.relpath(module_path, working_dir).replace(os.path.sep, ".").replace(".py", "")
                file_name = os.path.basename(module_path)
                spec = util.spec_from_file_location(module_name, module_path)
                module = util.module_from_spec(spec)
                
                try:
                    sys.modules[module_name] = module
                    spec.loader.exec_module(module)
                    module_attributes = dir(module)
                    if "main" not in module_attributes:
                        # Only python files with a main functions are valid chunk entrypoints.
                        continue
                    registered_modules.append(
                        ModuleInfo(module=module, file_name=module_name)
                    )
                except Exception as e:
                    print(f"Failed to load module {module_name}: {e}")
                    registered_modules.append(
                        ModuleInfo(module=module, file_name=file_name, failed=True)
                    )
        return registered_modules


class FlowLayout(QtWidgets.QLayout):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.chunk_list = []

    def __del__(self):
        item = self.takeAt(0)
        while item:
            item = self.takeAt(0)

    def addItem(self, item):
        self.chunk_list.append(item)

    def count(self):
        return len(self.chunk_list)

    def itemAt(self, index):
        if 0 <= index < self.count():
            return self.chunk_list[index]
        return None

    def takeAt(self, index):
        if 0 <= index < self.count():
            return self.chunk_list.pop(index)

        return None

    def expandingDirections(self):
        return QtCore.Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        height = self._do_layout(QtCore.QRect(0, 0, width, 0), True)
        return height

    def setGeometry(self, rect):
        super(FlowLayout, self).setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QtCore.QSize()
        for item in self.chunk_list:
            size = size.expandedTo(item.minimumSize())
        size += QtCore.QSize(2 * self.contentsMargins().top(), 2 * self.contentsMargins().top())
        return size

    def _do_layout(self, rect, test_only):
        x = rect.x()
        y = rect.y()
        line_height = 0
        spacing = self.spacing()

        for item in self.chunk_list:
            style = item.widget().style()
            layout_spacing_x = style.layoutSpacing(
                QtWidgets.QSizePolicy.ControlType.PushButton, QtWidgets.QSizePolicy.ControlType.PushButton,
                QtCore.Qt.Orientation.Horizontal
            )
            layout_spacing_y = style.layoutSpacing(
                QtWidgets.QSizePolicy.ControlType.PushButton, QtWidgets.QSizePolicy.ControlType.PushButton,
                QtCore.Qt.Orientation.Vertical
            )
            space_x = spacing + layout_spacing_x
            space_y = spacing + layout_spacing_y
            next_x = x + item.sizeHint().width() + space_x
            if next_x - space_x > rect.right() and line_height > 0:
                x = rect.x()
                y = y + line_height + space_y
                next_x = x + item.sizeHint().width() + space_x
                line_height = 0

            if not test_only:
                item.setGeometry(QtCore.QRect(QtCore.QPoint(x, y), item.sizeHint()))

            x = next_x
            line_height = max(line_height, item.sizeHint().height())

        return y + line_height - rect.y()

class ChunkArea(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.widget_holder = QtWidgets.QWidget(self)
        self.flow = FlowLayout()
        self.widget_holder.setLayout(self.flow)

        scroll = QtWidgets.QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.widget_holder)
        self.vbox = QtWidgets.QVBoxLayout()
        self.vbox.addWidget(scroll)
        
        self.setLayout(self.vbox)


class WidgetDocker(QtWidgets.QWidget):
    
    def __init__(self, registered_modules, usdviewApi, parent=None):
        super().__init__(parent)
        self.registered_modules = registered_modules
        self.api = usdviewApi
        self.build()

    def build(self):
        self.chunk_tabs = {}

        for registered_module in self.registered_modules:
            module = registered_module.module
            try:
                widget = module.main(self.api, parent=self)
                if not widget.category in self.chunk_tabs.keys():
                    self.chunk_tabs[widget.category] = ChunkArea(self)
                widget.minimize_button.setChecked(True)
                widget._animate(True)
            except Exception as e:
                print(f"Error creating Chunk {module.__name__}: {e}")
                continue
            self.chunk_tabs[widget.category].flow.addWidget(widget)

        
        tab_widget = QtWidgets.QTabWidget(self)
        for tab_name, chunk_widget in self.chunk_tabs.items():
            tab_widget.addTab(chunk_widget, tab_name)

        self.main_layout = QtWidgets.QVBoxLayout()
        self.main_layout.addWidget(tab_widget)
        self.setLayout(self.main_layout)
        self.show()


class ChunksReader:

    def __init__(self, api: UsdviewModule, file_reader: FileReader) -> None:
        """Initialize the ModuleReaded with the Usdview API instance.

        Args:
            api: The Usdview API instance to use for module creation.
        """
        self.api = api
        self.file_reader = file_reader
        self.registered_modules = self.file_reader.registered_modules
        self.successful_modules = [
            mod for mod in self.registered_modules if not mod.failed
        ]
        self.failed_modules = [mod for mod in self.registered_modules if mod.failed]
        self.mainWindow = api.qMainWindow
        # Create an empty dock widget to hold the modules
        self.module_dock = QtWidgets.QDockWidget("Chunks", self.mainWindow)
        self.widget_docker = WidgetDocker(self.registered_modules, usdviewApi=self.api, parent=self.mainWindow)
        self.module_dock.setWidget(self.widget_docker)
        self.mainWindow.addDockWidget(QtCore.Qt.RightDockWidgetArea, self.module_dock)

