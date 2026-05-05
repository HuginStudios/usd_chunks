from usd_chunks import chunk


def clicked(owner: chunk.Button, notice, sender):
    owner.add_callback(lambda _owner, _notice, _sender: print("CLICKED LAMBDA"), send_on_create=False)


def main():
    my_chunk = chunk.Chunk("closure")

    btn = chunk.Button("button")
    btn.add_callback(clicked, send_on_create=False)

    my_chunk.add_param(btn)
    return my_chunk


def config():
    return chunk.DefaulConfig(chunk.StandardCategory.EXAMPLES)
