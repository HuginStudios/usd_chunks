from usd_chunks.chunk import Chunk


def clicked(api):
    print("button clicked...")

def main(api, parent=None):
    chunk_api = Chunk(api, parent=parent)
    chunk_api.name = "Example Widget"
    chunk_api.category = "Examples"

    # All widgets are appended in vertical fashion
    # unless provided with a layout.
    chunk_api.create_label("test label:")
    chunk_api.create_button("Click Me!", clicked)
    chunk_api.create_slider(min_value=0, max_value=100, value=50)
    chunk_api.create_checkbox("checkbox test")
    chunk_api.create_line_edit("Enter text...")
    chunk_api.create_combobox(["A", "B", "C"])
    chunk_api.create_text_edit("Test text edit..")
    chunk_api.create_spinbox(min_value=0, max_value=10)
    chunk_api.create_float_spinbox(min_value=0.0, max_value=10.0)
    
    return chunk_api
