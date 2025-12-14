from usd_chunks import chunk, chunk_interface, chunk_notice, chunk_registry
from PySide6 import QtWidgets, QtCore, QtGui


def customButtonClicked(notice, sender):
    print(sender.user_data["customValue"])


class MyWidget(chunk_interface.ChunkParamWidget, QtWidgets.QPushButton):
    def __init__(self, param: chunk.ChunkParamBase):
        super().__init__(param)

        # calls param.send_self() when the button is clicked
        self.clicked.connect(self.value_changed)

        # Default stuff. Sets label, layout, icon and initial value 
        self.setup_chunk_widget()

    def value_changed(self, _):
        self.param.send_self()
    
    def set_label(self, text: str):
        self.setText(text)
        pass

    # These are declared in the ChunkParamWidget base class, and can be overridden
    # def get_value(self):
    #     return None
    #
    # def set_value(self, value: Any):
    #     pass
    #
    # def set_icon(self, icon: str):
    #     pass
    #
    # def set_param_layout(self, layout_flags: ChunkFlags.LayoutFlags):
    #     pass


def main(api):
    # An example of using a custom widget
    custom = chunk.Custom("Custom", user_data={"customValue": "Hello from Custom Widget!"})
    # Set the custom_widget member to the custom Qt widget
    custom.custom_widget = MyWidget(custom)

    # When custom.send_self() is called, a notice of type custom.get_notice_alias() is sent. Here we
    # decide to subscribe to that notice, and call customButtonClicked when sent
    custom.add_callback(customButtonClicked)

    myChunk = chunk.Chunk("Custom Widget", category="Examples")
    myChunk.add_param(custom)

    return myChunk
