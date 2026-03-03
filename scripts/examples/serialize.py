from usd_chunks import chunk

def slider_changed(owner: chunk.Slider, notice, sender):
    c = owner.get_chunk()
    if not c:
        raise AttributeError("COULD NOT FIND CHUNK")
    c.user_data["data"].va = owner.value

def chunk_changed(owner: chunk.Chunk, notice, sender):
    print("CHUNK CHANGED", owner.user_data)

class SomeCustomData:
    def __init__(self, va, vb):
        self.va = va
        self._vb = vb

    @property
    def vb(self):
        return self._vb

    # def __repr__(self) -> str:
    #     return f"SomeCustomData({self.va}, '{self.vb}')"


def main():
    c = chunk.Chunk("Serial")
    c.add_callback(chunk_changed)

    btn = chunk.Button("Serialize")
    c.add_param(btn)

    menu = chunk.Menu("Menu", menu_items=["A", "B", "C"])
    c.add_param(menu)

    vec = chunk.Vector("Vector", param=chunk.Slider(), length=3, suffixes="ABC")
    c.add_param(vec)

    slider = chunk.Slider("Slider", is_int=True)
    slider.add_callback(slider_changed)
    c.add_param(slider)

    c.user_data["data"] = SomeCustomData(10, "hello")

    return c
