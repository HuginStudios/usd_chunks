from __future__ import annotations
from usd_chunks import chunk


def clicked(owner: chunk.Button, notice, sender):
    # This is an example of a lambda function being used as a callback. In the first case, no closure args are used, and the lambda will evaluate the slider
    # when executed
    owner.add_callback(
        lambda _owner, _notice, _sender: print("CLICKED LAMBDA 1:", _owner.get_chunk().get_param("slider").value),
        send_on_create=False,
    )

    # Here, a closure argument is used. The value of arg will be saved when serialized
    arg = -1
    if c := owner.get_chunk():
        arg = c.get_param("slider", chunk.is_param_t(float)).value
    owner.add_callback(lambda _owner, _notice, _sender: print("CLICKED LAMBDA 2:", arg), send_on_create=False)


def main():
    my_chunk = chunk.Chunk("closure")
    btn = chunk.Button("button")
    btn.add_callback(clicked, send_on_create=False)

    slider = chunk.Slider("slider")
    my_chunk.add_param(slider)

    my_chunk.add_param(btn)
    return my_chunk


def config():
    return chunk.DefaultConfig(chunk.StandardCategory.EXAMPLES)
