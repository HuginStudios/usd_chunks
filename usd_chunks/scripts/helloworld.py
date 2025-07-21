from usd_chunks.chunk import Chunk

def greeting(api):
    print("Hello World")

def main(api, parent=None):
    # Init a chunk widget
    chunk_api = Chunk(api, parent=parent)
    # Set the name, this is is what will show up
    # as the title name of the widghet.
    chunk_api.name = "Hello World"
    # Set the category. This is optional.
    # It decides which tab your widget will be added to.
    # If no category is applied, it will default to General.
    chunk_api.category = "Examples"
    # Creating a layout is optional. By default it is vertical layout.
    # If you create a layout, like below, the default
    # layout will be a horizontal layout.
    layout = chunk_api.create_layout()
    # Creat a button and attach the callback function directly.
    chunk_api.create_button("Hello world!", greeting, layout=layout)
    return chunk_api