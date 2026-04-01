from usd_chunks import chunk, chunk_serial
import numpy as np


def sliderA_changed(owner: chunk.Slider, notice, sender):
    c = owner.get_chunk()
    if not c:
        raise AttributeError("COULD NOT FIND CHUNK")
    c.user_data["bar"].va = owner.value


def sliderB_changed(owner: chunk.Slider, notice, sender):
    c = owner.get_chunk()
    if not c:
        raise AttributeError("COULD NOT FIND CHUNK")

    dep = owner.get_dependency(0, chunk.is_param_t(int))

    c.user_data["foo"].baz = owner.value
    c.user_data["bar"].baz = np.array((owner.value, dep.value * 2))


def dep_changed(slider: chunk.Slider, sender):
    dep = slider.get_dependency(0, chunk.is_param_t(int))
    return dep.value


def chunk_changed(owner: chunk.Chunk, notice, sender):
    msg = f"Changed: \n\t{str(owner.user_data.get('foo'))}\n\t{str(owner.user_data.get('bar'))}"
    owner.get_logger().info(msg)


class Foo(chunk_serial.Serializeable):
    def __init__(self, bar):
        self.baz = bar

    def serialize(self, glb):
        if (res := super().serialize(glb)) and isinstance(res, chunk_serial.SerializedClass):
            res.kwargs["bar"] = self.baz
        return res

    def __str__(self):
        return f"Foo({self.baz})"


class Bar:
    def __init__(self, va, vb, nparray: np.ndarray):
        self.va = va
        self._vb = vb
        self.baz = nparray

    @property
    def vb(self):
        return self._vb

    def __str__(self) -> str:
        return f"Bar({self.va}, '{self.vb}',\n{self.baz})"


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
    sliderA.add_callback(sliderA_changed)

    sliderB = chunk.Slider("SliderB", is_int=True)
    sliderB.add_dependency(sliderA)
    sliderB.set_function("value", dep_changed)
    sliderB.add_callback(sliderB_changed)

    c.add_params([sliderA, sliderB])
    c.add_callback(chunk_changed, notice_alias=chunk.ChunkNoticeAlias.PARAM_CHANGED)

    c.user_data["foo"] = Foo(10)
    c.user_data["bar"] = Bar(10, "hello", np.ones((2, 1)))

    return c


def config():
    return chunk.DefaulConfig(category=chunk.StandardCategory.EXAMPLES, requirements=["numpy"])
