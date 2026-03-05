from usd_chunks import chunk

def slider_changed(owner: chunk.Slider, notice, sender):
    c = owner.get_chunk()
    if not c:
        raise AttributeError("COULD NOT FIND CHUNK")
    c.user_data["data"].va = owner.value

def dep_changed(slider, sender):
    dep = slider.get_dependency(0)
    print("DEP CHANGED...", slider.name)

    return dep.value

def chunk_changed(owner: chunk.Chunk, notice, sender):
    print("CHUNK CHANGED", owner.user_data)

class SomeCustomData:
    def __init__(self, va, vb):
        self.va = va
        self._vb = vb

    @property
    def vb(self):
        return self._vb

    def __repr__(self) -> str:
        return f"SomeCustomData({self.va}, '{self.vb}')"


def main():
    c = chunk.Chunk("Serial")

    # menu = chunk.Menu("Menu", menu_items=["A", "B", "C"])
    # c.add_param(menu)

    # vec = chunk.Vector("Vector", param=chunk.Slider(), length=3, suffixes="ABC")
    # c.add_param(vec)

    sliderA = chunk.Slider("SliderA", is_int=True)
    sliderA.add_callback(slider_changed)

    sliderB = chunk.Slider("SliderB", is_int=True)
    sliderB.add_dependency(sliderA)
    sliderB.set_function("value", dep_changed)

    c.add_params([sliderA, sliderB])
    c.add_callback(chunk_changed, notice_alias=chunk.ChunkNoticeAlias.PARAM_CHANGED, sender=sliderB)

    c.user_data["data"] = SomeCustomData(10, "hello")

    return c
