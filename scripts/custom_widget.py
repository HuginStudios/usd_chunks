from usd_chunks import chunk, chunk_interface, chunk_notice, chunk_registry
from PySide6 import QtWidgets, QtCore, QtGui


def customButtonCLicked(chunkItem: chunk.ChunkParamBase, notice, sender):
    print(chunkItem.user_data["customValue"])


class MyWidget(QtWidgets.QPushButton):
    def __init__(self, param: chunk.ChunkParamBase):
        super().__init__("Custom Button")

        self.param = param

        self.clicked.connect(self.param.send_self)


def main(api):
    # An example of using a custom widget
    custom = chunk.ChunkParamBase(chunk_interface.CustomTemplate(), "Custom", user_data={"customValue":"Hello from Custom Widget!"})
    # Set the custom_widget member to the custom Qt widget
    custom.custom_widget = MyWidget(custom)
    custom.add_callback(customButtonCLicked)

    myChunk = chunk.Chunk("Custom Widget", category="Examples", params=[custom])

    return myChunk
