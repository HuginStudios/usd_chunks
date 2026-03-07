from usd_chunks import chunk, chunk_serial


def slider_changed(owner: chunk.Slider, notice, sender):
    c = owner.get_chunk()
    if not c:
        raise AttributeError("COULD NOT FIND CHUNK")
    c.user_data["custom_class"].va = owner.value


def dep_changed(slider: chunk.Slider, sender):
    dep = slider.get_dependency(0)
    assert isinstance(dep, chunk.Slider)

    slider.get_logger().info("dep changed")
    return dep.value


def chunk_changed(owner: chunk.Chunk, notice, sender):
    owner.get_logger().info("changed")


class Foo(chunk_serial.Serializeable):
    def __init__(self, bar):
        self.bar = "hello"
        self.baz = bar
        self.__baz = bar

    def serialize(self, glb):
        if (res := super().serialize(glb)) and isinstance(res, chunk_serial.SerializedClass):
            res.kwargs["bar"] = self.baz
        return res

    def __repr__(self):
        return str(self.__dict__)


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

    listGroup = chunk.ListGroup("listGroup")
    listGroup.add_param(chunk.Slider("Slider"))
    listGroup.size = 1
    c.add_param(listGroup)

    menu = chunk.Menu("Menu", menu_items=["A", "B", "C"])
    c.add_param(menu)

    vec = chunk.Vector("Vector", param=chunk.Slider(), length=3, suffixes="ABC")
    c.add_param(vec)

    sliderA = chunk.Slider("SliderA", is_int=True)
    sliderA.add_callback(slider_changed)

    sliderB = chunk.Slider("SliderB", is_int=True)
    sliderB.add_dependency(sliderA)
    sliderB.set_function("value", dep_changed)

    c.add_params([sliderA, sliderB])
    c.add_callback(chunk_changed, notice_alias=chunk.ChunkNoticeAlias.PARAM_CHANGED)

    c.user_data["custom_class"] = SomeCustomData(10, "hello")
    c.user_data["foo"] = Foo(10)

    return c


def config():
    return chunk.DefaulConfig(load=True, category=chunk.StandardCategory.EXAMPLES)
